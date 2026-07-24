"""Tests for silent inject on the Claude Code adapter (WI-B).

The adapter used to log-and-drop every ``wake: false`` event. Claude Code's
channel protocol has no ``noReply`` flag, but it does document that
notifications arriving while Claude is busy "are delivered together on the next
turn" — so a silent event emitted while a turn is already happening arrives as
context without having caused a turn.

These tests pin that contract:

* a silent event is never dropped;
* it is not emitted while the session is idle (that would be a wake);
* it is emitted once a turn is provably underway, tagged ``silent="true"``;
* the semantics are declared machine-readably on ``initialize``.
"""

import pytest

from agent_wake_claude import channel, server


@pytest.fixture(autouse=True)
def _clean_buffer():
    channel.reset_deferred()
    yield
    channel.reset_deferred()


@pytest.fixture
def sent(monkeypatch):
    """Capture everything the adapter writes to the MCP client."""
    captured: list[dict] = []
    monkeypatch.setattr(channel, "send", lambda msg: captured.append(msg))
    monkeypatch.setattr(server, "send", lambda msg: captured.append(msg))
    return captured


def _event(event_id="ev-1", wake=True, content="hello", meta=None):
    return {
        "v": 0,
        "event_id": event_id,
        "source": "github-actions",
        "kind": "webhook",
        "content": content,
        "meta": meta if meta is not None else {},
        "wake": wake,
    }


def _channel_notifications(sent):
    return [m for m in sent if m.get("method") == "notifications/claude/channel"]


# ── the old bug: silent events were dropped ──────────────────────────────────


def test_silent_event_is_not_dropped(sent):
    channel.emit_wake_event(_event(wake=False))
    # Not emitted yet — emitting now would start a turn, which is the one thing
    # silent inject must not do...
    assert _channel_notifications(sent) == []
    # ...but it is retained, not discarded.
    assert channel.pending_silent_count() == 1


def test_wake_event_still_emits_immediately(sent):
    channel.emit_wake_event(_event())
    notes = _channel_notifications(sent)
    assert len(notes) == 1
    assert notes[0]["params"]["content"] == "hello"
    assert notes[0]["params"]["meta"]["source"] == "github-actions"
    assert "silent" not in notes[0]["params"]["meta"]


# ── flush triggers ────────────────────────────────────────────────────────────


def test_silent_event_rides_along_with_the_next_wake(sent):
    channel.emit_wake_event(_event("ev-silent", wake=False, content="fyi"))
    channel.emit_wake_event(_event("ev-wake", content="act now"))

    notes = _channel_notifications(sent)
    assert len(notes) == 2
    # The deferred event goes first: it is context for the turn the wake is
    # about to trigger.
    assert notes[0]["params"]["content"] == "fyi"
    assert notes[0]["params"]["meta"]["silent"] == "true"
    assert notes[0]["params"]["meta"]["flush_reason"] == "preceding_wake"
    assert notes[0]["params"]["meta"]["deferred_ms"].isdigit()
    assert notes[1]["params"]["content"] == "act now"
    assert channel.pending_silent_count() == 0


def test_tools_call_flushes_deferred_events(sent, monkeypatch):
    """A tool call can only happen mid-turn, so it is a safe delivery point."""
    monkeypatch.setattr(
        server, "handle_reply_tool_call", lambda args: {"content": []}
    )
    channel.emit_wake_event(_event(wake=False, content="deferred"))

    server.handle({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "agent_wake_reply", "arguments": {}},
    })

    notes = _channel_notifications(sent)
    assert len(notes) == 1
    assert notes[0]["params"]["content"] == "deferred"
    assert notes[0]["params"]["meta"]["flush_reason"] == "tools_call"
    assert channel.pending_silent_count() == 0


def test_permission_request_flushes_deferred_events(sent, monkeypatch):
    monkeypatch.setattr(server, "handle_permission_request", lambda params: None)
    channel.emit_wake_event(_event(wake=False, content="deferred"))

    server.handle({
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel/permission_request",
        "params": {"request_id": "abcde", "tool_name": "Bash",
                   "description": "x", "input_preview": "y"},
    })

    notes = _channel_notifications(sent)
    assert len(notes) == 1
    assert notes[0]["params"]["meta"]["flush_reason"] == "permission_request"


def test_idle_protocol_traffic_does_not_flush(sent):
    """tools/list happens at startup, not mid-turn — it must not wake anyone."""
    channel.emit_wake_event(_event(wake=False))
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert _channel_notifications(sent) == []
    assert channel.pending_silent_count() == 1


def test_multiple_silent_events_flush_in_order(sent):
    for i in range(3):
        channel.emit_wake_event(_event(f"ev-{i}", wake=False, content=f"c{i}"))
    assert channel.pending_silent_count() == 3

    assert channel.flush_silent_events("tools_call") == 3
    notes = _channel_notifications(sent)
    assert [n["params"]["content"] for n in notes] == ["c0", "c1", "c2"]


def test_flush_with_empty_buffer_is_a_noop(sent):
    assert channel.flush_silent_events("tools_call") == 0
    assert sent == []


# ── the honest limitation ─────────────────────────────────────────────────────


def test_buffer_overflow_flushes_rather_than_losing_or_growing(sent):
    """If no turn ever comes, the adapter wakes late instead of leaking or dropping."""
    for i in range(channel.MAX_DEFERRED):
        channel.emit_wake_event(_event(f"ev-{i}", wake=False))

    notes = _channel_notifications(sent)
    assert len(notes) == channel.MAX_DEFERRED
    assert all(n["params"]["meta"]["flush_reason"] == "overflow" for n in notes)
    assert channel.pending_silent_count() == 0


# ── machine-readable declaration ──────────────────────────────────────────────


def test_initialize_declares_silent_inject_semantics(sent):
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    result = sent[0]["result"]
    cap = result["capabilities"]["experimental"]["agent-wake/silent_inject"]
    assert cap["supported"] is True
    assert cap["mode"] == "deferred"
    assert cap["drops_events"] is False
    assert cap["overflow_behavior"] == "flush_and_wake"
    assert set(cap["flush_triggers"]) == {
        "preceding_wake",
        "tools_call",
        "permission_request",
    }
    # The pre-existing channel capabilities are untouched.
    assert result["capabilities"]["experimental"]["claude/channel"] == {}


# ── meta handling ─────────────────────────────────────────────────────────────


def test_non_identifier_meta_keys_are_filtered(sent):
    channel.emit_wake_event(_event(meta={"run_id": "1", "not-an-identifier": "x"}))
    meta = _channel_notifications(sent)[0]["params"]["meta"]
    assert meta["run_id"] == "1"
    assert "not-an-identifier" not in meta


def test_silent_meta_is_preserved_through_the_buffer(sent):
    channel.emit_wake_event(_event(wake=False, meta={"run_id": "42"}))
    channel.flush_silent_events("tools_call")
    meta = _channel_notifications(sent)[0]["params"]["meta"]
    assert meta["run_id"] == "42"
    assert meta["silent"] == "true"


def test_non_dict_meta_does_not_crash(sent):
    channel.emit_wake_event(_event(meta="oops"))
    assert len(_channel_notifications(sent)) == 1
