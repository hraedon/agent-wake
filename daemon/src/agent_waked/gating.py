"""HMAC gating for inbound wake events."""

import hashlib
import hmac
from typing import Any


def verify_signature_any(
    body: bytes, secrets: list[bytes], signature_header: str
) -> bool:
    """Verify HMAC-SHA256 signature against any of the provided secrets.

    Short-circuits on the first match.  Used during rotation windows where
    both the current and previous secret are valid.

    Args:
        body: The raw request body bytes.
        secrets: Ordered list of HMAC secret bytes to try.
        signature_header: The value of the X-AgentWake-Signature header.

    Returns:
        True if the signature matches any secret, False otherwise.
    """
    for secret in secrets:
        if verify_signature(body, secret, signature_header):
            return True
    return False


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


def check_trigger_identity(
    source_cfg: dict[str, Any],
    sender_identity_header: str | None,
) -> str | None:
    """Check sender identity against the source's allowlist.

    Per design/v1-implementation-spec.md §1: each source may declare
    ``allowed_trigger_identities`` — a list of ``principal_id`` values
    permitted to send events for that source.  If the list is absent or
    empty, any authenticated sender (HMAC-valid) is accepted (single-user
    backward-compat).

    The sender declares their identity via the ``X-AgentWake-Identity``
    header.  If the source has an allowlist but the header is missing,
    the request is rejected.

    Args:
        source_cfg: The source's config dict (from ``config["sources"][name]``).
        sender_identity_header: Value of the ``X-AgentWake-Identity`` header,
            or None if absent.

    Returns:
        None if accepted.  A string error code (e.g. ``"identity_missing"``,
        ``"identity_not_allowed"``) if rejected.
    """
    allowed = source_cfg.get("allowed_trigger_identities")
    if not allowed:
        return None

    if not sender_identity_header:
        return "identity_missing"

    if sender_identity_header not in allowed:
        return "identity_not_allowed"

    return None
