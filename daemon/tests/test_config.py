"""Tests for agent_waked.config — schema validation, URI forms, backwards compat."""

import json
from pathlib import Path

import pytest

from agent_waked.config import ConfigError, load_config


def _write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


# ── legacy secret_env form ────────────────────────────────────────────────────

def test_legacy_secret_env_loads(tmp_path, monkeypatch):
    """Legacy secret_env form works and converts to secret_uris."""
    monkeypatch.setenv("MY_SECRET", "hunter2")
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, {
        "version": 1,
        "listen": {"host": "127.0.0.1", "port": 8788},
        "sources": {
            "demo": {"secret_env": "MY_SECRET"}
        }
    })
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg["sources"]["demo"]["secret_uris"] == ["env://MY_SECRET"]


def test_legacy_secret_env_missing_var_raises(tmp_path, monkeypatch):
    """secret_env pointing to unset var raises ConfigError."""
    monkeypatch.delenv("MY_MISSING_SECRET", raising=False)
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, {
        "version": 1,
        "sources": {"demo": {"secret_env": "MY_MISSING_SECRET"}}
    })
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="MY_MISSING_SECRET"):
        load_config()


def test_legacy_two_sources(tmp_path, monkeypatch):
    """Two legacy sources both convert properly."""
    monkeypatch.setenv("SEC_A", "aaa")
    monkeypatch.setenv("SEC_B", "bbb")
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, {
        "version": 1,
        "sources": {
            "demo-claude": {"secret_env": "SEC_A"},
            "demo-opencode": {"secret_env": "SEC_B"},
        }
    })
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg["sources"]["demo-claude"]["secret_uris"] == ["env://SEC_A"]
    assert cfg["sources"]["demo-opencode"]["secret_uris"] == ["env://SEC_B"]


# ── new single-secret URI form ────────────────────────────────────────────────

def test_single_uri_env(tmp_path, monkeypatch):
    """secret: env://VAR form parses correctly."""
    monkeypatch.setenv("MY_URI_SECRET", "s3cr3t")
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, {
        "version": 1,
        "sources": {"gh": {"secret": "env://MY_URI_SECRET"}}
    })
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg["sources"]["gh"]["secret_uris"] == ["env://MY_URI_SECRET"]


def test_single_uri_invalid_scheme(tmp_path, monkeypatch):
    """Unknown URI scheme raises ConfigError."""
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, {
        "version": 1,
        "sources": {"gh": {"secret": "s3://bucket/key"}}
    })
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="unsupported URI scheme"):
        load_config()


# ── new multi-secret (rotation) form ─────────────────────────────────────────

def test_secrets_list_form(tmp_path, monkeypatch):
    """secrets: [...] form parses correctly."""
    monkeypatch.setenv("SEC_CUR", "cur")
    monkeypatch.setenv("SEC_PRV", "prv")
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, {
        "version": 1,
        "sources": {
            "sub": {
                "secrets": ["env://SEC_CUR", "env://SEC_PRV"]
            }
        }
    })
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg["sources"]["sub"]["secret_uris"] == ["env://SEC_CUR", "env://SEC_PRV"]


def test_secrets_empty_list_raises(tmp_path, monkeypatch):
    """secrets: [] must be rejected."""
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, {
        "version": 1,
        "sources": {"sub": {"secrets": []}}
    })
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="non-empty list"):
        load_config()


# ── mutual-exclusion enforcement ──────────────────────────────────────────────

def test_mutex_secret_env_and_secret(tmp_path, monkeypatch):
    """secret_env + secret together should be rejected."""
    monkeypatch.setenv("BOTH_SEC", "x")
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, {
        "version": 1,
        "sources": {
            "bad": {
                "secret_env": "BOTH_SEC",
                "secret": "env://BOTH_SEC",
            }
        }
    })
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="mutually exclusive"):
        load_config()


def test_mutex_secret_and_secrets(tmp_path, monkeypatch):
    """secret + secrets together should be rejected."""
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, {
        "version": 1,
        "sources": {
            "bad": {
                "secret": "env://FOO",
                "secrets": ["env://FOO"],
            }
        }
    })
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="mutually exclusive"):
        load_config()


