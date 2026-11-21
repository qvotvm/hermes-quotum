"""Public Venice health, cached in memory. no credentials enter this transport."""

import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

BASE = "https://metrics.venice.ai/v1/"
INDEX = BASE + "index.json"
TIMEOUT = 2.0
CACHE_SECONDS = 300.0
FAILURE_SECONDS = 60.0
MIN_UPTIME = 97.0
MAX_TTFT_P90 = 15000.0

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_CACHE: dict[str, tuple[float, dict]] = {}
_UNAVAILABLE_UNTIL = 0.0
_WARNED: dict[str, tuple] = {}


@dataclass(frozen=True)
class Health:
    state: str
    reason: str
    window: tuple = ()


def mode() -> str:
    value = os.environ.get("QUOTUM_MODEL_HEALTH", "warn").strip().lower()
    return value if value in ("warn", "route", "off") else "warn"


def cache_seconds() -> float:
    try:
        value = float(os.environ.get("QUOTUM_METRICS_CACHE_SECONDS", str(CACHE_SECONDS)))
        return value if math.isfinite(value) and value > 0 else CACHE_SECONDS
    except ValueError:
        return CACHE_SECONDS


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "quotum metrics redirects are disabled", headers, fp)


def _get_json(url: str) -> dict:
    if url != INDEX and not re.fullmatch(re.escape(BASE) + r"models/[A-Za-z0-9][A-Za-z0-9._-]*\.json", url):
        raise ValueError("quotum metrics requires its public https host and model path")
    request = urllib.request.Request(url, headers={"accept": "application/json"})
    # Disable environment proxies too; this opener carries no plugin or key headers.
    with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(request, timeout=TIMEOUT) as response:
        return json.load(response)


def _fetch(url: str, *, index: bool = False) -> tuple[float, dict] | None:
    global _UNAVAILABLE_UNTIL
    with _LOCK:
        now = time.monotonic()
        if now < _UNAVAILABLE_UNTIL:
            return None
        cached = _CACHE.get(url)
        if cached is not None and now - cached[0] < cache_seconds():
            return cached
        try:
            value = _get_json(url)
            if not isinstance(value, dict):
                raise ValueError("invalid metrics")
            if index:
                rows = value.get("models")
                if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get("id"), str) for r in rows):
                    raise ValueError("invalid metrics index")
            else:
                uptime = value.get("uptime")
                if not isinstance(uptime, dict) or not isinstance(uptime.get("buckets"), list):
                    raise ValueError("invalid metrics detail")
                if any(not isinstance(b, dict) for b in uptime["buckets"]):
                    raise ValueError("invalid metrics buckets")
        except Exception:
            _UNAVAILABLE_UNTIL = time.monotonic() + FAILURE_SECONDS
            logger.debug("quotum: model metrics unavailable; request continues")
            return None
        cached = (time.monotonic(), value)
        _CACHE[url] = cached
        return cached


def _number(value) -> bool:
    return type(value) in (int, float) and 0 <= value <= 10 ** 15 and math.isfinite(value)


def assess(row: dict | None, detail: dict) -> Health:
    """Unknown data takes precedence; a missing latency sample alone is advisory."""
    if row is None:
        return Health("unknown", "model missing from metrics index")
    uptime = row.get("uptime_24h_percent")
    if not _number(uptime) or uptime > 100:
        return Health("unknown", "24h uptime has no data")
    buckets = detail["uptime"]["buckets"]
    if len(buckets) < 2 or all(b.get("uptime_percent") is None for b in buckets[-2:]):
        return Health("unknown", "last two uptime buckets have no data")
    if uptime < MIN_UPTIME:
        return Health("unhealthy", "24h uptime below 97.0 percent")
    if buckets[-1].get("status") != "up":
        return Health("unhealthy", "last uptime bucket is not up")
    perf = row.get("performance")
    hour = perf.get("1h") if isinstance(perf, dict) else None
    ttft = hour.get("ttft_ms") if isinstance(hour, dict) else None
    latency = ttft.get("p90") if isinstance(ttft, dict) else None
    if _number(latency) and latency > MAX_TTFT_P90:
        return Health("unhealthy", "1h ttft p90 above 15000 ms")
    return Health("healthy", "uptime and latest bucket healthy" + ("; 1h latency unknown" if not _number(latency) else ""))


def check(model: str) -> Health | None:
    index = _fetch(INDEX, index=True)
    if index is None:
        return None
    row = next((r for r in index[1]["models"] if r["id"] == model), None)
    if row is None or row.get("uptime_24h_percent") is None:
        result = assess(row, {"uptime": {"buckets": []}})
        return Health(result.state, result.reason, (index[0],))
    path = row.get("detail_path")
    # A path from the public index is untrusted, even when it looks like a URL.
    detail = _fetch(BASE + path) if isinstance(path, str) else _fetch(BASE)
    if detail is None:
        return None
    result = assess(row, detail[1])
    return Health(result.state, result.reason, (index[0], detail[0]))


def warn_once(model: str, health: Health, log) -> None:
    with _LOCK:
        if _WARNED.get(model) != health.window:
            _WARNED[model] = health.window
            log.info("quotum: model %s is %s: %s. model unchanged.", model, health.state, health.reason)


def replace_model(content: bytes, model: str) -> bytes:
    """Replace one top-level JSON string, preserving every other byte and escape."""
    text = content.decode("utf-8")
    decoder = json.JSONDecoder()
    pos = 1 + len(text) - len(text.lstrip())
    spans = []
    while True:
        while text[pos].isspace():
            pos += 1
        if text[pos] == "}":
            break
        key, pos = decoder.raw_decode(text, pos)
        while text[pos].isspace():
            pos += 1
        pos += 1  # colon, already validated by json.loads at the call site.
        while text[pos].isspace():
            pos += 1
        start = pos
        _, pos = decoder.raw_decode(text, pos)
        if key == "model":
            spans.append((start, pos))
        while text[pos].isspace():
            pos += 1
        if text[pos] == "}":
            break
        pos += 1
    if len(spans) != 1:
        raise ValueError("ambiguous model field")
    start, end = spans[0]
    return (text[:start] + json.dumps(model) + text[end:]).encode("utf-8")
