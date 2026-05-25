"""HTTP listener that receives wake events and forwards them to the channel."""

import json
import http.server
import threading
from typing import Callable

import ulid

import sys

from .gating import verify_signature

# In-memory dedupe window: last 256 event_ids (FIFO)
_recent_event_ids: list[str] = []
_MAX_DEDUPE = 256
_DEDUPE_LOCK = threading.Lock()


def _generate_event_id() -> str:
    return str(ulid.new())


def _is_duplicate(event_id: str) -> bool:
    with _DEDUPE_LOCK:
        if event_id in _recent_event_ids:
            return True
        _recent_event_ids.append(event_id)
        while len(_recent_event_ids) > _MAX_DEDUPE:
            _recent_event_ids.pop(0)
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
        # Pass-through: already a v0 wake event.
        # Enforce that the body's claimed source matches the authenticated
        # source from the header to prevent source spoofing.
        claimed_source = payload.get("source")
        if claimed_source is not None and claimed_source != source:
            raise ValueError(
                f"event source mismatch: header says {source!r} but body says {claimed_source!r}"
            )
        return payload

    # Wrap arbitrary JSON
    return {
        "v": 0,
        "event_id": event_id or _generate_event_id(),
        "source": source,
        "kind": "webhook",
        "content": body.decode("utf-8", errors="replace"),
        "meta": {},
        "wake": True,
    }


class _Callback:
    """Holds a callable so it is not bound as a method when accessed via self."""
    __slots__ = ("fn",)

    def __init__(self, fn: Callable[[dict], None]):
        self.fn = fn

    def __call__(self, event: dict) -> None:
        self.fn(event)


class IngestHandler(http.server.BaseHTTPRequestHandler):
    """Handles POST / for incoming wake events."""

    config: dict
    emit_callback: _Callback

    def log_message(self, format, *args):
        # Suppress default logging to stderr
        pass

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/" and self.path != "/permission/verdict":
            self._send_json(404, {"error": "not found"})
            return

        # Permission verdict path (special, no HMAC)
        if self.path == "/permission/verdict":
            self._handle_permission_verdict()
            return

        content_length = self.headers.get("Content-Length")
        if not content_length:
            self._send_json(400, {"error": "missing Content-Length"})
            return

        raw_body = self.rfile.read(int(content_length))
        source = self.headers.get("X-AgentWake-Source", "")
        signature = self.headers.get("X-AgentWake-Signature", "")
        event_id_header = self.headers.get("X-AgentWake-Event-Id")

        cfg = self.config
        source_cfg = cfg.get("sources", {}).get(source)

        if not source_cfg:
            self._send_json(403, {"error": "unknown source or invalid signature"})
            return

        if not verify_signature(raw_body, source_cfg["secret"], signature):
            self._send_json(403, {"error": "unknown source or invalid signature"})
            return

        event = _build_wake_event(raw_body, source, event_id_header)
        event_id = event["event_id"]

        if _is_duplicate(event_id):
            self._send_json(202, {"status": "duplicate", "event_id": event_id})
            return

        try:
            self.emit_callback(event)
        except Exception as e:
            self._send_json(500, {"error": f"forward failed: {e}"})
            return

        self._send_json(202, {"status": "queued", "event_id": event_id})

    def _handle_permission_verdict(self):
        content_length = self.headers.get("Content-Length")
        if not content_length:
            self._send_json(400, {"error": "missing Content-Length"})
            return
        raw_body = self.rfile.read(int(content_length))
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON"})
            return

        from .permission import handle_verdict
        try:
            handle_verdict(payload)
        except Exception as e:
            self._send_json(500, {"error": f"verdict handler failed: {e}"})
            return
        self._send_json(202, {"status": "verdict received"})


class IngestServer(http.server.HTTPServer):
    def __init__(self, server_address, handler_class, config: dict, emit_callback: Callable[[dict], None]):
        super().__init__(server_address, handler_class)
        self.handler = handler_class
        self.handler.config = config
        self.handler.emit_callback = _Callback(emit_callback)


def start_listener(config: dict, emit_callback: Callable[[dict], None], return_server: bool = False) -> threading.Thread | IngestServer:
    """Start the HTTP ingest listener in a background thread.

    Returns the Thread object by default. If return_server is True, returns
    the IngestServer instance (which has a .shutdown() method for graceful
    teardown).
    """
    server = IngestServer(
        (config["host"], config["port"]),
        IngestHandler,
        config,
        emit_callback,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server if return_server else thread
