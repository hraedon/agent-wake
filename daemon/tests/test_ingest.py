"""Tests for agent_waked.ingest — HTTP ingest with gating, dedupe, routing."""

import asyncio
import hashlib
import hmac
import json
import logging

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent_waked.ingest import SourceRateLimiter, create_ingest_app


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

    def subscriber_health(self):
        return {
            "connected": 1,
            "connected_adapters": ["claude"],
            "by_source": {
                "test": {"subscribers": 1, "adapters": ["claude"], "oldest_age_seconds": 0.0},
                "other": {"subscribers": 1, "adapters": ["claude"], "oldest_age_seconds": 0.0},
            },
            "live_only_sources": ["other", "test"],
            "live_only_without_subscribers": [],
        }

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
async def test_per_source_rate_limit_returns_429_with_retry_after():
    config = _config()
    config["wake"] = {"ingest_rate_limit": 1.0, "ingest_rate_burst": 1}
    router = MockRouter()
    cli = TestClient(TestServer(create_ingest_app(config, router)))
    await cli.start_server()
    try:
        first_body = json.dumps({
            "v": 0,
            "event_id": "rate-1",
            "source": "test",
            "kind": "alert",
            "content": "first",
            "meta": {},
            "wake": True,
        }).encode()
        second_body = json.dumps({
            "v": 0,
            "event_id": "rate-2",
            "source": "test",
            "kind": "alert",
            "content": "second",
            "meta": {},
            "wake": True,
        }).encode()

        first = await _make_request(cli, first_body)
        second = await _make_request(cli, second_body)

        assert first.status == 202
        assert second.status == 429
        assert int(second.headers["Retry-After"]) >= 1
        assert await second.json() == {"error": "rate limit exceeded"}
    finally:
        await cli.close()


def test_rate_limit_bucket_refills_over_time():
    now = [0.0]
    limiter = SourceRateLimiter(2.0, 1, clock=lambda: now[0])

    assert limiter.allow("source-a") == (True, 0.0)
    allowed, retry_after = limiter.allow("source-a")
    assert allowed is False
    assert retry_after == pytest.approx(0.5)

    now[0] = 0.5
    assert limiter.allow("source-a") == (True, 0.0)


def test_rate_limit_buckets_are_independent_per_source():
    limiter = SourceRateLimiter(1.0, 1, clock=lambda: 0.0)

    assert limiter.allow("source-a")[0] is True
    assert limiter.allow("source-a")[0] is False
    assert limiter.allow("source-b")[0] is True


@pytest.mark.asyncio
async def test_unsigned_flood_cannot_starve_signed_sender(monkeypatch):
    """WI-008: an unsigned flood claiming a known source must not exhaust that
    source's bucket and 429 a legitimate signed sender on a different peer.

    Before the fix the pre-signature limiter was keyed on the (spoofable)
    source header, so the attacker's bogus-signature requests charged the same
    "test" bucket the signed sender uses; the signed sender would then be 429'd
    by traffic it never sent. Now unsigned traffic is bounded by remote address
    and only the verified sender identity keys the source bucket, so the flood
    stays in the attacker's own per-peer bucket.
    """
    import agent_waked.ingest as ingest_mod

    config = _config()
    config["wake"] = {"ingest_rate_limit": 1.0, "ingest_rate_burst": 2}
    router = MockRouter()
    app = create_ingest_app(config, router)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        peer = {"addr": "10.0.0.9"}
        monkeypatch.setattr(ingest_mod, "_client_address", lambda request: peer["addr"])

        flood_body = json.dumps({
            "v": 0, "event_id": "flood", "source": "test", "kind": "alert",
            "content": "x", "meta": {}, "wake": True,
        }).encode()
        for _ in range(4):
            resp = await _make_request(cli, flood_body, source="test", sig="sha256=bad")
            assert resp.status in (403, 429)

        peer["addr"] = "10.0.0.10"
        legit_body = json.dumps({
            "v": 0, "event_id": "legit-1", "source": "test", "kind": "alert",
            "content": "real", "meta": {}, "wake": True,
        }).encode()
        resp = await _make_request(cli, legit_body, source="test")
        assert resp.status == 202, await resp.json()
        assert (await resp.json())["status"] == "queued"
    finally:
        await cli.close()


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


