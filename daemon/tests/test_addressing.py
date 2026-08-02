"""Acceptance tests for the addressing model (WI-006).

Each section names the absorbed item it discharges.  These are the tests WI-006
is measured by, so they assert the *behaviour* the old model could not express
rather than the shape of the new config:

* BC-001      a session-scoped destination is woken and its siblings are not
* BC-WAKE-022 one sender addresses several destinations
* BC-WAKE-010 a per-destination connection cap is expressible and enforced
* BC-WAKE-012 the durable queue is per destination, not per sender
* BC-011      the heartbeat does not reap a connection that owes an ack
* migration   a v0/v1 file keeps working, proven against the live config
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agent_waked import addressing
from agent_waked.config import ConfigError, load_config
from agent_waked.proto import encode_frame, validate_frame
from agent_waked.router import Router
from agent_waked.socket_server import MAX_BUSY_MISSED_PONGS, SocketServer
from agent_waked.store import WakeStore

# ── helpers ───────────────────────────────────────────────────────────────────


class RecordingConnection:
    """Adapter stand-in: records frames, optionally auto-acks or fails."""

    def __init__(self, router: Router | None = None, auto_ack: bool = True):
        self.sent: list[dict[str, Any]] = []
        self._router = router
        self._auto_ack = auto_ack

    async def send_frame(self, frame: dict[str, Any]) -> None:
        self.sent.append(frame)
        if self._router and self._auto_ack and frame.get("type") == "wake":
            self._router.resolve_ack(frame.get("ack_id", ""), "ack")

    def close(self) -> None:
        pass

    @property
    def destination_names(self) -> list[str]:
        return [f["destination"]["name"] for f in self.sent if f["type"] == "wake"]

    @property
    def sessions(self) -> list[str | None]:
        return [
            f["destination"].get("session") for f in self.sent if f["type"] == "wake"
        ]


def _event(
    event_id: str = "ev-1", source: str = "ops", **meta: Any
) -> dict[str, Any]:
    """A minimal wake event. Keyword args become ``meta`` entries."""
    return {
        "v": 0,
        "event_id": event_id,
        "source": source,
        "kind": "webhook",
        "content": "build failed",
        "meta": dict(meta),
        "wake": True,
    }


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _two_session_config() -> dict[str, Any]:
    """One opencode adapter multiplexing two sessions of one principal.

    Exactly the BC-001 scenario: before destinations existed, both sessions were
    reachable only as the single source name ``ops``, so the adapter had no
    addressee to discriminate on and prompted every live session.
    """
    return {
        "version": 2,
        "senders": {"ops": {"secret": b"k1"}},
        "principals": {"agent:dev": {}},
        "destinations": {
            "dev-a": {
                "adapter": "opencode",
                "session": "sess-a",
                "principal": "agent:dev",
            },
            "dev-b": {
                "adapter": "opencode",
                "session": "sess-b",
                "principal": "agent:dev",
            },
        },
        "routes": [{"sender": "ops", "destinations": ["dev-a", "dev-b"]}],
    }


def _loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: dict[str, Any]):
    cfg_path = tmp_path / "config.json"
    _write(cfg_path, raw)
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    return load_config()


# ── BC-001: session-scoped delivery does not fan out ──────────────────────────


@pytest.mark.asyncio
async def test_session_scoped_destination_wakes_only_that_session():
    """BC-001: addressing one session must not touch its siblings."""
    cfg = _two_session_config()
    router = Router(cfg)
    conn_a = RecordingConnection(router)
    conn_b = RecordingConnection(router)
    router.subscribe("s1", "opencode", "oc", [], conn_a, destinations=["dev-a"])
    router.subscribe("s2", "opencode", "oc", [], conn_b, destinations=["dev-b"])

    result = await router.deliver(_event(destination="dev-a"))

    assert result == "queued"
    assert conn_a.destination_names == ["dev-a"]
    assert conn_a.sessions == ["sess-a"]
    # The whole point: the sibling session saw nothing.
    assert conn_b.sent == []


@pytest.mark.asyncio
async def test_wake_frame_carries_the_addressed_session():
    """The addressee is on the wire, so the adapter need not guess."""
    cfg = _two_session_config()
    router = Router(cfg)
    conn = RecordingConnection(router)
    router.subscribe("s1", "opencode", "oc", [], conn, destinations=["dev-b"])

    await router.deliver(_event(destination="dev-b"))

    frame = conn.sent[0]
    assert validate_frame(frame) is None
    assert frame["destination"] == {
        "name": "dev-b",
        "adapter": "opencode",
        "session": "sess-b",
        "principal": "agent:dev",
    }


@pytest.mark.asyncio
async def test_narrowing_to_an_unrouted_destination_is_refused():
    """A narrowing outside the sender's routes is authorization, not silence."""
    cfg = _two_session_config()
    cfg["routes"] = [{"sender": "ops", "destinations": ["dev-a"]}]
    router = Router(cfg)
    conn = RecordingConnection(router)
    router.subscribe("s2", "opencode", "oc", [], conn, destinations=["dev-b"])

    result = await router.deliver(_event(destination="dev-b"))

    assert result == "destination_not_routed"
    assert conn.sent == []


