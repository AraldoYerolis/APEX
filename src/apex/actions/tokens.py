"""HMAC-based action tokens for Pushover action links.

Token format: {uid}:{expiry_unix}:{hmac_hex}

The token is URL-safe and does not require DB storage.
"""
from __future__ import annotations

import hashlib
import hmac
import time


def _sign(secret: str, message: str) -> str:
    return hmac.new(
        secret.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()


def generate_token(uid: str, secret: str, ttl_hours: int = 48) -> str:
    expiry = int(time.time()) + ttl_hours * 3600
    payload = f"{uid}:{expiry}"
    sig = _sign(secret, payload)
    return f"{expiry}:{sig}"


def validate_token(uid: str, token: str, secret: str) -> tuple[bool, str]:
    """Returns (valid, reason)."""
    try:
        parts = token.split(":")
        if len(parts) != 2:
            return False, "malformed token"
        expiry_str, sig = parts
        expiry = int(expiry_str)
    except (ValueError, AttributeError):
        return False, "malformed token"

    if time.time() > expiry:
        return False, "token expired"

    payload = f"{uid}:{expiry}"
    expected = _sign(secret, payload)
    if not hmac.compare_digest(expected, sig):
        return False, "invalid signature"

    return True, "ok"
