"""env:// secret backend.

Reads secrets from environment variables.  The URI grammar is:

    env://VAR_NAME[?ttl=...]

The ``ttl`` query parameter is accepted but silently ignored — env vars are
static within a process lifetime.  A SIGHUP-triggered ``refresh`` re-reads the
env var in case the operator restarted the service with an updated
EnvironmentFile; in practice this is a no-op unless the process was relaunched.

Cache TTL is effectively infinite (cache lives forever until explicit refresh).
"""

import logging
import os
from urllib.parse import urlparse

log = logging.getLogger("agent_waked.secrets.env")

_SENTINEL = object()


class EnvBackend:
    """Backend that resolves ``env://`` URIs from the process environment."""

    def __init__(self) -> None:
        # uri → bytes  (None sentinel means "not yet fetched")
        self._cache: dict[str, bytes] = {}

    # ── SecretBackend protocol ────────────────────────────────────────────────

    def supports(self, scheme: str) -> bool:
        return scheme == "env"

    async def resolve(self, uri: str) -> bytes:
        if uri in self._cache:
            return self._cache[uri]
        return await self.refresh(uri)

    async def refresh(self, uri: str) -> bytes:
        value = self._fetch(uri)
        self._cache[uri] = value
        return value

    # ── internal ─────────────────────────────────────────────────────────────

    @staticmethod
    def _var_name(uri: str) -> str:
        """Extract the env-var name from an env:// URI."""
        parsed = urlparse(uri)
        # env://VAR_NAME  — netloc holds the var name
        name = parsed.netloc or parsed.path.lstrip("/")
        if not name:
            raise ValueError(f"env:// URI has no variable name: {uri!r}")
        return name

    @staticmethod
    def _fetch(uri: str) -> bytes:
        name = EnvBackend._var_name(uri)
        value = os.environ.get(name)
        if value is None:
            raise RuntimeError(
                f"Secret env var {name!r} (from URI {uri!r}) is not set."
            )
        return value.encode("utf-8")