# ── BC-WAKE-022: one sender, several destinations ─────────────────────────────


@pytest.mark.asyncio
async def test_one_sender_addresses_multiple_destinations():
    """BC-WAKE-022: fan-out across adapters from a single sender."""
    cfg = {
        "version": 2,
        "senders": {"alerts": {"secret": b"k1"}},
        "principals": {"agent:me": {}},
        "destinations": {
            "my-claude": {"adapter": "claude", "principal": "agent:me"},
            "my-opencode": {"adapter": "opencode", "principal": "agent:me"},
        },
        "routes": [{"sender": "alerts", "principal": "agent:me"}],
    }
    router = Router(cfg)
    claude = RecordingConnection(router)
    opencode = RecordingConnection(router)
    router.subscribe("s1", "claude", "c", [], claude, destinations=["my-claude"])
    router.subscribe("s2", "opencode", "o", [], opencode, destinations=["my-opencode"])

    result = await router.deliver(_event(source="alerts"))

    assert result == "queued"
    assert claude.destination_names == ["my-claude"]
    assert opencode.destination_names == ["my-opencode"]


@pytest.mark.asyncio
async def test_partial_fan_out_still_reports_queued(tmp_path):
    """One live and one idle destination is success, not failure.

    Reporting a failure here would make a sender retry a delivery that already
    landed on the live half.
    """
    cfg = {
        "version": 2,
        "senders": {"alerts": {"secret": b"k1"}},
        "principals": {"agent:me": {}},
        "destinations": {
            "my-claude": {"adapter": "claude", "principal": "agent:me"},
            "my-opencode": {"adapter": "opencode", "principal": "agent:me"},
        },
        "routes": [{"sender": "alerts", "principal": "agent:me"}],
        "state": {"default_delivery": "next_session"},
    }
    store = WakeStore(tmp_path / "state.db")
    try:
        router = Router(cfg, store=store)
        claude = RecordingConnection(router)
        router.subscribe("s1", "claude", "c", [], claude, destinations=["my-claude"])

        result = await router.deliver(_event(source="alerts"))

        assert result == "queued"
        assert claude.destination_names == ["my-claude"]
        # The idle half is durably reserved for its own destination.
        queued = store.list_pending()
        assert [(p.event_id, p.destination) for p in queued] == [
            ("ev-1", "my-opencode")
        ]
    finally:
        store.close()


def test_principal_route_expands_to_every_destination_of_that_principal():
    """sender -> principal -> destination(s): the middle term does work."""
    cfg = {
        "senders": {"alerts": {}},
        "principals": {"agent:me": {"channels": {}}, "agent:other": {"channels": {}}},
        "destinations": {
            "a": {"adapter": "claude", "principal": "agent:me"},
            "b": {"adapter": "opencode", "principal": "agent:me"},
            "c": {"adapter": "claude", "principal": "agent:other"},
        },
        "routes": [{"sender": "alerts", "principal": "agent:me", "destinations": []}],
    }
    names = [d.name for d in addressing.routed_destinations(cfg, "alerts")]
    assert sorted(names) == ["a", "b"]


# ── BC-WAKE-010: per-destination connection caps ──────────────────────────────


def test_per_destination_cap_is_expressible(tmp_path, monkeypatch):
    """BC-WAKE-010: the cap is a first-class field on the destination."""
    cfg = _loaded(
        tmp_path,
        monkeypatch,
        {
            "version": 2,
            "senders": {"ops": {"secret_env": "S"}},
            "principals": {"agent:dev": {}},
            "destinations": {
                "dev": {
                    "adapter": "opencode",
                    "principal": "agent:dev",
                    "max_connections": 2,
                }
            },
            "routes": [{"sender": "ops", "destinations": ["dev"]}],
        },
    )
    assert addressing.destination(cfg, "dev").max_connections == 2


def test_zero_and_negative_caps_are_rejected(tmp_path, monkeypatch):
    with pytest.raises(ConfigError, match="max_connections"):
        _loaded(
            tmp_path,
            monkeypatch,
            {
                "version": 2,
                "senders": {"ops": {"secret_env": "S"}},
                "principals": {"agent:dev": {}},
                "destinations": {
                    "dev": {
                        "adapter": "opencode",
                        "principal": "agent:dev",
                        "max_connections": 0,
                    }
                },
                "routes": [{"sender": "ops", "destinations": ["dev"]}],
            },
        )


