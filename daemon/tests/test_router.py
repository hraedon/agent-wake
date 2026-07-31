"""Tests for agent_waked.router — source → adapter routing."""


import pytest

from agent_waked.router import Router


class MockConnection:
    def __init__(self, session_id="s1", adapter="claude", sources=None, router=None):
        self.session_id = session_id
        self.adapter = adapter
        self.instance = "test"
        self.sources = sources or ["github-actions"]
        self.sent = []
        self._router = router

    async def send_frame(self, frame):
        self.sent.append(frame)
        # Auto-resolve ack for tests so deliver() doesn't hang
        if self._router and frame.get("type") == "wake":
            self._router.resolve_ack(frame.get("ack_id", ""), "ack")

    def close(self):
        pass


def _config_with_routing():
    return {
        "sources": {
            "github-actions": {"secret": b"k1"},
            "telegram-bot": {"secret": b"k2"},
            "unrouted": {"secret": b"k3"},
        },
        "routing": {
            "github-actions": {"adapter": "claude"},
            "telegram-bot": {"adapter": "opencode"},
        },
    }


def _config_legacy():
    return {
        "sources": {
            "github-actions": {"secret": b"k1"},
            "telegram-bot": {"secret": b"k2"},
        },
        "routing": {},
    }


# ── accepted_sources_for ─────────────────────────────────────────────

class TestAcceptedSources:
    def test_routed_source_matching_adapter(self):
        r = Router(_config_with_routing())
        assert r.accepted_sources_for("claude", ["github-actions"]) == ["github-actions"]

    def test_routed_source_wrong_adapter(self):
        r = Router(_config_with_routing())
        assert r.accepted_sources_for("claude", ["telegram-bot"]) == []

    def test_mixed_sources(self):
        r = Router(_config_with_routing())
        assert r.accepted_sources_for(
            "claude", ["github-actions", "telegram-bot"]
        ) == ["github-actions"]

    def test_unrouted_source_accepted_by_any(self):
        r = Router(_config_with_routing())
        assert r.accepted_sources_for("claude", ["unrouted"]) == ["unrouted"]

    def test_unknown_source_ignored(self):
        r = Router(_config_with_routing())
        assert r.accepted_sources_for("claude", ["nonexistent"]) == []

    def test_legacy_mode_all_accepted(self):
        r = Router(_config_legacy())
        assert r.accepted_sources_for("claude", ["github-actions", "telegram-bot"]) == [
            "github-actions",
            "telegram-bot",
        ]


# ── subscribe / unsubscribe ──────────────────────────────────────────

class TestSubscribe:
    def test_subscribe_adds_subscriber(self):
        r = Router(_config_with_routing())
        conn = MockConnection()
        r.subscribe("s1", "claude", "test", ["github-actions"], conn)
        assert len(r._subscribers) == 1

    def test_unsubscribe_removes(self):
        r = Router(_config_with_routing())
        conn = MockConnection()
        r.subscribe("s1", "claude", "test", ["github-actions"], conn)
        r.unsubscribe("s1")
        assert len(r._subscribers) == 0

    def test_unsubscribe_idempotent(self):
        r = Router(_config_with_routing())
        r.unsubscribe("nonexistent")


# ── deliver / _resolve ───────────────────────────────────────────────

