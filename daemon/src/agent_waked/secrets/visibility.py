"""Which of a config's secrets are readable from *this* process.

Secret resolution is the **daemon's** concern.  The daemon is the component that
signs and verifies wake events, so it is the only one that must be able to read
the material.  Other contexts legitimately cannot: ``agent-wake doctor`` is run
by the suite's scheduled ``agent-suite-doctor-alert.service`` as ``root`` with
only ``/etc/agent-suite/suite.env`` loaded, which by design does not carry
per-source signing keys (agent-suite ``docs/secrets-vault.md``), and a ``%h`` path
would not reach the user's ``secrets.env`` from a root unit anyway.

This module answers exactly one question and attaches no policy to the answer:
for a loaded config, which sources declare ``env://`` secrets that are not set
in this process's environment?  Callers decide what that means.

- ``main`` refuses to start, and refuses a SIGHUP reload, when the answer is
  non-empty — the daemon cannot sign for a source whose key it cannot read.
- ``ingest``'s ``GET /`` health handler reports the daemon's own answer, so the
  daemon can be *asked*.
- ``doctor`` treats a non-empty answer as a limitation of its own context and
  defers to the daemon's report, which is the authoritative one.

``vault://`` URIs are deliberately not probed here.  Vault reachability is a
runtime condition rather than a property of this process's environment, and a
network round-trip belongs in neither a startup gate nor an unauthenticated
health handler.  ``SecretResolver`` surfaces a vault failure per request with a
precise message, and the doctor's ``ingress_reachable`` check covers liveness.

Nothing here returns secret *values* — only names and counts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class SecretVisibility:
    """Per-config summary of env-backed secret readability in this process."""

    configured: int
    """Number of sources in the config."""

    unresolved: tuple[str, ...]
    """Source names with at least one ``env://`` secret that is not set here."""

    missing_env_vars: tuple[str, ...]
    """Distinct env-var names referenced but not set, sorted."""

    @property
    def all_visible(self) -> bool:
        """True when every ``env://`` secret referenced by the config is set."""
        return not self.unresolved


def env_var_name(uri: str) -> str | None:
    """Return the env-var name for an ``env://`` URI, or None for other schemes.

    Mirrors ``EnvBackend._var_name``: the name lives in the netloc, with the
    path as a fallback for ``env:///NAME`` spellings.
    """
    if not isinstance(uri, str):
        return None
    try:
        parsed = urlparse(uri)
    except Exception:
        return None
    if parsed.scheme != "env":
        return None
    name = parsed.netloc or parsed.path.lstrip("/")
    return name or None


def missing_env_vars(source_cfg: dict[str, Any]) -> list[str]:
    """Env-var names this source needs that are absent from ``os.environ``.

    A source with no ``secret_uris`` contributes nothing: "declares no secret"
    is a different defect, and the doctor's ``auth_configured`` check owns it.
    """
    uris = source_cfg.get("secret_uris") if isinstance(source_cfg, dict) else None
    if not isinstance(uris, list):
        return []
    missing: list[str] = []
    for uri in uris:
        name = env_var_name(uri)
        if name is not None and os.environ.get(name) is None:
            missing.append(name)
    return missing


def source_secret_visibility(cfg: dict[str, Any]) -> SecretVisibility:
    """Summarise which sources' env-backed secrets are readable in this process."""
    sources = cfg.get("sources") or {}
    if not isinstance(sources, dict):
        return SecretVisibility(configured=0, unresolved=(), missing_env_vars=())

    unresolved: list[str] = []
    missing: set[str] = set()
    for name, info in sources.items():
        if not isinstance(info, dict):
            continue
        gaps = missing_env_vars(info)
        if gaps:
            unresolved.append(str(name))
            missing.update(gaps)

    return SecretVisibility(
        configured=len(sources),
        unresolved=tuple(sorted(unresolved)),
        missing_env_vars=tuple(sorted(missing)),
    )


def health_summary(cfg: dict[str, Any]) -> dict[str, Any]:
    """The ``sources`` block for ``GET /``.

    ``GET /`` is reachable without authentication, so this deliberately carries
    only counts and source names — the same class of information as the existing
    ``delivery`` block's principal ids, and already known to any sender (a
    source name travels in the ``X-AgentWake-Source`` header).  Env-var names
    are *not* included here: unlike the operator-facing doctor detail, this is
    published on an unauthenticated port and naming the variables would tell an
    unauthenticated caller exactly which environment keys to go after.
    """
    vis = source_secret_visibility(cfg)
    return {
        "configured": vis.configured,
        "secrets_unresolved": len(vis.unresolved),
        "unresolved": list(vis.unresolved),
    }
