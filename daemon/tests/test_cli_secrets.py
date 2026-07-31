"""Tests for agent-wake secrets CLI subcommands.

All tests run against a temp HOME with a fixture config.json.
No real Vault server is needed — vault tests are mocked.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_waked.cli import main as cli_main
from agent_waked.cli.secrets import (
    _source_to_uris,
)

# ── fixtures ──────────────────────────────────────────────────────────────────


def _write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


@pytest.fixture
def cfg_env(tmp_path, monkeypatch):
    """A minimal env-based config with one source."""
    monkeypatch.setenv("TEST_SEC_A", "aaaa")
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, {
        "version": 1,
        "listen": {"host": "127.0.0.1", "port": 8788},
        "sources": {
            "demo": {"secret_env": "TEST_SEC_A"}
        },
        "routing": {},
    })
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    monkeypatch.setenv("AGENT_WAKE_SECRETS_ENV", str(tmp_path / "secrets.env"))
    return cfg_path, tmp_path


@pytest.fixture
def cfg_empty(tmp_path, monkeypatch):
    """Config with no env var needed (for add tests)."""
    monkeypatch.setenv("EXISTING_SEC", "existing")
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, {
        "version": 1,
        "listen": {"host": "127.0.0.1", "port": 8788},
        "sources": {
            "existing": {"secret_env": "EXISTING_SEC"}
        },
        "routing": {},
    })
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    monkeypatch.setenv("AGENT_WAKE_SECRETS_ENV", str(tmp_path / "secrets.env"))
    return cfg_path, tmp_path


def _run(args: list[str]) -> int:
    """Run the CLI with given args list, returns exit code."""
    import sys as _sys
    old_argv = _sys.argv
    _sys.argv = ["agent-wake", *args]
    try:
        return cli_main()
    finally:
        _sys.argv = old_argv


# ── list ──────────────────────────────────────────────────────────────────────


def test_secrets_list(cfg_env, capsys):
    _cfg_path, _tmp_path = cfg_env
    rc = _run(["secrets", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "demo" in out
    assert "env" in out


def test_secrets_list_no_values_in_output(cfg_env, capsys):
    """Secret material must never appear in list output."""
    _cfg_path, _tmp_path = cfg_env
    _run(["secrets", "list"])
    out = capsys.readouterr().out
    assert "aaaa" not in out  # the actual secret value


# ── add --backend env ─────────────────────────────────────────────────────────


def test_secrets_add_env_creates_source(cfg_empty, capsys):
    cfg_path, _tmp_path = cfg_empty
    rc = _run(["secrets", "add", "newsource", "--backend", "env"])
    assert rc == 0
    raw = json.loads(cfg_path.read_text())
    assert "newsource" in raw["sources"]
    src = raw["sources"]["newsource"]
    assert "secret" in src
    assert src["secret"].startswith("env://")


def test_secrets_add_env_writes_env_file(cfg_empty, capsys):
    _cfg_path, tmp_path = cfg_empty
    rc = _run(["secrets", "add", "newsource2", "--backend", "env"])
    assert rc == 0
    env_file = tmp_path / "secrets.env"
    assert env_file.exists()
    content = env_file.read_text()
    assert "AGENT_WAKE_NEWSOURCE2_SECRET=" in content


def test_secrets_add_env_prints_secret_once(cfg_empty, capsys):
    _cfg_path, _tmp_path = cfg_empty
    rc = _run(["secrets", "add", "newsource3", "--backend", "env"])
    assert rc == 0
    out = capsys.readouterr().out
    # Secret should appear in stdout
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    # There should be a 64-char hex line (32 bytes hex)
    hex_lines = [line for line in lines if len(line) == 64 and all(c in "0123456789abcdef" for c in line)]
    assert len(hex_lines) == 1


def test_secrets_add_env_custom_name(cfg_empty, capsys):
    cfg_path, tmp_path = cfg_empty
    rc = _run(["secrets", "add", "src4", "--backend", "env", "--name", "MY_CUSTOM_VAR"])
    assert rc == 0
    raw = json.loads(cfg_path.read_text())
    assert raw["sources"]["src4"]["secret"] == "env://MY_CUSTOM_VAR"
    env_file = tmp_path / "secrets.env"
    assert "MY_CUSTOM_VAR=" in env_file.read_text()


def test_secrets_add_env_refuses_existing_source(cfg_env, capsys):
    _cfg_path, _tmp_path = cfg_env
    rc = _run(["secrets", "add", "demo", "--backend", "env"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "already exists" in err


def test_secrets_add_env_file_mode_600(cfg_empty):
    _cfg_path, tmp_path = cfg_empty
    rc = _run(["secrets", "add", "sectest", "--backend", "env"])
    assert rc == 0
    env_file = tmp_path / "secrets.env"
    mode = oct(env_file.stat().st_mode)[-3:]
    assert mode == "600"


# ── rotate ────────────────────────────────────────────────────────────────────


def test_secrets_rotate_promotes_to_list(cfg_env, monkeypatch, capsys):
    cfg_path, _tmp_path = cfg_env
    monkeypatch.setattr("agent_waked.cli.secrets._sighup_daemon", lambda: None)
    rc = _run(["secrets", "rotate", "demo"])
    assert rc == 0
    raw = json.loads(cfg_path.read_text())
    src = raw["sources"]["demo"]
    # Should be promoted to secrets: [...] list
    assert "secrets" in src
    uris = src["secrets"]
    assert len(uris) == 2
    # New secret is first
    assert uris[0] != "env://TEST_SEC_A"
    # Old secret preserved as second
    assert uris[1] == "env://TEST_SEC_A"


def test_secrets_rotate_trims_to_two(cfg_env, monkeypatch, capsys):
    """Second rotation should trim list back to 2."""
    cfg_path, _tmp_path = cfg_env
    monkeypatch.setattr("agent_waked.cli.secrets._sighup_daemon", lambda: None)
    # First rotate
    _run(["secrets", "rotate", "demo"])
    # Second rotate
    rc = _run(["secrets", "rotate", "demo"])
    assert rc == 0
    raw = json.loads(cfg_path.read_text())
    uris = raw["sources"]["demo"]["secrets"]
    assert len(uris) == 2


def test_secrets_rotate_prints_new_secret(cfg_env, monkeypatch, capsys):
    _cfg_path, _tmp_path = cfg_env
    monkeypatch.setattr("agent_waked.cli.secrets._sighup_daemon", lambda: None)
    rc = _run(["secrets", "rotate", "demo"])
    assert rc == 0
    out = capsys.readouterr().out
    hex_lines = [line.strip() for line in out.splitlines()
                 if len(line.strip()) == 64 and all(c in "0123456789abcdef" for c in line.strip())]
    assert len(hex_lines) == 1


def test_secrets_rotate_unknown_source(cfg_env, capsys):
    _cfg_path, _tmp_path = cfg_env
    rc = _run(["secrets", "rotate", "doesntexist"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "not found" in err


# ── remove ────────────────────────────────────────────────────────────────────


def test_secrets_remove_drops_source(cfg_env, monkeypatch, capsys):
    # Need a second source so config remains valid after remove
    raw = json.loads((cfg_env[0]).read_text())
    monkeypatch.setenv("TEST_SEC_B", "bbbb")
    raw["sources"]["other"] = {"secret_env": "TEST_SEC_B"}
    (cfg_env[0]).write_text(json.dumps(raw))

    rc = _run(["secrets", "remove", "demo"])
    assert rc == 0
    updated = json.loads((cfg_env[0]).read_text())
    assert "demo" not in updated["sources"]
    assert "other" in updated["sources"]


def test_secrets_remove_unknown_source(cfg_env, capsys):
    rc = _run(["secrets", "remove", "nosuchsource"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "not found" in err


def test_secrets_remove_cleans_env_file(cfg_empty, monkeypatch, capsys):
    """Remove should clean up the env-file entry for env:// URIs."""
    cfg_path, tmp_path = cfg_empty
    # Add a source with env backend
    _run(["secrets", "add", "toclean", "--backend", "env"])
    env_file = tmp_path / "secrets.env"
    assert "AGENT_WAKE_TOCLEAN_SECRET=" in env_file.read_text()

    # Now remove it — need existing source to remain valid
    json.loads(cfg_path.read_text())
    # existing source is already there from cfg_empty fixture
    rc = _run(["secrets", "remove", "toclean"])
    assert rc == 0
    updated_env = env_file.read_text()
    assert "AGENT_WAKE_TOCLEAN_SECRET=" not in updated_env


