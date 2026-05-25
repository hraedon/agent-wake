"""Permission relay for Claude Code channel permission requests."""

import json
import time
import urllib.request
import threading

from ._notify import send
from .config import load_config

_PENDING_TTL_SECONDS = 300  # 5 minutes

_pending: dict[str, tuple[dict, float]] = {}
_lock = threading.Lock()


def _evict_expired() -> None:
    """Remove entries older than TTL. Must be called with _lock held."""
    cutoff = time.monotonic() - _PENDING_TTL_SECONDS
    expired = [k for k, (_, ts) in _pending.items() if ts < cutoff]
    for k in expired:
        del _pending[k]


def _forward_permission_request(payload: dict, callback_url: str) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        callback_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            _ = resp.read()
    except Exception:
        # Best-effort: permission forwarding may fail silently.
        # TODO: Add structured logging if/when substrate integration lands.
        pass


def handle_permission_request(params: dict) -> None:
    """Handle notifications/claude/channel/permission_request from Claude Code."""
    request_id = params.get("request_id", "")
    payload = {
        "request_id": request_id,
        "tool_name": params.get("tool_name", ""),
        "tool_input": params.get("tool_input", ""),
        "session_id": params.get("session_id", ""),
    }

    config = load_config()
    callback_url = config.get("default_callback_url")
    if callback_url:
        _forward_permission_request(payload, callback_url)

    with _lock:
        _evict_expired()
        _pending[request_id] = (payload, time.monotonic())


def handle_verdict(payload: dict) -> None:
    """Process a verdict POSTed to /permission/verdict and relay it to Claude Code."""
    request_id = payload.get("request_id", "")
    behavior = payload.get("behavior", "deny")

    with _lock:
        _evict_expired()
        _pending.pop(request_id, None)

    send({
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel/permission",
        "params": {
            "request_id": request_id,
            "behavior": behavior,
        },
    })