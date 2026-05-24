"""HMAC gating for inbound wake events."""

import hmac
import hashlib


def verify_signature(body: bytes, secret: bytes, signature_header: str) -> bool:
    """Verify HMAC-SHA256 signature against the raw body.

    Args:
        body: The raw request body bytes.
        secret: The shared HMAC secret bytes.
        signature_header: The value of the X-AgentWake-Signature header,
            expected format: "sha256=<hex>".

    Returns:
        True if the signature is valid, False otherwise.
    """
    if not signature_header:
        return False

    expected = signature_header.strip()
    if not expected.startswith("sha256="):
        return False

    expected_hex = expected[7:]
    try:
        expected_digest = bytes.fromhex(expected_hex)
    except ValueError:
        return False

    computed = hmac.new(secret, body, hashlib.sha256).digest()
    return hmac.compare_digest(computed, expected_digest)
