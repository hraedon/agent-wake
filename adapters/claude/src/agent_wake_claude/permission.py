"""Permission relay for Claude Code channel permission requests."""

import json
import logging
import threading
import time
import urllib.request
from typing import Any

from ._notify import send
from .config import load_config
from .wake_hmac import SIGNATURE_HEADER, load_keys, sign_body, verify_body

_PENDING_TTL_SECONDS = 300  # 5 minutes

_pending: dict[str, tuple[dict[str, Any], float]] = {}
_lock = threading.Lock()


def _evict_expired() -> None:
    """Remove entries older than TTL. Must be called with _lock held."""
    cutoff = time.monotonic() - _PENDING_TTL_SECONDS
    expired = [k for k, (_, ts) in _pending.items() if ts < cutoff]
    for k in expired:
        del _pending[k]


def _forward_permission_request(
    payload: dict[str, Any], callback_url: str, keys: tuple[bytes, ...] = ()
) -> None:
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    signature = sign_body(data, keys)
    if signature is not None:
        headers[SIGNATURE_HEADER] = signature
    req = urllib.request.Request(
        callback_url,
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            _ = resp.read()
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "permission forward failed: %s", exc
        )


def handle_permission_request(params: dict[str, Any]) -> None:
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
        _forward_permission_request(payload, callback_url, load_keys(config))

    with _lock:
        _evict_expired()
        _pending[request_id] = (payload, time.monotonic())


def verify_permission_body(
    body: bytes,
    signature: str | None,
    *,
    config: dict[str, Any] | None = None,
    require_auth: bool = True,
    now: int | None = None,
) -> bool:
    """Verify an inbound relay body; false maps to HTTP 401 for receivers."""
    keys = load_keys(load_config() if config is None else config)
    if signature is None:
        return not require_auth
    return verify_body(body, signature, keys, now=now)


def handle_verdict(payload: dict[str, Any]) -> None:
    """Process a verdict POSTed to /permission/verdict and relay it to Claude Code."""
    request_id = payload.get("request_id", "")
    behavior = payload.get("behavior", "deny")
    if behavior not in ("allow", "deny"):
        raise ValueError(f"invalid behavior: {behavior!r}")

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
