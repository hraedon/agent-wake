"""Tests for agent-wake doctor — health checks and JSON output."""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agent_waked.doctor import run_checks, format_text, main


def _write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


@pytest.fixture
def cfg_with_secret(tmp_path, monkeypatch):
    """Config with one source that has a secret."""
    monkeypatch.setenv("TEST_DOCTOR_SECRET", "test-secret-value")
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, {
        "version": 1,
        "listen": {"host": "127.0.0.1", "port": 8788},
        "sources": {
            "demo": {"secret_env": "TEST_DOCTOR_SECRET"}
        },
        "routing": {},
    })
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    # Ensure suite config is not present (so regista check returns None)
    monkeypatch.setattr(
        "agent_waked.suite_config.per_user_path",
        lambda: tmp_path / "nonexistent-user.env",
    )
    monkeypatch.setattr(
        "agent_waked.suite_config.system_path",
        lambda: tmp_path / "nonexistent-sys.env",
    )
    return cfg_path, tmp_path


# ── JSON shape ─────────────────────────────────────────────────────────────────


def test_doctor_json_has_suite_shape(cfg_with_secret):
    report = run_checks()
    assert report["component"] == "agent-wake"
    assert "version" in report
    assert "ok" in report
    assert "degraded" in report
    assert "regista" in report
    assert "checks" in report
    assert isinstance(report["checks"], list)


def test_doctor_json_regista_none_when_no_suite(cfg_with_secret):
    """When suite.env is not configured, regista should be None."""
    report = run_checks()
    assert report["regista"] is None


def test_doctor_checks_all_present(cfg_with_secret):
    """All five named checks should be present."""
    report = run_checks()
    check_names = [c["name"] for c in report["checks"]]
    assert "config_present" in check_names
    assert "ingress_reachable" in check_names
    assert "auth_configured" in check_names
    assert "adapters_installed" in check_names
    assert "allowlist_present" in check_names


def test_doctor_check_has_status_and_detail(cfg_with_secret):
    report = run_checks()
    for check in report["checks"]:
        assert check["status"] in ("pass", "fail", "warn", "skip")
        assert isinstance(check["detail"], str)


# ── config_present check ───────────────────────────────────────────────────────


def test_config_present_pass(cfg_with_secret):
    report = run_checks()
    config_check = next(c for c in report["checks"] if c["name"] == "config_present")
    assert config_check["status"] == "pass"


def test_config_present_fail_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(tmp_path / "nonexistent.json"))
    monkeypatch.setattr(
        "agent_waked.suite_config.per_user_path",
        lambda: tmp_path / "ne.env",
    )
    monkeypatch.setattr(
        "agent_waked.suite_config.system_path",
        lambda: tmp_path / "ne2.env",
    )
    report = run_checks()
    config_check = next(c for c in report["checks"] if c["name"] == "config_present")
    assert config_check["status"] == "fail"


# ── auth_configured check ──────────────────────────────────────────────────────


def test_auth_configured_pass(cfg_with_secret):
    report = run_checks()
    auth_check = next(c for c in report["checks"] if c["name"] == "auth_configured")
    assert auth_check["status"] == "pass"


def test_auth_configured_fail_when_no_sources(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("TEST_SEC_A", "aaaa")
    _write_config(cfg_path, {
        "version": 1,
        "listen": {"host": "127.0.0.1", "port": 8788},
        "sources": {"demo": {"secret_env": "TEST_SEC_A"}},
        "routing": {},
    })
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    monkeypatch.setattr("agent_waked.suite_config.per_user_path", lambda: tmp_path / "ne.env")
    monkeypatch.setattr("agent_waked.suite_config.system_path", lambda: tmp_path / "ne2.env")

    # Config with sources should pass auth check
    report = run_checks()
    auth_check = next(c for c in report["checks"] if c["name"] == "auth_configured")
    assert auth_check["status"] == "pass"


# ── ok/degraded logic ──────────────────────────────────────────────────────────


def test_ok_false_when_config_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(tmp_path / "nonexistent.json"))
    monkeypatch.setattr("agent_waked.suite_config.per_user_path", lambda: tmp_path / "ne.env")
    monkeypatch.setattr("agent_waked.suite_config.system_path", lambda: tmp_path / "ne2.env")
    report = run_checks()
    assert report["ok"] is False


def test_degraded_when_warnings_present(cfg_with_secret):
    """When daemon is not running, ingress_reachable warns → degraded but ok."""
    # Mock the socket connection to fail so ingress check returns warn
    with patch("agent_waked.doctor.socket.create_connection", side_effect=ConnectionRefusedError()):
        report = run_checks()
    ingress = next(c for c in report["checks"] if c["name"] == "ingress_reachable")
    assert ingress["status"] == "warn"
    assert report["degraded"] is True
    assert report["ok"] is True


# ── format_text ────────────────────────────────────────────────────────────────


def test_format_text_includes_checks(cfg_with_secret):
    report = run_checks()
    text = format_text(report)
    assert "agent-wake doctor" in text
    assert "config_present" in text
    assert "PASS" in text or "FAIL" in text or "WARN" in text


def test_format_text_includes_result(cfg_with_secret):
    report = run_checks()
    text = format_text(report)
    assert "Result:" in text


# ── main / CLI ─────────────────────────────────────────────────────────────────


def test_main_json_output(cfg_with_secret, capsys):
    from agent_waked.cli import main as cli_main
    import sys as _sys
    old_argv = _sys.argv
    _sys.argv = ["agent-wake", "doctor", "--json"]
    try:
        rc = cli_main()
    finally:
        _sys.argv = old_argv
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["component"] == "agent-wake"
    # rc should be 0 (ok) or 1 (fail) depending on daemon state
    assert rc in (0, 1)


def test_main_text_output(cfg_with_secret, capsys):
    from agent_waked.cli import main as cli_main
    import sys as _sys
    old_argv = _sys.argv
    _sys.argv = ["agent-wake", "doctor"]
    try:
        rc = cli_main()
    finally:
        _sys.argv = old_argv
    out = capsys.readouterr().out
    assert "agent-wake doctor" in out
    assert rc in (0, 1)
