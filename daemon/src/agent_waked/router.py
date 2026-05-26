"""Source → adapter routing.

Spec reference: v1-daemon-spec.md §5.4.

The router holds a subscriber entry per connected adapter (keyed by
daemon-minted ``session_id``) and resolves inbound events to the
correct subscriber based on the daemon config's ``routing`` block.
"""

import logging

from ulid import ULID

from .socket_server import ClientConnection

log = logging.getLogger("agent_waked.router")


class _Subscriber:
    __slots__ = ("session_id", "adapter", "instance", "sources", "connection")

    def __init__(
        self,
        session_id: str,
        adapter: str,
        instance: str,
        sources: list[str],
        connection: ClientConnection,
    ):
        self.session_id = session_id
        self.adapter = adapter
        self.instance = instance
        self.sources = sources
        self.connection = connection


class Router:
    def __init__(self, config: dict):
        self._config = config
        self._subscribers: dict[str, _Subscriber] = {}
        self._order: list[str] = []

    def subscribe(
        self,
        session_id: str,
        adapter: str,
        instance: str,
        sources: list[str],
        connection: ClientConnection,
    ) -> None:
        sub = _Subscriber(session_id, adapter, instance, sources, connection)
        self._subscribers[session_id] = sub
        self._order.append(session_id)

    def unsubscribe(self, session_id: str) -> None:
        self._subscribers.pop(session_id, None)

    async def deliver(self, event: dict) -> str:
        source = event.get("source", "")
        target = self._resolve(source)
        if target is None:
            log.info("no_subscriber source=%s", source)
            return "no_subscriber"
        ack_id = str(ULID())
        wake_frame = {"type": "wake", "ack_id": ack_id, "event": event}
        await target.connection.send_frame(wake_frame)
        log.info(
            "delivered source=%s ack_id=%s session_id=%s",
            source,
            ack_id,
            target.session_id,
        )
        return "queued"

    def accepted_sources_for(self, adapter: str, requested: list[str]) -> list[str]:
        """Compute the intersection used in ``hello_ack.accepted_sources``."""
        routing = self._config.get("routing", {})
        configured = set(self._config.get("sources", {}).keys())
        accepted: list[str] = []
        for s in requested:
            if s not in configured:
                continue
            route = routing.get(s)
            if route and route.get("adapter"):
                if route["adapter"] == adapter:
                    accepted.append(s)
            else:
                accepted.append(s)
        return accepted

    def _resolve(self, source: str) -> _Subscriber | None:
        routing = self._config.get("routing", {})
        route = routing.get(source)

        if route and route.get("adapter"):
            adapter_name = route["adapter"]
            for sub in self._subscribers.values():
                if sub.adapter == adapter_name and source in sub.sources:
                    return sub
            return None

        for sid in reversed(self._order):
            sub = self._subscribers.get(sid)
            if sub and source in sub.sources:
                return sub
        return None
