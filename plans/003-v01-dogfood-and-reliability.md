# Plan 003 — v0.1 Dogfood and Reliability

**Status:** proposed 2026-05-28
**Author:** Opus 4.8 (portfolio review)
**Strategic role:** Small Track of the 3-week grant plan. Gated, sequenced, and
deliberately feature-frozen at the front.

## The honest framing

The 2026-05-24 cross-project roadmap (§6) said plainly: v0 is built, not
dogfooded; the next step is **not** a new design doc, it is running the adapter
against a live harness and tagging v0.1. Since then the project shipped Plan 002
(secret management, Vault backend) and an identity layer (per-source
principal_id, trigger allowlist), plus ~25 design docs of identity/multi-user
review across five models over three rounds.

`design/v0-acceptance-status.md` criterion 8 still reads: the adapter has
**never talked to a live Claude Code session.** Every feature added since was
built on unvalidated assumptions about how the wake behaves in a real harness.
A Vault backend for a tool that has never woken a real session is the tell.

This plan freezes feature work until the core is validated, then fixes the three
items that are real correctness bugs (not features), then tags.

## Phase 1 — Dogfood (gate; do this before anything else)

No new code unless something breaks.

- **W1.** Install `agent-wake-claude`; start `agent-waked`; `curl` a signed wake
  to `localhost:8788`; confirm a **live `claude` session actually wakes**. Watch
  the three things the v0 spec flags as untested against real Claude Code:
  channel-tag parsing, timing, permission-request format drift.
- **W2.** Same against a live **opencode** session (`session.promptAsync` path).
- **W3.** Permission-relay round trip against a live session (request → external
  verdict → agent proceeds), since that is the most useful real-world flow and
  is only unit-tested today.
- **W4.** Fix only what the live runs break. Update
  `design/v0-acceptance-status.md` criteria 3 and 8 to reflect live results.

**Gate:** do not start Phase 2 until W1–W3 have each run clean against a real
harness at least once.

## Phase 2 — Reliability (the real bugs, not features)

These three are named as v1 deferrals in `v0-acceptance-status.md` "Deferred to
v1" but they are correctness gaps, not enhancements:

- **W5. Reply outbox + retry.** `reply.py` / `reply.ts` are fire-and-forget; a
  dropped reply is silent data loss. Add a bounded retry/outbox with documented
  drop-on-exhaustion behavior (matching the agent-notes bridge's documented
  drop-after-3 posture so the two compose predictably).
- **W6. Durable dedup.** The in-memory 256-event FIFO clears on daemon restart,
  so a redelivered event after a restart is reprocessed. Back dedup with a small
  persistent store (sqlite or a regista-backed table) keyed by `event_id`.
- **W7. Multi-session routing.** opencode broadcasts every wake to *all* active
  sessions. Add per-session targeting to the wake event so a signal can address
  one session. Claude Code is single-session today; design the field so it does
  not need to change when Claude Code gains multi-session.

## Phase 3 — Tag and document

- **W8.** Tag `v0.1`. Write install/usage in the README at a level a stranger
  could follow without reading the design docs.
- **W9.** Only *after* a clean dogfood: write a one-paragraph retro on whether
  the Plan 002 secrets / identity work was pointed at the right problem, so the
  next planning cycle does not repeat the build-ahead-of-validation pattern.

## Acceptance

- A live `claude` session and a live `opencode` session each demonstrably woken
  by an external `curl`, with evidence (transcript or log) committed under
  `reflections/` or `examples/`.
- Permission-relay round trip works live.
- Reply delivery survives a transient callback failure (retry) and a daemon
  restart does not reprocess an already-handled `event_id`.
- A wake event can target a single opencode session.
- `v0.1` tagged; README install path followed end-to-end by someone who has not
  read `design/`.

## Explicit non-goals

- No new identity / multi-user / secrets work this cycle. The existing Plan 002
  surface stays; it gets no additions until the core is validated.
- No second-harness adapter yet (gemini/codex) — legitimate, but only after
  v0.1 proves the two existing adapters work live.
