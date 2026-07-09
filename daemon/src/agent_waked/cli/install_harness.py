"""``agent-wake install-harness`` — wire wake adapters into harness configs.

Implements the shared install-harness contract (agent-suite/docs/install-harness-contract.md).

    agent-wake install-harness <harness> [--dry-run] [--uninstall] [--user <principal_id>]

For agent-wake, the scope is the wake receiver adapter (daemon subscription):

- **claude**: sets ``AGENT_WAKE_CONFIG`` in ``~/.claude/settings.json`` env block
  and records the channel launch flag. The adapter itself (``agent-wake-claude``)
  is a pip package; install-harness verifies it's on PATH and documents the
  ``--dangerously-load-development-channels server:agent-wake-claude`` flag.

- **opencode**: registers ``dist/index.js`` as a plugin in
  ``~/.config/opencode/opencode.json`` and sets ``AGENT_WAKE_CONFIG`` in the
  environment block.

- **hermes**: writes ``AGENT_WAKE_CONFIG`` to ``~/.hermes/.env`` (KEY=VALUE format)
  between sentinel comments and copies a Python plugin to
  ``~/.hermes/plugins/signaling/wake/``. The plugin source lives in the repo at
  ``adapters/hermes/`` and is copied by install-harness.

Idempotency: re-running on an already-wired profile is a no-op (exit 0).
``--uninstall`` removes only the entries this tool created, tracked by a
sidecar manifest at ``~/.config/agent-wake/install-manifest.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from ..config import DEFAULT_CONFIG_PATH

# ── paths ──────────────────────────────────────────────────────────────────────

_CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
_OPENCODE_CONFIG = Path.home() / ".config" / "opencode" / "opencode.json"
_HERMES_ENV = Path.home() / ".hermes" / ".env"
_HERMES_PLUGIN_DIR = Path.home() / ".hermes" / "plugins" / "signaling" / "wake"
_HERMES_MANIFEST = Path.home() / ".hermes" / ".agent-wake-harness.json"
_MANIFEST_PATH = Path.home() / ".config" / "agent-wake" / "install-manifest.json"


def _wake_env_vars() -> dict[str, str]:
    """Build env vars lazily so AGENT_WAKE_CONFIG is resolved at call time."""
    config_path = os.environ.get("AGENT_WAKE_CONFIG", str(DEFAULT_CONFIG_PATH))
    return {"AGENT_WAKE_CONFIG": config_path}


# ── manifest ───────────────────────────────────────────────────────────────────


def _load_manifest() -> dict[str, Any]:
    if not _MANIFEST_PATH.exists():
        return {"installed": {}}
    try:
        data: dict[str, Any] = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
        return data
    except (json.JSONDecodeError, OSError):
        return {"installed": {}}


def _save_manifest(manifest: dict[str, Any]) -> None:
    _atomic_write_json(_MANIFEST_PATH, manifest)


# ── JSON helpers ───────────────────────────────────────────────────────────────


class ConfigParseError(Exception):
    """Raised when a harness config file exists but is not valid JSON."""


def _load_json(path: Path) -> dict[str, Any]:
    """Load JSON from *path*. Returns {} for missing files.

    Raises ConfigParseError if the file exists but is not valid JSON
    (prevents silent data loss from overwriting corrupted configs).
    """
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigParseError(f"Cannot read {path}: {e}") from e
    try:
        data: dict[str, Any] = json.loads(text)
        return data
    except json.JSONDecodeError as e:
        raise ConfigParseError(
            f"{path} is not valid JSON (line {e.lineno}): {e.msg}. "
            "Fix the file before running install-harness."
        ) from e


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON to *path* (temp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        suffix=".tmp",
        delete=False,
    ) as tmp:
        json.dump(data, tmp, indent=2)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def _merge_env_block(config: dict[str, Any], env_vars: dict[str, str]) -> tuple[list[str], list[str]]:
    """Merge env vars into the config's 'env' block.

    Returns ``(changed, skipped)`` where:
    - ``changed``: keys that were created or updated
    - ``skipped``: keys that had a different existing value (no-clobber)

    Per contract rule 3: if a key already has a different value, keep the
    existing value and warn (never silently overwrite user config).
    """
    if not isinstance(config.get("env"), dict):
        if "env" not in config:
            config["env"] = {}
        else:
            raise TypeError(f"config 'env' is {type(config['env']).__name__}, expected dict")
    env = config["env"]
    changed: list[str] = []
    skipped: list[str] = []
    for key, value in env_vars.items():
        if key in env:
            if env[key] != value:
                skipped.append(f"env.{key} (existing: {env[key]})")
                continue
        else:
            env[key] = value
            changed.append(f"env.{key}")
    return changed, skipped


# ── claude wiring ──────────────────────────────────────────────────────────────


def _wire_claude(dry_run: bool, uninstall: bool, user: str | None) -> list[dict[str, Any]]:
    """Wire or unwire claude. Returns list of action dicts."""
    actions: list[dict[str, Any]] = []
    manifest = _load_manifest()

    if uninstall:
        return _unwire_claude(dry_run, manifest, actions)

    adapter_path = shutil.which("agent-wake-claude")
    if not adapter_path:
        actions.append({
            "kind": "check_failed",
            "path": "agent-wake-claude",
            "detail": "agent-wake-claude not on PATH; run: pip install -e adapters/claude",
        })
        return actions

    try:
        config = _load_json(_CLAUDE_SETTINGS)
    except ConfigParseError as e:
        actions.append({"kind": "error", "path": str(_CLAUDE_SETTINGS), "detail": str(e)})
        return actions

    env_vars = _wake_env_vars()
    if user:
        env_vars["AGENT_WAKE_PRINCIPAL_ID"] = user

    changed, skipped = _merge_env_block(config, env_vars)

    if skipped:
        actions.append({
            "kind": "warn",
            "path": str(_CLAUDE_SETTINGS),
            "keys": skipped,
            "detail": f"skipped (existing values differ): {', '.join(skipped)}",
        })

    if changed:
        actions.append({
            "kind": "merge_json",
            "path": str(_CLAUDE_SETTINGS),
            "keys": changed,
            "detail": "set wake env vars in claude settings",
        })
    elif not skipped:
        actions.append({
            "kind": "noop",
            "path": str(_CLAUDE_SETTINGS),
            "keys": [],
            "detail": "env vars already set",
        })

    actions.append({
        "kind": "info",
        "path": "",
        "keys": [],
        "detail": "launch claude with: --dangerously-load-development-channels server:agent-wake-claude",
    })

    if not dry_run and changed:
        _atomic_write_json(_CLAUDE_SETTINGS, config)
        manifest.setdefault("installed", {})["claude"] = {
            "settings_path": str(_CLAUDE_SETTINGS),
            "env_keys": [k.split(".", 1)[1] for k in changed if k.startswith("env.")],
        }
        _save_manifest(manifest)

    return actions


def _unwire_claude(
    dry_run: bool, manifest: dict[str, Any], actions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Remove agent-wake entries from claude settings."""
    entry = manifest.get("installed", {}).get("claude")
    if not entry:
        actions.append({"kind": "noop", "path": str(_CLAUDE_SETTINGS), "keys": [], "detail": "claude not wired by install-harness"})
        return actions

    try:
        config = _load_json(_CLAUDE_SETTINGS)
    except ConfigParseError as e:
        actions.append({"kind": "error", "path": str(_CLAUDE_SETTINGS), "detail": str(e)})
        return actions

    env = config.get("env", {})
    removed: list[str] = []
    for key in entry.get("env_keys", []):
        if key in env:
            del env[key]
            removed.append(f"env.{key}")
    if not env and "env" in config:
        del config["env"]

    if removed:
        actions.append({
            "kind": "remove_keys",
            "path": str(_CLAUDE_SETTINGS),
            "keys": removed,
            "detail": "removed wake env vars from claude settings",
        })
    else:
        actions.append({"kind": "noop", "path": str(_CLAUDE_SETTINGS), "keys": [], "detail": "nothing to remove"})

    if not dry_run and removed:
        _atomic_write_json(_CLAUDE_SETTINGS, config)
    if not dry_run:
        manifest.get("installed", {}).pop("claude", None)
        _save_manifest(manifest)

    return actions


