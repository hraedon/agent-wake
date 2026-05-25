# agent-wake

External-to-session signaling for agent harnesses. Push external events
(webhooks, alerts, scheduled triggers, cross-session messages) into a
running agent session — either to wake the agent for an immediate turn,
or to silently inject context for the next turn.

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

## What's here

| Component | Status | Language |
|---|---|---|
| [`core/`](core/) | Shared wire format (docs + examples) | — |
| [`adapters/claude/`](adapters/claude/) | Claude Code channel plugin (MCP stdio) | Python 3.11+ |
| [`adapters/opencode/`](adapters/opencode/) | opencode plugin (in-process) | TypeScript / Bun |
| [`tools/`](tools/) | `generate-secret.py`, `fakechat-test.py` | Python 3 |
| [`examples/`](examples/) | Real-world integration guides | — |

## Quick start

1. Generate a shared HMAC secret:
   ```bash
   python tools/generate-secret.py
   ```
   Export it: `export AGENT_WAKE_DEMO_SECRET=<output>`

2. Write a config at `~/.config/agent-wake/config.json` (see [`core/schema.md`](core/schema.md) §Configuration or [`core/examples/config.json`](core/examples/config.json)).

3. Install and run the adapter for your harness:

   **Claude Code:**
   ```bash
   cd adapters/claude && pip install -e .
   claude --dangerously-load-development-channels server:agent-wake-claude
   ```

   **opencode:**
   ```bash
   cd adapters/opencode && bun install && bun run build
   # Then add the plugin path to ~/.config/opencode/opencode.json:
   #   "plugin": ["/path/to/agent-wake/adapters/opencode/dist/index.js"]
   ```

   See adapter-specific docs: [`adapters/claude/README.md`](adapters/claude/README.md) · [`adapters/opencode/README.md`](adapters/opencode/README.md)

4. Send a test event:
   ```bash
   bash adapters/claude/examples/demo.sh
   ```

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
- Claude Code: channel plugin emitting `notifications/claude/channel` with `{ content, meta }`. Requires `--dangerously-load-development-channels server:agent-wake-claude` during the research preview.
- opencode: plugin calling `client.session.prompt({ noReply: false, parts })`. In-process, no allowlist friction.

## Scope

- **In scope**: harness adapters (Claude Code channel plugin, opencode plugin), shared event schema, ingest mechanisms (HTTP, substrate hook consumer), wake / silent-inject modes.
- **Out of scope (for now)**: pipeline orchestration (sf2's job), durable event storage (substrate's job), action audit (agent-provenance's job). agent-wake composes with these, doesn't replicate them.

## Real-world examples

- [GitHub Actions webhook → agent-wake](examples/github-actions-webhook.md) — trigger your agent when CI fails.

## Related projects

- `/projects/substrate` — natural durable-ingest path. Hook queue already exists; agent-wake hook target translates events to harness wakes.
- `/projects/agent-provenance` — consumer. Channel permission-relay primitive is useful for approval chains; wake events themselves are audit-worthy.
- `/projects/software-factory-2` — consumer. External events re-entering a pipeline run.
- `/projects/wake-probe` — testing instrument. The single-file Python MCP server used to validate wake mechanisms during research (deprecated; see `adapters/claude/` for the production plugin).

## Consumers

- **agent-notes-mcp** ships `agent-notes-bridge`, a small daemon that
  LISTENs on its Postgres `agent_notes_changes` channel and POSTs each
  breadcrumb / memory mutation to an agent-wake HTTP ingest endpoint
  (HMAC-signed, one v0 event per request, `kind=note-change`,
  `source=agent-notes`). This is the first observed wake source intended
  to be attested through `agent-provenance`'s harness adapter once both
  components ship together (Plan 004 decision 60). See
  `/projects/agent-notes-mcp/README.md` for the bridge config.

## CI

Both adapters are tested on every push / PR via GitHub Actions (`.github/workflows/ci.yml`):
- **Python**: `pytest` + `fakechat-test.py` end-to-end
- **TypeScript**: `bun test` + `tsc --noEmit`

## License

MIT.
