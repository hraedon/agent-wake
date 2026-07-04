"""HTTP ingest for wake events.

Spec reference: v1-daemon-spec.md §7.  Translated from the legacy
``adapters/claude/.../ingest.py`` (stdlib ``http.server`` → ``aiohttp``).
Semantics preserved exactly; the only addition is that after a successful
gate + dedupe the handler calls ``Router.deliver(event)``.
"""

import json
import logging
from collections import deque
from typing import TYPE_CHECKING, Any

from aiohttp import web
from ulid import ULID

from .gating import check_trigger_identity, verify_signature, verify_signature_any

if TYPE_CHECKING:
    from .router import Router
    from .secrets.resolver import SecretResolver
    from .socket_server import SocketServer

log = logging.getLogger("agent_waked.ingest")


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
    """In-memory 4096-id FIFO dedupe window."""

    def __init__(self, max_size: int = 4096):
        self._seen: set[str] = set()
        self._order: deque[str] = deque(maxlen=max_size)
        self._max = max_size

    def check(self, event_id: str) -> bool:
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
) -> web.Application:
    dedupe = Dedupe()

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

        if dedupe.check(event_id):
            return _json_response(202, {"status": "duplicate", "event_id": event_id})

        result = await router.deliver(event)
        if result == "no_subscriber":
            return _json_response(
                202, {"status": "no_subscriber", "event_id": event_id}
            )

        return _json_response(202, {"status": "queued", "event_id": event_id})

    async def default_handler(request: web.Request) -> web.Response:
        return _json_response(404, {"error": "not found"})

    async def health_handler(request: web.Request) -> web.Response:
        body: dict[str, Any] = {"status": "ok", "version": version}
        if socket_server is not None:
            body["adapters"] = len(socket_server.connections)
        return _json_response(200, body)

    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_post("/", post_root)
    app.router.add_route("*", "/{path:.*}", default_handler)
    return app
