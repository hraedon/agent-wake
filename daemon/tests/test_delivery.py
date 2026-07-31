"""Tests for agent_waked.delivery — human-directed delivery dispatcher (Plan 005)."""

import asyncio
import logging
import sqlite3
from typing import Any

import pytest

from agent_waked.delivery import DeliveryHealth, HumanDelivery
from agent_waked.store import WakeStore


class FakeResolver:
    """Minimal SecretResolver stand-in for unit tests."""

    def __init__(self, secrets: dict[str, bytes] | None = None) -> None:
        self._secrets = secrets or {}

    async def resolve(self, uri: str) -> bytes:
        if uri not in self._secrets:
            raise RuntimeError(f"unknown secret uri: {uri}")
        return self._secrets[uri]


class FakeChannel:
    """Minimal DeliveryChannel stand-in."""

    def __init__(self, name: str, result: dict[str, Any]) -> None:
        self._name = name
        self._result = result
        self.deliver_calls: list[tuple[dict, dict]] = []
        self.closed = False

    @property
    def name(self) -> str:
        return self._name

    async def deliver(self, event, channel_cfg, resolver):
        self.deliver_calls.append((event, channel_cfg))
        return dict(self._result)

    async def close(self) -> None:
        self.closed = True


def _config_with_delivery(principals: dict | None = None) -> dict[str, Any]:
    return {
        "sources": {"demo": {"secret": b"k1"}},
        "delivery": principals or {},
    }


def _event(target: str | None = None, kind: str = "webhook") -> dict[str, Any]:
    meta: dict[str, Any] = {}
    if target:
        meta["target"] = target
    return {
        "v": 0,
        "event_id": "evt-001",
        "source": "demo",
        "kind": kind,
        "content": "hello",
        "meta": meta,
        "wake": True,
    }


# ── DeliveryHealth ──────────────────────────────────────────────────


class TestDeliveryHealth:
    def test_initial_state_no_failures(self):
        h = DeliveryHealth()
        assert h.has_failures() is False
        s = h.summary()
        assert s["delivered"] == 0
        assert s["failing_channels"] == []
        assert s["unknown_principals"] == []

    def test_record_success_increments_delivered(self):
        h = DeliveryHealth()
        h.record_success("webhook", "operator")
        assert h.summary()["delivered"] == 1
        assert h.has_failures() is False

    def test_record_failure_marks_failures(self):
        h = DeliveryHealth()
        h.record_failure("webhook", "operator", "connection refused")
        assert h.has_failures() is True
        s = h.summary()
        assert len(s["failing_channels"]) == 1
        assert s["failing_channels"][0]["channel"] == "webhook"
        assert s["failing_channels"][0]["principal_id"] == "operator"
        assert "connection refused" in s["failing_channels"][0]["last_error"]

    def test_record_success_clears_failure(self):
        h = DeliveryHealth()
        h.record_failure("webhook", "operator", "timeout")
        assert h.has_failures() is True
        h.record_success("webhook", "operator")
        assert h.has_failures() is False

    def test_record_unknown_principal(self):
        h = DeliveryHealth()
        h.record_unknown_principal("mystery")
        assert h.has_failures() is True
        assert "mystery" in h.summary()["unknown_principals"]


# ── HumanDelivery ──────────────────────────────────────────────────


class TestResolvePrincipal:
    def test_known_principal(self):
        cfg = _config_with_delivery({
            "operator": {"webhook": {"url": "http://x", "secret_uri": "env://K"}}
        })
        hd = HumanDelivery(cfg, FakeResolver())
        result = hd.resolve_principal("operator")
        assert result is not None
        assert "webhook" in result

    def test_unknown_principal(self):
        hd = HumanDelivery(_config_with_delivery(), FakeResolver())
        assert hd.resolve_principal("nobody") is None

    def test_empty_delivery_config(self):
        hd = HumanDelivery(_config_with_delivery({}), FakeResolver())
        assert hd.resolve_principal("operator") is None


