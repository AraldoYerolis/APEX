"""Tests for HMAC action token generation and validation."""
import time

import pytest

from apex.actions.tokens import generate_token, validate_token


SECRET = "test-secret-key-12345"


def test_valid_token():
    uid = "abc123"
    token = generate_token(uid, SECRET, ttl_hours=24)
    valid, reason = validate_token(uid, token, SECRET)
    assert valid
    assert reason == "ok"


def test_wrong_uid():
    uid = "abc123"
    token = generate_token(uid, SECRET, ttl_hours=24)
    valid, reason = validate_token("different-uid", token, SECRET)
    assert not valid
    assert "signature" in reason.lower()


def test_wrong_secret():
    uid = "abc123"
    token = generate_token(uid, SECRET, ttl_hours=24)
    valid, reason = validate_token(uid, token, "wrong-secret")
    assert not valid
    assert "signature" in reason.lower()


def test_expired_token():
    uid = "abc123"
    # Generate with TTL that's already past
    expiry = int(time.time()) - 1
    import hashlib, hmac
    payload = f"{uid}:{expiry}"
    sig = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    token = f"{expiry}:{sig}"

    valid, reason = validate_token(uid, token, SECRET)
    assert not valid
    assert "expired" in reason.lower()


def test_malformed_token():
    valid, reason = validate_token("abc", "notavalidtoken", SECRET)
    assert not valid
    assert "malformed" in reason.lower()


def test_empty_token():
    valid, reason = validate_token("abc", "", SECRET)
    assert not valid


def test_token_unique_per_uid():
    t1 = generate_token("uid1", SECRET)
    t2 = generate_token("uid2", SECRET)
    assert t1 != t2
