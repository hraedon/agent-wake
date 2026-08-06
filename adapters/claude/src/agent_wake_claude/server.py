"""MCP stdio server for agent-wake Claude Code adapter.

This module uses raw JSON-RPC over stdio (same pattern as wake-probe) because
the official MCP Python SDK does not yet expose custom notification methods
required by the Claude Code channels research preview (notifications/claude/channel
and notifications/claude/channel/permission).

All replies to the client go through _notify.send for thread-safe stdout writing.
"""

import json
import sys
import threading
from typing import Any

from ._notify import send
from .channel import MAX_DEFERRED, flush_silent_events
from .reply import get_tool_definition, handle_reply_tool_call
from .permission import handle_permission_request

# Machine-readable declaration of what silent inject (``wake: false``) actually
# means on this harness. The channel protocol has no ``noReply`` equivalent, so
# rather than dropping silent events (the old behaviour) or pretending they are
# quiet (they are not), the adapter defers them until a turn is happening
# anyway. A caller can read this off ``initialize`` instead of guessing.
SILENT_INJECT_CAPABILITY: dict[str, Any] = {
    "supported": True,
    "mode": "deferred",
    "reason": (
        "notifications/claude/channel has no noReply flag; an event delivered "
        "to an idle session always starts a turn"
    ),
    "semantics": (
        "wake:false events are buffered and emitted once a turn is already "
        "underway, so they arrive as context without having triggered it"
    ),
    "flush_triggers": ["preceding_wake", "tools_call", "permission_request"],
    "max_deferred": MAX_DEFERRED,
    "overflow_behavior": "flush_and_wake",
    "drops_events": False,
}


def _reply(req_id: Any, result: dict[str, Any]) -> None:
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id: Any, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _run_reply_tool(req_id: Any, args: dict[str, Any]) -> None:
    try:
        _reply(req_id, handle_reply_tool_call(args))
    except Exception as e:
        _error(req_id, -32603, f"tool error: {e}")


def handle(msg: dict[str, Any]) -> None:
    """Dispatch a single JSON-RPC message."""
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        # Pin the protocol revision the channels research preview was built
        # against — do NOT echo the client's offer. Claude Code >=2.1.x
        # offers a modern MCP revision, and its channel registration skips
        # any connection that "negotiated a modern protocol revision with no
        # unsolicited notification path" (skip kind: era). Echoing the offer
        # therefore silently disabled every wake delivery (WI-011). MCP
        # version negotiation allows the server to answer with the revision
        # it supports; the client continues on the legacy path.
        _reply(req_id, {
            "protocolVersion": "2025-03-26",
            "capabilities": {
                "experimental": {
                    "claude/channel": {},
                    "claude/channel/permission": {},
                    "agent-wake/silent_inject": SILENT_INJECT_CAPABILITY,
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
        # A tool call can only happen inside an active turn, so this is proof
        # that deferred silent events can be delivered without waking anything.
        flush_silent_events("tools_call")
        name = params.get("name")
        args = params.get("arguments") or {}
        if name != "agent_wake_reply":
            _error(req_id, -32601, f"unknown tool: {name}")
            return
        threading.Thread(
            target=_run_reply_tool,
            args=(req_id, args),
            name=f"agent-wake-reply-{req_id}",
            daemon=True,
        ).start()
        return

    if method == "notifications/claude/channel/permission_request":
        # Permission prompts only open mid-turn — another safe flush point.
        flush_silent_events("permission_request")
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
