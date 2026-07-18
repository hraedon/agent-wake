"""Tests for agent-wake install-harness CLI subcommand."""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agent_waked.cli.install_harness import (
    _wire_claude,
    _wire_opencode,
    _wire_hermes,
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
    hermes_env = tmp_path / ".hermes" / ".env"
    hermes_plugin_dir = tmp_path / ".hermes" / "plugins" / "signaling" / "wake"
    manifest = tmp_path / ".config" / "agent-wake" / "install-manifest.json"

    monkeypatch.setattr("agent_waked.cli.install_harness._CLAUDE_SETTINGS", claude_settings)
    monkeypatch.setattr("agent_waked.cli.install_harness._OPENCODE_CONFIG", opencode_config)
    monkeypatch.setattr("agent_waked.cli.install_harness._HERMES_ENV", hermes_env)
    monkeypatch.setattr("agent_waked.cli.install_harness._HERMES_PLUGIN_DIR", hermes_plugin_dir)
    monkeypatch.setattr("agent_waked.cli.install_harness._MANIFEST_PATH", manifest)

    return type("Paths", (), {
        "claude": claude_settings,
        "opencode": opencode_config,
        "hermes_env": hermes_env,
        "hermes_plugin": hermes_plugin_dir,
        "manifest": manifest,
        "tmp": tmp_path,
    })()


# ── claude wiring ──────────────────────────────────────────────────────────────


def test_claude_install_sets_env_vars(isolated_home):
    with patch("agent_waked.cli.install_harness.shutil.which", return_value="/fake/agent-wake-claude"):
        actions = _wire_claude(dry_run=False, uninstall=False, user=None)

    assert any(a["kind"] == "merge_json" for a in actions)
    config = json.loads(isolated_home.claude.read_text())
    assert "env" in config
    assert "AGENT_WAKE_CONFIG" in config["env"]


def test_claude_install_idempotent(isolated_home):
    """Re-running install-harness on already-wired config is a no-op."""
    with patch("agent_waked.cli.install_harness.shutil.which", return_value="/fake/agent-wake-claude"):
        _wire_claude(dry_run=False, uninstall=False, user=None)
        actions = _wire_claude(dry_run=False, uninstall=False, user=None)

    # Second run should be a noop for env vars
    noop_actions = [a for a in actions if a["kind"] == "noop"]
    assert len(noop_actions) >= 1
    assert any("already" in a["detail"] for a in noop_actions)


def test_claude_install_missing_adapter(isolated_home):
    with patch("agent_waked.cli.install_harness.shutil.which", return_value=None):
        actions = _wire_claude(dry_run=False, uninstall=False, user=None)

    assert any(a["kind"] == "check_failed" for a in actions)


def test_claude_install_dry_run_does_not_write(isolated_home):
    with patch("agent_waked.cli.install_harness.shutil.which", return_value="/fake/agent-wake-claude"):
        actions = _wire_claude(dry_run=True, uninstall=False, user=None)

    assert not isolated_home.claude.exists()
    assert not isolated_home.manifest.exists()


def test_claude_uninstall_removes_env_vars(isolated_home):
    with patch("agent_waked.cli.install_harness.shutil.which", return_value="/fake/agent-wake-claude"):
        _wire_claude(dry_run=False, uninstall=False, user=None)
        actions = _wire_claude(dry_run=False, uninstall=True, user=None)

    assert any(a["kind"] == "remove_keys" for a in actions)
    if isolated_home.claude.exists():
        config = json.loads(isolated_home.claude.read_text())
        assert "AGENT_WAKE_CONFIG" not in config.get("env", {})


def test_claude_uninstall_idempotent(isolated_home):
    """Uninstalling on a clean profile is a no-op, not an error."""
    actions = _wire_claude(dry_run=False, uninstall=True, user=None)
    assert all(a["kind"] == "noop" for a in actions)


def test_claude_install_preserves_existing_config(isolated_home):
    """Existing user config should be preserved, not overwritten."""
    _write_json(isolated_home.claude, {
        "env": {"MY_CUSTOM_VAR": "value"},
        "some_other_setting": True,
    })

    with patch("agent_waked.cli.install_harness.shutil.which", return_value="/fake/agent-wake-claude"):
        _wire_claude(dry_run=False, uninstall=False, user=None)

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
        actions = _wire_opencode(dry_run=False, uninstall=False, user=None)

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
        _wire_opencode(dry_run=False, uninstall=False, user=None)
        actions = _wire_opencode(dry_run=False, uninstall=False, user=None)

    noop_actions = [a for a in actions if a["kind"] == "noop"]
    assert len(noop_actions) >= 1
    assert any("already" in a["detail"] for a in noop_actions)


def test_opencode_install_missing_plugin(isolated_home):
    with patch("agent_waked.cli.install_harness._find_opencode_plugin_path", return_value=None):
        actions = _wire_opencode(dry_run=False, uninstall=False, user=None)

    assert any(a["kind"] == "check_failed" for a in actions)


def test_opencode_plugin_env_override(isolated_home, tmp_path, monkeypatch):
    """AGENT_WAKE_OPENCODE_PLUGIN points the installer at an arbitrary plugin path."""
    fake_dist = tmp_path / "custom" / "index.js"
    fake_dist.parent.mkdir(parents=True)
    fake_dist.touch()
    monkeypatch.setenv("AGENT_WAKE_OPENCODE_PLUGIN", str(fake_dist))

    from agent_waked.cli.install_harness import _find_opencode_plugin_path
    assert _find_opencode_plugin_path() == str(fake_dist)


def test_opencode_plugin_env_override_missing_returns_none(tmp_path, monkeypatch):
    """A non-existent env override path returns None (not an error)."""
    monkeypatch.setenv("AGENT_WAKE_OPENCODE_PLUGIN", str(tmp_path / "nope.js"))

    from agent_waked.cli.install_harness import _find_opencode_plugin_path
    assert _find_opencode_plugin_path() is None


def test_opencode_plugin_env_override_directory_rejected(tmp_path, monkeypatch):
    """A directory or non-.js file is rejected (no arbitrary path steering)."""
    fake_dir = tmp_path / "custom"
    fake_dir.mkdir()
    monkeypatch.setenv("AGENT_WAKE_OPENCODE_PLUGIN", str(fake_dir))

    from agent_waked.cli.install_harness import _find_opencode_plugin_path
    assert _find_opencode_plugin_path() is None


def test_opencode_plugin_env_override_non_js_rejected(tmp_path, monkeypatch):
    """A non-.js file is rejected even if it exists."""
    fake_txt = tmp_path / "index.txt"
    fake_txt.touch()
    monkeypatch.setenv("AGENT_WAKE_OPENCODE_PLUGIN", str(fake_txt))

    from agent_waked.cli.install_harness import _find_opencode_plugin_path
    assert _find_opencode_plugin_path() is None


def test_opencode_uninstall_removes_plugin(isolated_home, tmp_path):
    fake_dist = tmp_path / "dist" / "index.js"
    fake_dist.parent.mkdir(parents=True)
    fake_dist.touch()

    with patch("agent_waked.cli.install_harness._find_opencode_plugin_path", return_value=str(fake_dist)):
        _wire_opencode(dry_run=False, uninstall=False, user=None)
        actions = _wire_opencode(dry_run=False, uninstall=True, user=None)

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
        _wire_opencode(dry_run=False, uninstall=False, user=None)

    config = json.loads(isolated_home.opencode.read_text())
    assert config["env"]["EXISTING_VAR"] == "val"
    assert any(p.get("path") == "/other/plugin/index.js" for p in config["plugins"])
    assert any(p.get("path") == str(fake_dist) for p in config["plugins"])


def test_opencode_install_with_user_writes_principal_id(isolated_home, tmp_path):
    """--user should write AGENT_WAKE_PRINCIPAL_ID to the opencode env block."""
    fake_dist = tmp_path / "dist" / "index.js"
    fake_dist.parent.mkdir(parents=True)
    fake_dist.touch()

    with patch("agent_waked.cli.install_harness._find_opencode_plugin_path", return_value=str(fake_dist)):
        _wire_opencode(dry_run=False, uninstall=False, user="alice@example.com")

    config = json.loads(isolated_home.opencode.read_text())
    assert config["env"]["AGENT_WAKE_PRINCIPAL_ID"] == "alice@example.com"


# ── hermes wiring ──────────────────────────────────────────────────────────────


def _make_fake_plugin_source(tmp_path):
    """Create a fake adapters/hermes/ source dir for testing."""
    src = tmp_path / "fake_hermes_source"
    src.mkdir()
    (src / "plugin.yaml").write_text('name: wake\nversion: "0.1.0"\n', encoding="utf-8")
    (src / "__init__.py").write_text("# fake plugin\n", encoding="utf-8")
    return src


def test_hermes_install_sets_env_vars(isolated_home):
    src = _make_fake_plugin_source(isolated_home.tmp)
    with patch("agent_waked.cli.install_harness._find_hermes_plugin_source", return_value=str(src)):
        actions = _wire_hermes(dry_run=False, uninstall=False, user=None)

    assert any(a["kind"] in ("merge_env", "install_hermes") for a in actions)
    env_content = isolated_home.hermes_env.read_text()
    assert "AGENT_WAKE_CONFIG" in env_content
    assert "# BEGIN agent-wake-harness-managed" in env_content
    assert "# END agent-wake-harness-managed" in env_content


def test_hermes_install_copies_plugin(isolated_home):
    src = _make_fake_plugin_source(isolated_home.tmp)
    with patch("agent_waked.cli.install_harness._find_hermes_plugin_source", return_value=str(src)):
        _wire_hermes(dry_run=False, uninstall=False, user=None)

    assert (isolated_home.hermes_plugin / "plugin.yaml").exists()
    assert (isolated_home.hermes_plugin / "__init__.py").exists()


def test_hermes_install_idempotent(isolated_home):
    src = _make_fake_plugin_source(isolated_home.tmp)
    with patch("agent_waked.cli.install_harness._find_hermes_plugin_source", return_value=str(src)):
        _wire_hermes(dry_run=False, uninstall=False, user=None)
        actions = _wire_hermes(dry_run=False, uninstall=False, user=None)

    noop_actions = [a for a in actions if a["kind"] == "noop"]
    assert len(noop_actions) >= 1
    assert any("already" in a["detail"] for a in noop_actions)


def test_hermes_install_missing_plugin_source(isolated_home):
    with patch("agent_waked.cli.install_harness._find_hermes_plugin_source", return_value=None):
        actions = _wire_hermes(dry_run=False, uninstall=False, user=None)

    assert any(a["kind"] == "check_failed" for a in actions)


def test_hermes_install_dry_run_does_not_write(isolated_home):
    src = _make_fake_plugin_source(isolated_home.tmp)
    with patch("agent_waked.cli.install_harness._find_hermes_plugin_source", return_value=str(src)):
        _wire_hermes(dry_run=True, uninstall=False, user=None)

    assert not isolated_home.hermes_env.exists()
    assert not isolated_home.hermes_plugin.exists()
    assert not isolated_home.manifest.exists()


def test_hermes_uninstall_removes_env_and_plugin(isolated_home):
    src = _make_fake_plugin_source(isolated_home.tmp)
    with patch("agent_waked.cli.install_harness._find_hermes_plugin_source", return_value=str(src)):
        _wire_hermes(dry_run=False, uninstall=False, user=None)
        actions = _wire_hermes(dry_run=False, uninstall=True, user=None)

    assert any(a["kind"] == "remove_keys" for a in actions)
    if isolated_home.hermes_env.exists():
        content = isolated_home.hermes_env.read_text()
        assert "AGENT_WAKE_CONFIG" not in content
        assert "# BEGIN agent-wake-harness-managed" not in content
    assert not isolated_home.hermes_plugin.exists()


def test_hermes_uninstall_idempotent(isolated_home):
    actions = _wire_hermes(dry_run=False, uninstall=True, user=None)
    assert all(a["kind"] == "noop" for a in actions)


def test_hermes_uninstall_dry_run_does_not_delete_plugin(isolated_home):
    """--dry-run --uninstall must report intent but not touch the filesystem."""
    src = _make_fake_plugin_source(isolated_home.tmp)
    with patch("agent_waked.cli.install_harness._find_hermes_plugin_source", return_value=str(src)):
        _wire_hermes(dry_run=False, uninstall=False, user=None)
    assert isolated_home.hermes_plugin.exists()
    assert isolated_home.hermes_env.exists()

    with patch("agent_waked.cli.install_harness._find_hermes_plugin_source", return_value=str(src)):
        actions = _wire_hermes(dry_run=True, uninstall=True, user=None)

    assert isolated_home.hermes_plugin.exists(), "dry-run must not delete plugin dir"
    assert isolated_home.hermes_env.exists(), "dry-run must not delete env file"
    assert "# BEGIN agent-wake-harness-managed" in isolated_home.hermes_env.read_text()
    assert any(a["kind"] == "remove_keys" for a in actions), "dry-run should still report intent"


def test_hermes_install_preserves_existing_env(isolated_home):
    """Existing user .env entries should be preserved, not overwritten."""
    isolated_home.hermes_env.parent.mkdir(parents=True, exist_ok=True)
    isolated_home.hermes_env.write_text(
        "MY_CUSTOM_VAR=value\nOTHER_VAR=stuff\n", encoding="utf-8"
    )

    src = _make_fake_plugin_source(isolated_home.tmp)
    with patch("agent_waked.cli.install_harness._find_hermes_plugin_source", return_value=str(src)):
        _wire_hermes(dry_run=False, uninstall=False, user=None)

    content = isolated_home.hermes_env.read_text()
    assert "MY_CUSTOM_VAR=value" in content
    assert "OTHER_VAR=stuff" in content
    assert "AGENT_WAKE_CONFIG" in content


def test_hermes_install_no_clobber_existing_value(isolated_home):
    """Existing AGENT_WAKE_CONFIG with a different value should be preserved."""
    isolated_home.hermes_env.parent.mkdir(parents=True, exist_ok=True)
    isolated_home.hermes_env.write_text(
        "AGENT_WAKE_CONFIG=/custom/path/config.json\n", encoding="utf-8"
    )

    src = _make_fake_plugin_source(isolated_home.tmp)
    with patch("agent_waked.cli.install_harness._find_hermes_plugin_source", return_value=str(src)):
        actions = _wire_hermes(dry_run=False, uninstall=False, user=None)

    content = isolated_home.hermes_env.read_text()
    assert "/custom/path/config.json" in content
    assert any(a["kind"] == "warn" for a in actions)


def test_hermes_install_with_user_writes_principal_id(isolated_home):
    src = _make_fake_plugin_source(isolated_home.tmp)
    with patch("agent_waked.cli.install_harness._find_hermes_plugin_source", return_value=str(src)):
        _wire_hermes(dry_run=False, uninstall=False, user="alice@example.com")

    content = isolated_home.hermes_env.read_text()
    assert "AGENT_WAKE_PRINCIPAL_ID=alice@example.com" in content


# ── _run_install (dispatch) ────────────────────────────────────────────────────


def test_run_install_all_targets_all_wired(isolated_home, tmp_path):
    fake_dist = tmp_path / "dist" / "index.js"
    fake_dist.parent.mkdir(parents=True)
    fake_dist.touch()

    hermes_src = _make_fake_plugin_source(tmp_path)

    with patch("agent_waked.cli.install_harness.shutil.which", return_value="/fake/agent-wake-claude"), \
         patch("agent_waked.cli.install_harness._find_opencode_plugin_path", return_value=str(fake_dist)), \
         patch("agent_waked.cli.install_harness._find_hermes_plugin_source", return_value=str(hermes_src)):
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


def test_run_install_codex_is_honestly_unsupported_and_non_noop(isolated_home):
    result = _run_install("codex", dry_run=False, uninstall=False, user=None)
    assert result["status"] == "unsupported"
    assert result["no_op"] is False
    assert result["actions"] == [
        {
            "kind": "unsupported",
            "path": "",
            "detail": result["actions"][0]["detail"],
        }
    ]
    assert not isolated_home.manifest.exists()


# ── no-clobber / --user / corrupted JSON ───────────────────────────────────────


def test_claude_install_no_clobber_existing_value(isolated_home):
    """Existing AGENT_WAKE_CONFIG with a different value should be preserved."""
    _write_json(isolated_home.claude, {
        "env": {"AGENT_WAKE_CONFIG": "/custom/path/config.json"},
    })
    with patch("agent_waked.cli.install_harness.shutil.which", return_value="/fake/agent-wake-claude"):
        actions = _wire_claude(dry_run=False, uninstall=False, user=None)

    config = json.loads(isolated_home.claude.read_text())
    # Existing value should be preserved
    assert config["env"]["AGENT_WAKE_CONFIG"] == "/custom/path/config.json"
    # Should have a warn action
    assert any(a["kind"] == "warn" for a in actions)


def test_claude_install_with_user_writes_principal_id(isolated_home):
    """--user should write AGENT_WAKE_PRINCIPAL_ID to the env block."""
    with patch("agent_waked.cli.install_harness.shutil.which", return_value="/fake/agent-wake-claude"):
        _wire_claude(dry_run=False, uninstall=False, user="alice@example.com")

    config = json.loads(isolated_home.claude.read_text())
    assert config["env"]["AGENT_WAKE_PRINCIPAL_ID"] == "alice@example.com"


def test_claude_install_corrupted_json_errors(isolated_home):
    """Corrupted settings.json should produce an error, not silent overwrite."""
    isolated_home.claude.parent.mkdir(parents=True, exist_ok=True)
    isolated_home.claude.write_text("{invalid json", encoding="utf-8")

    with patch("agent_waked.cli.install_harness.shutil.which", return_value="/fake/agent-wake-claude"):
        actions = _wire_claude(dry_run=False, uninstall=False, user=None)

    assert any(a["kind"] == "error" for a in actions)
    # Original corrupted content should be preserved, not overwritten
    assert isolated_home.claude.read_text() == "{invalid json"


def test_claude_uninstall_missing_env_block_no_crash(isolated_home):
    """Uninstall should not crash if env block was manually removed."""
    with patch("agent_waked.cli.install_harness.shutil.which", return_value="/fake/agent-wake-claude"):
        _wire_claude(dry_run=False, uninstall=False, user=None)
        # Manually remove env block
        config = json.loads(isolated_home.claude.read_text())
        del config["env"]
        _write_json(isolated_home.claude, config)
        # Uninstall should not crash
        actions = _wire_claude(dry_run=False, uninstall=True, user=None)

    assert all(a["kind"] != "error" for a in actions)


# ── dual-harness validation (Plan 004 harness note) ────────────────────────────


def test_dual_harness_config_vocabulary_consistent(isolated_home, tmp_path, monkeypatch):
    """Both claude and opencode receive the same AGENT_WAKE_CONFIG value.

    Plan 004's harness note requires a dual-harness validation confirming the
    cohesion changes (config vocabulary, secret resolution) don't regress an
    existing opencode setup. This test wires both harnesses simultaneously and
    verifies the config vocabulary is consistent: both get AGENT_WAKE_CONFIG
    pointing at the same path, and --user propagates AGENT_WAKE_PRINCIPAL_ID
    to both env blocks identically.
    """
    fake_dist = tmp_path / "dist" / "index.js"
    fake_dist.parent.mkdir(parents=True)
    fake_dist.touch()

    custom_config = str(tmp_path / "custom-wake.json")
    monkeypatch.setenv("AGENT_WAKE_CONFIG", custom_config)

    with patch("agent_waked.cli.install_harness.shutil.which", return_value="/fake/agent-wake-claude"), \
         patch("agent_waked.cli.install_harness._find_opencode_plugin_path", return_value=str(fake_dist)):
        _wire_claude(dry_run=False, uninstall=False, user="bob@example.com")
        _wire_opencode(dry_run=False, uninstall=False, user="bob@example.com")

    claude_cfg = json.loads(isolated_home.claude.read_text())
    opencode_cfg = json.loads(isolated_home.opencode.read_text())

    # Both harnesses get the same AGENT_WAKE_CONFIG value
    assert claude_cfg["env"]["AGENT_WAKE_CONFIG"] == custom_config
    assert opencode_cfg["env"]["AGENT_WAKE_CONFIG"] == custom_config
    assert claude_cfg["env"]["AGENT_WAKE_CONFIG"] == opencode_cfg["env"]["AGENT_WAKE_CONFIG"]

    # --user propagates AGENT_WAKE_PRINCIPAL_ID to both identically
    assert claude_cfg["env"]["AGENT_WAKE_PRINCIPAL_ID"] == "bob@example.com"
    assert opencode_cfg["env"]["AGENT_WAKE_PRINCIPAL_ID"] == "bob@example.com"

    # opencode also has the plugin registered
    assert any(p.get("path") == str(fake_dist) for p in opencode_cfg.get("plugins", []))


def test_dual_harness_uninstall_cleans_both(isolated_home, tmp_path):
    """Uninstalling both harnesses leaves no wake env vars behind."""
    fake_dist = tmp_path / "dist" / "index.js"
    fake_dist.parent.mkdir(parents=True)
    fake_dist.touch()

    with patch("agent_waked.cli.install_harness.shutil.which", return_value="/fake/agent-wake-claude"), \
         patch("agent_waked.cli.install_harness._find_opencode_plugin_path", return_value=str(fake_dist)):
        _wire_claude(dry_run=False, uninstall=False, user=None)
        _wire_opencode(dry_run=False, uninstall=False, user=None)
        _wire_claude(dry_run=False, uninstall=True, user=None)
        _wire_opencode(dry_run=False, uninstall=True, user=None)

    if isolated_home.claude.exists():
        claude_cfg = json.loads(isolated_home.claude.read_text())
        assert "AGENT_WAKE_CONFIG" not in claude_cfg.get("env", {})
    if isolated_home.opencode.exists():
        opencode_cfg = json.loads(isolated_home.opencode.read_text())
        assert "AGENT_WAKE_CONFIG" not in opencode_cfg.get("env", {})
        assert not any(
            p.get("path") == str(fake_dist)
            for p in opencode_cfg.get("plugins", [])
        )
