"""Emit wake events via Claude Code channel notifications."""

import logging
from typing import Any

from ._notify import send

log = logging.getLogger("agent_wake_claude.channel")


def emit_wake_event(event: dict[str, Any]) -> None:
    """Translate a wake event into a Claude Code channel notification and send it.

    If event["wake"] is False, the event is silently dropped because Claude Code
    channels always trigger a turn (silent inject is not supported in v0).
    """
    if not event.get("wake", True):
        log.warning(
            "dropping wake=false event (silent inject not supported on Claude Code v0) "
            "event_id=%s source=%s kind=%s",
            event.get("event_id", "?"),
            event.get("source", "?"),
            event.get("kind", "?"),
        )
        return

    meta = {
        "source": event.get("source", ""),
        "kind": event.get("kind", ""),
    }
    for k, v in event.get("meta", {}).items():
        if k.isidentifier():
            meta[k] = str(v)

    send({
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel",
        "params": {
            "content": event.get("content", ""),
            "meta": meta,
        },
    })
