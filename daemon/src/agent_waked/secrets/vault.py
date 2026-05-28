"""vault:// secret backend using HashiCorp Vault via hvac.

URI grammar:

    vault://PATH#KEY[?ttl=15m]

Where:
  PATH = the KV v2 mount path + secret path (e.g. ``secret/data/agent-wake/src``)
  KEY  = the field name inside the secret data dict (e.g. ``current``)
  ttl  = optional Go-style duration; defaults to 15 m for KV-static secrets.
         Lease-based (dynamic) secrets use ⅔ of the lease_duration instead.

Authentication:
  AppRole only in v1.  ``role_id`` is read from the vault config block;
  ``secret_id`` is read from a mode-600 file at ``auth.secret_id_file``.
  On token expiry (HVAC raises ``hvac.exceptions.Forbidden`` or similar),
  the backend automatically re-logs-in and retries once.

Optional dependency:
  ``hvac>=2.0`` is required.  If it is not installed the resolver raises a
  clear RuntimeError (see resolver.py).

Thread safety:
  ``hvac.Client`` is synchronous; all Vault I/O is run in a thread via
  ``asyncio.to_thread`` to avoid blocking the event loop.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("agent_waked.secrets.vault")

try:
    import hvac  # type: ignore[import-untyped]
    import hvac.exceptions  # type: ignore[import-untyped]
    _HVAC_AVAILABLE = True
except ImportError:
    _HVAC_AVAILABLE = False


class VaultBackend:
    """Backend that resolves ``vault://`` URIs from HashiCorp Vault (KV v2)."""

    def __init__(self, vault_cfg: dict[str, Any]) -> None:
        if not _HVAC_AVAILABLE:
            raise RuntimeError(
                "vault:// URIs require the 'hvac' package. "
                "Install with: pip install agent-waked[vault]"
            )
        self._cfg = vault_cfg
        self._client: Any = None  # hvac.Client, lazy-initialised

    # ── SecretBackend protocol ────────────────────────────────────────────────

    def supports(self, scheme: str) -> bool:
        return scheme == "vault"

    async def resolve(self, uri: str) -> bytes:
        # Cache is managed by the resolver; here we always go to Vault.
        return await self.refresh(uri)

    async def refresh(self, uri: str) -> bytes:
        return await asyncio.to_thread(self._sync_fetch, uri)

    # ── internal ─────────────────────────────────────────────────────────────

    def _ensure_client(self) -> Any:
        """Lazy-create and authenticate the hvac client."""
        if self._client is None:
            self._client = self._login()
        return self._client

    def _login(self) -> Any:
        """AppRole login; returns an authenticated hvac.Client."""
        cfg = self._cfg
        addr: str = cfg["addr"]
        auth: dict[str, Any] = cfg["auth"]
        role_id: str = auth["role_id"]
        secret_id_path = Path(os.path.expanduser(auth["secret_id_file"]))
        secret_id = secret_id_path.read_text(encoding="utf-8").strip()

        namespace: str | None = cfg.get("namespace")
        client = hvac.Client(url=addr, namespace=namespace)
        client.auth.approle.login(role_id=role_id, secret_id=secret_id)
        if not client.is_authenticated():
            raise RuntimeError("Vault AppRole login succeeded but client is not authenticated.")
        log.info("Vault AppRole login successful (addr=%s)", addr)
        return client

    def _sync_fetch(self, uri: str) -> bytes:
        """Synchronous fetch — runs in a thread pool thread."""
        client = self._ensure_client()
        try:
            return self._read_kv(client, uri)
        except Exception as exc:
            # On auth errors, attempt a single re-login.
            if self._is_auth_error(exc):
                log.warning("Vault token expired, re-logging in: %s", exc)
                self._client = None
                client = self._ensure_client()
                return self._read_kv(client, uri)
            raise

    def _read_kv(self, client: Any, uri: str) -> bytes:
        """Read a KV v2 secret and return the requested field as bytes."""
        parsed = urlparse(uri)
        # vault://PATH#KEY[?ttl=...] — netloc + path gives the KV path.
        # The fragment may carry ?ttl= (plan grammar); strip it for the key.
        kv_path = (parsed.netloc + parsed.path).lstrip("/")
        fragment = parsed.fragment
        # Strip trailing ?ttl=... from fragment if present
        key = fragment.split("?")[0] if "?" in fragment else fragment
        if not key:
            raise ValueError(
                f"vault:// URI {uri!r} is missing a fragment (#key). "
                "Specify which field to read, e.g. vault://secret/data/foo#current"
            )

        # KV v2: split mount from secret path.
        # Convention: first path component is the mount point.
        parts = kv_path.split("/", 1)
        mount = parts[0]
        secret_path = parts[1] if len(parts) > 1 else ""

        response = client.secrets.kv.v2.read_secret_version(
            path=secret_path,
            mount_point=mount,
        )
        data: dict[str, Any] = response["data"]["data"]
        if key not in data:
            raise KeyError(
                f"Field {key!r} not found in Vault secret at {kv_path!r}. "
                f"Available fields: {sorted(data.keys())}"
            )
        value = data[key]
        if isinstance(value, bytes):
            return value
        return str(value).encode("utf-8")

    @staticmethod
    def _is_auth_error(exc: Exception) -> bool:
        if not _HVAC_AVAILABLE:
            return False
        return isinstance(exc, (hvac.exceptions.Forbidden, hvac.exceptions.InvalidRequest))
