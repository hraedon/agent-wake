"""Email delivery channel (Plan 005 WI-1.3).

SMTP delivery for shops where webhooks aren't the idiom.  Templates for the
Plan 018 notification event classes + digest payloads.  TLS + auth via the
secret backend (``secret_uri`` resolved by SecretResolver).

SMTP is synchronous, so all I/O is run via ``asyncio.to_thread`` to avoid
blocking the event loop (same pattern as the vault secret backend).
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

if False:  # TYPE_CHECKING
    from ..secrets.resolver import SecretResolver

log = logging.getLogger("agent_waked.channels.email")

_SMTP_TIMEOUT = 30

_SUBJECT_TEMPLATES: dict[str, str] = {
    "awaiting_accept": "Action required: an item awaits your accept",
    "digest": "Your agent-suite digest",
    "alert": "Alert: {content}",
    "chain_verify_failed": "Chain verification failed",
    "suite_health": "Suite health: {content}",
}

_BODY_TEMPLATES: dict[str, str] = {
    "awaiting_accept": (
        "An item is awaiting your accept.\n\n"
        "{content}\n\n"
        "Deep link: {deep_link}\n\n"
        "— agent-wake"
    ),
    "digest": (
        "Here is your digest.\n\n"
        "{content}\n\n"
        "Deep link: {deep_link}\n\n"
        "— agent-wake"
    ),
}

_DEFAULT_SUBJECT = "agent-wake notification"
_DEFAULT_BODY = "{content}\n\n— agent-wake"


def render_email(event: dict[str, Any]) -> tuple[str, str]:
    """Render an event into ``(subject, body)`` for email delivery.

    Uses the Plan 018 event-class templates when the event ``kind`` matches
    a known class; falls back to a generic subject/body otherwise.
    """
    kind = str(event.get("kind", ""))
    content = str(event.get("content", ""))
    meta = event.get("meta", {})
    if not isinstance(meta, dict):
        meta = {}
    deep_link = str(meta.get("deep_link", meta.get("link", "")))

    content_short = content[:120] if content else ""
    subject_tmpl = _SUBJECT_TEMPLATES.get(kind, _DEFAULT_SUBJECT)
    body_tmpl = _BODY_TEMPLATES.get(kind, _DEFAULT_BODY)

    subject = subject_tmpl.format(content=content_short) if "{content}" in subject_tmpl else subject_tmpl
    body = body_tmpl.format(content=content, deep_link=deep_link or "(none)")
    return subject.strip(), body


class EmailChannel:
    """SMTP delivery channel with TLS + auth via the secret backend."""

    def __init__(self) -> None:
        pass

    @property
    def name(self) -> str:
        return "email"

    async def deliver(
        self,
        event: dict[str, Any],
        channel_cfg: dict[str, Any],
        resolver: "SecretResolver",
    ) -> dict[str, Any]:
        host = channel_cfg["smtp_host"]
        port = int(channel_cfg["smtp_port"])
        from_addr = channel_cfg["from_addr"]
        to_addr = channel_cfg["to_addr"]
        use_tls = bool(channel_cfg.get("use_tls", True))
        secret_uri = channel_cfg.get("secret_uri")

        subject, body = render_email(event)

        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.set_content(body)

        password: str | None = None
        if secret_uri:
            try:
                password = (await resolver.resolve(secret_uri)).decode("utf-8")
            except Exception as exc:
                log.warning("email secret resolve failed uri=%s: %s", secret_uri, exc)
                return {"status": "failed", "error": f"secret resolve failed: {exc}"}

        try:
            await asyncio.to_thread(
                _smtp_send,
                host,
                port,
                from_addr,
                to_addr,
                msg,
                password,
                use_tls,
            )
        except Exception as exc:
            log.warning(
                "email failed to=%s host=%s error=%s",
                _redact_addr(to_addr),
                host,
                exc,
            )
            return {"status": "failed", "error": str(exc)}

        log.info(
            "email delivered to=%s host=%s subject=%r",
            _redact_addr(to_addr),
            host,
            subject[:60],
        )
        return {"status": "delivered"}

    async def close(self) -> None:
        pass


def _smtp_send(
    host: str,
    port: int,
    from_addr: str,
    to_addr: str,
    msg: EmailMessage,
    password: str | None,
    use_tls: bool,
) -> None:
    """Synchronous SMTP send — called via ``asyncio.to_thread``."""
    server = smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT)
    try:
        server.ehlo()
        if use_tls:
            # Validate the server certificate: a default SSL context enforces
            # hostname + CA-chain verification, preventing MITM/cert-spoofing
            # on the STARTTLS upgrade.
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        if password:
            server.login(from_addr, password)
        server.send_message(msg)
    finally:
        server.quit()


def _redact_addr(addr: str) -> str:
    """Redact the local part of an email address for safe logging."""
    parts = addr.split("@", 1)
    if len(parts) == 2:
        local, domain = parts
        if local:
            return local[0] + "***@" + domain
    return "***"
