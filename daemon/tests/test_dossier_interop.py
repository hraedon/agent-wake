"""Contract proof for dossier Plan 018 → agent-wake Plan 005 delivery."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent_waked.ingest import create_ingest_app


class _Router:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def deliver(self, event: dict[str, Any]) -> str:
        self.events.append(event)
        return "no_subscriber"


class _Delivery:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def resolve_principal(self, principal_id: str) -> dict[str, Any] | None:
        if principal_id in {"human:reviewer", "human:other"}:
            return {"email": {}}
        return None

    async def deliver(self, event: dict[str, Any]) -> dict[str, Any]:
        self.events.append(event)
        return {"status": "delivered"}


@pytest.mark.asyncio
async def test_authenticated_dossier_event_routes_to_named_human() -> None:
    secret = b"d" * 32
    config = {
        "sources": {
            "dossier": {
                "secret": secret,
                "allowed_trigger_identities": ["service:dossier"],
                "allowed_target_principals": ["human:reviewer"],
            }
        },
        "routing": {},
    }
    event = {
        "v": 0,
        "event_id": "dossier-event-1",
        "source": "dossier",
        "kind": "awaiting_your_accept",
        "content": "item submitted for review — awaiting your accept",
        "wake": False,
        "meta": {
            "target": "human:reviewer",
            "deep_link": "https://dossier.example/p/project-example/issues/item-1",
            "project": "project-example",
            "item_id": "item-1",
            "item_key": "ITEM-1",
            "item_title": "Review the change",
        },
    }
    body = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
    router = _Router()
    delivery = _Delivery()
    app = create_ingest_app(config, router, delivery=delivery)  # type: ignore[arg-type]
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/",
            data=body,
            headers={
                "X-AgentWake-Source": "dossier",
                "X-AgentWake-Signature": f"sha256={signature}",
                "X-AgentWake-Event-Id": "dossier-event-1",
                "X-AgentWake-Identity": "service:dossier",
            },
        )
        assert response.status == 202
        result = await response.json()
        assert result["delivery"] == {
            "status": "dispatched",
            "principal_id": "human:reviewer",
        }
        await asyncio.sleep(0)
    finally:
        await client.close()

    assert delivery.events == [event]
    assert router.events == [event]


@pytest.mark.asyncio
async def test_dossier_event_cannot_target_unapproved_principal() -> None:
    secret = b"d" * 32
    config = {
        "sources": {
            "dossier": {
                "secret": secret,
                "allowed_target_principals": ["human:reviewer"],
            }
        },
        "routing": {},
    }
    event = {
        "v": 0,
        "event_id": "dossier-event-denied",
        "source": "dossier",
        "kind": "awaiting_your_accept",
        "content": "review requested",
        "wake": False,
        "meta": {"target": "human:other"},
    }
    body = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
    router = _Router()
    delivery = _Delivery()
    app = create_ingest_app(config, router, delivery=delivery)  # type: ignore[arg-type]
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/",
            data=body,
            headers={
                "X-AgentWake-Source": "dossier",
                "X-AgentWake-Signature": f"sha256={signature}",
            },
        )
        assert response.status == 403
        result = await response.json()
        assert result["error"] == "source not authorized to deliver to principal"
    finally:
        await client.close()

    assert delivery.events == []
    assert router.events == []
