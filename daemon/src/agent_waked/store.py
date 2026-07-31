"""Durable daemon state: dedupe window, next-session queue, dead-letter.

Spec reference: v1-daemon-spec.md §7 (dedupe), §8 (outbox), Plan 006 Phase 1
(next-session delivery).  Closes BC-WAKE-004 (dedupe lost on restart) and
BC-WAKE-012 (no durable redelivery / no dead-letter visibility).

Why SQLite
----------
The daemon had *no* persistence at all before this module: dedupe was an
in-memory FIFO and the outbox retried in-process and then logged.  The
requirement is a store that (a) survives a daemon restart, (b) is readable
by a *separate* operator CLI process while the daemon holds it open, and
(c) adds no dependency.  ``sqlite3`` is in the stdlib, gives us atomic
commits and cross-process reads under WAL, and opens in about a millisecond
— so the daemon startup path stays fast.  Anything heavier (regista /
Postgres) would make the daemon's core loop depend on a service that
AGENTS.md explicitly says must stay optional.

Concurrency
-----------
All calls are synchronous.  Each operation is a single indexed statement on
a local file, i.e. tens of microseconds; wrapping them in a thread executor
would cost more than it saves.  The connection is created with
``check_same_thread=False`` and every statement is taken under a
``threading.Lock`` so the CLI's short-lived connection and the daemon's
long-lived one cannot interleave a transaction.

Retention
---------
The dedupe table is bounded twice over: rows older than ``dedupe_ttl_seconds``
are dropped, and if the table still exceeds ``dedupe_max_rows`` the oldest
rows above the cap are dropped.  Pruning runs on open and then amortised
every ``_PRUNE_INTERVAL`` inserts, so no single ingest pays the full cost.
Pending (next-session) rows expire after ``pending_ttl_seconds`` and are
moved to the dead-letter table rather than deleted, so an event is never
lost silently.

The dead-letter table is bounded the same way, by ``dead_letter_ttl_seconds``
(30 days, deliberately generous — nobody looks at a dead-letter the same day)
and ``dead_letter_max_rows``.  It has to be: rows carry full event bodies, and
both expired pending events and failed human deliveries add to it, so an
unattended daemon would grow the state db forever.  Dropping a row that was
never redriven is real data loss, on the TTL path as much as the cap path, and
both log it at warning.  When the cap does bite, eviction ranks on operator
value — already-redriven first, then ``next_session``, then ``reply``, then
``human_delivery`` — so a burst of expiring queue entries cannot spend the
human alerts.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ulid import ULID

log = logging.getLogger("agent_waked.store")

SCHEMA_VERSION = 2

DEFAULT_DEDUPE_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_DEDUPE_MAX_ROWS = 100_000
DEFAULT_PENDING_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_PENDING_MAX_ROWS = 10_000
DEFAULT_PENDING_MAX_ATTEMPTS = 5
# Dead-letter retention. Longer than the dedupe/pending windows on purpose: a
# dead-letter is the record of something that already went wrong, and an
# operator may not look for a week. But it is not unbounded — rows carry full
# event bodies, and every expired pending event and every failed human
# delivery adds one, so an unattended daemon would otherwise grow the state db
# without limit.
DEFAULT_DEAD_LETTER_TTL_SECONDS = 30 * 24 * 3600
# Strictly larger than DEFAULT_PENDING_MAX_ROWS, and that relationship is the
# point rather than a coincidence. ``prune`` promotes every expired pending
# event into this table in one pass, so a cap below the queue's own cap means a
# single full-queue expiry burst can overflow the table on its own and start
# evicting unrelated human alerts. Sizing it above the queue means the burst
# fits; the kind-ranked eviction order is the second line of defence for when
# an operator has narrowed the gap.
DEFAULT_DEAD_LETTER_MAX_ROWS = 2 * DEFAULT_PENDING_MAX_ROWS

# Amortised pruning: run the retention sweep once every N dedupe inserts.
_PRUNE_INTERVAL = 256

# Every kind here must be handled by ``cli.queue._cmd_dead_letter_redrive``:
# a kind that can be written but not redriven is a durable record of an alert
# an operator cannot act on. ``test_cli_queue`` asserts the two stay in step.
DEAD_LETTER_KINDS = ("reply", "next_session", "human_delivery")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dedupe (
    event_id TEXT PRIMARY KEY,
    source   TEXT NOT NULL,
    seen_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dedupe_seen_at ON dedupe(seen_at);

CREATE TABLE IF NOT EXISTS pending (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL UNIQUE,
    source      TEXT NOT NULL,
    destination TEXT,
    target      TEXT,
    event_json  TEXT NOT NULL,
    enqueued_at REAL NOT NULL,
    expires_at  REAL NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pending_source ON pending(source);
CREATE INDEX IF NOT EXISTS idx_pending_destination ON pending(destination);
CREATE INDEX IF NOT EXISTS idx_pending_expires ON pending(expires_at);

CREATE TABLE IF NOT EXISTS dead_letter (
    id             TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,
    source         TEXT NOT NULL,
    ref_id         TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    error          TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    redriven_at    REAL,
    redrive_status TEXT
);
CREATE INDEX IF NOT EXISTS idx_dead_letter_created ON dead_letter(created_at);
"""


