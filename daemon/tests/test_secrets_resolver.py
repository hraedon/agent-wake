"""Tests for the secret resolver and env backend.

Covers: cache hit/miss, refresh, missing env var error, multi-URI resolve_all,
any-of HMAC verification with two valid secrets and one invalid signature.
"""

import asyncio
import hashlib
import hmac
import json
import time

import pytest

from aiohttp.test_utils import TestClient, TestServer

from agent_waked.gating import verify_signature, verify_signature_any
from agent_waked.ingest import create_ingest_app
from agent_waked.secrets.env import EnvBackend
from agent_waked.secrets.resolver import SecretResolver, _parse_duration


# ── _parse_duration ───────────────────────────────────────────────────────────

def test_parse_duration_seconds():
    assert _parse_duration("30s") == 30.0


def test_parse_duration_minutes():
    assert _parse_duration("5m") == 300.0


def test_parse_duration_hours():
    assert _parse_duration("1h") == 3600.0


def test_parse_duration_invalid():
    with pytest.raises(ValueError, match="Cannot parse duration"):
        _parse_duration("10d")


# ── EnvBackend ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_env_backend_resolve(monkeypatch):
    monkeypatch.setenv("TEST_SECRET_X", "myvalue")
    backend = EnvBackend()
    result = await backend.resolve("env://TEST_SECRET_X")
    assert result == b"myvalue"


@pytest.mark.asyncio
async def test_env_backend_cache_hit(monkeypatch):
    monkeypatch.setenv("TEST_SECRET_CACHE", "cached")
    backend = EnvBackend()
    first = await backend.resolve("env://TEST_SECRET_CACHE")
    # Change env after first resolve — cache should return old value.
    monkeypatch.setenv("TEST_SECRET_CACHE", "changed")
    second = await backend.resolve("env://TEST_SECRET_CACHE")
    assert first == second == b"cached"


@pytest.mark.asyncio
async def test_env_backend_refresh_bypasses_cache(monkeypatch):
    monkeypatch.setenv("TEST_SECRET_REFRESH", "original")
    backend = EnvBackend()
    await backend.resolve("env://TEST_SECRET_REFRESH")
    monkeypatch.setenv("TEST_SECRET_REFRESH", "updated")
    result = await backend.refresh("env://TEST_SECRET_REFRESH")
    assert result == b"updated"


@pytest.mark.asyncio
async def test_env_backend_missing_var_raises(monkeypatch):
    monkeypatch.delenv("TEST_MISSING_VAR", raising=False)
    backend = EnvBackend()
    with pytest.raises(RuntimeError, match="not set"):
        await backend.resolve("env://TEST_MISSING_VAR")


def test_env_backend_supports():
    b = EnvBackend()
    assert b.supports("env") is True
    assert b.supports("vault") is False


# ── SecretResolver ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolver_env_resolve(monkeypatch):
    monkeypatch.setenv("RES_SECRET_1", "hello")
    r = SecretResolver()
    result = await r.resolve("env://RES_SECRET_1")
    assert result == b"hello"


@pytest.mark.asyncio
async def test_resolver_env_cached_forever(monkeypatch):
    monkeypatch.setenv("RES_CACHE_SEC", "stable")
    r = SecretResolver()
    await r.resolve("env://RES_CACHE_SEC")
    monkeypatch.setenv("RES_CACHE_SEC", "changed")
    # Should still be cached
    result = await r.resolve("env://RES_CACHE_SEC")
    assert result == b"stable"


@pytest.mark.asyncio
async def test_resolver_refresh_all(monkeypatch):
    monkeypatch.setenv("RES_RA_SEC", "v1")
    r = SecretResolver()
    await r.resolve("env://RES_RA_SEC")
    monkeypatch.setenv("RES_RA_SEC", "v2")
    await r.refresh_all()
    result = await r.resolve("env://RES_RA_SEC")
    assert result == b"v2"


@pytest.mark.asyncio
async def test_resolver_resolve_all_single(monkeypatch):
    monkeypatch.setenv("RA_SINGLE", "secret123")
    r = SecretResolver()
    source_cfg = {"secret_uris": ["env://RA_SINGLE"]}
    results = await r.resolve_all(source_cfg)
    assert results == [b"secret123"]


