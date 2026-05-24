# agent-wake v0 implementation plan

**Audience:** Sonnet-class implementer. This plan is concrete enough
that you should not need to make architectural decisions. Where a
decision is genuinely open, the plan flags it explicitly. Otherwise,
follow the spec.

**Goal:** Ship a functional, dogfoodable agent-wake v0 that delivers
wake-on-event for Claude Code and opencode. No signing, no identity,
no multi-user. That comes later (see
[v1-implementation-spec.md](v1-implementation-spec.md)).

**Time estimate:** 2–3 weeks of focused implementation.

---

## 1. Mandatory reading before you start

Read these in order. Do not skip.

1. `/projects/agent-wake/README.md` — project scope and posture
2. `/projects/agent-wake/AGENTS.md` — research findings on
   harness-specific wake mechanisms
3. `/projects/agent-wake/design/v1-implementation-spec.md` — the
   v1 spec. You are building v0, but v0 must not preclude any v1
   decision. Pay attention to where v1 needs schema hooks.
4. The Claude Code channels reference:
   `https://code.claude.com/docs/en/channels-reference` — read the
   full webhook-receiver walkthrough and the permission-relay
   section.
5. `https://opencode.ai/docs/plugins/` — opencode plugin overview.
6. `/projects/wake-probe/server.py` — the existing prototype. You
   will refactor this into the Claude Code adapter; preserve the
   logging discipline and HTTP shape where they make sense.

You may also find the round-2 and round-3 review docs in
`/projects/agent-wake/design/` useful for context, but they are not
load-bearing for v0 (they focus on the v1 identity questions).

---

## 2. Scope

### In scope for v0

- Claude Code channel plugin (Python, MCP stdio)
- opencode plugin (TypeScript, in-process)
- Shared wire schema for wake events (a docs spec + minimal helpers)
- Local HTTP webhook receiver as part of each adapter
- Per-deployment shared-secret HMAC gating on inbound webhooks
- Permission relay (Claude Code only — opencode doesn't have an
  equivalent primitive yet)
- Reply tool for two-way channels (both adapters)
- Tests for each adapter, including a fakechat-style local demo

### Out of scope for v0

- Identity / signing / `principal_id` (waits for substrate BC-216+)
- Multi-user routing (one operator per adapter instance)
- Cross-org delegation
- Persistent state — adapters are stateless; configuration lives in
  a local file, secrets live in env or local file
- Bundle export, audit verifier, provenance integration
- sf2 adoption (separate, later work item)
- DNS-based bootstrap, OIDC, DIDs — none of v1's identity machinery
- Telegram / Discord / iMessage / SMS integrations — those are
  separate channel plugins in `claude-plugins-official`; v0 ships
  the webhook ingest pattern and lets the user wire other
  transports themselves
- Anything that requires changes to substrate

### Defer with explicit hooks

- Where v1 will need a `principal_id` field, leave the wire format
  open: every wake event carries an optional `meta` dict that can
  hold future identity fields without a schema migration.
- Where v1 will sign events, do not add `signature` fields yet, but
  do not introduce field names that would conflict with future
  additions (no field called `signature`, `key_id`, etc.).

---

## 3. Repository structure

Create a single repository at `/projects/agent-wake/` with this
top-level layout:

```
agent-wake/
├── README.md                    # already exists
├── AGENTS.md                    # already exists
├── design/                      # already exists — do not modify
├── core/                        # NEW — shared wire format spec + helpers
│   ├── README.md
│   ├── schema.md                # the canonical wire spec
│   └── examples/                # sample event payloads
├── adapters/
│   ├── claude/                  # NEW — Python channel plugin
│   │   ├── README.md
│   │   ├── pyproject.toml
│   │   ├── src/
│   │   │   └── agent_wake_claude/
│   │   │       ├── __init__.py
│   │   │       ├── __main__.py
│   │   │       ├── server.py         # MCP server entry
│   │   │       ├── channel.py        # channel notification emission
│   │   │       ├── ingest.py         # HTTP listener
│   │   │       ├── reply.py          # reply tool implementation
│   │   │       ├── permission.py     # permission relay (claude-specific)
│   │   │       ├── gating.py         # HMAC sender check
│   │   │       └── config.py         # config loading
│   │   ├── tests/
│   │   └── examples/
│   │       └── demo.sh               # end-to-end curl demo
│   └── opencode/                # NEW — TypeScript plugin
│       ├── README.md
│       ├── package.json
│       ├── src/
│       │   ├── index.ts              # plugin entry
│       │   ├── ingest.ts             # HTTP listener
│       │   ├── wake.ts               # session.prompt translation
│       │   ├── reply.ts              # outbound to webhook callback
│       │   ├── gating.ts             # HMAC sender check
│       │   └── config.ts             # config loading
│       ├── tests/
│       └── examples/
│           └── demo.sh
└── tools/                       # NEW — operational helpers
    ├── fakechat-test.py         # local demo for end-to-end testing
    └── generate-secret.py       # CLI to create the shared HMAC secret
```

