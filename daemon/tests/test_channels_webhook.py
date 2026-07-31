"""Tests for agent_waked.channels.webhook — outbound signed webhook (Plan 005 WI-1.2).

AC:
1. A notification event posted to ingress arrives at a test receiver with the
   deep link intact.
2. The outbound webhook is HMAC-SHA256 signed (receiver can verify).
3. The event_id is sent as an Idempotency-Key header.
4. A dead receiver retries and surfaces as a doctor warn, not silence.
"""

import hashlib
import hmac
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent_waked import netguard
from agent_waked.channels.webhook import WebhookChannel, _redact_url


class FakeResolver:
    def __init__(self, secrets: dict[str, bytes] | None = None) -> None:
        self._secrets = secrets or {}

    async def resolve(self, uri: str) -> bytes:
        if uri not in self._secrets:
            raise RuntimeError(f"unknown: {uri}")
        return self._secrets[uri]


def _event(event_id: str = "evt-001", deep_link: str = "https://suite/dossier/WI-42") -> dict:
    return {
        "v": 0,
        "event_id": event_id,
        "source": "dossier",
        "kind": "awaiting_accept",
        "content": "WI-42 awaits your accept",
        "meta": {"target": "operator", "deep_link": deep_link},
        "wake": True,
    }


def _config(url: str, secret_uri: str = "env://WEBHOOK_SECRET") -> dict:
    return {"url": url, "secret_uri": secret_uri}


# ── AC 1+2+3: successful delivery with signing + idempotency + deep link ──


@pytest.mark.asyncio
async def test_webhook_delivered_with_deep_link_intact():
    received_bodies = []
    received_headers = []

    async def handler(request):
        body = await request.read()
        received_bodies.append(json.loads(body))
        received_headers.append(dict(request.headers))
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/hook", handler)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    try:
        url = str(cli.make_url("/hook"))
        secret = b"test-signing-secret"
        resolver = FakeResolver({"env://WEBHOOK_SECRET": secret})
        ch = WebhookChannel(max_retries=1)
        result = await ch.deliver(_event(), _config(url), resolver)
        await ch.close()

        assert result["status"] == "delivered"
        assert result["http_status"] == 200

        assert len(received_bodies) == 1
        # Deep link intact
        assert received_bodies[0]["meta"]["deep_link"] == "https://suite/dossier/WI-42"
        assert received_bodies[0]["kind"] == "awaiting_accept"

        # Idempotency-Key header = event_id
        assert received_headers[0]["Idempotency-Key"] == "evt-001"

        # Signature is HMAC-SHA256 and verifies
        sig_header = received_headers[0]["X-AgentWake-Signature"]
        assert sig_header.startswith("sha256=")
        sent_body = json.dumps(_event(), separators=(",", ":")).encode("utf-8")
        expected_sig = hmac.new(secret, sent_body, hashlib.sha256).hexdigest()
        assert sig_header == f"sha256={expected_sig}"
    finally:
        await cli.close()


# ── AC 4: dead receiver retries and fails ────────────────────────────


@pytest.mark.asyncio
async def test_webhook_dead_receiver_retries_then_fails():
    call_count = 0

    async def handler(request):
        nonlocal call_count
        call_count += 1
        return web.Response(status=503, text="unavailable")

    app = web.Application()
    app.router.add_post("/hook", handler)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    try:
        url = str(cli.make_url("/hook"))
        resolver = FakeResolver({"env://WEBHOOK_SECRET": b"secret"})
        ch = WebhookChannel(max_retries=3, backoff_delays=(0.01, 0.01, 0.01))
        result = await ch.deliver(_event(), _config(url), resolver)
        await ch.close()

        assert result["status"] == "failed"
        assert result["http_status"] == 503
        assert call_count == 3  # retried 3 times
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_webhook_connection_error_retries():
    """A receiver that refuses connections retries and fails."""
    # Use a port that's almost certainly not listening
    url = "http://127.0.0.1:1/hook"
    resolver = FakeResolver({"env://WEBHOOK_SECRET": b"secret"})
    ch = WebhookChannel(max_retries=2, backoff_delays=(0.01, 0.01))
    result = await ch.deliver(_event(), _config(url), resolver)
    await ch.close()

    assert result["status"] == "failed"
    assert result["error"] is not None


@pytest.mark.asyncio
async def test_webhook_secret_resolve_failure():
    resolver = FakeResolver({})  # no secret registered
    ch = WebhookChannel(max_retries=1)
    result = await ch.deliver(_event(), _config("http://127.0.0.1:9999/x"), resolver)
    await ch.close()

    assert result["status"] == "failed"
    assert "secret resolve failed" in result["error"]


@pytest.mark.asyncio
async def test_webhook_idempotency_key_matches_event_id():
    received_keys = []

    async def handler(request):
        received_keys.append(request.headers.get("Idempotency-Key", ""))
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/hook", handler)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    try:
        url = str(cli.make_url("/hook"))
        resolver = FakeResolver({"env://WEBHOOK_SECRET": b"secret"})
        ch = WebhookChannel(max_retries=1)

        evt1 = _event(event_id="evt-AAA")
        evt2 = _event(event_id="evt-BBB")
        await ch.deliver(evt1, _config(url), resolver)
        await ch.deliver(evt2, _config(url), resolver)
        await ch.close()

        assert received_keys == ["evt-AAA", "evt-BBB"]
    finally:
        await cli.close()


