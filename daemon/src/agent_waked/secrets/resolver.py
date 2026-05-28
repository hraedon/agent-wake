"""SecretResolver — cache-aware dispatcher across multiple backends.

Cache key  = URI string.
Cache entry = (value: bytes, expires_at: float)   [float('-inf') = never expires]

Backends are tried in registration order; the first that ``supports`` the
URI scheme wins.  If no backend supports a scheme, RuntimeError is raised.
"""

import logging
import math
import time
from typing import Any
from urllib.parse import urlparse, parse_qs

from .base import SecretBackend
from .env import EnvBackend

log = logging.getLogger("agent_waked.secrets.resolver")

# Default cache TTL for vault-static secrets (15 minutes).
_DEFAULT_VAULT_TTL = 15 * 60.0

# Entries with this expiry never expire (env backend).
_NEVER_EXPIRES = math.inf


class SecretResolver:
    """Dispatcher that routes URIs to registered backends with a TTL cache."""

    def __init__(
        self,
        backends: list[SecretBackend] | None = None,
        vault_cfg: dict[str, Any] | None = None,
    ) -> None:
        self._vault_cfg = vault_cfg
        # (value, expires_at) pairs keyed by URI
        self._cache: dict[str, tuple[bytes, float]] = {}

        if backends is not None:
            self._backends: list[SecretBackend] = list(backends)
        else:
            # Default set: env always present; vault added lazily if configured.
            self._backends = [EnvBackend()]
            if vault_cfg is not None:
                self._backends.append(_build_vault_backend(vault_cfg))

    # ── public API ────────────────────────────────────────────────────────────

    async def resolve(self, uri: str) -> bytes:
        """Return the secret for *uri*, using the cache if still fresh."""
        entry = self._cache.get(uri)
        if entry is not None:
            value, expires_at = entry
            if expires_at == _NEVER_EXPIRES or time.monotonic() < expires_at:
                return value
        return await self._fetch_and_cache(uri)

    async def resolve_all(self, source_cfg: dict[str, Any]) -> list[bytes]:
        """Resolve all URIs for a source config dict.

        Returns a list of secret bytes in the same order as ``secret_uris``.
        """
        uris: list[str] = source_cfg.get("secret_uris", [])
        results: list[bytes] = []
        for uri in uris:
            results.append(await self.resolve(uri))
        return results

    async def refresh_all(self) -> None:
        """Bypass cache and re-fetch every URI currently cached.

        Called on SIGHUP so vault-mode picks up rotations without restart.
        """
        for uri in list(self._cache.keys()):
            try:
                await self._fetch_and_cache(uri)
            except Exception as exc:
                log.warning("refresh_all: failed to refresh %r: %s", uri, exc)

    # ── internal ─────────────────────────────────────────────────────────────

    def _backend_for(self, uri: str) -> SecretBackend:
        scheme = urlparse(uri).scheme
        for backend in self._backends:
            if backend.supports(scheme):
                return backend
        if scheme == "vault":
            raise RuntimeError(
                f"No vault backend available for {uri!r}. "
                "Install agent-waked[vault] and ensure the 'vault' config block is set."
            )
        raise RuntimeError(
            f"No backend supports URI scheme {scheme!r} in {uri!r}."
        )

    async def _fetch_and_cache(self, uri: str) -> bytes:
        backend = self._backend_for(uri)
        value = await backend.refresh(uri)
        expires_at = self._ttl_for(uri)
        self._cache[uri] = (value, expires_at)
        return value

    def _ttl_for(self, uri: str) -> float:
        """Compute the cache-expiry timestamp for *uri*.

        - env://  → never expires (math.inf)
        - vault:// → now + 15 min (default) or ?ttl= override
        """
        parsed = urlparse(uri)
        scheme = parsed.scheme

        if scheme == "env":
            return _NEVER_EXPIRES

        # vault:// — check for ?ttl= override.
        # The plan grammar places ?ttl= after the fragment: #KEY?ttl=30s
        # Standard urlparse puts this in the fragment; also check parsed.query.
        ttl_vals = parse_qs(parsed.query).get("ttl", [])
        if not ttl_vals and "?" in parsed.fragment:
            fragment_qs = parsed.fragment.split("?", 1)[1]
            ttl_vals = parse_qs(fragment_qs).get("ttl", [])
        if ttl_vals:
            ttl_secs = _parse_duration(ttl_vals[0])
        else:
            ttl_secs = _DEFAULT_VAULT_TTL

        return time.monotonic() + ttl_secs


def _build_vault_backend(vault_cfg: dict[str, Any]) -> SecretBackend:
    try:
        from .vault import VaultBackend
    except ImportError:
        raise RuntimeError(
            "vault:// URIs require the 'hvac' package. "
            "Install with: pip install agent-waked[vault]"
        )
    backend: SecretBackend = VaultBackend(vault_cfg)
    return backend


def _parse_duration(s: str) -> float:
    """Parse a Go-style duration string to seconds.

    Accepted suffixes: s (seconds), m (minutes), h (hours).
    """
    s = s.strip()
    if s.endswith("h"):
        return float(s[:-1]) * 3600
    if s.endswith("m"):
        return float(s[:-1]) * 60
    if s.endswith("s"):
        return float(s[:-1])
    raise ValueError(f"Cannot parse duration {s!r}. Expected Go-style: 30s, 5m, 1h.")