Do not create directories beyond this structure. Resist the urge to
add `lib/`, `internal/`, `models/`, etc. The flat structure is
intentional.

The Python adapter uses standard `pyproject.toml` (build with
`uv` or `pip`). The opencode adapter uses `package.json` (Bun-compatible
per the channels docs, but `npm` also works).

---

## 4. The wire format (canonical)

Write this as `core/schema.md`. Both adapters MUST emit and accept
this shape.

### Wake event (external → adapter → harness)

```json
{
  "v": 0,
  "event_id": "string — sender-generated opaque id, unique per logical event",
  "source": "string — opaque identifier of the event source",
  "kind": "string — event class, e.g. 'webhook', 'alert', 'message'",
  "content": "string — human-readable body delivered to the agent",
  "meta": {
    "key": "value"
  },
  "wake": true
}
```

- `v` is the schema version. v0 events use `0`. Future schemas may
  add fields; consumers must reject unknown `v` values fail-shut.
- `event_id` is a sender-generated identifier (recommended: ULID or
  UUIDv7). Used for idempotent retry: the adapter MAY deduplicate
  recently-seen `event_id` values within a bounded window. Required
  in v0 — wrapped requests (arbitrary JSON bodies, see §HTTP ingest)
  receive a server-generated `event_id` if none is supplied via the
  `X-AgentWake-Event-Id` header.
- `source` is free-form, used by the agent to identify what sent the
  event (e.g., `"github-actions"`, `"datadog"`, `"telegram"`).
- `kind` is also free-form but standardized to a small set:
  `webhook`, `alert`, `message`, `approval_request`, `approval_verdict`.
- `content` is what the agent reads in its context window.
- `meta` is `Record<string, string>`. Identifier keys only
  (letters, digits, underscores). Hyphens and other characters are
  silently stripped by the Claude Code channel layer (per the
  channels-reference spec).
- `wake: true` triggers an agent turn. `wake: false` injects context
  silently (useful for "FYI, this happened" events that don't need
  immediate response).

### Retry and idempotency semantics

The HTTP ingest layer is modeled on the Stripe / GitHub webhook
pattern. Senders SHOULD treat the ingest endpoint as best-effort and
retry with exponential backoff on non-2xx responses or connection
failures (suggested: 3 retries over 60 seconds, then surface the
failure to the operator).

The adapter SHOULD deduplicate by `event_id` within a recent-events
window (suggested: 256 most-recent ids in memory; no persistence in
v0). A duplicate `event_id` returns 202 with `{"status": "duplicate"}`
and does NOT wake the agent a second time. This makes the ingest
endpoint safe for at-least-once delivery.

The dedupe window is intentionally in-memory only for v0. After an
adapter restart, the window is empty and a retry-after-restart MAY
result in a duplicate wake. Document this; durable dedupe is a v1
question tied to the same substrate-inbox decision as missed-event
queueing (see §Open questions in `AGENTS.md`).

*Prior art:* Stripe `Idempotency-Key` headers, GitHub webhook
redelivery IDs. See `design/research-findings-round2.md` §3.1.

### Reply event (agent → adapter → external)

```json
{
  "v": 0,
  "in_reply_to": "string — opaque ID of the event being replied to",
  "content": "string",
  "meta": {}
}
```

The adapter delivers the reply by POSTing to a configured callback
URL (per-source or default).

**Delivery loss when the session exits.** Replies are produced by the
agent during a session turn. If the session exits or the harness
process dies before the reply tool fires, the reply is lost — agent-
wake does not maintain a durable outbox in v0. The sender must treat
the absence of a reply as inconclusive (the agent may have acted but
crashed before replying) and reconcile via its own state.

