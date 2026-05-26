import sys
import json
import io
import threading

from agent_wake_claude.server import handle
from agent_wake_claude.reply import get_tool_definition, handle_reply_tool_call


def test_initialize():
    out = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    captured = []
    def fake_send(msg):
        captured.append(msg)

    import agent_wake_claude.server as srv
    orig_send = srv.send
    srv.send = fake_send
    try:
        handle(json.loads(out))
        assert len(captured) == 1
        assert captured[0]["result"]["serverInfo"]["name"] == "agent-wake"
        assert "claude/channel" in captured[0]["result"]["capabilities"]["experimental"]
    finally:
        srv.send = orig_send


def test_tools_list():
    out = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    captured = []
    def fake_send(msg):
        captured.append(msg)

    import agent_wake_claude.server as srv
    orig_send = srv.send
    srv.send = fake_send
    try:
        handle(json.loads(out))
        assert len(captured) == 1
        tools = captured[0]["result"]["tools"]
        assert any(t["name"] == "agent_wake_reply" for t in tools)
    finally:
        srv.send = orig_send


def test_unhandled_exception_in_handler_returns_jsonrpc_internal_error():
    import agent_wake_claude.server as srv

    captured = []

    def fake_send(msg):
        captured.append(msg)

    def boom(_args):
        raise RuntimeError("simulated unhandled failure")

    orig_send = srv.send
    orig_reply_handler = srv.handle_reply_tool_call
    srv.send = fake_send
    srv.handle_reply_tool_call = boom
    try:
        msg = {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {"name": "agent_wake_reply", "arguments": {}},
        }
        srv.handle(msg)
        assert len(captured) == 1, captured
        resp = captured[0]
        assert resp.get("id") == 99
        assert "error" in resp
        assert resp["error"]["code"] == -32603
    finally:
        srv.send = orig_send
        srv.handle_reply_tool_call = orig_reply_handler


def test_reply_tool_no_callback(monkeypatch):
    import agent_wake_claude.client as client_mod

    monkeypatch.setattr(client_mod, "send_reply_frame", lambda frame: None)

    def fake_wait(reply_id, timeout=35.0):
        return {"status": "no_callback", "reply_id": reply_id}

    monkeypatch.setattr(client_mod.ReplyResultBus, "wait_result", fake_wait)

    result = handle_reply_tool_call({"source": "demo", "content": "hello"})
    assert "sent (no callback_url configured)" in str(result)


def test_reply_tool_delivered(monkeypatch):
    import agent_wake_claude.client as client_mod

    monkeypatch.setattr(client_mod, "send_reply_frame", lambda frame: None)

    def fake_wait(reply_id, timeout=35.0):
        return {"status": "delivered", "reply_id": reply_id}

    monkeypatch.setattr(client_mod.ReplyResultBus, "wait_result", fake_wait)

    result = handle_reply_tool_call({"source": "demo", "content": "hello"})
    assert result["content"][0]["text"] == "sent"


def test_reply_tool_failed(monkeypatch):
    import agent_wake_claude.client as client_mod

    monkeypatch.setattr(client_mod, "send_reply_frame", lambda frame: None)

    def fake_wait(reply_id, timeout=35.0):
        return {"status": "failed", "reply_id": reply_id, "error": "HTTP 500"}

    monkeypatch.setattr(client_mod.ReplyResultBus, "wait_result", fake_wait)

    result = handle_reply_tool_call({"source": "demo", "content": "hello"})
    assert "reply delivery failed: HTTP 500" in str(result)
    assert result.get("isError") is True


def test_reply_tool_not_connected(monkeypatch):
    import agent_wake_claude.client as client_mod

    def boom(frame):
        raise RuntimeError("not connected to daemon")

    monkeypatch.setattr(client_mod, "send_reply_frame", boom)

    result = handle_reply_tool_call({"source": "demo", "content": "hello"})
    assert "reply delivery failed" in str(result)
    assert result.get("isError") is True
