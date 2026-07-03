"""Suite configuration resolution — reads suite.env with the canonical precedence.

Implements the config contract from agent-suite-blueprint.md §2.1 and
bootstrap-contract.md §2:

    process env  >  per-user suite.env  >  system suite.env  >  tool default

The per-user file lives at ``~/.config/agent-suite/suite.env`` (override via
``$AGENT_SUITE_CONFIG``).  The system file lives at ``/etc/agent-suite/suite.env``
on Linux and ``%ProgramData%\\agent-suite\\suite.env`` on Windows.

Canonical suite vars (owned by regista, adopted by every consumer):

- ``REGISTA_DSN`` — Postgres connection string for the regista store
- ``REGISTA_KEY_PATH`` — signing key path or backend ref (``file:``, ``vault:``, etc.)
- ``REGISTA_REQUIRE_SSL`` — ``true``/``false``
- ``AGENT_WAKE_PROJECT`` — per-consumer project slug (agent-wake's own)

agent-wake keeps its own vars (``AGENT_WAKE_CONFIG``, ``AGENT_WAKE_LOG_LEVEL``,
etc.) separate — those are wake-specific and not part of the suite contract.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# ── suite.env path resolution ──────────────────────────────────────────────────

_PER_USER_DEFAULT = Path.home() / ".config" / "agent-suite" / "suite.env"

if sys.platform == "win32":
    _SYSTEM_DEFAULT = Path(os.environ.get("ProgramData", "C:\\ProgramData")) / "agent-suite" / "suite.env"
else:
    _SYSTEM_DEFAULT = Path("/etc/agent-suite/suite.env")

# Canonical suite env vars that agent-wake reads.
_SUITE_VARS: tuple[str, ...] = (
    "REGISTA_DSN",
    "REGISTA_KEY_PATH",
    "REGISTA_REQUIRE_SSL",
    "AGENT_WAKE_PROJECT",
)

# Back-compat aliases: none yet (agent-wake predates the suite contract with its
# own AGENT_WAKE_* vars). When regista DSN aliases are added, they go here and
# are removed after one release.
_ALIASES: dict[str, str] = {}


def per_user_path() -> Path:
    """Path to the per-user suite.env (override via ``$AGENT_SUITE_CONFIG``)."""
    env = os.environ.get("AGENT_SUITE_CONFIG")
    return Path(env) if env else _PER_USER_DEFAULT


def system_path() -> Path:
    """Path to the system-level suite.env."""
    return _SYSTEM_DEFAULT


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE file with # comments. Returns a dict."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return result
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if not key:
            continue
        # Strip surrounding quotes if present
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value
    return result


def resolve_suite_env() -> dict[str, str]:
    """Resolve suite env vars with the canonical precedence.

    Returns a dict of canonical var → value for all vars that are set.

    Precedence (highest wins):
        1. Process environment (``os.environ``)
        2. Per-user suite.env (``$AGENT_SUITE_CONFIG`` or ``~/.config/agent-suite/suite.env``)
        3. System suite.env (``/etc/agent-suite/suite.env``)
    """
    # Layer 3: system file (lowest priority)
    merged = _parse_env_file(system_path())
    # Layer 2: per-user file (overrides system)
    merged.update(_parse_env_file(per_user_path()))
    # Layer 1: process env (highest priority)
    for var in _SUITE_VARS:
        val = os.environ.get(var)
        if val is not None:
            merged[var] = val
    # Apply aliases
    for alias, canonical in _ALIASES.items():
        if alias in merged and canonical not in merged:
            merged[canonical] = merged[alias]

    # Return only the canonical vars that have values
    return {k: v for k, v in merged.items() if k in _SUITE_VARS}


def get(key: str, default: str | None = None) -> str | None:
    """Get a single suite var, resolved through the full precedence chain."""
    return resolve_suite_env().get(key, default)


def is_suite_configured() -> bool:
    """True if any suite.env file exists (per-user or system)."""
    return per_user_path().exists() or system_path().exists()


def regista_config() -> dict[str, Any]:
    """Return regista-related config from the suite, if present.

    Keys: ``dsn``, ``key_path``, ``require_ssl``, ``project``.
    Values are ``None`` if not configured.
    """
    env = resolve_suite_env()
    return {
        "dsn": env.get("REGISTA_DSN"),
        "key_path": env.get("REGISTA_KEY_PATH"),
        "require_ssl": env.get("REGISTA_REQUIRE_SSL", "false").lower() == "true",
        "project": env.get("AGENT_WAKE_PROJECT"),
    }