This is a known v0 limitation, not a bug. A durable per-session
inbox/outbox (Centrifugo-style history recovery, or Temporal
Signal-With-Start semantics for the wake direction) is a v1+ design
question deferred to substrate (see `AGENTS.md` open questions).
Document the loss mode in any sender integration that depends on the
reply.

*Prior art:* Centrifugo history & stream recovery, Temporal
Signal-With-Start. See `design/research-findings-round2.md` §3.2–3.3.

### HTTP ingest

The adapter exposes a local HTTP listener (default `127.0.0.1:8788`).
External systems POST wake events to it:

```
POST / HTTP/1.1
Host: 127.0.0.1:8788
Content-Type: application/json
X-AgentWake-Source: github-actions
X-AgentWake-Signature: sha256=<hex hmac of body>

{ "v": 0, "kind": "alert", "content": "build failed on main", ... }
```

- `X-AgentWake-Source` is required.
- `X-AgentWake-Signature` is required (HMAC-SHA256 of the raw body
  using the shared secret).
- `X-AgentWake-Event-Id` is optional. If present, populates
  `event_id` on the wrapped event (when the body is not already a v0
  event). If absent, the adapter generates a `ULID`-shaped id. If the
  body is already a v0 event, the body's `event_id` wins.
- Missing or invalid signature → 403 Forbidden. No leakage of
  whether the source is configured.
- Successful ingest → 202 Accepted, body `{"status": "queued",
  "event_id": "<the id used>"}`.
- Duplicate `event_id` within the dedupe window → 202 Accepted, body
  `{"status": "duplicate", "event_id": "<id>"}`. No wake is emitted.

If the body is a full wake event object (matching the schema), the
adapter forwards it as-is. If the body is arbitrary JSON (e.g., a
GitHub webhook payload), the adapter wraps it:

```json
{
  "v": 0,
  "event_id": "<X-AgentWake-Event-Id header, or generated ULID>",
  "source": "<X-AgentWake-Source value>",
  "kind": "webhook",
  "content": "<JSON-stringified body>",
  "meta": {},
  "wake": true
}
```

The adapter does not interpret the body. The agent reads it and
decides what to do.

### Configuration file

Located at `~/.config/agent-wake/config.json` (override via
`AGENT_WAKE_CONFIG` env var).

```json
{
  "version": 0,
  "listen": {
    "host": "127.0.0.1",
    "port": 8788
  },
  "sources": {
    "github-actions": {
      "secret_env": "AGENT_WAKE_GITHUB_SECRET",
      "callback_url": null
    },
    "telegram-bot": {
      "secret_env": "AGENT_WAKE_TELEGRAM_SECRET",
      "callback_url": "https://api.telegram.org/bot.../sendMessage"
    }
  },
  "default_callback_url": null
}
```

- Each source has its own HMAC secret (read from an env var; never
  written to the config file).
- `callback_url` is optional. If present, replies POST there. If
  absent, replies fail gracefully (log a warning, return "sent" to
  Claude — replies are best-effort in v0).

---

## 5. Component spec: Claude Code adapter

### Package: `agent_wake_claude`

Python 3.11+. Use the official MCP Python SDK
(`modelcontextprotocol` on PyPI). Do not hand-roll JSON-RPC — the
wake-probe approach was for diagnostics; the real adapter uses the
SDK.

### Entry point: `src/agent_wake_claude/__main__.py`

Reads config, starts the MCP server on stdio, starts the HTTP
listener in a background thread, waits.

### `server.py` — MCP server

Declares capabilities:

```python
Server(
    name="agent-wake",
    version="0.0.1",
    capabilities={
        "experimental": {
            "claude/channel": {},
            "claude/channel/permission": {},  # for permission relay
        },
        "tools": {},  # for the reply tool
    },
    instructions=(
        "Events arrive as <channel source=\"agent-wake\" ...>. "
        "The event's source is in the 'source' attribute and the "
        "kind is in the 'kind' attribute. Reply with the reply tool, "
        "passing the source from the tag."
    ),
)
```

Registers the reply tool (see `reply.py`).

Registers the permission-request notification handler (see
`permission.py`).

### `ingest.py` — HTTP listener

