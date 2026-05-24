import pytest
import hmac
import hashlib

from agent_wake_claude.gating import verify_signature


def test_verify_valid_signature():
    body = b'{"hello":"world"}'
    secret = b"supersecret"
    sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    assert verify_signature(body, secret, sig) is True


def test_verify_invalid_signature():
    body = b'{"hello":"world"}'
    secret = b"supersecret"
    assert verify_signature(body, secret, "sha256=deadbeef") is False


def test_verify_missing_signature():
    assert verify_signature(b"x", b"y", "") is False


def test_verify_unknown_format():
    assert verify_signature(b"x", b"y", "md5=abc") is False
