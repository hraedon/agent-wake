"""HTTP ingest for wake events.

Spec reference: v1-daemon-spec.md §7.  Translated from the legacy
``adapters/claude/.../ingest.py`` (stdlib ``http.server`` → ``aiohttp``).
Semantics preserved exactly; the only addition is that after a successful
gate + dedupe the handler calls ``Router.deliver(event)``.
"""

import asyncio
import json
import logging
from collections import deque
from typing import TYPE_CHECKING, Any

from aiohttp import web
from ulid import ULID

from .gating import check_trigger_identity, verify_signature, verify_signature_any

if TYPE_CHECKING:
    from .delivery import HumanDelivery
    from .router import Router
    from .secrets.resolver import SecretResolver
    from .socket_server import SocketServer
    from .store import WakeStore

log = logging.getLogger("agent_waked.ingest")

# How long shutdown waits for in-flight human deliveries before cancelling
# them. Deliberately shorter than a full webhook retry ladder (1s+4s+16s plus
# per-attempt timeouts): a delivery still in backoff when the daemon is asked
# to stop is cancelled, and ``HumanDelivery.deliver`` dead-letters on
# cancellation, so the alert is preserved durably rather than by making
# shutdown hang for a minute. Must stay below ``main._DRAIN_TIMEOUT``, which
# bounds the whole ``runner.cleanup()`` this drain runs inside.
_DELIVERY_DRAIN_TIMEOUT = 3.0


class SourceMismatchError(ValueError):
    """Raised when the body's claimed source does not match the authenticated header source.

    Carries the offending values for warn-level logging, but the HTTP layer
    must NOT surface them in the response body (information leak).
    """

    def __init__(self, header_source: str, body_source: str):
        super().__init__("source mismatch")
        self.header_source = header_source
        self.body_source = body_source


class UnsupportedVersionError(ValueError):
    """Raised when the body carries an unknown ``v`` field (schema violation).

    Per the wake-event schema, an envelope with ``v`` set must be a known
    version. Silently wrapping it as a v0 webhook (the prior behaviour) was a
    correctness bug (BC-010): a sender that upgrades to a newer envelope
    version would have its events silently misinterpreted instead of rejected
    with an explicit error. Carries the offending version for logging.
    """

    def __init__(self, version: object):
        super().__init__(f"unsupported wake event version: {version!r}")
        self.version = version


class Dedupe:
    """Replay window over ``event_id``.

    Two backings:

    * **durable** — when a :class:`~agent_waked.store.WakeStore` is supplied,
      every check is a single atomic ``INSERT OR IGNORE`` against the store's
      ``dedupe`` table.  The window therefore survives a daemon restart, which
      is the whole point of BC-WAKE-004: before this, killing the daemon
      re-admitted every replay the sender still held.  Retention and bounding
      are the store's job (TTL + row cap, see ``store.prune``).
    * **in-memory 4096-id FIFO** — the v0 behaviour, retained as the fallback
      when no store is configured (unit tests that build the app directly, and
      any deployment that deliberately opts out of on-disk state).
    """

    def __init__(self, max_size: int = 4096, store: "WakeStore | None" = None):
        self._seen: set[str] = set()
        self._order: deque[str] = deque(maxlen=max_size)
        self._max = max_size
        self._store = store

    @property
    def durable(self) -> bool:
        return self._store is not None

    def check(self, event_id: str, source: str = "") -> bool:
        if self._store is not None:
            return self._store.check_and_record_event(event_id, source)
        if event_id in self._seen:
            return True
        if len(self._order) == self._max:
            evicted = self._order[0]
            self._seen.discard(evicted)
        self._order.append(event_id)
        self._seen.add(event_id)
        return False


