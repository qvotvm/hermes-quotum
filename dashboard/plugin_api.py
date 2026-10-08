"""quotum Seals tab backend, mounted at /api/plugins/quotum/.

Reads the seat's record from quotum.org. The seat key never leaves this machine: the meter is asked with the first 16 hex of the
key's sha256 (as /usage does), which answers the seat; the record of the seat's holder is public, asked by seat. QUOTUM_WALLET reads any
wallet's record instead.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlencode

from fastapi import APIRouter

router = APIRouter()
VERSION = "0.2.0"
SITE = os.environ.get("QUOTUM_SITE", "https://quotum.org").rstrip("/")
CACHE_TTL_SECONDS = 120
_CACHE: Dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()


def _env(name: str) -> str:
    """The process env first, then ~/.hermes/.env, where the install line puts QUOTUM_SEAT_KEY."""
    v = os.environ.get(name, "").strip()
    if v:
        return v
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except Exception:
        home = Path.home() / ".hermes"
    try:
        for line in (home / ".env").read_text(encoding="utf-8").splitlines():
            k, _, val = line.partition("=")
            if k.strip() == name:
                return val.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _get(path: str) -> Dict[str, Any]:
    req = urllib.request.Request(SITE + path, headers={"x-quotum-plugin": VERSION, "user-agent": f"hermes-quotum/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return {"error": json.loads(e.read().decode("utf-8")).get("error", str(e.code))}
        except Exception:
            return {"error": str(e.code)}
    except Exception as e:  # network
        return {"error": f"quotum.org did not answer ({type(e).__name__})"}


@router.get("/record")
def record() -> Dict[str, Any]:
    """Cache successful records per identity; never retain the seat key in the cache."""
    wallet, key = _env("QUOTUM_WALLET"), _env("QUOTUM_SEAT_KEY")
    identity = hashlib.sha256(("wallet:" + wallet if wallet else "key:" + key).encode()).hexdigest()
    with _CACHE_LOCK:
        now = time.monotonic()
        cached = _CACHE.get(identity)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]
        result = _read_record(wallet, key)
        if not result.get("error"):
            # Bound memory even when the dashboard switches profiles/wallets.
            _CACHE.clear()
            _CACHE[identity] = (time.monotonic(), result)
        return result


def _read_record(wallet: str, key: str) -> Dict[str, Any]:
    seat = None
    if not wallet:
        if not key:
            return {"error": "Set QUOTUM_SEAT_KEY (your seat key or Hermes key) or QUOTUM_WALLET in ~/.hermes/.env."}
        m = _get("/api/meter/" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16])
        if m.get("error"):
            return {"error": "This key is not a live quotum key." if m["error"] == "unknown" else m["error"]}
        seat = m.get("seat")
        if not seat:
            return {"error": "The meter did not name a seat for this key."}
        try:
            seat = int(seat)
        except (TypeError, ValueError):
            return {"error": "The meter returned an invalid seat number."}
        r = _get("/api/record?" + urlencode({"seat": seat}))
        return {"seat": seat, **r}
    r = _get("/api/record?" + urlencode({"w": wallet}))
    return {"seat": seat, "wallet": wallet, **r}
