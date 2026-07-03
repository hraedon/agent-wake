"""Tests for suite_config — suite.env parsing and precedence."""

import os
from pathlib import Path

import pytest

from agent_waked.suite_config import (
    _parse_env_file,
    resolve_suite_env,
    regista_config,
    is_suite_configured,
    per_user_path,
    system_path,
)


def _write_env_file(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_parse_env_file_basic(tmp_path):
    f = tmp_path / "suite.env"
    _write_env_file(f, [
        "# comment line",
        "REGISTA_DSN=postgresql://user@host/db",
        "REGISTA_KEY_PATH=file:/path/to/key",
        "# another comment",
        "AGENT_WAKE_PROJECT=wake-project",
    ])
    result = _parse_env_file(f)
    assert result["REGISTA_DSN"] == "postgresql://user@host/db"
    assert result["REGISTA_KEY_PATH"] == "file:/path/to/key"
    assert result["AGENT_WAKE_PROJECT"] == "wake-project"


def test_parse_env_file_strips_quotes(tmp_path):
    f = tmp_path / "suite.env"
    _write_env_file(f, [
        'REGISTA_DSN="postgresql://user@host/db"',
        "REGISTA_KEY_PATH='file:/path/to/key'",
    ])
    result = _parse_env_file(f)
    assert result["REGISTA_DSN"] == "postgresql://user@host/db"
    assert result["REGISTA_KEY_PATH"] == "file:/path/to/key"


def test_parse_env_file_skips_malformed(tmp_path):
    f = tmp_path / "suite.env"
    _write_env_file(f, [
        "REGISTA_DSN=postgresql://user@host/db",
        "MALFORMED_NO_EQUALS",
        "=NO_KEY",
    ])
    result = _parse_env_file(f)
    assert result == {"REGISTA_DSN": "postgresql://user@host/db"}


def test_parse_env_file_missing_file(tmp_path):
    result = _parse_env_file(tmp_path / "nonexistent.env")
    assert result == {}


def test_resolve_precedence_process_env_wins(tmp_path, monkeypatch):
    """Process env should override suite.env files."""
    user_env = tmp_path / "user.env"
    _write_env_file(user_env, ["REGISTA_DSN=postgresql://from-file/db"])
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(user_env))

    # System file doesn't exist in this test
    monkeypatch.setattr("agent_waked.suite_config.system_path", lambda: tmp_path / "nonexistent-system.env")

    # Process env should win
    monkeypatch.setenv("REGISTA_DSN", "postgresql://from-process/db")
    result = resolve_suite_env()
    assert result["REGISTA_DSN"] == "postgresql://from-process/db"


def test_resolve_precedence_user_over_system(tmp_path, monkeypatch):
    """Per-user suite.env should override system suite.env."""
    user_env = tmp_path / "user.env"
    sys_env = tmp_path / "sys.env"
    _write_env_file(user_env, ["REGISTA_DSN=postgresql://from-user/db"])
    _write_env_file(sys_env, ["REGISTA_DSN=postgresql://from-system/db"])

    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(user_env))
    monkeypatch.setattr("agent_waked.suite_config.system_path", lambda: sys_env)
    monkeypatch.delenv("REGISTA_DSN", raising=False)

    result = resolve_suite_env()
    assert result["REGISTA_DSN"] == "postgresql://from-user/db"


def test_resolve_only_returns_canonical_vars(tmp_path, monkeypatch):
    """Non-canonical vars in suite.env should be filtered out."""
    user_env = tmp_path / "user.env"
    _write_env_file(user_env, [
        "REGISTA_DSN=postgresql://user@host/db",
        "SOME_OTHER_VAR=value",
        "AGENT_WAKE_CONFIG=/custom/path",
    ])
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(user_env))
    monkeypatch.setattr("agent_waked.suite_config.system_path", lambda: tmp_path / "nonexistent.env")
    monkeypatch.delenv("REGISTA_DSN", raising=False)

    result = resolve_suite_env()
    assert "REGISTA_DSN" in result
    assert "SOME_OTHER_VAR" not in result
    assert "AGENT_WAKE_CONFIG" not in result


def test_regista_config_with_ssl(tmp_path, monkeypatch):
    user_env = tmp_path / "user.env"
    _write_env_file(user_env, [
        "REGISTA_DSN=postgresql://user@host/db",
        "REGISTA_REQUIRE_SSL=true",
        "AGENT_WAKE_PROJECT=my-project",
    ])
    monkeypatch.setenv("AGENT_SUITE_CONFIG", str(user_env))
    monkeypatch.setattr("agent_waked.suite_config.system_path", lambda: tmp_path / "nonexistent.env")
    monkeypatch.delenv("REGISTA_DSN", raising=False)
    monkeypatch.delenv("REGISTA_REQUIRE_SSL", raising=False)
    monkeypatch.delenv("AGENT_WAKE_PROJECT", raising=False)

    reg = regista_config()
    assert reg["dsn"] == "postgresql://user@host/db"
    assert reg["require_ssl"] is True
    assert reg["project"] == "my-project"


def test_regista_config_defaults(tmp_path, monkeypatch):
    """When nothing is configured, regista_config returns Nones."""
    monkeypatch.setattr("agent_waked.suite_config.per_user_path", lambda: tmp_path / "nonexistent.env")
    monkeypatch.setattr("agent_waked.suite_config.system_path", lambda: tmp_path / "nonexistent2.env")
    for var in ("REGISTA_DSN", "REGISTA_KEY_PATH", "REGISTA_REQUIRE_SSL", "AGENT_WAKE_PROJECT"):
        monkeypatch.delenv(var, raising=False)

    reg = regista_config()
    assert reg["dsn"] is None
    assert reg["key_path"] is None
    assert reg["require_ssl"] is False
    assert reg["project"] is None


def test_is_suite_configured(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_waked.suite_config.per_user_path", lambda: tmp_path / "user.env")
    monkeypatch.setattr("agent_waked.suite_config.system_path", lambda: tmp_path / "sys.env")

    assert not is_suite_configured()

    _write_env_file(tmp_path / "user.env", ["REGISTA_DSN=x"])
    assert is_suite_configured()
