"""Permission relay for Claude Code channel permission requests."""

import hashlib
import hmac as hmac_mod
import json
import time
import urllib.request
import threading
from typing import Any

from ._notify import send
from .config import load_config

_PENDING_TTL_SECONDS = 300  # 5 minutes

_pending: dict[str, tuple[dict[str, Any], float]] = {}
_lock = threading.Lock()


def _evict_expired() -> None:
    """Remove entries older than TTL. Must be called with _lock held."""
    cutoff = time.monotonic() - _PENDING_TTL_SECONDS
    expired = [k for k, (_, ts) in _pending.items() if ts < cutoff]
    for k in expired:
        del _pending[k]


def _hmac_sign(secret: bytes, body: bytes) -> str:
    """Return sha256=<hex> HMAC signature."""
    digest = hmac_mod.new(secret, body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _forward_permission_request(payload: dict[str, Any], callback_url: str, secret: bytes | None = None) -> None:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-AgentWake-Signature"] = _hmac_sign(secret, data)
    req = urllib.request.Request(
        callback_url,
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            _ = resp.read()
    except Exception:
        # Best-effort: permission forwarding may fail silently.
        pass


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
        # Sign with the first available source's secret
        secret = None
        for src_cfg in config.get("sources", {}).values():
            s = src_cfg.get("secret")
            if s:
                secret = s
                break
        _forward_permission_request(payload, callback_url, secret)

    with _lock:
        _evict_expired()
        _pending[request_id] = (payload, time.monotonic())


def handle_verdict(payload: dict[str, Any]) -> None:
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