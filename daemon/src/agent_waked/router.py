"""Sender → principal → destination routing.

Spec reference: v1-daemon-spec.md §5.4, restructured by WI-006.

What changed and why
--------------------
v1 resolved an event to **one** subscriber by looking up ``routing[source]``
and matching an adapter name.  A source name was simultaneously the credential,
the addressee and the routing key, so three things were impossible by
construction:

* addressing one *session* of a multi-session adapter (BC-001) — the routing
  key had no session dimension, so the opencode adapter broadcast;
* addressing more than one place (BC-WAKE-022) — a name-keyed map holds one
  adapter per key;
* queueing durably *per addressee* (BC-WAKE-012) — the pending queue was keyed
  on ``source``, so a drain handed every queued event for a source to whichever
  session connected first.

Resolution is now: the sender's routes give the destination set;
``meta.destination`` / ``meta.principal`` may narrow it (never widen it); every
resolved destination gets its own frame, its own ack and its own durable-queue
row.  A destination carries the session it addresses, so "deliver to session X
and not its siblings" is a property of the config rather than of adapter luck.
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from ulid import ULID

from . import addressing
from .addressing import AddressingError, Destination
from .socket_server import ClientConnection
from .store import PendingEvent, WakeStore

log = logging.getLogger("agent_waked.router")

_ACK_TIMEOUT = 30.0

# Delivery modes an event may request via ``meta.delivery``.
#   live_only     — v0 behaviour: if no session is subscribed, drop.
#   next_session  — durable: persist and hand to the next session for the
#     addressed destination.
#   managed_session — reserved for Plan 006 Phase 2B (daemon-owned harness
#     process). The daemon queues it exactly like next_session; the difference
#     is which component drains it, so there is no second store.
_DURABLE_MODES = frozenset({"next_session", "managed_session"})
_LIVE_ONLY = "live_only"
VALID_DELIVERY_MODES = frozenset({_LIVE_ONLY}) | _DURABLE_MODES

#: Status strings ``deliver`` may return that are authorization refusals rather
#: than delivery outcomes.  Ingest maps these to 403.
REFUSAL_STATUSES = frozenset({"destination_not_routed", "principal_not_routed"})


class _Subscriber:
    """One connected adapter and the destinations it claims.

    ``sources`` is kept because it is what a legacy ``hello`` sends and what
    ``hello_ack.accepted_sources`` echoes back; ``destinations`` is the set the
    router actually addresses.  For a legacy hello the two are the same names —
    see ``addressing.derive_from_legacy`` for why that is deliberate.
    """

    __slots__ = (
        "adapter",
        "connection",
        "destinations",
        "in_flight",
        "instance",
        "session_id",
        "sources",
    )

    def __init__(
        self,
        session_id: str,
        adapter: str,
        instance: str,
        sources: list[str],
        connection: ClientConnection,
        destinations: list[str] | None = None,
    ):
        self.session_id = session_id
        self.adapter = adapter
        self.instance = instance
        self.sources = sources
        self.destinations = list(destinations if destinations is not None else sources)
        self.connection = connection
        #: Wake frames handed to this subscriber that have neither acked nor
        #: nacked. The *delivery* lifecycle, which the heartbeat consults before
        #: reaping a connection for a missed pong (BC-011).
        self.in_flight = 0


class Router:
    def __init__(self, config: dict[str, Any], store: WakeStore | None = None):
        self._config = config
        self._store = store
        self._subscribers: dict[str, _Subscriber] = {}
        self._order: list[str] = []
        self._pending_acks: dict[str, asyncio.Future[str]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()

    @property
    def config(self) -> dict[str, Any]:
        """The live config dict.

        Exposed because the socket server needs the destination table to enforce
        per-destination caps, and reaching into ``_config`` from another module
        is the kind of accident that survives a reload only by luck: ``main``
        updates this dict **in place** on SIGHUP, so anything that copies it
        silently keeps the old routing.
        """
        return self._config

    def _spawn_background(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # ── addressing ───────────────────────────────────────────────────────────

    def destinations_for_hello(
        self, adapter: str, sources: list[str], claimed: list[str] | None
    ) -> list[str]:
        """Destination names a connecting adapter is allowed to serve.

        Two spellings converge here.  An adapter that names ``destinations``
        explicitly gets those, filtered to ones that exist and whose configured
        adapter matches — a claim on somebody else's destination must not be
        honoured just because the socket peer asked nicely.  An adapter that
        only sends ``filters.sources`` (the deployed Claude adapter) gets the
        destinations those source names resolve to, which under the legacy
        translation are the same names.
        """
        table = addressing.destination_table(self._config)
        out: list[str] = []
        names = claimed if claimed else sources
        for name in names:
            dest = table.get(name)
            if dest is None:
                if claimed:
                    log.warning(
                        "adapter=%s claimed unknown destination %r", adapter, name
                    )
                continue
            if not dest.accepts_adapter(adapter):
                if claimed:
                    log.warning(
                        "adapter=%s claimed destination %r which is configured "
                        "for adapter %r",
                        adapter,
                        name,
                        dest.adapter,
                    )
                continue
            if name not in out:
                out.append(name)
        return out

    def destination_load(self, name: str, *, exclude: str | None = None) -> int:
        """How many live subscribers currently claim destination *name*.

        The count BC-WAKE-010 needed and had nowhere to live: with no
        per-destination object there was only one global connection cap, so a
        chatty adapter could occupy every slot and starve the others.
        """
        return sum(
            1
            for sid, sub in self._subscribers.items()
            if sid != exclude and name in sub.destinations
        )

    def _resolve_addressing(
        self, event: dict[str, Any]
    ) -> tuple[list[Destination], str | None]:
        """Resolve the destination set for *event*.

        Returns ``(destinations, error_code)``.  A non-None error code is an
        authorization failure the caller must surface, not an empty delivery: a
        sender that could tell "not routed to you" from "nobody is listening"
        could enumerate the box's destinations.
        """
        sender_name = str(event.get("source", ""))
        meta = event.get("meta")
        requested_dest = None
        requested_principal = None
        if isinstance(meta, dict):
            raw_dest = meta.get("destination")
            if isinstance(raw_dest, str) and raw_dest:
                requested_dest = raw_dest
            raw_principal = meta.get("principal")
            if isinstance(raw_principal, str) and raw_principal:
                requested_principal = raw_principal
        try:
            return (
                addressing.resolve(
                    self._config,
                    sender_name,
                    destination_name=requested_dest,
                    principal_id=requested_principal,
                ),
                None,
            )
        except AddressingError as exc:
            log.warning(
                "addressing refused sender=%s event_id=%s code=%s: %s",
                sender_name,
                event.get("event_id"),
                exc.code,
                exc,
            )
            return [], exc.code

    def _subscriber_for(self, dest: Destination) -> _Subscriber | None:
        """The live subscriber serving *dest*, most-recently-connected first.

        Most-recent-first preserves the v1 §5.4 rule-2 tiebreak for a
        destination several connections claim.  Session scoping makes that tie
        rare rather than routine: two sessions of one adapter now claim two
        different destinations, so an event for one is never eligible for the
        other.
        """
        for sid in reversed(self._order):
            sub = self._subscribers.get(sid)
            if sub is None:
                continue
            if dest.name not in sub.destinations:
                continue
            if not dest.accepts_adapter(sub.adapter):
                continue
            return sub
        return None

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

    def _undeliverable(
        self,
        event: dict[str, Any],
        reason: str,
        destination: str | None = None,
    ) -> str:
        """Handle an event no live subscriber accepted.

        Returns the ingest status string. When the event asked for durable
        delivery and a store is configured, it is persisted **for that
        destination**; otherwise the v0 drop is preserved.

        Per-destination is the point (BC-WAKE-012): a queue keyed only on the
        sender hands a session-scoped event to whichever session connects next,
        which is the same fan-out bug as BC-001 with a delay in front of it.
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
            if self._store.enqueue_pending(event, destination=destination):
                log.info(
                    "queued for next session sender=%s destination=%s "
                    "event_id=%s mode=%s reason=%s",
                    event.get("source"),
                    destination,
                    event.get("event_id"),
                    mode,
                    reason,
                )
            else:
                log.warning(
                    "next-session enqueue refused sender=%s destination=%s "
                    "event_id=%s (duplicate or queue full)",
                    event.get("source"),
                    destination,
                    event.get("event_id"),
                )
            return "queued_next_session"
        log.info(
            "%s sender=%s destination=%s",
            reason,
            event.get("source", ""),
            destination,
        )
        return "no_subscriber"

    async def drain_pending(self, session_id: str) -> int:
        """Deliver queued events to a session that has just subscribed.

        Called by the socket server immediately after ``hello_ack``. Only the
        destinations this subscriber actually serves are drained, so a queued
        event cannot leak to an adapter — or a sibling session — the operator
        did not address it to.

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
        table = addressing.destination_table(self._config)
        drained = 0
        for name in sub.destinations:
            dest = table.get(name)
            if dest is None:
                continue
            if self._subscriber_for(dest) is not sub:
                continue
            for row in self._store.claim_pending(name):
                if await self._send_pending(sub, row, dest):
                    drained += 1
        if drained:
            log.info(
                "drained %d queued event(s) to session_id=%s adapter=%s",
                drained,
                session_id,
                sub.adapter,
            )
        return drained

    async def _send_pending(
        self, sub: _Subscriber, row: PendingEvent, dest: Destination
    ) -> bool:
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
        frame = {
            "type": "wake",
            "ack_id": ack_id,
            "destination": dest.to_frame(),
            "event": row.event,
        }
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
        sub.in_flight += 1
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
        destinations: list[str] | None = None,
    ) -> None:
        sub = _Subscriber(
            session_id, adapter, instance, sources, connection, destinations
        )
        self._subscribers[session_id] = sub
        self._order.append(session_id)

    def unsubscribe(self, session_id: str) -> None:
        self._subscribers.pop(session_id, None)
        try:
            self._order.remove(session_id)
        except ValueError:
            pass

    async def deliver(self, event: dict[str, Any]) -> str:
        """Deliver *event* to every destination it addresses.

        Returns the ingest status string:

        * ``"queued"`` — at least one live subscriber took a copy;
        * ``"queued_next_session"`` — nothing was live but the event asked for
          durable delivery and was persisted per destination;
        * ``"no_subscriber"`` — nothing live and nothing durable;
        * ``"destination_not_routed"`` / ``"principal_not_routed"`` — the event
          narrowed to somewhere this sender has no route to. An authorization
          answer, which ingest maps to 403.

        A partial fan-out reports ``queued``. Some destinations landing while
        others are queued or dropped is the *normal* case for BC-WAKE-022 — an
        alert going to a live Claude session and an idle opencode one — and
        calling that a failure would make senders retry deliveries that already
        landed.
        """
        sender_name = str(event.get("source", ""))
        destinations, error = self._resolve_addressing(event)
        if error is not None:
            return error
        if not destinations:
            # No route at all. Deliberately the v1 shape — a 202 carrying
            # no_subscriber — because a sender must not be able to tell an
            # unrouted name from an idle one.
            return self._undeliverable(event, "no_route")

        delivered = 0
        undeliverable: list[str] = []
        for dest in destinations:
            sub = self._subscriber_for(dest)
            if sub is None:
                undeliverable.append(dest.name)
                continue
            if await self._send_to(sub, dest, event, sender_name):
                delivered += 1
            else:
                undeliverable.append(dest.name)

        statuses = [
            self._undeliverable(event, "no_subscriber", destination=name)
            for name in undeliverable
        ]

        if delivered:
            return "queued"
        if "queued_next_session" in statuses:
            return "queued_next_session"
        return "no_subscriber"

    async def _send_to(
        self,
        sub: _Subscriber,
        dest: Destination,
        event: dict[str, Any],
        sender_name: str,
    ) -> bool:
        ack_id = str(ULID())
        wake_frame = {
            "type": "wake",
            "ack_id": ack_id,
            "destination": dest.to_frame(),
            "event": event,
        }
        # Register the ack future *before* the send: an in-process adapter (or
        # a very fast one) can reply before ``send_frame`` returns, and a
        # future created afterwards would never see that ack.
        fut = self._register_ack(ack_id)
        try:
            await sub.connection.send_frame(wake_frame)
        except OSError as exc:
            # BC-009: the adapter disconnected between resolution and the send.
            # OSError is the common ancestor of ConnectionError
            # (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)
            # and the generic transport errors asyncio surfaces from
            # StreamWriter.drain(). Re-resolving would race with the
            # socket_server's teardown, so treat this subscriber as dead, evict
            # it, and report the destination undeliverable rather than letting
            # the connection error propagate to the HTTP handler as a 500.
            #
            # NOTE: dedupe.check() already consumed the event_id upstream, so a
            # same-id retry is rejected as duplicate — the documented "wake hits
            # live sessions only" design (AGENTS.md) unless the event asked for
            # durable delivery, in which case the caller queues it.
            log.warning(
                "send_failed sender=%s destination=%s session_id=%s error=%s; "
                "evicting dead subscriber",
                sender_name,
                dest.name,
                sub.session_id,
                exc,
            )
            self._pending_acks.pop(ack_id, None)
            self._evict(sub.session_id)
            return False
        sub.in_flight += 1
        log.info(
            "delivered sender=%s destination=%s session=%s ack_id=%s session_id=%s",
            sender_name,
            dest.name,
            dest.session,
            ack_id,
            sub.session_id,
        )

        # Track ack in background; don't block the HTTP response
        self._spawn_background(
            self._wait_for_ack(sender_name, ack_id, sub.session_id, fut)
        )
        return True

    def _evict(self, session_id: str) -> None:
        """Remove a subscriber whose connection is dead (BC-009).

        The socket_server's _handle_connection finally-block also performs
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