class TestDeliver:
    async def test_no_target_returns_no_target(self):
        hd = HumanDelivery(_config_with_delivery(), FakeResolver())
        result = await hd.deliver(_event(target=None))
        assert result["status"] == "no_target"

    async def test_unknown_principal_rejected_loudly(self):
        hd = HumanDelivery(_config_with_delivery(), FakeResolver())
        result = await hd.deliver(_event(target="nobody"))
        assert result["status"] == "unknown_principal"
        assert result["principal_id"] == "nobody"
        assert hd.health.has_failures() is True
        assert "nobody" in hd.health.summary()["unknown_principals"]

    async def test_known_principal_no_channels(self):
        cfg = _config_with_delivery({"operator": {}})
        hd = HumanDelivery(cfg, FakeResolver())
        result = await hd.deliver(_event(target="operator"))
        assert result["status"] == "no_channels"

    async def test_delivered_to_single_channel(self):
        ch = FakeChannel("webhook", {"status": "delivered"})
        hd = HumanDelivery(
            _config_with_delivery({
                "operator": {"webhook": {"url": "http://x", "secret_uri": "env://K"}}
            }),
            FakeResolver({"env://K": b"secret"}),
            channels={"webhook": ch},
        )
        result = await hd.deliver(_event(target="operator"))
        assert result["status"] == "delivered"
        assert len(ch.deliver_calls) == 1
        assert ch.deliver_calls[0][0]["event_id"] == "evt-001"
        assert hd.health.has_failures() is False

    async def test_partial_delivery(self):
        ok = FakeChannel("webhook", {"status": "delivered"})
        fail = FakeChannel("email", {"status": "failed", "error": "smtp down"})
        hd = HumanDelivery(
            _config_with_delivery({
                "operator": {
                    "webhook": {"url": "http://x", "secret_uri": "env://K"},
                    "email": {"smtp_host": "smtp", "smtp_port": 587,
                              "from_addr": "a@b", "to_addr": "c@d"},
                }
            }),
            FakeResolver({"env://K": b"secret"}),
            channels={"webhook": ok, "email": fail},
        )
        result = await hd.deliver(_event(target="operator"))
        assert result["status"] == "partial"
        assert hd.health.has_failures() is True

    async def test_all_channels_fail(self):
        fail = FakeChannel("webhook", {"status": "failed", "error": "conn refused"})
        hd = HumanDelivery(
            _config_with_delivery({
                "operator": {"webhook": {"url": "http://x", "secret_uri": "env://K"}}
            }),
            FakeResolver({"env://K": b"secret"}),
            channels={"webhook": fail},
        )
        result = await hd.deliver(_event(target="operator"))
        assert result["status"] == "failed"
        assert hd.health.has_failures() is True

    async def test_channel_exception_caught(self):
        class BoomChannel:
            name = "webhook"

            async def deliver(self, event, cfg, resolver):
                raise RuntimeError("boom")

            async def close(self):
                pass

        hd = HumanDelivery(
            _config_with_delivery({
                "operator": {"webhook": {"url": "http://x", "secret_uri": "env://K"}}
            }),
            FakeResolver({"env://K": b"secret"}),
            channels={"webhook": BoomChannel()},
        )
        result = await hd.deliver(_event(target="operator"))
        assert result["status"] == "failed"
        assert hd.health.has_failures() is True

    async def test_no_adapter_for_channel_kind(self):
        hd = HumanDelivery(
            _config_with_delivery({
                "operator": {"webhook": {"url": "http://x", "secret_uri": "env://K"}}
            }),
            FakeResolver({"env://K": b"secret"}),
            channels={},
        )
        result = await hd.deliver(_event(target="operator"))
        assert result["status"] == "failed"
        assert hd.health.has_failures() is True

    async def test_close_calls_all_channels(self):
        ch1 = FakeChannel("webhook", {"status": "delivered"})
        ch2 = FakeChannel("email", {"status": "delivered"})
        hd = HumanDelivery(
            _config_with_delivery(),
            FakeResolver(),
            channels={"webhook": ch1, "email": ch2},
        )
        await hd.close()
        assert ch1.closed is True
        assert ch2.closed is True