# ── the delivery task's done-callback and shutdown drain (WI-001) ─────


def _delivery_config(target: str = "operator") -> dict:
    return {
        "sources": {
            "test": {
                "secret": b"shhh",
                "callback_url": None,
                "allowed_target_principals": [target],
            },
        },
        "delivery": {
            target: {"webhook": {
                "url": "https://hooks.example.com/inbox",
                "secret_uri": "env://WH",
            }},
        },
        "routing": {},
    }


async def _post_delivery_event(cli, event_id: str = "evt-del-1") -> None:
    body = _delivery_event("operator", event_id=event_id)
    sig = "sha256=" + hmac.new(b"shhh", body, hashlib.sha256).hexdigest()
    resp = await cli.post("/", data=body, headers={
        "X-AgentWake-Source": "test",
        "X-AgentWake-Signature": sig,
    })
    assert resp.status == 202


@pytest.mark.asyncio
async def test_delivery_task_crash_is_logged_with_a_traceback(caplog):
    """A crashing delivery task must leave a traceback, not just a message.

    The callback runs outside any ``except`` block, so ``log.exception`` there
    records ``NoneType: None`` — the traceback has to be passed explicitly.
    """
    from agent_waked.delivery import HumanDelivery

    config = _delivery_config()
    delivery = HumanDelivery(config, _FakeResolver())

    async def _boom(event):
        raise RuntimeError("delivery exploded")

    delivery.deliver = _boom  # type: ignore[method-assign]

    app = create_ingest_app(config, MockRouter(), delivery=delivery)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        with caplog.at_level(logging.ERROR, logger="agent_waked.ingest"):
            await _post_delivery_event(cli)
            await asyncio.sleep(0.05)
        records = [
            r for r in caplog.records
            if "human delivery task crashed" in r.getMessage()
        ]
        assert len(records) == 1
        assert records[0].exc_info is not None
        assert isinstance(records[0].exc_info[1], RuntimeError)
        assert "delivery exploded" in caplog.text
        assert "RuntimeError" in caplog.text  # the traceback made it out
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_delivery_task_cancel_does_not_raise_from_the_callback(caplog):
    """``task.exception()`` raises on a cancelled task — the guard is load-bearing.

    Before the ``task.cancelled()`` check, every shutdown with a delivery in
    flight raised CancelledError out of the done-callback and into the loop's
    exception handler.
    """
    from agent_waked.delivery import HumanDelivery

    config = _delivery_config()
    delivery = HumanDelivery(config, _FakeResolver())
    started = asyncio.Event()

    async def _hang(event):
        started.set()
        await asyncio.sleep(30)
        return {"status": "delivered"}

    delivery.deliver = _hang  # type: ignore[method-assign]

    loop_errors: list[dict] = []
    asyncio.get_running_loop().set_exception_handler(
        lambda loop, context: loop_errors.append(context)
    )

    app = create_ingest_app(config, MockRouter(), delivery=delivery)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        with caplog.at_level(logging.WARNING, logger="agent_waked.ingest"):
            await _post_delivery_event(cli)
            await asyncio.wait_for(started.wait(), timeout=2)
            # This is what shutdown does. cleanup(), not shutdown(): the drain
            # is on on_cleanup, because on_shutdown fires before aiohttp waits
            # for in-flight request handlers.
            await app.cleanup()
        assert "cancelled" in caplog.text
        assert loop_errors == []
    finally:
        asyncio.get_running_loop().set_exception_handler(None)
        await cli.close()


