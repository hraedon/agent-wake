"""Tests for agent_waked.store — durable dedupe, retention, dead-letter.

Acceptance criteria (BC-WAKE-004 / BC-WAKE-012):
1. A duplicate event_id is rejected across a simulated daemon restart.
2. The dedupe table is bounded by TTL *and* by a row cap.
3. Dead-lettered deliveries are listable, inspectable and markable.
4. Pending (next-session) events expire into the dead-letter table rather
   than disappearing.
"""

import logging
import time

import pytest

from agent_waked.store import (
    DEFAULT_DEAD_LETTER_MAX_ROWS,
    DEFAULT_DEAD_LETTER_TTL_SECONDS,
    DEFAULT_DEDUPE_MAX_ROWS,
    StoreError,
    WakeStore,
    open_store,
    resolve_state_path,
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "state" / "state.db"


def _event(event_id="ev-1", source="github-actions", **extra):
    ev = {
        "v": 0,
        "event_id": event_id,
        "source": source,
        "kind": "webhook",
        "content": "hello",
        "meta": {},
        "wake": True,
    }
    ev.update(extra)
    return ev


# ── durable dedupe ────────────────────────────────────────────────────────────


def test_first_event_is_not_duplicate(db_path):
    store = WakeStore(db_path)
    try:
        assert store.check_and_record_event("ev-1", "src") is False
        assert store.check_and_record_event("ev-1", "src") is True
    finally:
        store.close()


def test_dedupe_survives_restart(db_path):
    """AC-1: the whole point of BC-WAKE-004."""
    store = WakeStore(db_path)
    assert store.check_and_record_event("ev-replay", "src") is False
    store.close()

    # Simulated daemon restart: brand-new process-equivalent object, same file.
    store2 = WakeStore(db_path)
    try:
        assert store2.check_and_record_event("ev-replay", "src") is True
    finally:
        store2.close()


def test_dedupe_ttl_retention(db_path):
    store = WakeStore(db_path, dedupe_ttl_seconds=0.05)
    try:
        store.check_and_record_event("old", "src")
        assert store.dedupe_count() == 1
        time.sleep(0.08)
        result = store.prune()
        assert result["dedupe_ttl_deleted"] == 1
        assert store.dedupe_count() == 0
        # Past the retention window the id is admitted again — that is the
        # documented trade-off of a bounded window, not a bug.
        assert store.check_and_record_event("old", "src") is False
    finally:
        store.close()


def test_dedupe_row_cap_retention(db_path):
    store = WakeStore(db_path, dedupe_max_rows=10)
    try:
        for i in range(25):
            store.check_and_record_event(f"ev-{i:02d}", "src")
        store.prune()
        assert store.dedupe_count() == 10
        # The newest ids survived; the oldest were evicted.
        assert store.check_and_record_event("ev-24", "src") is True
        assert store.check_and_record_event("ev-00", "src") is False
    finally:
        store.close()


def test_amortised_prune_bounds_growth(db_path):
    """Growth is bounded without an explicit prune() call from the caller."""
    store = WakeStore(db_path, dedupe_max_rows=5)
    try:
        for i in range(600):  # > _PRUNE_INTERVAL, so the sweep fires twice
            store.check_and_record_event(f"auto-{i:04d}", "src")
        # Between sweeps the table can overshoot; it must not grow unbounded.
        assert store.dedupe_count() <= 300
        store.prune()
        assert store.dedupe_count() == 5
    finally:
        store.close()


# ── pending queue ─────────────────────────────────────────────────────────────


def test_enqueue_and_claim_pending(db_path):
    store = WakeStore(db_path)
    try:
        assert store.enqueue_pending(_event("ev-a")) is True
        # Idempotent on event_id.
        assert store.enqueue_pending(_event("ev-a")) is False
        assert store.pending_count() == 1

        claimed = store.claim_pending("github-actions")
        assert [c.event_id for c in claimed] == ["ev-a"]
        assert claimed[0].attempts == 1
        assert claimed[0].event["content"] == "hello"

        # Claiming again increments attempts (at-least-once until acked).
        assert store.claim_pending("github-actions")[0].attempts == 2

        store.delete_pending(claimed[0].row_id)
        assert store.pending_count() == 0
    finally:
        store.close()


def test_pending_claim_is_source_scoped(db_path):
    store = WakeStore(db_path)
    try:
        store.enqueue_pending(_event("ev-a", source="alpha"))
        store.enqueue_pending(_event("ev-b", source="beta"))
        assert [c.event_id for c in store.claim_pending("alpha")] == ["ev-a"]
    finally:
        store.close()


def test_pending_queue_is_bounded(db_path):
    store = WakeStore(db_path, pending_max_rows=3)
    try:
        for i in range(5):
            store.enqueue_pending(_event(f"ev-{i}"))
        assert store.pending_count() == 3
    finally:
        store.close()


def test_expired_pending_becomes_dead_letter(db_path):
    store = WakeStore(db_path, pending_ttl_seconds=0.05)
    try:
        store.enqueue_pending(_event("ev-expiring"))
        time.sleep(0.08)
        result = store.prune()
        assert result["pending_expired"] == 1
        assert store.pending_count() == 0
        dl = store.list_dead_letters()
        assert len(dl) == 1
        assert dl[0].kind == "next_session"
        assert dl[0].ref_id == "ev-expiring"
        assert "expired" in (dl[0].error or "")
    finally:
        store.close()


def test_pending_survives_restart(db_path):
    store = WakeStore(db_path)
    store.enqueue_pending(_event("ev-durable"))
    store.close()

    store2 = WakeStore(db_path)
    try:
        assert [p.event_id for p in store2.list_pending()] == ["ev-durable"]
    finally:
        store2.close()


# ── dead letter ───────────────────────────────────────────────────────────────


def test_dead_letter_roundtrip(db_path):
    store = WakeStore(db_path)
    try:
        dl_id = store.dead_letter(
            kind="reply",
            source="github-actions",
            ref_id="reply-1",
            payload={"content": "hi"},
            error="HTTP 500",
            attempts=3,
        )
        assert store.dead_letter_count() == 1
        entry = store.get_dead_letter(dl_id)
        assert entry is not None
        assert entry.payload == {"content": "hi"}
        assert entry.error == "HTTP 500"
        assert entry.attempts == 3
        assert entry.to_json()["kind"] == "reply"

        # Successfully redriven entries drop out of the default listing.
        store.mark_redriven(dl_id, "ok")
        assert store.list_dead_letters() == []
        assert len(store.list_dead_letters(include_redriven=True)) == 1
        assert store.dead_letter_count() == 0
    finally:
        store.close()


def test_dead_letter_kind_filter_and_purge(db_path):
    store = WakeStore(db_path)
    try:
        store.dead_letter(kind="reply", source="s", ref_id="r1", payload={})
        store.dead_letter(kind="next_session", source="s", ref_id="e1", payload={})
        assert len(store.list_dead_letters(kind="reply")) == 1
        assert store.purge_dead_letters(older_than_seconds=3600) == 0
        assert store.purge_dead_letters() == 2
        assert store.dead_letter_count() == 0
    finally:
        store.close()


def test_dead_letter_rejects_unknown_kind(db_path):
    store = WakeStore(db_path)
    try:
        with pytest.raises(ValueError):
            store.dead_letter(kind="nonsense", source="s", ref_id="x", payload={})
    finally:
        store.close()


# ── path resolution / open_store ──────────────────────────────────────────────


def test_resolve_state_path_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_WAKE_STATE_DIR", raising=False)
    cfg = {"state": {"dir": str(tmp_path / "from-config")}}
    assert resolve_state_path(cfg) == tmp_path / "from-config" / "state.db"

    monkeypatch.setenv("AGENT_WAKE_STATE_DIR", str(tmp_path / "from-env"))
    assert resolve_state_path(cfg) == tmp_path / "from-env" / "state.db"


def test_open_store_applies_config_knobs(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WAKE_STATE_DIR", str(tmp_path))
    store = open_store({"state": {"dedupe_max_rows": 7, "pending_max_attempts": 2}})
    try:
        assert store.dedupe_max_rows == 7
        assert store.pending_max_attempts == 2
    finally:
        store.close()


def test_open_store_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WAKE_STATE_DIR", str(tmp_path))
    store = open_store({})
    try:
        assert store.dedupe_max_rows == DEFAULT_DEDUPE_MAX_ROWS
    finally:
        store.close()


def test_unopenable_store_raises_store_error(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    with pytest.raises(StoreError):
        WakeStore(blocker / "sub" / "state.db")


# ── dead-letter retention (WI-001) ────────────────────────────────────────────


def test_dead_letter_ttl_retention(db_path):
    """The dead-letter table used to grow forever; prune() never touched it."""
    store = WakeStore(db_path, dead_letter_ttl_seconds=0.05)
    try:
        store.dead_letter(kind="reply", source="s", ref_id="old", payload={})
        assert store.dead_letter_count() == 1
        time.sleep(0.08)
        result = store.prune()
        assert result["dead_letter_ttl_deleted"] == 1
        assert store.dead_letter_count() == 0
    finally:
        store.close()


def test_dead_letter_row_cap_retention(db_path):
    store = WakeStore(db_path, dead_letter_max_rows=5)
    try:
        for i in range(12):
            store.dead_letter(
                kind="reply", source="s", ref_id=f"r-{i:02d}", payload={"i": i}
            )
        result = store.prune()
        assert result["dead_letter_cap_deleted"] == 7
        remaining = {e.ref_id for e in store.list_dead_letters(limit=100)}
        assert len(remaining) == 5
        # The newest survived; the oldest were evicted.
        assert "r-11" in remaining
        assert "r-00" not in remaining
    finally:
        store.close()


def test_dead_letter_cap_spends_redriven_rows_first(db_path):
    """An entry an operator has not dealt with outlives one they have."""
    store = WakeStore(db_path, dead_letter_max_rows=2)
    try:
        redriven = [
            store.dead_letter(kind="reply", source="s", ref_id=f"done-{i}", payload={})
            for i in range(3)
        ]
        for dl_id in redriven:
            store.mark_redriven(dl_id, "ok")
        fresh = [
            store.dead_letter(kind="reply", source="s", ref_id=f"todo-{i}", payload={})
            for i in range(2)
        ]
        store.prune()
        surviving = {
            e.ref_id for e in store.list_dead_letters(limit=100, include_redriven=True)
        }
        assert surviving == {"todo-0", "todo-1"}
        assert len(fresh) == 2
    finally:
        store.close()


def test_dead_letter_cap_eviction_of_unredriven_rows_is_loud(db_path, caplog):
    """Dropping an alert nobody handled is data loss; it must not be silent."""
    store = WakeStore(db_path, dead_letter_max_rows=1)
    try:
        store.dead_letter(kind="reply", source="s", ref_id="a", payload={})
        store.dead_letter(kind="reply", source="s", ref_id="b", payload={})
        with caplog.at_level(logging.WARNING, logger="agent_waked.store"):
            store.prune()
        assert "never" in caplog.text and "redriven" in caplog.text
    finally:
        store.close()


def test_dead_letter_cap_does_not_evict_rows_it_just_created(db_path):
    """The pending→dead-letter promotion must not be undone by its own sweep."""
    store = WakeStore(
        db_path, pending_ttl_seconds=0.05, dead_letter_max_rows=1
    )
    try:
        store.enqueue_pending(_event("ev-a"))
        store.enqueue_pending(_event("ev-b"))
        time.sleep(0.08)
        result = store.prune()
        assert result["pending_expired"] == 2
        # Both promotions survive this sweep; the cap applies on the next one.
        assert store.dead_letter_count() == 2
    finally:
        store.close()


def test_amortised_prune_bounds_dead_letter_growth(db_path):
    """Growth is bounded without an operator ever running `dead-letter purge`."""
    store = WakeStore(db_path, dead_letter_max_rows=5)
    try:
        for i in range(20):
            store.dead_letter(
                kind="human_delivery",
                source="dossier",
                ref_id=f"ev-{i:02d}",
                payload={"event": {"content": "x" * 512}, "principal_id": "operator"},
            )
        store.prune()
        assert store.dead_letter_count(include_redriven=True) == 5
    finally:
        store.close()


def test_dead_letter_retention_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WAKE_STATE_DIR", str(tmp_path / "state"))
    store = open_store(
        {"state": {"dead_letter_ttl_seconds": 60, "dead_letter_max_rows": 7}}
    )
    try:
        assert store.dead_letter_ttl_seconds == 60
        assert store.dead_letter_max_rows == 7
    finally:
        store.close()


def test_dead_letter_retention_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WAKE_STATE_DIR", str(tmp_path / "state"))
    store = open_store({})
    try:
        assert store.dead_letter_ttl_seconds == DEFAULT_DEAD_LETTER_TTL_SECONDS
        assert store.dead_letter_max_rows == DEFAULT_DEAD_LETTER_MAX_ROWS
    finally:
        store.close()
