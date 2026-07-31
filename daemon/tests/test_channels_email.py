"""Tests for agent_waked.channels.email — SMTP delivery (Plan 005 WI-1.3).

AC:
1. The digest event renders as a readable email.
2. SMTP failure is a doctor finding (delivery status = failed).
"""

from email.message import EmailMessage
from typing import Any
from unittest.mock import patch

import pytest

from agent_waked.channels.email import EmailChannel, _redact_addr, render_email


class FakeResolver:
    def __init__(self, secrets: dict[str, bytes] | None = None) -> None:
        self._secrets = secrets or {}

    async def resolve(self, uri: str) -> bytes:
        if uri not in self._secrets:
            raise RuntimeError(f"unknown: {uri}")
        return self._secrets[uri]


def _config(secret_uri: str = "env://SMTP_PASS") -> dict[str, Any]:
    return {
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "from_addr": "wake@example.com",
        "to_addr": "operator@example.com",
        "use_tls": True,
        "secret_uri": secret_uri,
    }


def _event(kind: str = "digest", content: str = "3 items need review") -> dict[str, Any]:
    return {
        "v": 0,
        "event_id": "evt-001",
        "source": "dossier",
        "kind": kind,
        "content": content,
        "meta": {"target": "operator", "deep_link": "https://suite/dossier"},
        "wake": True,
    }


# ── render_email ─────────────────────────────────────────────────────


class TestRenderEmail:
    def test_digest_renders_with_subject_and_body(self):
        event = _event(kind="digest", content="3 items need review")
        subject, body = render_email(event)
        assert "digest" in subject.lower()
        assert "3 items need review" in body
        assert "https://suite/dossier" in body

    def test_awaiting_accept_uses_action_required_subject(self):
        event = _event(kind="awaiting_accept", content="WI-42 needs your accept")
        subject, body = render_email(event)
        assert "action required" in subject.lower()
        assert "WI-42 needs your accept" in body
        assert "https://suite/dossier" in body

    def test_dossier_awaiting_your_accept_uses_action_required_subject(self):
        event = _event(
            kind="awaiting_your_accept", content="ITEM-42 needs your accept"
        )
        subject, body = render_email(event)
        assert "action required" in subject.lower()
        assert "ITEM-42 needs your accept" in body
        assert "https://suite/dossier" in body

    @pytest.mark.parametrize(
        ("kind", "subject_fragment"),
        [
            ("review_requested", "review requested"),
            ("item_returned", "changes requested"),
        ],
    )
    def test_dossier_event_kinds_have_specific_templates(
        self, kind: str, subject_fragment: str
    ):
        subject, body = render_email(_event(kind=kind, content="ITEM-42"))
        assert subject_fragment in subject.lower()
        assert "ITEM-42" in body

    def test_alert_subject_includes_content(self):
        event = _event(kind="alert", content="suite doctor went red")
        subject, _ = render_email(event)
        assert "suite doctor went red" in subject

    def test_chain_verify_failed_subject(self):
        event = _event(kind="chain_verify_failed", content="hash mismatch")
        subject, _ = render_email(event)
        assert "chain verification failed" in subject.lower()

    def test_suite_health_subject_includes_content(self):
        event = _event(kind="suite_health", content="all green")
        subject, _ = render_email(event)
        assert "all green" in subject

    def test_unknown_kind_falls_back_to_default(self):
        event = _event(kind="custom_kind", content="something happened")
        subject, body = render_email(event)
        assert "notification" in subject.lower()
        assert "something happened" in body

    def test_no_deep_link_shows_none(self):
        event = _event(kind="digest", content="x")
        event["meta"] = {}
        _, body = render_email(event)
        assert "(none)" in body


# ── EmailChannel.deliver ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_email_delivered_successfully():
    sent_messages: list[EmailMessage] = []

    def fake_smtp_send(host, port, from_addr, to_addr, msg, password, use_tls):
        sent_messages.append(msg)
        assert host == "smtp.example.com"
        assert port == 587
        assert from_addr == "wake@example.com"
        assert to_addr == "operator@example.com"
        assert password == "smtp-password"
        assert use_tls is True

    resolver = FakeResolver({"env://SMTP_PASS": b"smtp-password"})
    ch = EmailChannel()
    with patch("agent_waked.channels.email._smtp_send", side_effect=fake_smtp_send):
        result = await ch.deliver(_event(kind="digest"), _config(), resolver)
    await ch.close()

    assert result["status"] == "delivered"
    assert len(sent_messages) == 1
    assert sent_messages[0]["To"] == "operator@example.com"
    assert "digest" in sent_messages[0]["Subject"].lower()


@pytest.mark.asyncio
async def test_email_smtp_failure_returns_failed():
    resolver = FakeResolver({"env://SMTP_PASS": b"smtp-password"})
    ch = EmailChannel()
    with patch(
        "agent_waked.channels.email._smtp_send",
        side_effect=ConnectionRefusedError("connection refused"),
    ):
        result = await ch.deliver(_event(), _config(), resolver)
    await ch.close()

    assert result["status"] == "failed"
    assert "connection refused" in result["error"]


@pytest.mark.asyncio
async def test_email_no_secret_uri_works_without_auth():
    """When no secret_uri is configured, SMTP sends without auth."""
    sent_messages: list[EmailMessage] = []

    def fake_smtp_send(host, port, from_addr, to_addr, msg, password, use_tls):
        sent_messages.append(msg)
        assert password is None

    cfg = _config()
    del cfg["secret_uri"]
    resolver = FakeResolver({})
    ch = EmailChannel()
    with patch("agent_waked.channels.email._smtp_send", side_effect=fake_smtp_send):
        result = await ch.deliver(_event(), cfg, resolver)
    await ch.close()

    assert result["status"] == "delivered"


@pytest.mark.asyncio
async def test_email_secret_resolve_failure():
    resolver = FakeResolver({})  # no secret registered
    ch = EmailChannel()
    result = await ch.deliver(_event(), _config(), resolver)
    await ch.close()

    assert result["status"] == "failed"
    assert "secret resolve failed" in result["error"]


# ── _redact_addr ──────────────────────────────────────────────────────


def test_redact_addr():
    assert _redact_addr("operator@example.com") == "o***@example.com"


def test_redact_addr_no_at():
    assert _redact_addr("garbage") == "***"
