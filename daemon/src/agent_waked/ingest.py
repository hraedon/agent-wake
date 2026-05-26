"""HTTP ingest for wake events.

Spec reference: v1-daemon-spec.md §7.  Translated from the legacy
``adapters/claude/.../ingest.py`` (stdlib ``http.server`` → ``aiohttp``).
Semantics preserved exactly; the only addition is that after a successful
gate + dedupe the handler calls ``Router.deliver(event)``.
"""

import json
import logging
from typing import TYPE_CHECKING

from aiohttp import web
from ulid import ULID

from .gating import verify_signature

if TYPE_CHECKING:
    from .router import Router

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


class Dedupe:
    """In-memory 256-id FIFO dedupe window."""

    def __init__(self, max_size: int = 256):
        self._ids: list[str] = []
        self._max = max_size

    def check(self, event_id: str) -> bool:
        if event_id in self._ids:
            return True
        self._ids.append(event_id)
        while len(self._ids) > self._max:
            self._ids.pop(0)
        return False


def _build_wake_event(body: bytes, source: str, event_id: str | None) -> dict:
    """Parse body. If it already looks like a v0 wake event, pass through.
    Otherwise wrap arbitrary JSON as a webhook event.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, dict) and payload.get("v") == 0 and "event_id" in payload:
        claimed_source = payload.get("source")
        if claimed_source is not None and claimed_source != source:
            raise SourceMismatchError(source, str(claimed_source))
        if "source" not in payload:
            payload["source"] = source
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


def _json_response(status: int, payload: dict) -> web.Response:
    return web.Response(
        status=status,
        content_type="application/json",
        body=json.dumps(payload).encode("utf-8"),
    )


def create_ingest_app(config: dict, router: "Router") -> web.Application:
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

        if not verify_signature(raw_body, source_cfg["secret"], signature):
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
            return _json_response(500, {"error": "source mismatch"})

        event_id = event["event_id"]

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

    app = web.Application()
    app.router.add_post("/", post_root)
    app.router.add_route("*", "/{path:.*}", default_handler)
    return app
