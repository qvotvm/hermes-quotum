"""quotum Seals tab backend, mounted at /api/plugins/quotum/.

Reads the seat's record from quotum.org. The seat key never leaves this machine: the meter is asked with the first 16 hex of the
key's sha256 or QUOTUM_METER_HANDLE (as /usage does), which answers the seat; the record of the seat's holder is public, asked by seat. QUOTUM_WALLET reads any
wallet's record instead.

With QUOTUM_PAYG_KEY set, the backend reads PAYG balance and receipts with
the project bearer. Only presentation metadata reaches the browser.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlencode

from fastapi import APIRouter

router = APIRouter()
# Hermes loads this backend as a standalone module, outside the provider package.
_spec = importlib.util.spec_from_file_location("_quotum_dashboard_payg", Path(__file__).resolve().parent.parent / "payg.py")
payg_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(payg_helpers)
_handle_name = "_quotum_meter_handle"
if _handle_name not in sys.modules:
    _handle_spec = importlib.util.spec_from_file_location(_handle_name, Path(__file__).resolve().parent.parent / "meter_handle.py")
    _handle_module = importlib.util.module_from_spec(_handle_spec)
    sys.modules[_handle_name] = _handle_module
    _handle_spec.loader.exec_module(_handle_module)
else:
    _handle_module = sys.modules[_handle_name]
VERSION = payg_helpers.VERSION
SITE = os.environ.get("QUOTUM_SITE", "https://quotum.org").rstrip("/")
CACHE_TTL_SECONDS = 120
_CACHE: Dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()


def _env(name: str) -> str:
    """Read configuration through the active Hermes profile scope."""
    return payg_helpers.secret(name)


def _meter_handle(key: str) -> str | None:
    return _handle_module.meter_handle(key, payg_helpers.secret("QUOTUM_METER_HANDLE", strip=False))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "quotum metadata redirects are disabled", headers, fp)


def _get(path: str, *, bearer: str | None = None) -> Dict[str, Any]:
    headers = {"x-quotum-plugin": VERSION, "user-agent": f"hermes-quotum/{VERSION}"}
    # PAYG uses its fixed host, including public status; QUOTUM_SITE is seat-only.
    url = payg_helpers.BASE + path.removeprefix("/api/payg/v1") if path.startswith("/api/payg/v1/") else SITE + path
    req = urllib.request.Request(url, headers=headers)
    if bearer:
        req.add_unredirected_header("authorization", f"Bearer {bearer}")
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=10) as r:
            result = json.loads(r.read().decode("utf-8"))
            if not isinstance(result, dict) or result.get("error") is not None:
                return _safe_error(path, 400, result)
            return result
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = None
        finally:
            payg_helpers.close_quietly(e)
        return _safe_error(path, e.code, body)
    except Exception as e:  # network
        return {"error": "quotum.org did not answer. retry in a moment."}


def _safe_error(path: str, status: int, body: Any) -> Dict[str, Any]:
    if path.startswith("/api/payg/v1/"):
        return {"error": payg_helpers.error_message(status, body)}
    if isinstance(body, dict) and body.get("error") == "unknown":
        return {"error": "this key is not a live quotum key."}
    return {"error": f"quotum request failed (http {status}). check https://quotum.org/seat."}


@router.get("/record")
def record() -> Dict[str, Any]:
    """Cache successful records per identity; never retain the seat key in the cache."""
    try:
        if _env("QUOTUM_PAYG_KEY"):
            return payg_account()
        wallet, key = _env("QUOTUM_WALLET"), _env("QUOTUM_SEAT_KEY")
    except payg_helpers.SecretScopeUnavailable:
        return {"error": payg_helpers.SECRET_SCOPE_MESSAGE}
    if not wallet and not key:
        return _read_record(wallet, key)
    handle = _meter_handle(key) if not wallet else None
    if not wallet and key and handle is None:
        return {"error": _handle_module.PROXY_MESSAGE}
    identity = hashlib.sha256(("wallet:" + wallet if wallet else "key:" + key + ":handle:" + str(handle)).encode()).hexdigest()
    with _CACHE_LOCK:
        now = time.monotonic()
        cached = _CACHE.get(identity)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return payg_helpers.redact_keys(cached[1], payg_helpers.credentials(key))
        result = payg_helpers.seat_record(_read_record(wallet, key), payg_helpers.credentials(key))
        if not result.get("error"):
            # Bound memory even when the dashboard switches profiles/wallets.
            _CACHE.clear()
            _CACHE[identity] = (time.monotonic(), result)
        return result


@router.get("/account")
def account() -> Dict[str, Any]:
    """Resolve the active mode for the dashboard; seat behavior is unchanged."""
    return record()


@router.get("/payg")
def payg_account() -> Dict[str, Any]:
    try:
        key = _env("QUOTUM_PAYG_KEY")
    except payg_helpers.SecretScopeUnavailable:
        return {"mode": "payg", "error": payg_helpers.SECRET_SCOPE_MESSAGE}
    if not key:
        return {"mode": "payg", "error": "set QUOTUM_PAYG_KEY to a project API key from https://quotum.org/payg."}
    # Never cache a changing balance or access decision.
    meter = payg_helpers.redact_keys(_get("/api/payg/v1/meter", bearer=key), payg_helpers.credentials(key))
    if meter.get("error"):
        return {"mode": "payg", "error": meter["error"]}
    status = payg_helpers.redact_keys(_get("/api/payg/v1/status"), payg_helpers.credentials(key))
    return payg_helpers.presentation(meter, status)


def _read_record(wallet: str, key: str) -> Dict[str, Any]:
    seat = None
    if not wallet:
        if not key:
            return {"error": "set QUOTUM_SEAT_KEY (your seat key or Hermes key) or QUOTUM_WALLET in ~/.hermes/.env."}
        handle = _meter_handle(key)
        if handle is None:
            return {"error": _handle_module.PROXY_MESSAGE}
        m = _get("/api/meter/" + handle)
        if m.get("error"):
            return {"error": "this key is not a live quotum key." if m["error"] == "unknown" else m["error"]}
        seat = payg_helpers._count(m.get("seat"))
        if not seat:
            return {"error": "the meter did not name a seat for this key."}
        try:
            seat = int(seat)
        except (TypeError, ValueError):
            return {"error": "the meter returned an invalid seat number."}
        r = _get("/api/record?" + urlencode({"seat": seat}))
        return {"seat": seat, **r}
    r = _get("/api/record?" + urlencode({"w": wallet}))
    return {"seat": seat, "wallet": wallet, **r}
