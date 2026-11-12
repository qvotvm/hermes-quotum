"""Shared PAYG presentation helpers. no credentials or network state are kept here."""

import math
import re
from datetime import datetime
from typing import Any

VERSION = "0.3.2"
KEY_ENV = "QUOTUM_PAYG_KEY"
OUTPUT_LIMIT = 2048
BASE = "https://quotum.org/api/payg/v1"
PAUSED_CODES = frozenset({
    "payg-halted", "payg-unavailable", "reconciliation-required", "pilot-loss-limit",
    "pilot-kill-line", "pilot-not-started", "supplier-unavailable", "supplier-daily-limit",
    "supplier-float-low", "provider-unavailable", "model-reconciliation-pending",
    "catalog-stale", "model-not-calibrated",
})


def error_code(body: Any) -> str:
    error = body.get("error", body) if isinstance(body, dict) else body
    return str(error.get("code", "")) if isinstance(error, dict) else str(error or "")


def error_message(status: int, body: Any = None) -> str:
    """One readable line and one action; never echo upstream bodies or credentials."""
    code = error_code(body)
    if status == 403:
        if code in {"key-model-scope", "project-model-not-allowed", "model-key-policy", "key-scope"}:
            return "payg access denied for this model or operation. choose a model allowed by your project key at https://quotum.org/payg."
        return "payg access denied. check project access or redeem an invite at https://quotum.org/payg."
    if status == 402:
        if code == "session_limit":
            return "payg session limit reached. raise the session limit at https://quotum.org/payg."
        return "payg insufficient balance. top up your project at https://quotum.org/payg."
    if status == 503 or code in PAUSED_CODES:
        return "payg intake is paused. check https://quotum.org/payg and wait for intake to reopen."
    if status == 401:
        return "payg project key is missing, invalid, expired or revoked. create a new project key at https://quotum.org/payg."
    if status == 404:
        return "payg is unavailable or the receipt was not found. check https://quotum.org/payg."
    if status == 429:
        return "payg rate or budget limit reached. check your project limits at https://quotum.org/payg before trying again."
    return f"payg request failed (http {status}). check your project and request settings at https://quotum.org/payg."


def usd_nano(value: Any) -> float | None:
    return value / 1_000_000_000 if type(value) is int and 0 <= value <= 2 ** 63 - 1 else None


def money(value: Any) -> str:
    amount = usd_nano(value)
    return f"${amount:.6f}" if amount is not None else "unknown"


