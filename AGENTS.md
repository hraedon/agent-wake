# agent-wake — agent orientation

> **Upstream renamed 2026-05-27:** the coordination spine was previously `substrate`; it is now `regista` (Plan 001 consumer migration here, regista Plan 018 upstream). Older design docs and research notes under `design/` that still say "substrate" are intentional historical record.

## What this project is

External-to-session signaling layer for agent harnesses. See [README.md](README.md)
for the scope statement.

## What's been decided (2026-05-23)

- **Working name only.** "agent-wake" parallels agent-provenance,
  agent-notes. Rename if a better name emerges.
- **Two modes**: wake (turn-triggering) and silent inject (next-turn
  context). Same machinery, different flag.
- **Two initial harness targets**: Claude Code (via channel plugin) and
  opencode (via in-process plugin). Both confirmed feasible from docs
  review; channels API is more standardized but in research preview.
- **Regista is the preferred durable-ingest path**, not a hard
  dependency. HTTP-only ingest should work without regista for
  lightweight cases.
- **Out of scope**: pipeline orchestration, durable storage,
  action audit. Composes with regista / sf2 / agent-provenance,
  doesn't replicate.

## Research findings

### Claude Code: Channels (research preview, v2.1.80+)

- MCP server with `experimental: { 'claude/channel': {} }` capability
- Emits `notifications/claude/channel` with `{ content: string, meta:
  Record<string, string> }`
- Arrives in-context as `<channel source="..." attr="...">body</channel>`
- Launch: `--dangerously-load-development-channels server:NAME` for
  custom; `--channels plugin:NAME@MARKETPLACE` for allowlisted
- Auth: claude.ai or Console API key only — NOT Bedrock/Vertex/Foundry
- Notable: `claude/channel/permission` relay forwards tool-approval
  prompts with structured `request_id` → verdict pairs. Useful primitive
  for agent-provenance approval chains.
- **Confirmed during testing**: plain `notifications/message` from a
  regular MCP server does NOT wake Claude Code. Must use the channel
  protocol. This is why `/projects/wake-probe` tests showed FIRE in the
  log but no wake.
- Docs: https://code.claude.com/docs/en/channels and `/en/channels-reference`

### opencode: SDK session.prompt

- Plugins receive `client` SDK in context
- Call `client.session.prompt({ path: { id }, body: { noReply, parts }})`
- `noReply: false` (default) triggers an agent turn
- `noReply: true` injects silently into next-turn context
- Plugin can run long-lived background tasks (HTTP listener, websocket,
  file watcher) and call `session.prompt` from inside them
- Track live sessions via `session.created` / `session.deleted`
- In-process JS/TS — simpler than Claude Code's subprocess model
- Docs: https://opencode.ai/docs/plugins/ and SDK reference

## Open questions for next session

- Wake event schema: minimum portable shape is `{ event_id, source,
  kind, content, meta, wake }`. `event_id` added 2026-05-23 for
  idempotent retry (Stripe/GitHub webhook pattern). See
  `design/v0-implementation-plan.md` §4.
- Routing: how does an event find the right session(s)? Session ID is
  obvious for opencode; Claude Code channels are session-scoped at
  launch. Multi-session routing is a v1.5+ problem.
- ~~**Durable inbox / Signal-With-Start semantics.**~~ **DECIDED
  2026-07-24** (BC-WAKE-004 / BC-WAKE-012). The product split is
  resolved by making it *per-event*, not global: `live_only` (the
  default, i.e. the old "wake hits live sessions only" contract) vs
  `next_session` / `managed_session`, chosen with `meta.delivery` and
  defaultable per-daemon via `state.default_delivery`. The inbox is
  **daemon-local SQLite**, not regista — the daemon must not gain a
  hard dependency on a coordination service to answer "have I seen
  this event_id before?". Same store backs durable post-restart
  dedupe, the next-session queue and the dead-letter table. See
  README §Delivery semantics and `daemon/src/agent_waked/store.py`.
  Regista remains the *ingest* path, not the delivery-state store.
