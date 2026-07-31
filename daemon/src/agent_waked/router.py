"""Source → adapter routing.

Spec reference: v1-daemon-spec.md §5.4.

The router holds a subscriber entry per connected adapter (keyed by
daemon-minted ``session_id``) and resolves inbound events to the
correct subscriber based on the daemon config's ``routing`` block.
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from ulid import ULID

from .socket_server import ClientConnection
from .store import PendingEvent, WakeStore

log = logging.getLogger("agent_waked.router")

_ACK_TIMEOUT = 30.0

# Delivery modes an event may request via ``meta.delivery``.
#   live_only     — v0 behaviour: if no session is subscribed, drop.
#   next_session  — durable: persist and hand to the next session for the source.
#   managed_session — reserved for Plan 006 Phase 2B (daemon-owned harness
#     process). The daemon queues it exactly like next_session; the difference
#     is which component drains it, so there is no second store.
_DURABLE_MODES = frozenset({"next_session", "managed_session"})
_LIVE_ONLY = "live_only"
VALID_DELIVERY_MODES = frozenset({_LIVE_ONLY}) | _DURABLE_MODES


class _Subscriber:
    __slots__ = ("adapter", "connection", "instance", "session_id", "sources")

    def __init__(
        self,
        session_id: str,
        adapter: str,
        instance: str,
        sources: list[str],
        connection: ClientConnection,
    ):
        self.session_id = session_id
        self.adapter = adapter
        self.instance = instance
        self.sources = sources
        self.connection = connection


class Router:
    def __init__(self, config: dict[str, Any], store: WakeStore | None = None):
        self._config = config
        self._store = store
        self._subscribers: dict[str, _Subscriber] = {}
        self._order: list[str] = []
        self._pending_acks: dict[str, asyncio.Future[str]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def _spawn_background(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # ── next-session delivery (Plan 006 Phase 1 / BC-WAKE-012) ───────────────

    def _delivery_mode(self, event: dict[str, Any]) -> str:
        """Resolve the delivery mode for *event*.

        Per-event ``meta.delivery`` wins; otherwise the daemon-wide
        ``state.default_delivery`` applies; otherwise ``live_only``, which
        preserves the documented v0 semantics ("wake hits live sessions only")
        for every deployment that has not opted in.
        """
        meta = event.get("meta")
        if isinstance(meta, dict):
            requested = meta.get("delivery")
            if isinstance(requested, str):
                if requested in VALID_DELIVERY_MODES:
                    return requested
                log.warning(
                    "unknown meta.delivery=%r on event_id=%s; treating as %s",
                    requested,
                    event.get("event_id"),
                    _LIVE_ONLY,
                )
                return _LIVE_ONLY
        state_cfg = self._config.get("state") or {}
        default = state_cfg.get("default_delivery", _LIVE_ONLY)
        return default if default in VALID_DELIVERY_MODES else _LIVE_ONLY

    def _undeliverable(self, event: dict[str, Any], reason: str) -> str:
        """Handle an event no live subscriber accepted.

        Returns the ingest status string. When the event asked for durable
        delivery and a store is configured, it is persisted for the next
        session; otherwise the v0 drop is preserved.
        """
        mode = self._delivery_mode(event)
        if mode in _DURABLE_MODES:
            if self._store is None:
                log.warning(
                    "event_id=%s requested delivery=%s but no durable store is "
                    "configured; dropping (%s)",
                    event.get("event_id"),
                    mode,
                    reason,
                )
                return "no_subscriber"
            if self._store.enqueue_pending(event):
                log.info(
                    "queued for next session source=%s event_id=%s mode=%s reason=%s",
                    event.get("source"),
                    event.get("event_id"),
                    mode,
                    reason,
                )
            else:
                log.warning(
                    "next-session enqueue refused source=%s event_id=%s "
                    "(duplicate or queue full)",
                    event.get("source"),
                    event.get("event_id"),
                )
            return "queued_next_session"
        log.info("%s source=%s", reason, event.get("source", ""))
        return "no_subscriber"

    async def drain_pending(self, session_id: str) -> int:
        """Deliver queued events to a session that has just subscribed.

        Called by the socket server immediately after ``hello_ack``. Only
        sources this subscriber would actually be routed (per the config's
        ``routing`` block) are drained, so a queued event cannot leak to an
        adapter the operator did not route it to.

        A claimed row is removed only when the adapter *acks* it — a nack or
        an ack timeout leaves it queued for the next session (at-least-once).
        Once ``pending_max_attempts`` deliveries have failed, the event is
        dead-lettered so it stops cycling and becomes operator-visible.
        """
        if self._store is None:
            return 0
        sub = self._subscribers.get(session_id)
        if sub is None:
            return 0
        drained = 0
        for source in sub.sources:
            if self._resolve(source) is not sub:
                continue
            for row in self._store.claim_pending(source):
                if await self._send_pending(sub, row):
                    drained += 1
        if drained:
            log.info(
                "drained %d queued event(s) to session_id=%s adapter=%s",
                drained,
                session_id,
                sub.adapter,
            )
        return drained

    async def _send_pending(self, sub: _Subscriber, row: PendingEvent) -> bool:
        assert self._store is not None
        if row.attempts > self._store.pending_max_attempts:
            self._store.dead_letter(
                kind="next_session",
                source=row.source,
                ref_id=row.event_id,
                payload=row.event,
                error=(
                    f"no ack after {row.attempts - 1} next-session delivery "
                    f"attempt(s)"
                ),
                attempts=row.attempts - 1,
            )
            self._store.delete_pending(row.row_id)
            return False
        ack_id = str(ULID())
        frame = {"type": "wake", "ack_id": ack_id, "event": row.event}
        fut = self._register_ack(ack_id)
        try:
            await sub.connection.send_frame(frame)
        except OSError as exc:
            log.warning(
                "next-session send failed session_id=%s event_id=%s error=%s",
                sub.session_id,
                row.event_id,
                exc,
            )
            self._pending_acks.pop(ack_id, None)
            self._evict(sub.session_id)
            return False
        self._spawn_background(
            self._wait_for_ack(
                row.source,
                ack_id,
                sub.session_id,
                fut,
                on_result=lambda result: self._settle_pending(row, result),
            )
        )
        return True

    def _settle_pending(self, row: PendingEvent, result: str | None) -> None:
        """Ack → done. Nack/timeout → leave queued for the next session."""
        if self._store is None:
            return
        if result == "ack":
            self._store.delete_pending(row.row_id)
            log.info(
                "next-session delivery acked source=%s event_id=%s",
                row.source,
                row.event_id,
            )
        else:
            log.warning(
                "next-session delivery unacked (%s) source=%s event_id=%s "
                "attempt=%d; remains queued",
                result or "timeout",
                row.source,
                row.event_id,
                row.attempts,
            )

    def subscribe(
        self,
        session_id: str,
        adapter: str,
        instance: str,
        sources: list[str],
        connection: ClientConnection,
    ) -> None:
        sub = _Subscriber(session_id, adapter, instance, sources, connection)
        self._subscribers[session_id] = sub
        self._order.append(session_id)

    def unsubscribe(self, session_id: str) -> None:
        self._subscribers.pop(session_id, None)
        try:
            self._order.remove(session_id)
        except ValueError:
            pass

    async def deliver(self, event: dict[str, Any]) -> str:
        source = event.get("source", "")
        target = self._resolve(source)
        if target is None:
            return self._undeliverable(event, "no_subscriber")
        ack_id = str(ULID())
        wake_frame = {"type": "wake", "ack_id": ack_id, "event": event}
        # Register the ack future *before* the send: an in-process adapter (or
        # a very fast one) can reply before ``send_frame`` returns, and a
        # future created afterwards would never see that ack.
        fut = self._register_ack(ack_id)
        try:
            await target.connection.send_frame(wake_frame)
        except OSError as exc:
            # BC-009: the adapter disconnected between _resolve() and the
            # send. OSError is the common ancestor of ConnectionError
            # (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)
            # and the generic transport errors asyncio surfaces from
            # StreamWriter.drain(). Re-resolving would race with the
            # socket_server's teardown, so treat this subscriber as dead,
            # evict it, and report no_subscriber rather than letting the
            # connection error propagate to the HTTP handler as a 500.
            #
            # NOTE: this turns a mid-send disconnect into a terminal
            # "no_subscriber" for the current event_id. dedupe.check()
            # already consumed the event_id upstream, so a same-id retry
            # is rejected as duplicate — this is the documented "wake hits
            # live sessions only" design (AGENTS.md); durable redelivery
            # is BC-WAKE-012.
            log.warning(
                "send_failed source=%s session_id=%s error=%s; evicting dead subscriber",
                source,
                target.session_id,
                exc,
            )
            self._pending_acks.pop(ack_id, None)
            self._evict(target.session_id)
            return self._undeliverable(event, "send_failed")
        log.info(
            "delivered source=%s ack_id=%s session_id=%s",
            source,
            ack_id,
            target.session_id,
        )

        # Track ack in background; don't block the HTTP response
        self._spawn_background(
            self._wait_for_ack(source, ack_id, target.session_id, fut)
        )

        return "queued"

    def _evict(self, session_id: str) -> None:
        """Remove a subscriber whose connection is dead (BC-009).

        The socket_server's _handleConnection finally-block also performs
        this cleanup when it observes the broken socket; calling it here as
        well is defensive and idempotent. Closing the writer prompts the
        socket_server's read loop to unblock on the next event-loop tick.
        """
        sub = self._subscribers.get(session_id)
        self.unsubscribe(session_id)
        if sub is not None:
            try:
                sub.connection.close()
            except Exception:
                pass

    def _register_ack(self, ack_id: str) -> "asyncio.Future[str]":
        """Create and register the future ``resolve_ack`` will complete."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._pending_acks[ack_id] = fut
        return fut

    async def _wait_for_ack(
        self,
        source: str,
        ack_id: str,
        session_id: str,
        fut: "asyncio.Future[str]",
        on_result: "Callable[[str | None], None] | None" = None,
    ) -> None:
        """Wait for ack/nack with timeout. Logs result; does not block deliver().

        *on_result* is invoked with ``"ack"`` / ``"nack"`` / ``None`` (timeout)
        so callers with durable state — the next-session queue — can settle the
        row exactly once, without the ack path knowing about the store.
        """
        outcome: str | None = None
        try:
            outcome = await asyncio.wait_for(fut, timeout=_ACK_TIMEOUT)
            log.info(
                "ack received source=%s ack_id=%s result=%s",
                source,
                ack_id,
                outcome,
            )
        except TimeoutError:
            log.warning(
                "ack timeout source=%s ack_id=%s session_id=%s timeout=%.0fs",
                source,
                ack_id,
                session_id,
                _ACK_TIMEOUT,
            )
        finally:
            self._pending_acks.pop(ack_id, None)
            if on_result is not None:
                try:
                    on_result(outcome)
                except Exception:
                    log.exception("ack callback failed ack_id=%s", ack_id)

    def resolve_ack(self, ack_id: str, frame_type: str) -> None:
        """Resolve a pending ack/nack future. Called by the socket server."""
        fut = self._pending_acks.get(ack_id)
        if fut is not None and not fut.done():
            fut.set_result(frame_type)

    def accepted_sources_for(self, adapter: str, requested: list[str]) -> list[str]:
        """Compute the intersection used in ``hello_ack.accepted_sources``."""
        routing = self._config.get("routing", {})
        configured = set(self._config.get("sources", {}).keys())
        accepted: list[str] = []
        for s in requested:
            if s not in configured:
                continue
            route = routing.get(s)
            if route and route.get("adapter"):
                if route["adapter"] == adapter:
                    accepted.append(s)
            else:
                accepted.append(s)
        return accepted

    def _resolve(self, source: str) -> _Subscriber | None:
        routing = self._config.get("routing", {})
        route = routing.get(source)

        if route and route.get("adapter"):
            adapter_name = route["adapter"]
            for sub in self._subscribers.values():
                if sub.adapter == adapter_name and source in sub.sources:
                    return sub
            return None

        for sid in reversed(self._order):
            if sid not in self._subscribers:
                continue
            sub = self._subscribers[sid]
            if source in sub.sources:
                return sub
        return None
