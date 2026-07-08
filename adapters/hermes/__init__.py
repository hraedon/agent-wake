"""agent-wake Hermes plugin.

Subscribes to wake events (HTTP webhook listener) and delivers them via
Hermes's in-session context injection hooks.

Config is read from the ``AGENT_WAKE_CONFIG`` env var (path to the
agent-waked config file). The plugin starts a background HTTP listener
on ``on_session_start`` and stops it on ``on_session_end``.

When a wake event arrives, it is stored in a per-session queue and
logged. Full delivery via ``transform_tool_result`` / ``pre_llm_call``
hooks can be refined later.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_LISTEN_HOST = "127.0.0.1"
_DEFAULT_LISTEN_PORT = 0  # ephemeral

# Per-session wake-event queues, keyed by session id.
# ``deque`` keeps insertion order; events are consumed by the session hook.
_session_queues: dict[str, deque[dict[str, Any]]] = {}

# Active HTTP listeners keyed by session id — so on_session_end can stop them.
_listeners: dict[str, tuple[HTTPServer, threading.Thread]] = {}


class _WakeRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler that receives wake events and enqueues them."""

    def do_POST(self) -> None:  # noqa: N802 — stdlib naming convention
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""

        session_id = self.headers.get("X-Agent-Wake-Session", "")
        try:
            event: dict[str, Any] = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._respond(400, {"error": "invalid JSON"})
            return

        if not session_id:
            self._respond(400, {"error": "missing X-Agent-Wake-Session header"})
            return

        queue = _session_queues.setdefault(session_id, deque())
        queue.append(event)
        logger.info("wake event queued for session %s: %s", session_id, event)

        self._respond(200, {"status": "queued", "session_id": session_id})

    def _respond(self, code: int, payload: dict[str, Any]) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        body = json.dumps(payload).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("wake HTTP: " + format, *args)


def _start_listener(session_id: str) -> dict[str, Any]:
    """Start a background HTTP listener for the given session.

    Returns a dict with ``host`` and ``port`` keys.
    """
    server = HTTPServer(
        (_DEFAULT_LISTEN_HOST, _DEFAULT_LISTEN_PORT),
        _WakeRequestHandler,
    )
    port = server.server_address[1]
    thread = threading.Thread(
        target=server.serve_forever,
        name=f"wake-listener-{session_id}",
        daemon=True,
    )
    thread.start()

    _listeners[session_id] = (server, thread)
    _session_queues.setdefault(session_id, deque())

    logger.info(
        "wake listener started for session %s on %s:%d",
        session_id, _DEFAULT_LISTEN_HOST, port,
    )
    return {"host": _DEFAULT_LISTEN_HOST, "port": port}


def _stop_listener(session_id: str) -> None:
    """Stop the HTTP listener for the given session."""
    pair = _listeners.pop(session_id, None)
    if pair is None:
        return
    server, thread = pair
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    logger.info("wake listener stopped for session %s", session_id)


def register(ctx: Any) -> None:
    """Hermes plugin entry point.

    Registers hooks for session lifecycle and wake event delivery.
    """

    config_path = os.environ.get("AGENT_WAKE_CONFIG", "")
    if config_path:
        logger.info("agent-wake config: %s", config_path)
    else:
        logger.warning("AGENT_WAKE_CONFIG not set; wake plugin running unconfigured")

    def on_session_start(session_id: str, **_kwargs: Any) -> None:
        info = _start_listener(session_id)
        ctx.log(f"wake listener started on {info['host']}:{info['port']}")

    def on_session_end(session_id: str, **_kwargs: Any) -> None:
        _stop_listener(session_id)
        queue = _session_queues.pop(session_id, None)
        if queue:
            count = len(queue)
            ctx.log(f"wake: {count} uncollected event(s) discarded for session {session_id}")

    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("on_session_end", on_session_end)