def test_no_secret_field_raises(tmp_path, monkeypatch):
    """Source with no secret form at all should be rejected."""
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, {
        "version": 1,
        "sources": {"bad": {"callback_url": "http://example.com"}}
    })
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="exactly one of"):
        load_config()


# ── vault block validation ────────────────────────────────────────────────────

def test_vault_uri_without_vault_block_raises(tmp_path, monkeypatch):
    """vault:// URI without 'vault' config block raises ConfigError."""
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, {
        "version": 1,
        "sources": {"vs": {"secret": "vault://secret/data/foo#key"}}
    })
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="vault"):
        load_config()


def test_vault_block_valid(tmp_path, monkeypatch):
    """Full valid vault config (no actual vault needed for parser)."""
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, {
        "version": 1,
        "sources": {"vs": {"secret": "vault://secret/data/foo#key"}},
        "vault": {
            "addr": "https://vault.example.com:8200",
            "auth": {
                "method": "approle",
                "role_id": "abc",
                "secret_id_file": "~/.config/agent-wake/vault-secret-id",
            }
        }
    })
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg["vault"]["addr"] == "https://vault.example.com:8200"
    assert cfg["sources"]["vs"]["secret_uris"] == ["vault://secret/data/foo#key"]


def test_vault_block_missing_addr_raises(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, {
        "version": 1,
        "sources": {"vs": {"secret": "vault://secret/data/foo#key"}},
        "vault": {
            "auth": {
                "method": "approle",
                "role_id": "abc",
                "secret_id_file": "/tmp/sid",
            }
        }
    })
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match=r"vault.addr"):
        load_config()


def test_vault_block_bad_method_raises(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, {
        "version": 1,
        "sources": {"vs": {"secret": "vault://secret/data/foo#key"}},
        "vault": {
            "addr": "https://vault.example.com",
            "auth": {
                "method": "kubernetes",
                "role_id": "abc",
                "secret_id_file": "/tmp/sid",
            }
        }
    })
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="approle"):
        load_config()


# ── BC-013: malformed config surfaces ConfigError, not a traceback ───────────


def test_malformed_json_raises_config_error(tmp_path, monkeypatch):
    """BC-013: invalid JSON must raise ConfigError, not a raw JSONDecodeError."""
    cfg_path = tmp_path / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("{not valid json")
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config()


def test_non_object_json_raises_config_error(tmp_path, monkeypatch):
    """A JSON document that isn't an object (e.g. a list) raises ConfigError
    instead of an AttributeError on ``raw.get``."""
    cfg_path = tmp_path / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("[1, 2, 3]")
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="must be a JSON object"):
        load_config()


def test_scalar_json_raises_config_error(tmp_path, monkeypatch):
    """A bare scalar JSON value is also rejected cleanly."""
    cfg_path = tmp_path / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text('"just a string"')
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="must be a JSON object"):
        load_config()


def test_sources_not_object_raises_config_error(tmp_path, monkeypatch):
    """BC-013 hardening: 'sources' must be an object; a list must not crash
    with an AttributeError on .items()."""
    cfg_path = tmp_path / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    _write_config(cfg_path, {"version": 1, "sources": ["not", "an", "object"]})
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="'sources' must be an object"):
        load_config()


def test_routing_not_object_raises_config_error(tmp_path, monkeypatch):
    """BC-013 hardening: 'routing' must be an object when present."""
    cfg_path = tmp_path / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    _write_config(cfg_path, {"version": 1, "routing": "oops", "sources": {}})
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="'routing' must be an object"):
        load_config()


# ── state block (durable store: BC-WAKE-004 / BC-WAKE-012) ───────────────────


def _state_cfg(tmp_path, monkeypatch, state):
    monkeypatch.setenv("ST_SEC", "x")
    cfg_path = tmp_path / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "version": 1,
        "sources": {"demo": {"secret_env": "ST_SEC"}},
        "routing": {},
    }
    if state is not None:
        body["state"] = state
    _write_config(cfg_path, body)
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    return cfg_path


