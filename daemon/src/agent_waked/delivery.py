"""Human-directed delivery dispatcher (Plan 005 WI-1.1).

After gating + dedupe, the ingest handler checks ``event["meta"]["target"]``
for a principal_id.  If present, the ``HumanDelivery`` dispatcher resolves
the principal → delivery channel(s) from the daemon's ``delivery`` config
block and dispatches the event to each configured channel (webhook, email).

This is the same principal vocabulary as regista (Plan 026 enrollment),
not a parallel identity: ``principal_id`` is the opaque on-the-wire
identifier from ``design/v1-implementation-spec.md`` §1, and the delivery
routing table is a config-time mapping from that identifier to one or more
delivery channel configs.

At-least-once with idempotency keys (Plan 005 principle): each channel
deliverer includes the event_id as an idempotency token so the receiver can
deduplicate retries.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .channels.email import EmailChannel
from .channels.webhook import WebhookChannel
from .secrets.resolver import SecretResolver

if TYPE_CHECKING:
    from .store import WakeStore

log = logging.getLogger("agent_waked.delivery")


class DeliveryHealth:
    """In-memory tracker for delivery channel health (consumed by doctor)."""

    def __init__(self) -> None:
        self._failures: dict[tuple[str, str], dict[str, Any]] = {}
        self._unknown_principals: set[str] = set()
        self._delivered: int = 0

    def record_success(self, channel: str, principal_id: str) -> None:
        self._failures.pop((channel, principal_id), None)
        self._delivered += 1

    def record_failure(
        self, channel: str, principal_id: str, error: str
    ) -> None:
        key = (channel, principal_id)
        self._failures.setdefault(key, {})
        self._failures[key]["last_error"] = error

    def record_unknown_principal(self, principal_id: str) -> None:
        self._unknown_principals.add(principal_id)

    def summary(self) -> dict[str, Any]:
        failing_channels = [
            {"channel": ch, "principal_id": pid, **info}
            for (ch, pid), info in sorted(self._failures.items())
        ]
        return {
            "delivered": self._delivered,
            "failing_channels": failing_channels,
            "unknown_principals": sorted(self._unknown_principals),
        }

    def has_failures(self) -> bool:
        return bool(self._failures or self._unknown_principals)


class HumanDelivery:
    """Resolves ``meta.target`` → delivery channels and dispatches.

    The daemon constructs one ``HumanDelivery`` with all registered channel
    adapters (webhook, email).  Each channel handles its own secret custody
    via the shared ``SecretResolver``.

    When a ``WakeStore`` is supplied, a delivery that does not end
    ``delivered`` is dead-lettered as ``kind="human_delivery"``, mirroring the
    reply / next_session paths, so a routed alert that never landed survives a
    restart and can be redriven (``agent-wake dead-letter redrive``).
    """

    def __init__(
        self,
        config: dict[str, Any],
        resolver: SecretResolver,
        health: DeliveryHealth | None = None,
        channels: dict[str, Any] | None = None,
        store: WakeStore | None = None,
    ) -> None:
        self._config = config
        self._resolver = resolver
        self._health = health or DeliveryHealth()
        self._store = store
        if channels is not None:
            self._channels = channels
        else:
            self._channels = {
                "webhook": WebhookChannel(),
                "email": EmailChannel(),
            }

    def _dead_letter(
        self,
        *,
        event: dict[str, Any],
        principal_id: str,
        error: str,
    ) -> str | None:
        """Persist a permanently-failed human delivery for operator redrive.

        Without this, a channel that never recovers left nothing but an
        in-memory health entry lost on restart — a routed alert could vanish
        with no durable trace.

        **Never raises.** Every caller is on a path that has already failed;
        this is the last line of defence, and a last line of defence that can
        itself throw turns a failed delivery into an unhandled exception in a
        background task. Two real cases: the store was closed under us (a
        shutdown race — ``sqlite3.ProgrammingError``), or the write lost the
        lock race past sqlite's busy timeout (``OperationalError: database is
        locked``). Both are logged loudly with the identifying metadata; the
        event body is deliberately *not* logged, because dead-letter payloads
        carry user content and log sinks are not the durable store.

        A transient lock is already covered by sqlite's connect-level busy
        timeout (5s), so there is no retry loop here — a failure at this point
        means the store is genuinely unavailable, not merely contended.
        """
        if self._store is None:
            return None
        source = str(event.get("source", ""))
        ref_id = str(event.get("event_id", ""))
        try:
            return self._store.dead_letter(
                kind="human_delivery",
                source=source,
                ref_id=ref_id,
                payload={"event": event, "principal_id": principal_id},
                error=error,
                attempts=1,
            )
        except Exception as exc:
            log.error(
                "LOST ALERT: could not dead-letter human delivery "
                "source=%s event_id=%s principal_id=%s delivery_error=%r "
                "store_error=%s",
                source,
                ref_id,
                principal_id,
                error,
                exc,
                exc_info=exc,
            )
            return None

    @property
    def health(self) -> DeliveryHealth:
        return self._health

    def resolve_principal(
        self, principal_id: str
    ) -> dict[str, dict[str, Any]] | None:
        """Return the channel configs for *principal_id*, or None if unknown."""
        principals = self._config.get("delivery", {})
        if not isinstance(principals, dict):
            return None
        result = principals.get(principal_id)
        if not isinstance(result, dict):
            return None
        return result

    async def deliver(self, event: dict[str, Any]) -> dict[str, Any]:
        """Deliver *event* to the principal named in ``meta.target``.

        Returns a dict:
          - ``{"status": "no_target"}`` — no ``meta.target`` set; no delivery.
          - ``{"status": "unknown_principal", "principal_id": ...}`` — principal
            not in the routing table; rejected loudly, not dropped.
          - ``{"status": "no_channels", "principal_id": ...}`` — principal
            exists but has zero delivery channels configured.
          - ``{"status": "delivered"|"failed"|"partial", ...}`` — dispatch
            result across all configured channels.
        """
        meta = event.get("meta", {})
        if not isinstance(meta, dict):
            return {"status": "no_target"}

        target = meta.get("target")
        if not target:
            return {"status": "no_target"}

        principal_id = str(target)
        channels_cfg = self.resolve_principal(principal_id)

        if channels_cfg is None:
            self._health.record_unknown_principal(principal_id)
            log.error(
                "delivery rejected: unknown principal %r event_id=%s",
                principal_id,
                event.get("event_id", "?"),
            )
            return {"status": "unknown_principal", "principal_id": principal_id}

        if not channels_cfg:
            log.warning(
                "principal %r has no delivery channels configured event_id=%s",
                principal_id,
                event.get("event_id", "?"),
            )
            self._dead_letter(
                event=event,
                principal_id=principal_id,
                error="principal has no delivery channels configured",
            )
            return {"status": "no_channels", "principal_id": principal_id}

        results: list[dict[str, Any]] = []
        any_delivered = False
        any_failed = False

        try:
            for kind, ch_cfg in channels_cfg.items():
                channel = self._channels.get(kind)
                if channel is None:
                    log.error(
                        "no adapter registered for channel kind %r (principal %r)",
                        kind,
                        principal_id,
                    )
                    any_failed = True
                    self._health.record_failure(kind, principal_id, "no adapter")
                    results.append(
                        {"channel": kind, "status": "failed", "error": "no adapter"}
                    )
                    continue

                try:
                    result = await channel.deliver(event, ch_cfg, self._resolver)
                except Exception as exc:
                    log.exception(
                        "channel %r raised for principal %r: %s",
                        kind,
                        principal_id,
                        exc,
                    )
                    result = {"status": "failed", "error": str(exc)}

                status = result.get("status", "unknown")
                if status == "delivered":
                    self._health.record_success(kind, principal_id)
                    any_delivered = True
                else:
                    self._health.record_failure(
                        kind, principal_id, result.get("error", status)
                    )
                    any_failed = True

                results.append({"channel": kind, **result})
        except asyncio.CancelledError:
            # CancelledError is a BaseException: it does not pass through the
            # ``except Exception`` above, and without this arm a SIGTERM while
            # a channel sits in 1s/4s/16s backoff cancelled the task and the
            # alert left *no* durable trace — precisely the gap the
            # dead-letter table exists to close. The store write is
            # synchronous, so it completes before the cancellation is
            # re-raised; the daemon's shutdown order guarantees the store is
            # still open at this point (see main._run).
            delivered = [
                r.get("channel", "?")
                for r in results
                if r.get("status") == "delivered"
            ]
            self._dead_letter(
                event=event,
                principal_id=principal_id,
                error=(
                    "human delivery cancelled mid-flight (daemon shutdown); "
                    f"delivered channels={delivered}"
                ),
            )
            raise

        if any_delivered and not any_failed:
            overall = "delivered"
        elif any_delivered and any_failed:
            overall = "partial"
        else:
            overall = "failed"

        if overall != "delivered":
            failed_channels = [
                r.get("channel", "?")
                for r in results
                if r.get("status") != "delivered"
            ]
            self._dead_letter(
                event=event,
                principal_id=principal_id,
                error=f"human delivery {overall}: failed channels={failed_channels}",
            )

        return {
            "status": overall,
            "principal_id": principal_id,
            "channels": results,
        }

    async def close(self) -> None:
        for channel in self._channels.values():
            try:
                await channel.close()
            except Exception:
                log.debug("channel close error for %r", channel.name)