@pytest.mark.asyncio
async def test_per_destination_cap_is_enforced_at_hello(tmp_path):
    """A third connection to a cap-2 destination is refused, not admitted."""
    cfg = {
        "version": 2,
        "senders": {"ops": {"secret": b"k1"}},
        "principals": {"agent:dev": {}},
        "destinations": {
            "dev": {
                "adapter": "opencode",
                "principal": "agent:dev",
                "max_connections": 2,
            },
            "spare": {"adapter": "opencode", "principal": "agent:dev"},
        },
        "routes": [{"sender": "ops", "destinations": ["dev", "spare"]}],
    }
    router = Router(cfg)
    server = SocketServer(tmp_path / "sock", router)
    await server.start()
    try:
        writers = []

        async def _hello(dest: str) -> dict[str, Any]:
            reader, writer = await asyncio.open_unix_connection(
                str(tmp_path / "sock")
            )
            writers.append(writer)
            writer.write(
                encode_frame(
                    {
                        "type": "hello",
                        "v": 1,
                        "adapter": "opencode",
                        "instance": "oc",
                        "destinations": [dest],
                    }
                )
            )
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=2)
            return json.loads(line)

        first = await _hello("dev")
        second = await _hello("dev")
        assert first["type"] == "hello_ack"
        assert first["accepted_destinations"] == ["dev"]
        assert second["type"] == "hello_ack"

        third = await _hello("dev")
        assert third["type"] == "error"
        assert third["code"] == "connection_limit"
        assert "dev (max 2)" in third["message"]

        # An uncapped sibling destination is unaffected: the cap is
        # per-destination, which is the whole point of BC-WAKE-010.
        spare = await _hello("spare")
        assert spare["type"] == "hello_ack"

        for w in writers:
            w.close()
    finally:
        server.close()


@pytest.mark.asyncio
async def test_adapter_cannot_claim_another_adapters_destination(tmp_path):
    """A socket peer asking nicely is not authorization."""
    cfg = _two_session_config()
    router = Router(cfg)
    server = SocketServer(tmp_path / "sock", router)
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(tmp_path / "sock"))
        writer.write(
            encode_frame(
                {
                    "type": "hello",
                    "v": 1,
                    "adapter": "claude",
                    "instance": "c",
                    "destinations": ["dev-a"],
                }
            )
        )
        await writer.drain()
        ack = json.loads(await asyncio.wait_for(reader.readline(), timeout=2))
        assert ack["type"] == "hello_ack"
        assert ack["accepted_destinations"] == []
        writer.close()
    finally:
        server.close()


# ── BC-WAKE-012: the durable queue is per destination ─────────────────────────


@pytest.mark.asyncio
async def test_queued_event_drains_only_to_its_own_session(tmp_path):
    """BC-WAKE-012: a sibling session must not drain another's inbox.

    Keyed on the sender, the queue handed a session-scoped event to whichever
    session connected first — BC-001's fan-out with a delay in front of it.
    """
    cfg = _two_session_config()
    cfg["state"] = {"default_delivery": "next_session"}
    store = WakeStore(tmp_path / "state.db")
    try:
        router = Router(cfg, store=store)
        # Nothing live: the event is reserved for dev-a specifically.
        result = await router.deliver(_event(destination="dev-a"))
        assert result == "queued_next_session"
        assert [p.destination for p in store.list_pending()] == ["dev-a"]

        # The wrong session connects first and must drain nothing.
        conn_b = RecordingConnection(router)
        router.subscribe("s2", "opencode", "oc", [], conn_b, destinations=["dev-b"])
        assert await router.drain_pending("s2") == 0
        assert conn_b.sent == []
        assert store.pending_count() == 1

        # The addressed session connects and gets it.
        conn_a = RecordingConnection(router)
        router.subscribe("s1", "opencode", "oc", [], conn_a, destinations=["dev-a"])
        assert await router.drain_pending("s1") == 1
        await asyncio.sleep(0)
        assert conn_a.destination_names == ["dev-a"]
        assert conn_a.sessions == ["sess-a"]
        # The recording connection acks on send, but deleting the claimed row
        # runs in a background ack-waiter task. Await it, or the queue-state
        # assertion below races the settlement (a single sleep(0) is not
        # enough on CPython 3.11, where wait_for needs several loop turns).
        await asyncio.gather(*tuple(router._background_tasks))
        assert store.pending_count() == 0
    finally:
        store.close()