# ── SSRF: redirects are not followed ─────────────────────────────────


@pytest.mark.asyncio
async def test_webhook_does_not_follow_redirects():
    """A 3xx must not re-POST the signed body to the redirect target.

    Without ``allow_redirects=False`` a hijacked-but-valid target can 301 the
    body anywhere — including an internal address nothing validates.
    """
    internal_hits = []

    async def hook(request):
        raise web.HTTPFound(location="/internal")

    async def internal(request):
        internal_hits.append(await request.read())
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/hook", hook)
    app.router.add_post("/internal", internal)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    try:
        url = str(cli.make_url("/hook"))
        resolver = FakeResolver({"env://WEBHOOK_SECRET": b"secret"})
        ch = WebhookChannel(max_retries=1)
        result = await ch.deliver(_event(), _config(url), resolver)
        await ch.close()

        assert internal_hits == []
        assert result["status"] == "failed"
        assert result["http_status"] == 302
    finally:
        await cli.close()


# ── SSRF: the forbidden-range guard runs per request, not just at load ──


@pytest.mark.asyncio
async def test_webhook_rejects_target_that_rebinds_to_internal(monkeypatch):
    """A hostname re-pointed at an internal address after load is refused.

    This is the DNS-rebinding case the config-load check cannot see: the URL
    was valid when the daemon started, and resolves to 169.254.169.254 now.
    """
    hits = []

    async def handler(request):
        hits.append(await request.read())
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/hook", handler)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    try:
        async def _rebound(host: str) -> list[str]:
            return ["169.254.169.254"]

        monkeypatch.setattr(netguard, "aresolve_hostname", _rebound)

        url = str(cli.make_url("/hook"))
        resolver = FakeResolver({"env://WEBHOOK_SECRET": b"secret"})
        ch = WebhookChannel(max_retries=3, backoff_delays=(0.01, 0.01, 0.01))
        result = await ch.deliver(_event(), _config(url), resolver)
        await ch.close()

        assert result["status"] == "failed"
        assert "unsafe target" in result["error"]
        assert "169.254.169.254" in result["error"]
        # Nothing was sent, and no retry ladder was walked for an unsafe URL.
        assert hits == []
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_webhook_rejects_unresolvable_target(monkeypatch):
    async def _fail(host: str) -> list[str]:
        raise OSError("Name or service not known")

    monkeypatch.setattr(netguard, "aresolve_hostname", _fail)
    resolver = FakeResolver({"env://WEBHOOK_SECRET": b"secret"})
    ch = WebhookChannel(max_retries=1)
    result = await ch.deliver(_event(), _config("https://gone.example/hook"), resolver)
    await ch.close()

    assert result["status"] == "failed"
    assert "does not resolve" in result["error"]


@pytest.mark.asyncio
async def test_webhook_rejects_non_http_scheme():
    resolver = FakeResolver({"env://WEBHOOK_SECRET": b"secret"})
    ch = WebhookChannel(max_retries=1)
    result = await ch.deliver(_event(), _config("file:///etc/passwd"), resolver)
    await ch.close()

    assert result["status"] == "failed"
    assert "disallowed scheme" in result["error"]


@pytest.mark.asyncio
async def test_webhook_unsafe_target_never_computes_a_signature(monkeypatch):
    """The guard runs before secret resolution, so no signature is minted."""
    resolved = []

    class TrackingResolver(FakeResolver):
        async def resolve(self, uri: str) -> bytes:
            resolved.append(uri)
            return await super().resolve(uri)

    async def _rebound(host: str) -> list[str]:
        return ["127.0.0.1"]

    monkeypatch.setattr(netguard, "aresolve_hostname", _rebound)
    ch = WebhookChannel(max_retries=1)
    result = await ch.deliver(
        _event(), _config("https://hook.example/x"),
        TrackingResolver({"env://WEBHOOK_SECRET": b"secret"}),
    )
    await ch.close()

    assert result["status"] == "failed"
    assert resolved == []


# ── netguard range predicate ──────────────────────────────────────────


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # RFC 6598 CGNAT — is_private is False for this range
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
    ],
)
def test_netguard_rejects_forbidden_addresses(addr):
    assert netguard.is_forbidden_address(addr) is True


@pytest.mark.parametrize("addr", ["93.184.216.34", "8.8.8.8", "2606:2800:220:1::1"])
def test_netguard_allows_public_addresses(addr):
    assert netguard.is_forbidden_address(addr) is False


def test_netguard_ignores_unparseable_address():
    """Garbage from the resolver is not silently treated as forbidden."""
    assert netguard.is_forbidden_address("not-an-ip") is False
    assert netguard.forbidden_address(["not-an-ip", "10.0.0.1"]) == "10.0.0.1"


# ── _redact_url ───────────────────────────────────────────────────────


def test_redact_url_no_credentials():
    assert _redact_url("https://example.com/hook") == "https://example.com/hook"


def test_redact_url_with_credentials():
    redacted = _redact_url("https://user:pass@example.com/hook")
    assert "***@" in redacted
    assert "pass" not in redacted
    assert "user" not in redacted
    assert "example.com/hook" in redacted
