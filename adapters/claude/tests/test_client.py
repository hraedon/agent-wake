"""Tests for agent_wake_claude.client — daemon client.

Spec reference: v1-daemon-spec.md §9.3, Phase 4 acceptance tests.

Covers:
- hello frame is the first write
- wake frame triggers emit_wake_event
- ack is sent post-emit
- malformed daemon frame is ignored without crashing
- reconnect-on-disconnect with backoff
"""

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_wake_claude.client import (
    ReplyResultBus,
    _one_session,
    run_client,
    send_reply_frame,
)


@pytest.fixture(autouse=True)
def _clear_bus():
    yield
    with ReplyResultBus._lock:
        ReplyResultBus._pending.clear()


async def _read_frame(reader: asyncio.StreamReader) -> dict:
    line = await asyncio.wait_for(reader.readline(), timeout=2)
    assert line, "connection closed unexpectedly"
    return json.loads(line)


async def _write_frame(writer: asyncio.StreamWriter, frame: dict) -> None:
    writer.write((json.dumps(frame) + "\n").encode())
    await writer.drain()


# ── hello is first write ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hello_is_first_write(tmp_path):
    sock_path = tmp_path / "test.sock"
    received = []

    async def mock_daemon(reader, writer):
        line = await asyncio.wait_for(reader.readline(), timeout=2)
        received.append(json.loads(line))
        writer.close()

    server = await asyncio.start_unix_server(mock_daemon, str(sock_path))
    try:
        task = asyncio.create_task(_one_session(sock_path, ["test"]))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    finally:
        server.close()
        await server.wait_closed()

    assert len(received) == 1
    assert received[0]["type"] == "hello"
    assert received[0]["v"] == 1
    assert received[0]["adapter"] == "claude"
    assert received[0]["filters"]["sources"] == ["test"]


# ── wake triggers emit_wake_event ────────────────────────────────────

@pytest.mark.asyncio
async def test_wake_triggers_emit(tmp_path):
    sock_path = tmp_path / "test.sock"
    emitted = []

    async def mock_daemon(reader, writer):
        line = await reader.readline()
        json.loads(line)
        ack = {"type": "hello_ack", "v": 1, "session_id": "s1", "accepted_sources": ["test"]}
        await _write_frame(writer, ack)
        wake = {"type": "wake", "ack_id": "a1", "event": {"v": 0, "source": "test", "content": "hello"}}
        await _write_frame(writer, wake)
        await asyncio.sleep(0.5)
        writer.close()

    server = await asyncio.start_unix_server(mock_daemon, str(sock_path))

    with patch("agent_wake_claude.client.emit_wake_event", side_effect=lambda e: emitted.append(e)):
        task = asyncio.create_task(_one_session(sock_path, ["test"]))
        try:
            await asyncio.wait_for(task, timeout=3)
        except (ConnectionError, Exception):
            pass
        finally:
            server.close()
            await server.wait_closed()

    assert len(emitted) == 1
    assert emitted[0]["content"] == "hello"


# ── ack is sent post-emit ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ack_sent_after_emit(tmp_path):
    sock_path = tmp_path / "test.sock"
    daemon_received = []

    async def mock_daemon(reader, writer):
        line = await reader.readline()
        json.loads(line)
        ack = {"type": "hello_ack", "v": 1, "session_id": "s1", "accepted_sources": ["test"]}
        await _write_frame(writer, ack)
        wake = {"type": "wake", "ack_id": "a1", "event": {"v": 0, "source": "test", "content": "hi"}}
        await _write_frame(writer, wake)
        resp_line = await asyncio.wait_for(reader.readline(), timeout=2)
        daemon_received.append(json.loads(resp_line))
        writer.close()

    server = await asyncio.start_unix_server(mock_daemon, str(sock_path))

    with patch("agent_wake_claude.client.emit_wake_event"):
        task = asyncio.create_task(_one_session(sock_path, ["test"]))
        try:
            await asyncio.wait_for(task, timeout=3)
        except (ConnectionError, Exception):
            pass
        finally:
            server.close()
            await server.wait_closed()

    assert len(daemon_received) == 1
    assert daemon_received[0]["type"] == "ack"
    assert daemon_received[0]["ack_id"] == "a1"


