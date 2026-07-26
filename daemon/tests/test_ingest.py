"""Tests for agent_waked.ingest — HTTP ingest with gating, dedupe, routing."""

import asyncio
import hashlib
import hmac
import json

import pytest
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


@pytest.mark.asyncio
async def test_unknown_version_event_rejected(client, router):
    """BC-010: an envelope with event_id but unknown `v` MUST be rejected (400)."""
    body = json.dumps({
        "v": 1, "event_id": "evt-v1", "source": "test",
        "kind": "alert", "content": "future", "meta": {}, "wake": True,
    }).encode()
    resp = await _make_request(client, body)
    assert resp.status == 400
    data = await resp.json()
    assert "unsupported" in data["error"]
    assert len(router.delivered) == 0


@pytest.mark.asyncio
async def test_unknown_version_without_event_id_rejected(client, router):
    """BC-010: unknown `v` MUST be rejected even without event_id (no silent wrap)."""
    body = json.dumps({"v": 1, "source": "test", "content": "future"}).encode()
    resp = await _make_request(client, body)
    assert resp.status == 400
    assert len(router.delivered) == 0


@pytest.mark.asyncio
async def test_version_false_rejected(client, router):
    """BC-010: ``v: false`` is not a valid version (bool sneaks past ``== 0``)."""
    body = json.dumps({
        "v": False, "event_id": "evt-vbool", "source": "test",
        "kind": "alert", "content": "x", "meta": {}, "wake": True,
    }).encode()
    resp = await _make_request(client, body)
    assert resp.status == 400
    assert len(router.delivered) == 0


@pytest.mark.asyncio
async def test_version_float_rejected(client, router):
    """BC-010: ``v: 0.0`` is not a valid version (float sneaks past ``== 0``)."""
    body = json.dumps({
        "v": 0.0, "event_id": "evt-vfloat", "source": "test",
        "kind": "alert", "content": "x", "meta": {}, "wake": True,
    }).encode()
    resp = await _make_request(client, body)
    assert resp.status == 400
    assert len(router.delivered) == 0


@pytest.mark.asyncio
async def test_unknown_version_event_string_rejected(client, router):
    """BC-010: non-integer `v` is also rejected, not wrapped."""
    body = json.dumps({
        "v": "next", "event_id": "evt-vstr", "source": "test",
        "kind": "alert", "content": "future", "meta": {}, "wake": True,
    }).encode()
    resp = await _make_request(client, body)
    assert resp.status == 400
    assert len(router.delivered) == 0


@pytest.mark.asyncio
async def test_envelope_without_v_still_accepted(client, router):
    """A bare event_id payload without `v` is treated as v0 (forward-compat wrap path)."""
    body = json.dumps({
        "event_id": "evt-nov", "source": "test",
        "kind": "alert", "content": "hi", "meta": {}, "wake": True,
    }).encode()
    resp = await _make_request(client, body)
    assert resp.status == 202
    assert len(router.delivered) == 1


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
    body = json.dumps(
        {"v": 0, "event_id": "evt-sig", "source": "test", "kind": "alert", "content": "x"}
    ).encode()
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


# ── human-directed delivery integration (Plan 005) ───────────────────


class _FakeResolver:
    async def resolve(self, uri: str) -> bytes:
        return b"k"


def _delivery_event(target: str, event_id: str = "evt-del-1") -> bytes:
    return json.dumps({
        "v": 0, "event_id": event_id, "source": "test",
        "kind": "alert", "content": "hi",
        "meta": {"target": target}, "wake": True,
    }).encode()


