"""Emit wake events via Claude Code channel notifications."""

from ._notify import send


def emit_wake_event(event: dict) -> None:
    """Translate a wake event into a Claude Code channel notification and send it.

    If event["wake"] is False, the event is silently dropped because Claude Code
    channels always trigger a turn (silent inject is not supported in v0).
    """
    if not event.get("wake", True):
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
