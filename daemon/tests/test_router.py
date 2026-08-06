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

    def test_subscriber_health_reports_adapters_and_coverage(self):
        clock = [100.0]
        r = Router(_config_with_routing(), clock=lambda: clock[0])
        conn = MockConnection()
        r.subscribe("s1", "claude", "test", ["github-actions"], conn)

        assert r.subscriber_health() == {
            "connected": 1,
            "connected_adapters": ["claude"],
            "by_source": {
                "github-actions": {
                    "subscribers": 1,
                    "adapters": ["claude"],
                    "oldest_age_seconds": 0.0,
                },
                "telegram-bot": {
                    "subscribers": 0,
                    "adapters": [],
                    "oldest_age_seconds": None,
                },
                "unrouted": {
                    "subscribers": 0,
                    "adapters": [],
                    "oldest_age_seconds": None,
                },
            },
            "live_only_sources": ["github-actions", "telegram-bot", "unrouted"],
            "live_only_without_subscribers": ["telegram-bot", "unrouted"],
        }

    def test_subscriber_health_ages_and_lists_each_adapter(self):
        clock = [100.0]
        r = Router(_config_with_routing(), clock=lambda: clock[0])
        r.subscribe("s1", "claude", "test", ["github-actions"], MockConnection())
        clock[0] = 105.0
        r.subscribe("s2", "opencode", "test", ["telegram-bot"], MockConnection("s2", "opencode"))
        clock[0] = 107.0

        health = r.subscriber_health()
        github = health["by_source"]["github-actions"]
        telegram = health["by_source"]["telegram-bot"]
        # oldest subscriber for github-actions connected at t=100, now t=107.
        assert github["oldest_age_seconds"] == 7.0
        assert github["adapters"] == ["claude"]
        # telegram-bot's opencode subscriber connected later, at t=105 -> age 2.0.
        assert telegram["oldest_age_seconds"] == 2.0
        assert telegram["adapters"] == ["opencode"]
        assert sorted(health["connected_adapters"]) == ["claude", "opencode"]

    def test_subscriber_health_ignores_sources_with_durable_default(self):
        cfg = _config_with_routing()
        cfg["state"] = {"default_delivery": "next_session"}
        r = Router(cfg)

        summary = r.subscriber_health()
        assert summary["live_only_sources"] == []
        assert summary["live_only_without_subscribers"] == []

    @pytest.mark.asyncio
    async def test_shutdown_drains_ack_waiters(self):
        r = Router(_config_legacy())
        conn = MockConnection("s1", "claude", ["github-actions"])
        r.subscribe("s1", "claude", "test", ["github-actions"], conn)

        assert await r.deliver({"source": "github-actions", "event_id": "e1"}) == "queued"
        assert r._background_tasks
        assert r._pending_acks

        await r.shutdown()

        assert r._background_tasks == set()
        assert r._pending_acks == {}


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


# ── silent-accumulation warning (WI-011) ─────────────────────────────

class TestQueueWithoutSubscriberWarning:
    """Durable queueing must not look healthy when the leg is broken.

    Nine wakes accumulated for six days on mvmcc03 while every log line said
    ``queued for next session`` — the Claude adapter's channel was silently
    skipped by org policy and nothing surfaced it.
    """

    def _router(self):
        return Router(_config_with_routing())

    def test_warns_every_third_queued_event_and_flags_never_subscribed(self, caplog):
        r = self._router()
        caplog.set_level("WARNING")
        for _ in range(3):
            r._note_queued_without_delivery("github-actions")
        assert "NO subscriber has served it" in caplog.text
        assert "github-actions" in caplog.text

    def test_quiet_below_threshold(self, caplog):
        r = self._router()
        caplog.set_level("WARNING")
        for _ in range(2):
            r._note_queued_without_delivery("github-actions")
        assert caplog.text == ""

    def test_subscribed_destination_gets_the_softer_message(self, caplog):
        r = self._router()
        r.note_subscribed(["github-actions"])
        caplog.set_level("WARNING")
        for _ in range(3):
            r._note_queued_without_delivery("github-actions")
        assert "has not returned" in caplog.text
        assert "NO subscriber has served it" not in caplog.text

    def test_delivery_resets_the_counter(self, caplog):
        r = self._router()
        for _ in range(2):
            r._note_queued_without_delivery("github-actions")
        r.note_delivered("github-actions")
        caplog.set_level("WARNING")
        for _ in range(2):
            r._note_queued_without_delivery("github-actions")
        assert caplog.text == ""

    def test_subscribe_resets_the_counter(self, caplog):
        r = self._router()
        for _ in range(2):
            r._note_queued_without_delivery("github-actions")
        r.note_subscribed(["github-actions"])
        caplog.set_level("WARNING")
        for _ in range(2):
            r._note_queued_without_delivery("github-actions")
        assert caplog.text == ""

    def test_counters_are_per_destination(self, caplog):
        r = self._router()
        caplog.set_level("WARNING")
        for _ in range(2):
            r._note_queued_without_delivery("github-actions")
        r._note_queued_without_delivery("telegram-bot")
        assert caplog.text == ""

    def test_note_subscribed_tolerates_none(self):
        r = self._router()
        r.note_subscribed(None)  # must not raise

    def test_rejected_destination_claims_do_not_record_a_subscription(self):
        """A hello whose explicit destination claims are all rejected must not
        mark a same-named legacy source as served.

        destinations_for_hello() returns [] when every claim is filtered out
        (unknown destination, or one configured for a different adapter). The
        socket server must pass exactly that, not fall back to
        accepted_sources_for() — the namespaces differ, and a false
        subscription silences the accumulation warning (sol review, WI-011).
        """
        r = self._router()
        # 'telegram-bot' is configured for the opencode adapter, so a claude
        # adapter claiming it gets nothing back.
        destinations = r.destinations_for_hello("claude", [], ["telegram-bot"])
        assert destinations == []
        accepted = r.accepted_sources_for("claude", ["github-actions"])
        assert accepted  # the legacy namespace WOULD have offered a name

        r.note_subscribed(destinations)
        assert "telegram-bot" not in r._ever_subscribed
        assert not r._ever_subscribed

    def test_legacy_source_only_hello_still_records_its_destination(self):
        """The legacy path must keep working: a sources-only hello resolves to
        destination names, which are what gets recorded."""
        r = self._router()
        destinations = r.destinations_for_hello("claude", ["github-actions"], None)
        assert destinations == ["github-actions"]
        r.note_subscribed(destinations)
        assert "github-actions" in r._ever_subscribed

    def test_prune_drops_state_for_removed_destinations(self):
        r = self._router()
        r.note_subscribed(["github-actions", "telegram-bot"])
        r._note_queued_without_delivery("telegram-bot")
        # Config reload removes telegram-bot entirely.
        del r._config["sources"]["telegram-bot"]
        del r._config["routing"]["telegram-bot"]
        r.prune_warning_state()
        assert "telegram-bot" not in r._ever_subscribed
        assert "telegram-bot" not in r._consecutive_queued
        assert "github-actions" in r._ever_subscribed

    def test_prune_keeps_state_for_surviving_destinations(self):
        r = self._router()
        r._note_queued_without_delivery("github-actions")
        r.prune_warning_state()
        assert r._consecutive_queued.get("github-actions") == 1