- ~~**Should delivery record an attributable event in regista?**~~
  **DECIDED 2026-07-31 (WI-006). No — not on the delivery path, and not
  as a store dependency at all for now.** The question is real: a wake
  event that causes an agent to act is provenance-relevant, and this
  file already says wake events are audit-worthy. But nothing in the
  estate supports a synchronous regista write here:
  - **There is no async ingest.** Every regista write is a synchronous,
    signed Postgres transaction — `_events_api.append_event`. No queue,
    no batch endpoint, no buffered path. The HTTP sidecar exists but has
    zero consumers, no client library, and its own plan calls v1
    "provisional and will be reshaped when consumer #2 lands."
  - **Two independently-filed high-severity bugs this month were exactly
    this mistake**, and both were fixed by *removing* store work from a
    polled path: agent-provenance WI-030 (`cairn doctor` replayed the
    production chain; 3m55s → 0.5s once split out) and dossier WI-034
    (`/healthz` called `gateway.integrity()` per project per request:
    102 MiB → 2.09 GiB after one probe, never released — the suite's own
    doctor was killing the service it checked). agent-wake's own
    `doctor._check_regista` already follows that rule.
  - **`append_event` needs a parent entity** (`WORK_ITEM_NOT_FOUND`
    otherwise), so a wake delivery cannot be a free-standing event; it
    would need a `session`-kind entity and a lifecycle to manage.
  - **An unattributable event is worse than none.** A signing key not
    registered for the project it signs for produces chains
    `regista bundle verify` rejects. And the estate has *three* live
    `principal_id` conventions (`human:x` prefixed, bare
    `alice@example.com`, and a separate `PrincipalKind` enum), with
    regista's only format validator — `_provision._validate_principal_id`
    — rejecting both of the string forms agent-wake and cairn actually
    use. agent-wake must not invent a fourth; the config layer therefore
    treats `principal_id` as **opaque** and validates only non-emptiness.
  - **It would make `test_suite_lock_declares_no_spine` a lie** and add a
    cross-repo version-pin obligation for a Tier 2 component taking a
    synchronous dependency on Tier 0 — which inverts
    "absent Tier 2 is ABSENT, not FAILED".

  This is the *same* answer the 2026-07-24 durable-inbox decision gave,
  for the same reason, so it is consistent rather than a second
  exception: **regista is the ingest path, not the delivery-state store,
  and now not the attestation path either.**

  What WI-006 did instead is make attestation *possible later without
  redoing it*: delivery now knows all three identities separately —
  `meta.trigger_identity` (who asked), `destination.principal` (whose
  attention was requested), `meta.actor_identity` (who is operating the
  harness) — which is exactly the tuple an attestation needs and which
  the fused `sources` entry could not produce. If the requirement
  becomes real, the shape is agent-notes'
  `core/outbox.py`: write to the SQLite store already on the path (a
  lock-guarded indexed insert, tens of microseconds) and drain to regista
  from a separate command, optional, absent-is-not-failure, with a
  `provenance_pending` counter in `doctor --json`. Filed rather than
  built, because the `principal_id` vocabulary is an unresolved
  prerequisite either way.
- Authentication / sender gating: required for any HTTP ingest. Same
  rationale as the channels docs — "an ungated channel is a prompt
  injection vector."
- Build vs. buy: is there enough overlap with channels to make agent-wake
  *only* an opencode adapter + a thin "use channels directly" doc for
  Claude Code? Or does it earn its keep as a unified surface?
- **MCP Triggers and Events WG alignment.** The MCP WG chartered in
  2025 to standardize server-to-client proactive notifications could
  produce a protocol the Claude Code adapter should align with. No
  spec as of 2026-05. Track passively; revisit when WG produces a
  draft. `design/research-findings-round2.md` §3.4.
- **Publishing the wake wire format as prior art.** No published
  primitive covers external-event-to-LLM-session delivery (workflow
  engines target durable workflows; webhooks need a listening URL;
  SSE/WebSocket assume client reconnect). A short spec / blog post
  on `core/schema.md` would establish prior art and invite review.
  Pair with the agent-provenance publication item.
- **Multi-user + identity** (joint with [[project-agent-provenance]]):
  three review rounds completed and consolidated into
  [design/v1-implementation-spec.md](design/v1-implementation-spec.md).
  Locked positions and regista change inventory ready to drive
  implementation. Regista-side gaps filed as BC-214 through BC-221.
  Round-by-round history preserved in `design/identity-and-multi-user.md`
  and the round-N review files in the same directory.

## Conventions

- Posture: single-person OSS, MIT, no commercial pivot pre-designed in.
  Matches agent-provenance.
- Will use regista breadcrumbs (via agent-notes) for defect /
  design tracking once there's code to break.
- Research-preview caveat for Claude Code channels: the `--channels` flag
  syntax and protocol contract may change. Adapter should isolate the
  unstable parts.

## Cross-references

- `/projects/regista` — coordination spine, hook queue is the durable
  ingest path
- `/projects/agent-provenance` — channel permission-relay primitive is
  useful here; wake events are themselves audit-worthy
- `/projects/wake-probe` — single-file Python testing instrument used to
  validate wake mechanisms during research
- Memory: [[reference-wake-and-hooks-across-harnesses]]
