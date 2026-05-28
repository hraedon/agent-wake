"""Tests for the Vault backend.

Uses unittest.mock to stub out hvac so no real Vault server is needed.
Covers: read path, TTL behaviour, AppRole login retry on token expiry,
SIGHUP-triggered refresh.
"""

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_waked.secrets.vault import VaultBackend
from agent_waked.secrets.resolver import SecretResolver


# ── helpers ───────────────────────────────────────────────────────────────────

def _vault_cfg(tmp_path: Path) -> dict:
    sid_file = tmp_path / "vault-secret-id"
    sid_file.write_text("my-secret-id")
    return {
        "addr": "https://vault.example.com:8200",
        "auth": {
            "method": "approle",
            "role_id": "my-role-id",
            "secret_id_file": str(sid_file),
        },
    }


def _mock_hvac_client(secret_data: dict) -> MagicMock:
    """Return a mock hvac.Client that returns secret_data from KV v2 reads."""
    client = MagicMock()
    client.is_authenticated.return_value = True
    client.secrets.kv.v2.read_secret_version.return_value = {
        "data": {"data": secret_data}
    }
    return client


# ── read path ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_vault_backend_resolve(tmp_path):
    cfg = _vault_cfg(tmp_path)
    mock_client = _mock_hvac_client({"current": "supersecret"})

    backend = VaultBackend(cfg)
    with patch("hvac.Client", return_value=mock_client):
        result = await backend.resolve("vault://secret/data/agent-wake/src#current")

    assert result == b"supersecret"


@pytest.mark.asyncio
async def test_vault_backend_refresh(tmp_path):
    cfg = _vault_cfg(tmp_path)
    mock_client = _mock_hvac_client({"current": "rotated"})

    backend = VaultBackend(cfg)
    with patch("hvac.Client", return_value=mock_client):
        result = await backend.refresh("vault://secret/data/agent-wake/src#current")

    assert result == b"rotated"


@pytest.mark.asyncio
async def test_vault_backend_missing_key_raises(tmp_path):
    cfg = _vault_cfg(tmp_path)
    mock_client = _mock_hvac_client({"other_key": "value"})

    backend = VaultBackend(cfg)
    with patch("hvac.Client", return_value=mock_client):
        with pytest.raises(KeyError, match="current"):
            await backend.refresh("vault://secret/data/agent-wake/src#current")


@pytest.mark.asyncio
async def test_vault_backend_missing_fragment_raises(tmp_path):
    cfg = _vault_cfg(tmp_path)
    mock_client = _mock_hvac_client({"current": "val"})

    backend = VaultBackend(cfg)
    with patch("hvac.Client", return_value=mock_client):
        with pytest.raises(ValueError, match="fragment"):
            await backend.refresh("vault://secret/data/agent-wake/src")


# ── AppRole login retry on token expiry ───────────────────────────────────────

@pytest.mark.asyncio
async def test_vault_backend_relogin_on_auth_error(tmp_path):
    """On Forbidden, backend re-logs-in and retries once."""
    import hvac.exceptions

    cfg = _vault_cfg(tmp_path)

    fresh_client = _mock_hvac_client({"current": "after-relogin"})
    expired_client = MagicMock()
    expired_client.is_authenticated.return_value = True
    expired_client.secrets.kv.v2.read_secret_version.side_effect = (
        hvac.exceptions.Forbidden("token expired")
    )

    call_count = 0

    def make_client(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return expired_client if call_count == 1 else fresh_client

    backend = VaultBackend(cfg)
    with patch("hvac.Client", side_effect=make_client):
        result = await backend.refresh("vault://secret/data/foo#current")

    assert result == b"after-relogin"
    assert call_count == 2  # initial login + re-login


# ── TTL via resolver ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolver_vault_ttl_default(tmp_path, monkeypatch):
    """vault:// URIs get a 15-min TTL by default (not infinity)."""
    cfg = _vault_cfg(tmp_path)
    mock_client = _mock_hvac_client({"key": "val"})

    backend = VaultBackend(cfg)

    with patch("hvac.Client", return_value=mock_client):
        resolver = SecretResolver(backends=[backend])
        uri = "vault://secret/data/foo#key"
        await resolver.resolve(uri)

    # The cache entry should have an expiry roughly 15 min from now (not inf).
    cached_entry = resolver._cache.get(uri)
    assert cached_entry is not None
    _, expires_at = cached_entry
    import math
    assert expires_at != math.inf
    assert expires_at > time.monotonic() + 800  # at least 800s remaining


@pytest.mark.asyncio
async def test_resolver_vault_ttl_override(tmp_path):
    """?ttl= override is respected."""
    cfg = _vault_cfg(tmp_path)
    mock_client = _mock_hvac_client({"key": "val"})

    backend = VaultBackend(cfg)
    uri = "vault://secret/data/foo#key?ttl=30s"

    with patch("hvac.Client", return_value=mock_client):
        resolver = SecretResolver(backends=[backend])
        await resolver.resolve(uri)

    _, expires_at = resolver._cache[uri]
    import math
    assert expires_at != math.inf
    # Should expire roughly 30s from now (not 900s)
    assert expires_at < time.monotonic() + 60


# ── SIGHUP-triggered refresh ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolver_refresh_all_hits_vault(tmp_path):
    """refresh_all re-fetches vault URIs from the server."""
    cfg = _vault_cfg(tmp_path)
    responses = [{"key": "v1"}, {"key": "v2"}]
    call_count = 0

    def read_kv(**kwargs):
        nonlocal call_count
        data = responses[min(call_count, len(responses) - 1)]
        call_count += 1
        return {"data": {"data": data}}

    mock_client = MagicMock()
    mock_client.is_authenticated.return_value = True
    mock_client.secrets.kv.v2.read_secret_version.side_effect = read_kv

    backend = VaultBackend(cfg)
    uri = "vault://secret/data/foo#key"

    with patch("hvac.Client", return_value=mock_client):
        resolver = SecretResolver(backends=[backend])
        r1 = await resolver.resolve(uri)
        await resolver.refresh_all()
        r2 = await resolver.resolve(uri)

    assert r1 == b"v1"
    assert r2 == b"v2"