An ack or nack decrements the subscriber's in-flight count, which is what
        lets the heartbeat tell a busy adapter from a dead one (BC-011).  An ack
        **timeout** deliberately does *not*: the adapter may still be mid-turn,
        and the whole point of BC-011 is that a liveness check must not infer
        death from a delivery deadline.  The heartbeat bounds its own tolerance
        (``MAX_BUSY_MISSED_PONGS``) so a peer that genuinely died mid-delivery is
        still reaped.
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
            if outcome is not None:
                self._end_delivery(session_id)
            if on_result is not None:
                try:
                    on_result(outcome)
                except Exception:
                    log.exception("ack callback failed ack_id=%s", ack_id)

    def in_flight_for(self, session_id: str) -> int:
        """Wake frames this session owes an ack for.

        The socket server's heartbeat asks this before closing a connection that
        missed a pong (BC-011): an adapter that is busy running the turn the
        daemon just asked it to run is not a dead one.
        """
        sub = self._subscribers.get(session_id)
        return sub.in_flight if sub is not None else 0

    def _end_delivery(self, session_id: str) -> None:
        sub = self._subscribers.get(session_id)
        if sub is not None and sub.in_flight > 0:
            sub.in_flight -= 1

    def resolve_ack(self, ack_id: str, frame_type: str) -> None:
        """Resolve a pending ack/nack future. Called by the socket server."""
        fut = self._pending_acks.get(ack_id)
        if fut is not None and not fut.done():
            fut.set_result(frame_type)

    def accepted_sources_for(self, adapter: str, requested: list[str]) -> list[str]:
        """Compute the intersection used in ``hello_ack.accepted_sources``.

        Kept on the source vocabulary because it is the field a legacy adapter
        reads.  Under the v2 model the question is "does a destination with this
        name exist that this adapter may serve", and the legacy translation makes
        source names and destination names identical — so the answer is exactly
        the one v1 gave for every already-deployed config.
        """
        table = addressing.destination_table(self._config)
        accepted: list[str] = []
        for s in requested:
            dest = table.get(s)
            if dest is None:
                continue
            if not dest.accepts_adapter(adapter):
                continue
            accepted.append(s)
        return accepted