# ── nack on emit failure ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_nack_on_emit_failure(tmp_path):
    sock_path = tmp_path / "test.sock"
    daemon_received = []

    async def mock_daemon(reader, writer):
        await reader.readline()
        ack = {"type": "hello_ack", "v": 1, "session_id": "s1", "accepted_sources": ["test"]}
        await _write_frame(writer, ack)
        wake = {"type": "wake", "ack_id": "a1", "event": {"v": 0, "source": "test", "content": "hi"}}
        await _write_frame(writer, wake)
        resp_line = await asyncio.wait_for(reader.readline(), timeout=2)
        daemon_received.append(json.loads(resp_line))
        writer.close()

    server = await asyncio.start_unix_server(mock_daemon, str(sock_path))

    def boom(e):
        raise RuntimeError("channel broken")

    with patch("agent_wake_claude.client.emit_wake_event", side_effect=boom):
        task = asyncio.create_task(_one_session(sock_path, ["test"]))
        try:
            await asyncio.wait_for(task, timeout=3)
        except (ConnectionError, Exception):
            pass
        finally:
            server.close()
            await server.wait_closed()

    assert len(daemon_received) == 1
    assert daemon_received[0]["type"] == "nack"
    assert daemon_received[0]["ack_id"] == "a1"


# ── ping frame sends pong ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ping_sends_pong(tmp_path):
    sock_path = tmp_path / "test.sock"
    daemon_received = []

    async def mock_daemon(reader, writer):
        await reader.readline()
        ack = {"type": "hello_ack", "v": 1, "session_id": "s1", "accepted_sources": ["test"]}
        await _write_frame(writer, ack)
        ping = {"type": "ping"}
        await _write_frame(writer, ping)
        resp_line = await asyncio.wait_for(reader.readline(), timeout=2)
        daemon_received.append(json.loads(resp_line))
        writer.close()

    server = await asyncio.start_unix_server(mock_daemon, str(sock_path))

    task = asyncio.create_task(_one_session(sock_path, ["test"]))
    try:
        await asyncio.wait_for(task, timeout=3)
    except (ConnectionError, Exception):
        pass
    finally:
        server.close()
        await server.wait_closed()

    assert len(daemon_received) == 1
    assert daemon_received[0]["type"] == "pong"


# ── malformed daemon frame ignored ───────────────────────────────────

@pytest.mark.asyncio
async def test_malformed_frame_ignored(tmp_path):
    sock_path = tmp_path / "test.sock"
    errors = []

    async def mock_daemon(reader, writer):
        await reader.readline()
        ack = {"type": "hello_ack", "v": 1, "session_id": "s1", "accepted_sources": ["test"]}
        await _write_frame(writer, ack)
        writer.write(b"not-json\n")
        await writer.drain()
        await asyncio.sleep(0.5)
        wake = {"type": "wake", "ack_id": "a1", "event": {"v": 0, "source": "test", "content": "after"}}
        await _write_frame(writer, wake)
        resp_line = await asyncio.wait_for(reader.readline(), timeout=2)
        errors.append(json.loads(resp_line))
        writer.close()

    server = await asyncio.start_unix_server(mock_daemon, str(sock_path))

    with patch("agent_wake_claude.client.emit_wake_event"):
        task = asyncio.create_task(_one_session(sock_path, ["test"]))
        try:
            await asyncio.wait_for(task, timeout=3)
        except (ConnectionError, Exception):
            pass
        finally:
            server.close()
            await server.wait_closed()

    assert len(errors) == 1
    assert errors[0]["type"] == "ack"


# ── reconnect-on-disconnect with backoff ─────────────────────────────

@pytest.mark.asyncio
async def test_reconnect_after_disconnect(tmp_path):
    sock_path = tmp_path / "test.sock"
    connect_count = 0

    async def mock_daemon_factory(reader, writer):
        nonlocal connect_count
        connect_count += 1
        await reader.readline()
        ack = {"type": "hello_ack", "v": 1, "session_id": f"s{connect_count}", "accepted_sources": ["test"]}
        await _write_frame(writer, ack)
        await asyncio.sleep(0.1)
        writer.close()

    config = {"socket_path": str(sock_path), "sources": {"test": {}}}

    server = await asyncio.start_unix_server(mock_daemon_factory, str(sock_path))
    try:
        task = asyncio.create_task(run_client(config))
        await asyncio.sleep(3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        server.close()
        await server.wait_closed()

    assert connect_count >= 2


# ── ReplyResultBus ───────────────────────────────────────────────────

def test_reply_result_bus_deliver_and_wait():
    import threading

    ReplyResultBus.register("r1")

    def deliver():
        import time
        time.sleep(0.1)
        ReplyResultBus.deliver({"reply_id": "r1", "status": "delivered"})

    t = threading.Thread(target=deliver)
    t.start()

    result = ReplyResultBus.wait_result("r1", timeout=2)
    t.join()

    assert result is not None
    assert result["status"] == "delivered"


def test_reply_result_bus_timeout():
    ReplyResultBus.register("r2")
    result = ReplyResultBus.wait_result("r2", timeout=0.1)
    assert result is None
