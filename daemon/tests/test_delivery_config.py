"""Tests for delivery config validation (Plan 005 WI-1.1).

Verifies that the ``delivery`` block in config.json is validated correctly:
- Valid webhook/email channel configs are accepted.
- Missing required fields, bad URI schemes, and unknown channel kinds are rejected.
- The routing table maps principal_id → delivery channel(s).
"""

import json
from pathlib import Path

import pytest

import agent_waked.config as cfg_module
from agent_waked.config import ConfigError, load_config


def _write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _base_cfg(delivery: dict | None = None) -> dict:
    cfg = {
        "version": 1,
        "listen": {"host": "127.0.0.1", "port": 8788},
        "sources": {"demo": {"secret_env": "TEST_SECRET"}},
        "routing": {},
    }
    if delivery is not None:
        cfg["delivery"] = delivery
    return cfg


# ── valid configs ────────────────────────────────────────────────────


def test_valid_webhook_channel(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_SECRET", "s")
    monkeypatch.setenv("WEBHOOK_SECRET", "wh-secret")
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, _base_cfg({
        "operator": {
            "webhook": {
                "url": "https://hooks.example.com/inbox",
                "secret_uri": "env://WEBHOOK_SECRET",
            }
        }
    }))
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    cfg = load_config()
    assert "delivery" in cfg
    assert "operator" in cfg["delivery"]
    assert cfg["delivery"]["operator"]["webhook"]["url"] == "https://hooks.example.com/inbox"
    assert cfg["delivery"]["operator"]["webhook"]["secret_uri"] == "env://WEBHOOK_SECRET"


def test_valid_email_channel(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_SECRET", "s")
    monkeypatch.setenv("SMTP_PASS", "pass")
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, _base_cfg({
        "operator": {
            "email": {
                "smtp_host": "smtp.example.com",
                "smtp_port": 587,
                "from_addr": "wake@example.com",
                "to_addr": "op@example.com",
                "use_tls": True,
                "secret_uri": "env://SMTP_PASS",
            }
        }
    }))
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    cfg = load_config()
    email = cfg["delivery"]["operator"]["email"]
    assert email["smtp_host"] == "smtp.example.com"
    assert email["smtp_port"] == 587
    assert email["use_tls"] is True


def test_both_channels_for_one_principal(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_SECRET", "s")
    monkeypatch.setenv("WH", "wh")
    monkeypatch.setenv("SMTP", "sm")
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, _base_cfg({
        "operator": {
            "webhook": {"url": "https://h.example.com", "secret_uri": "env://WH"},
            "email": {
                "smtp_host": "smtp.example.com", "smtp_port": 465,
                "from_addr": "w@e.com", "to_addr": "o@e.com",
            }
        }
    }))
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    cfg = load_config()
    assert len(cfg["delivery"]["operator"]) == 2


def test_no_delivery_block_ok(monkeypatch, tmp_path):
    """No delivery block is fine — Plan 005 not deployed."""
    monkeypatch.setenv("TEST_SECRET", "s")
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, _base_cfg())
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg["delivery"] == {}


# ── invalid configs ──────────────────────────────────────────────────


def test_unknown_channel_kind_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_SECRET", "s")
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, _base_cfg({
        "operator": {"slack": {"url": "https://x"}}
    }))
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="unknown channel kind"):
        load_config()


def test_webhook_missing_url(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_SECRET", "s")
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, _base_cfg({
        "operator": {"webhook": {"secret_uri": "env://K"}}
    }))
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="'url' is required"):
        load_config()


def test_webhook_missing_secret_uri(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_SECRET", "s")
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, _base_cfg({
        "operator": {"webhook": {"url": "https://x"}}
    }))
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="'secret_uri' is required"):
        load_config()


def test_webhook_bad_url_scheme(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_SECRET", "s")
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, _base_cfg({
        "operator": {"webhook": {"url": "ftp://x", "secret_uri": "env://K"}}
    }))
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="http or https"):
        load_config()


def test_email_missing_required_field(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_SECRET", "s")
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, _base_cfg({
        "operator": {"email": {"smtp_host": "smtp.example.com", "smtp_port": 587}}
    }))
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="'from_addr' is required"):
        load_config()


def test_email_bad_port(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_SECRET", "s")
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, _base_cfg({
        "operator": {"email": {
            "smtp_host": "s", "smtp_port": 99999,
            "from_addr": "a@b", "to_addr": "c@d",
        }}
    }))
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="smtp_port"):
        load_config()


