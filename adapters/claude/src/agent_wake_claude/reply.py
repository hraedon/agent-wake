"""Reply tool for agent-wake Claude adapter."""

import json
import urllib.request

from .config import load_config


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
            "content": [{"type": "text", "text": "missing required fields: source, content"}],
        }

    config = load_config()
    source_cfg = config.get("sources", {}).get(source)
    callback_url = source_cfg.get("callback_url") if source_cfg else None
    callback_url = callback_url or config.get("default_callback_url")

    if not callback_url:
        return {
            "content": [{"type": "text", "text": "sent (no callback_url configured)"}],
        }

    payload = {
        "v": 0,
        "in_reply_to": in_reply_to or "",
        "content": content,
        "meta": {},
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        callback_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            _ = resp.read()
    except Exception as e:
        return {
            "content": [{"type": "text", "text": f"reply delivery failed: {e}"}],
            "isError": True,
        }

    return {
        "content": [{"type": "text", "text": "sent"}],
    }
