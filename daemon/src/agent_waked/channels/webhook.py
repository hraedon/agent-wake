"""Outbound webhook delivery channel.

A generic signed-webhook deliverer (Plan 005 WI-1.2): a suite component
posts a notification event to wake's ingress; wake delivers it to the
principal's configured webhook with retry + idempotency key.  Secret custody
via the Plan 004 backend contract (``secret_uri`` resolved by SecretResolver).

The outbound webhook is HMAC-SHA256 signed with the principal's webhook
secret — the same scheme the daemon uses for inbound ingest, so a receiver
that already knows how to verify agent-wake webhooks can verify these too.
The event_id is sent as an ``Idempotency-Key`` header so the receiver can
deduplicate retries (at-least-once delivery, Plan 005 principle).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any

from aiohttp import ClientSession, ClientTimeout

if False:  # TYPE_CHECKING
    from ..secrets.resolver import SecretResolver

log = logging.getLogger("agent_waked.channels.webhook")

_WEBHOOK_TIMEOUT = ClientTimeout(total=30)
_MAX_RETRIES = 3
_BACKOFF_DELAYS: tuple[float, ...] = (1.0, 4.0, 16.0)


class WebhookChannel:
    """Signed outbound webhook deliverer with retry + idempotency."""

    def __init__(
        self,
        max_retries: int = _MAX_RETRIES,
        backoff_delays: tuple[float, ...] = _BACKOFF_DELAYS,
    ) -> None:
        self._max_retries = max_retries
        self._backoff_delays = backoff_delays
        self._session: ClientSession | None = None

    @property
    def name(self) -> str:
        return "webhook"

    def _ensure_session(self) -> ClientSession:
        if self._session is None or self._session.closed:
            self._session = ClientSession(timeout=_WEBHOOK_TIMEOUT)
        return self._session

    async def deliver(
        self,
        event: dict[str, Any],
        channel_cfg: dict[str, Any],
        resolver: "SecretResolver",
    ) -> dict[str, Any]:
        url = channel_cfg["url"]
        secret_uri = channel_cfg["secret_uri"]

        try:
            secret = await resolver.resolve(secret_uri)
        except Exception as exc:
            log.warning("webhook secret resolve failed uri=%s: %s", secret_uri, exc)
            return {"status": "failed", "error": f"secret resolve failed: {exc}"}

        body = json.dumps(event, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
        event_id = str(event.get("event_id", ""))

        headers = {
            "Content-Type": "application/json",
            "X-AgentWake-Signature": f"sha256={signature}",
            "Idempotency-Key": event_id,
        }

        session = self._ensure_session()
        last_error: str | None = None
        last_http_status: int | None = None

        for attempt in range(self._max_retries):
            t0 = time.monotonic()
            try:
                async with session.post(url, data=body, headers=headers) as resp:
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    if 200 <= resp.status < 300:
                        log.info(
                            "webhook delivered url=%s http=%d duration_ms=%d "
                            "attempt=%d event_id=%s",
                            _redact_url(url),
                            resp.status,
                            elapsed_ms,
                            attempt + 1,
                            event_id,
                        )
                        return {"status": "delivered", "http_status": resp.status}
                    last_http_status = resp.status
                    last_error = f"HTTP {resp.status}"
                    log.warning(
                        "webhook failed url=%s http=%d duration_ms=%d "
                        "attempt=%d/%d event_id=%s",
                        _redact_url(url),
                        resp.status,
                        elapsed_ms,
                        attempt + 1,
                        self._max_retries,
                        event_id,
                    )
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                msg = str(exc) if str(exc) else type(exc).__name__
                if isinstance(exc, asyncio.TimeoutError):
                    msg = "webhook timed out"
                last_error = msg
                last_http_status = None
                log.warning(
                    "webhook failed url=%s error=%s duration_ms=%d "
                    "attempt=%d/%d event_id=%s",
                    _redact_url(url),
                    msg,
                    elapsed_ms,
                    attempt + 1,
                    self._max_retries,
                    event_id,
                )

            if attempt < self._max_retries - 1:
                delay = self._backoff_delays[
                    min(attempt, len(self._backoff_delays) - 1)
                ]
                log.info(
                    "webhook retry url=%s event_id=%s backoff=%.1fs",
                    _redact_url(url),
                    event_id,
                    delay,
                )
                await asyncio.sleep(delay)

        log.warning(
            "webhook permanently failed url=%s event_id=%s error=%s",
            _redact_url(url),
            event_id,
            last_error,
        )
        return {
            "status": "failed",
            "http_status": last_http_status,
            "error": last_error,
        }

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None


def _redact_url(url: str) -> str:
    """Redact credentials from a URL for safe logging."""
    if "@" in url:
        scheme_sep = url.find("://")
        if scheme_sep > 0:
            rest = url[scheme_sep + 3 :]
            at = rest.find("@")
            if at > 0:
                return url[: scheme_sep + 3] + "***@" + rest[at + 1 :]
    return url