def test_email_bad_use_tls(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_SECRET", "s")
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, _base_cfg({
        "operator": {"email": {
            "smtp_host": "s", "smtp_port": 587,
            "from_addr": "a@b", "to_addr": "c@d", "use_tls": "yes",
        }}
    }))
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="use_tls"):
        load_config()


def test_delivery_not_object(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_SECRET", "s")
    cfg_path = tmp_path / "config.json"
    c = _base_cfg()
    c["delivery"] = "not an object"
    _write_config(cfg_path, c)
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="object"):
        load_config()


def test_delivery_empty_principal_id(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_SECRET", "s")
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, _base_cfg({"": {"webhook": {"url": "https://x", "secret_uri": "env://K"}}}))
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="non-empty"):
        load_config()


# ── SSRF protection (Plan 005 review: webhook URL must not target internal IPs) ─


def _webhook_cfg(url: str) -> dict:
    return _base_cfg({
        "operator": {"webhook": {"url": url, "secret_uri": "env://K"}}
    })


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",       # loopback v4
        "::1",             # loopback v6
        "169.254.169.254", # link-local / cloud metadata
        "10.0.0.1",        # private 10/8
        "172.16.0.1",      # private 172.16/12
        "192.168.1.1",     # private 192.168/16
        "100.64.0.1",      # CGNAT 100.64/10 (RFC 6598 — is_private is False)
        "0.0.0.0",         # unspecified
        "224.0.0.1",       # multicast
        # IPv4-mapped v6 forms. CGNAT specifically was a live bypass here:
        # ``IPv6Address in IPv4Network`` is False on a version mismatch, and
        # CPython's mapped handling covers is_private but not 100.64.0.0/10.
        "::ffff:100.64.0.1",
        "::ffff:6440:1",              # same address, hex spelling
        "0:0:0:0:0:ffff:100.64.0.1",  # same address, expanded
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "::ffff:10.0.0.1",
    ],
)
def test_webhook_ssrf_rejects_forbidden_ip(monkeypatch, tmp_path, ip):
    monkeypatch.setenv("TEST_SECRET", "s")
    monkeypatch.setenv("K", "k")
    monkeypatch.setattr(cfg_module, "_resolve_hostname", lambda host: [ip])
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, _webhook_cfg("https://hooks.example.com/inbox"))
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="forbidden address"):
        load_config()


def test_webhook_ssrf_accepts_public_ip(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_SECRET", "s")
    monkeypatch.setenv("K", "k")
    monkeypatch.setattr(cfg_module, "_resolve_hostname", lambda host: ["93.184.216.34"])
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, _webhook_cfg("https://hooks.example.com/inbox"))
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg["delivery"]["operator"]["webhook"]["url"] == "https://hooks.example.com/inbox"


def test_webhook_ssrf_rejects_any_internal_in_multi_resolve(monkeypatch, tmp_path):
    """If any resolved address is forbidden, reject (don't allow mixed results)."""
    monkeypatch.setenv("TEST_SECRET", "s")
    monkeypatch.setenv("K", "k")
    monkeypatch.setattr(
        cfg_module,
        "_resolve_hostname",
        lambda host: ["93.184.216.34", "127.0.0.1"],
    )
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, _webhook_cfg("https://hooks.example.com/inbox"))
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="forbidden address"):
        load_config()


def test_webhook_ssrf_rejects_unresolvable_hostname(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_SECRET", "s")
    monkeypatch.setenv("K", "k")

    def _fail(host: str) -> list[str]:
        raise OSError("no such host")

    monkeypatch.setattr(cfg_module, "_resolve_hostname", _fail)
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, _webhook_cfg("https://hooks.example.com/inbox"))
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match="does not resolve"):
        load_config()


def test_webhook_ssrf_accepts_a_mapped_public_ip(monkeypatch, tmp_path):
    """Unwrapping mapped addresses must not over-block legitimate targets."""
    monkeypatch.setenv("TEST_SECRET", "s")
    monkeypatch.setenv("K", "k")
    monkeypatch.setattr(
        cfg_module, "_resolve_hostname", lambda host: ["::ffff:93.184.216.34"]
    )
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, _webhook_cfg("https://hooks.example.com/inbox"))
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    assert load_config()["delivery"]["operator"]["webhook"]["url"] == (
        "https://hooks.example.com/inbox"
    )
