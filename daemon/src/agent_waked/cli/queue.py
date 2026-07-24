"""``agent-wake dead-letter`` and ``agent-wake pending`` subcommands.

Operator visibility over the daemon's durable store (BC-WAKE-012). Before
this, a reply whose callback was permanently down produced one warning line
and vanished; there was no way to see what had been lost, let alone retry it.

Commands
--------
``agent-wake dead-letter list``      — what failed, when, why
``agent-wake dead-letter show ID``   — the full payload of one entry
``agent-wake dead-letter redrive ID``— retry it for real
``agent-wake dead-letter purge``     — bounded cleanup
``agent-wake pending list``          — what is queued for the next session
``agent-wake pending prune``         — apply retention now

All commands honour ``--json`` and emit the suite CLI contract v1 error
envelope on failure (see ``cli.emit_error``). Read-only commands work even
without a loadable config: the store path falls back to the env override /
default location, because "the config is broken" is precisely when an
operator needs to see the backlog.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

from ..config import ConfigError, load_config
from ..store import WakeStore, open_store


def _build_queue_parsers(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> None:
    """Add the ``dead-letter`` and ``pending`` subcommands."""
    dl = sub.add_parser(
        "dead-letter", help="Inspect and redrive permanently-failed deliveries"
    )
    dl_sub = dl.add_subparsers(dest="dead_letter_command")

    list_p = dl_sub.add_parser("list", help="List dead-lettered deliveries")
    list_p.add_argument("--json", action="store_true", help="Emit a JSON document")
    list_p.add_argument(
        "--kind", choices=["reply", "next_session"], help="Filter by delivery kind"
    )
    list_p.add_argument("--limit", type=int, default=50, help="Max rows (default 50)")
    list_p.add_argument(
        "--include-redriven",
        action="store_true",
        help="Also show entries already redriven successfully",
    )
    list_p.set_defaults(func=_cmd_dead_letter_list)

    show_p = dl_sub.add_parser("show", help="Show one dead-lettered delivery in full")
    show_p.add_argument("id", help="Dead-letter id")
    show_p.add_argument("--json", action="store_true", help="Emit a JSON document")
    show_p.set_defaults(func=_cmd_dead_letter_show)

    redrive_p = dl_sub.add_parser("redrive", help="Retry a dead-lettered delivery")
    redrive_p.add_argument("id", help="Dead-letter id")
    redrive_p.add_argument("--json", action="store_true", help="Emit a JSON document")
    redrive_p.set_defaults(func=_cmd_dead_letter_redrive)

    purge_p = dl_sub.add_parser("purge", help="Delete dead-letter entries")
    purge_p.add_argument("--json", action="store_true", help="Emit a JSON document")
    purge_p.add_argument(
        "--older-than-days",
        type=float,
        help="Only delete entries created more than N days ago",
    )
    purge_p.add_argument(
        "--redriven-only",
        action="store_true",
        help="Only delete entries that were successfully redriven",
    )
    purge_p.set_defaults(func=_cmd_dead_letter_purge)

    pending = sub.add_parser(
        "pending", help="Inspect the next-session delivery queue"
    )
    pending_sub = pending.add_subparsers(dest="pending_command")

    plist_p = pending_sub.add_parser("list", help="List queued next-session events")
    plist_p.add_argument("--json", action="store_true", help="Emit a JSON document")
    plist_p.add_argument("--source", help="Filter by source")
    plist_p.add_argument("--limit", type=int, default=50, help="Max rows (default 50)")
    plist_p.set_defaults(func=_cmd_pending_list)

    pprune_p = pending_sub.add_parser(
        "prune", help="Apply retention now (expire queued events, bound dedupe)"
    )
    pprune_p.add_argument("--json", action="store_true", help="Emit a JSON document")
    pprune_p.set_defaults(func=_cmd_pending_prune)


# ── helpers ───────────────────────────────────────────────────────────────────


def _load_config_optional() -> dict[str, Any]:
    """Load config, or return an empty dict if it is missing/invalid.

    Read-only store commands must keep working when the config is broken.
    """
    try:
        return load_config()
    except (ConfigError, Exception):
        return {}


def _open(cfg: dict[str, Any] | None = None) -> WakeStore:
    return open_store(cfg if cfg is not None else _load_config_optional())


def _ts(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def _emit(use_json: bool, doc: dict[str, Any], text: str) -> int:
    if use_json:
        print(json.dumps(doc, indent=2))
    else:
        print(text)
    return 0


# ── dead-letter ───────────────────────────────────────────────────────────────


def _cmd_dead_letter_list(args: argparse.Namespace) -> int:
    store = _open()
    try:
        entries = store.list_dead_letters(
            kind=args.kind,
            limit=args.limit,
            include_redriven=args.include_redriven,
        )
        rows: list[dict[str, Any]] = []
        for e in entries:
            doc = e.to_json()
            doc.pop("payload", None)
            doc["created_at"] = _ts(e.created_at)
            doc["redriven_at"] = _ts(e.redriven_at)
            rows.append(doc)
        if args.json:
            return _emit(True, {"ok": True, "dead_letters": rows}, "")
        if not rows:
            return _emit(False, {}, "No dead-lettered deliveries.")
        lines = [
            f"{'ID':28} {'KIND':13} {'SOURCE':16} {'REF':26} CREATED",
        ]
        for r in rows:
            lines.append(
                f"{r['id']:28} {r['kind']:13} {r['source'][:16]:16} "
                f"{r['ref_id'][:26]:26} {r['created_at']}"
            )
            if r.get("error"):
                lines.append(f"    error: {r['error']}")
        lines.append("")
        lines.append(f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'}.")
        return _emit(False, {}, "\n".join(lines))
    finally:
        store.close()


def _cmd_dead_letter_show(args: argparse.Namespace) -> int:
    from . import emit_error

    store = _open()
    try:
        entry = store.get_dead_letter(args.id)
        if entry is None:
            return emit_error(
                "NOT_FOUND",
                f"no dead-letter entry with id {args.id!r}",
                use_json=args.json,
                detail="run `agent-wake dead-letter list` to see current entries",
            )
        doc = entry.to_json()
        doc["created_at"] = _ts(entry.created_at)
        doc["redriven_at"] = _ts(entry.redriven_at)
        if args.json:
            return _emit(True, {"ok": True, "dead_letter": doc}, "")
        return _emit(False, {}, json.dumps(doc, indent=2))
    finally:
        store.close()


def _cmd_dead_letter_redrive(args: argparse.Namespace) -> int:
    """Retry one dead-lettered delivery.

    ``reply`` entries are re-POSTed here and now, reusing the daemon's own
    ``Outbox`` so retry/backoff/URL-validation semantics are identical to the
    live path. ``next_session`` entries are put back on the durable queue for
    the daemon to hand to the next session — the CLI has no socket to any
    adapter, and inventing one would be a second delivery path.
    """
    from . import emit_error

    cfg = _load_config_optional()
    store = _open(cfg)
    try:
        entry = store.get_dead_letter(args.id)
        if entry is None:
            return emit_error(
                "NOT_FOUND",
                f"no dead-letter entry with id {args.id!r}",
                use_json=args.json,
            )

        if entry.kind == "next_session":
            requeued = store.enqueue_pending(entry.payload)
            store.mark_redriven(entry.id, "ok" if requeued else "failed")
            if not requeued:
                return emit_error(
                    "REDRIVE_FAILED",
                    "could not re-queue the event (already queued, or the "
                    "next-session queue is full)",
                    use_json=args.json,
                    retryable=True,
                )
            return _emit(
                args.json,
                {"ok": True, "id": entry.id, "action": "requeued"},
                f"Re-queued {entry.ref_id} for the next session on "
                f"source {entry.source!r}.",
            )

        if entry.kind == "reply":
            if not cfg:
                return emit_error(
                    "CONFIG_ERROR",
                    "redriving a reply needs a loadable config (for the "
                    "source's callback_url)",
                    use_json=args.json,
                )
            result = asyncio.run(_redrive_reply(cfg, entry.payload))
            ok = result.get("status") == "delivered"
            store.mark_redriven(
                entry.id, "ok" if ok else "failed", error=result.get("error")
            )
            if not ok:
                return emit_error(
                    "REDRIVE_FAILED",
                    f"reply redrive failed: {result.get('error')}",
                    use_json=args.json,
                    detail=f"status={result.get('status')}",
                    retryable=True,
                )
            return _emit(
                args.json,
                {"ok": True, "id": entry.id, "action": "delivered", "result": result},
                f"Redrive delivered reply {entry.ref_id} "
                f"(HTTP {result.get('http_status')}).",
            )

        return emit_error(
            "INTERNAL_ERROR",
            f"unknown dead-letter kind {entry.kind!r}",
            use_json=args.json,
        )
    finally:
        store.close()


async def _redrive_reply(cfg: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from ..outbox import Outbox

    # store=None: a failed redrive must not create a *second* dead-letter row;
    # the existing one is updated by the caller instead.
    outbox = Outbox(cfg, store=None)
    await outbox.start()
    try:
        return await outbox.deliver(
            source=str(payload.get("source", "")),
            reply_id=str(payload.get("reply_id", "")),
            in_reply_to=str(payload.get("in_reply_to", "")),
            content=str(payload.get("content", "")),
        )
    finally:
        await outbox.close()


def _cmd_dead_letter_purge(args: argparse.Namespace) -> int:
    store = _open()
    try:
        older = (
            args.older_than_days * 24 * 3600
            if args.older_than_days is not None
            else None
        )
        deleted = store.purge_dead_letters(
            older_than_seconds=older, redriven_only=args.redriven_only
        )
        return _emit(
            args.json,
            {"ok": True, "deleted": deleted},
            f"Deleted {deleted} dead-letter entr{'y' if deleted == 1 else 'ies'}.",
        )
    finally:
        store.close()


# ── pending ───────────────────────────────────────────────────────────────────


def _cmd_pending_list(args: argparse.Namespace) -> int:
    store = _open()
    try:
        rows = store.list_pending(source=args.source, limit=args.limit)
        docs = [
            {
                "event_id": r.event_id,
                "source": r.source,
                "target": r.target,
                "attempts": r.attempts,
                "enqueued_at": _ts(r.enqueued_at),
                "expires_at": _ts(r.expires_at),
            }
            for r in rows
        ]
        if args.json:
            return _emit(True, {"ok": True, "pending": docs}, "")
        if not docs:
            return _emit(False, {}, "No events queued for next-session delivery.")
        lines = [f"{'EVENT_ID':28} {'SOURCE':16} {'ATT':>3} EXPIRES"]
        for r in rows:
            lines.append(
                f"{r.event_id[:28]:28} {r.source[:16]:16} "
                f"{r.attempts:>3} {_ts(r.expires_at)}"
            )
        lines.append("")
        lines.append(f"{len(docs)} queued.")
        return _emit(False, {}, "\n".join(lines))
    finally:
        store.close()


def _cmd_pending_prune(args: argparse.Namespace) -> int:
    store = _open()
    try:
        t0 = time.monotonic()
        result = store.prune()
        result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        return _emit(
            args.json,
            {"ok": True, "pruned": result},
            "\n".join(f"{k}: {v}" for k, v in result.items()),
        )
    finally:
        store.close()


__all__ = ["_build_queue_parsers"]
