"""End-to-end test: daemon subprocess → mock adapter → HTTP wake → reply → callback.

Spec reference: v1-daemon-spec.md Phase 6 acceptance test 1.

Spins up the daemon on an ephemeral port + ephemeral socket path, connects a
mock adapter over the unix socket, sends an HTTP wake event, asserts the wake
frame arrives, sends ack + reply, and verifies the callback POST is delivered.
"""

import asyncio
import hashlib
import hmac
import json
import os
import socket
import sys
from pathlib import Path

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from agent_waked.proto import encode_frame


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _hmac_sig(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def _write_config(
    path: Path,
    port: int,
    sock_path: Path,
    sources: dict,
    default_callback_url: str | None = None,
    routing: dict | None = None,
) -> None:
    cfg = {
        "version": 1,
        "listen": {"host": "127.0.0.1", "port": port},
        "socket_path": str(sock_path),
        "sources": sources,
        "default_callback_url": default_callback_url,
        "routing": routing or {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg))


async def _read_frame(reader: asyncio.StreamReader) -> dict:
    line = await asyncio.wait_for(reader.readline(), timeout=5)
    assert line, "connection closed unexpectedly"
    return json.loads(line)


@pytest.mark.asyncio
async def test_e2e_wake_and_reply(tmp_path):
    port = _free_port()
    sock_path = tmp_path / "aw.sock"
    config_path = tmp_path / "config.json"
    secret_env = "AW_E2E_SECRET"
    secret = "e2e-test-secret"

    callback_received: list[dict] = []

    async def callback_handler(request: web.Request) -> web.Response:
        body = await request.json()
        callback_received.append(body)
        return web.json_response({"ok": True})

    cb_app = web.Application()
    cb_app.router.add_post("/callback", callback_handler)
    cb_server = TestServer(cb_app)
    cb_client = TestClient(cb_server)
    await cb_client.start_server()
    callback_url = str(cb_client.make_url("/callback"))

    try:
        _write_config(
            config_path,
            port=port,
            sock_path=sock_path,
            sources={
                "test-source": {
                    "secret_env": secret_env,
                    "callback_url": callback_url,
                },
            },
        )

        env = {
            **os.environ,
            secret_env: secret,
            "AGENT_WAKE_CONFIG": str(config_path),
        }
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "agent_waked",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            await asyncio.sleep(1.5)
            assert proc.returncode is None, "daemon crashed on startup"

            reader, writer = await asyncio.open_unix_connection(str(sock_path))

            hello = {
                "type": "hello",
                "v": 1,
                "adapter": "mock",
                "instance": "e2e-test",
                "filters": {"sources": ["test-source"]},
            }
            writer.write(encode_frame(hello))
            await writer.drain()

            hello_ack = await _read_frame(reader)
            assert hello_ack["type"] == "hello_ack"
            assert hello_ack["v"] == 1
            assert "test-source" in hello_ack["accepted_sources"]

            body = json.dumps({
                "v": 0,
                "event_id": "e2e-001",
                "source": "test-source",
                "kind": "alert",
                "content": "build failed",
                "meta": {},
                "wake": True,
            }).encode()

            async with ClientSession() as http:
                async with http.post(
                    f"http://127.0.0.1:{port}/",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-AgentWake-Source": "test-source",
                        "X-AgentWake-Signature": _hmac_sig(secret.encode(), body),
                    },
                ) as resp:
                    assert resp.status == 202
                    data = await resp.json()
                    assert data["status"] == "queued"
                    assert data["event_id"] == "e2e-001"

            wake = await _read_frame(reader)
            assert wake["type"] == "wake"
            assert wake["event"]["event_id"] == "e2e-001"
            assert wake["event"]["content"] == "build failed"
            assert wake["event"]["source"] == "test-source"
            ack_id = wake["ack_id"]
            assert isinstance(ack_id, str) and len(ack_id) > 0

            ack_frame = {"type": "ack", "ack_id": ack_id}
            writer.write(encode_frame(ack_frame))
            await writer.drain()

            reply_frame = {
                "type": "reply",
                "reply_id": "rpl-e2e-001",
                "source": "test-source",
                "in_reply_to": ack_id,
                "content": "deployed!",
            }
            writer.write(encode_frame(reply_frame))
            await writer.drain()

            result = await _read_frame(reader)
            assert result["type"] == "reply_result"
            assert result["reply_id"] == "rpl-e2e-001"
            assert result["status"] == "delivered"
            assert result["http_status"] == 200
            assert result["error"] is None

            await asyncio.sleep(0.3)
            assert len(callback_received) == 1
            assert callback_received[0] == {
                "v": 0,
                "in_reply_to": ack_id,
                "content": "deployed!",
                "meta": {},
            }

        finally:
            if proc.returncode is None:
                proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
    finally:
        await cb_client.close()