class StoreError(Exception):
    """Raised when the durable store cannot be opened or migrated."""


@dataclass(frozen=True)
class PendingEvent:
    """One queued next-session event.

    ``destination`` is the addressee the row is reserved for (WI-006).  Keyed on
    ``source`` alone, a drain handed every queued event for a sender to whichever
    session connected first — the BC-001 fan-out with a delay in front of it, and
    the reason BC-WAKE-012 asked for a *session-scoped* inbox rather than just a
    durable one.  Nullable so rows written by an older build still drain: the
    schema migration backfills them from ``source``, which is correct because the
    legacy translation makes a source name and its destination name identical.
    """

    row_id: int
    event_id: str
    source: str
    destination: str | None
    target: str | None
    event: dict[str, Any]
    enqueued_at: float
    expires_at: float
    attempts: int


@dataclass(frozen=True)
class DeadLetter:
    """One dead-lettered delivery."""

    id: str
    kind: str
    source: str
    ref_id: str
    payload: dict[str, Any]
    error: str | None
    attempts: int
    created_at: float
    redriven_at: float | None
    redrive_status: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "source": self.source,
            "ref_id": self.ref_id,
            "payload": self.payload,
            "error": self.error,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "redriven_at": self.redriven_at,
            "redrive_status": self.redrive_status,
        }


DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "agent-wake"
STATE_DB_NAME = "state.db"


def resolve_state_path(cfg: dict[str, Any] | None = None) -> Path:
    """Resolve the state database path.

    Precedence: ``AGENT_WAKE_STATE_DIR`` env override (used by the container
    and by tests) > ``cfg["state"]["dir"]`` > ``~/.local/state/agent-wake``.
    Mirrors ``main._resolve_socket_path`` so operators only have one mental
    model for daemon-owned local state.
    """
    env_dir = os.environ.get("AGENT_WAKE_STATE_DIR")
    if env_dir:
        return Path(env_dir) / STATE_DB_NAME
    if cfg:
        state_cfg = cfg.get("state") or {}
        explicit = state_cfg.get("dir")
        if explicit:
            return Path(str(explicit)) / STATE_DB_NAME
    return DEFAULT_STATE_DIR / STATE_DB_NAME


