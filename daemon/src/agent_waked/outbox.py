"""Outbound reply delivery.

Spec reference: v1-daemon-spec.md §8.

Wraps an ``aiohttp.ClientSession`` used to POST v0 reply objects to
per-source callback URLs.  Fire-once, 30-second timeout, no retries.
"""

import asyncio
import logging
import time

from aiohttp import ClientSession, ClientTimeout

log = logging.getLogger("agent_waked.outbox")

_REPLY_TIMEOUT = ClientTimeout(total=30)


class Outbox:
    def __init__(self, config: dict):
        self._config = config
        self._session: ClientSession | None = None

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
    ) -> dict:
        """Deliver a reply to the configured callback URL.

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

        t0 = time.monotonic()
        try:
            async with self._session.post(
                callback_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as resp:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                if 200 <= resp.status < 300:
                    log.info(
                        "reply delivered source=%s reply_id=%s http=%d duration_ms=%d",
                        source,
                        reply_id,
                        resp.status,
                        elapsed_ms,
                    )
                    return {
                        "reply_id": reply_id,
                        "status": "delivered",
                        "http_status": resp.status,
                        "error": None,
                    }
                log.warning(
                    "reply failed source=%s reply_id=%s http=%d duration_ms=%d",
                    source,
                    reply_id,
                    resp.status,
                    elapsed_ms,
                )
                return {
                    "reply_id": reply_id,
                    "status": "failed",
                    "http_status": resp.status,
                    "error": f"HTTP {resp.status}",
                }
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            msg = str(exc) if str(exc) else type(exc).__name__
            if isinstance(exc, asyncio.TimeoutError):
                msg = "callback timed out"
            log.warning(
                "reply failed source=%s reply_id=%s error=%s duration_ms=%d",
                source,
                reply_id,
                msg,
                elapsed_ms,
            )
            return {
                "reply_id": reply_id,
                "status": "failed",
                "http_status": None,
                "error": msg,
            }
