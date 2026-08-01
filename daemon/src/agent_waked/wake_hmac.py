"""HMAC signing for agent-wake callback bodies."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections.abc import Mapping, Sequence
from typing import Any

SIGNATURE_HEADER = "X-Wake-Signature"
REPLAY_WINDOW_SECONDS = 300


def load_keys(
    config: Mapping[str, Any], environ: Mapping[str, str] | None = None
) -> tuple[bytes, ...]:
    """Load rotation-ordered keys, with environment entries first."""
    env = os.environ if environ is None else environ
    values: list[str] = []
    env_value = env.get("WAKE_HMAC_SECRET")
    if env_value:
        values.extend(env_value.split(","))

    wake = config.get("wake")
    configured = wake.get("hmac_secret") if isinstance(wake, Mapping) else None
    if isinstance(configured, str):
        values.extend(configured.split(","))
    elif isinstance(configured, Sequence):
        for value in configured:
            if isinstance(value, str):
                values.extend(value.split(","))

    keys: list[bytes] = []
    for value in values:
        key = value.strip().encode("utf-8")
        if key and key not in keys:
            keys.append(key)
    return tuple(keys)


def sign_body(
    body: bytes, keys: Sequence[bytes], *, timestamp: int | None = None
) -> str | None:
    """Sign *body* with the first configured key."""
    if not keys:
        return None
    signed_at = int(time.time()) if timestamp is None else timestamp
    body_hash = hashlib.sha256(body).hexdigest()
    signing_input = f"{signed_at}.{body_hash}".encode("ascii")
    digest = hmac.new(keys[0], signing_input, hashlib.sha256).hexdigest()
    return f"t={signed_at},v1={digest}"


def verify_body(
    body: bytes,
    header: str | None,
    keys: Sequence[bytes],
    *,
    now: int | None = None,
    replay_window: int = REPLAY_WINDOW_SECONDS,
) -> bool:
    """Verify signature and timestamp against every accepted rotation key."""
    if not header or not keys:
        return False
    fields: dict[str, str] = {}
    for item in header.split(","):
        name, separator, value = item.strip().partition("=")
        if not separator or not name or not value or name in fields:
            return False
        fields[name] = value
    if set(fields) != {"t", "v1"}:
        return False
    try:
        timestamp = int(fields["t"])
    except ValueError:
        return False
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > replay_window:
        return False

    body_hash = hashlib.sha256(body).hexdigest()
    signing_input = f"{timestamp}.{body_hash}".encode("ascii")
    supplied = fields["v1"]
    matched = False
    for key in keys:
        expected = hmac.new(key, signing_input, hashlib.sha256).hexdigest()
        matched |= hmac.compare_digest(expected, supplied)
    return matched
