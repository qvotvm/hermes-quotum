"""seat meter identity shared by the provider and standalone dashboard."""
import hashlib
import logging
import re
import threading

logger = logging.getLogger("quotum.meter_handle")
_INVALID_WARNED = False
_WARNING_LOCK = threading.Lock()
PROXY_MESSAGE = ('quotum: key is behind a secrets proxy. set QUOTUM_METER_HANDLE to the first 16 hex of sha256 of your seat key: '
                 'printf %s "$KEY" | sha256sum | cut -c1-16')


def key_hash(key: str) -> str:
    """the first 16 hex of the stripped key's sha256."""
    return hashlib.sha256(key.strip().encode()).hexdigest()[:16]


def meter_handle(key: str, value: str | None = None) -> str | None:
    """prefer a valid explicit handle; proxy markers have no usable hash."""
    global _INVALID_WARNED
    key = key.strip()
    if not key:
        return None
    if value is not None:
        if re.fullmatch(r"[0-9a-f]{16}", value):
            return value
        with _WARNING_LOCK:
            if not _INVALID_WARNED:
                _INVALID_WARNED = True
                logger.warning("quotum: invalid QUOTUM_METER_HANDLE. expected 16 lowercase hex characters; ignored.")
    return None if key.startswith("ibsec_") else key_hash(key)
