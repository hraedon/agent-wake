# agent-wake

External-to-session signaling for agent harnesses. Push external events
(webhooks, alerts, scheduled triggers, cross-session messages) into a
running agent session — either to wake the agent for an immediate turn,
or to silently inject context for the next turn.

Working name. Status: scaffolding, no implementation yet.

## Why

Today, agent sessions are reactive only to the operator at the terminal.
There's no clean way for external systems (CI, monitors, chat platforms,
other agents) to push an event into a session and have the agent act on
it. Each harness exposes a different primitive for this — or none at all
— so any orchestration or async-operation workflow that touches agents
ends up reinventing the integration.

agent-wake is the harness-adapter layer: one event schema, one set of
ingest mechanisms, per-harness adapters that translate to the native
wake primitive.

## Scope

- **In scope**: harness adapters (Claude Code channel plugin, opencode
  plugin), shared event schema, ingest mechanisms (HTTP, substrate hook
  consumer), wake / silent-inject modes.
- **Out of scope (for now)**: pipeline orchestration (sf2's job),
  durable event storage (substrate's job), action audit (agent-provenance's
  job). agent-wake composes with these, doesn't replicate them.

## Architecture sketch

```
External system
      ↓ (event)
[ingest: HTTP listener | substrate hook | scheduled trigger]
      ↓
[router: which session(s)?]
      ↓
[harness adapter]
      ↓ (native wake primitive)
Running agent session
```

**Per-harness adapter:**
- Claude Code: channel plugin emitting `notifications/claude/channel`
  with `{ content, meta }`. Requires `--dangerously-load-development-channels`
  during the research preview.
- opencode: plugin calling `client.session.prompt({ noReply: false, parts })`.
  In-process, no allowlist friction.

**Composition with substrate** (preferred path for durable triggers):
External event → substrate (signed, replayable) → substrate hook → agent-wake
adapter → harness. Gets you a signed, replayable audit trail of every wake,
which is itself a provenance primitive.

## Related projects

- `/projects/substrate` — natural durable-ingest path. Hook queue already
  exists; agent-wake hook target translates events to harness wakes.
- `/projects/agent-provenance` — consumer. Channel permission-relay
  primitive is useful for approval chains; wake events themselves are
  audit-worthy.
- `/projects/software-factory-2` — consumer. External events re-entering
  a pipeline run.
- `/projects/wake-probe` — testing instrument. The single-file Python MCP
  server used to validate wake mechanisms during research.

## Status

- 2026-05-23: project created. Research findings in [AGENTS.md](AGENTS.md).
  No code yet.

## License

MIT (planned, matching agent-provenance posture).
