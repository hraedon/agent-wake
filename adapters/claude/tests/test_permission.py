import time
import threading

import agent_wake_claude.config as cm
from agent_wake_claude.permission import (
    handle_permission_request,
    handle_verdict,
    _pending,
    _lock,
    _PENDING_TTL_SECONDS,
)


def _clear_pending():
    with _lock:
        _pending.clear()


def _set_mock_config():
    cm._cached_config = {
        "sources": {"demo": {"secret": b"shhh", "callback_url": None}},
        "default_callback_url": None,
    }


def _clear_mock_config():
    cm._cached_config = None


def test_pending_stores_request():
    _clear_pending()
    _set_mock_config()
    try:
        handle_permission_request({
            "request_id": "abc01",
            "tool_name": "Bash",
            "tool_input": "ls",
            "session_id": "sess1",
        })
        with _lock:
            keys = list(_pending.keys())
        assert "abc01" in keys
    finally:
        _clear_pending()
        _clear_mock_config()


def test_verdict_removes_request():
    _clear_pending()
    _set_mock_config()
    try:
        handle_permission_request({
            "request_id": "abc02",
            "tool_name": "Bash",
            "tool_input": "rm",
            "session_id": "sess1",
        })
        with _lock:
            assert "abc02" in _pending
        handle_verdict({"request_id": "abc02", "behavior": "allow"})
        with _lock:
            assert "abc02" not in _pending
    finally:
        _clear_pending()
        _clear_mock_config()


def test_verdict_for_unknown_request_is_noop():
    _clear_pending()
    handle_verdict({"request_id": "nonexistent", "behavior": "deny"})
    with _lock:
        assert len(_pending) == 0


def test_eviction_removes_expired_entries():
    _clear_pending()
    _set_mock_config()
    try:
        handle_permission_request({
            "request_id": "old01",
            "tool_name": "Bash",
            "tool_input": "echo old",
            "session_id": "sess1",
        })
        with _lock:
            assert "old01" in _pending
            entry = _pending["old01"]
            _pending["old01"] = (entry[0], time.monotonic() - _PENDING_TTL_SECONDS - 1)
        handle_permission_request({
            "request_id": "new01",
            "tool_name": "Bash",
            "tool_input": "echo new",
            "session_id": "sess1",
        })
        with _lock:
            assert "old01" not in _pending
            assert "new01" in _pending
    finally:
        _clear_pending()
        _clear_mock_config()


def test_eviction_on_verdict():
    _clear_pending()
    _set_mock_config()
    try:
        handle_permission_request({
            "request_id": "old02",
            "tool_name": "Bash",
            "tool_input": "echo old",
            "session_id": "sess1",
        })
        with _lock:
            entry = _pending["old02"]
            _pending["old02"] = (entry[0], time.monotonic() - _PENDING_TTL_SECONDS - 1)
        handle_verdict({"request_id": "old02", "behavior": "deny"})
        with _lock:
            assert "old02" not in _pending
    finally:
        _clear_pending()
        _clear_mock_config()