# Plan 006 — Codex delivery feasibility and honest fallback

**Status:** Proposed 2026-07-10.
**Author:** GPT-5.6 Sol, from the suite Codex integration audit.
**Strategic role:** Determine what “wake Codex” can honestly mean, ship useful
next-session delivery where supported, and refuse to disguise polling or
process control as native live-turn injection.

## Ground truth

- Codex lifecycle hooks run because a session event is already occurring. They
  are observation/context hooks, not an external API for waking an idle local
  session.
- SessionStart may add developer context. This provides a legitimate
  deliver-on-next-start/resume path for queued signals.
- Stop may request another continuation, but only while a turn is stopping. It
  is not an external wake primitive and must not be abused as one.
- Codex App Server/SDK may support a daemon-managed session model, which is
  operationally different from attaching to an arbitrary existing CLI/IDE/app
  session.
- agent-wake must keep wake (turn-triggering) distinct from silent/next-turn
  delivery.

References:

- https://learn.chatgpt.com/docs/hooks
- https://learn.chatgpt.com/docs/customization/overview

## Decisions

1. Start with a time-boxed surface proof. Do not promise a live-session adapter
   until an official supported API can target an existing session.
2. Ship `next_session` delivery through SessionStart if the proof confirms
   safe daemon inbox access and context injection.
3. Label a daemon-managed App Server session as `managed_session`, not as
   attachment to an arbitrary Codex session.
4. Do not mutate transcripts, synthesize terminal input, scrape UI state, keep a
   turn artificially alive, or use periodic model prompts as a wake substitute.
5. Preserve authenticated ingress, durable dedup, target routing, provenance,
   and at-least-once semantics across any supported mode.
6. Unsupported live wake is a first-class doctor capability state, not an
   install success or component failure.

## Phase 0 — Feasibility gate

### WI-0.1 — Current surface matrix

Against the installed Codex release and official docs/source, test:

- targeting an existing idle CLI, IDE, and app session;
- externally starting a turn on a known session;
- adding context for the next start/resume;
- daemon-managed App Server/SDK sessions;
- session identity, authentication, acknowledgements, sandbox/network behavior,
  and Windows support.

**AC:** commit a dated matrix with command/API evidence. Each surface is
`supported`, `managed-only`, `next-session-only`, or `unsupported`.
Absence of evidence is unsupported.

### WI-0.2 — Architecture decision

Choose one of:

- **A:** native targeted live wake exists → implement Phase 2A.
- **B:** only managed sessions can be triggered → implement Phase 2B and name
  the operational boundary.
- **C:** neither exists → omit live wake and retain only Phase 1.

**AC:** the decision identifies the official contract, version floor, threat
model, and lifecycle owner. No private/unstable API enters the stable adapter.

## Phase 1 — Durable next-session delivery

### WI-1.1 — Codex inbox client

Add a small SessionStart command hook that authenticates to the local daemon,
claims queued events for the session/principal/project target, and returns
sanitized `additionalContext`.

**AC:**

- No event means silent success.
- A queued event appears once on startup/resume and is acked only after Codex
  accepts the hook result.
- Daemon unavailable does not block session start; it leaves a durable
  degradation/undelivered state.
- Size limits and untrusted-content framing prevent an event from silently
  becoming developer policy.
- Secrets never enter hook stdout.

### WI-1.2 — Routing and identity

Define how Codex session id, principal id, project, and optional label map to
existing wake targets.

**AC:** cross-principal delivery is rejected; broadcast requires explicit
policy; resume does not redeliver an acked event; expired sessions are cleaned
without losing queued events.

### WI-1.3 — Install and health

Implement `agent-wake install-harness codex` for the owned SessionStart hook
and manifest. Doctor reports next-session and live-wake capabilities separately.

**AC:**

- Dry-run/re-run/uninstall preserve unrelated Codex hooks.
- Hook trust/policy state is named.
- A system with next-session delivery but no live wake reports exactly that,
  not green “wake supported.”

## Phase 2A — Native live wake (conditional)

Implement only if WI-0.2 selects A.

### WI-2A.1 — Targeted adapter

Use the supported authenticated API to trigger or inject into the specified live
session, with ack/retry/dedup.

**AC:** two simultaneous sessions receive only their addressed events; an idle
session starts a turn; silent mode does not start one; unsupported client
surfaces fail explicitly.

## Phase 2B — Managed-session adapter (conditional)

Implement only if WI-0.2 selects B.

### WI-2B.1 — App Server/SDK ownership

Run Codex as an agent-wake-managed child/session and deliver through its
supported turn API.

**AC:**

- Docs say this mode does not attach to arbitrary user-started sessions.
- Process crash/restart, session resume, auth, sandbox, and backpressure are
  tested.
- The daemon cannot silently broaden Codex permissions.
- Operator opt-in is explicit because wake now owns process lifecycle.

## Phase 3 — Composition proof

### WI-3.1 — Provenance and suite proof

Exercise next-session delivery and whichever conditional live mode landed while
cairn Codex hooks are enabled.

**AC:** ingress, delivery/ack, Codex turn/session, and resulting tool calls share
correlatable event/session identifiers without duplicating secrets or content.

### WI-3.2 — Documentation

Document the capability matrix, mode names, install/trust steps, delivery
latency semantics, limitations, and local-vs-cloud scope.

## Stop condition

If Phase 0 selects C, this plan is complete after Phase 1 and documentation.
Revisit live wake only when an official supported external session-targeting
surface appears.