# ── dead-lettering a human delivery that never landed (WI-001 M3) ────


class SlowFailChannel:
    """A channel that hangs (as a webhook does mid-retry-backoff) then fails."""

    def __init__(self, name: str = "webhook", delay: float = 10.0) -> None:
        self._name = name
        self._delay = delay
        self.started = asyncio.Event()
        self.closed = False

    @property
    def name(self) -> str:
        return self._name

    async def deliver(self, event, channel_cfg, resolver):
        self.started.set()
        await asyncio.sleep(self._delay)
        return {"status": "failed", "error": "unreachable"}

    async def close(self) -> None:
        self.closed = True


class RaisingChannel:
    def __init__(self, name: str = "webhook", exc: Exception | None = None) -> None:
        self._name = name
        self._exc = exc or RuntimeError("channel exploded")
        self.closed = False

    @property
    def name(self) -> str:
        return self._name

    async def deliver(self, event, channel_cfg, resolver):
        raise self._exc

    async def close(self) -> None:
        self.closed = True


def _webhook_principal(pid: str = "operator") -> dict[str, Any]:
    return {pid: {"webhook": {"url": "http://x", "secret_uri": "env://K"}}}


class TestHumanDeliveryDeadLetter:
    async def test_failed_delivery_is_dead_lettered(self, tmp_path):
        fail = FakeChannel("webhook", {"status": "failed", "error": "conn refused"})
        store = WakeStore(tmp_path / "state.db")
        try:
            hd = HumanDelivery(
                _config_with_delivery(_webhook_principal()),
                FakeResolver({"env://K": b"secret"}),
                channels={"webhook": fail},
                store=store,
            )
            result = await hd.deliver(_event(target="operator"))
            assert result["status"] == "failed"
            entries = store.list_dead_letters(kind="human_delivery")
            assert len(entries) == 1
            entry = entries[0]
            assert entry.kind == "human_delivery"
            assert entry.source == "demo"
            assert entry.ref_id == "evt-001"
            assert entry.payload["principal_id"] == "operator"
            assert entry.payload["event"]["event_id"] == "evt-001"
            assert "failed" in (entry.error or "")
        finally:
            store.close()

    async def test_partial_delivery_is_dead_lettered(self, tmp_path):
        """One channel landing does not make the other channel's loss acceptable."""
        ok = FakeChannel("webhook", {"status": "delivered"})
        bad = FakeChannel("email", {"status": "failed", "error": "smtp down"})
        store = WakeStore(tmp_path / "state.db")
        try:
            hd = HumanDelivery(
                _config_with_delivery(
                    {
                        "operator": {
                            "webhook": {"url": "http://x", "secret_uri": "env://K"},
                            "email": {"to": "a@b.c"},
                        }
                    }
                ),
                FakeResolver({"env://K": b"secret"}),
                channels={"webhook": ok, "email": bad},
                store=store,
            )
            result = await hd.deliver(_event(target="operator"))
            assert result["status"] == "partial"
            entries = store.list_dead_letters(kind="human_delivery")
            assert len(entries) == 1
            assert "email" in (entries[0].error or "")
        finally:
            store.close()

    async def test_no_channels_is_dead_lettered(self, tmp_path):
        store = WakeStore(tmp_path / "state.db")
        try:
            hd = HumanDelivery(
                _config_with_delivery({"operator": {}}),
                FakeResolver(),
                store=store,
            )
            result = await hd.deliver(_event(target="operator"))
            assert result["status"] == "no_channels"
            assert store.dead_letter_count() == 1
        finally:
            store.close()

    async def test_delivered_is_not_dead_lettered(self, tmp_path):
        ok = FakeChannel("webhook", {"status": "delivered"})
        store = WakeStore(tmp_path / "state.db")
        try:
            hd = HumanDelivery(
                _config_with_delivery(_webhook_principal()),
                FakeResolver({"env://K": b"secret"}),
                channels={"webhook": ok},
                store=store,
            )
            result = await hd.deliver(_event(target="operator"))
            assert result["status"] == "delivered"
            assert store.dead_letter_count() == 0
        finally:
            store.close()

    async def test_failure_without_store_keeps_behaviour(self):
        fail = FakeChannel("webhook", {"status": "failed", "error": "x"})
        hd = HumanDelivery(
            _config_with_delivery(_webhook_principal()),
            FakeResolver({"env://K": b"secret"}),
            channels={"webhook": fail},
        )
        result = await hd.deliver(_event(target="operator"))
        assert result["status"] == "failed"
        assert hd.health.has_failures() is True

    async def test_raising_channel_is_dead_lettered(self, tmp_path):
        store = WakeStore(tmp_path / "state.db")
        try:
            hd = HumanDelivery(
                _config_with_delivery(_webhook_principal()),
                FakeResolver({"env://K": b"secret"}),
                channels={"webhook": RaisingChannel()},
                store=store,
            )
            result = await hd.deliver(_event(target="operator"))
            assert result["status"] == "failed"
            assert store.dead_letter_count() == 1
        finally:
            store.close()


