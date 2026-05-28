"""Tests for agent_waked.ingest — HTTP ingest with gating, dedupe, routing."""

import hashlib
import hmac
import json

import pytest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent_waked.ingest import create_ingest_app


def _hmac(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def _config():
    return {
        "sources": {
            "test": {"secret": b"shhh", "callback_url": None},
            "other": {"secret": b"other-secret", "callback_url": None},
        },
        "routing": {},
    }


class MockRouter:
    def __init__(self, result="queued"):
        self.delivered = []
        self._result = result

    def accepted_sources_for(self, adapter, requested):
        return requested

    async def deliver(self, event):
        self.delivered.append(event)
        return self._result


@pytest.fixture
def config():
    return _config()


@pytest.fixture
def router():
    return MockRouter()


@pytest.fixture
async def client(config, router):
    app = create_ingest_app(config, router)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    yield cli
    await cli.close()


def _make_request(cli, body, source="test", sig=None, event_id=None):
    secret = _config()["sources"].get(source, {}).get("secret", b"")
    if sig is None:
        sig = _hmac(secret, body)
    headers = {
        "X-AgentWake-Source": source,
        "X-AgentWake-Signature": sig,
    }
    if event_id:
        headers["X-AgentWake-Event-Id"] = event_id
    return cli.post("/", data=body, headers=headers)


# ── happy path ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_post_v0_event_queued(client, router):
    body = json.dumps({
        "v": 0, "event_id": "evt-001", "source": "test",
        "kind": "alert", "content": "hi", "meta": {}, "wake": True,
    }).encode()
    resp = await _make_request(client, body)
    assert resp.status == 202
    data = await resp.json()
    assert data["status"] == "queued"
    assert data["event_id"] == "evt-001"
    assert len(router.delivered) == 1
    assert router.delivered[0]["content"] == "hi"


@pytest.mark.asyncio
async def test_post_duplicate_event(client, router):
    body = json.dumps({
        "v": 0, "event_id": "evt-dup", "source": "test",
        "kind": "alert", "content": "hi", "meta": {}, "wake": True,
    }).encode()
    resp1 = await _make_request(client, body)
    data1 = await resp1.json()
    assert resp1.status == 202
    assert data1["status"] == "queued"

    resp2 = await _make_request(client, body)
    data2 = await resp2.json()
    assert resp2.status == 202
    assert data2["status"] == "duplicate"
    assert data2["event_id"] == "evt-dup"
    assert len(router.delivered) == 1


@pytest.mark.asyncio
async def test_post_wrapped_payload(client, router):
    raw = json.dumps({"action": "opened", "number": 42}).encode()
    resp = await _make_request(client, raw, event_id="evt-wrap-001")
    assert resp.status == 202
    data = await resp.json()
    assert data["status"] == "queued"
    assert data["event_id"] == "evt-wrap-001"
    assert len(router.delivered) == 1
    ev = router.delivered[0]
    assert ev["kind"] == "webhook"
    assert ev["source"] == "test"
    assert ev["meta"] == {}


# ── 403 unknown source ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_source_403(client, router):
    body = b'{"v":0,"event_id":"x","source":"nope"}'
    resp = await _make_request(client, body, source="nope", sig="sha256=deadbeef")
    assert resp.status == 403
    assert len(router.delivered) == 0


# ── 403 bad signature ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_invalid_signature_403(client, router):
    body = json.dumps({"v": 0, "event_id": "evt-sig", "source": "test", "kind": "alert", "content": "x"}).encode()
    resp = await _make_request(client, body, sig="sha256=deadbeef")
    assert resp.status == 403


# ── 403 source mismatch ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_source_spoofing_403(client, router):
    body = json.dumps({
        "v": 0, "event_id": "spoof-001", "source": "other",
        "kind": "alert", "content": "pwn", "meta": {}, "wake": True,
    }).encode()
    resp = await _make_request(client, body)
    assert resp.status == 403
    data = await resp.json()
    assert data == {"error": "unknown source or invalid signature"}
    assert "other" not in json.dumps(data)
    assert len(router.delivered) == 0


# ── no_subscriber ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_subscriber_returns_202():
    config = _config()
    no_sub = MockRouter(result="no_subscriber")
    app = create_ingest_app(config, no_sub)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()

    body = json.dumps({
        "v": 0, "event_id": "evt-nosub", "source": "test",
        "kind": "alert", "content": "hi", "meta": {}, "wake": True,
    }).encode()
    resp = await _make_request(cli, body)
    assert resp.status == 202
    data = await resp.json()
    assert data["status"] == "no_subscriber"
    await cli.close()


# ── 404 for other paths ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_other_path_404(client, router):
    resp = await client.get("/other")
    assert resp.status == 404


# ── identity stamping ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trigger_identity_stamped_in_meta():
    """When source has principal_id, it's stamped as trigger_identity in meta."""
    config = {
        "sources": {
            "test": {
                "secret": b"shhh",
                "callback_url": None,
                "principal_id": "key:sha256:abc123",
            },
        },
        "routing": {},
    }
    router = MockRouter()
    app = create_ingest_app(config, router)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()

    body = json.dumps({
        "v": 0, "event_id": "evt-id-001", "source": "test",
        "kind": "alert", "content": "hi", "meta": {}, "wake": True,
    }).encode()
    sig = "sha256=" + hmac.new(b"shhh", body, hashlib.sha256).hexdigest()
    resp = await cli.post("/", data=body, headers={
        "X-AgentWake-Source": "test",
        "X-AgentWake-Signature": sig,
    })
    assert resp.status == 202
    assert len(router.delivered) == 1
    assert router.delivered[0]["meta"]["trigger_identity"] == "key:sha256:abc123"
    await cli.close()


@pytest.mark.asyncio
async def test_no_principal_id_no_trigger_identity():
    """When source has no principal_id, trigger_identity is not added."""
    config = _config()
    router = MockRouter()
    app = create_ingest_app(config, router)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()

    body = json.dumps({
        "v": 0, "event_id": "evt-id-002", "source": "test",
        "kind": "alert", "content": "hi", "meta": {}, "wake": True,
    }).encode()
    sig = _hmac(b"shhh", body)
    resp = await cli.post("/", data=body, headers={
        "X-AgentWake-Source": "test",
        "X-AgentWake-Signature": sig,
    })
    assert resp.status == 202
    assert len(router.delivered) == 1
    assert "trigger_identity" not in router.delivered[0]["meta"]
    await cli.close()


# ── identity allowlist enforcement ──────────────────────────────────

@pytest.mark.asyncio
async def test_identity_allowlist_accepts_matching_sender():
    config = {
        "sources": {
            "test": {
                "secret": b"shhh",
                "callback_url": None,
                "allowed_trigger_identities": ["alice", "bob"],
            },
        },
        "routing": {},
    }
    router = MockRouter()
    app = create_ingest_app(config, router)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()

    body = json.dumps({
        "v": 0, "event_id": "evt-id-003", "source": "test",
        "kind": "alert", "content": "hi", "meta": {}, "wake": True,
    }).encode()
    sig = "sha256=" + hmac.new(b"shhh", body, hashlib.sha256).hexdigest()
    resp = await cli.post("/", data=body, headers={
        "X-AgentWake-Source": "test",
        "X-AgentWake-Signature": sig,
        "X-AgentWake-Identity": "alice",
    })
    assert resp.status == 202
    assert len(router.delivered) == 1
    await cli.close()


@pytest.mark.asyncio
async def test_identity_allowlist_rejects_unlisted_sender():
    config = {
        "sources": {
            "test": {
                "secret": b"shhh",
                "callback_url": None,
                "allowed_trigger_identities": ["alice", "bob"],
            },
        },
        "routing": {},
    }
    router = MockRouter()
    app = create_ingest_app(config, router)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()

    body = json.dumps({
        "v": 0, "event_id": "evt-id-004", "source": "test",
        "kind": "alert", "content": "hi", "meta": {}, "wake": True,
    }).encode()
    sig = "sha256=" + hmac.new(b"shhh", body, hashlib.sha256).hexdigest()
    resp = await cli.post("/", data=body, headers={
        "X-AgentWake-Source": "test",
        "X-AgentWake-Signature": sig,
        "X-AgentWake-Identity": "mallory",
    })
    assert resp.status == 403
    assert len(router.delivered) == 0
    await cli.close()


@pytest.mark.asyncio
async def test_identity_allowlist_rejects_missing_identity():
    config = {
        "sources": {
            "test": {
                "secret": b"shhh",
                "callback_url": None,
                "allowed_trigger_identities": ["alice"],
            },
        },
        "routing": {},
    }
    router = MockRouter()
    app = create_ingest_app(config, router)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()

    body = json.dumps({
        "v": 0, "event_id": "evt-id-005", "source": "test",
        "kind": "alert", "content": "hi", "meta": {}, "wake": True,
    }).encode()
    sig = "sha256=" + hmac.new(b"shhh", body, hashlib.sha256).hexdigest()
    resp = await cli.post("/", data=body, headers={
        "X-AgentWake-Source": "test",
        "X-AgentWake-Signature": sig,
    })
    assert resp.status == 403
    assert len(router.delivered) == 0
    await cli.close()