# ── opencode wiring ────────────────────────────────────────────────────────────


def _find_opencode_plugin_path() -> str | None:
    """Find the built opencode plugin dist/index.js.

    Resolution order:
    1. ``$AGENT_WAKE_OPENCODE_PLUGIN`` (explicit operator override; useful for
       pip-installed daemon installs that have no source tree on disk). Must
       point at an existing ``.js`` file — directories and other file types
       are rejected so a stale or hostile value can't steer the installer at
       an arbitrary location.
    2. The daemon's sibling source-tree path
       (``<repo>/adapters/opencode/dist/index.js``).
    3. The shared install location
       (``~/.local/share/agent-wake/opencode/dist/index.js``).
    """
    env_override = os.environ.get("AGENT_WAKE_OPENCODE_PLUGIN")
    if env_override:
        p = Path(env_override).expanduser()
        if p.is_file() and p.suffix == ".js":
            return str(p)
        return None
    # parents[4] from daemon/src/agent_waked/cli/install_harness.py → repo root
    candidates = [
        Path(__file__).resolve().parents[4] / "adapters" / "opencode" / "dist" / "index.js",
        Path.home() / ".local" / "share" / "agent-wake" / "opencode" / "dist" / "index.js",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _wire_opencode(dry_run: bool, uninstall: bool, user: str | None) -> list[dict[str, Any]]:
    """Wire or unwire opencode. Returns list of action dicts."""
    actions: list[dict[str, Any]] = []
    manifest = _load_manifest()

    if uninstall:
        return _unwire_opencode(dry_run, manifest, actions)

    plugin_path = _find_opencode_plugin_path()
    if not plugin_path:
        actions.append({
            "kind": "check_failed",
            "path": "adapters/opencode/dist/index.js",
            "detail": "opencode plugin not built; run: cd adapters/opencode && bun install && bun run build",
        })
        return actions

    try:
        config = _load_json(_OPENCODE_CONFIG)
    except ConfigParseError as e:
        actions.append({"kind": "error", "path": str(_OPENCODE_CONFIG), "detail": str(e)})
        return actions

    env_vars = _wake_env_vars()
    if user:
        env_vars["AGENT_WAKE_PRINCIPAL_ID"] = user

    changed, skipped = _merge_env_block(config, env_vars)

    if skipped:
        actions.append({
            "kind": "warn",
            "path": str(_OPENCODE_CONFIG),
            "keys": skipped,
            "detail": f"skipped (existing values differ): {', '.join(skipped)}",
        })

    # Register plugin if not already present
    if not isinstance(config.get("plugins"), list):
        if "plugins" not in config:
            config["plugins"] = []
        else:
            raise TypeError(f"config 'plugins' is {type(config['plugins']).__name__}, expected list")
    plugins = config["plugins"]
    already_present = any(
        isinstance(p, dict) and p.get("path") == plugin_path
        for p in plugins
    )
    plugin_added = False
    if not already_present:
        plugins.append({"path": plugin_path, "type": "module"})
        changed.append(f"plugins[{plugin_path}]")
        plugin_added = True

    if changed:
        actions.append({
            "kind": "merge_json",
            "path": str(_OPENCODE_CONFIG),
            "keys": changed,
            "detail": "set wake env vars and register opencode plugin",
        })
    elif not skipped:
        actions.append({
            "kind": "noop",
            "path": str(_OPENCODE_CONFIG),
            "keys": [],
            "detail": "env vars and plugin already configured",
        })

    if not dry_run and changed:
        _atomic_write_json(_OPENCODE_CONFIG, config)
        manifest_entry: dict[str, Any] = {
            "config_path": str(_OPENCODE_CONFIG),
            "env_keys": [k.split(".", 1)[1] for k in changed if k.startswith("env.")],
        }
        if plugin_added:
            manifest_entry["plugin_path"] = plugin_path
        manifest.setdefault("installed", {})["opencode"] = manifest_entry
        _save_manifest(manifest)

    return actions


def _unwire_opencode(
    dry_run: bool, manifest: dict[str, Any], actions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Remove agent-wake entries from opencode config."""
    entry = manifest.get("installed", {}).get("opencode")
    if not entry:
        actions.append({"kind": "noop", "path": str(_OPENCODE_CONFIG), "keys": [], "detail": "opencode not wired by install-harness"})
        return actions

    try:
        config = _load_json(_OPENCODE_CONFIG)
    except ConfigParseError as e:
        actions.append({"kind": "error", "path": str(_OPENCODE_CONFIG), "detail": str(e)})
        return actions

    removed: list[str] = []

    # Remove env vars
    env = config.get("env", {})
    for key in entry.get("env_keys", []):
        if key in env:
            del env[key]
            removed.append(f"env.{key}")
    if not env and "env" in config:
        del config["env"]

    # Remove plugin
    plugin_path = entry.get("plugin_path")
    if plugin_path and "plugins" in config:
        plugins = config.get("plugins", [])
        if isinstance(plugins, list):
            new_plugins = [p for p in plugins if not (isinstance(p, dict) and p.get("path") == plugin_path)]
            if len(new_plugins) < len(plugins):
                config["plugins"] = new_plugins
                removed.append(f"plugins[{plugin_path}]")
            if not config["plugins"]:
                del config["plugins"]

    if removed:
        actions.append({
            "kind": "remove_keys",
            "path": str(_OPENCODE_CONFIG),
            "keys": removed,
            "detail": "removed wake env vars and plugin from opencode config",
        })
    else:
        actions.append({"kind": "noop", "path": str(_OPENCODE_CONFIG), "keys": [], "detail": "nothing to remove"})

    if not dry_run and removed:
        _atomic_write_json(_OPENCODE_CONFIG, config)
    if not dry_run:
        manifest.get("installed", {}).pop("opencode", None)
        _save_manifest(manifest)

    return actions


# ── .env file helpers ─────────────────────────────────────────────────────────

_SENTINEL_BEGIN = "# BEGIN agent-wake-harness-managed"
_SENTINEL_END = "# END agent-wake-harness-managed"


def _parse_env_file(path: Path) -> list[str]:
    """Read .env file as a list of lines (no trailing newline splitting)."""
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _write_env_file(path: Path, lines: list[str]) -> None:
    """Atomically write .env file (lines joined by newline)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines)
    if content and not content.endswith("\n"):
        content += "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def _parse_existing_env_keys(lines: list[str], exclude_block: bool = False) -> dict[str, str]:
    """Parse KEY=VALUE from .env lines, optionally excluding the managed sentinel block.

    Returns dict of key → value.
    """
    result: dict[str, str] = {}
    in_block = False
    for line in lines:
        stripped = line.strip()
        if stripped == _SENTINEL_BEGIN:
            in_block = True
            continue
        if stripped == _SENTINEL_END:
            in_block = False
            continue
        if exclude_block and in_block:
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            key, _, value = stripped.partition("=")
            result[key.strip()] = value.strip()
    return result


def _merge_env_file_block(
    lines: list[str], env_vars: dict[str, str]
) -> tuple[list[str], list[str], list[str]]:
    """Merge env vars into a .env file's sentinel-managed block.

    Returns ``(new_lines, changed, skipped)`` where:
    - ``new_lines``: the updated file content as a list of lines
    - ``changed``: keys that were created or updated
    - ``skipped``: keys that had a different existing value outside the block

    Idempotent: if sentinels exist, the block is replaced.
    No-clobber: if a key exists outside the block with a different value, warn and skip.
    """
    # Check for existing keys outside the managed block
    outside_keys = _parse_existing_env_keys(lines, exclude_block=True)
    # Also parse the existing managed block to detect no-op
    existing_block_keys = _parse_existing_env_keys(lines, exclude_block=False)
    # Remove outside keys from the block set so we only see managed-block values
    managed_block_keys = {k: v for k, v in existing_block_keys.items() if k not in outside_keys}

    skipped: list[str] = []
    to_write: dict[str, str] = {}
    for key, value in env_vars.items():
        if key in outside_keys:
            if outside_keys[key] != value:
                skipped.append(f"{key} (existing: {outside_keys[key]})")
                continue
            # Same value already set outside — skip silently
            continue
        to_write[key] = value

    # Detect no-op: all keys already in the managed block with the same value
    already_managed = all(
        key in managed_block_keys and managed_block_keys[key] == value
        for key, value in to_write.items()
    ) and len(to_write) == len(managed_block_keys)

    # Build the managed block
    managed_lines: list[str] = [_SENTINEL_BEGIN]
    for key, value in to_write.items():
        managed_lines.append(f"{key}={value}")
    managed_lines.append(_SENTINEL_END)

    changed: list[str] = [] if already_managed else list(to_write.keys())

    # Find and replace (or insert) the sentinel block
    new_lines: list[str] = []
    in_block = False
    block_replaced = False

    for line in lines:
        stripped = line.strip()
        if stripped == _SENTINEL_BEGIN:
            in_block = True
            continue
        if stripped == _SENTINEL_END:
            in_block = False
            # Insert managed block here
            new_lines.extend(managed_lines)
            block_replaced = True
            continue
        if in_block:
            continue
        new_lines.append(line)

    if not block_replaced:
        # No existing sentinel block — append it
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.extend(managed_lines)

    return new_lines, changed, skipped


def _remove_env_file_block(lines: list[str]) -> tuple[list[str], list[str]]:
    """Remove the sentinel-managed block from .env lines.

    Returns ``(new_lines, removed_keys)``.
    """
    new_lines: list[str] = []
    in_block = False
    removed_keys: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped == _SENTINEL_BEGIN:
            in_block = True
            continue
        if stripped == _SENTINEL_END:
            in_block = False
            continue
        if in_block:
            if "=" in stripped and not stripped.startswith("#"):
                key, _, _ = stripped.partition("=")
                removed_keys.append(key.strip())
            continue
        new_lines.append(line)
    return new_lines, removed_keys


def _find_hermes_plugin_source() -> str | None:
    """Find the hermes plugin source directory (adapters/hermes/).

    Resolution order:
    1. ``$AGENT_WAKE_HERMES_PLUGIN`` (explicit operator override).
    2. The daemon's sibling source-tree path
       (``<repo>/adapters/hermes/``).
    3. The shared install location
       (``~/.local/share/agent-wake/hermes/``).
    """
    env_override = os.environ.get("AGENT_WAKE_HERMES_PLUGIN")
    if env_override:
        p = Path(env_override).expanduser()
        if p.is_dir() and (p / "plugin.yaml").is_file() and (p / "__init__.py").is_file():
            return str(p)
        return None
    # parents[4] from daemon/src/agent_waked/cli/install_harness.py → repo root
    candidates = [
        Path(__file__).resolve().parents[4] / "adapters" / "hermes",
        Path.home() / ".local" / "share" / "agent-wake" / "hermes",
    ]
    for c in candidates:
        if c.is_dir() and (c / "plugin.yaml").is_file() and (c / "__init__.py").is_file():
            return str(c)
    return None


# ── hermes wiring ──────────────────────────────────────────────────────────────


def _wire_hermes(dry_run: bool, uninstall: bool, user: str | None) -> list[dict[str, Any]]:
    """Wire or unwire hermes. Returns list of action dicts."""
    actions: list[dict[str, Any]] = []
    manifest = _load_manifest()

    if uninstall:
        return _unwire_hermes(dry_run, manifest, actions)

    plugin_source = _find_hermes_plugin_source()
    if not plugin_source:
        actions.append({
            "kind": "check_failed",
            "path": "adapters/hermes",
            "detail": "hermes plugin source not found; expected adapters/hermes/plugin.yaml and __init__.py",
        })
        return actions

    # ── env file ──
    env_vars = _wake_env_vars()
    if user:
        env_vars["AGENT_WAKE_PRINCIPAL_ID"] = user

    lines = _parse_env_file(_HERMES_ENV)
    new_lines, changed_env, skipped_env = _merge_env_file_block(lines, env_vars)

    if skipped_env:
        actions.append({
            "kind": "warn",
            "path": str(_HERMES_ENV),
            "keys": skipped_env,
            "detail": f"skipped (existing values differ): {', '.join(skipped_env)}",
        })

    env_changed = bool(changed_env)

    # ── plugin copy ──
    plugin_changed = False
    src = Path(plugin_source)
    plugin_files = ["plugin.yaml", "__init__.py"]
    need_copy = any(
        not (_HERMES_PLUGIN_DIR / f).exists()
        or (_HERMES_PLUGIN_DIR / f).read_bytes() != (src / f).read_bytes()
        for f in plugin_files
    )
    if need_copy:
        plugin_changed = True

    if env_changed or plugin_changed:
        detail_parts: list[str] = []
        if env_changed:
            detail_parts.append(f"set wake env vars ({', '.join(changed_env)})")
        if plugin_changed:
            detail_parts.append("copy hermes plugin")
        actions.append({
            "kind": "merge_env" if env_changed and not plugin_changed else "install_hermes",
            "path": str(_HERMES_ENV),
            "keys": changed_env,
            "detail": "; ".join(detail_parts),
        })
    elif not skipped_env:
        actions.append({
            "kind": "noop",
            "path": str(_HERMES_ENV),
            "keys": [],
            "detail": "env vars and plugin already configured",
        })

    if not dry_run and (env_changed or plugin_changed):
        if env_changed:
            _write_env_file(_HERMES_ENV, new_lines)
        if plugin_changed:
            _HERMES_PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
            for fname in plugin_files:
                shutil.copy2(src / fname, _HERMES_PLUGIN_DIR / fname)
        manifest.setdefault("installed", {})["hermes"] = {
            "env_path": str(_HERMES_ENV),
            "plugin_dir": str(_HERMES_PLUGIN_DIR),
            "env_keys": changed_env,
        }
        _save_manifest(manifest)

    return actions


def _unwire_hermes(
    dry_run: bool, manifest: dict[str, Any], actions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Remove agent-wake entries from hermes config."""
    entry = manifest.get("installed", {}).get("hermes")
    if not entry:
        actions.append({"kind": "noop", "path": str(_HERMES_ENV), "keys": [], "detail": "hermes not wired by install-harness"})
        return actions

    removed: list[str] = []

    # Remove env block
    lines = _parse_env_file(_HERMES_ENV)
    new_lines, removed_keys = _remove_env_file_block(lines)
    if removed_keys:
        removed.extend(f"env.{k}" for k in removed_keys)

    # Remove plugin directory
    plugin_dir = entry.get("plugin_dir")
    plugin_removed = False
    if plugin_dir:
        pdir = Path(plugin_dir)
        if pdir.exists():
            plugin_removed = True
            if not dry_run:
                shutil.rmtree(pdir)

    if removed or plugin_removed:
        actions.append({
            "kind": "remove_keys",
            "path": str(_HERMES_ENV),
            "keys": removed,
            "detail": f"removed wake env block{' and plugin directory' if plugin_removed else ''} from hermes",
        })
    else:
        actions.append({"kind": "noop", "path": str(_HERMES_ENV), "keys": [], "detail": "nothing to remove"})

    if not dry_run and removed_keys:
        _write_env_file(_HERMES_ENV, new_lines)
    if not dry_run:
        manifest.get("installed", {}).pop("hermes", None)
        _save_manifest(manifest)

    return actions


# ── command dispatch ───────────────────────────────────────────────────────────


def _run_install(harness: str, dry_run: bool, uninstall: bool, user: str | None) -> dict[str, Any]:
    """Run install-harness for the given target. Returns the dry-run shape or result."""
    targets: list[str]
    if harness == "all":
        targets = ["claude", "opencode", "hermes"]
    else:
        targets = [harness]

    all_actions: list[dict[str, Any]] = []
    no_op = True

    for target in targets:
        if target == "claude":
            actions = _wire_claude(dry_run, uninstall, user)
        elif target == "opencode":
            actions = _wire_opencode(dry_run, uninstall, user)
        elif target == "hermes":
            actions = _wire_hermes(dry_run, uninstall, user)
        else:
            all_actions.append({"kind": "error", "path": "", "detail": f"unknown harness: {target}"})
            no_op = False
            continue
        all_actions.extend(actions)
        if any(a["kind"] not in ("noop", "info") for a in actions):
            no_op = False

    return {
        "tool": "agent-wake",
        "harness": harness,
        "user": user,
        "actions": all_actions,
        "no_op": no_op,
    }


def _cmd_install_harness(args: argparse.Namespace) -> int:
    result = _run_install(
        harness=args.harness,
        dry_run=args.dry_run,
        uninstall=args.uninstall,
        user=args.user,
    )

    if args.dry_run:
        print(json.dumps(result, indent=2))
        return 2

    # Human-readable summary
    for action in result["actions"]:
        kind = action["kind"]
        path = action.get("path") or ""
        detail = action.get("detail", "")
        print(f"  {kind:12} {path}: {detail}")

    has_error = any(a["kind"] in ("error", "check_failed") for a in result["actions"])
    if has_error:
        print("Install failed — see errors above.")
        return 1
    if result["no_op"]:
        print("Already installed — no changes needed.")
    elif args.uninstall:
        print("Uninstall complete.")
    else:
        print("Install complete.")
    return 0


def _build_install_harness_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "install-harness",
        help="Wire wake adapters into a harness config",
    )
    p.add_argument(
        "harness",
        choices=["claude", "opencode", "hermes", "all"],
        help="Target harness to wire",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned changes as JSON; act on nothing",
    )
    p.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove agent-wake wiring from the harness config",
    )
    p.add_argument(
        "--user",
        help="Per-user wiring (writes principal_id into harness env block)",
    )
    p.set_defaults(func=_cmd_install_harness)