Use `http.server` from stdlib for v0 — no external HTTP framework.
Single-threaded is fine; the agent processes events in batches anyway.

The listener:
1. Accepts POST requests on `/`.
2. Reads the body.
3. Looks up the `X-AgentWake-Source` header in the configured sources.
4. Verifies HMAC against the source's secret. Reject on mismatch.
5. Constructs a wake event (passes through if the body is already a
   v0 event; wraps if it's arbitrary JSON).
6. Calls `channel.emit_wake_event(event)` to forward to the MCP layer.
7. Returns 202.

### `channel.py` — emit wake events to the channel

Exposes `emit_wake_event(event: dict) -> None`. Translates the wake
event into the channel notification format:

```python
mcp.notification({
    "method": "notifications/claude/channel",
    "params": {
        "content": event["content"],
        "meta": {
            "source": event["source"],
            "kind": event["kind"],
            # meta keys from the wake event, filtered to identifier-safe names
            **{k: v for k, v in event.get("meta", {}).items() if k.isidentifier()},
        },
    },
})
```

If `event["wake"] is False`, do not emit. (v0: silent inject is not
supported in Claude Code channels — `notifications/claude/channel`
always wakes. Document this as a known v0 limitation; opencode
supports silent inject via `noReply: true`.)

### `reply.py` — reply tool

Standard MCP tool registration. Input schema:

```json
{
  "type": "object",
  "properties": {
    "source": {"type": "string"},
    "content": {"type": "string"},
    "in_reply_to": {"type": "string"}
  },
  "required": ["source", "content"]
}
```

Implementation: looks up the source's `callback_url` in config.
POSTs the reply to that URL. Returns `{"content": [{"type": "text",
"text": "sent"}]}` on success, error otherwise.

### `permission.py` — permission relay

Handles `notifications/claude/channel/permission_request`. Forwards
the prompt to the configured callback URL (per the channels-reference
spec). Accepts verdicts back via a dedicated path in the HTTP
listener.

For v0, the verdict mechanism is simple: a POST to
`/permission/verdict` with body `{"request_id": "abcde", "behavior":
"allow" | "deny"}`. The adapter emits `notifications/claude/channel/permission`
back to Claude Code.

The five-letter `request_id` format is defined by Claude Code; just
echo what was sent.

### `gating.py` — HMAC verification

Standard `hmac.compare_digest` against the raw body bytes. Reject if
the source isn't configured, the signature is missing, or the
signature doesn't match.

Constant-time comparison is mandatory. Do not use `==`.

### `config.py` — configuration loading

Read JSON from `AGENT_WAKE_CONFIG` or `~/.config/agent-wake/config.json`.
Validate the schema. Resolve `secret_env` references to actual
secrets via `os.environ`. Reject configs that reference unset env
vars.

### `tools/generate-secret.py`

Stdlib script:

```python
import secrets
print(secrets.token_hex(32))
```

Run by the user once during setup; they put the output in their env
var.

---

## 6. Component spec: opencode adapter

### Package: `agent-wake-opencode`

TypeScript, Bun runtime (per opencode plugin convention). Plugin
exports a default function that returns hooks.

### Entry point: `src/index.ts`

```typescript
export default async function plugin(ctx: PluginContext): Promise<Hooks> {
  const config = loadConfig();
  const ingest = startIngest(ctx, config);

  return {
    "session.created": (event) => trackSession(event),
    "session.deleted": (event) => untrackSession(event),
    // (no other hooks needed for v0)
  };
}
```

### `ingest.ts` — HTTP listener

Use Bun's built-in `Bun.serve()`. Same shape as the Claude adapter's
HTTP listener.

On valid wake event, call `client.session.prompt()` for the active
session (or each active session, if the wake event doesn't target a
specific one):

```typescript
await ctx.client.session.prompt({
  path: { id: sessionId },
  body: {
    noReply: !event.wake,
    parts: [
      { type: "text", text: formatWakeEvent(event) },
    ],
  },
});
```

`formatWakeEvent` produces a string that mirrors the
`<channel>`-tagged shape Claude Code uses, so prompts can be written
once for both harnesses:

```
<wake source="github-actions" kind="webhook">
build failed on main
</wake>
```

### Session tracking

`session.created` event: store the session id in a Set.
`session.deleted`: remove it.

For v0, wake events target *all* active sessions by default. A
future addition (when identity lands) will target specific sessions
by id.

### `reply.ts`, `gating.ts`, `config.ts`

Same shape as the Claude adapter. opencode doesn't have a reply
*tool* per se; instead, the plugin exposes a custom tool that the
agent can call to reply. Use the opencode `tool()` helper from the
plugin SDK.

### opencode-specific: permission relay

opencode's plugin events include `permission.asked` and
`permission.replied`. v0 does NOT implement opencode permission
relay (it adds complexity without much value; the user can permit
all opencode tools and skip the prompt). Document as a v1 extension.

---

## 7. Component spec: `core/`

This is a small directory with:

- `schema.md` — the canonical wire format from §4. The authoritative
  source. Both adapters must conform.
- `examples/` — JSON files with example wake events, reply events,
  and permission requests/verdicts. Use these as test fixtures in
  both adapters.

No code in `core/` for v0. If a shared library emerges as both
adapters grow, that's a v0.5 or v1 task.

---

## 8. Testing strategy

### Unit tests (per adapter)

- HMAC gating accepts valid signatures, rejects everything else.
- Wake event wrapping produces a valid v0 event for arbitrary JSON
  bodies.
- Config loader rejects malformed configs and missing env vars.
- Reply tool resolves the correct callback URL per source.

### Integration tests (per adapter)

For the Claude Code adapter:
- Launch the adapter as a subprocess with stdio mocked.
- POST a wake event to the HTTP listener.
- Verify the MCP notification is emitted with the correct shape.

For the opencode adapter:
- Mock the opencode `client` and the `session.created` event.
- POST a wake event.
- Verify `client.session.prompt()` was called with the correct
  arguments.

### End-to-end demo

`tools/fakechat-test.py` — a Python script that:
1. Starts the Claude Code adapter via `claude
   --dangerously-load-development-channels server:agent-wake`.
2. POSTs a sample wake event to the listener.
3. Confirms the agent receives the event.

This is for hand-testing during development, not CI.

`adapters/claude/examples/demo.sh`:

```bash
#!/bin/bash
# Generate a secret, configure, and send a test wake event.
export AGENT_WAKE_DEMO_SECRET=$(python tools/generate-secret.py)
# ... write config ...
# (assumes Claude Code is running with --channels server:agent-wake)
BODY='{"v": 0, "event_id": "01HZXPDEMO0000000000000001", "source": "demo", "kind": "alert", "content": "hello from agent-wake", "wake": true, "meta": {}}'
SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$AGENT_WAKE_DEMO_SECRET" | awk '{print $2}')
curl -X POST http://127.0.0.1:8788/ \
  -H "Content-Type: application/json" \
  -H "X-AgentWake-Source: demo" \
  -H "X-AgentWake-Signature: sha256=$SIG" \
  -d "$BODY"
```

`adapters/opencode/examples/demo.sh` — same shape.

---

## 9. Acceptance criteria for v0

The implementation is "done" when:

1. `agent-wake-claude` is installable as a Claude Code plugin and
   wakes a running session on a curl POST to `localhost:8788`.
2. The wake event arrives in the agent's context as a `<channel>`
   tag with the correct `source` and `kind` attributes.
3. The agent can reply via the reply tool, and the reply is POSTed
   to the configured callback URL (verified in a test with a local
   echo server).
4. Permission relay round-trip works: a `Bash` permission prompt
   from Claude Code is forwarded to the callback URL, and a verdict
   POST back closes the prompt.
5. `agent-wake-opencode` is installable as an opencode plugin and
   wakes the active session on a curl POST to the same shape of
   endpoint.
6. HMAC gating rejects unsigned, mis-signed, and unknown-source
   requests with 403.
7. Both adapters have unit tests covering the wire format, gating
   logic, and `event_id` deduplication within the recent-events
   window (duplicate id → 202 with `status: "duplicate"`, no wake).
8. The fakechat-style demo (`tools/fakechat-test.py`) runs end-to-end
   without errors against a real Claude Code session.
9. The README in each adapter directory documents setup, config,
   and a curl-based first-event test.
10. Neither adapter modifies any file outside its own directory.
    No substrate changes, no agent-provenance changes.

---

## 10. Things that will tempt you and should be resisted

- **Adding identity / signing now.** You don't have the substrate
  primitives. Stub the wire format to allow future fields; do not
  add them.
- **Adding multi-source routing.** v0 has one operator per adapter.
  The HTTP listener forwards to the active session(s); there is no
  "user A's events go to user A's session" routing.
- **Sharing code between the Python and TypeScript adapters.** They
  are separate languages with different ecosystems. The shared
  artifact is the wire spec, not a runtime library. Do not create
  a polyglot codegen system or a shared OpenAPI schema.
- **Building a UI.** No web dashboard, no TUI. The CLI demo scripts
  are sufficient for v0.
- **Adding observability (Prometheus, structured logging, tracing).**
  v0 logs to stderr in a human-readable format. Add structured
  logging in v0.5 if needed.
- **Wiring sf2 in.** That's a separate work item per the prior
  conversation. v0 is the standalone wake primitive; sf2 adoption
  comes after the user has dogfooded v0.
- **Implementing channels for Telegram / Discord / iMessage.** Those
  exist as separate plugins in `claude-plugins-official`. The user
  can wire them up to agent-wake via the webhook interface if they
  want.
- **Refactoring `wake-probe/server.py` in place.** Treat it as
  reference material. Build the new adapter cleanly in
  `adapters/claude/`. Once the new adapter is working, deprecate
  wake-probe by adding a note to its file pointing at the new code.

---

## 11. Order of work

Suggested implementation order, optimized for getting to a usable
end-to-end demo as fast as possible.

**Week 1: Claude Code adapter to working demo**

1. Scaffold `adapters/claude/` (pyproject.toml, package skeleton,
   tests directory).
2. Implement `config.py` and `gating.py`. Add unit tests for both.
3. Implement `ingest.py` with a minimal POST handler that just logs
   the wake event.
4. Add the MCP server skeleton in `server.py` with the channel
   capability declared.
5. Wire `ingest.py` to call `channel.emit_wake_event()` which calls
   `mcp.notification()`.
6. Hand-test: launch Claude Code with
   `--dangerously-load-development-channels server:agent-wake`,
   curl a wake event, verify the channel tag appears in the
   session.
7. Add the reply tool (`reply.py`).
8. Add permission relay (`permission.py`) with the verdict path.
9. Polish: README, demo script, error handling.

**Week 2: opencode adapter to working demo**

10. Scaffold `adapters/opencode/` (package.json, plugin skeleton).
11. Implement the equivalent ingest/wake/gating/config files in
    TypeScript.
12. Wire `client.session.prompt()` for wake.
13. Hand-test: install the plugin in opencode, curl a wake event,
    verify the agent responds.
14. Polish: README, demo script.

**Week 3: Hardening and dogfood**

15. Run both adapters in the user's daily workflow for a week.
    Capture rough edges, fix them.
16. Write the `core/schema.md` doc capturing what actually shipped
    (the schema in §4 of this plan is the design intent; the doc
    should reflect implementation reality).
17. Add the `tools/fakechat-test.py` end-to-end harness.
18. Tag v0.0.1.

---

## 12. Where to put questions

If you hit a question this plan doesn't answer:

- **Implementation-detail questions** (which HTTP library, how to
  structure a specific function) — make a reasonable choice and
  document it in a code comment. Do not block.
- **Behavior questions** (what should happen when X is misconfigured)
  — default to "fail loudly with a clear error message." Do not
  default to silent fallback.
- **Schema questions** — fall back to §4. If §4 doesn't cover the
  case, default to the smallest possible extension.
- **Scope questions** (does feature X belong in v0?) — default to
  no. v0 is intentionally small.
- **Anything design-level** — stop, document the question in
  `design/v0-open-questions.md`, and surface it to the human. Do
  not invent design.

---

## 13. Deliverables checklist

When you submit the work, the human will check:

- [ ] `adapters/claude/` exists and the package is installable
- [ ] `adapters/opencode/` exists and the package is installable
- [ ] `core/schema.md` documents the wire format
- [ ] Each adapter has a README with setup and demo instructions
- [ ] Each adapter has unit tests
- [ ] `tools/fakechat-test.py` runs end-to-end
- [ ] The 10 acceptance criteria in §9 are met
- [ ] No files outside `agent-wake/` are modified
- [ ] No design documents in `design/` are modified
- [ ] The wake-probe is annotated as deprecated, not deleted

Good luck. Build it small, build it real, ship it.
