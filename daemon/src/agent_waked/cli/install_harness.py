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
from pathlib import Path
from typing import Any

from ..config import DEFAULT_CONFIG_PATH

# ── paths ──────────────────────────────────────────────────────────────────────

_CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
_OPENCODE_CONFIG = Path.home() / ".config" / "opencode" / "opencode.json"
_MANIFEST_PATH = Path.home() / ".config" / "agent-wake" / "install-manifest.json"

# Sentinel to mark entries as agent-wake-owned (for safe uninstall)
_SENTINEL = "# managed by agent-wake install-harness"

# Env vars we set in harness configs
_WAKE_ENV_VARS: dict[str, str] = {
    "AGENT_WAKE_CONFIG": str(DEFAULT_CONFIG_PATH),
}


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
    _MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


# ── JSON merge helpers ─────────────────────────────────────────────────────────


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data
    except (json.JSONDecodeError, OSError):
        return {}


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _merge_env_block(config: dict[str, Any], env_vars: dict[str, str]) -> list[str]:
    """Merge env vars into the config's 'env' block. Returns list of keys changed."""
    env = config.setdefault("env", {})
    changed: list[str] = []
    for key, value in env_vars.items():
        if key not in env or env[key] != value:
            env[key] = value
            changed.append(f"env.{key}")
    return changed


# ── claude wiring ──────────────────────────────────────────────────────────────


def _wire_claude(dry_run: bool, uninstall: bool) -> list[dict[str, Any]]:
    """Wire or unwire claude. Returns list of action dicts."""
    actions: list[dict[str, Any]] = []
    manifest = _load_manifest()

    if uninstall:
        return _unwire_claude(dry_run, manifest, actions)

    # Check adapter is installed
    adapter_path = shutil.which("agent-wake-claude")
    if not adapter_path:
        actions.append({
            "kind": "check_failed",
            "path": "agent-wake-claude",
            "detail": "agent-wake-claude not on PATH; run: pip install -e adapters/claude",
        })
        return actions

    config = _load_json(_CLAUDE_SETTINGS)
    changed = _merge_env_block(config, _WAKE_ENV_VARS)

    if changed:
        actions.append({
            "kind": "merge_json",
            "path": str(_CLAUDE_SETTINGS),
            "keys": changed,
            "detail": "set wake env vars in claude settings",
        })
    else:
        actions.append({
            "kind": "noop",
            "path": str(_CLAUDE_SETTINGS),
            "keys": [],
            "detail": "env vars already set",
        })

    # Document the launch flag (informational — channels use a CLI flag, not config)
    actions.append({
        "kind": "info",
        "path": None,
        "keys": [],
        "detail": "launch claude with: --dangerously-load-development-channels server:agent-wake-claude",
    })

    if not dry_run and changed:
        _save_json(_CLAUDE_SETTINGS, config)
        manifest.setdefault("installed", {})["claude"] = {
            "settings_path": str(_CLAUDE_SETTINGS),
            "env_keys": list(_WAKE_ENV_VARS.keys()),
        }
        _save_manifest(manifest)

    return actions