class WakeStore:
    """Durable dedupe / next-session queue / dead-letter store."""

    def __init__(
        self,
        path: Path | str,
        *,
        dedupe_ttl_seconds: float = DEFAULT_DEDUPE_TTL_SECONDS,
        dedupe_max_rows: int = DEFAULT_DEDUPE_MAX_ROWS,
        pending_ttl_seconds: float = DEFAULT_PENDING_TTL_SECONDS,
        pending_max_rows: int = DEFAULT_PENDING_MAX_ROWS,
        pending_max_attempts: int = DEFAULT_PENDING_MAX_ATTEMPTS,
        dead_letter_ttl_seconds: float = DEFAULT_DEAD_LETTER_TTL_SECONDS,
        dead_letter_max_rows: int = DEFAULT_DEAD_LETTER_MAX_ROWS,
    ):
        self.path = Path(path)
        self.dedupe_ttl_seconds = float(dedupe_ttl_seconds)
        self.dedupe_max_rows = int(dedupe_max_rows)
        self.pending_ttl_seconds = float(pending_ttl_seconds)
        self.pending_max_rows = int(pending_max_rows)
        self.pending_max_attempts = int(pending_max_attempts)
        self.dead_letter_ttl_seconds = float(dead_letter_ttl_seconds)
        self.dead_letter_max_rows = int(dead_letter_max_rows)

        self._lock = threading.Lock()
        self._inserts_since_prune = 0

        if str(self.path) != ":memory:":
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError as exc:
                raise StoreError(f"cannot create state dir {self.path.parent}: {exc}") from exc
        try:
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open state db {self.path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        try:
            # WAL lets the operator CLI read while the daemon holds the db open.
            # ``:memory:`` databases reject WAL; ignore the failure there.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            prior = self._read_schema_version()
            self._conn.executescript(_SCHEMA)
            self._migrate(prior)
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            raise StoreError(f"cannot initialise state db {self.path}: {exc}") from exc

        if str(self.path) != ":memory:":
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

        self.prune()

    # ── schema migration ─────────────────────────────────────────────────────

    def _read_schema_version(self) -> int | None:
        """The version recorded in an existing database, or None if new.

        Read *before* ``_SCHEMA`` runs, because ``CREATE TABLE IF NOT EXISTS``
        makes "the table exists" say nothing about which columns it has.
        """
        try:
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return None

    def _migrate(self, prior: int | None) -> None:
        """Bring an existing database up to ``SCHEMA_VERSION`` in place.

        v1 → v2 (WI-006) adds ``pending.destination``.  ``CREATE TABLE IF NOT
        EXISTS`` will not add a column to a table that already exists, so a
        daemon upgraded on a live box would otherwise fail every enqueue with
        ``no such column``.

        Existing rows are backfilled from ``source``, and that is *correct*
        rather than merely convenient: the addressing translation makes a v1
        source name and its destination name the same string, so a row queued
        before the upgrade already names its destination — it just spelled it
        ``source``.
        """
        if prior is None or prior >= SCHEMA_VERSION:
            return
        cols = {
            str(r["name"])
            for r in self._conn.execute("PRAGMA table_info(pending)").fetchall()
        }
        if "destination" not in cols:
            log.info(
                "migrating state db %s from schema v%d to v%d "
                "(pending.destination)",
                self.path,
                prior,
                SCHEMA_VERSION,
            )
            self._conn.execute("ALTER TABLE pending ADD COLUMN destination TEXT")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pending_destination "
                "ON pending(destination)"
            )
        cur = self._conn.execute(
            "UPDATE pending SET destination = source WHERE destination IS NULL"
        )
        if cur.rowcount:
            log.info(
                "backfilled destination on %d queued event(s) from their source",
                cur.rowcount,
            )

    # ── lifecycle ────────────────────────────────────────────────────────────

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ── dedupe ───────────────────────────────────────────────────────────────

    def check_and_record_event(self, event_id: str, source: str = "") -> bool:
        """Return True if *event_id* was already seen; otherwise record it.

        Durable: the answer is the same after a daemon restart, which is the
        whole point (BC-WAKE-004).  The insert and the check are one atomic
        statement — ``INSERT OR IGNORE`` plus ``rowcount`` — so two concurrent
        ingests of the same id cannot both be admitted.
        """
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO dedupe (event_id, source, seen_at) VALUES (?, ?, ?)",
                (event_id, source, now),
            )
            self._conn.commit()
            inserted = cur.rowcount == 1
            self._inserts_since_prune += 1
            due = self._inserts_since_prune >= _PRUNE_INTERVAL
        if due:
            self.prune()
        return not inserted

    def dedupe_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM dedupe").fetchone()
        return int(row["n"])

    # ── next-session queue ───────────────────────────────────────────────────

    def enqueue_pending(
        self, event: dict[str, Any], destination: str | None = None
    ) -> bool:
        """Queue *event* for delivery at the next session for its destination.

        Returns False if the event_id is already queued (the queue is
        idempotent on event_id, matching dedupe) or if the queue is full.

        ``destination`` defaults to the event's ``source``, which is the correct
        legacy answer — a v0/v1 source name *is* its destination name after the
        addressing translation — so callers that predate WI-006 keep working.

        Note the idempotency key is still ``event_id`` alone.  One event fanned
        out to several destinations therefore takes at most one queue row: the
        first destination with no live subscriber reserves it.  Making the key
        ``(event_id, destination)`` would let one offline sibling multiply a
        burst by the fan-out width, and the queue cap exists precisely to bound
        that.
        """
        event_id = str(event.get("event_id", ""))
        if not event_id:
            return False
        source = str(event.get("source", ""))
        meta = event.get("meta")
        target = None
        if isinstance(meta, dict) and isinstance(meta.get("target"), str):
            target = meta["target"]
        now = time.time()
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM pending").fetchone()
            if int(row["n"]) >= self.pending_max_rows:
                log.warning(
                    "next-session queue full (%d rows); refusing event_id=%s",
                    self.pending_max_rows,
                    event_id,
                )
                return False
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO pending "
                "(event_id, source, destination, target, event_json, "
                "enqueued_at, expires_at, attempts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    event_id,
                    source,
                    destination if destination is not None else source,
                    target,
                    json.dumps(event, separators=(",", ":")),
                    now,
                    now + self.pending_ttl_seconds,
                ),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def claim_pending(self, destination: str, limit: int = 100) -> list[PendingEvent]:
        """Return queued events for *destination*, incrementing their attempts.

        Claimed rows stay in the table: they are removed by
        ``delete_pending`` once the adapter acks, so a crash between claim and
        ack redelivers rather than loses (at-least-once, per Plan 006 §5).

        Matching is on ``destination`` **or** a legacy row whose ``destination``
        is NULL and whose ``source`` matches.  The NULL arm covers a row written
        by a pre-WI-006 daemon in a database the migration has since touched but
        which was enqueued in between — belt to the backfill's braces, and it
        costs one indexed OR.
        """
        now = time.time()
        out: list[PendingEvent] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM pending WHERE "
                "(destination = ? OR (destination IS NULL AND source = ?)) "
                "AND expires_at > ? ORDER BY id ASC LIMIT ?",
                (destination, destination, now, limit),
            ).fetchall()
            for row in rows:
                self._conn.execute(
                    "UPDATE pending SET attempts = attempts + 1 WHERE id = ?",
                    (row["id"],),
                )
            self._conn.commit()
            for row in rows:
                out.append(_pending_from_row(row, attempts_delta=1))
        return out

    def delete_pending(self, row_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM pending WHERE id = ?", (row_id,))
            self._conn.commit()

    def list_pending(
        self,
        source: str | None = None,
        limit: int = 200,
        destination: str | None = None,
    ) -> list[PendingEvent]:
        """Operator-facing listing. *destination* narrows to one addressee."""
        with self._lock:
            if destination:
                rows = self._conn.execute(
                    "SELECT * FROM pending WHERE "
                    "(destination = ? OR (destination IS NULL AND source = ?)) "
                    "ORDER BY id ASC LIMIT ?",
                    (destination, destination, limit),
                ).fetchall()
            elif source:
                rows = self._conn.execute(
                    "SELECT * FROM pending WHERE source = ? ORDER BY id ASC LIMIT ?",
                    (source, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM pending ORDER BY id ASC LIMIT ?", (limit,)
                ).fetchall()
        return [_pending_from_row(r) for r in rows]

    def pending_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM pending").fetchone()
        return int(row["n"])

    # ── dead letter ──────────────────────────────────────────────────────────

    def dead_letter(
        self,
        *,
        kind: str,
        source: str,
        ref_id: str,
        payload: dict[str, Any],
        error: str | None = None,
        attempts: int = 0,
    ) -> str:
        """Record a permanently-failed delivery and return its dead-letter id."""
        if kind not in DEAD_LETTER_KINDS:
            raise ValueError(f"unknown dead-letter kind {kind!r}")
        dl_id = str(ULID())
        with self._lock:
            self._conn.execute(
                "INSERT INTO dead_letter "
                "(id, kind, source, ref_id, payload_json, error, attempts, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    dl_id,
                    kind,
                    source,
                    ref_id,
                    json.dumps(payload, separators=(",", ":")),
                    error,
                    attempts,
                    time.time(),
                ),
            )
            self._conn.commit()
        log.warning(
            "dead-lettered kind=%s source=%s ref_id=%s id=%s error=%s",
            kind,
            source,
            ref_id,
            dl_id,
            error,
        )
        return dl_id

    def list_dead_letters(
        self,
        *,
        kind: str | None = None,
        limit: int = 100,
        include_redriven: bool = False,
    ) -> list[DeadLetter]:
        sql = "SELECT * FROM dead_letter WHERE 1=1"
        params: list[Any] = []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if not include_redriven:
            sql += " AND (redrive_status IS NULL OR redrive_status != 'ok')"
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_dead_letter_from_row(r) for r in rows]

    def get_dead_letter(self, dl_id: str) -> DeadLetter | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM dead_letter WHERE id = ?", (dl_id,)
            ).fetchone()
        return _dead_letter_from_row(row) if row is not None else None

    def mark_redriven(self, dl_id: str, status: str, error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE dead_letter SET redriven_at = ?, redrive_status = ?, "
                "error = COALESCE(?, error) WHERE id = ?",
                (time.time(), status, error, dl_id),
            )
            self._conn.commit()

    def dead_letter_count(self, *, include_redriven: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM dead_letter"
        if not include_redriven:
            sql += " WHERE redrive_status IS NULL OR redrive_status != 'ok'"
        with self._lock:
            row = self._conn.execute(sql).fetchone()
        return int(row["n"])

    def purge_dead_letters(
        self, *, older_than_seconds: float | None = None, redriven_only: bool = False
    ) -> int:
        sql = "DELETE FROM dead_letter WHERE 1=1"
        params: list[Any] = []
        if older_than_seconds is not None:
            sql += " AND created_at < ?"
            params.append(time.time() - older_than_seconds)
        if redriven_only:
            sql += " AND redrive_status = 'ok'"
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
        return int(cur.rowcount)

    # ── retention ────────────────────────────────────────────────────────────

    def prune(self) -> dict[str, int]:
        """Apply retention policy. Returns per-table deletion counts.

        Expired pending events are dead-lettered, not dropped: "we never got a
        session for this in N days" is exactly the thing an operator needs to
        see.

        The dead-letter table is swept too, which it was not before: it grew
        without limit while holding full event bodies, and nothing but a manual
        ``dead-letter purge`` ever removed a row. Rows past
        ``dead_letter_ttl_seconds`` go first, then rows above
        ``dead_letter_max_rows``, evicted in order of operator value: anything
        already redriven, then by kind (``next_session`` before ``reply`` before
        ``human_delivery`` — a re-queueable event that already sat unclaimed for
        its whole TTL is worth less than an alert addressed to a person that
        never arrived), then oldest first. Dropping an entry that was never
        redriven is data loss on *either* path, and both log it at warning.

        Sweep order matters: the dead-letter sweep runs *before* expired pending
        events are dead-lettered, so a row created by this same call is never
        immediately eligible for its own cap eviction. That is only true one
        prune deep, which is why the default cap is sized above the pending
        queue's cap and why eviction ranks on kind — otherwise a burst of
        expiring events evicts the human alerts on the *next* sweep instead.
        """
        now = time.time()
        expired_pending: list[PendingEvent] = []
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM dedupe WHERE seen_at < ?",
                (now - self.dedupe_ttl_seconds,),
            )
            dedupe_ttl_deleted = int(cur.rowcount)

            row = self._conn.execute("SELECT COUNT(*) AS n FROM dedupe").fetchone()
            overflow = int(row["n"]) - self.dedupe_max_rows
            dedupe_cap_deleted = 0
            if overflow > 0:
                cur = self._conn.execute(
                    "DELETE FROM dedupe WHERE event_id IN "
                    "(SELECT event_id FROM dedupe ORDER BY seen_at ASC LIMIT ?)",
                    (overflow,),
                )
                dedupe_cap_deleted = int(cur.rowcount)

            rows = self._conn.execute(
                "SELECT * FROM pending WHERE expires_at <= ?", (now,)
            ).fetchall()
            expired_pending = [_pending_from_row(r) for r in rows]
            if expired_pending:
                self._conn.execute(
                    "DELETE FROM pending WHERE expires_at <= ?", (now,)
                )

            # TTL sweep. Count what is about to be lost *before* deleting it:
            # the docs claimed un-redriven eviction was always logged, and on
            # this path — the dominant one, since the TTL fires long before any
            # sane cap — it silently was not.
            ttl_cutoff = now - self.dead_letter_ttl_seconds
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM dead_letter WHERE created_at < ? "
                "AND (redrive_status IS NULL OR redrive_status != 'ok')",
                (ttl_cutoff,),
            ).fetchone()
            ttl_unredriven = int(row["n"])
            cur = self._conn.execute(
                "DELETE FROM dead_letter WHERE created_at < ?", (ttl_cutoff,)
            )
            dead_letter_ttl_deleted = int(cur.rowcount)

            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM dead_letter"
            ).fetchone()
            dl_overflow = int(row["n"]) - self.dead_letter_max_rows
            dead_letter_cap_deleted = 0
            cap_unredriven = 0
            if dl_overflow > 0:
                # Eviction order is operator value, not just age:
                #   1. anything already redriven — the operator is done with it;
                #   2. then by kind, cheapest first. A next_session entry is a
                #      re-queueable event that already sat unclaimed for its
                #      whole TTL; a human_delivery entry is an alert addressed
                #      to a person that never arrived. Age alone got this
                #      backwards: a burst of expiring pending events (the queue
                #      cap is larger than this table's) evicted every
                #      pre-existing human alert in favour of the promotions that
                #      caused the overflow.
                #   3. then oldest first.
                victims = self._conn.execute(
                    "SELECT id, redrive_status FROM dead_letter "
                    "ORDER BY (redrive_status = 'ok') DESC, "
                    "  CASE kind "
                    "    WHEN 'human_delivery' THEN 2 "
                    "    WHEN 'reply' THEN 1 "
                    "    ELSE 0 "
                    "  END ASC, "
                    "  created_at ASC "
                    "LIMIT ?",
                    (dl_overflow,),
                ).fetchall()
                cap_unredriven = sum(
                    1 for v in victims if v["redrive_status"] != "ok"
                )
                self._conn.executemany(
                    "DELETE FROM dead_letter WHERE id = ?",
                    [(v["id"],) for v in victims],
                )
                dead_letter_cap_deleted = len(victims)

            self._conn.commit()
            self._inserts_since_prune = 0

        if ttl_unredriven:
            log.warning(
                "dead-letter retention (%.0fs) expired %s that %s never "
                "redriven — %s permanently lost; redrive or purge sooner, or "
                "raise state.dead_letter_ttl_seconds",
                self.dead_letter_ttl_seconds,
                _entries(ttl_unredriven),
                "was" if ttl_unredriven == 1 else "were",
                "that delivery is" if ttl_unredriven == 1 else "those deliveries are",
            )
        if cap_unredriven:
            log.warning(
                "dead-letter cap (max %d) evicted %s that %s never redriven "
                "— %s permanently lost; raise state.dead_letter_max_rows or "
                "redrive/purge more often",
                self.dead_letter_max_rows,
                _entries(cap_unredriven),
                "was" if cap_unredriven == 1 else "were",
                "that delivery is" if cap_unredriven == 1 else "those deliveries are",
            )

        for pe in expired_pending:
            self.dead_letter(
                kind="next_session",
                source=pe.source,
                ref_id=pe.event_id,
                payload=pe.event,
                error=(
                    f"expired after {self.pending_ttl_seconds:.0f}s with no session "
                    f"for source {pe.source!r}"
                ),
                attempts=pe.attempts,
            )

        result = {
            "dedupe_ttl_deleted": dedupe_ttl_deleted,
            "dedupe_cap_deleted": dedupe_cap_deleted,
            "dead_letter_ttl_deleted": dead_letter_ttl_deleted,
            "dead_letter_cap_deleted": dead_letter_cap_deleted,
            "pending_expired": len(expired_pending),
        }
        if any(result.values()):
            log.info("store prune %s", result)
        return result


