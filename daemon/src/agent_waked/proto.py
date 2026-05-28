"""Frame encode / decode / validate for the daemon↔adapter wire protocol.

Spec reference: v1-daemon-spec.md §4 (Daemon ↔ adapter wire protocol).

Framing is newline-delimited JSON.  Each line is one frame.  UTF-8 only,
no BOM.  Lines longer than 1 MiB trigger ``frame_too_large``.
"""

import json
from typing import Any, Sequence

MAX_FRAME_SIZE = 1 * 1024 * 1024  # 1 MiB

KNOWN_TYPES = frozenset({
    "hello",
    "hello_ack",
    "wake",
    "ack",
    "nack",
    "reply",
    "reply_result",
    "error",
    "ping",
    "pong",
})


class FrameError(Exception):
    pass


class BadFrameError(FrameError):
    pass


class FrameTooLargeError(FrameError):
    pass


def encode_frame(frame: dict[str, Any]) -> bytes:
    return (json.dumps(frame, separators=(",", ":")) + "\n").encode("utf-8")


def decode_line(line: bytes) -> dict[str, Any]:
    return json.loads(line)  # type: ignore[no-any-return]


def _require_str(frame: dict[str, Any], key: str) -> str | None:
    val = frame.get(key)
    if not isinstance(val, str):
        return "bad_frame"
    return None


def _validate_hello(frame: dict[str, Any]) -> str | None:
    if frame.get("v") != 1:
        return "version_unsupported"
    err = _require_str(frame, "adapter")
    if err:
        return err
    err = _require_str(frame, "instance")
    if err:
        return err
    filters = frame.get("filters", {})
    sources = filters.get("sources")
    if not isinstance(sources, list):
        return "bad_frame"
    return None


def _validate_hello_ack(frame: dict[str, Any]) -> str | None:
    if frame.get("v") != 1:
        return "bad_frame"
    err = _require_str(frame, "session_id")
    if err:
        return err
    accepted = frame.get("accepted_sources")
    if not isinstance(accepted, list):
        return "bad_frame"
    return None


def _validate_wake(frame: dict[str, Any]) -> str | None:
    err = _require_str(frame, "ack_id")
    if err:
        return err
    if not isinstance(frame.get("event"), dict):
        return "bad_frame"
    return None


def _validate_ack(frame: dict[str, Any]) -> str | None:
    return _require_str(frame, "ack_id")


def _validate_nack(frame: dict[str, Any]) -> str | None:
    err = _require_str(frame, "ack_id")
    if err:
        return err
    return _require_str(frame, "reason")


def _validate_reply(frame: dict[str, Any]) -> str | None:
    for key in ("reply_id", "source", "in_reply_to", "content"):
        err = _require_str(frame, key)
        if err:
            return err
    return None


def _validate_reply_result(frame: dict[str, Any]) -> str | None:
    err = _require_str(frame, "reply_id")
    if err:
        return err
    status = frame.get("status")
    if status not in ("delivered", "failed", "no_callback"):
        return "bad_frame"
    return None


def _validate_error(frame: dict[str, Any]) -> str | None:
    return _require_str(frame, "code")


def _validate_ping(frame: dict[str, Any]) -> str | None:
    return None


def _validate_pong(frame: dict[str, Any]) -> str | None:
    return None


_VALIDATORS = {
    "hello": _validate_hello,
    "hello_ack": _validate_hello_ack,
    "wake": _validate_wake,
    "ack": _validate_ack,
    "nack": _validate_nack,
    "reply": _validate_reply,
    "reply_result": _validate_reply_result,
    "error": _validate_error,
    "ping": _validate_ping,
    "pong": _validate_pong,
}


def validate_frame(
    frame: dict[str, Any],
    expected_types: set[str] | None = None,
) -> str | None:
    """Validate *frame* structure.

    Returns an error-code string (e.g. ``"bad_frame"``,
    ``"version_unsupported"``) if the frame is invalid, or ``None`` if
    it is structurally sound.

    *expected_types*, when provided, restricts which ``type`` values are
    considered valid. If the frame's ``type`` is not in *expected_types*
    and is also not a known type, the frame is treated as unknown
    (forward-compat) and ``None`` is returned — the caller should log a
    warning. If the type *is* known but not in *expected_types*, the
    caller decides the semantics (e.g. ``unauthenticated`` when hello
    was expected).
    """
    if not isinstance(frame, dict):
        return "bad_frame"

    ftype = frame.get("type")
    if not isinstance(ftype, str):
        return "bad_frame"

    if expected_types is not None and ftype not in expected_types:
        if ftype not in KNOWN_TYPES:
            return None
        return "unexpected_type"

    validator = _VALIDATORS.get(ftype)
    if validator is None:
        return None

    return validator(frame)