def redact(value: Any, key: str) -> Any:
    """Remove the active credential from metadata, including nested field names."""
    if not key:
        return value
    if isinstance(value, str):
        return value.replace(key, "[redacted]")
    if isinstance(value, dict):
        return {redact(k, key): redact(v, key) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    return value


def presentation(meter: dict, status: dict) -> dict:
    """The browser sees only balance, intake and receipt metadata, never a bearer."""
    if not isinstance(meter, dict) or meter.get("error") is not None:
        return {"mode": "payg", "error": error_message(400)}
    if not isinstance(status, dict):
        status = {"error": True}
    if meter.get("unit") != "USD-nano":
        return {"mode": "payg", "error": "payg meter returned an unsupported balance unit. check https://quotum.org/payg."}
    fields = ("total", "available", "spent", "held", "refundPending")
    receipts = []
    rows = meter.get("receipts")
    for r in reversed(rows[-100:] if isinstance(rows, list) else []):
        if not isinstance(r, dict):
            continue
        footer = r.get("footer") if isinstance(r.get("footer"), dict) else {}
        receipts.append({
            "id": _text(r.get("id")), "model": _text(r.get("model")), "status": _text(r.get("status")),
            "chargeUsd": usd_nano(footer.get("finalPrice")) if footer else usd_nano(r.get("charge")),
            "refundUsd": usd_nano(r.get("refund")), "burnShareUsd": usd_nano(footer.get("burnShare")),
            "tokensIn": _count(footer.get("tokensIn")), "tokensOut": _count(footer.get("tokensOut")),
        })
    return {
        "mode": "payg", "project": _text(meter.get("project")),
        "balance": {f + "Usd": usd_nano(meter.get(f)) for f in fields},
        "frozen": meter.get("frozen") is True, "intake": status.get("intake") if isinstance(status.get("intake"), bool) else None,
        "notice": ("payg intake status is unavailable or paused. check https://quotum.org/payg."
                   if status.get("error") else error_message(503) if status.get("intake") is False else None),
        "receipts": receipts,
    }


def _text(value: Any) -> str | None:
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", value)[:512] if isinstance(value, str) else None


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 2 ** 53 - 1 else None


def public_error_code(status: int, body: Any) -> str:
    """Use fixed codes rather than reflecting untrusted upstream text."""
    if status == 403 and error_code(body) in {"key-model-scope", "project-model-not-allowed", "model-key-policy", "key-scope"}:
        return error_code(body)
    if status == 402:
        return "session_limit" if error_code(body) == "session_limit" else "insufficient_balance"
    return {401: "invalid-key", 403: "access-denied", 404: "not-found", 429: "rate-limit", 503: "payg-unavailable"}.get(status, "payg-error")


SECRET_SCOPE_MESSAGE = ("quotum requires Hermes's profile secret API (agent.secret_scope.get_secret). "
                        "upgrade Hermes and restart Hermes and its gateway.")


class SecretScopeUnavailable(RuntimeError):
    """The host cannot provide profile-scoped credentials to this plugin."""


def secret(name: str, *, strip: bool = True) -> str | None:
    """Only Hermes resolves credentials; never borrow process or file secrets."""
    import importlib

    try:
        scope = importlib.import_module("agent.secret_scope")
    except ModuleNotFoundError as exc:
        if exc.name not in {"agent", "agent.secret_scope"}:
            raise
        raise SecretScopeUnavailable(SECRET_SCOPE_MESSAGE) from None
    get_secret = getattr(scope, "get_secret", None)
    if not callable(get_secret):
        raise SecretScopeUnavailable(SECRET_SCOPE_MESSAGE)
    value = get_secret(name)
    if value is None:
        return "" if strip else None
    return value.strip() if strip else value


# Public output schemas are deliberately separate from credential redaction.
# Unknown fields, including nested diagnostics, never become SDK/browser data.
def credentials(*extra: str) -> tuple[str, ...]:
    return tuple(sorted({key for key in (*extra, secret(KEY_ENV), secret("QUOTUM_SEAT_KEY")) if key}, key=len, reverse=True))


def redact_keys(value: Any, keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = redact(value, key)
    return value


def model_id(value: Any, keys: tuple[str, ...] = ()) -> str | None:
    if (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", value)
            and not any(key in value for key in keys)):
        return value
    return None


def _amount(value: Any) -> int | float | None:
    if type(value) in (int, float) and 0 <= value <= 10 ** 15 and math.isfinite(value):
        return value
    return None


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def seat_meter(value: Any, keys: tuple[str, ...]) -> dict | None:
    if not isinstance(value, dict) or value.get("error") is not None:
        return None
    value = redact_keys(value, keys)
    result = {k: _amount(value.get(k)) for k in ("capUsd", "spentUsd", "leftUsd", "seatCapUsd", "seatSpentUsd")}
    result.update({k: _count(value.get(k)) for k in ("seat", "session")})
    result["kind"] = value.get("kind") if value.get("kind") in ("seat", "agent", "day") else None
    result.update({k: _timestamp(value.get(k)) for k in ("bellAt", "readAt")})
    last = value.get("lastBell")
    result["lastBell"] = {"session": _count(last.get("session")), "burnedUsd": _amount(last.get("burnedUsd"))} if isinstance(last, dict) else {}
    return result


SEAL_IDS = frozenset(("bells_held", "held_through", "bell_one", "witness", "same_block", "pyre", "pyre_days",
    "seated", "never_idle", "used", "agent_on_seat", "zero_left", "came_back", "calls_sealed", "closest",
    "beat_house", "beat_open", "by_hand", "own_agent", "within_a_bip", "guesses", "beat_bot", "guess_bip", "full_house"))


def seat_record(value: dict, keys: tuple[str, ...]) -> dict:
    value = redact_keys(value, keys)
    if value.get("error"):
        # The caller already converted network/error envelopes to local messages.
        return {"error": _text(value["error"])}
    result = {k: _count(value.get(k)) for k in ("seat", "session")}
    for name, digits in (("wallet", 40), ("rootTx", 64)):
        text = value.get(name)
        result[name] = text if isinstance(text, str) and re.fullmatch(r"0x[0-9a-fA-F]{" + str(digits) + "}", text) else None
    record = value.get("record")
    result["record"] = None
    if isinstance(record, dict):
        result["record"] = {}
        for name in ("seals", "counts"):
            rows = record.get(name)
            result["record"][name] = {k: n for k, v in rows.items() if k in SEAL_IDS and (n := _count(v)) is not None
                                      and (name != "seals" or n <= 5)} if isinstance(rows, dict) else {}
    rows = value.get("catalog")
    catalog = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] not in SEAL_IDS:
            continue
        tiers = row.get("tiers")
        if (row.get("family") not in ("bell", "pyre", "seat", "call", "guess", "meta")
                or not isinstance(tiers, list) or not 1 <= len(tiers) <= 5
                or any(_count(n) is None or n <= 0 for n in tiers) or tiers != sorted(set(tiers))):
            continue
        catalog.append({"id": row["id"], "name": _text(row.get("name")), "description": _text(row.get("description")),
                        "family": row["family"], "tiers": tiers, "secret": row.get("secret") is True})
    result["catalog"] = catalog
    rarity = value.get("rarity")
    result["rarity"] = {k: v for k, v in rarity.items() if k in SEAL_IDS and _amount(v) is not None and v <= 1} if isinstance(rarity, dict) else {}
    return result


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _nano(value: Any) -> int | None:
    return value if usd_nano(value) is not None else None


def _signed(value: Any) -> int | float | None:
    return value if type(value) in (int, float) and -10 ** 15 <= value <= 10 ** 15 and math.isfinite(value) else None


# Each map is an exact field allowlist; lists recurse into their element schema.
FUNCTION = {"name": _string, "arguments": _string}
TOOL = {"index": _count, "id": _string, "type": _string, "function": FUNCTION}
MESSAGE = {"role": _string, "content": _string, "refusal": _string,
           "reasoning_content": _string, "reasoning": _string,
           "function_call": FUNCTION, "tool_calls": [TOOL],
           "audio": {"id": _string, "data": _string, "expires_at": _count, "transcript": _string},
           "annotations": [{"type": _string, "url_citation": {"start_index": _count, "end_index": _count, "title": _string, "url": _string}}]}
TOKEN_LOG = {"token": _string, "bytes": [_count], "logprob": _signed}
LOGPROBS = {name: [{**TOKEN_LOG, "top_logprobs": [TOKEN_LOG]}] for name in ("content", "refusal")}
RECEIPT = {"id": _text, "model": _text, "status": _text, "charge": _nano, "refund": _nano,
           "footer": {"finalPrice": _nano, "burnShare": _nano, "tokensIn": _count, "tokensOut": _count}}
CHAT = {"id": _string, "object": _string, "created": _count, "model": _string,
        "system_fingerprint": _string, "service_tier": _string,
        "choices": [{"index": _count, "finish_reason": _string, "message": MESSAGE, "delta": MESSAGE, "logprobs": LOGPROBS}],
        "usage": {"prompt_tokens": _count, "completion_tokens": _count, "total_tokens": _count,
                  "prompt_tokens_details": {"cached_tokens": _count, "audio_tokens": _count},
                  "completion_tokens_details": {k: _count for k in ("reasoning_tokens", "audio_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens")}},
        "quotum_billing": RECEIPT}


def project(value: Any, schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: project(value[k], rule) for k, rule in schema.items() if k in value} if isinstance(value, dict) else None
    if isinstance(schema, list):
        return [project(v, schema[0]) for v in value] if isinstance(value, list) else None
    return schema(value)


def chat(value: Any, keys: tuple[str, ...]) -> dict | None:
    if (not isinstance(value, dict) or value.get("error") is not None
            or not isinstance(value.get("choices"), list) or any(not isinstance(c, dict) for c in value["choices"])):
        return None
    return redact_keys(project(value, CHAT), keys)


def close_quietly(response: Any) -> None:
    try:
        response.close()
    except Exception:
        # A failed close must not expose upstream text or replace a known status.
        pass


class DeltaRedactor:
    """Hold only possible credential prefixes, independently per choice/text field."""
    def __init__(self, keys: tuple[str, ...]):
        self.keys = keys
        self.pending: dict[tuple, str] = {}
        self.header: dict = {}

    def _text(self, path: tuple, text: str) -> str:
        text = self.pending.pop(path, "") + text
        text = redact_keys(text, self.keys)
        keep = max((n for key in self.keys for n in range(1, min(len(key), len(text) + 1))
                    if text.endswith(key[:n])), default=0)
        if keep:
            self.pending[path] = text[-keep:]
            return text[:-keep]
        return text

    @staticmethod
    def _put(delta: dict, path: tuple, text: str) -> None:
        if path[0] == "tool_calls":
            calls = delta.setdefault("tool_calls", [])
            call = next((c for c in calls if c.get("index", 0) == path[1]), None)
            if call is None:
                call = {"index": path[1]}
                calls.append(call)
            DeltaRedactor._put(call, path[2:], text)
        elif len(path) > 1:
            DeltaRedactor._put(delta.setdefault(path[0], {}), path[1:], text)
        else:
            delta[path[0]] = (delta.get(path[0]) or "") + text

    def _flush_choice(self, index: int, delta: dict) -> None:
        for path in list(self.pending):
            if path[0] == index:
                self._put(delta, path[1:], self.pending.pop(path))

    def apply(self, body: dict) -> dict:
        self.header = {k: body[k] for k in ("id", "model", "object", "created") if k in body}
        for choice in body["choices"]:
            index = choice.get("index", 0)
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            def visit(node: dict, path: tuple) -> None:
                for name in ("content", "refusal", "reasoning", "reasoning_content", "id", "name", "arguments"):
                    if isinstance(node.get(name), str):
                        node[name] = self._text((index, *path, name), node[name])
                for name in ("function", "function_call"):
                    if isinstance(node.get(name), dict):
                        visit(node[name], (*path, name))
                for tool in node.get("tool_calls") or []:
                    if isinstance(tool, dict):
                        visit(tool, (*path, "tool_calls", tool.get("index", 0)))
            visit(delta, ())
            if choice.get("finish_reason"):
                self._flush_choice(index, delta)
        return body

    def flush(self) -> dict | None:
        choices = []
        for index in dict.fromkeys(path[0] for path in self.pending):
            delta = {}
            self._flush_choice(index, delta)
            choices.append({"index": index, "delta": delta})
        return {**self.header, "choices": choices} if choices else None
