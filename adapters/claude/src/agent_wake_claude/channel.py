"""Emit wake events via Claude Code channel notifications.

Silent inject on Claude Code
----------------------------
agent-wake has two modes: **wake** (``wake: true`` — trigger a turn) and
**silent inject** (``wake: false`` — put context in front of the model without
triggering a turn). The opencode adapter maps the second onto the SDK's
``noReply: true``.

The Claude Code channel protocol has no equivalent flag. Its notification
``params`` are exactly ``{content, meta}`` (channels reference, "Notification
format"), and an event arriving at an idle session starts a turn. There is no
"quiet" field to set, so a literal port of ``noReply`` is not available.

What the protocol *does* document is this:

    "Events queue into the session and are processed in order. If several
    notifications arrive while Claude is busy, they're delivered together on
    the next turn and Claude handles them as a group."

That is the honest silent-inject primitive on this harness: an event emitted
while a turn is already going to happen rides along as context **without
having caused a turn**. So this adapter implements silent inject as
*deferred* inject:

1. A ``wake: false`` event is buffered, never dropped, and acked to the daemon.
2. It is flushed the moment a turn is known to be happening anyway:
   * immediately before the next ``wake: true`` event's notification — that
     turn is happening regardless, so the silent event costs nothing;
   * on a ``tools/call`` request, which Claude can only issue mid-turn;
   * on a permission relay request, which only fires mid-turn.
3. Flushed events carry ``silent="true"`` and ``deferred_ms`` in ``meta`` so
   the model can see they are context, not a fresh alert.

The one honest limitation: if silent events keep arriving and no turn ever
happens, the buffer would grow without bound. At ``MAX_DEFERRED`` the adapter
flushes anyway and *does* trigger a turn, tagged ``flush_reason="overflow"``.
Waking late beats losing the event or leaking memory, and the model is told
which happened.

This behaviour is declared machine-readably in the MCP ``initialize`` result
under ``capabilities.experimental['agent-wake/silent_inject']`` — see
``server.SILENT_INJECT_CAPABILITY`` — so a caller can discover the semantics
instead of inferring them from a dropped event.
"""

import logging
import threading
import time
from typing import Any

from ._notify import send

log = logging.getLogger("agent_wake_claude.channel")

# Ceiling on buffered silent events before the adapter gives up on waiting for
# a turn and flushes anyway (which does wake the session).
MAX_DEFERRED = 64

_deferred_lock = threading.Lock()
_deferred: list[tuple[float, dict[str, Any]]] = []


def _channel_meta(event: dict[str, Any], **extra: str) -> dict[str, str]:
    """Build the ``meta`` map for a channel notification.

    Channel meta keys become ``<channel>`` tag attributes and must be
    identifiers (channels reference, "Notification format"); non-identifier
    keys are silently dropped by the client, so they are filtered here rather
    than sent and lost.
    """
    meta: dict[str, str] = {
        "source": str(event.get("source", "")),
        "kind": str(event.get("kind", "")),
    }
    event_meta = event.get("meta")
    if isinstance(event_meta, dict):
        for k, v in event_meta.items():
            if isinstance(k, str) and k.isidentifier():
                meta[k] = str(v)
    meta.update(extra)
    return meta


def _send_channel(content: str, meta: dict[str, str]) -> None:
    send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel",
            "params": {"content": content, "meta": meta},
        }
    )


def pending_silent_count() -> int:
    """Number of silent events buffered awaiting a turn."""
    with _deferred_lock:
        return len(_deferred)


def reset_deferred() -> None:
    """Drop the buffer. For tests only."""
    with _deferred_lock:
        _deferred.clear()


def defer_silent_event(event: dict[str, Any]) -> None:
    """Buffer a ``wake: false`` event for delivery on the next turn."""
    overflow: list[tuple[float, dict[str, Any]]] = []
    with _deferred_lock:
        _deferred.append((time.monotonic(), event))
        if len(_deferred) >= MAX_DEFERRED:
            overflow = _deferred[:]
            _deferred.clear()

    log.info(
        "silent event deferred event_id=%s source=%s kind=%s queued=%d",
        event.get("event_id", "?"),
        event.get("source", "?"),
        event.get("kind", "?"),
        len(overflow) or pending_silent_count(),
    )

    if overflow:
        log.warning(
            "silent-inject buffer reached %d events with no turn to ride "
            "along with; flushing now, which will wake the session",
            MAX_DEFERRED,
        )
        _emit_deferred(overflow, "overflow")


def flush_silent_events(reason: str) -> int:
    """Emit every buffered silent event. Returns how many were emitted.

    *reason* records why we believe a turn is already underway, and is passed
    through to the model as ``flush_reason``.
    """
    with _deferred_lock:
        batch = _deferred[:]
        _deferred.clear()
    if not batch:
        return 0
    _emit_deferred(batch, reason)
    return len(batch)


def _emit_deferred(batch: list[tuple[float, dict[str, Any]]], reason: str) -> None:
    now = time.monotonic()
    for queued_at, event in batch:
        meta = _channel_meta(
            event,
            silent="true",
            flush_reason=reason,
            deferred_ms=str(int((now - queued_at) * 1000)),
        )
        _send_channel(str(event.get("content", "")), meta)
        log.info(
            "silent event delivered event_id=%s reason=%s",
            event.get("event_id", "?"),
            reason,
        )


def emit_wake_event(event: dict[str, Any]) -> None:
    """Translate a wake event into a Claude Code channel notification and send it.

    ``wake: false`` events are deferred (see the module docstring), never
    dropped. A ``wake: true`` event first flushes anything deferred, because
    the turn it is about to trigger is exactly the turn those events were
    waiting for.
    """
    if not event.get("wake", True):
        defer_silent_event(event)
        return

    flushed = flush_silent_events("preceding_wake")
    if flushed:
        log.info("flushed %d deferred silent event(s) ahead of a wake", flushed)

    _send_channel(str(event.get("content", "")), _channel_meta(event))
