import json
import hmac
import hashlib
import http.client
import urllib.request
import urllib.error
import time
import socket

from agent_wake_claude.ingest import start_listener


def _hmac(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return s.getsockname()[1]


def _start(config, callback):
    thread = start_listener(config, callback)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((config["host"], config["port"]), timeout=0.2):
                return thread
        except (ConnectionRefusedError, OSError):
            time.sleep(0.05)
    raise RuntimeError("listener did not start")


def test_missing_content_length():
    port = _find_free_port()
    config = {
        "host": "127.0.0.1",
        "port": port,
        "sources": {"demo": {"secret": b"shhh", "callback_url": None}},
    }
    thread = _start(config, lambda e: None)

    body = b'{"test":1}'
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("POST", "/")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("X-AgentWake-Source", "demo")
    conn.putheader("X-AgentWake-Signature", _hmac(b"shhh", body))
    conn.endheaders(body)
    resp = conn.getresponse()
    assert resp.status == 400
    data = json.loads(resp.read())
    assert "missing Content-Length" in data["error"]


def test_malformed_json_body():
    port = _find_free_port()
    config = {
        "host": "127.0.0.1",
        "port": port,
        "sources": {"demo": {"secret": b"shhh", "callback_url": None}},
    }
    received = []
    thread = _start(config, received.append)

    raw = b"this is not json {{{"
    sig = _hmac(b"shhh", raw)
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "X-AgentWake-Source": "demo",
            "X-AgentWake-Signature": sig,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read())
        assert data["status"] == "queued"

    time.sleep(0.2)
    assert len(received) == 1
    assert received[0]["kind"] == "webhook"
    assert "this is not json" in received[0]["content"]


def test_unknown_path():
    port = _find_free_port()
    config = {
        "host": "127.0.0.1",
        "port": port,
        "sources": {"demo": {"secret": b"shhh", "callback_url": None}},
    }
    thread = _start(config, lambda e: None)

    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/nonexistent",
            data=b"",
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_silent_wake_false():
    port = _find_free_port()
    config = {
        "host": "127.0.0.1",
        "port": port,
        "sources": {"demo": {"secret": b"shhh", "callback_url": None}},
    }
    received = []
    thread = _start(config, received.append)

    body = json.dumps({
        "v": 0, "event_id": "evt-silent", "source": "demo",
        "kind": "info", "content": "fyi", "meta": {}, "wake": False,
    }).encode()
    sig = _hmac(b"shhh", body)
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-AgentWake-Source": "demo",
            "X-AgentWake-Signature": sig,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read())
        assert data["status"] == "queued"

    time.sleep(0.2)
    assert len(received) == 1
    assert received[0]["wake"] is False


def test_missing_source_header():
    port = _find_free_port()
    config = {
        "host": "127.0.0.1",
        "port": port,
        "sources": {"demo": {"secret": b"shhh", "callback_url": None}},
    }
    thread = _start(config, lambda e: None)

    body = b'{"v":0,"event_id":"x","source":"demo","kind":"alert","content":"x","meta":{},"wake":true}'
    sig = _hmac(b"shhh", body)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-AgentWake-Signature": sig,
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_large_body():
    port = _find_free_port()
    config = {
        "host": "127.0.0.1",
        "port": port,
        "sources": {"demo": {"secret": b"shhh", "callback_url": None}},
    }
    received = []
    thread = _start(config, received.append)

    big_content = "x" * 100_000
    body = json.dumps({
        "v": 0, "event_id": "evt-big", "source": "demo",
        "kind": "webhook", "content": big_content, "meta": {}, "wake": True,
    }).encode()
    sig = _hmac(b"shhh", body)
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-AgentWake-Source": "demo",
            "X-AgentWake-Signature": sig,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
        assert data["status"] == "queued"

    time.sleep(0.2)
    assert len(received) == 1
    assert len(received[0]["content"]) == 100_000


def test_invalid_signature_format():
    port = _find_free_port()
    config = {
        "host": "127.0.0.1",
        "port": port,
        "sources": {"demo": {"secret": b"shhh", "callback_url": None}},
    }
    thread = _start(config, lambda e: None)

    body = b'{"v":0,"event_id":"x","source":"demo","kind":"alert","content":"x"}'
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-AgentWake-Source": "demo",
                "X-AgentWake-Signature": "not-sha256-format",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_server_unknown_method():
    from agent_wake_claude.server import handle
    import agent_wake_claude.server as srv

    captured = []
    srv.send = captured.append
    handle({"jsonrpc": "2.0", "id": 99, "method": "nonexistent/method"})
    assert len(captured) == 1
    assert captured[0]["error"]["code"] == -32601


def test_server_unknown_tool():
    from agent_wake_claude.server import handle
    import agent_wake_claude.server as srv

    captured = []
    srv.send = captured.append
    handle({"jsonrpc": "2.0", "id": 100, "method": "tools/call", "params": {"name": "nonexistent_tool"}})
    assert len(captured) == 1
    assert captured[0]["error"]["code"] == -32601
