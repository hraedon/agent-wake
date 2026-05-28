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
| [`daemon/`](daemon/) | `agent-waked` — shared ingest daemon | Python 3.11+ |
| [`core/`](core/) | Shared wire format (docs + examples) | — |
| [`adapters/claude/`](adapters/claude/) | Claude Code channel plugin (MCP stdio) | Python 3.11+ |
| [`adapters/opencode/`](adapters/opencode/) | opencode plugin (daemon client) | TypeScript / Bun |
| [`tools/`](tools/) | `generate-secret.py`, `fakechat-test.py` | Python 3 |
| [`examples/`](examples/) | Real-world integration guides | — |

## Quick start

### 1. Install and start the daemon

```bash
pip install --user -e daemon/
```

Create `~/.config/agent-wake/config.json` with a minimal skeleton (one empty sources block is enough to start):

```json
{
  "version": 1,
  "listen": {"host": "127.0.0.1", "port": 8788},
  "sources": {},
  "routing": {}
}
```

Add a source with the CLI (generates and stores the secret for you):

```bash
agent-wake secrets add demo --backend env
# Prints the secret once — give it to whoever sends wake events.
```

Start the daemon (foreground for testing, or install as a systemd user service — see [`daemon/README.md`](daemon/README.md)):

```bash
agent-waked
```

> **Legacy note:** you can also set `"secret_env": "MY_ENV_VAR"` directly in config.json and export the var yourself — this still works and is not going away.

### 2. Install the adapter for your harness

**Claude Code:**
```bash
cd adapters/claude && pip install -e .
claude --dangerously-load-development-channels server:agent-wake-claude
```

**opencode:**
```bash
cd adapters/opencode && bun install && bun run build
# then register dist/index.js as a plugin in your opencode config
```

See adapter-specific docs: [`adapters/claude/README.md`](adapters/claude/README.md), [`adapters/opencode/README.md`](adapters/opencode/README.md)

### 3. Send a test event

```bash
bash adapters/claude/examples/demo.sh
```

The daemon receives the wake event via HTTP ingest and routes it to connected adapters, which deliver it to your running agent session.

## Architecture

```
External system
      │  POST / (HTTPS, HMAC-signed body, v0 wake event)
      ▼
┌───────────────────────────────────────────────────────────┐
│ agent-waked                                               │
│                                                           │
│  HTTP ingest ──► gating ──► dedupe ──► router             │
│                                            │              │
│                                            ▼              │
│                                  unix-socket pub          │
│                                            │              │
│  HTTPS reply ◄── outbox ◄── reply rx ◄─── (frames)       │
└───────────────────────────────────────────────────────────┘
                        │ (unix socket: $XDG_RUNTIME_DIR/agent-wake.sock)
                        ▼
┌───────────────────────────────────────────────────────────┐
│ adapter (e.g. agent-wake-claude)                          │
│                                                           │
│  daemon client (long-lived unix conn)                     │
│      │                                                    │
│      ├──► hello on connect                                │
│      ├──◄ wake frames ──► harness wake primitive          │
│      └──► reply frames (from agent_wake_reply tool)       │
└───────────────────────────────────────────────────────────┘
```

**Per-harness adapter:**
- Claude Code: channel plugin emitting `notifications/claude/channel` with `{ content, meta }`. Requires `--dangerously-load-development-channels server:agent-wake-claude` during the research preview. Connects to the daemon via unix socket.
- opencode: in-process plugin emitting `session.promptAsync` for each wake event. Connects to the daemon via unix socket. See `adapters/opencode.archived/` for the pre-daemon implementation (kept for historical reference).

## Scope

- **In scope**: daemon (shared ingest port, routing, reply delivery), harness adapters, shared event schema, ingest mechanisms (HTTP, regista hook consumer), wake / silent-inject modes.
- **Out of scope (for now)**: pipeline orchestration (sf2's job), durable event storage (regista's job), action audit (agent-provenance's job). agent-wake composes with these, doesn't replicate them.

## Real-world examples

- [GitHub Actions webhook → agent-wake](examples/github-actions-webhook.md) — trigger your agent when CI fails.
- [Regista webhook → agent-wake](#regista-integration) — wake your agent on regista workflow events.

## Regista integration

[Regista](/projects/regista) can push workflow events to agent-wake
via its webhook system. When a regista event matches your filter
(transitions, work_item_types, workflows), regista POSTs the event to
the daemon's HTTP ingest with an HMAC-SHA256 signature.

### Setup

1. Generate a shared secret (or reuse an existing one):

```bash
python3 tools/generate-secret.py
# export REGISTA_WEBHOOK_SECRET=<hex output>
```

2. Add a `regista` source to `~/.config/agent-wake/config.json`:

```json
{
  "version": 1,
  "listen": {"host": "127.0.0.1", "port": 8788},
  "sources": {
    "regista": {
      "secret_env": "REGISTA_WEBHOOK_SECRET",
      "callback_url": "http://127.0.0.1:9999/v1/reply"
    }
  },
  "routing": {
    "regista": {"adapter": "claude"}
  }
}
```

3. Register agent-wake as a regista webhook (via the sidecar API or
   Python SDK):

```python
from regista import Regista

sub = Regista("postgresql://...")
sub.register_webhook(
    url="http://127.0.0.1:8788/",
    headers={
        "X-AgentWake-Source": "regista",
    },
    sign_secret=b"<your-hex-secret>",
    transitions=["finish", "deploy", "alert"],
)
```

Regista will POST matching events to the daemon, which verifies the
HMAC signature and routes them to the configured adapter. Replies from
the agent are delivered back to regista's sidecar reply endpoint (if
`callback_url` is configured).

### Filtering

Webhook filters are AND'd across fields, OR'd within each field:

```python
sub.register_webhook(
    url="http://127.0.0.1:8788/",
    headers={"X-AgentWake-Source": "regista"},
    sign_secret=secret,
    transitions=["finish"],              # only these transitions
    work_item_types=["feature", "bug"],  # AND these work-item types
    workflows=["deploy-v2"],             # AND these workflows
)
```

## Related projects

- `/projects/regista` — natural durable-ingest path. Hook queue already exists; agent-wake hook target translates events to harness wakes.
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

The daemon and Claude adapter are tested on every push / PR via GitHub Actions (`.github/workflows/ci.yml`):
- **Python (daemon)**: `mypy src/` + `pytest`
- **Python (claude adapter)**: `mypy src/` + `pytest` + installability check
- **TypeScript (opencode adapter)**: `bun run build` + `bun test`

## License

MIT.
