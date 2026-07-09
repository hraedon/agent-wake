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

import logging
from typing import Any

from .channels.email import EmailChannel
from .channels.webhook import WebhookChannel
from .secrets.resolver import SecretResolver

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
    """

    def __init__(
        self,
        config: dict[str, Any],
        resolver: SecretResolver,
        health: DeliveryHealth | None = None,
        channels: dict[str, Any] | None = None,
    ) -> None:
        self._config = config
        self._resolver = resolver
        self._health = health or DeliveryHealth()
        if channels is not None:
            self._channels = channels
        else:
            self._channels = {
                "webhook": WebhookChannel(),
                "email": EmailChannel(),
            }

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
            return {"status": "no_channels", "principal_id": principal_id}

        results: list[dict[str, Any]] = []
        any_delivered = False
        any_failed = False

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

        if any_delivered and not any_failed:
            overall = "delivered"
        elif any_delivered and any_failed:
            overall = "partial"
        else:
            overall = "failed"

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
