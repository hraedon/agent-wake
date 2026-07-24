"""Outbound reply delivery.

Spec reference: v1-daemon-spec.md §8.

Wraps an ``aiohttp.ClientSession`` used to POST v0 reply objects to
per-source callback URLs.  Retries up to 3 times with exponential
backoff (1s / 4s / 16s).  On permanent failure the reply is **dead-lettered**
into the durable store (BC-WAKE-012) so an operator can list, inspect and
redrive it — previously the retries were exhausted and the reply was lost with
nothing but a warning line.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from aiohttp import ClientSession, ClientTimeout

if TYPE_CHECKING:
    from .store import WakeStore

log = logging.getLogger("agent_waked.outbox")

_REPLY_TIMEOUT = ClientTimeout(total=30)
_MAX_RETRIES = 3
_BACKOFF_DELAYS = (1.0, 4.0, 16.0)
_ALLOWED_SCHEMES = ("http:", "https:")


def _validate_callback_url(url: str) -> str | None:
    """Return an error string if *url* is unsafe, or None if valid."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"disallowed scheme {parsed.scheme!r}"
    return None


class Outbox:
    def __init__(
        self,
        config: dict[str, Any],
        max_retries: int = _MAX_RETRIES,
        backoff_delays: tuple[float, ...] = _BACKOFF_DELAYS,
        store: "WakeStore | None" = None,
    ):
        self._config = config
        self._session: ClientSession | None = None
        self._max_retries = max_retries
        self._backoff_delays = backoff_delays
        self._store = store

    def _dead_letter(
        self,
        *,
        source: str,
        reply_id: str,
        in_reply_to: str,
        content: str,
        error: str | None,
        attempts: int,
    ) -> str | None:
        """Persist a permanently-failed reply for operator redrive."""
        if self._store is None:
            return None
        return self._store.dead_letter(
            kind="reply",
            source=source,
            ref_id=reply_id,
            payload={
                "source": source,
                "reply_id": reply_id,
                "in_reply_to": in_reply_to,
                "content": content,
            },
            error=error,
            attempts=attempts,
        )

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

        url_err = _validate_callback_url(callback_url)
        if url_err:
            log.warning(
                "reply rejected source=%s reply_id=%s url=%s error=%s",
                source,
                reply_id,
                callback_url,
                url_err,
            )
            self._dead_letter(
                source=source,
                reply_id=reply_id,
                in_reply_to=in_reply_to,
                content=content,
                error=url_err,
                attempts=0,
            )
            return {
                "reply_id": reply_id,
                "status": "rejected",
                "http_status": None,
                "error": url_err,
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
        dead_letter_id = self._dead_letter(
            source=source,
            reply_id=reply_id,
            in_reply_to=in_reply_to,
            content=content,
            error=last_error,
            attempts=self._max_retries,
        )
        result: dict[str, Any] = {
            "reply_id": reply_id,
            "status": "failed",
            "http_status": last_http_status,
            "error": last_error,
        }
        if dead_letter_id is not None:
            result["dead_letter_id"] = dead_letter_id
        return result
