import json
import hmac
import hashlib
import urllib.request
import time

from agent_wake_claude.ingest import start_listener


def _hmac(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def test_ingest_wake_event_and_dedupe():
    config = {
        "host": "127.0.0.1",
        "port": 19876,
        "sources": {
            "demo": {"secret": b"shhh", "callback_url": None},
        },
    }

    received = []
    thread = start_listener(config, received.append)
    time.sleep(0.3)

    body = json.dumps({
        "v": 0, "event_id": "evt-001", "source": "demo",
        "kind": "alert", "content": "hi", "meta": {}, "wake": True,
    }).encode()
    sig = _hmac(b"shhh", body)
    req = urllib.request.Request(
        "http://127.0.0.1:19876/",
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
        assert data["event_id"] == "evt-001"

    # Duplicate should return status duplicate
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read())
        assert data["status"] == "duplicate"

    time.sleep(0.2)
    assert len(received) == 1
    assert received[0]["content"] == "hi"


def test_ingest_wrapped_payload():
    config = {
        "host": "127.0.0.1",
        "port": 19877,
        "sources": {
            "gh": {"secret": b"abc", "callback_url": None},
        },
    }

    received = []
    thread = start_listener(config, received.append)
    time.sleep(0.3)

    raw = json.dumps({"action": "opened", "number": 42}).encode()
    sig = _hmac(b"abc", raw)
    req = urllib.request.Request(
        "http://127.0.0.1:19877/",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "X-AgentWake-Source": "gh",
            "X-AgentWake-Signature": sig,
            "X-AgentWake-Event-Id": "evt-wrapped-001",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read())
        assert data["status"] == "queued"
        assert data["event_id"] == "evt-wrapped-001"

    time.sleep(0.2)
    assert len(received) == 1
    ev = received[0]
    assert ev["kind"] == "webhook"
    assert ev["source"] == "gh"
    assert ev["meta"] == {}


def test_ingest_source_spoofing_returns_500_generic():
    """A v0 event body whose `source` differs from the authenticated header
    must be rejected fail-shut with a generic 500 body (no source-name leak).
    """
    config = {
        "host": "127.0.0.1",
        "port": 19880,
        "sources": {
            "real-source": {"secret": b"k1", "callback_url": None},
            "secret-internal": {"secret": b"k2", "callback_url": None},
        },
    }

    received = []
    thread = start_listener(config, received.append)
    time.sleep(0.3)

    # Authenticated as "real-source" but body claims to be from "secret-internal".
    body = json.dumps({
        "v": 0,
        "event_id": "spoof-001",
        "source": "secret-internal",
        "kind": "alert",
        "content": "pwn",
        "meta": {},
        "wake": True,
    }).encode()
    sig = _hmac(b"k1", body)
    req = urllib.request.Request(
        "http://127.0.0.1:19880/",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-AgentWake-Source": "real-source",
            "X-AgentWake-Signature": sig,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert False, f"expected 500, got {resp.status}"
    except urllib.error.HTTPError as e:
        assert e.code == 500
        body_text = e.read().decode()
        data = json.loads(body_text)
        # Generic body — must NOT leak either source name
        assert data == {"error": "source mismatch"}, data
        assert "real-source" not in body_text
        assert "secret-internal" not in body_text

    # Event must NOT have been delivered
    time.sleep(0.2)
    assert received == []


def test_ingest_invalid_signature():
    config = {
        "host": "127.0.0.1",
        "port": 19878,
        "sources": {
            "demo": {"secret": b"shhh", "callback_url": None},
        },
    }

    thread = start_listener(config, lambda e: None)
    time.sleep(0.3)

    body = json.dumps({"v": 0, "event_id": "evt-002", "source": "demo", "kind": "alert", "content": "x"}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:19878/",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-AgentWake-Source": "demo",
            "X-AgentWake-Signature": "sha256=deadbeef",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 403
    except urllib.error.HTTPError as e:
        assert e.code == 403
