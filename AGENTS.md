# agent-wake — agent orientation

## What this project is

External-to-session signaling layer for agent harnesses. See [README.md](README.md)
for the scope statement.

## What's been decided (2026-05-23)

- **Working name only.** "agent-wake" parallels agent-provenance,
  agent-notes-mcp. Rename if a better name emerges.
- **Two modes**: wake (turn-triggering) and silent inject (next-turn
  context). Same machinery, different flag.
- **Two initial harness targets**: Claude Code (via channel plugin) and
  opencode (via in-process plugin). Both confirmed feasible from docs
  review; channels API is more standardized but in research preview.
- **Substrate is the preferred durable-ingest path**, not a hard
  dependency. HTTP-only ingest should work without substrate for
  lightweight cases.
- **Out of scope**: pipeline orchestration, durable storage,
  action audit. Composes with substrate / sf2 / agent-provenance,
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
- **Durable inbox / Signal-With-Start semantics.** Today wake hits
  live sessions only; if no session is active, the event is lost (or
  for opencode, only delivered if a session exists at ingest time).
  Temporal's Signal-With-Start is the closest functional analog:
  lazy-init a workflow if none is running, then deliver the signal.
  Open product decision: does agent-wake want substrate to maintain
  a per-session inbox so missed events are delivered on next session
  start? This is the same decision as durable post-restart dedupe
  for `event_id`. Punt to v1 with substrate's involvement, but make
  the call explicitly — "wake hits live sessions only" vs "wake has
  durable delivery semantics" is a real product split.
  See `design/research-findings-round2.md` §3.2.
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
  Locked positions and substrate change inventory ready to drive
  implementation. Substrate-side gaps filed as BC-214 through BC-221.
  Round-by-round history preserved in `design/identity-and-multi-user.md`
  and the round-N review files in the same directory.

## Conventions

- Posture: single-person OSS, MIT, no commercial pivot pre-designed in.
  Matches agent-provenance.
- Will use substrate breadcrumbs (via agent-notes-mcp) for defect /
  design tracking once there's code to break.
- Research-preview caveat for Claude Code channels: the `--channels` flag
  syntax and protocol contract may change. Adapter should isolate the
  unstable parts.

## Cross-references

- `/projects/substrate` — coordination spine, hook queue is the durable
  ingest path
- `/projects/agent-provenance` — channel permission-relay primitive is
  useful here; wake events are themselves audit-worthy
- `/projects/wake-probe` — single-file Python testing instrument used to
  validate wake mechanisms during research
- Memory: [[reference-wake-and-hooks-across-harnesses]]
