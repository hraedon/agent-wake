"""Tests for agent-wake install-harness CLI subcommand."""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agent_waked.cli.install_harness import (
    _wire_claude,
    _wire_opencode,
    _run_install,
    _load_manifest,
    _save_manifest,
    _MANIFEST_PATH,
    _CLAUDE_SETTINGS,
    _OPENCODE_CONFIG,
)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Isolate all home-relative paths to tmp_path."""
    claude_settings = tmp_path / ".claude" / "settings.json"
    opencode_config = tmp_path / ".config" / "opencode" / "opencode.json"
    manifest = tmp_path / ".config" / "agent-wake" / "install-manifest.json"

    monkeypatch.setattr("agent_waked.cli.install_harness._CLAUDE_SETTINGS", claude_settings)
    monkeypatch.setattr("agent_waked.cli.install_harness._OPENCODE_CONFIG", opencode_config)
    monkeypatch.setattr("agent_waked.cli.install_harness._MANIFEST_PATH", manifest)

    return type("Paths", (), {
        "claude": claude_settings,
        "opencode": opencode_config,
        "manifest": manifest,
        "tmp": tmp_path,
    })()


# ── claude wiring ──────────────────────────────────────────────────────────────


def test_claude_install_sets_env_vars(isolated_home):
    with patch("agent_waked.cli.install_harness.shutil.which", return_value="/fake/agent-wake-claude"):
        actions = _wire_claude(dry_run=False, uninstall=False)

    assert any(a["kind"] == "merge_json" for a in actions)
    config = json.loads(isolated_home.claude.read_text())
    assert "env" in config
    assert "AGENT_WAKE_CONFIG" in config["env"]


def test_claude_install_idempotent(isolated_home):
    """Re-running install-harness on already-wired config is a no-op."""
    with patch("agent_waked.cli.install_harness.shutil.which", return_value="/fake/agent-wake-claude"):
        _wire_claude(dry_run=False, uninstall=False)
        actions = _wire_claude(dry_run=False, uninstall=False)

    # Second run should be a noop for env vars
    noop_actions = [a for a in actions if a["kind"] == "noop"]
    assert len(noop_actions) >= 1
    assert any("already" in a["detail"] for a in noop_actions)


def test_claude_install_missing_adapter(isolated_home):
    with patch("agent_waked.cli.install_harness.shutil.which", return_value=None):
        actions = _wire_claude(dry_run=False, uninstall=False)

    assert any(a["kind"] == "check_failed" for a in actions)


def test_claude_install_dry_run_does_not_write(isolated_home):
    with patch("agent_waked.cli.install_harness.shutil.which", return_value="/fake/agent-wake-claude"):
        actions = _wire_claude(dry_run=True, uninstall=False)

    assert not isolated_home.claude.exists()
    assert not isolated_home.manifest.exists()


def test_claude_uninstall_removes_env_vars(isolated_home):
    with patch("agent_waked.cli.install_harness.shutil.which", return_value="/fake/agent-wake-claude"):
        _wire_claude(dry_run=False, uninstall=False)
        actions = _wire_claude(dry_run=False, uninstall=True)

    assert any(a["kind"] == "remove_keys" for a in actions)
    if isolated_home.claude.exists():
        config = json.loads(isolated_home.claude.read_text())
        assert "AGENT_WAKE_CONFIG" not in config.get("env", {})


def test_claude_uninstall_idempotent(isolated_home):
    """Uninstalling on a clean profile is a no-op, not an error."""
    actions = _wire_claude(dry_run=False, uninstall=True)
    assert all(a["kind"] == "noop" for a in actions)


def test_claude_install_preserves_existing_config(isolated_home):
    """Existing user config should be preserved, not overwritten."""
    _write_json(isolated_home.claude, {
        "env": {"MY_CUSTOM_VAR": "value"},
        "some_other_setting": True,
    })

    with patch("agent_waked.cli.install_harness.shutil.which", return_value="/fake/agent-wake-claude"):
        _wire_claude(dry_run=False, uninstall=False)

    config = json.loads(isolated_home.claude.read_text())
    assert config["env"]["MY_CUSTOM_VAR"] == "value"
    assert config["some_other_setting"] is True
    assert "AGENT_WAKE_CONFIG" in config["env"]


# ── opencode wiring ────────────────────────────────────────────────────────────


def test_opencode_install_registers_plugin(isolated_home, tmp_path):
    # Create a fake dist/index.js for the plugin path finder
    fake_dist = tmp_path / "dist" / "index.js"
    fake_dist.parent.mkdir(parents=True)
    fake_dist.touch()

    with patch("agent_waked.cli.install_harness._find_opencode_plugin_path", return_value=str(fake_dist)):
        actions = _wire_opencode(dry_run=False, uninstall=False)

    assert any(a["kind"] == "merge_json" for a in actions)
    config = json.loads(isolated_home.opencode.read_text())
    assert "plugins" in config
    assert any(p.get("path") == str(fake_dist) for p in config["plugins"])
    assert "AGENT_WAKE_CONFIG" in config.get("env", {})


def test_opencode_install_idempotent(isolated_home, tmp_path):
    fake_dist = tmp_path / "dist" / "index.js"
    fake_dist.parent.mkdir(parents=True)
    fake_dist.touch()

    with patch("agent_waked.cli.install_harness._find_opencode_plugin_path", return_value=str(fake_dist)):
        _wire_opencode(dry_run=False, uninstall=False)
        actions = _wire_opencode(dry_run=False, uninstall=False)

    noop_actions = [a for a in actions if a["kind"] == "noop"]
    assert len(noop_actions) >= 1
    assert any("already" in a["detail"] for a in noop_actions)


def test_opencode_install_missing_plugin(isolated_home):
    with patch("agent_waked.cli.install_harness._find_opencode_plugin_path", return_value=None):
        actions = _wire_opencode(dry_run=False, uninstall=False)

    assert any(a["kind"] == "check_failed" for a in actions)


def test_opencode_uninstall_removes_plugin(isolated_home, tmp_path):
    fake_dist = tmp_path / "dist" / "index.js"
    fake_dist.parent.mkdir(parents=True)
    fake_dist.touch()

    with patch("agent_waked.cli.install_harness._find_opencode_plugin_path", return_value=str(fake_dist)):
        _wire_opencode(dry_run=False, uninstall=False)
        actions = _wire_opencode(dry_run=False, uninstall=True)

    assert any(a["kind"] == "remove_keys" for a in actions)
    if isolated_home.opencode.exists():
        config = json.loads(isolated_home.opencode.read_text())
        assert "AGENT_WAKE_CONFIG" not in config.get("env", {})
        plugins = config.get("plugins", [])
        assert not any(p.get("path") == str(fake_dist) for p in plugins)


def test_opencode_install_preserves_existing_config(isolated_home, tmp_path):
    _write_json(isolated_home.opencode, {
        "env": {"EXISTING_VAR": "val"},
        "plugins": [{"path": "/other/plugin/index.js"}],
    })

    fake_dist = tmp_path / "dist" / "index.js"
    fake_dist.parent.mkdir(parents=True)
    fake_dist.touch()

    with patch("agent_waked.cli.install_harness._find_opencode_plugin_path", return_value=str(fake_dist)):
        _wire_opencode(dry_run=False, uninstall=False)

    config = json.loads(isolated_home.opencode.read_text())
    assert config["env"]["EXISTING_VAR"] == "val"
    assert any(p.get("path") == "/other/plugin/index.js" for p in config["plugins"])
    assert any(p.get("path") == str(fake_dist) for p in config["plugins"])


# ── _run_install (dispatch) ────────────────────────────────────────────────────


def test_run_install_all_targets_both_wired(isolated_home, tmp_path):
    fake_dist = tmp_path / "dist" / "index.js"
    fake_dist.parent.mkdir(parents=True)
    fake_dist.touch()

    with patch("agent_waked.cli.install_harness.shutil.which", return_value="/fake/agent-wake-claude"), \
         patch("agent_waked.cli.install_harness._find_opencode_plugin_path", return_value=str(fake_dist)):
        result = _run_install("all", dry_run=False, uninstall=False, user=None)

    assert result["tool"] == "agent-wake"
    assert result["harness"] == "all"
    assert not result["no_op"]


def test_run_install_dry_run_returns_actions(isolated_home):
    with patch("agent_waked.cli.install_harness.shutil.which", return_value="/fake/agent-wake-claude"):
        result = _run_install("claude", dry_run=True, uninstall=False, user=None)

    assert "actions" in result
    assert all(a.get("kind") for a in result["actions"])
    # dry-run should not have written manifest
    assert not isolated_home.manifest.exists()


def test_run_install_unknown_harness(isolated_home):
    result = _run_install("gemini", dry_run=False, uninstall=False, user=None)
    assert any(a["kind"] == "error" for a in result["actions"])
