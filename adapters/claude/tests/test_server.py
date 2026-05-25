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


def test_reply_tool_no_callback(monkeypatch):
    import agent_wake_claude.config as cm
    orig_cached = cm._cached_config
    cm._cached_config = {
        "sources": {"demo": {"callback_url": None}},
        "default_callback_url": None,
    }
    try:
        result = handle_reply_tool_call({"source": "demo", "content": "hello"})
        assert "sent (no callback_url configured)" in str(result)
    finally:
        cm._cached_config = orig_cached
