"""DeliveryChannel Protocol — the interface every human-delivery backend satisfies.

Mirrors the ``SecretBackend`` pattern: a protocol with a single ``deliver``
method.  Each channel handles one delivery medium (webhook, email) and
resolves its own secrets via the shared ``SecretResolver``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

if False:  # TYPE_CHECKING
    from ..secrets.resolver import SecretResolver


@runtime_checkable
class DeliveryChannel(Protocol):
    """Interface for a human-delivery channel adapter."""

    @property
    def name(self) -> str:
        """Channel kind identifier (e.g. ``"webhook"``, ``"email"``)."""
        ...

    async def deliver(
        self,
        event: dict[str, Any],
        channel_cfg: dict[str, Any],
        resolver: SecretResolver,
    ) -> dict[str, Any]:
        """Deliver *event* via this channel.

        Args:
            event: The v0 wake event to deliver.
            channel_cfg: This principal's config for this channel kind
                (validated by ``config._validate_channel``).
            resolver: Shared secret resolver (Plan 004 backend contract).

        Returns:
            A result dict with at least ``{"status": "delivered"|"failed"}``
            and optionally ``{"error": str, "detail": str}``.
        """
        ...

    async def close(self) -> None:
        """Release any persistent resources (HTTP sessions, SMTP connections)."""
        ...
