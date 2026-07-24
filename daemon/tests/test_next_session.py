"""Tests for next-session delivery (WI-C / Plan 006 Phase 1, BC-WAKE-012).

An event addressed to a source with no live session is durably queued and
handed to the next session that subscribes for that source. Exercised at two
levels:

* router level — enqueue on ``no_subscriber``, drain on ``drain_pending``;
* daemon level — a real unix socket, a real ``hello`` handshake, and a real
  HTTP ingest, with the "session" arriving *after* the event.
"""

import asyncio
import hashlib
import hmac
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent_waked.ingest import create_ingest_app
from agent_waked.proto import encode_frame
from agent_waked.router import Router
from agent_waked.socket_server import SocketServer
from agent_waked.store import WakeStore


async def _settle(store, expected=0, tries=100):
    """Yield to the loop until the ack callbacks have settled the queue."""
    for _ in range(tries):
        if store.pending_count() == expected:
            return
        await asyncio.sleep(0.01)


def _config(default_delivery=None):
    cfg = {
        "sources": {
            "github-actions": {"secret": b"k1"},
            "telegram-bot": {"secret": b"k2"},
        },
        "routing": {},
    }
    if default_delivery:
        cfg["state"] = {"default_delivery": default_delivery}
    return cfg


def _event(event_id="ev-1", source="github-actions", delivery="next_session"):
    meta = {}
    if delivery is not None:
        meta["delivery"] = delivery
    return {
        "v": 0,
        "event_id": event_id,
        "source": source,
        "kind": "webhook",
        "content": "build failed",
        "meta": meta,
        "wake": True,
    }


class MockConnection:
    """Adapter stand-in that records frames and can auto-ack."""

    def __init__(self, router=None, auto_ack=True, fail=False):
        self.sent: list[dict] = []
        self._router = router
        self._auto_ack = auto_ack
        self._fail = fail

    async def send_frame(self, frame):
        if self._fail:
            raise OSError("broken pipe")
        self.sent.append(frame)
        if self._router and self._auto_ack and frame.get("type") == "wake":
            self._router.resolve_ack(frame.get("ack_id", ""), "ack")

    def close(self):
        pass


@pytest.fixture
def store(tmp_path):
    s = WakeStore(tmp_path / "state.db")
    yield s
    s.close()


# ── router level ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_session_queues_when_delivery_requested(store):
    router = Router(_config(), store=store)
    result = await router.deliver(_event())
    assert result == "queued_next_session"
    assert [p.event_id for p in store.list_pending()] == ["ev-1"]


@pytest.mark.asyncio
async def test_no_session_drops_by_default(store):
    """Default stays live_only, so existing deployments are unchanged."""
    router = Router(_config(), store=store)
    result = await router.deliver(_event(delivery=None))
    assert result == "no_subscriber"
    assert store.list_pending() == []


@pytest.mark.asyncio
async def test_config_default_delivery_opts_the_daemon_in(store):
    router = Router(_config(default_delivery="next_session"), store=store)
    assert await router.deliver(_event(delivery=None)) == "queued_next_session"
    assert store.pending_count() == 1


@pytest.mark.asyncio
async def test_event_can_opt_out_of_daemon_default(store):
    router = Router(_config(default_delivery="next_session"), store=store)
    assert await router.deliver(_event(delivery="live_only")) == "no_subscriber"
    assert store.pending_count() == 0


@pytest.mark.asyncio
async def test_unknown_delivery_mode_falls_back_to_live_only(store):
    router = Router(_config(), store=store)
    assert await router.deliver(_event(delivery="teleport")) == "no_subscriber"
    assert store.pending_count() == 0


@pytest.mark.asyncio
async def test_managed_session_uses_the_same_queue(store):
    """Plan 006 §3 names managed_session separately but must not fork storage."""
    router = Router(_config(), store=store)
    assert await router.deliver(_event(delivery="managed_session")) == "queued_next_session"
    assert store.pending_count() == 1


@pytest.mark.asyncio
async def test_durable_request_without_store_is_not_silently_claimed():
    """No store configured → honest no_subscriber, not a fake 'queued'."""
    router = Router(_config(), store=None)
    assert await router.deliver(_event()) == "no_subscriber"


@pytest.mark.asyncio
async def test_queued_event_delivered_on_next_session(store):
    router = Router(_config(), store=store)
    await router.deliver(_event())
    assert store.pending_count() == 1

    conn = MockConnection(router=router)
    router.subscribe("s1", "claude", "i1", ["github-actions"], conn)
    drained = await router.drain_pending("s1")

    assert drained == 1
    assert conn.sent[0]["type"] == "wake"
    assert conn.sent[0]["event"]["event_id"] == "ev-1"
    await _settle(store)  # the ack callback runs on the next loop ticks
    assert store.pending_count() == 0


@pytest.mark.asyncio
async def test_drain_respects_routing(store):
    """A queued event must not leak to an adapter it was never routed to."""
    cfg = _config()
    cfg["routing"] = {"github-actions": {"adapter": "opencode"}}
    router = Router(cfg, store=store)
    await router.deliver(_event())

    claude = MockConnection(router=router)
    router.subscribe("s1", "claude", "i1", ["github-actions"], claude)
    assert await router.drain_pending("s1") == 0
    assert claude.sent == []
    assert store.pending_count() == 1

    opencode = MockConnection(router=router)
    router.subscribe("s2", "opencode", "i2", ["github-actions"], opencode)
    assert await router.drain_pending("s2") == 1