@pytest.mark.asyncio
async def test_shutdown_drains_a_finishing_delivery():
    """A delivery that completes inside the grace period is not cancelled."""
    from agent_waked.delivery import HumanDelivery

    config = _delivery_config()
    delivery = HumanDelivery(config, _FakeResolver())
    finished: list[str] = []
    started = asyncio.Event()

    async def _slow(event):
        started.set()
        await asyncio.sleep(0.1)
        finished.append(event["event_id"])
        return {"status": "delivered"}

    delivery.deliver = _slow  # type: ignore[method-assign]

    app = create_ingest_app(config, MockRouter(), delivery=delivery)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        await _post_delivery_event(cli)
        await asyncio.wait_for(started.wait(), timeout=2)
        await app.cleanup()
        assert finished == ["evt-del-1"]
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_shutdown_cancels_a_stuck_delivery_and_it_is_dead_lettered(tmp_path):
    """The whole point of the drain: an alert stuck in backoff still lands.

    A real HumanDelivery (not a stub) with a channel that hangs, so the
    cancellation arm of ``deliver`` runs against a still-open store — which is
    what the daemon's shutdown ordering guarantees.
    """
    from agent_waked.delivery import HumanDelivery
    from agent_waked.ingest import DELIVERY_DRAIN_TIMEOUT
    from agent_waked.store import WakeStore

    class HangingChannel:
        name = "webhook"

        def __init__(self):
            self.started = asyncio.Event()

        async def deliver(self, event, channel_cfg, resolver):
            self.started.set()
            await asyncio.sleep(60)  # as a webhook does in 16s backoff
            return {"status": "failed"}

        async def close(self):
            return None

    config = _delivery_config()
    channel = HangingChannel()
    store = WakeStore(tmp_path / "state.db")
    try:
        delivery = HumanDelivery(
            config, _FakeResolver(), channels={"webhook": channel}, store=store
        )
        app = create_ingest_app(config, MockRouter(), delivery=delivery, store=store)
        cli = TestClient(TestServer(app))
        await cli.start_server()
        try:
            await _post_delivery_event(cli)
            await asyncio.wait_for(channel.started.wait(), timeout=2)
            await asyncio.wait_for(
                app.cleanup(), timeout=DELIVERY_DRAIN_TIMEOUT + 5
            )
            entries = store.list_dead_letters(kind="human_delivery")
            assert len(entries) == 1
            assert "cancelled" in (entries[0].error or "")
            assert entries[0].ref_id == "evt-del-1"
        finally:
            await cli.close()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_shutdown_with_no_deliveries_is_a_no_op():
    config = _delivery_config()
    app = create_ingest_app(config, MockRouter())
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        await app.cleanup()
    finally:
        await cli.close()


def test_the_drain_is_on_on_cleanup_not_on_shutdown():
    """The hook choice is the whole fix; assert it directly.

    ``BaseRunner.cleanup()`` fires ``on_shutdown`` BEFORE it waits for in-flight
    request handlers (``await self.shutdown()`` precedes
    ``await self._server.shutdown(timeout)``), and ``post_root`` registers its
    delivery task after two awaits. A drain on ``on_shutdown`` therefore runs
    against an incomplete task set. Moving it back would silently reopen a lost
    alert with a 202 response, which no other test in this file would catch.
    """
    app = create_ingest_app(_delivery_config(), MockRouter())
    cleanup_names = [getattr(h, "__name__", "") for h in app.on_cleanup]
    shutdown_names = [getattr(h, "__name__", "") for h in app.on_shutdown]
    assert "_drain_delivery_tasks" in cleanup_names
    assert "_drain_delivery_tasks" not in shutdown_names


@pytest.mark.asyncio
async def test_a_task_registered_during_the_drain_is_still_covered():
    """The drain re-checks instead of returning on the first empty set."""
    from agent_waked.ingest import DELIVERY_TASKS_KEY

    config = _delivery_config()
    app = create_ingest_app(config, MockRouter())
    # on_cleanup only fires on a *frozen* app, which is what AppRunner /
    # TestServer setup does — an unstarted app silently skips the signal.
    cli = TestClient(TestServer(app))
    await cli.start_server()
    tasks = app[DELIVERY_TASKS_KEY]
    finished: list[str] = []

    async def _late() -> None:
        await asyncio.sleep(0.2)
        finished.append("late")

    async def _early() -> None:
        # Registers a second task while the drain is already awaiting the first.
        await asyncio.sleep(0.05)
        t = asyncio.ensure_future(_late())
        tasks.add(t)
        t.add_done_callback(tasks.discard)
        finished.append("early")

    try:
        first = asyncio.ensure_future(_early())
        tasks.add(first)
        first.add_done_callback(tasks.discard)

        await app.cleanup()
        assert finished == ["early", "late"]
        assert not [t for t in tasks if not t.done()]
    finally:
        await cli.close()
