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

## Suite integration

agent-wake is a **Tier 2** component of the agent suite — optional for a
first deployment, but the thing that lets external events (CI, monitors,
cross-session messages) drive agent work. When deployed as part of the
suite, it conforms to the shared config, doctor, and install-harness
contracts:

- **`agent-wake doctor --json`** — health check with the suite-shaped JSON
  output (`{component, version, ok, regista, checks}`)
- **`agent-wake install-harness <claude|opencode|codex|all>`** — wires adapters
  into harness configs idempotently (part of the suite bootstrap). `codex` is
  currently accepted but returns an explicit non-zero `unsupported` result;
  Plan 006 delivery work has not landed.
- **Suite config** — reads `REGISTA_*` env vars from `suite.env` with the
  standard precedence chain
- **Packaging** — systemd unit, Docker container, and Windows Service

## Quick start

For the shortest end-to-end path, follow [First wake in two minutes](docs/quickstart.md).

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
│                              │             │              │
│                              ▼             ▼              │
│                        durable store   unix-socket pub    │
│                        (SQLite)            │              │
│                          ▲   ▲             │              │
│  HTTPS reply ◄── outbox ─┘   └─ next-session queue        │
│                    ▲                                      │
│                    └── reply rx ◄─── (frames)             │
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

## Delivery semantics

By default a wake **hits live sessions only**: if no adapter is subscribed for
the event's source, the daemon reports `no_subscriber` and the event is not
retained. That is the v0 contract and it is unchanged.

An event can ask for more by setting `meta.delivery`:

| `meta.delivery`   | Behaviour when no session is live                          |
|-------------------|------------------------------------------------------------|
| `live_only`       | Default. Dropped, reported as `no_subscriber`.              |
| `next_session`    | Durably queued; delivered when the next session for that source subscribes. Ingest reports `queued_next_session`. |
| `managed_session` | Same queue. Reserved for Plan 006 Phase 2B (a daemon-owned harness process drains it). |

A daemon-wide default can be set with `state.default_delivery`; a per-event
`meta.delivery` always wins, so a deployment that opts everything in can still
mark individual events `live_only`.

Queued events are delivered at-least-once: the row is removed only when the
adapter acks. After `state.pending_max_attempts` unacked deliveries — or after
`state.pending_ttl_seconds` with no session at all — the event is
**dead-lettered** rather than dropped.

### Durable state

The daemon keeps dedupe, the next-session queue and the dead-letter table in a
single SQLite file (default `~/.local/state/agent-wake/state.db`, override with
`state.dir` or `AGENT_WAKE_STATE_DIR`). Consequences:

- A replayed `event_id` is rejected **across daemon restarts**, not just within
  one process lifetime.
- A reply whose callback permanently fails is dead-lettered and can be resent.
- A human-directed alert that never reached its principal is dead-lettered too
  (`kind=human_delivery`), including one cut short by a daemon restart, and can
  be re-dispatched with `dead-letter redrive`.
- The dead-letter table is bounded like the others. Entries hold full event
  bodies, so they expire after `dead_letter_ttl_seconds` and are capped at
  `dead_letter_max_rows`. Losing an entry nobody redrove is logged as a warning
  on **both** paths — expiry and cap eviction.
- When the cap bites, eviction ranks on operator value, not age: already-redriven
  entries first, then `next_session` (a re-queueable event that already sat
  unclaimed for its whole TTL), then `reply`, then `human_delivery` last. Keep
  `dead_letter_max_rows` above `pending_max_rows` — one `prune` promotes every
  expired queue entry into this table at once, so a smaller cap lets a queue
  expiry burst evict unrelated human alerts.
- Redriving a *partial* delivery only retries the channels that failed. The
  outbound webhook carries an `Idempotency-Key`, but email carries no
  idempotency token, so replaying everything would mean a second real email.

```jsonc
// config.json
"state": {
  "dir": "/var/lib/agent-wake",   // optional
  "enabled": true,                // false = in-memory dedupe, no queue
  "default_delivery": "live_only",
  "dedupe_ttl_seconds": 604800,
  "dedupe_max_rows": 100000,
  "pending_ttl_seconds": 604800,
  "pending_max_rows": 10000,
  "pending_max_attempts": 5,
  "dead_letter_ttl_seconds": 2592000,
  "dead_letter_max_rows": 20000       // keep above pending_max_rows
}
```

### Operator commands

```bash
agent-wake pending list                  # what is queued for the next session
agent-wake pending prune                 # apply retention now
agent-wake dead-letter list              # what failed permanently, and why
agent-wake dead-letter show <id>         # the full payload
agent-wake dead-letter list --kind human_delivery   # undelivered human alerts
agent-wake dead-letter redrive <id>      # resend it (replies, human alerts) /
                                         # requeue it (next-session events)
agent-wake dead-letter purge --older-than-days 30
```

All of these accept `--json` and emit the suite CLI contract v1 error envelope
on failure. `agent-wake doctor` reports a `durable_state` check and warns when
the dead-letter table is non-empty.

## Scope

- **In scope**: daemon (shared ingest port, routing, reply delivery), harness adapters, shared event schema, ingest mechanisms (HTTP, regista hook consumer), wake / silent-inject modes.
- **Out of scope (for now)**: pipeline orchestration (sf2's job), durable event storage (regista's job), action audit (agent-provenance's job). agent-wake composes with these, doesn't replicate them. The daemon's own SQLite state is delivery bookkeeping (dedupe window, next-session queue, dead-letter) with a bounded retention policy — it is deliberately *not* an event archive.

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

- **agent-notes** ships `agent-notes-bridge`, a small daemon that
  LISTENs on its Postgres `agent_notes_changes` channel and POSTs each
  breadcrumb / memory mutation to an agent-wake HTTP ingest endpoint
  (HMAC-signed, one v0 event per request, `kind=note-change`,
  `source=agent-notes`, `wake=false`). This is the first observed wake
  source intended to be attested through `agent-provenance`'s harness
  adapter once both components ship together (Plan 004 decision 60). See
  `/projects/agent-notes/README.md` for the bridge config.

## CI

The daemon and Claude adapter are tested on every push / PR via GitHub Actions (`.github/workflows/ci.yml`):
- **Python (daemon)**: `mypy src/` + `pytest`
- **Python (claude adapter)**: `mypy src/` + `pytest` + installability check
- **TypeScript (opencode adapter)**: `bun run build` + `bun test`

## License

MIT.
