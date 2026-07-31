"""Tests for `agent-wake dead-letter` / `agent-wake pending` (BC-WAKE-012).

Operator visibility AC: dead-lettered deliveries are listable, inspectable and
redrivable; the next-session queue is listable and prunable; every failure path
emits the suite CLI contract v1 error envelope on stdout under ``--json``.
"""

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent_waked.cli import build_parser
from agent_waked.cli import main as cli_main
from agent_waked.store import DEAD_LETTER_KINDS, WakeStore


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setenv("AGENT_WAKE_STATE_DIR", str(d))
    # No config on this box: the read-only commands must still work.
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(tmp_path / "absent.json"))
    return d


@pytest.fixture
def store(state_dir):
    s = WakeStore(state_dir / "state.db")
    yield s
    s.close()


def _run(capsys, *argv):
    code = cli_main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def _json_out(capsys, *argv):
    code, out, _ = _run(capsys, *argv)
    return code, json.loads(out)


# ── dead-letter list / show ───────────────────────────────────────────────────


def test_dead_letter_list_empty(store, capsys):
    code, doc = _json_out(capsys, "dead-letter", "list", "--json")
    assert code == 0
    assert doc == {"ok": True, "dead_letters": []}


def test_dead_letter_list_text_when_empty(store, capsys):
    code, out, _ = _run(capsys, "dead-letter", "list")
    assert code == 0
    assert "No dead-lettered deliveries." in out


def test_dead_letter_list_and_show(store, capsys):
    dl_id = store.dead_letter(
        kind="reply",
        source="github-actions",
        ref_id="rpl-1",
        payload={"source": "github-actions", "reply_id": "rpl-1",
                 "in_reply_to": "e1", "content": "hi"},
        error="HTTP 500",
        attempts=3,
    )

    code, doc = _json_out(capsys, "dead-letter", "list", "--json")
    assert code == 0
    row = doc["dead_letters"][0]
    assert row["id"] == dl_id
    assert row["kind"] == "reply"
    assert row["error"] == "HTTP 500"
    # The listing is a summary: the payload only appears in `show`.
    assert "payload" not in row

    code, doc = _json_out(capsys, "dead-letter", "show", dl_id, "--json")
    assert code == 0
    assert doc["dead_letter"]["payload"]["content"] == "hi"


def test_dead_letter_kind_filter(store, capsys):
    store.dead_letter(kind="reply", source="s", ref_id="r", payload={})
    store.dead_letter(kind="next_session", source="s", ref_id="e", payload={})
    code, doc = _json_out(capsys, "dead-letter", "list", "--kind", "reply", "--json")
    assert code == 0
    assert [d["kind"] for d in doc["dead_letters"]] == ["reply"]


def test_dead_letter_show_missing_id_emits_error_envelope(store, capsys):
    code, doc = _json_out(capsys, "dead-letter", "show", "nope", "--json")
    assert code == 1
    assert doc["ok"] is False
    assert doc["error"]["code"] == "NOT_FOUND"
    assert doc["error"]["retryable"] is False


# ── redrive ───────────────────────────────────────────────────────────────────


def test_redrive_next_session_requeues(store, capsys):
    event = {
        "v": 0,
        "event_id": "ev-1",
        "source": "github-actions",
        "kind": "webhook",
        "content": "x",
        "meta": {"delivery": "next_session"},
        "wake": True,
    }
    dl_id = store.dead_letter(
        kind="next_session", source="github-actions", ref_id="ev-1", payload=event
    )

    code, doc = _json_out(capsys, "dead-letter", "redrive", dl_id, "--json")
    assert code == 0
    assert doc["action"] == "requeued"
    assert [p.event_id for p in store.list_pending()] == ["ev-1"]
    # A successfully redriven entry drops out of the default listing.
    assert store.list_dead_letters() == []


def test_redrive_next_session_twice_is_an_error(store, capsys):
    event = {"event_id": "ev-2", "source": "github-actions", "meta": {}}
    dl_id = store.dead_letter(
        kind="next_session", source="github-actions", ref_id="ev-2", payload=event
    )
    assert _json_out(capsys, "dead-letter", "redrive", dl_id, "--json")[0] == 0
    code, doc = _json_out(capsys, "dead-letter", "redrive", dl_id, "--json")
    assert code == 1
    assert doc["error"]["code"] == "REDRIVE_FAILED"
    assert doc["error"]["retryable"] is True


def test_redrive_reply_without_config_is_an_error(store, capsys):
    dl_id = store.dead_letter(
        kind="reply", source="s", ref_id="r", payload={"source": "s"}
    )
    code, doc = _json_out(capsys, "dead-letter", "redrive", dl_id, "--json")
    assert code == 1
    assert doc["error"]["code"] == "CONFIG_ERROR"


