"""quotum provider profile for Hermes Agent.

A quotum seat holds a Venice key with a daily dollar cap, paid for by the
QUOTUM trading tax; what the seat leaves unspent at the 20:00 UTC bell buys
QUOTUM and burns it. This profile lets Hermes use that key (a seat key, the
seat's agent key or a day key) as a provider:

- inference goes straight to Venice (https://api.venice.ai/api/v1) with the
  key in QUOTUM_SEAT_KEY. quotum never sees a prompt or a reply;
- the model list is the seat's catalog, private and anonymized models only,
  read from https://quotum.org/api/seat/models (no key sent);
- /usage reads https://quotum.org/api/meter/<h>, where h is the first 16 hex
  characters of the key's sha256, or QUOTUM_METER_HANDLE behind a secrets proxy.
  The key itself never leaves the machine
  except to Venice. The request carries the plugin version in a header.

- with QUOTUM_X_SEARCH=1 only: the list of models that support xAI's live
  web and X search, read from https://api.venice.ai/api/v1/models (no key
  sent), so the search is asked for only on those models (the Grok family).

With QUOTUM_PAYG_KEY set, PAYG takes precedence: inference goes through
https://quotum.org/api/payg/v1 with a project bearer, the public /models
and /status describe intake, and authenticated /meter returns balance
and billing receipts. The PAYG gateway processes prompts and replies.
No telemetry, no wallet code, no self-update.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import json
import logging
import math
import os
import threading
import sys
import time
import urllib.error
import urllib.request
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any
from pathlib import Path

from providers import register_provider
from providers.base import ProviderProfile
from . import payg

logger = logging.getLogger(__name__)

VERSION = payg.VERSION
PROVIDER_ID = "quotum"
API_KEY_ENV = "QUOTUM_SEAT_KEY"
PAYG_KEY_ENV = payg.KEY_ENV
PAYG_BASE = payg.BASE
X_SEARCH_ENV = "QUOTUM_X_SEARCH"
VENICE_BASE = "https://api.venice.ai/api/v1"
SITE = "https://quotum.org"
TIMEOUT = 8.0


# the dashboard is loaded outside this package; share its handle validation state.
_handle_name = "_quotum_meter_handle"
if _handle_name not in sys.modules:
    _handle_spec = importlib.util.spec_from_file_location(_handle_name, Path(__file__).with_name("meter_handle.py"))
    _handle_module = importlib.util.module_from_spec(_handle_spec)
    sys.modules[_handle_name] = _handle_module
    _handle_spec.loader.exec_module(_handle_module)
else:
    _handle_module = sys.modules[_handle_name]
key_hash = _handle_module.key_hash
PROXY_MESSAGE = _handle_module.PROXY_MESSAGE


def meter_handle(key: str) -> str | None:
    return _handle_module.meter_handle(key, payg.secret("QUOTUM_METER_HANDLE", strip=False))


def payg_key() -> str:
    return payg.secret(PAYG_KEY_ENV)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "quotum metadata redirects are disabled", headers, fp)


def _get_json(url: str, timeout: float = TIMEOUT, *, bearer: str | None = None) -> Any:
    request = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": f"hermes-quotum/{VERSION}", "x-quotum-plugin": VERSION})
    if bearer:
        request.add_unredirected_header("authorization", f"Bearer {bearer}")
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:  # noqa: S310 - fixed https hosts
        return json.load(response)


_X_MODELS: set[str] | None = None


def x_search_on() -> bool:
    return os.environ.get(X_SEARCH_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def x_search_models(timeout: float = TIMEOUT) -> set[str]:
    """Venice's models with supportsXSearch, read once; an empty set (not kept) when the catalog does not answer."""
    global _X_MODELS
    if _X_MODELS is None:
        try:
            rows = _get_json(f"{VENICE_BASE}/models?type=text", timeout=timeout).get("data") or []
        except Exception as exc:
            logger.debug("quotum: venice catalog unavailable")
            return set()
        try:
            _X_MODELS = {r["id"] for r in rows if isinstance(r, dict) and payg.model_id(r.get("id"), payg.credentials())
                         and ((r.get("model_spec") or {}).get("capabilities") or {}).get("supportsXSearch")}
        except (AttributeError, TypeError):
            return set()
    return _X_MODELS