def test_store_migrates_v1_pending_rows_by_backfilling_destination(tmp_path):
    """An upgraded box must not fail every enqueue with 'no such column'.

    Backfilling ``destination`` from ``source`` is correct rather than merely
    convenient: the legacy translation makes a v1 source name and its
    destination name the same string.
    """
    db = tmp_path / "state.db"
    # Hand-build the v1 schema, exactly as the previous release wrote it.
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE pending (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id    TEXT NOT NULL UNIQUE,
            source      TEXT NOT NULL,
            target      TEXT,
            event_json  TEXT NOT NULL,
            enqueued_at REAL NOT NULL,
            expires_at  REAL NOT NULL,
            attempts    INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO schema_meta VALUES ('schema_version', '1');
        """
    )
    conn.execute(
        "INSERT INTO pending (event_id, source, target, event_json, enqueued_at, "
        "expires_at, attempts) VALUES ('old-1', 'mvmcc03-claude', NULL, '{}', "
        "0, 9999999999, 0)"
    )
    conn.commit()
    conn.close()

    store = WakeStore(db)
    try:
        rows = store.list_pending()
        assert [(r.event_id, r.destination) for r in rows] == [
            ("old-1", "mvmcc03-claude")
        ]
        # And it is claimable by the destination that inherited the name.
        assert [r.event_id for r in store.claim_pending("mvmcc03-claude")] == ["old-1"]
    finally:
        store.close()


# ── BC-011: heartbeat vs delivery lifecycle ───────────────────────────────────


@pytest.mark.asyncio
async def test_router_tracks_in_flight_deliveries_for_the_heartbeat():
    """BC-011: an adapter running the turn we asked for is not a dead one."""
    cfg = _two_session_config()
    router = Router(cfg)
    # auto_ack off: the adapter is "busy", exactly as during a long model turn.
    conn = RecordingConnection(router, auto_ack=False)
    router.subscribe("s1", "opencode", "oc", [], conn, destinations=["dev-a"])

    await router.deliver(_event(destination="dev-a"))
    assert router.in_flight_for("s1") == 1

    ack_id = conn.sent[0]["ack_id"]
    router.resolve_ack(ack_id, "ack")
    # The in-flight decrement happens in the background ack-waiter task, not
    # synchronously in resolve_ack; await it (one sleep(0) is not enough on
    # CPython 3.11, where wait_for needs several loop turns).
    await asyncio.gather(*tuple(router._background_tasks))
    assert router.in_flight_for("s1") == 0


_PING = 0.05


async def _silent_adapter(
    tmp_path: Path, in_flight: int
) -> tuple[SocketServer, str, asyncio.StreamWriter]:
    """A connected adapter that never pongs, with *in_flight* deliveries owed.

    The writer is handed back and must stay referenced: letting it be collected
    closes the socket, and the connection then disappears for a reason that has
    nothing to do with the heartbeat.
    """
    cfg = _two_session_config()
    router = Router(cfg)
    server = SocketServer(tmp_path / "sock", router)
    await server.start()
    reader, writer = await asyncio.open_unix_connection(str(tmp_path / "sock"))
    writer.write(
        encode_frame(
            {
                "type": "hello",
                "v": 1,
                "adapter": "opencode",
                "instance": "oc",
                "destinations": ["dev-a"],
            }
        )
    )
    await writer.drain()
    json.loads(await asyncio.wait_for(reader.readline(), timeout=2))
    session_id = next(iter(server.connections))
    router._subscribers[session_id].in_flight = in_flight
    return server, session_id, writer


@pytest.mark.asyncio
async def test_heartbeat_reaps_an_idle_silent_adapter(tmp_path, monkeypatch):
    """The baseline v1 behaviour, unchanged: no pong and nothing owed → gone."""
    monkeypatch.setattr("agent_waked.socket_server.PING_INTERVAL", _PING)
    server, session_id, writer = await _silent_adapter(tmp_path, in_flight=0)
    try:
        # Reaped on the second interval: ping at t=P, no pong seen at t=2P.
        await asyncio.sleep(_PING * 3)
        assert session_id not in server.connections
    finally:
        writer.close()
        server.close()


@pytest.mark.asyncio
async def test_heartbeat_tolerates_a_busy_adapter_then_reaps_it(
    tmp_path, monkeypatch
):
    """BC-011, both halves: forgiveness while a delivery is owed, but bounded.

    A wake event whose purpose is to start a model turn can occupy a
    single-threaded adapter for far longer than the ping interval, so reaping it
    there means the liveness probe kills the healthy connection it just woke.
    Unconditional forgiveness would instead leak a peer that died mid-turn, so
    the tolerance is ``MAX_BUSY_MISSED_PONGS`` intervals and then the connection
    goes.
    """
    monkeypatch.setattr("agent_waked.socket_server.PING_INTERVAL", _PING)
    server, session_id, writer = await _silent_adapter(tmp_path, in_flight=1)
    try:
        # Still here well past the point an idle connection would have died.
        await asyncio.sleep(_PING * 3)
        assert session_id in server.connections

        # Gone once the tolerance is spent (reaped at (MAX+2) intervals).
        for _ in range(200):
            await asyncio.sleep(_PING)
            if session_id not in server.connections:
                break
        assert session_id not in server.connections
        assert MAX_BUSY_MISSED_PONGS >= 1
    finally:
        writer.close()
        server.close()


# ── backward compatibility and migration ──────────────────────────────────────


def test_live_mvmcc03_shaped_config_still_loads(tmp_path, monkeypatch):
    """The real deployed file, translated, must address the same places.

    A copy of ``~/.config/agent-wake/config.json`` on mvmcc03 as of this change:
    two sources, each with ``secret_env``, ``principal_id`` and
    ``allowed_trigger_identities``, and a name-keyed ``routing`` block.
    """
    cfg = _loaded(
        tmp_path,
        monkeypatch,
        {
            "version": 1,
            "listen": {"host": "127.0.0.1", "port": 8788},
            "socket_path": None,
            "sources": {
                "mvmcc03-claude": {
                    "secret_env": "AGENT_WAKE_MVMCC03_CLAUDE_SECRET",
                    "callback_url": None,
                    "principal_id": "agent:mvmcc03-claude",
                    "allowed_trigger_identities": ["human:itadmin", "mvmcc03-agent"],
                },
                "mvmcc03-opencode": {
                    "secret_env": "AGENT_WAKE_MVMCC03_OPENCODE_SECRET",
                    "callback_url": None,
                    "principal_id": "agent:mvmcc03-opencode",
                    "allowed_trigger_identities": ["human:itadmin", "mvmcc03-agent"],
                },
            },
            "default_callback_url": None,
            "routing": {
                "mvmcc03-claude": {"adapter": "claude"},
                "mvmcc03-opencode": {"adapter": "opencode"},
            },
        },
    )

    # Credentials unchanged, and still reachable under the legacy key.
    assert sorted(cfg["senders"]) == ["mvmcc03-claude", "mvmcc03-opencode"]
    assert cfg["sources"] is cfg["senders"]
    assert cfg["senders"]["mvmcc03-claude"]["secret_uris"] == [
        "env://AGENT_WAKE_MVMCC03_CLAUDE_SECRET"
    ]
    assert cfg["senders"]["mvmcc03-claude"]["allowed_trigger_identities"] == [
        "human:itadmin",
        "mvmcc03-agent",
    ]

    # A destination per source, named identically, so a connected adapter's
    # hello.filters.sources keeps resolving.
    claude = addressing.destination(cfg, "mvmcc03-claude")
    assert claude.adapter == "claude"
    assert claude.session is None
    assert claude.principal == "agent:mvmcc03-claude"

    # And the route reaches exactly the one destination v1 reached.
    routed = addressing.routed_destinations(cfg, "mvmcc03-claude")
    assert [d.name for d in routed] == ["mvmcc03-claude"]


def test_routes_never_grant_out_of_band_delivery(tmp_path, monkeypatch):
    """In-band routing is not egress authority, in either vocabulary.

    Waking an adapter over a 0600 unix socket is local; an out-of-band channel
    sends a real email or POSTs to a resolved URL. Deriving the second from the
    first would turn every route into egress permission — and on migration would
    hand a v1 ``routing`` entry authority its old config never gave it.
    """
    legacy = _loaded(
        tmp_path,
        monkeypatch,
        {
            "version": 1,
            "sources": {"ops": {"secret_env": "S", "principal_id": "agent:dev"}},
            "routing": {"ops": {"adapter": "claude"}},
        },
    )
    assert addressing.out_of_band_principals(legacy, "ops") is None

    v2 = _loaded(
        tmp_path / "v2",
        monkeypatch,
        {
            "version": 2,
            "senders": {"ops": {"secret_env": "S"}},
            "principals": {"agent:dev": {}},
            "destinations": {"dev": {"adapter": "claude", "principal": "agent:dev"}},
            "routes": [{"sender": "ops", "destinations": ["dev"]}],
        },
    )
    assert addressing.out_of_band_principals(v2, "ops") is None


def test_out_of_band_delivery_needs_its_own_declaration(tmp_path, monkeypatch):
    """``allowed_target_principals`` remains the only grant, in both spellings."""
    cfg = _loaded(
        tmp_path,
        monkeypatch,
        {
            "version": 2,
            "senders": {
                "ops": {
                    "secret_env": "S",
                    "allowed_target_principals": ["human:me"],
                }
            },
            "principals": {"human:me": {}, "agent:dev": {}},
            "destinations": {"dev": {"adapter": "claude", "principal": "agent:dev"}},
            "routes": [{"sender": "ops", "destinations": ["dev"]}],
        },
    )
    assert addressing.out_of_band_principals(cfg, "ops") == {"human:me"}


def test_legacy_hello_sources_are_still_accepted(tmp_path, monkeypatch):
    """The deployed Claude adapter sends filters.sources and nothing else."""
    cfg = _loaded(
        tmp_path,
        monkeypatch,
        {
            "version": 1,
            "sources": {"mvmcc03-claude": {"secret_env": "S"}},
            "routing": {"mvmcc03-claude": {"adapter": "claude"}},
        },
    )
    router = Router(cfg)
    assert router.accepted_sources_for("claude", ["mvmcc03-claude"]) == [
        "mvmcc03-claude"
    ]
    assert router.destinations_for_hello("claude", ["mvmcc03-claude"], None) == [
        "mvmcc03-claude"
    ]
    assert router.destinations_for_hello("opencode", ["mvmcc03-claude"], None) == []


# ── config validation ─────────────────────────────────────────────────────────


def test_mixing_vocabularies_is_an_actionable_error(tmp_path, monkeypatch):
    with pytest.raises(ConfigError) as exc:
        _loaded(
            tmp_path,
            monkeypatch,
            {
                "version": 2,
                "sources": {"a": {"secret_env": "S"}},
                "senders": {"b": {"secret_env": "S"}},
            },
        )
    msg = str(exc.value)
    assert "'sources' and 'senders'" in msg
    assert "agent-wake config migrate" in msg


def test_route_to_unknown_destination_is_rejected(tmp_path, monkeypatch):
    with pytest.raises(ConfigError, match="not declared in 'destinations'"):
        _loaded(
            tmp_path,
            monkeypatch,
            {
                "version": 2,
                "senders": {"ops": {"secret_env": "S"}},
                "principals": {},
                "destinations": {},
                "routes": [{"sender": "ops", "destinations": ["nope"]}],
            },
        )


def test_destination_with_undeclared_principal_is_rejected(tmp_path, monkeypatch):
    with pytest.raises(ConfigError, match="not declared in 'principals'"):
        _loaded(
            tmp_path,
            monkeypatch,
            {
                "version": 2,
                "senders": {"ops": {"secret_env": "S"}},
                "principals": {},
                "destinations": {"d": {"adapter": "claude", "principal": "agent:x"}},
                "routes": [{"sender": "ops", "destinations": ["d"]}],
            },
        )


def test_session_without_adapter_is_rejected(tmp_path, monkeypatch):
    with pytest.raises(ConfigError, match="'session' requires 'adapter'"):
        _loaded(
            tmp_path,
            monkeypatch,
            {
                "version": 2,
                "senders": {"ops": {"secret_env": "S"}},
                "principals": {},
                "destinations": {"d": {"session": "s1"}},
                "routes": [{"sender": "ops", "destinations": ["d"]}],
            },
        )


def test_route_principal_must_own_its_destinations(tmp_path, monkeypatch):
    with pytest.raises(ConfigError, match="does not own destination"):
        _loaded(
            tmp_path,
            monkeypatch,
            {
                "version": 2,
                "senders": {"ops": {"secret_env": "S"}},
                "principals": {"agent:a": {}, "agent:b": {}},
                "destinations": {"d": {"adapter": "claude", "principal": "agent:b"}},
                "routes": [
                    {
                        "sender": "ops",
                        "principal": "agent:a",
                        "destinations": ["d"],
                    }
                ],
            },
        )


def test_v2_principal_channels_replace_the_delivery_block(tmp_path, monkeypatch):
    """One validator, two spellings — same normalised channel config."""
    channel = {
        "webhook": {
            "url": "https://example.com/hook",
            "secret_uri": "env://WEBHOOK_SECRET",
        }
    }
    v2 = _loaded(
        tmp_path,
        monkeypatch,
        {
            "version": 2,
            "senders": {"ops": {"secret_env": "S"}},
            "principals": {"human:me": {"channels": channel}},
            "destinations": {},
            "routes": [{"sender": "ops", "principal": "human:me"}],
        },
    )
    v1 = _loaded(
        tmp_path / "v1",
        monkeypatch,
        {
            "version": 1,
            "sources": {"ops": {"secret_env": "S"}},
            "routing": {},
            "delivery": {"human:me": channel},
        },
    )
    assert v2["delivery"] == v1["delivery"]
    assert addressing.principal_channels(v2, "human:me") == v1["delivery"]["human:me"]


# ── operator surfaces: config migrate / show, doctor ──────────────────────────


def test_config_migrate_round_trips_the_live_config_shape(tmp_path, monkeypatch, capsys):
    """The migrated file must address exactly what the original addressed.

    Proven against the shape of the real mvmcc03 file: same credentials, same
    in-band destinations, and — crucially — the same out-of-band authority.
    """
    from agent_waked.cli import main as cli_main

    legacy_raw = {
        "version": 1,
        "listen": {"host": "127.0.0.1", "port": 8788},
        "socket_path": None,
        "sources": {
            "mvmcc03-claude": {
                "secret_env": "AGENT_WAKE_MVMCC03_CLAUDE_SECRET",
                "callback_url": None,
                "principal_id": "agent:mvmcc03-claude",
                "allowed_trigger_identities": ["human:itadmin", "mvmcc03-agent"],
            },
            "mvmcc03-opencode": {
                "secret_env": "AGENT_WAKE_MVMCC03_OPENCODE_SECRET",
                "callback_url": None,
                "principal_id": "agent:mvmcc03-opencode",
                "allowed_trigger_identities": ["human:itadmin", "mvmcc03-agent"],
            },
        },
        "default_callback_url": None,
        "routing": {
            "mvmcc03-claude": {"adapter": "claude"},
            "mvmcc03-opencode": {"adapter": "opencode"},
        },
    }
    legacy_path = tmp_path / "config.json"
    _write(legacy_path, legacy_raw)

    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(legacy_path))
    legacy_cfg = load_config()

    assert cli_main(["config", "migrate", str(legacy_path)]) == 0
    out = capsys.readouterr()
    migrated_raw = json.loads(out.out)
    assert migrated_raw["version"] == 2

    # The conflation is *reported*, not silently rewritten: only the operator
    # knows which identity they meant.
    assert "attributed to their own addressee" in out.err

    migrated_path = tmp_path / "v2" / "config.json"
    _write(migrated_path, migrated_raw)
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(migrated_path))
    v2_cfg = load_config()

    for name in legacy_cfg["senders"]:
        assert (
            v2_cfg["senders"][name]["secret_uris"]
            == legacy_cfg["senders"][name]["secret_uris"]
        )
        assert (
            v2_cfg["senders"][name]["allowed_trigger_identities"]
            == legacy_cfg["senders"][name]["allowed_trigger_identities"]
        )
        before = [
            (d.name, d.adapter, d.session, d.principal)
            for d in addressing.routed_destinations(legacy_cfg, name)
        ]
        after = [
            (d.name, d.adapter, d.session, d.principal)
            for d in addressing.routed_destinations(v2_cfg, name)
        ]
        assert before == after
        assert addressing.out_of_band_principals(
            v2_cfg, name
        ) == addressing.out_of_band_principals(legacy_cfg, name)


def test_config_migrate_write_refuses_to_clobber(tmp_path, monkeypatch):
    from agent_waked.cli import main as cli_main

    src = tmp_path / "config.json"
    _write(src, {"version": 1, "sources": {"a": {"secret_env": "S"}}, "routing": {}})
    dest = tmp_path / "out.json"
    dest.write_text("{}")
    monkeypatch.delenv("AGENT_WAKE_CONFIG", raising=False)
    # The error boundary maps ConfigError to the contract envelope, exit 1.
    assert cli_main(["config", "migrate", str(src), "--write", str(dest)]) == 1
    assert dest.read_text() == "{}"


def test_config_migrate_refuses_an_already_v2_file(tmp_path, monkeypatch):
    from agent_waked.cli import main as cli_main

    src = tmp_path / "config.json"
    _write(src, {"version": 2, "senders": {"a": {"secret_env": "S"}}})
    monkeypatch.delenv("AGENT_WAKE_CONFIG", raising=False)
    assert cli_main(["config", "migrate", str(src)]) == 1


def test_config_show_json_answers_who_can_wake_what(tmp_path, monkeypatch, capsys):
    from agent_waked.cli import main as cli_main

    _write(
        tmp_path / "config.json",
        {
            "version": 2,
            "senders": {"ops": {"secret_env": "S"}},
            "principals": {"agent:dev": {}},
            "destinations": {
                "dev-a": {
                    "adapter": "opencode",
                    "session": "sess-a",
                    "principal": "agent:dev",
                    "max_connections": 1,
                }
            },
            "routes": [{"sender": "ops", "destinations": ["dev-a"]}],
        },
    )
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(tmp_path / "config.json"))
    assert cli_main(["config", "show", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["senders"]["ops"]["reaches"] == ["dev-a"]
    assert doc["senders"]["ops"]["out_of_band_principals"] == []
    assert doc["destinations"]["dev-a"]["session"] == "sess-a"
    assert doc["destinations"]["dev-a"]["max_connections"] == 1


def test_doctor_addressing_check_flags_a_config_that_addresses_nobody(
    tmp_path, monkeypatch
):
    """Load-time validation cannot catch a coherent config that wakes nothing."""
    from agent_waked.doctor import _check_addressing

    _write(
        tmp_path / "config.json",
        {
            "version": 2,
            "senders": {"ops": {"secret_env": "S"}, "orphan": {"secret_env": "S"}},
            "principals": {"agent:dev": {}},
            "destinations": {
                "dev-a": {"adapter": "opencode", "principal": "agent:dev"},
                "dev-unused": {"adapter": "opencode", "principal": "agent:dev"},
            },
            "routes": [{"sender": "ops", "destinations": ["dev-a"]}],
        },
    )
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(tmp_path / "config.json"))
    status, detail = _check_addressing()
    assert status == "warn"
    assert "orphan" in detail
    assert "dev-unused" in detail


def test_doctor_addressing_check_fails_on_an_ambiguous_session(tmp_path, monkeypatch):
    """Two destinations for one adapter session: a delivery could land wrong."""
    from agent_waked.doctor import _check_addressing

    _write(
        tmp_path / "config.json",
        {
            "version": 2,
            "senders": {"ops": {"secret_env": "S"}},
            "principals": {"agent:dev": {}},
            "destinations": {
                "dup-1": {
                    "adapter": "opencode",
                    "session": "sess-a",
                    "principal": "agent:dev",
                },
                "dup-2": {
                    "adapter": "opencode",
                    "session": "sess-a",
                    "principal": "agent:dev",
                },
            },
            "routes": [{"sender": "ops", "destinations": ["dup-1", "dup-2"]}],
        },
    )
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(tmp_path / "config.json"))
    status, detail = _check_addressing()
    assert status == "fail"
    assert "opencode/sess-a" in detail


def test_doctor_addressing_is_ok_on_the_legacy_live_shape(tmp_path, monkeypatch):
    """The deployed config must not start reporting a new warning."""
    from agent_waked.doctor import _check_addressing

    _write(
        tmp_path / "config.json",
        {
            "version": 1,
            "sources": {
                "mvmcc03-claude": {"secret_env": "S1"},
                "mvmcc03-opencode": {"secret_env": "S2"},
            },
            "routing": {
                "mvmcc03-claude": {"adapter": "claude"},
                "mvmcc03-opencode": {"adapter": "opencode"},
            },
        },
    )
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(tmp_path / "config.json"))
    status, detail = _check_addressing()
    assert status == "ok", detail
    assert "2 sender(s) → 2 destination(s)" in detail


# ── outbound peer identity (prerequisite for BC-WAKE-008/017/018) ─────────────


def test_reply_carries_the_replying_peers_identity():
    """The outbound path can finally name its peer.

    BC-WAKE-008/017/018 are blocked on this and not on cryptography: there was
    nothing to sign *as*, because a reply's only identifier was the source name
    it was addressed from.
    """
    from agent_waked.outbox import Outbox

    cfg = _two_session_config()
    outbox = Outbox(cfg)
    assert outbox.peer_identity("ops", "dev-a") == {
        "destination": "dev-a",
        "principal": "agent:dev",
    }
    # Nothing is fabricated when the reply names no destination: an invented
    # identity on an attribution field is worse than an absent one.
    assert outbox.peer_identity("ops", None) == {}
    assert outbox.peer_identity("ops", "nonexistent") == {"destination": "nonexistent"}


@pytest.mark.asyncio
async def test_reply_cannot_claim_a_destination_the_connection_does_not_serve(
    tmp_path,
):
    """An adapter must not attribute its reply to somebody else's principal."""

    class _Outbox:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def deliver(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            return {
                "reply_id": kwargs["reply_id"],
                "status": "no_callback",
                "http_status": None,
                "error": None,
            }

    cfg = _two_session_config()
    router = Router(cfg)
    outbox = _Outbox()
    server = SocketServer(tmp_path / "sock", router, outbox=outbox)  # type: ignore[arg-type]
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(tmp_path / "sock"))
        writer.write(
            encode_frame(
                {
                    "type": "hello",
                    "v": 1,
                    "adapter": "opencode",
                    "instance": "oc",
                    "destinations": ["dev-a"],
                }
            )
        )
        await writer.drain()
        json.loads(await asyncio.wait_for(reader.readline(), timeout=2))

        # Claiming the sibling's destination is refused, non-fatally.
        writer.write(
            encode_frame(
                {
                    "type": "reply",
                    "reply_id": "r1",
                    "source": "ops",
                    "in_reply_to": "ev-1",
                    "content": "done",
                    "destination": "dev-b",
                }
            )
        )
        await writer.drain()
        err = json.loads(await asyncio.wait_for(reader.readline(), timeout=2))
        assert err["type"] == "error"
        assert err["code"] == "unauthorized_destination"
        assert err["fatal"] is False
        assert outbox.calls == []

        # Its own destination is fine, and reaches the outbox.
        writer.write(
            encode_frame(
                {
                    "type": "reply",
                    "reply_id": "r2",
                    "source": "ops",
                    "in_reply_to": "ev-1",
                    "content": "done",
                    "destination": "dev-a",
                }
            )
        )
        await writer.drain()
        result = json.loads(await asyncio.wait_for(reader.readline(), timeout=2))
        assert result["type"] == "reply_result"
        assert outbox.calls[0]["destination"] == "dev-a"
        writer.close()
    finally:
        server.close()
