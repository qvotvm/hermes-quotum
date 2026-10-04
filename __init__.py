"""quotum provider profile for Hermes Agent.

A quotum seat holds a Venice key with a daily dollar cap, paid for by the
QUOTUM trading tax; what the seat leaves unspent at the 20:00 UTC bell buys
QUOTUM and burns it. This profile lets Hermes use that key (a seat key or a
day key) as a provider:

- inference goes straight to Venice (https://api.venice.ai/api/v1) with the
  key in QUOTUM_SEAT_KEY. quotum never sees a prompt or a reply;
- the model list is the seat's catalog, private and anonymized models only,
  read from https://quotum.org/api/seat/models (no key sent);
- /usage reads https://quotum.org/api/meter/<h>, where h is the first 16 hex
  characters of the key's sha256. The key itself never leaves the machine
  except to Venice. The request carries the plugin version in a header.

- with QUOTUM_X_SEARCH=1 only: the list of models that support xAI's live
  web and X search, read from https://api.venice.ai/api/v1/models (no key
  sent), so the search is asked for only on those models (the Grok family).

Nothing else is sent anywhere. No telemetry, no wallet code, no self-update.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

VERSION = "0.1.2"
PROVIDER_ID = "quotum"
API_KEY_ENV = "QUOTUM_SEAT_KEY"
X_SEARCH_ENV = "QUOTUM_X_SEARCH"
VENICE_BASE = "https://api.venice.ai/api/v1"
SITE = "https://quotum.org"
TIMEOUT = 8.0


def key_hash(key: str) -> str:
    """The meter's handle for a key: the first 16 hex of its sha256. The key is not recoverable from it."""
    return hashlib.sha256(key.strip().encode()).hexdigest()[:16]


def _get_json(url: str, timeout: float = TIMEOUT) -> Any:
    request = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": f"hermes-quotum/{VERSION}", "x-quotum-plugin": VERSION})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https hosts
        return json.load(response)


_X_MODELS: set[str] | None = None


def x_search_on() -> bool:
    return os.environ.get(X_SEARCH_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def x_search_models(timeout: float = TIMEOUT) -> set[str]:
    """Venice's models with supportsXSearch, read once; an empty set (not kept) when the catalog does not answer."""
    global _X_MODELS
    if _X_MODELS is None:
        request = urllib.request.Request(f"{VENICE_BASE}/models?type=text", headers={"accept": "application/json", "user-agent": f"hermes-quotum/{VERSION}"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https host
                rows = json.load(response).get("data") or []
        except Exception as exc:
            logger.debug("quotum: venice catalog unavailable: %s", exc)
            return set()
        _X_MODELS = {r["id"] for r in rows if isinstance(r, dict) and ((r.get("model_spec") or {}).get("capabilities") or {}).get("supportsXSearch")}
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


def _x_search_client(client_kwargs: dict[str, Any]) -> Any:
    """openai.OpenAI on openai's own default httpx client, whose transport applies _without_web_search to chat requests."""
    import httpx
    import openai

    class XSearchTransport(httpx.HTTPTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path.endswith("/chat/completions"):
                body = _without_web_search(request.read())
                if body is not None:
                    headers = [(k, v) for k, v in request.headers.items() if k.lower() != "content-length"]
                    request = httpx.Request(request.method, request.url, headers=headers, content=body, extensions=request.extensions)
            return super().handle_request(request)

    known = set(inspect.signature(openai.OpenAI.__init__).parameters) - {"self", "http_client"}
    return openai.OpenAI(http_client=openai.DefaultHttpxClient(transport=XSearchTransport()), **{k: v for k, v in client_kwargs.items() if k in known})


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
    if status_code == 402:
        return {"reason": "billing"}      # the seat's cap for this session is spent: it comes back at the 20:00 UTC bell
    if status_code == 401:
        return {"reason": "auth_permanent"}   # ended, rotated or the seat was freed: get the key again on quotum.org/seat
    return None


class QuotumProfile(ProviderProfile):
    """A quotum seat's Venice key: the seat's catalog, its meter in /usage, no Venice system prompt."""

    def fetch_models(self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = TIMEOUT) -> list[str] | None:
        try:
            rows = _get_json(f"{SITE}/api/seat/models", timeout=timeout).get("models") or []
        except Exception as exc:  # the picker falls back to its static list
            logger.debug("quotum: catalog unavailable: %s", exc)
            return None
        ids = [r["id"] for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str) and r.get("privacy") in ("private", "anonymized")]
        return ids or None

    def build_extra_body(self, *, session_id: str | None = None, **context: Any) -> dict[str, Any]:
        params: dict[str, Any] = {"include_venice_system_prompt": False}
        # QUOTUM_X_SEARCH=1: xAI's live web and X search, on the models that have it; Venice charges it per search
        if x_search_on() and context.get("model") in x_search_models():
            params["enable_x_search"] = True
        return {"venice_parameters": params}

    def create_client(self, **client_kwargs: Any) -> Any | None:
        # only with QUOTUM_X_SEARCH on: Hermes' own client otherwise
        return _x_search_client(client_kwargs) if x_search_on() else None

    def fetch_account_usage(self, *, base_url: str | None = None, api_key: str | None = None):
        from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow

        now = datetime.now(timezone.utc)
        key = (api_key or os.environ.get(API_KEY_ENV) or "").strip()
        if not key:
            return AccountUsageSnapshot(provider=self.name, source="quotum_meter", fetched_at=now, title="quotum seat", unavailable_reason=f"{API_KEY_ENV} is not set")
        try:
            m = _get_json(f"{SITE}/api/meter/{key_hash(key)}")
        except urllib.error.HTTPError as exc:
            why = "this key is not a live quotum seat or day key" if exc.code == 404 else f"the meter answered {exc.code}"
            return AccountUsageSnapshot(provider=self.name, source="quotum_meter", fetched_at=now, title="quotum seat", unavailable_reason=why)
        except Exception as exc:
            return AccountUsageSnapshot(provider=self.name, source="quotum_meter", fetched_at=now, title="quotum seat", unavailable_reason=f"the meter did not answer ({type(exc).__name__})")
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


quotum = QuotumProfile(**_supported_kwargs(QuotumProfile, dict(
    name=PROVIDER_ID,
    display_name="quotum seat",
    description="Private and anonymized Venice models on a quotum seat's key, capped daily",
    signup_url="https://quotum.org/seat",
    env_vars=(API_KEY_ENV,),
    base_url=VENICE_BASE,
    auth_type="api_key",
    api_mode="chat_completions",
    hostname="api.venice.ai",
    classify_api_error=classify_error,
)))

register_provider(quotum)
