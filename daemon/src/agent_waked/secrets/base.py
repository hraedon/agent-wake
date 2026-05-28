"""SecretBackend Protocol — the interface every backend must satisfy."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class SecretBackend(Protocol):
    """Abstract interface for a secret backend.

    Each backend handles one or more URI schemes (e.g. "env", "vault").
    The resolver calls ``supports`` to route URIs to the right backend.
    """

    async def resolve(self, uri: str) -> bytes:
        """Return the secret value for *uri*, using cache if still fresh."""
        ...

    async def refresh(self, uri: str) -> bytes:
        """Bypass the cache and re-fetch the secret for *uri*."""
        ...

    def supports(self, scheme: str) -> bool:
        """Return True if this backend handles *scheme* (e.g. "env")."""
        ...