@pytest.mark.asyncio
async def test_delivery_to_authorized_principal_dispatches():
    """A source with allowed_target_principals dispatches to a known principal."""
    from agent_waked.delivery import HumanDelivery

    config = {
        "sources": {
            "test": {
                "secret": b"shhh",
                "callback_url": None,
                "allowed_target_principals": ["operator"],
            },
        },
        "delivery": {
            "operator": {"webhook": {
                "url": "https://hooks.example.com/inbox",
                "secret_uri": "env://WH",
            }},
        },
        "routing": {},
    }
    router = MockRouter()
    delivery = HumanDelivery(config, _FakeResolver())
    delivered: list[dict] = []

    async def _record(event):
        delivered.append(event)
        return {"status": "delivered"}

    delivery.deliver = _record  # type: ignore[method-assign]

    app = create_ingest_app(config, router, delivery=delivery)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    try:
        body = _delivery_event("operator")
        sig = "sha256=" + hmac.new(b"shhh", body, hashlib.sha256).hexdigest()
        resp = await cli.post("/", data=body, headers={
            "X-AgentWake-Source": "test",
            "X-AgentWake-Signature": sig,
        })
        assert resp.status == 202
        data = await resp.json()
        assert data["delivery"]["status"] == "dispatched"
        assert data["delivery"]["principal_id"] == "operator"
        # The fire-and-forget deliver task was scheduled; let it run.
        await asyncio.sleep(0.05)
        assert len(delivered) == 1
        assert delivered[0]["meta"]["target"] == "operator"
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_delivery_unknown_principal_422():
    """An event targeting an unknown principal is rejected with 422."""
    config = {
        "sources": {
            "test": {
                "secret": b"shhh",
                "callback_url": None,
                "allowed_target_principals": ["operator"],
            },
        },
        "delivery": {"operator": {"webhook": {
            "url": "https://hooks.example.com/inbox", "secret_uri": "env://WH",
        }}},
        "routing": {},
    }
    router = MockRouter()
    from agent_waked.delivery import HumanDelivery
    delivery = HumanDelivery(config, _FakeResolver())
    app = create_ingest_app(config, router, delivery=delivery)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    try:
        body = _delivery_event("nobody")
        sig = "sha256=" + hmac.new(b"shhh", body, hashlib.sha256).hexdigest()
        resp = await cli.post("/", data=body, headers={
            "X-AgentWake-Source": "test",
            "X-AgentWake-Signature": sig,
        })
        assert resp.status == 422
        data = await resp.json()
        assert data["error"] == "unknown principal"
        assert data["principal_id"] == "nobody"
        assert len(router.delivered) == 0
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_delivery_unauthorized_source_403():
    """A source without allowed_target_principals cannot deliver (default-deny)."""
    config = {
        "sources": {
            "test": {"secret": b"shhh", "callback_url": None},
        },
        "delivery": {"operator": {"webhook": {
            "url": "https://hooks.example.com/inbox", "secret_uri": "env://WH",
        }}},
        "routing": {},
    }
    router = MockRouter()
    from agent_waked.delivery import HumanDelivery
    delivery = HumanDelivery(config, _FakeResolver())
    delivered: list[dict] = []

    async def _record(event):
        delivered.append(event)
        return {"status": "delivered"}

    delivery.deliver = _record  # type: ignore[method-assign]
    app = create_ingest_app(config, router, delivery=delivery)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    try:
        body = _delivery_event("operator")
        sig = "sha256=" + hmac.new(b"shhh", body, hashlib.sha256).hexdigest()
        resp = await cli.post("/", data=body, headers={
            "X-AgentWake-Source": "test",
            "X-AgentWake-Signature": sig,
        })
        assert resp.status == 403
        data = await resp.json()
        assert data["error"] == "source not authorized to deliver to principal"
        assert data["principal_id"] == "operator"
        assert len(delivered) == 0
    finally:
        await cli.close()


# ── durable dedupe across a daemon restart (BC-WAKE-004) ─────────────────────


def _wake_body(event_id: str) -> bytes:
    return json.dumps(
        {
            "v": 0,
            "event_id": event_id,
            "source": "test",
            "kind": "webhook",
            "content": "payload",
            "meta": {},
            "wake": True,
        }
    ).encode()


async def _post_once(store, body: bytes) -> dict:
    """Start a fresh ingest app against *store*, POST *body*, tear it down.

    Each call stands in for one daemon lifetime: a brand-new app, router and
    Dedupe over the same on-disk store.
    """
    app = create_ingest_app(_config(), MockRouter(), store=store)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    try:
        resp = await cli.post(
            "/",
            data=body,
            headers={
                "X-AgentWake-Source": "test",
                "X-AgentWake-Signature": _hmac(b"shhh", body),
            },
        )
        assert resp.status == 202
        return await resp.json()
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_duplicate_rejected_across_daemon_restart(tmp_path):
    """AC (WI-A): a replay is rejected even after the daemon process dies.

    Before BC-WAKE-004 was closed, ``Dedupe`` was an in-memory FIFO, so the
    second POST here — issued against a fresh app after the store was closed
    and reopened — was admitted as brand new.
    """
    from agent_waked.store import WakeStore

    db = tmp_path / "state.db"
    body = _wake_body("ev-replay-1")

    store = WakeStore(db)
    first = await _post_once(store, body)
    assert first["status"] == "queued"
    store.close()  # daemon dies

    store2 = WakeStore(db)  # daemon restarts, same state file
    try:
        second = await _post_once(store2, body)
        assert second["status"] == "duplicate"
        assert second["event_id"] == "ev-replay-1"
    finally:
        store2.close()


@pytest.mark.asyncio
async def test_in_memory_dedupe_is_lost_across_restart():
    """The documented v0 fallback: no store means no durability.

    This is the control for the test above — it pins *why* the store is
    required rather than leaving the durable path untestable.
    """
    body = _wake_body("ev-replay-2")
    first = await _post_once(None, body)
    assert first["status"] == "queued"
    second = await _post_once(None, body)
    assert second["status"] == "queued"


@pytest.mark.asyncio
async def test_health_reports_store_counters(tmp_path):
    from agent_waked.store import WakeStore

    store = WakeStore(tmp_path / "state.db")
    store.dead_letter(kind="reply", source="test", ref_id="r1", payload={})
    app = create_ingest_app(_config(), MockRouter(), store=store)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    try:
        resp = await cli.get("/")
        body = await resp.json()
        assert body["store"] == {"durable": True, "pending": 0, "dead_letter": 1}
    finally:
        await cli.close()
        store.close()