def _unwire_claude(dry_run: bool, manifest: dict[str, Any], actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove agent-wake entries from claude settings."""
    entry = manifest.get("installed", {}).get("claude")
    if not entry:
        actions.append({"kind": "noop", "path": str(_CLAUDE_SETTINGS), "keys": [], "detail": "claude not wired by install-harness"})
        return actions

    config = _load_json(_CLAUDE_SETTINGS)
    env = config.get("env", {})
    removed: list[str] = []
    for key in entry.get("env_keys", []):
        if key in env:
            del env[key]
            removed.append(f"env.{key}")
    if not env:
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

    if not dry_run:
        _save_json(_CLAUDE_SETTINGS, config)
        manifest.get("installed", {}).pop("claude", None)
        _save_manifest(manifest)

    return actions


# ── opencode wiring ────────────────────────────────────────────────────────────


def _find_opencode_plugin_path() -> str | None:
    """Find the built opencode plugin dist/index.js."""
    candidates = [
        Path(__file__).resolve().parents[3] / "adapters" / "opencode" / "dist" / "index.js",
        Path.home() / ".local" / "share" / "agent-wake" / "opencode" / "dist" / "index.js",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _wire_opencode(dry_run: bool, uninstall: bool) -> list[dict[str, Any]]:
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

    config = _load_json(_OPENCODE_CONFIG)

    # Merge env vars
    changed = _merge_env_block(config, _WAKE_ENV_VARS)

    # Register plugin if not already present
    plugins = config.setdefault("plugins", [])
    plugin_entry = {"path": plugin_path, "type": "module"}
    already_present = any(
        isinstance(p, dict) and p.get("path") == plugin_path
        for p in plugins
    )
    if not already_present:
        plugins.append(plugin_entry)
        changed.append(f"plugins[{plugin_path}]")

    if changed:
        actions.append({
            "kind": "merge_json",
            "path": str(_OPENCODE_CONFIG),
            "keys": changed,
            "detail": "set wake env vars and register opencode plugin",
        })
    else:
        actions.append({
            "kind": "noop",
            "path": str(_OPENCODE_CONFIG),
            "keys": [],
            "detail": "env vars and plugin already configured",
        })

    if not dry_run and changed:
        _save_json(_OPENCODE_CONFIG, config)
        manifest.setdefault("installed", {})["opencode"] = {
            "config_path": str(_OPENCODE_CONFIG),
            "env_keys": list(_WAKE_ENV_VARS.keys()),
            "plugin_path": plugin_path,
        }
        _save_manifest(manifest)

    return actions


def _unwire_opencode(dry_run: bool, manifest: dict[str, Any], actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove agent-wake entries from opencode config."""
    entry = manifest.get("installed", {}).get("opencode")
    if not entry:
        actions.append({"kind": "noop", "path": str(_OPENCODE_CONFIG), "keys": [], "detail": "opencode not wired by install-harness"})
        return actions

    config = _load_json(_OPENCODE_CONFIG)
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
    if plugin_path:
        plugins = config.get("plugins", [])
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

    if not dry_run:
        _save_json(_OPENCODE_CONFIG, config)
        manifest.get("installed", {}).pop("opencode", None)
        _save_manifest(manifest)

    return actions


# ── command dispatch ───────────────────────────────────────────────────────────


def _run_install(harness: str, dry_run: bool, uninstall: bool, user: str | None) -> dict[str, Any]:
    """Run install-harness for the given target. Returns the dry-run shape or result."""
    targets: list[str]
    if harness == "all":
        targets = ["claude", "opencode"]
    else:
        targets = [harness]

    all_actions: list[dict[str, Any]] = []
    no_op = True

    for target in targets:
        if target == "claude":
            actions = _wire_claude(dry_run, uninstall)
        elif target == "opencode":
            actions = _wire_opencode(dry_run, uninstall)
        else:
            all_actions.append({"kind": "error", "path": None, "keys": [], "detail": f"unknown harness: {target}"})
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
        return 2  # per contract: --dry-run exits 2

    # Human-readable summary
    for action in result["actions"]:
        kind = action["kind"]
        path = action.get("path") or ""
        detail = action.get("detail", "")
        print(f"  {kind:12} {path}: {detail}")

    if result["no_op"]:
        print("Already installed — no changes needed.")
    elif args.uninstall:
        print("Uninstall complete.")
    else:
        print("Install complete.")

    # Exit 0 for success (including idempotent no-op), 1 if any check_failed
    if any(a["kind"] == "check_failed" for a in result["actions"]):
        return 1
    return 0


def _build_install_harness_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "install-harness",
        help="Wire wake adapters into a harness config",
    )
    p.add_argument(
        "harness",
        choices=["claude", "opencode", "all"],
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
        help="Per-user wiring (principal_id); system-level when omitted",
    )
    p.set_defaults(func=_cmd_install_harness)
