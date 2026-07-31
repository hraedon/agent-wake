"""Tests for agent_waked.outbox — outbound reply delivery.

Acceptance criteria (spec §Phase 3):
1. Configured callback → POST {"v":0,...,"meta":{}} → status=delivered
2. No callback configured → status=no_callback, no HTTP egress
3. Callback returning 500 → status=failed
4. Callback timeout → status=failed with timeout message
"""

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent_waked.outbox import Outbox


def _config(callback_url=None, default_callback_url=None):
    sources = {
        "github-actions": {
            "secret": b"k1",
            "callback_url": callback_url,
        },
        "no-cb": {
            "secret": b"k2",
            "callback_url": None,
        },
    }
    return {
        "sources": sources,
        "default_callback_url": default_callback_url,
    }


@pytest.fixture
async def outbox(_config_with_callback):
    ob = Outbox(_config_with_callback)
    await ob.start()
    yield ob
    await ob.close()


@pytest.fixture
def _config_with_callback():
    return _config(callback_url=None, default_callback_url=None)


# ── acceptance 1: delivered ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_reply_delivered_to_callback():
    received = []

    async def handler(request):
        body = await request.json()
        received.append(body)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/callback", handler)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    try:
        url = str(cli.make_url("/callback"))
        cfg = _config(callback_url=url)
        ob = Outbox(cfg)
        await ob.start()
        result = await ob.deliver(
            source="github-actions",
            reply_id="rpl-001",
            in_reply_to="evt-001",
            content="deployed!",
        )
        await ob.close()

        assert result["status"] == "delivered"
        assert result["http_status"] == 200
        assert result["reply_id"] == "rpl-001"
        assert result["error"] is None

        assert len(received) == 1
        assert received[0] == {
            "v": 0,
            "in_reply_to": "evt-001",
            "content": "deployed!",
            "meta": {},
        }
    finally:
        await cli.close()


# ── acceptance 2: no callback configured ─────────────────────────────

@pytest.mark.asyncio
async def test_reply_no_callback():
    cfg = _config(callback_url=None, default_callback_url=None)
    ob = Outbox(cfg)
    await ob.start()
    result = await ob.deliver(
        source="no-cb",
        reply_id="rpl-002",
        in_reply_to="evt-002",
        content="ok",
    )
    await ob.close()

    assert result["status"] == "no_callback"
    assert result["http_status"] is None
    assert result["reply_id"] == "rpl-002"


@pytest.mark.asyncio
async def test_reply_uses_default_callback():
    received = []

    async def handler(request):
        body = await request.json()
        received.append(body)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/default-cb", handler)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    try:
        url = str(cli.make_url("/default-cb"))
        cfg = _config(callback_url=None, default_callback_url=url)
        ob = Outbox(cfg)
        await ob.start()
        result = await ob.deliver(
            source="no-cb",
            reply_id="rpl-003",
            in_reply_to="evt-003",
            content="via-default",
        )
        await ob.close()

        assert result["status"] == "delivered"
        assert len(received) == 1
    finally:
        await cli.close()


# ── acceptance 3: callback returns 500 ───────────────────────────────

@pytest.mark.asyncio
async def test_reply_callback_500():
    async def handler(request):
        return web.Response(status=500, text="internal error")

    app = web.Application()
    app.router.add_post("/callback", handler)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    try:
        url = str(cli.make_url("/callback"))
        cfg = _config(callback_url=url)
        ob = Outbox(cfg, max_retries=1)
        await ob.start()
        result = await ob.deliver(
            source="github-actions",
            reply_id="rpl-004",
            in_reply_to="evt-004",
            content="fail",
        )
        await ob.close()

        assert result["status"] == "failed"
        assert result["http_status"] == 500
        assert result["error"] is not None
    finally:
        await cli.close()


# ── acceptance 4: callback timeout ───────────────────────────────────

@pytest.mark.asyncio
async def test_reply_callback_timeout():
    async def handler(request):
        await asyncio.sleep(60)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/callback", handler)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    try:
        url = str(cli.make_url("/callback"))
        cfg = _config(callback_url=url)
        ob = Outbox(cfg, max_retries=1)
        await ob.start()
        result = await ob.deliver(
            source="github-actions",
            reply_id="rpl-005",
            in_reply_to="evt-005",
            content="slow",
        )
        await ob.close()

        assert result["status"] == "failed"
        assert result["http_status"] is None
        assert "timed out" in (result["error"] or "").lower()
    finally:
        await cli.close()


# ── acceptance 4 (supplementary): timeout is ~30s ────────────────────