@pytest.mark.asyncio
async def test_redrive_reply_delivers_for_real(tmp_path, monkeypatch, capsys):
    """The redrive is a real HTTP POST, not a status flip."""
    received: list[dict] = []

    async def handler(request):
        received.append(await request.json())
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/callback", handler)
    http = TestClient(TestServer(app))
    await http.start_server()
    try:
        url = str(http.make_url("/callback"))
        monkeypatch.setenv("AW_SEC", "shhh")
        monkeypatch.setenv("AGENT_WAKE_STATE_DIR", str(tmp_path / "state"))
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({
            "version": 1,
            "sources": {"github-actions": {
                "secret_env": "AW_SEC", "callback_url": url,
            }},
            "routing": {},
            "state": {"dir": str(tmp_path / "state")},
        }))
        monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))

        store = WakeStore(tmp_path / "state" / "state.db")
        dl_id = store.dead_letter(
            kind="reply",
            source="github-actions",
            ref_id="rpl-9",
            payload={
                "source": "github-actions",
                "reply_id": "rpl-9",
                "in_reply_to": "evt-9",
                "content": "second time lucky",
            },
            error="HTTP 500",
        )
        store.close()

        # The CLI redrive runs its own event loop, so it must be driven from a
        # worker thread while this test's loop hosts the callback server.
        code = await asyncio.to_thread(
            cli_main, ["dead-letter", "redrive", dl_id, "--json"]
        )
        doc = json.loads(capsys.readouterr().out)
        assert code == 0, doc
        assert doc["action"] == "delivered"
        assert received == [{
            "v": 0,
            "in_reply_to": "evt-9",
            "content": "second time lucky",
            "meta": {},
        }]

        store = WakeStore(tmp_path / "state" / "state.db")
        try:
            entry = store.get_dead_letter(dl_id)
            assert entry is not None
            assert entry.redrive_status == "ok"
            assert entry.redriven_at is not None
        finally:
            store.close()
    finally:
        await http.close()


# ── purge / pending ───────────────────────────────────────────────────────────


def test_purge_is_bounded_by_age(store, capsys):
    store.dead_letter(kind="reply", source="s", ref_id="r", payload={})
    code, doc = _json_out(
        capsys, "dead-letter", "purge", "--older-than-days", "1", "--json"
    )
    assert code == 0 and doc["deleted"] == 0
    code, doc = _json_out(capsys, "dead-letter", "purge", "--json")
    assert code == 0 and doc["deleted"] == 1


def test_pending_list(store, capsys):
    store.enqueue_pending(
        {"event_id": "ev-1", "source": "github-actions", "meta": {"target": "ops"}}
    )
    code, doc = _json_out(capsys, "pending", "list", "--json")
    assert code == 0
    assert doc["pending"][0]["event_id"] == "ev-1"
    assert doc["pending"][0]["target"] == "ops"
    assert doc["pending"][0]["attempts"] == 0


def test_pending_list_text(store, capsys):
    code, out, _ = _run(capsys, "pending", "list")
    assert code == 0
    assert "No events queued" in out


def test_pending_prune_expires_into_dead_letter(state_dir, capsys, monkeypatch):
    """Retention is operator-runnable, and an expired event is not lost.

    Note the sweep actually fires when the CLI *opens* the store (retention is
    applied on every open), so ``prune`` reports zeros here; what matters is
    the end state: gone from ``pending``, present in ``dead-letter``.
    """
    store = WakeStore(state_dir / "state.db", pending_ttl_seconds=1)
    store.enqueue_pending({"event_id": "ev-1", "source": "github-actions", "meta": {}})
    # Age the row past its TTL without sleeping.
    with store._lock:
        store._conn.execute("UPDATE pending SET expires_at = 0")
        store._conn.commit()
    store.close()

    code, doc = _json_out(capsys, "pending", "prune", "--json")
    assert code == 0
    assert set(doc["pruned"]) >= {
        "dedupe_ttl_deleted",
        "dedupe_cap_deleted",
        "pending_expired",
    }

    code, doc = _json_out(capsys, "pending", "list", "--json")
    assert doc["pending"] == []
    code, doc = _json_out(
        capsys, "dead-letter", "list", "--kind", "next_session", "--json"
    )
    assert doc["dead_letters"][0]["ref_id"] == "ev-1"