@pytest.mark.asyncio
async def test_e2e_no_subscriber(tmp_path):
    port = _free_port()
    sock_path = tmp_path / "aw.sock"
    config_path = tmp_path / "config.json"
    secret_env = "AW_E2E_NS_SECRET"
    secret = "ns-test-secret"

    _write_config(
        config_path,
        port=port,
        sock_path=sock_path,
        sources={
            "orphan-source": {
                "secret_env": secret_env,
                "callback_url": None,
            },
        },
    )

    env = {
        **os.environ,
        secret_env: secret,
        "AGENT_WAKE_CONFIG": str(config_path),
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "agent_waked",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        await asyncio.sleep(1.5)
        assert proc.returncode is None

        body = json.dumps({
            "v": 0,
            "event_id": "ns-001",
            "source": "orphan-source",
            "kind": "alert",
            "content": "nobody listening",
            "meta": {},
            "wake": True,
        }).encode()

        async with ClientSession() as http:
            async with http.post(
                f"http://127.0.0.1:{port}/",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-AgentWake-Source": "orphan-source",
                    "X-AgentWake-Signature": _hmac_sig(secret.encode(), body),
                },
            ) as resp:
                assert resp.status == 202
                data = await resp.json()
                assert data["status"] == "no_subscriber"
                assert data["event_id"] == "ns-001"
    finally:
        if proc.returncode is None:
            proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=5)


@pytest.mark.asyncio
async def test_e2e_duplicate_event(tmp_path):
    port = _free_port()
    sock_path = tmp_path / "aw.sock"
    config_path = tmp_path / "config.json"
    secret_env = "AW_E2E_DUP_SECRET"
    secret = "dup-test-secret"

    _write_config(
        config_path,
        port=port,
        sock_path=sock_path,
        sources={
            "dup-source": {
                "secret_env": secret_env,
                "callback_url": None,
            },
        },
    )

    env = {
        **os.environ,
        secret_env: secret,
        "AGENT_WAKE_CONFIG": str(config_path),
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "agent_waked",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        await asyncio.sleep(1.5)
        assert proc.returncode is None

        body = json.dumps({
            "v": 0,
            "event_id": "dup-001",
            "source": "dup-source",
            "kind": "alert",
            "content": "first",
            "meta": {},
            "wake": True,
        }).encode()

        headers = {
            "Content-Type": "application/json",
            "X-AgentWake-Source": "dup-source",
            "X-AgentWake-Signature": _hmac_sig(secret.encode(), body),
        }

        async with ClientSession() as http:
            async with http.post(
                f"http://127.0.0.1:{port}/",
                data=body,
                headers=headers,
            ) as resp1:
                assert resp1.status == 202
                data1 = await resp1.json()
                assert data1["status"] == "no_subscriber"

            async with http.post(
                f"http://127.0.0.1:{port}/",
                data=body,
                headers=headers,
            ) as resp2:
                assert resp2.status == 202
                data2 = await resp2.json()
                assert data2["status"] == "duplicate"
                assert data2["event_id"] == "dup-001"
    finally:
        if proc.returncode is None:
            proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=5)