@pytest.mark.asyncio
async def test_resolver_resolve_all_multiple(monkeypatch):
    monkeypatch.setenv("RA_CUR", "current")
    monkeypatch.setenv("RA_PRV", "previous")
    r = SecretResolver()
    source_cfg = {"secret_uris": ["env://RA_CUR", "env://RA_PRV"]}
    results = await r.resolve_all(source_cfg)
    assert results == [b"current", b"previous"]


@pytest.mark.asyncio
async def test_resolver_unknown_scheme_raises():
    r = SecretResolver()
    with pytest.raises(RuntimeError, match="No backend supports URI scheme"):
        await r.resolve("s3://bucket/key")


@pytest.mark.asyncio
async def test_resolver_vault_no_backend_raises():
    """vault:// URI with no vault backend raises clear error."""
    r = SecretResolver(backends=[EnvBackend()])  # no vault backend
    with pytest.raises(RuntimeError, match="vault backend"):
        await r.resolve("vault://secret/data/foo#key")


# ── verify_signature_any ──────────────────────────────────────────────────────

def _sig(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def test_verify_signature_any_first_matches():
    body = b'{"x":1}'
    s1, s2 = b"secret1", b"secret2"
    assert verify_signature_any(body, [s1, s2], _sig(s1, body)) is True


def test_verify_signature_any_second_matches():
    body = b'{"x":1}'
    s1, s2 = b"secret1", b"secret2"
    assert verify_signature_any(body, [s1, s2], _sig(s2, body)) is True


def test_verify_signature_any_none_match():
    body = b'{"x":1}'
    s1, s2 = b"secret1", b"secret2"
    assert verify_signature_any(body, [s1, s2], _sig(b"other", body)) is False


def test_verify_signature_any_empty_list():
    body = b'{"x":1}'
    assert verify_signature_any(body, [], _sig(b"s", body)) is False


# ── Integration: resolver wired into ingest any-of HMAC ───────────────────────

def _hmac(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


@pytest.fixture
async def two_secret_client(monkeypatch):
    """Ingest app with two env-backed secrets (rotation window)."""
    monkeypatch.setenv("INGEST_SEC_CUR", "current-secret")
    monkeypatch.setenv("INGEST_SEC_PRV", "previous-secret")
    resolver = SecretResolver()
    config = {
        "sources": {
            "test": {
                "secret_uris": ["env://INGEST_SEC_CUR", "env://INGEST_SEC_PRV"],
                "callback_url": None,
            }
        },
        "routing": {},
    }

    class MockRouter:
        delivered: list = []
        async def deliver(self, event):  # type: ignore[override]
            self.delivered.append(event)
            return "queued"

    router = MockRouter()
    app = create_ingest_app(config, router, resolver=resolver)  # type: ignore[arg-type]
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    yield cli, router
    await cli.close()


@pytest.mark.asyncio
async def test_ingest_current_secret_accepted(two_secret_client):
    cli, router = two_secret_client
    body = json.dumps({"v": 0, "event_id": "x1", "source": "test",
                       "kind": "alert", "content": "hi", "meta": {}, "wake": True}).encode()
    resp = await cli.post("/", data=body, headers={
        "X-AgentWake-Source": "test",
        "X-AgentWake-Signature": _hmac(b"current-secret", body),
    })
    assert resp.status == 202


@pytest.mark.asyncio
async def test_ingest_previous_secret_accepted(two_secret_client):
    cli, router = two_secret_client
    body = json.dumps({"v": 0, "event_id": "x2", "source": "test",
                       "kind": "alert", "content": "hi", "meta": {}, "wake": True}).encode()
    resp = await cli.post("/", data=body, headers={
        "X-AgentWake-Source": "test",
        "X-AgentWake-Signature": _hmac(b"previous-secret", body),
    })
    assert resp.status == 202


@pytest.mark.asyncio
async def test_ingest_unknown_secret_rejected(two_secret_client):
    cli, router = two_secret_client
    body = json.dumps({"v": 0, "event_id": "x3", "source": "test",
                       "kind": "alert", "content": "hi", "meta": {}, "wake": True}).encode()
    resp = await cli.post("/", data=body, headers={
        "X-AgentWake-Source": "test",
        "X-AgentWake-Signature": _hmac(b"wrong-secret", body),
    })
    assert resp.status == 403
