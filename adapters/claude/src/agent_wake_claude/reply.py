"""Reply tool for agent-wake Claude adapter.

Spec reference: v1-daemon-spec.md §9.4.

No longer makes HTTPS calls directly.  Sends a reply frame to the
daemon and awaits the matching reply_result via ReplyResultBus.
"""

import uuid

from . import client as daemon_client


def get_tool_definition() -> dict:
    return {
        "name": "agent_wake_reply",
        "description": "Reply to an external event source via agent-wake.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "content": {"type": "string"},
                "in_reply_to": {"type": "string"},
            },
            "required": ["source", "content"],
        },
    }


def handle_reply_tool_call(arguments: dict) -> dict:
    source = arguments.get("source")
    content = arguments.get("content")
    in_reply_to = arguments.get("in_reply_to")

    if not source or not content:
        return {
            "content": [
                {"type": "text", "text": "missing required fields: source, content"}
            ],
        }

    reply_id = str(uuid.uuid4())
    frame = {
        "type": "reply",
        "reply_id": reply_id,
        "source": source,
        "in_reply_to": in_reply_to or "",
        "content": content,
    }

    try:
        daemon_client.send_reply_frame(frame)
    except Exception as e:
        return {
            "content": [
                {"type": "text", "text": f"reply delivery failed: {e}"}
            ],
            "isError": True,
        }

    result = daemon_client.ReplyResultBus.wait_result(reply_id, timeout=35.0)
    if result is None:
        return {
            "content": [
                {"type": "text", "text": "reply delivery failed: timed out"}
            ],
            "isError": True,
        }

    status = result.get("status")
    if status == "delivered":
        return {"content": [{"type": "text", "text": "sent"}]}
    if status == "no_callback":
        return {
            "content": [
                {"type": "text", "text": "sent (no callback_url configured)"}
            ],
        }
    error = result.get("error", "unknown error")
    return {
        "content": [{"type": "text", "text": f"reply delivery failed: {error}"}],
        "isError": True,
    }