@pytest.mark.asyncio
async def test_reply_timeout_is_approximately_30s():
    import time

    async def handler(request):
        await asyncio.sleep(120)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/callback", handler)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    try:
        url = str(cli.make_url("/callback"))
        cfg = _config(callback_url=url)
        ob = Outbox(cfg, max_retries=1)
        await ob.start()
        t0 = time.monotonic()
        result = await ob.deliver(
            source="github-actions",
            reply_id="rpl-006",
            in_reply_to="evt-006",
            content="timing",
        )
        elapsed = time.monotonic() - t0
        await ob.close()

        assert result["status"] == "failed"
        assert 29 <= elapsed <= 35, f"timeout took {elapsed:.1f}s, expected ~30s"
    finally:
        await cli.close()


# ── unknown source with default callback ─────────────────────────────

@pytest.mark.asyncio
async def test_reply_unknown_source_with_default():
    received = []

    async def handler(request):
        body = await request.json()
        received.append(body)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/cb", handler)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    try:
        url = str(cli.make_url("/cb"))
        cfg = _config(callback_url=None, default_callback_url=url)
        ob = Outbox(cfg)
        await ob.start()
        result = await ob.deliver(
            source="nonexistent",
            reply_id="rpl-007",
            in_reply_to="evt-007",
            content="fallback",
        )
        await ob.close()

        assert result["status"] == "delivered"
        assert len(received) == 1
    finally:
        await cli.close()


# ── unknown source without default callback ──────────────────────────

@pytest.mark.asyncio
async def test_reply_unknown_source_no_callback():
    cfg = _config(callback_url=None, default_callback_url=None)
    ob = Outbox(cfg)
    await ob.start()
    result = await ob.deliver(
        source="nonexistent",
        reply_id="rpl-008",
        in_reply_to="evt-008",
        content="nothing",
    )
    await ob.close()

    assert result["status"] == "no_callback"


# ── dead-letter (BC-WAKE-012) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_permanent_failure_is_dead_lettered(tmp_path):
    """A reply whose callback never recovers must survive as an inspectable row.

    Before this, the retries were exhausted and the reply left nothing but a
    log line — there was no way for an operator to see or resend it.
    """
    from agent_waked.store import WakeStore

    async def handler(request):
        return web.json_response({"nope": True}, status=500)

    app = web.Application()
    app.router.add_post("/callback", handler)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    store = WakeStore(tmp_path / "state.db")
    try:
        cfg = _config(callback_url=str(cli.make_url("/callback")))
        ob = Outbox(cfg, max_retries=2, backoff_delays=(0.0,), store=store)
        await ob.start()
        result = await ob.deliver(
            source="github-actions",
            reply_id="rpl-dl",
            in_reply_to="evt-dl",
            content="never lands",
        )
        await ob.close()

        assert result["status"] == "failed"
        entries = store.list_dead_letters(kind="reply")
        assert len(entries) == 1
        entry = entries[0]
        assert entry.id == result["dead_letter_id"]
        assert entry.ref_id == "rpl-dl"
        assert entry.source == "github-actions"
        assert entry.attempts == 2
        assert entry.error == "HTTP 500"
        # The payload carries everything a redrive needs.
        assert entry.payload == {
            "source": "github-actions",
            "reply_id": "rpl-dl",
            "in_reply_to": "evt-dl",
            "content": "never lands",
        }
    finally:
        await cli.close()
        store.close()


@pytest.mark.asyncio
async def test_rejected_url_is_dead_lettered(tmp_path):
    from agent_waked.store import WakeStore

    store = WakeStore(tmp_path / "state.db")
    try:
        cfg = _config(callback_url="file:///etc/passwd")
        ob = Outbox(cfg, store=store)
        await ob.start()
        result = await ob.deliver(
            source="github-actions",
            reply_id="rpl-bad-url",
            in_reply_to="evt-1",
            content="x",
        )
        await ob.close()
        assert result["status"] == "rejected"
        assert store.dead_letter_count() == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_no_callback_is_not_dead_lettered(tmp_path):
    """"Nobody asked for a reply" is not a failure; it must not fill the queue."""
    from agent_waked.store import WakeStore

    store = WakeStore(tmp_path / "state.db")
    try:
        ob = Outbox(_config(), store=store)
        await ob.start()
        result = await ob.deliver(
            source="no-cb", reply_id="r", in_reply_to="e", content="x"
        )
        await ob.close()
        assert result["status"] == "no_callback"
        assert store.dead_letter_count() == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_outbox_without_store_keeps_v0_shape():
    cfg = _config(callback_url="file:///etc/passwd")
    ob = Outbox(cfg)
    await ob.start()
    result = await ob.deliver(
        source="github-actions", reply_id="r", in_reply_to="e", content="x"
    )
    await ob.close()
    assert result["status"] == "rejected"
    assert "dead_letter_id" not in result