@pytest.mark.asyncio
class TestDeliver:
    async def test_routed_delivers_to_correct_adapter(self):
        r = Router(_config_with_routing())
        conn = MockConnection("s1", "claude", ["github-actions"], router=r)
        r.subscribe("s1", "claude", "test", ["github-actions"], conn)

        result = await r.deliver({"source": "github-actions", "event_id": "e1"})
        assert result == "queued"

    async def test_routed_no_matching_adapter(self):
        r = Router(_config_with_routing())
        conn = MockConnection("s1", "claude", ["github-actions"], router=r)
        r.subscribe("s1", "claude", "test", ["github-actions"], conn)

        result = await r.deliver({"source": "telegram-bot", "event_id": "e2"})
        assert result == "no_subscriber"

    async def test_no_subscriber_at_all(self):
        r = Router(_config_with_routing())
        result = await r.deliver({"source": "github-actions", "event_id": "e3"})
        assert result == "no_subscriber"

    async def test_legacy_mode_picks_subscriber(self):
        r = Router(_config_legacy())
        conn = MockConnection("s1", "claude", ["github-actions"], router=r)
        r.subscribe("s1", "claude", "test", ["github-actions"], conn)

        result = await r.deliver({"source": "github-actions", "event_id": "e4"})
        assert result == "queued"

    async def test_legacy_mode_picks_most_recent(self):
        r = Router(_config_legacy())
        conn1 = MockConnection("s1", "claude", ["github-actions"], router=r)
        conn2 = MockConnection("s2", "claude", ["github-actions"], router=r)
        r.subscribe("s1", "claude", "test", ["github-actions"], conn1)
        r.subscribe("s2", "claude", "test", ["github-actions"], conn2)

        result = await r.deliver({"source": "github-actions", "event_id": "e5"})
        assert result == "queued"

    async def test_deliver_sends_wake_frame_with_event(self):
        r = Router(_config_legacy())
        sent = []

        class MockConn:
            async def send_frame(self, frame):
                sent.append(frame)
                if frame.get("type") == "wake":
                    r.resolve_ack(frame.get("ack_id", ""), "ack")

        conn = MockConn()
        r.subscribe("s1", "claude", "test", ["github-actions"], conn)

        event = {
            "v": 0,
            "event_id": "e-test",
            "source": "github-actions",
            "kind": "webhook",
            "content": "hi",
            "meta": {},
            "wake": True,
        }
        await r.deliver(event)

        assert len(sent) == 1
        assert sent[0]["type"] == "wake"
        assert sent[0]["event"]["event_id"] == "e-test"
        assert "ack_id" in sent[0]

    async def test_deliver_does_not_mutate_event(self):
        r = Router(_config_legacy())
        sent = []

        class MockConn:
            async def send_frame(self, frame):
                sent.append(frame)
                if frame.get("type") == "wake":
                    r.resolve_ack(frame.get("ack_id", ""), "ack")

        conn = MockConn()
        r.subscribe("s1", "claude", "test", ["github-actions"], conn)

        event = {
            "v": 0,
            "event_id": "e-imm",
            "source": "github-actions",
            "kind": "webhook",
            "content": "hi",
            "meta": {},
            "wake": True,
        }
        original = dict(event)
        await r.deliver(event)
        assert event == original

    async def test_source_not_in_subscriber_filters(self):
        r = Router(_config_legacy())
        conn = MockConnection("s1", "claude", ["telegram-bot"], router=r)
        r.subscribe("s1", "claude", "test", ["telegram-bot"], conn)

        result = await r.deliver({"source": "github-actions", "event_id": "e6"})
        assert result == "no_subscriber"

    # ── BC-009: deliver must not propagate ConnectionError ─────────

    async def test_deliver_returns_no_subscriber_on_connection_reset(self):
        """BC-009: a dead subscriber is evicted; deliver returns no_subscriber
        instead of letting ConnectionResetError reach the HTTP handler (500)."""
        r = Router(_config_legacy())

        class DeadConn:
            def __init__(self):
                self.session_id = "s1"
                self.adapter = "claude"
                self.instance = "test"
                self.sources = ["github-actions"]
                self.closed = False

            async def send_frame(self, frame):
                raise ConnectionResetError("peer closed")

            def close(self):
                self.closed = True

        conn = DeadConn()
        r.subscribe("s1", "claude", "test", ["github-actions"], conn)

        result = await r.deliver({"source": "github-actions", "event_id": "e-bc009"})
        assert result == "no_subscriber"
        # Dead subscriber evicted from the router.
        assert "s1" not in r._subscribers
        assert "s1" not in r._order
        # Writer closed to accelerate socket_server teardown.
        assert conn.closed is True

    async def test_deliver_evicts_on_broken_pipe_then_reroutes(self):
        """After evicting the dead subscriber, a second event with a live
        subscriber is delivered normally (no lingering dead entry)."""
        r = Router(_config_legacy())

        class DeadConn:
            def __init__(self):
                self.session_id = "s2"
                self.adapter = "claude"
                self.instance = "test"
                self.sources = ["github-actions"]

            async def send_frame(self, frame):
                raise BrokenPipeError("broken pipe")

            def close(self):
                pass

        live = MockConnection("s1", "claude", ["github-actions"], router=r)
        r.subscribe("s1", "claude", "test", ["github-actions"], live)
        r.subscribe("s2", "claude", "test", ["github-actions"], DeadConn())

        first = await r.deliver({"source": "github-actions", "event_id": "e1"})
        assert first == "no_subscriber"

        second = await r.deliver({"source": "github-actions", "event_id": "e2"})
        assert second == "queued"
        # The wake went to the live connection.
        assert any(f.get("type") == "wake" for f in live.sent)

    async def test_deliver_does_not_swallow_non_connection_errors(self):
        """BC-009: only OSError-family connection failures are caught. A
        RuntimeError or TypeError from send_frame indicates a real bug and
        must propagate (not be silently masked as a dead subscriber)."""
        import pytest as _pytest

        r = Router(_config_legacy())

        class BugConn:
            async def send_frame(self, frame):
                raise RuntimeError("not a connection error — a real bug")

            def close(self):
                pass

        r.subscribe("s1", "claude", "test", ["github-actions"], BugConn())

        with _pytest.raises(RuntimeError, match="real bug"):
            await r.deliver({"source": "github-actions", "event_id": "e-bug"})
        # A non-connection failure must NOT evict the subscriber (it's a
        # code bug, not a dead socket).
        assert "s1" in r._subscribers