def _entries(n: int) -> str:
    """Pluralise a row count for a log line ("1 entry" / "3 entries")."""
    return f"{n} entry" if n == 1 else f"{n} entries"


def _pending_from_row(row: sqlite3.Row, attempts_delta: int = 0) -> PendingEvent:
    keys = row.keys()
    return PendingEvent(
        row_id=int(row["id"]),
        event_id=str(row["event_id"]),
        source=str(row["source"]),
        destination=(row["destination"] if "destination" in keys else None),
        target=row["target"],
        event=json.loads(row["event_json"]),
        enqueued_at=float(row["enqueued_at"]),
        expires_at=float(row["expires_at"]),
        attempts=int(row["attempts"]) + attempts_delta,
    )


def _dead_letter_from_row(row: sqlite3.Row) -> DeadLetter:
    return DeadLetter(
        id=str(row["id"]),
        kind=str(row["kind"]),
        source=str(row["source"]),
        ref_id=str(row["ref_id"]),
        payload=json.loads(row["payload_json"]),
        error=row["error"],
        attempts=int(row["attempts"]),
        created_at=float(row["created_at"]),
        redriven_at=row["redriven_at"],
        redrive_status=row["redrive_status"],
    )


def open_store(cfg: dict[str, Any] | None = None) -> WakeStore:
    """Open the daemon state store from *cfg* (or env/default paths)."""
    state_cfg = (cfg or {}).get("state") or {}
    return WakeStore(
        resolve_state_path(cfg),
        dedupe_ttl_seconds=state_cfg.get("dedupe_ttl_seconds", DEFAULT_DEDUPE_TTL_SECONDS),
        dedupe_max_rows=state_cfg.get("dedupe_max_rows", DEFAULT_DEDUPE_MAX_ROWS),
        pending_ttl_seconds=state_cfg.get("pending_ttl_seconds", DEFAULT_PENDING_TTL_SECONDS),
        pending_max_rows=state_cfg.get("pending_max_rows", DEFAULT_PENDING_MAX_ROWS),
        pending_max_attempts=state_cfg.get(
            "pending_max_attempts", DEFAULT_PENDING_MAX_ATTEMPTS
        ),
        dead_letter_ttl_seconds=state_cfg.get(
            "dead_letter_ttl_seconds", DEFAULT_DEAD_LETTER_TTL_SECONDS
        ),
        dead_letter_max_rows=state_cfg.get(
            "dead_letter_max_rows", DEFAULT_DEAD_LETTER_MAX_ROWS
        ),
    )
