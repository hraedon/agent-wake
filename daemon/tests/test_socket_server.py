"""Tests for agent_waked.socket_server — unix-socket connection handling."""

import asyncio
import json
from pathlib import Path

import pytest

from agent_waked.proto import MAX_FRAME_SIZE, encode_frame
from agent_waked.router import Router
from agent_waked.socket_server import SocketServer


def _router_config():
    return {
        "sources": {"github-actions": {"secret": b"k1"}},
        "routing": {},
    }


def _hello_frame(**overrides) -> dict:
    f = {
        "type": "hello",
        "v": 1,
        "adapter": "claude",
        "instance": "test",
        "filters": {"sources": ["github-actions"]},
    }
    f.update(overrides)
    return f


@pytest.fixture
def sock_dir(tmp_path):
    return tmp_path / "sock"


@pytest.fixture
async def server(sock_dir):
    router = Router(_router_config())
    srv = SocketServer(sock_dir, router)
    await srv.start()
    yield srv
    srv.close()


async def _connect(sock_path: Path) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_unix_connection(str(sock_path))


async def _read_frame(reader: asyncio.StreamReader) -> dict:
    line = await asyncio.wait_for(reader.readline(), timeout=2)
    assert line, "connection closed unexpectedly"
    return json.loads(line)


async def _write_frame(writer: asyncio.StreamWriter, frame: dict) -> None:
    writer.write(encode_frame(frame))
    await writer.drain()


# ── happy path ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hello_receives_hello_ack(server, sock_dir):
    reader, writer = await _connect(sock_dir)
    await _write_frame(writer, _hello_frame())
    ack = await _read_frame(reader)
    assert ack["type"] == "hello_ack"
    assert ack["v"] == 1
    assert isinstance(ack["session_id"], str)
    assert ack["accepted_sources"] == ["github-actions"]
    writer.close()


@pytest.mark.asyncio
async def test_connection_stays_open_after_hello(server, sock_dir):
    reader, writer = await _connect(sock_dir)
    await _write_frame(writer, _hello_frame())
    await _read_frame(reader)
    await _write_frame(writer, {"type": "ack", "ack_id": "test-1"})
    await asyncio.sleep(0.1)
    assert not reader.at_eof()
    writer.close()


@pytest.mark.asyncio
async def test_subscribed_adapter_tracked(server, sock_dir):
    reader, writer = await _connect(sock_dir)
    await _write_frame(writer, _hello_frame())
    await _read_frame(reader)
    assert len(server.connections) == 1
    conn = next(iter(server.connections.values()))
    assert conn.adapter == "claude"
    assert conn.sources == ["github-actions"]
    writer.close()


@pytest.mark.asyncio
async def test_disconnect_removes_connection(server, sock_dir):
    reader, writer = await _connect(sock_dir)
    await _write_frame(writer, _hello_frame())
    await _read_frame(reader)
    writer.close()
    await asyncio.sleep(0.2)
    assert len(server.connections) == 0


# ── version_unsupported ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hello_wrong_version_gets_error_and_close(server, sock_dir):
    reader, writer = await _connect(sock_dir)
    await _write_frame(writer, _hello_frame(v=99))
    err = await _read_frame(reader)
    assert err["type"] == "error"
    assert err["code"] == "version_unsupported"
    assert err["fatal"] is True
    await asyncio.sleep(0.2)
    assert reader.at_eof()


# ── bad_frame (malformed JSON) ───────────────────────────────────────

@pytest.mark.asyncio
async def test_malformed_json_gets_bad_frame_error(server, sock_dir):
    reader, writer = await _connect(sock_dir)
    writer.write(b"not json\n")
    await writer.drain()
    err = await _read_frame(reader)
    assert err["type"] == "error"
    assert err["code"] == "bad_frame"
    assert err["fatal"] is True
    await asyncio.sleep(0.2)
    assert reader.at_eof()


# ── frame_too_large ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_oversize_line_gets_frame_too_large(server, sock_dir):
    reader, writer = await _connect(sock_dir)
    big = b'{"type":"hello"}' + b" " * (MAX_FRAME_SIZE + 1) + b"\n"
    writer.write(big)
    await writer.drain()
    err = await _read_frame(reader)
    assert err["type"] == "error"
    assert err["code"] == "frame_too_large"
    assert err["fatal"] is True
    await asyncio.sleep(0.2)
    assert reader.at_eof()


# ── unauthenticated (non-hello first frame) ──────────────────────────

@pytest.mark.asyncio
async def test_non_hello_first_frame_gets_unauthenticated(server, sock_dir):
    reader, writer = await _connect(sock_dir)
    await _write_frame(writer, {"type": "ack", "ack_id": "x"})
    err = await _read_frame(reader)
    assert err["type"] == "error"
    assert err["code"] == "unauthenticated"
    assert err["fatal"] is True
    await asyncio.sleep(0.2)
    assert reader.at_eof()


# ── connection cap ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connection_cap_enforced(sock_dir):
    from agent_waked.socket_server import MAX_CONNECTIONS
    router = Router(_router_config())
    srv = SocketServer(sock_dir, router)
    await srv.start()
    connections = []
    try:
        for i in range(MAX_CONNECTIONS):
            r, w = await _connect(sock_dir)
            await _write_frame(w, _hello_frame(instance=f"t-{i}"))
            await _read_frame(r)
            connections.append((r, w))

        r, w = await _connect(sock_dir)
        await _write_frame(w, _hello_frame(instance="overflow"))
        err = await _read_frame(r)
        assert err["type"] == "error"
        assert err["code"] == "connection_limit"
    finally:
        for r, w in connections:
            w.close()
        srv.close()


# ── single-instance lock ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_second_instance_rejected(sock_dir):
    router = Router(_router_config())
    srv1 = SocketServer(sock_dir, router)
    await srv1.start()
    try:
        router2 = Router(_router_config())
        srv2 = SocketServer(sock_dir, router2)
        with pytest.raises(RuntimeError, match="another agent-waked instance"):
            await srv2.start()
    finally:
        srv1.close()


# ── multiple adapters ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_adapters_connect(server, sock_dir):
    r1, w1 = await _connect(sock_dir)
    await _write_frame(w1, _hello_frame(adapter="claude", instance="a"))
    ack1 = await _read_frame(r1)

    r2, w2 = await _connect(sock_dir)
    await _write_frame(w2, _hello_frame(adapter="opencode", instance="b"))
    ack2 = await _read_frame(r2)

    assert len(server.connections) == 2
    assert ack1["session_id"] != ack2["session_id"]
    w1.close()
    w2.close()