def _without_web_search(content: bytes) -> bytes | None:
    """A chat request asking for Venice's X search, without Hermes' own web_search tool; None to leave it as is.

    With enable_x_search Venice adds xAI's search tool named web_search, and a request that already has one is refused
    (400 "Duplicate tool names: web_search"). The model then searches the web and X through Venice instead.
    """
    try:
        data = json.loads(content)
    except ValueError:
        return None
    if not isinstance(data, dict) or not (data.get("venice_parameters") or {}).get("enable_x_search") or not data.get("tools"):
        return None
    tools = [t for t in data["tools"] if ((t or {}).get("function") or {}).get("name") != "web_search"]
    if len(tools) == len(data["tools"]):
        return None
    if tools:
        data["tools"] = tools
    else:
        data.pop("tools", None)
        data.pop("tool_choice", None)
    return json.dumps(data).encode()



def _error_headers(headers: Any, *, terminal: bool) -> dict[str, str]:
    """Keep retry controls, never arbitrary upstream diagnostics in SDK logs."""
    from email.utils import format_datetime, parsedate_to_datetime

    if terminal:
        return {"x-should-retry": "false"}
    safe = {}
    if headers.get("x-should-retry") in {"true", "false"}:
        safe["x-should-retry"] = headers["x-should-retry"]
    for name in ("retry-after", "retry-after-ms"):
        value = headers.get(name)
        if value is None:
            continue
        try:
            number = float(value)
            if math.isfinite(number) and number >= 0:
                safe[name] = str(number)
        except ValueError:
            if name == "retry-after":
                try:
                    safe[name] = format_datetime(parsedate_to_datetime(value))
                except (TypeError, ValueError, OverflowError):
                    pass
    return safe


def _stream_requested(request: Any) -> bool:
    try:
        body = json.loads(request.read())
        return isinstance(body, dict) and body.get("stream") is True
    except ValueError:
        return False


def _private_trace(name: str, info: dict) -> None:
    # httpcore invokes the request-local trace before logging its info dict.
    # Started events share kwargs with the actual I/O call; leave them intact.
    # Completed and failed events carry raw headers or exception text.
    if not name.endswith(".started"):
        info.clear()


def _safe_stream(response: Any, *, payg_mode: bool, streaming: bool = False, keys: tuple[str, ...] = ()) -> Any:
    """Only the declared chat schema reaches the SDK; failures use local text."""
    import httpx

    media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    is_sse = media == "text/event-stream" or streaming
    headers = {"content-type": "text/event-stream" if streaming else "application/json"}

    def error_body() -> dict:
        message = ("payg response failed. check your project and https://quotum.org/payg." if payg_mode else
                   "quotum response failed. check your request and https://quotum.org/seat.")
        return {"error": {"message": message}}

    redactor = payg.DeltaRedactor(keys)

    def encode(body: dict) -> bytes:
        return ("data: " + json.dumps(body) + "\n\n").encode()

    def flush() -> bytes:
        body = redactor.flush()
        return encode(body) if body is not None else b""

    def frame(lines: list[str]) -> bytes:
        data = []
        event = ""
        for line in lines:
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "data":
                data.append(value)
            elif field == "event":
                event = value
        if not data:
            return b""
        text = "\n".join(data)
        if text == "[DONE]":
            return flush() + b"data: [DONE]\n\n"
        try:
            value = json.loads(text)
        except ValueError:
            value = None
        body = payg.chat(value, keys) if event in ("", "message") else None
        if body is None:
            redactor.pending.clear()
            return encode(error_body())
        return encode(redactor.apply(body))

    class SafeStream(httpx.SyncByteStream):
        def __iter__(self):
            try:
                if media == "application/json" or not is_sse:
                    try:
                        value = json.loads(response.read())
                    except (ValueError, UnicodeError):
                        value = None
                    body = payg.chat(value, keys)
                    safe = json.dumps(body if body is not None else error_body())
                    yield ("data: " + safe + "\n\n" if streaming else safe).encode()
                    return
                lines = []
                for line in response.iter_lines():
                    if line:
                        lines.append(line)
                    else:
                        yield frame(lines)
                        lines = []
                if lines:
                    yield frame(lines)
                yield flush()
            except Exception:
                raise httpx.ReadError("quotum response could not be read") from None

        def close(self):
            payg.close_quietly(response)

    return httpx.Response(response.status_code, headers=headers, stream=SafeStream())


