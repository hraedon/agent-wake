"""Tests for agent_waked.delivery — human-directed delivery dispatcher (Plan 005)."""

from typing import Any

from agent_waked.delivery import DeliveryHealth, HumanDelivery


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