def test_pending_source_filter(store, capsys):
    store.enqueue_pending({"event_id": "a", "source": "alpha", "meta": {}})
    store.enqueue_pending({"event_id": "b", "source": "beta", "meta": {}})
    _code, doc = _json_out(capsys, "pending", "list", "--source", "beta", "--json")
    assert [d["event_id"] for d in doc["pending"]] == ["b"]


def test_unknown_subcommand_is_a_usage_error(store):
    with pytest.raises(SystemExit) as exc:
        cli_main(["dead-letter", "teleport"])
    assert exc.value.code == 2


# ── human_delivery: listable and redrivable (WI-001 M2) ───────────────────────


def _human_dl_payload(event_id: str = "ev-h1", target: str = "operator") -> dict:
    return {
        "event": {
            "v": 0,
            "event_id": event_id,
            "source": "dossier",
            "kind": "awaiting_accept",
            "content": "WI-42 awaits your accept",
            "meta": {"target": target, "deep_link": "https://suite/WI-42"},
            "wake": True,
        },
        "principal_id": target,
    }


def test_dead_letter_kind_filter_accepts_human_delivery(store, capsys):
    """--kind human_delivery used to be an argparse usage error (exit 2).

    A kind the daemon writes but the CLI refuses to filter on is a durable
    record an operator cannot find.
    """
    store.dead_letter(kind="reply", source="s", ref_id="r", payload={})
    store.dead_letter(
        kind="human_delivery",
        source="dossier",
        ref_id="ev-h1",
        payload=_human_dl_payload(),
        error="human delivery failed: failed channels=['webhook']",
    )
    code, doc = _json_out(
        capsys, "dead-letter", "list", "--kind", "human_delivery", "--json"
    )
    assert code == 0
    assert [d["kind"] for d in doc["dead_letters"]] == ["human_delivery"]
    assert doc["dead_letters"][0]["ref_id"] == "ev-h1"


def test_dead_letter_list_kind_choices_cover_every_store_kind():
    """The argparse choices are derived from the store, so they cannot drift."""
    parser = build_parser()
    dl = parser._subparsers._group_actions[0].choices["dead-letter"]  # type: ignore[union-attr]
    list_p = dl._subparsers._group_actions[0].choices["list"]  # type: ignore[union-attr]
    kind_action = next(a for a in list_p._actions if a.dest == "kind")
    assert tuple(kind_action.choices or ()) == DEAD_LETTER_KINDS


def test_every_dead_letter_kind_has_a_redrive_branch(store, capsys):
    """No kind may fall through to 'unknown dead-letter kind'.

    That fall-through is what made a routed human alert permanently
    un-redrivable: durably recorded, and refused by the only tool that could
    act on it.
    """
    payloads = {
        "reply": {"source": "s", "reply_id": "r"},
        "next_session": {"event_id": "ev-x", "source": "s", "meta": {}},
        "human_delivery": _human_dl_payload(event_id="ev-h9"),
    }
    assert set(payloads) == set(DEAD_LETTER_KINDS)
    for kind, payload in payloads.items():
        dl_id = store.dead_letter(
            kind=kind, source="s", ref_id=f"ref-{kind}", payload=payload
        )
        code, doc = _json_out(capsys, "dead-letter", "redrive", dl_id, "--json")
        if code != 0:
            assert doc["error"]["code"] != "INTERNAL_ERROR", (kind, doc)


def test_redrive_human_delivery_without_config_is_an_error(store, capsys):
    dl_id = store.dead_letter(
        kind="human_delivery",
        source="dossier",
        ref_id="ev-h1",
        payload=_human_dl_payload(),
    )
    code, doc = _json_out(capsys, "dead-letter", "redrive", dl_id, "--json")
    assert code == 1
    assert doc["error"]["code"] == "CONFIG_ERROR"


def test_redrive_human_delivery_with_unusable_payload_is_an_error(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("AGENT_WAKE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AW_SRC", "src-secret")
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "version": 1,
        "sources": {"dossier": {"secret_env": "AW_SRC"}},
        "routing": {},
        "state": {"dir": str(tmp_path / "state")},
    }))
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))

    store = WakeStore(tmp_path / "state" / "state.db")
    dl_id = store.dead_letter(
        kind="human_delivery",
        source="dossier",
        ref_id="ev-broken",
        payload={"principal_id": "operator"},  # no "event"
    )
    store.close()

    code, doc = _json_out(capsys, "dead-letter", "redrive", dl_id, "--json")
    assert code == 1
    assert doc["error"]["code"] == "INTERNAL_ERROR"
    assert "event" in doc["error"]["message"]


