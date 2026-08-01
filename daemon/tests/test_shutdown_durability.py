"""Shutdown must not lose a human alert, whatever the daemon was mid-way through.

This is an integration test on purpose: the defect it guards is a *seam*
between aiohttp's teardown order, the ingest handler, and ``main._shutdown``.
Every component here is the real one — real ``create_ingest_app``, real
``AppRunner`` built the way ``main`` builds it, real ``WakeStore``, real
``main._shutdown`` — because the two bugs this file exists for were both
invisible to unit tests of the parts.

The failure mode, in order:

1. ``AppRunner.cleanup()`` fires ``on_shutdown`` **before** it waits for
   in-flight request handlers (``await self.shutdown()`` precedes
   ``await self._server.shutdown(timeout)``).
2. ``post_root`` has awaits *before* it registers the delivery task — a secret
   resolution that can be a vault round-trip, and reading the body.
3. So a drain registered on ``on_shutdown`` looks at an empty set, returns, and
   the handler then spawns a task nobody will ever drain. The sender is told
   202 ``{"delivery": {"status": "dispatched"}}``, the store closes underneath
   the orphan, and its dead-letter write hits a closed database.

The alert is lost and the sender believes it was delivered — the exact
guarantee the dead-letter table exists to provide.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import socket
from typing import Any

import pytest
from aiohttp import ClientSession, web

from agent_waked import main as main_module
from agent_waked.delivery import HumanDelivery
from agent_waked.ingest import create_ingest_app
from agent_waked.store import WakeStore

_SECRET = b"shhh"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _SlowResolver:
    """A resolver with a vault-shaped latency, before the task is registered."""

    def __init__(self, delay: float = 0.6) -> None:
        self._delay = delay
        self.started = asyncio.Event()

    async def resolve_all(self, source_cfg: dict[str, Any]) -> list[bytes]:
        self.started.set()
        await asyncio.sleep(self._delay)
        return [_SECRET]

    async def resolve(self, uri: str) -> bytes:
        return _SECRET


class _HangingChannel:
    """A channel that never returns, as a webhook in 16s backoff does not."""

    name = "webhook"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def deliver(self, event, channel_cfg, resolver):
        self.started.set()
        await asyncio.sleep(60)
        return {"status": "failed", "error": "unreachable"}

    async def close(self) -> None:
        return None


class _MockRouter:
    def accepted_sources_for(self, adapter, requested):
        return requested

    async def deliver(self, event):
        return "queued"

    async def shutdown(self) -> None:
        return None


class _FakeSocketServer:
    def close(self) -> None:
        return None


class _FakeOutbox:
    async def close(self) -> None:
        return None


def _config() -> dict[str, Any]:
    return {
        "sources": {
            "test": {
                "secret_uris": ["env://AW_X"],
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


def _event_body(event_id: str = "evt-shutdown-1") -> bytes:
    return json.dumps({
        "v": 0,
        "event_id": event_id,
        "source": "test",
        "kind": "alert",
        "content": "the disk is full",
        "meta": {"target": "operator"},
        "wake": True,
    }).encode()


async def _run_shutdown_race(
    tmp_path,
    *,
    wait_for: str,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """POST an alert, interrupt the daemon at *wait_for*, return (responses, dls).

    ``wait_for="resolver"`` stops the daemon while the handler is still
    resolving secrets — before it has registered the delivery task at all.
    ``wait_for="channel"`` stops it once the delivery is in flight.
    """
    db_path = tmp_path / "state.db"
    store = WakeStore(db_path)
    resolver = _SlowResolver()
    channel = _HangingChannel()
    config = _config()
    delivery = HumanDelivery(
        config, resolver, channels={"webhook": channel}, store=store
    )

    router = _MockRouter()
    app = create_ingest_app(
        config,
        router,
        resolver=resolver,
        delivery=delivery,
        store=store,
    )
    runner = main_module._make_runner(app)
    await runner.setup()
    port = _free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    body = _event_body()
    sig = "sha256=" + hmac.new(_SECRET, body, hashlib.sha256).hexdigest()

    results: list[Any] = []
    async with ClientSession() as sess:
        async def _post() -> None:
            try:
                async with sess.post(
                    f"http://127.0.0.1:{port}/",
                    data=body,
                    headers={
                        "X-AgentWake-Source": "test",
                        "X-AgentWake-Signature": sig,
                    },
                ) as resp:
                    results.append((resp.status, await resp.json()))
            except Exception as exc:  # connection closed under us: also fine
                results.append(exc)

        post_task = asyncio.ensure_future(_post())
        gate = resolver.started if wait_for == "resolver" else channel.started
        await asyncio.wait_for(gate.wait(), timeout=5)

        # This is what SIGTERM does, with nothing stubbed.
        await main_module._shutdown(
            socket_server=_FakeSocketServer(),  # type: ignore[arg-type]
            runner=runner,
            router=router,  # type: ignore[arg-type]
            outbox=_FakeOutbox(),  # type: ignore[arg-type]
            delivery=delivery,
            store=store,
            app=app,
        )
        await asyncio.wait([post_task], timeout=5)

    # Reopen from disk: durability means surviving the process, not the object.
    reopened = WakeStore(db_path)
    try:
        entries = [e.to_json() for e in reopened.list_dead_letters()]
    finally:
        reopened.close()
    return results, entries


@pytest.mark.asyncio
async def test_sigterm_while_resolving_secrets_still_dead_letters(tmp_path, caplog):
    """The reviewer's reproduction: interrupted before the task even exists.

    A drain hung on ``on_shutdown`` sees an empty task set here and returns, so
    the task the handler spawns a moment later is never drained.
    """
    results, entries = await _run_shutdown_race(tmp_path, wait_for="resolver")

    assert len(entries) == 1, (
        f"the alert was lost on shutdown; dead-letter rows={entries} "
        f"client got {results}"
    )
    assert entries[0]["kind"] == "human_delivery"
    assert entries[0]["ref_id"] == "evt-shutdown-1"

    # Whatever the sender was told, it must not have been a lie.
    for result in results:
        if isinstance(result, tuple):
            status, doc = result
            assert status == 202, doc
            assert doc.get("delivery", {}).get("status") == "dispatched", doc

    # No write may have reached a closed database.
    assert "Cannot operate on a closed database" not in caplog.text
    assert "LOST ALERT" not in caplog.text


@pytest.mark.asyncio
async def test_sigterm_while_delivering_still_dead_letters(tmp_path, caplog):
    """The already-covered case, re-asserted end to end through main._shutdown."""
    results, entries = await _run_shutdown_race(tmp_path, wait_for="channel")

    assert len(entries) == 1, f"dead-letter rows={entries} client got {results}"
    assert entries[0]["kind"] == "human_delivery"
    assert "cancelled" in (entries[0]["error"] or "")
    assert "LOST ALERT" not in caplog.text


@pytest.mark.asyncio
async def test_no_delivery_task_survives_shutdown(tmp_path):
    """Nothing may still be running once the store is closed."""
    before = {t for t in asyncio.all_tasks() if not t.done()}
    await _run_shutdown_race(tmp_path, wait_for="resolver")
    await asyncio.sleep(0)
    leaked = [
        t
        for t in asyncio.all_tasks()
        if not t.done() and t not in before and "deliver" in repr(t)
    ]
    assert leaked == [], f"orphaned delivery tasks after shutdown: {leaked}"