@pytest.mark.asyncio
async def test_drain_only_covers_subscribed_sources(store):
    router = Router(_config(), store=store)
    await router.deliver(_event("ev-a", source="github-actions"))
    await router.deliver(_event("ev-b", source="telegram-bot"))

    conn = MockConnection(router=router)
    router.subscribe("s1", "claude", "i1", ["github-actions"], conn)
    assert await router.drain_pending("s1") == 1
    assert conn.sent[0]["event"]["event_id"] == "ev-a"


@pytest.mark.asyncio
async def test_unacked_event_stays_queued_for_the_session_after(store):
    router = Router(_config(), store=store)
    await router.deliver(_event())

    silent = MockConnection(router=router, auto_ack=False)
    router.subscribe("s1", "claude", "i1", ["github-actions"], silent)
    assert await router.drain_pending("s1") == 1
    # No ack arrived, so the row is still there (at-least-once).
    assert store.pending_count() == 1
    router.unsubscribe("s1")

    good = MockConnection(router=router)
    router.subscribe("s2", "claude", "i2", ["github-actions"], good)
    assert await router.drain_pending("s2") == 1
    await _settle(store)
    assert store.pending_count() == 0


@pytest.mark.asyncio
async def test_event_dead_letters_after_max_attempts(tmp_path):
    store = WakeStore(tmp_path / "state.db", pending_max_attempts=2)
    try:
        router = Router(_config(), store=store)
        await router.deliver(_event())

        for i in range(4):
            conn = MockConnection(router=router, auto_ack=False)
            router.subscribe(f"s{i}", "claude", "i", ["github-actions"], conn)
            await router.drain_pending(f"s{i}")
            router.unsubscribe(f"s{i}")
            if store.pending_count() == 0:
                break

        assert store.pending_count() == 0
        dl = store.list_dead_letters(kind="next_session")
        assert len(dl) == 1
        assert dl[0].ref_id == "ev-1"
        assert "no ack" in (dl[0].error or "")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_send_failure_falls_back_to_the_queue(store):
    """A subscriber that dies mid-send must not swallow a durable event."""
    router = Router(_config(), store=store)
    dead = MockConnection(router=router, fail=True)
    router.subscribe("s1", "claude", "i1", ["github-actions"], dead)
    assert await router.deliver(_event()) == "queued_next_session"
    assert store.pending_count() == 1


# ── daemon level (real socket + real ingest) ─────────────────────────────────


@pytest.mark.asyncio
async def test_daemon_level_next_session_delivery(tmp_path):
    """AC (WI-C): HTTP event with no session → next session receives it.

    Real unix socket, real hello handshake, real HMAC-authenticated ingest.
    The only thing faked is the harness on the far side of the socket.
    """
    store = WakeStore(tmp_path / "state.db")
    cfg = _config()
    router = Router(cfg, store=store)
    sock_path = tmp_path / "aw.sock"
    socket_server = SocketServer(sock_path, router)
    await socket_server.start()

    app = create_ingest_app(cfg, router, store=store)
    http = TestClient(TestServer(app))
    await http.start_server()

    try:
        body = json.dumps(_event("ev-daemon")).encode()
        sig = "sha256=" + hmac.new(b"k1", body, hashlib.sha256).hexdigest()
        resp = await http.post(
            "/",
            data=body,
            headers={
                "X-AgentWake-Source": "github-actions",
                "X-AgentWake-Signature": sig,
            },
        )
        assert resp.status == 202
        assert (await resp.json())["status"] == "queued_next_session"
        assert store.pending_count() == 1

        # ...now a session starts.
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        try:
            writer.write(
                encode_frame(
                    {
                        "type": "hello",
                        "v": 1,
                        "adapter": "claude",
                        "instance": "late-session",
                        "filters": {"sources": ["github-actions"]},
                    }
                )
            )
            await writer.drain()

            ack = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
            assert ack["type"] == "hello_ack"

            wake = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
            assert wake["type"] == "wake"
            assert wake["event"]["event_id"] == "ev-daemon"
            assert wake["event"]["content"] == "build failed"

            writer.write(encode_frame({"type": "ack", "ack_id": wake["ack_id"]}))
            await writer.drain()

            for _ in range(50):
                if store.pending_count() == 0:
                    break
                await asyncio.sleep(0.02)
            assert store.pending_count() == 0
        finally:
            writer.close()
    finally:
        await http.close()
        socket_server.close()
        store.close()


@pytest.mark.asyncio
async def test_daemon_level_queue_survives_daemon_restart(tmp_path):
    """The queue is durable, not just deferred within one process lifetime."""
    db = tmp_path / "state.db"
    cfg = _config()

    store = WakeStore(db)
    router = Router(cfg, store=store)
    assert await router.deliver(_event("ev-across-restart")) == "queued_next_session"
    store.close()  # daemon dies with the event still queued

    store2 = WakeStore(db)
    sock_path = tmp_path / "aw2.sock"
    router2 = Router(cfg, store=store2)
    socket_server = SocketServer(sock_path, router2)
    await socket_server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        try:
            writer.write(
                encode_frame(
                    {
                        "type": "hello",
                        "v": 1,
                        "adapter": "claude",
                        "instance": "post-restart",
                        "filters": {"sources": ["github-actions"]},
                    }
                )
            )
            await writer.drain()
            assert json.loads(
                await asyncio.wait_for(reader.readline(), timeout=5)
            )["type"] == "hello_ack"
            wake = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
            assert wake["event"]["event_id"] == "ev-across-restart"
        finally:
            writer.close()
    finally:
        socket_server.close()
        store2.close()