# ── add --backend vault (mocked) ──────────────────────────────────────────────


def test_secrets_add_vault_requires_vault_block(cfg_empty, capsys):
    rc = _run(["secrets", "add", "vsrc", "--backend", "vault", "--path", "secret/data/foo"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "vault" in err.lower()


def test_secrets_add_vault_requires_path(cfg_empty, capsys):
    rc = _run(["secrets", "add", "vsrc", "--backend", "vault"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "--path" in err


def test_secrets_add_vault_writes_config(cfg_empty, tmp_path, capsys):
    """With mocked vault, add --backend vault updates config.json."""
    cfg_path, tmp = cfg_empty
    # Inject vault block into config
    raw = json.loads(cfg_path.read_text())
    raw["vault"] = {
        "addr": "https://vault.example.com:8200",
        "auth": {
            "method": "approle",
            "role_id": "r",
            "secret_id_file": str(tmp / "sid"),
        }
    }
    (tmp / "sid").write_text("mysecretid")
    cfg_path.write_text(json.dumps(raw))

    mock_client = MagicMock()
    mock_client.is_authenticated.return_value = True

    with patch("hvac.Client", return_value=mock_client):
        rc = _run(["secrets", "add", "vsrc", "--backend", "vault",
                   "--path", "secret/data/foo", "--key", "current"])

    assert rc == 0
    updated = json.loads(cfg_path.read_text())
    assert "vsrc" in updated["sources"]
    assert "vault://" in updated["sources"]["vsrc"]["secret"]


# ── _source_to_uris helper ────────────────────────────────────────────────────


def test_source_to_uris_secret_env():
    assert _source_to_uris({"secret_env": "FOO"}) == ["env://FOO"]


def test_source_to_uris_secret_uri():
    assert _source_to_uris({"secret": "env://BAR"}) == ["env://BAR"]


def test_source_to_uris_secrets_list():
    uris = ["env://A", "env://B"]
    assert _source_to_uris({"secrets": uris}) == uris


def test_source_to_uris_empty():
    assert _source_to_uris({}) == []