@pytest.mark.asyncio
async def test_redrive_human_delivery_delivers_for_real(tmp_path, monkeypatch, capsys):
    """A dead-lettered human alert is really re-dispatched to the principal."""
    received: list[dict] = []

    async def handler(request):
        received.append(await request.json())
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/hook", handler)
    http = TestClient(TestServer(app))
    await http.start_server()
    try:
        url = str(http.make_url("/hook"))
        monkeypatch.setenv("AW_HOOK", "hook-secret")
        monkeypatch.setenv("AW_SRC", "src-secret")
        monkeypatch.setenv("AGENT_WAKE_STATE_DIR", str(tmp_path / "state"))
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({
            "version": 1,
            "sources": {"dossier": {"secret_env": "AW_SRC"}},
            "routing": {},
            "state": {"dir": str(tmp_path / "state")},
            "delivery": {
                "operator": {
                    "webhook": {"url": url, "secret_uri": "env://AW_HOOK"},
                }
            },
        }))
        monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))

        store = WakeStore(tmp_path / "state" / "state.db")
        dl_id = store.dead_letter(
            kind="human_delivery",
            source="dossier",
            ref_id="ev-h1",
            payload=_human_dl_payload(),
            error="human delivery failed: failed channels=['webhook']",
        )
        store.close()

        code = await asyncio.to_thread(
            cli_main, ["dead-letter", "redrive", dl_id, "--json"]
        )
        doc = json.loads(capsys.readouterr().out)
        assert code == 0, doc
        assert doc["action"] == "delivered"
        assert len(received) == 1
        assert received[0]["event_id"] == "ev-h1"
        assert received[0]["meta"]["deep_link"] == "https://suite/WI-42"

        store = WakeStore(tmp_path / "state" / "state.db")
        try:
            entry = store.get_dead_letter(dl_id)
            assert entry is not None
            assert entry.redrive_status == "ok"
            assert entry.redriven_at is not None
            # Redriven entries drop out of the default listing.
            assert store.list_dead_letters(kind="human_delivery") == []
        finally:
            store.close()
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_redrive_human_delivery_failure_is_retryable(tmp_path, monkeypatch, capsys):
    """A redrive that fails again reports retryable and does not double-record."""
    async def handler(request):
        return web.Response(status=503)

    app = web.Application()
    app.router.add_post("/hook", handler)
    http = TestClient(TestServer(app))
    await http.start_server()
    try:
        url = str(http.make_url("/hook"))
        monkeypatch.setenv("AW_HOOK", "hook-secret")
        monkeypatch.setenv("AW_SRC", "src-secret")
        monkeypatch.setenv("AGENT_WAKE_STATE_DIR", str(tmp_path / "state"))
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({
            "version": 1,
            "sources": {"dossier": {"secret_env": "AW_SRC"}},
            "routing": {},
            "state": {"dir": str(tmp_path / "state")},
            "delivery": {
                "operator": {
                    "webhook": {"url": url, "secret_uri": "env://AW_HOOK"},
                }
            },
        }))
        monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))

        store = WakeStore(tmp_path / "state" / "state.db")
        dl_id = store.dead_letter(
            kind="human_delivery",
            source="dossier",
            ref_id="ev-h2",
            payload=_human_dl_payload(event_id="ev-h2"),
        )
        store.close()

        # Keep the retry ladder short: the channel's own defaults are 1/4/16s.
        monkeypatch.setattr(
            "agent_waked.channels.webhook._BACKOFF_DELAYS", (0.01, 0.01, 0.01)
        )
        code = await asyncio.to_thread(
            cli_main, ["dead-letter", "redrive", dl_id, "--json"]
        )
        doc = json.loads(capsys.readouterr().out)
        assert code == 1
        assert doc["error"]["code"] == "REDRIVE_FAILED"
        assert doc["error"]["retryable"] is True

        store = WakeStore(tmp_path / "state" / "state.db")
        try:
            # Marked failed, still listed, and NOT duplicated by the redrive.
            assert store.dead_letter_count() == 1
            entry = store.get_dead_letter(dl_id)
            assert entry is not None
            assert entry.redrive_status == "failed"
        finally:
            store.close()
    finally:
        await http.close()


def test_dead_letter_list_text_columns_stay_aligned(store, capsys):
    """"human_delivery" is 14 chars; a 13-wide KIND column shifted every row."""
    store.dead_letter(
        kind="human_delivery",
        source="dossier",
        ref_id="ev-h1",
        payload=_human_dl_payload(),
    )
    code, out, _ = _run(capsys, "dead-letter", "list")
    assert code == 0
    header, row = out.splitlines()[0], out.splitlines()[1]
    assert header.index("SOURCE") == row.index("dossier")
    assert header.index("REF") == row.index("ev-h1")
