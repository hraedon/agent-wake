"""Outbound reply delivery.

Spec reference: v1-daemon-spec.md §8.

Wraps an ``aiohttp.ClientSession`` used to POST v0 reply objects to
per-source callback URLs.  Retries up to 3 times with exponential
backoff (1s / 4s / 16s).  On permanent failure, logs at warning level.
"""

import asyncio
import logging
import time
from typing import Any

from aiohttp import ClientSession, ClientTimeout

log = logging.getLogger("agent_waked.outbox")

_REPLY_TIMEOUT = ClientTimeout(total=30)
_MAX_RETRIES = 3
_BACKOFF_DELAYS = (1.0, 4.0, 16.0)


class Outbox:
    def __init__(
        self,
        config: dict[str, Any],
        max_retries: int = _MAX_RETRIES,
        backoff_delays: tuple[float, ...] = _BACKOFF_DELAYS,
    ):
        self._config = config
        self._session: ClientSession | None = None
        self._max_retries = max_retries
        self._backoff_delays = backoff_delays

    async def start(self) -> None:
        self._session = ClientSession(timeout=_REPLY_TIMEOUT)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def deliver(
        self,
        source: str,
        reply_id: str,
        in_reply_to: str,
        content: str,
    ) -> dict[str, Any]:
        """Deliver a reply to the configured callback URL.

        Retries up to ``_MAX_RETRIES`` times with exponential backoff.
        Returns a dict matching the ``reply_result`` payload fields
        (minus the ``"type"`` key) per spec §4.3.7.
        """
        sources = self._config.get("sources", {})
        source_cfg = sources.get(source, {})
        callback_url = source_cfg.get("callback_url") or self._config.get(
            "default_callback_url"
        )

        if not callback_url:
            log.info("reply no_callback source=%s reply_id=%s", source, reply_id)
            return {
                "reply_id": reply_id,
                "status": "no_callback",
                "http_status": None,
                "error": None,
            }

        payload = {
            "v": 0,
            "in_reply_to": in_reply_to,
            "content": content,
            "meta": {},
        }

        last_error: str | None = None
        last_http_status: int | None = None

        for attempt in range(self._max_retries):
            t0 = time.monotonic()
            try:
                if self._session is None:
                    raise RuntimeError("outbox not started")
                async with self._session.post(
                    callback_url,
                    json=payload,
                ) as resp:
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    if 200 <= resp.status < 300:
                        log.info(
                            "reply delivered source=%s reply_id=%s http=%d "
                            "duration_ms=%d attempt=%d",
                            source,
                            reply_id,
                            resp.status,
                            elapsed_ms,
                            attempt + 1,
                        )
                        return {
                            "reply_id": reply_id,
                            "status": "delivered",
                            "http_status": resp.status,
                            "error": None,
                        }
                    last_http_status = resp.status
                    last_error = f"HTTP {resp.status}"
                    log.warning(
                        "reply failed source=%s reply_id=%s http=%d "
                        "duration_ms=%d attempt=%d/%d",
                        source,
                        reply_id,
                        resp.status,
                        elapsed_ms,
                        attempt + 1,
                        self._max_retries,
                    )
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                msg = str(exc) if str(exc) else type(exc).__name__
                if isinstance(exc, asyncio.TimeoutError):
                    msg = "callback timed out"
                last_error = msg
                log.warning(
                    "reply failed source=%s reply_id=%s error=%s "
                    "duration_ms=%d attempt=%d/%d",
                    source,
                    reply_id,
                    msg,
                    elapsed_ms,
                    attempt + 1,
                    self._max_retries,
                )

            if attempt < self._max_retries - 1:
                delay = self._backoff_delays[min(attempt, len(self._backoff_delays) - 1)]
                log.info(
                    "reply retry source=%s reply_id=%s backoff=%.1fs",
                    source,
                    reply_id,
                    delay,
                )
                await asyncio.sleep(delay)

        log.warning(
            "reply permanently failed source=%s reply_id=%s error=%s",
            source,
            reply_id,
            last_error,
        )
        return {
            "reply_id": reply_id,
            "status": "failed",
            "http_status": last_http_status,
            "error": last_error,
        }
