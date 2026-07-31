import hashlib
import hmac

from agent_waked.gating import check_trigger_identity, verify_signature


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


# ── trigger identity gating ─────────────────────────────────────────


def test_identity_no_allowlist_accepts():
    """Source with no allowed_trigger_identities accepts any sender."""
    cfg = {"secret": b"x"}
    assert check_trigger_identity(cfg, "anyone") is None


def test_identity_no_allowlist_accepts_missing_header():
    cfg = {"secret": b"x"}
    assert check_trigger_identity(cfg, None) is None


def test_identity_allowlisted_sender_accepted():
    cfg = {
        "secret": b"x",
        "allowed_trigger_identities": ["alice", "bob"],
    }
    assert check_trigger_identity(cfg, "alice") is None


def test_identity_unlisted_sender_rejected():
    cfg = {
        "secret": b"x",
        "allowed_trigger_identities": ["alice", "bob"],
    }
    assert check_trigger_identity(cfg, "mallory") == "identity_not_allowed"


def test_identity_missing_header_rejected():
    cfg = {
        "secret": b"x",
        "allowed_trigger_identities": ["alice"],
    }
    assert check_trigger_identity(cfg, None) == "identity_missing"


def test_identity_empty_allowlist_accepts():
    """Empty list is treated as no allowlist (backward-compat)."""
    cfg = {"secret": b"x", "allowed_trigger_identities": []}
    assert check_trigger_identity(cfg, "anyone") is None


def test_string_allowed_trigger_identities_denies_instead_of_substring_matching():
    """Defence in depth: the predicate must not trust its caller's validation.

    ``header not in allowed`` is a membership test on a list but a *substring*
    test on a string, so "alice" would admit "ali". config.load_config rejects
    the shape at startup, but this predicate is also called with hand-built
    dicts, so it has to be safe on its own.
    """
    cfg = {"secret": b"x", "allowed_trigger_identities": "alice"}
    assert check_trigger_identity(cfg, "ali") == "identity_not_allowed"
    assert check_trigger_identity(cfg, "alice") == "identity_not_allowed"
    assert check_trigger_identity(cfg, "e") == "identity_not_allowed"


def test_list_allowed_trigger_identities_still_works():
    """The string guard must not break the supported list path."""
    cfg = {"secret": b"x", "allowed_trigger_identities": ["alice", "bob"]}
    assert check_trigger_identity(cfg, "alice") is None
    assert check_trigger_identity(cfg, "ali") == "identity_not_allowed"