def test_state_block_absent_defaults_to_empty(tmp_path, monkeypatch):
    _state_cfg(tmp_path, monkeypatch, None)
    cfg = load_config()
    assert cfg["state"] == {}


def test_state_block_normalised(tmp_path, monkeypatch):
    _state_cfg(tmp_path, monkeypatch, {
        "dir": "/var/lib/agent-wake",
        "dedupe_max_rows": 50,
        "default_delivery": "next_session",
    })
    cfg = load_config()
    assert cfg["state"]["dir"] == "/var/lib/agent-wake"
    assert cfg["state"]["dedupe_max_rows"] == 50
    assert cfg["state"]["default_delivery"] == "next_session"
    assert cfg["state"]["enabled"] is True


def test_state_block_rejects_bad_delivery_mode(tmp_path, monkeypatch):
    _state_cfg(tmp_path, monkeypatch, {"default_delivery": "teleport"})
    with pytest.raises(ConfigError, match="default_delivery"):
        load_config()


def test_state_block_rejects_non_positive_retention(tmp_path, monkeypatch):
    _state_cfg(tmp_path, monkeypatch, {"dedupe_max_rows": 0})
    with pytest.raises(ConfigError, match="must be a positive integer"):
        load_config()


def test_state_block_rejects_bool_retention(tmp_path, monkeypatch):
    """bool is an int subclass; ``dedupe_max_rows: true`` must not slip through."""
    _state_cfg(tmp_path, monkeypatch, {"pending_max_attempts": True})
    with pytest.raises(ConfigError, match="must be a positive integer"):
        load_config()


def test_state_block_must_be_object(tmp_path, monkeypatch):
    _state_cfg(tmp_path, monkeypatch, "nope")
    with pytest.raises(ConfigError, match="'state' must be an object"):
        load_config()


def test_state_can_be_disabled(tmp_path, monkeypatch):
    _state_cfg(tmp_path, monkeypatch, {"enabled": False})
    assert load_config()["state"]["enabled"] is False


# ── allowed_trigger_identities shape (WI-001) ─────────────────────────────────


def _identities_cfg(tmp_path, monkeypatch, value):
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, {
        "version": 1,
        "sources": {
            "demo": {"secret": "env://AW_S", "allowed_trigger_identities": value},
        },
    })
    monkeypatch.setenv("AW_S", "x")
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))


def test_allowed_trigger_identities_rejects_a_bare_string(tmp_path, monkeypatch):
    """A string would become a *substring* allowlist, not an identity list.

    gating.check_trigger_identity does ``header not in allowed``, so "alice"
    as a string admits the sender "ali".
    """
    _identities_cfg(tmp_path, monkeypatch, "alice")
    with pytest.raises(ConfigError, match="must be a list"):
        load_config()


def test_allowed_trigger_identities_rejects_non_string_entries(tmp_path, monkeypatch):
    _identities_cfg(tmp_path, monkeypatch, ["alice", 7])
    with pytest.raises(ConfigError, match="non-empty string"):
        load_config()


def test_allowed_trigger_identities_rejects_empty_entries(tmp_path, monkeypatch):
    _identities_cfg(tmp_path, monkeypatch, ["alice", ""])
    with pytest.raises(ConfigError, match="non-empty string"):
        load_config()


def test_allowed_trigger_identities_list_is_accepted(tmp_path, monkeypatch):
    _identities_cfg(tmp_path, monkeypatch, ["alice", "bob"])
    cfg = load_config()
    assert cfg["sources"]["demo"]["allowed_trigger_identities"] == ["alice", "bob"]


def test_allowed_trigger_identities_absent_is_accepted(tmp_path, monkeypatch):
    """No allowlist means any HMAC-valid sender — documented v0 behaviour."""
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, {
        "version": 1,
        "sources": {"demo": {"secret": "env://AW_S"}},
    })
    monkeypatch.setenv("AW_S", "x")
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    assert load_config()["sources"]["demo"]["allowed_trigger_identities"] is None