class TestHumanDeliveryCancellation:
    """SIGTERM mid-backoff must not swallow the alert (WI-001 M3).

    CancelledError is a BaseException, so it never passed through the
    ``except Exception`` in the dispatch loop: before the fix, a cancelled
    delivery wrote *no* dead-letter at all.
    """

    async def test_cancelled_delivery_is_dead_lettered(self, tmp_path):
        slow = SlowFailChannel(delay=30.0)
        store = WakeStore(tmp_path / "state.db")
        try:
            hd = HumanDelivery(
                _config_with_delivery(_webhook_principal()),
                FakeResolver({"env://K": b"secret"}),
                channels={"webhook": slow},
                store=store,
            )
            task = asyncio.ensure_future(hd.deliver(_event(target="operator")))
            await asyncio.wait_for(slow.started.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            entries = store.list_dead_letters(kind="human_delivery")
            assert len(entries) == 1
            assert "cancelled" in (entries[0].error or "")
            assert entries[0].payload["event"]["event_id"] == "evt-001"
        finally:
            store.close()

    async def test_cancellation_still_propagates(self, tmp_path):
        """The dead-letter write must not swallow the cancellation."""
        slow = SlowFailChannel(delay=30.0)
        store = WakeStore(tmp_path / "state.db")
        try:
            hd = HumanDelivery(
                _config_with_delivery(_webhook_principal()),
                FakeResolver({"env://K": b"secret"}),
                channels={"webhook": slow},
                store=store,
            )
            task = asyncio.ensure_future(hd.deliver(_event(target="operator")))
            await asyncio.wait_for(slow.started.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled() is True
        finally:
            store.close()

    async def test_cancelled_without_store_does_not_raise(self):
        slow = SlowFailChannel(delay=30.0)
        hd = HumanDelivery(
            _config_with_delivery(_webhook_principal()),
            FakeResolver({"env://K": b"secret"}),
            channels={"webhook": slow},
        )
        task = asyncio.ensure_future(hd.deliver(_event(target="operator")))
        await asyncio.wait_for(slow.started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestDeadLetterWriteFailures:
    """The last line of defence must not itself become the failure."""

    async def test_closed_store_does_not_raise_out_of_deliver(self, tmp_path, caplog):
        """The old shutdown order closed the store first: ProgrammingError."""
        fail = FakeChannel("webhook", {"status": "failed", "error": "x"})
        store = WakeStore(tmp_path / "state.db")
        store.close()  # simulate store.close() winning the shutdown race
        hd = HumanDelivery(
            _config_with_delivery(_webhook_principal()),
            FakeResolver({"env://K": b"secret"}),
            channels={"webhook": fail},
            store=store,
        )
        with caplog.at_level(logging.ERROR, logger="agent_waked.delivery"):
            result = await hd.deliver(_event(target="operator"))
        assert result["status"] == "failed"
        assert "LOST ALERT" in caplog.text
        # The event body must not be spilled into the log sink.
        assert "hello" not in caplog.text

    async def test_locked_store_does_not_raise_out_of_deliver(self, tmp_path, caplog):
        """A 'database is locked' past the busy timeout is logged, not raised."""

        class LockedStore:
            def dead_letter(self, **kwargs):
                raise sqlite3.OperationalError("database is locked")

        fail = FakeChannel("webhook", {"status": "failed", "error": "x"})
        hd = HumanDelivery(
            _config_with_delivery(_webhook_principal()),
            FakeResolver({"env://K": b"secret"}),
            channels={"webhook": fail},
            store=LockedStore(),  # type: ignore[arg-type]
        )
        with caplog.at_level(logging.ERROR, logger="agent_waked.delivery"):
            result = await hd.deliver(_event(target="operator"))
        assert result["status"] == "failed"
        assert "LOST ALERT" in caplog.text
        assert "database is locked" in caplog.text

    async def test_cancelled_with_closed_store_still_propagates(self, tmp_path):
        slow = SlowFailChannel(delay=30.0)
        store = WakeStore(tmp_path / "state.db")
        store.close()
        hd = HumanDelivery(
            _config_with_delivery(_webhook_principal()),
            FakeResolver({"env://K": b"secret"}),
            channels={"webhook": slow},
            store=store,
        )
        task = asyncio.ensure_future(hd.deliver(_event(target="operator")))
        await asyncio.wait_for(slow.started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestLostAlertIsObservable:
    """A lost alert must be detectable above a log line (WI-001 review)."""

    async def test_lost_alert_is_counted_and_surfaces_in_health(self, tmp_path):
        fail = FakeChannel("webhook", {"status": "failed", "error": "x"})
        store = WakeStore(tmp_path / "state.db")
        store.close()  # the store is gone; the dead-letter write cannot land
        hd = HumanDelivery(
            _config_with_delivery(_webhook_principal()),
            FakeResolver({"env://K": b"secret"}),
            channels={"webhook": fail},
            store=store,
        )
        assert hd.health.lost_alerts == 0
        await hd.deliver(_event(target="operator"))

        assert hd.health.lost_alerts == 1
        summary = hd.health.summary()
        assert summary["lost_alerts"] == 1
        assert summary["last_lost_alert"] == "demo/evt-001"
        # A lost alert alone must make health report unhealthy.
        assert hd.health.has_failures() is True

    async def test_a_healthy_run_reports_no_lost_alerts(self, tmp_path):
        ok = FakeChannel("webhook", {"status": "delivered"})
        store = WakeStore(tmp_path / "state.db")
        try:
            hd = HumanDelivery(
                _config_with_delivery(_webhook_principal()),
                FakeResolver({"env://K": b"secret"}),
                channels={"webhook": ok},
                store=store,
            )
            await hd.deliver(_event(target="operator"))
            assert hd.health.summary()["lost_alerts"] == 0
            assert hd.health.has_failures() is False
        finally:
            store.close()

    async def test_a_dead_letter_that_lands_is_not_a_lost_alert(self, tmp_path):
        fail = FakeChannel("webhook", {"status": "failed", "error": "x"})
        store = WakeStore(tmp_path / "state.db")
        try:
            hd = HumanDelivery(
                _config_with_delivery(_webhook_principal()),
                FakeResolver({"env://K": b"secret"}),
                channels={"webhook": fail},
                store=store,
            )
            await hd.deliver(_event(target="operator"))
            assert store.dead_letter_count() == 1
            assert hd.health.lost_alerts == 0
        finally:
            store.close()


class TestSkipChannelsOnRedrive:
    """Replaying a partial delivery sends a real second email (WI-001 review)."""

    def _two_channel_config(self):
        return _config_with_delivery({
            "operator": {
                "webhook": {"url": "http://x", "secret_uri": "env://K"},
                "email": {"to": "ops@example.com"},
            }
        })

    async def test_partial_records_the_delivered_channel(self, tmp_path):
        ok = FakeChannel("webhook", {"status": "delivered"})
        bad = FakeChannel("email", {"status": "failed", "error": "smtp down"})
        store = WakeStore(tmp_path / "state.db")
        try:
            hd = HumanDelivery(
                self._two_channel_config(),
                FakeResolver({"env://K": b"secret"}),
                channels={"webhook": ok, "email": bad},
                store=store,
            )
            assert (await hd.deliver(_event(target="operator")))["status"] == "partial"
            entry = store.list_dead_letters(kind="human_delivery")[0]
            assert entry.payload["delivered_channels"] == ["webhook"]
        finally:
            store.close()

    async def test_skipped_channel_is_not_redelivered(self):
        ok = FakeChannel("webhook", {"status": "delivered"})
        email = FakeChannel("email", {"status": "delivered"})
        hd = HumanDelivery(
            self._two_channel_config(),
            FakeResolver({"env://K": b"secret"}),
            channels={"webhook": ok, "email": email},
        )
        result = await hd.deliver(
            _event(target="operator"), skip_channels=["webhook"]
        )
        assert result["status"] == "delivered"
        assert ok.deliver_calls == []  # the already-delivered channel was spared
        assert len(email.deliver_calls) == 1
        statuses = {c["channel"]: c["status"] for c in result["channels"]}
        assert statuses == {"webhook": "skipped", "email": "delivered"}

    async def test_all_channels_skipped_is_delivered_not_failed(self):
        ok = FakeChannel("webhook", {"status": "delivered"})
        hd = HumanDelivery(
            _config_with_delivery(_webhook_principal()),
            FakeResolver({"env://K": b"secret"}),
            channels={"webhook": ok},
        )
        result = await hd.deliver(
            _event(target="operator"), skip_channels=["webhook"]
        )
        assert result["status"] == "delivered"
        assert ok.deliver_calls == []

    async def test_a_second_failure_carries_the_skip_list_forward(self, tmp_path):
        """Two redrives must not lose track of what already landed."""
        email = FakeChannel("email", {"status": "failed", "error": "still down"})
        store = WakeStore(tmp_path / "state.db")
        try:
            hd = HumanDelivery(
                self._two_channel_config(),
                FakeResolver({"env://K": b"secret"}),
                channels={"webhook": FakeChannel("webhook", {}), "email": email},
                store=store,
            )
            result = await hd.deliver(
                _event(target="operator"), skip_channels=["webhook"]
            )
            assert result["status"] == "failed"
            entry = store.list_dead_letters(kind="human_delivery")[0]
            assert entry.payload["delivered_channels"] == ["webhook"]
        finally:
            store.close()

    async def test_skip_is_recorded_through_a_cancellation(self, tmp_path):
        slow = SlowFailChannel("email", delay=30.0)
        store = WakeStore(tmp_path / "state.db")
        try:
            hd = HumanDelivery(
                self._two_channel_config(),
                FakeResolver({"env://K": b"secret"}),
                channels={"webhook": FakeChannel("webhook", {}), "email": slow},
                store=store,
            )
            task = asyncio.ensure_future(
                hd.deliver(_event(target="operator"), skip_channels=["webhook"])
            )
            await asyncio.wait_for(slow.started.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            entry = store.list_dead_letters(kind="human_delivery")[0]
            assert entry.payload["delivered_channels"] == ["webhook"]
        finally:
            store.close()
