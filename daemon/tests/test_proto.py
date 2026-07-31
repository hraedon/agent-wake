"""Tests for agent_waked.proto — frame encode/decode/validate."""

import json

import pytest

from agent_waked.proto import (
    decode_line,
    encode_frame,
    validate_frame,
)

# ── round-trip ────────────────────────────────────────────────────────

@pytest.mark.parametrize("frame", [
    {"type": "hello", "v": 1, "adapter": "claude", "instance": "t", "filters": {"sources": ["x"]}},
    {"type": "hello_ack", "v": 1, "session_id": "01ABC", "accepted_sources": ["x"]},
    {
        "type": "wake",
        "ack_id": "01DEF",
        "event": {
            "v": 0,
            "event_id": "e1",
            "source": "x",
            "kind": "webhook",
            "content": "hi",
            "meta": {},
            "wake": True,
        },
    },
    {"type": "ack", "ack_id": "01DEF"},
    {"type": "nack", "ack_id": "01DEF", "reason": "busy"},
    {"type": "reply", "reply_id": "01G", "source": "x", "in_reply_to": "01DEF", "content": "ok"},
    {
        "type": "reply_result",
        "reply_id": "01G",
        "status": "delivered",
        "http_status": 200,
        "error": None,
    },
    {"type": "error", "code": "bad_frame", "message": "nope", "fatal": True},
])
def test_round_trip(frame):
    encoded = encode_frame(frame)
    assert isinstance(encoded, bytes)
    assert encoded.endswith(b"\n")
    decoded = decode_line(encoded)
    assert decoded == frame


def test_encode_is_utf8():
    frame = {
        "type": "hello",
        "v": 1,
        "adapter": "claude",
        "instance": "t",
        "filters": {"sources": ["x"]},
    }
    encoded = encode_frame(frame)
    encoded.decode("utf-8")


def test_decode_malformed_json_raises():
    with pytest.raises(json.JSONDecodeError):
        decode_line(b"not json\n")


# ── validate_frame ────────────────────────────────────────────────────

def test_validate_hello_valid():
    f = {
        "type": "hello",
        "v": 1,
        "adapter": "claude",
        "instance": "t",
        "filters": {"sources": ["x"]},
    }
    assert validate_frame(f) is None


def test_validate_hello_bad_version():
    f = {
        "type": "hello",
        "v": 99,
        "adapter": "claude",
        "instance": "t",
        "filters": {"sources": ["x"]},
    }
    assert validate_frame(f) == "version_unsupported"


def test_validate_hello_missing_adapter():
    f = {"type": "hello", "v": 1, "instance": "t", "filters": {"sources": ["x"]}}
    assert validate_frame(f) == "bad_frame"


def test_validate_hello_missing_instance():
    f = {"type": "hello", "v": 1, "adapter": "claude", "filters": {"sources": ["x"]}}
    assert validate_frame(f) == "bad_frame"


def test_validate_hello_sources_not_list():
    f = {"type": "hello", "v": 1, "adapter": "claude", "instance": "t", "filters": {"sources": "x"}}
    assert validate_frame(f) == "bad_frame"


def test_validate_ack_valid():
    assert validate_frame({"type": "ack", "ack_id": "01A"}) is None


def test_validate_ack_missing_id():
    assert validate_frame({"type": "ack"}) == "bad_frame"


def test_validate_nack_valid():
    assert validate_frame({"type": "nack", "ack_id": "01A", "reason": "busy"}) is None


def test_validate_nack_missing_reason():
    assert validate_frame({"type": "nack", "ack_id": "01A"}) == "bad_frame"


def test_validate_wake_valid():
    f = {"type": "wake", "ack_id": "01A", "event": {"v": 0, "event_id": "e"}}
    assert validate_frame(f) is None


def test_validate_wake_missing_event():
    assert validate_frame({"type": "wake", "ack_id": "01A"}) == "bad_frame"


def test_validate_reply_valid():
    f = {"type": "reply", "reply_id": "01R", "source": "x", "in_reply_to": "01A", "content": "ok"}
    assert validate_frame(f) is None


def test_validate_reply_missing_reply_id():
    f = {"type": "reply", "source": "x", "in_reply_to": "01A", "content": "ok"}
    assert validate_frame(f) == "bad_frame"


def test_validate_reply_result_delivered():
    assert validate_frame(
        {"type": "reply_result", "reply_id": "01R", "status": "delivered"}
    ) is None


def test_validate_reply_result_failed():
    assert validate_frame(
        {
            "type": "reply_result",
            "reply_id": "01R",
            "status": "failed",
            "error": "timeout",
        }
    ) is None


def test_validate_reply_result_no_callback():
    assert validate_frame(
        {"type": "reply_result", "reply_id": "01R", "status": "no_callback"}
    ) is None


def test_validate_reply_result_bad_status():
    assert validate_frame(
        {"type": "reply_result", "reply_id": "01R", "status": "pending"}
    ) == "bad_frame"


def test_validate_error_valid():
    assert validate_frame({"type": "error", "code": "bad_frame", "message": "x"}) is None


def test_validate_error_missing_code():
    assert validate_frame({"type": "error", "message": "x"}) == "bad_frame"


def test_validate_unknown_type_returns_none():
    assert validate_frame({"type": "future_frame", "data": 42}) is None


def test_validate_non_dict_returns_bad_frame():
    assert validate_frame("not a dict") == "bad_frame"


def test_validate_missing_type_returns_bad_frame():
    assert validate_frame({"v": 1}) == "bad_frame"


def test_validate_type_not_string_returns_bad_frame():
    assert validate_frame({"type": 42}) == "bad_frame"


def test_validate_hello_ack_valid():
    assert validate_frame(
        {"type": "hello_ack", "v": 1, "session_id": "01A", "accepted_sources": []}
    ) is None


def test_validate_hello_ack_missing_session_id():
    assert validate_frame({"type": "hello_ack", "v": 1, "accepted_sources": []}) == "bad_frame"