def _build_wake_event(body: bytes, source: str, event_id: str | None) -> dict[str, Any]:
    """Parse body. If it already looks like a v0 wake event, pass through.
    Otherwise wrap arbitrary JSON as a webhook event.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, dict):
        # Known-version check (BC-010): an envelope that declares a ``v`` we
        # don't understand MUST be rejected, never silently re-wrapped —
        # regardless of whether the rest of the envelope is complete. Without
        # this guard, a sender upgrading to v1 with an incomplete body would
        # have its declared version silently discarded and the body
        # misinterpreted as an opaque webhook.
        version = payload.get("v", 0)
        # Strict type + value check: bool is a subclass of int in Python
        # (``False == 0``), so an explicit bool exclusion keeps ``v: false``
        # and floats (``v: 0.0``) from sneaking through as v0.
        if not isinstance(version, int) or isinstance(version, bool) or version != 0:
            raise UnsupportedVersionError(version)
        if "event_id" in payload:
            claimed_source = payload.get("source")
            if claimed_source is not None and claimed_source != source:
                raise SourceMismatchError(source, str(claimed_source))
            if "source" not in payload:
                payload["source"] = source
            # Ensure minimum viable event structure
            payload.setdefault("kind", "webhook")
            payload.setdefault("content", "")
            payload.setdefault("meta", {})
            payload.setdefault("wake", True)
            return payload

    return {
        "v": 0,
        "event_id": event_id or str(ULID()),
        "source": source,
        "kind": "webhook",
        "content": body.decode("utf-8", errors="replace"),
        "meta": {},
        "wake": True,
    }


def _json_response(status: int, payload: dict[str, Any]) -> web.Response:
    return web.Response(
        status=status,
        content_type="application/json",
        body=json.dumps(payload).encode("utf-8"),
    )


def create_ingest_app(
    config: dict[str, Any],
    router: "Router",
    socket_server: "SocketServer | None" = None,
    version: str = "0.1.0",
    resolver: "SecretResolver | None" = None,
    delivery: "HumanDelivery | None" = None,
    store: "WakeStore | None" = None,
) -> web.Application:
    dedupe = Dedupe(store=store)
    # RUF006: an ``ensure_future`` task nobody holds a reference to can be
    # garbage-collected mid-flight, silently dropping the delivery. Same
    # treatment as ``Router._background_tasks``. The set is also what lets
    # shutdown drain in-flight deliveries before the store closes.
    delivery_tasks: set[asyncio.Task[Any]] = set()

    def _on_delivery_done(task: asyncio.Task[Any]) -> None:
        delivery_tasks.discard(task)
        if task.cancelled():
            # ``task.exception()`` *raises* CancelledError on a cancelled
            # task, so this guard is not cosmetic: without it every shutdown
            # raised out of the done-callback. Cancellation is not a lost
            # alert — HumanDelivery.deliver dead-letters on its way out.
            log.warning("human delivery task cancelled (shutdown?)")
            return
        exc = task.exception()
        if exc is not None:
            # exc_info=exc, not log.exception(): this callback runs outside
            # any except block, where implicit exc_info is empty and the
            # traceback — the only reason to log here — is lost.
            log.error("human delivery task crashed: %s", exc, exc_info=exc)

    async def _drain_delivery_tasks(_app: web.Application) -> None:
        """Let in-flight human deliveries finish before the store closes.

        Registered on ``app.on_shutdown``, so ``AppRunner.cleanup()`` runs it
        — and ``main._run`` closes the store *after* that cleanup. Anything
        still in retry backoff when the grace period expires is cancelled,
        which routes it to the dead-letter arm in ``HumanDelivery.deliver``.
        """
        if not delivery_tasks:
            return
        pending = list(delivery_tasks)
        log.info(
            "draining %d in-flight human deliver%s (grace %.0fs)",
            len(pending),
            "y" if len(pending) == 1 else "ies",
            _DELIVERY_DRAIN_TIMEOUT,
        )
        _, still_running = await asyncio.wait(
            pending, timeout=_DELIVERY_DRAIN_TIMEOUT
        )
        if not still_running:
            return
        log.warning(
            "cancelling %d human deliver%s still in retry backoff; they are "
            "dead-lettered for redrive",
            len(still_running),
            "y" if len(still_running) == 1 else "ies",
        )
        for task in still_running:
            task.cancel()
        # Wait for the cancellations to actually land: the dead-letter write
        # happens in the tasks' CancelledError handlers, and the store must
        # still be open when it does.
        await asyncio.wait(still_running)

    async def post_root(request: web.Request) -> web.Response:
        raw_body = await request.read()
        source = request.headers.get("X-AgentWake-Source", "")
        signature = request.headers.get("X-AgentWake-Signature", "")
        event_id_header = request.headers.get("X-AgentWake-Event-Id")

        source_cfg = config.get("sources", {}).get(source)
        if not source_cfg:
            return _json_response(
                403, {"error": "unknown source or invalid signature"}
            )

        # Resolve secrets: prefer resolver + secret_uris; fall back to
        # legacy inline "secret" bytes for tests that build config directly.
        if resolver is not None and "secret_uris" in source_cfg:
            try:
                secrets = await resolver.resolve_all(source_cfg)
            except Exception:
                log.exception("failed to resolve secrets for source %r", source)
                return _json_response(500, {"error": "internal error"})
            verified = verify_signature_any(raw_body, secrets, signature)
        else:
            # Legacy / test path: direct bytes in source_cfg["secret"]
            verified = verify_signature(raw_body, source_cfg["secret"], signature)

        if not verified:
            return _json_response(
                403, {"error": "unknown source or invalid signature"}
            )

        # Identity gating: check sender identity against source allowlist.
        sender_identity = request.headers.get("X-AgentWake-Identity")
        identity_err = check_trigger_identity(source_cfg, sender_identity)
        if identity_err:
            log.warning(
                "identity check failed source=%s identity=%r reason=%s",
                source,
                sender_identity,
                identity_err,
            )
            return _json_response(
                403, {"error": "unknown source or invalid signature"}
            )

        try:
            event = _build_wake_event(raw_body, source, event_id_header)
        except SourceMismatchError as e:
            log.warning(
                "source mismatch: header=%r body=%r",
                e.header_source,
                e.body_source,
            )
            return _json_response(403, {"error": "unknown source or invalid signature"})
        except UnsupportedVersionError as e:
            log.warning(
                "rejected event with unsupported version %r source=%s",
                e.version,
                source,
            )
            # 400 is the correct client-error code; do not echo the value.
            return _json_response(400, {"error": "unsupported wake event version"})

        event_id = event["event_id"]

        # Stamp trigger_identity in meta if the source has a principal_id.
        principal_id = source_cfg.get("principal_id")
        if principal_id:
            event.setdefault("meta", {})["trigger_identity"] = principal_id

        if dedupe.check(event_id, source):
            return _json_response(202, {"status": "duplicate", "event_id": event_id})

        delivery_result: dict[str, Any] | None = None
        if delivery is not None:
            meta = event.get("meta", {})
            if isinstance(meta, dict) and meta.get("target"):
                principal_id = str(meta["target"])
                if delivery.resolve_principal(principal_id) is None:
                    delivery.health.record_unknown_principal(principal_id)
                    log.warning(
                        "delivery rejected unknown principal=%s source=%s "
                        "event_id=%s",
                        principal_id,
                        source,
                        event_id,
                    )
                    return _json_response(
                        422,
                        {
                            "error": "unknown principal",
                            "principal_id": principal_id,
                            "event_id": event_id,
                        },
                    )
                # Delivery authorization (Plan 005): a source may only deliver
                # to principals it explicitly declares in allowed_target_principals.
                # Default-deny — an authenticated source cannot steer deliveries
                # to arbitrary principals.
                allowed_targets = source_cfg.get("allowed_target_principals")
                if (
                    not isinstance(allowed_targets, list)
                    or principal_id not in allowed_targets
                ):
                    log.warning(
                        "delivery denied source=%s target=%s event_id=%s "
                        "(not in allowed_target_principals)",
                        source,
                        principal_id,
                        event_id,
                    )
                    return _json_response(
                        403,
                        {
                            "error": "source not authorized to deliver to principal",
                            "source": source,
                            "principal_id": principal_id,
                            "event_id": event_id,
                        },
                    )
                task = asyncio.ensure_future(delivery.deliver(event))
                delivery_tasks.add(task)
                task.add_done_callback(_on_delivery_done)
                delivery_result = {
                    "status": "dispatched",
                    "principal_id": principal_id,
                }

        result = await router.deliver(event)
        if result in ("no_subscriber", "queued_next_session"):
            # ``queued_next_session``: no live session accepted the event, but
            # durable delivery was requested, so the daemon has persisted it and
            # will hand it to the next session that subscribes for this source.
            response: dict[str, Any] = {
                "status": result,
                "event_id": event_id,
            }
            if delivery_result is not None:
                response["delivery"] = delivery_result
            return _json_response(202, response)

        response = {"status": "queued", "event_id": event_id}
        if delivery_result is not None:
            response["delivery"] = delivery_result
        return _json_response(202, response)

    async def default_handler(request: web.Request) -> web.Response:
        return _json_response(404, {"error": "not found"})

    async def health_handler(request: web.Request) -> web.Response:
        body: dict[str, Any] = {"status": "ok", "version": version}
        if socket_server is not None:
            body["adapters"] = len(socket_server.connections)
        if delivery is not None:
            body["delivery"] = delivery.health.summary()
        if store is not None:
            body["store"] = {
                "durable": True,
                "pending": store.pending_count(),
                "dead_letter": store.dead_letter_count(),
            }
        return _json_response(200, body)

    app = web.Application()
    # The ignore below is an upstream typing mismatch: aiohttp declares its
    # signals as ``Signal[Callable[[Application], Awaitable[None]]]``, but
    # aiosignal's Signal is now variadic (``Signal[*Ts]``), so that declared
    # element type collapses to a callable *taking* a handler. The runtime
    # contract is the documented one.
    app.on_shutdown.append(_drain_delivery_tasks)  # type: ignore[arg-type]
    app.router.add_get("/", health_handler)
    app.router.add_post("/", post_root)
    app.router.add_route("*", "/{path:.*}", default_handler)
    return app
