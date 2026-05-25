"""MCP stdio server for agent-wake Claude Code adapter.

This module uses raw JSON-RPC over stdio (same pattern as wake-probe) because
the official MCP Python SDK does not yet expose custom notification methods
required by the Claude Code channels research preview (notifications/claude/channel
and notifications/claude/channel/permission).

All replies to the client go through _notify.send for thread-safe stdout writing.
"""

import json
import sys

from ._notify import send
from .reply import get_tool_definition, handle_reply_tool_call
from .permission import handle_permission_request


def _reply(req_id, result: dict) -> None:
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def handle(msg: dict) -> None:
    """Dispatch a single JSON-RPC message."""
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        _reply(req_id, {
            "protocolVersion": params.get("protocolVersion", "2025-03-26"),
            "capabilities": {
                "experimental": {
                    "claude/channel": {},
                    "claude/channel/permission": {},
                },
                "tools": {},
            },
            "serverInfo": {"name": "agent-wake", "version": "0.0.1"},
            "instructions": (
                "Events arrive as <channel source=\"agent-wake\" ...>. "
                "The event's source is in the 'source' attribute and the "
                "kind is in the 'kind' attribute. Reply with the reply tool, "
                "passing the source from the tag."
            ),
        })
        return

    if method == "notifications/initialized":
        return

    if method == "tools/list":
        _reply(req_id, {"tools": [get_tool_definition()]})
        return

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "agent_wake_reply":
                result = handle_reply_tool_call(args)
            else:
                _error(req_id, -32601, f"unknown tool: {name}")
                return
            _reply(req_id, result)
        except Exception as e:
            _error(req_id, -32603, f"tool error: {e}")
        return

    if method == "notifications/claude/channel/permission_request":
        handle_permission_request(params)
        return

    if method and method.startswith("notifications/"):
        return

    if req_id is not None:
        _error(req_id, -32601, f"unknown method: {method}")


def main() -> None:
    """Read JSON-RPC lines from stdin and dispatch them."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        try:
            handle(msg)
        except Exception:
            # Log to stderr and continue; do not crash the server on a bad message.
            import traceback

            traceback.print_exc(file=sys.stderr)
            # If the message was a request (has id) and we haven't replied yet,
            # send an internal-error response so the client isn't left hanging.
            try:
                req_id = msg.get("id")
                if req_id is not None:
                    _error(
                        req_id, -32603, "Internal error — check adapter logs"
                    )
            except Exception:
                traceback.print_exc(file=sys.stderr)
