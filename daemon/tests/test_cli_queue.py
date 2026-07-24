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

from agent_waked.cli import main as cli_main
from agent_waked.store import WakeStore


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
    code, doc = _json_out(capsys, "pending", "list", "--source", "beta", "--json")
    assert [d["event_id"] for d in doc["pending"]] == ["b"]


def test_unknown_subcommand_is_a_usage_error(store):
    with pytest.raises(SystemExit) as exc:
        cli_main(["dead-letter", "teleport"])
    assert exc.value.code == 2