def _quotum_client(client_kwargs: dict[str, Any]) -> Any:
    """Keep SDK errors informative and terminal, using only the inference key's hash."""
    import httpx
    import openai

    class QuotumTransport(httpx.HTTPTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.scheme != "https" or request.url.host != "api.venice.ai" or request.url.port not in (None, 443):
                raise ValueError("quotum inference requires https://api.venice.ai")
            auth = request.headers.get("authorization", "")
            key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            if request.method == "POST" and request.url.path.endswith("/chat/completions"):
                content = request.read()
                try:
                    model = json.loads(content).get("model")
                except (ValueError, AttributeError):
                    model = None
                try:
                    preflight(key, model, _SESSION.get())
                except Exception:
                    logger.debug("quotum: preflight unavailable; request continues")
                body = _without_web_search(content) if x_search_on() else None
                if body is not None:
                    headers = [(k, v) for k, v in request.headers.items() if k.lower() != "content-length"]
                    request = httpx.Request(request.method, request.url, headers=headers, content=body, extensions=request.extensions)
            request.extensions["trace"] = _private_trace
            try:
                response = super().handle_request(request)
            except httpx.TimeoutException:
                raise httpx.ConnectTimeout("quotum inference timed out") from None
            except Exception:
                raise httpx.ConnectError("quotum inference transport failed") from None
            if not 200 <= response.status_code < 300:
                status = response.status_code
                headers = _error_headers(response.headers, terminal=status in (401, 402))
                payg.close_quietly(response)
                message = cap_spent_message(key) if status == 402 else AUTH_MESSAGE if status == 401 else (
                    f"quotum request failed (http {status}). check your request and https://quotum.org/seat."
                )
                kind = "quotum_cap_spent" if status == 402 else "quotum_key_ended" if status == 401 else "quotum_request_failed"
                return httpx.Response(status, headers=headers,
                                      json={"error": {"message": message, "type": kind}}, request=request)
            return _safe_stream(response, payg_mode=False, streaming=_stream_requested(request), keys=payg.credentials(key))

    known = set(inspect.signature(openai.OpenAI.__init__).parameters) - {"self", "http_client"}
    return openai.OpenAI(http_client=openai.DefaultHttpxClient(transport=QuotumTransport(), trust_env=False, follow_redirects=False), **{k: v for k, v in client_kwargs.items() if k in known})


def _payg_client(client_kwargs: dict[str, Any]) -> Any:
    """PAYG always uses its project bearer, unique admission id and no SDK retries."""
    import httpx
    import openai
    import uuid

    class PaygTransport(httpx.BaseTransport):
        def __init__(self):
            self.transport = httpx.HTTPTransport(trust_env=False)

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.scheme != "https" or request.url.host != "quotum.org" or request.url.port not in (None, 443) or not request.url.path.startswith("/api/payg/v1/"):
                raise ValueError("quotum payg inference requires https://quotum.org/api/payg/v1")
            auth = request.headers.get("authorization", "")
            key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            if request.method == "POST" and request.url.path.endswith("/chat/completions"):
                request.headers.setdefault("idempotency-key", str(uuid.uuid4()))
                # Seat preflight is deliberately skipped: its meter and prices do not apply to PAYG.
            request.extensions["trace"] = _private_trace
            try:
                response = self.transport.handle_request(request)
            except httpx.TimeoutException:
                raise httpx.ConnectTimeout("quotum payg inference timed out") from None
            except Exception:
                raise httpx.ConnectError("quotum payg inference transport failed") from None
            if not 200 <= response.status_code < 300:
                try:
                    body = json.loads(response.read())
                except Exception:
                    body = None
                message = payg.error_message(response.status_code, body)
                headers = _error_headers(response.headers, terminal=True)
                payg.close_quietly(response)
                headers["x-should-retry"] = "false"
                return httpx.Response(response.status_code, headers=headers,
                                      json={"error": {"code": payg.public_error_code(response.status_code, body), "message": message}}, request=request)
            return _safe_stream(response, payg_mode=True, streaming=_stream_requested(request), keys=payg.credentials(key))

        def close(self):
            payg.close_quietly(self.transport)

    known = set(inspect.signature(openai.OpenAI.__init__).parameters) - {"self", "http_client"}
    kwargs = {k: v for k, v in client_kwargs.items() if k in known}
    kwargs.update(api_key=payg_key(), base_url=PAYG_BASE, max_retries=0)
    return openai.OpenAI(http_client=openai.DefaultHttpxClient(transport=PaygTransport(), follow_redirects=False, trust_env=False), **kwargs)


AUTH_MESSAGE = "the quotum key was rotated or ended, or the seat was freed. get the key again at https://quotum.org/seat."
_SESSION: ContextVar[str | None] = ContextVar("quotum_session", default=None)
_CACHE_LOCK = threading.RLock()
_METER_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
_CATALOG_CACHE: tuple[float, list[dict[str, Any]]] | None = None
_WARNED: set[str] = set()


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _cache_seconds() -> float:
    try:
        minutes = float(os.environ.get("QUOTUM_PREFLIGHT_MINUTES", "5"))
        return max(1.0, minutes) * 60 if math.isfinite(minutes) else 300.0
    except ValueError:
        return 300.0


def meter(key: str) -> dict[str, Any] | None:
    """Cache successes and failures per meter handle, including across /usage and 402s."""
    if not key:
        return None
    h = meter_handle(key)
    if h is None:
        return None
    with _CACHE_LOCK:
        now = time.monotonic()
        cached = _METER_CACHE.get(h)
        if cached is not None and now - cached[0] < _cache_seconds():
            return cached[1]
        try:
            value = _get_json(f"{SITE}/api/meter/{h}")
            value = payg.seat_meter(value, payg.credentials(key))
        except Exception:
            value = None
        _METER_CACHE[h] = (time.monotonic(), value)
        return value


def catalog(timeout: float = TIMEOUT) -> list[dict[str, Any]]:
    global _CATALOG_CACHE
    with _CACHE_LOCK:
        if _CATALOG_CACHE is not None and time.monotonic() - _CATALOG_CACHE[0] < _cache_seconds():
            return _CATALOG_CACHE[1]
        try:
            rows = _get_json(f"{SITE}/api/seat/models", timeout=timeout).get("models") or []
            rows = [{"id": r["id"], "privacy": r["privacy"], "in": payg._amount(r.get("in")), "out": payg._amount(r.get("out"))}
                    for r in rows if isinstance(r, dict) and payg.model_id(r.get("id"), payg.credentials())
                    and r.get("privacy") in ("private", "anonymized") and not r.get("offline")]
        except Exception:
            rows = []
        _CATALOG_CACHE = (time.monotonic(), rows)
        return rows


def _number(value: Any) -> bool:
    return payg._amount(value) is not None


def _turn_cost(row: dict[str, Any]) -> float | None:
    # Same defined turn as quotum.org/models: 10k input + 1k output, USD per million.
    if _number(row.get("in")) and _number(row.get("out")):
        return (row["in"] * 10_000 + row["out"] * 1_000) / 1_000_000
    return None


def preflight(key: str, model: str | None, session_id: str | None) -> None:
    """Advisory only. No session identity means no session-scoped warning."""
    if not _enabled("QUOTUM_PREFLIGHT") or not key or not model or not session_id:
        return
    identity = session_id
    with _CACHE_LOCK:
        if meter_handle(key) is None:
            if identity not in _WARNED:
                _WARNED.add(identity)
                logger.warning(PROXY_MESSAGE)
            return
        if identity in _WARNED:
            return
        rows = catalog()
        chosen = next((r for r in rows if r["id"] == model), None)
        cost = _turn_cost(chosen) if chosen else None
        if cost is None or cost <= 0:
            return
        cheaper = [(price, r["id"]) for r in rows if r.get("privacy") == "private" and r["id"] != model
                   and (price := _turn_cost(r)) is not None and price < cost]
        if not cheaper:
            return
        m = meter(key)
        left = m.get("leftUsd") if m else None
        if not _number(left) or left >= cost:
            return
        _WARNED.add(identity)
        logger.warning("quotum: %s left, below the estimated %s for %s (10k input + 1k output tokens). "
                       "consider the cheaper private model %s. model unchanged.", _usd(left), _usd(cost),
                       payg.model_id(model, payg.credentials(key)) or "selected model",
                       payg.model_id(min(cheaper)[1], payg.credentials(key)) or "another private model")


def cap_spent_message(key: str, *, now: datetime | None = None) -> str:
    if meter_handle(key) is None and key.strip().startswith("ibsec_"):
        return PROXY_MESSAGE
    now = now or datetime.now(timezone.utc)
    m = meter(key)
    bell = _iso(m.get("bellAt")) if m else None
    if bell is None or bell.tzinfo is None or bell <= now:
        bell = now.astimezone(timezone.utc).replace(hour=20, minute=0, second=0, microsecond=0)
        if bell <= now:
            bell += timedelta(days=1)
    return f"the quotum key's cap is spent. it resets at the 20:00 UTC bell ({bell.astimezone(timezone.utc):%Y-%m-%d %H:%M UTC}). retry after that bell."


def _iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _usd(value: Any) -> str:
    return f"${value:.4f}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "unknown"


def _supported_kwargs(cls: type, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop profile fields this Hermes build does not define, so an older build still registers the provider."""
    try:
        known = {f.name for f in dataclasses.fields(cls)}
    except TypeError:
        return dict(kwargs)
    return {k: v for k, v in kwargs.items() if k in known}


def classify_error(error: Any = None, *, status_code: int | None = None, error_code: Any = None, message: str | None = None, body: Any = None, model: str | None = None, **_: Any) -> dict[str, Any] | None:
    """Venice answers 402 when the key's cap is spent and 401 when the key was deleted or rotated."""
    if payg_key():
        if status_code in (401, 402, 403, 503):
            return {"reason": "auth_permanent" if status_code in (401, 403) else "billing" if status_code == 402 else "server_error",
                    "retryable": False, "should_rotate_credential": False, "should_fallback": False,
                    "error_context": {"message": payg.error_message(status_code, body or {"code": error_code})}}
        return None
    if status_code == 402:
        return {"reason": "billing", "retryable": False, "should_rotate_credential": False, "should_fallback": False}
    if status_code == 401:
        return {"reason": "auth_permanent", "retryable": False, "should_rotate_credential": False, "should_fallback": False}
    return None


class QuotumProfile(ProviderProfile):
    """A quotum seat's Venice key: the seat's catalog, its meter in /usage, no Venice system prompt."""

    def fetch_models(self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = TIMEOUT) -> list[str] | None:
        if payg_key():
            try:
                rows = _get_json(f"{PAYG_BASE}/models", timeout=timeout).get("data") or []
                return [r["id"] for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)
                        and r.get("privacy") in ("private", "anonymized")
                        and payg.model_id(r["id"], payg.credentials(api_key or ""))
                        and r.get("type", "text") == "text" and r.get("available", True) is not False
                        and "/chat/completions" in r.get("endpoints", ["/chat/completions"])]
            except Exception as exc:
                logger.debug("quotum: payg catalog unavailable")
                return None
        return [r["id"] for r in catalog(timeout) if payg.model_id(r["id"], payg.credentials(api_key or ""))] or None

    def build_extra_body(self, *, session_id: str | None = None, **context: Any) -> dict[str, Any]:
        _SESSION.set(session_id)
        if payg_key():
            return {}
        params: dict[str, Any] = {"include_venice_system_prompt": False}
        # QUOTUM_X_SEARCH=1: xAI's live web and X search, on the models that have it; Venice charges it per search
        if x_search_on() and context.get("model") in x_search_models():
            params["enable_x_search"] = True
        return {"venice_parameters": params}

    def create_client(self, **client_kwargs: Any) -> Any | None:
        if payg_key():
            return _payg_client(client_kwargs)
        return _quotum_client(client_kwargs)

    def get_max_tokens(self, model: str | None) -> int | None:
        return payg.OUTPUT_LIMIT if payg_key() else super().get_max_tokens(model)

    def fetch_account_usage(self, *, base_url: str | None = None, api_key: str | None = None):
        from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow

        now = datetime.now(timezone.utc)
        if payg_key():
            return self._fetch_payg_usage(now)
        key = (api_key or payg.secret(API_KEY_ENV)).strip()
        if not key:
            return AccountUsageSnapshot(provider=self.name, source="quotum_meter", fetched_at=now, title="quotum seat", unavailable_reason=f"{API_KEY_ENV} is not set")
        if meter_handle(key) is None:
            return AccountUsageSnapshot(provider=self.name, source="quotum_meter", fetched_at=now, title="quotum seat", unavailable_reason=PROXY_MESSAGE)
        m = meter(key)
        if m is None:
            return AccountUsageSnapshot(provider=self.name, source="quotum_meter", fetched_at=now, title="quotum seat", unavailable_reason="the meter is unavailable or this is not a live quotum key")
        cap, spent, left = m.get("capUsd"), m.get("spentUsd"), m.get("leftUsd")
        used = 100.0 * spent / cap if isinstance(cap, (int, float)) and cap > 0 and isinstance(spent, (int, float)) else None
        kind = {"seat": "seat key", "agent": "agent key", "day": "day key"}.get(m.get("kind"), "key")
        details = [f"{kind} of seat {m.get('seat')}, session {m.get('session')}", f"spent {_usd(spent)} of {_usd(cap)}, {_usd(left)} left"]
        if m.get("seatCapUsd") is not None:
            details.append(f"the seat: {_usd(m.get('seatSpentUsd'))} of {_usd(m.get('seatCapUsd'))}")
        details.append("what the seat leaves unspent burns at the 20:00 UTC bell")
        last = m.get("lastBell") or {}
        if last.get("burnedUsd") is not None:
            details.append(f"last bell (session {last.get('session')}): {_usd(last.get('burnedUsd'))} burned")
        if m.get("readAt"):
            details.append(f"read at {m['readAt']}")
        return AccountUsageSnapshot(
            provider=self.name, source="quotum_meter", fetched_at=now, title="quotum seat",
            windows=(AccountUsageWindow(label="this session", used_percent=used, reset_at=_iso(m.get("bellAt")), detail=f"{_usd(left)} left"),),
            details=tuple(details), raw=m,
        )

    def _fetch_payg_usage(self, now: datetime):
        from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow

        key = payg_key()
        try:
            m = payg.redact_keys(_get_json(f"{PAYG_BASE}/meter", bearer=key), payg.credentials(key))
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read())
            except Exception:
                body = None
            finally:
                payg.close_quietly(exc)
            return AccountUsageSnapshot(provider=self.name, source="quotum_payg", fetched_at=now,
                                        title="quotum pay as you go", unavailable_reason=payg.error_message(exc.code, body))
        except Exception as exc:
            return AccountUsageSnapshot(provider=self.name, source="quotum_payg", fetched_at=now,
                                        title="quotum pay as you go", unavailable_reason="payg meter did not answer. check https://quotum.org/payg.")
        try:
            status = payg.redact_keys(_get_json(f"{PAYG_BASE}/status"), payg.credentials(key))
        except Exception:
            status = {"error": "payg intake status is unavailable. check https://quotum.org/payg."}
        view = payg.presentation(m, status)
        if view.get("error"):
            return AccountUsageSnapshot(provider=self.name, source="quotum_payg", fetched_at=now,
                                        title="quotum pay as you go", unavailable_reason=view["error"])
        details = [f"project {view.get('project')}",
                   f"balance {payg.money(m.get('available'))} available; {payg.money(m.get('held'))} held; {payg.money(m.get('refundPending'))} refund pending",
                   f"spent {payg.money(m.get('spent'))}; {len(view['receipts'])} recent receipts in the dashboard"]
        if view.get("notice"):
            details.append(view["notice"])
        if view["frozen"]:
            details.append("project balance is frozen. contact the operator at https://quotum.org/payg.")
        return AccountUsageSnapshot(provider=self.name, source="quotum_payg", fetched_at=now,
                                    title="quotum pay as you go", details=tuple(details), raw=view,
                                    windows=(AccountUsageWindow(label="balance", detail=f"{payg.money(m.get('available'))} available"),))


try:
    _INITIAL_PAYG = bool(payg_key())
except payg.SecretScopeUnavailable:
    # Registration remains import-safe on old hosts. Credential-dependent hooks
    # resolve again and report the upgrade error before creating a client.
    _INITIAL_PAYG = False


quotum = QuotumProfile(**_supported_kwargs(QuotumProfile, dict(
    name=PROVIDER_ID,
    display_name="quotum pay as you go" if _INITIAL_PAYG else "quotum seat",
    description="A quotum seat or a prepaid pay as you go project",
    signup_url="https://quotum.org/payg" if _INITIAL_PAYG else "https://quotum.org/seat",
    env_vars=(PAYG_KEY_ENV, API_KEY_ENV),
    base_url=PAYG_BASE if _INITIAL_PAYG else VENICE_BASE,
    auth_type="api_key",
    api_mode="chat_completions",
    hostname="quotum.org" if _INITIAL_PAYG else "api.venice.ai",
    classify_api_error=classify_error,
)))

register_provider(quotum)
