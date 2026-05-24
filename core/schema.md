# agent-wake wire format — v0

**Status:** canonical. Both adapters MUST accept and emit this shape.
**Version:** 0

---

## Wake event (external → adapter → harness)

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

### Fields

- **`v`** — Schema version. v0 events use `0`. Future schemas may add fields;
  consumers MUST reject unknown `v` values fail-shut.
- **`event_id`** — Sender-generated identifier (recommended: ULID or UUIDv7).
  Used for idempotent retry: the adapter MAY deduplicate recently-seen
  `event_id` values within a bounded window. Required in v0 — wrapped requests
  (arbitrary JSON bodies) receive a server-generated `event_id` if none is
  supplied via the `X-AgentWake-Event-Id` header.
- **`source`** — Free-form, used by the agent to identify what sent the event
  (e.g., `"github-actions"`, `"datadog"`, `"telegram"`).
- **`kind`** — Free-form but standardized to a small set: `webhook`, `alert`,
  `message`, `approval_request`, `approval_verdict`.
- **`content`** — What the agent reads in its context window.
- **`meta`** — `Record<string, string>`. Identifier keys only (letters, digits,
  underscores). Hyphens and other characters are silently stripped by the
  Claude Code channel layer (per the channels-reference spec).
- **`wake`** — `true` triggers an agent turn. `false` injects context silently
  (useful for "FYI" events that do not need immediate response).

### Retry and idempotency semantics

The HTTP ingest layer is modeled on the Stripe / GitHub webhook pattern.
Senders SHOULD treat the ingest endpoint as best-effort and retry with
exponential backoff on non-2xx responses or connection failures (suggested:
3 retries over 60 seconds, then surface the failure to the operator).

The adapter SHOULD deduplicate by `event_id` within a recent-events window
(suggested: 256 most-recent ids in memory; no persistence in v0). A duplicate
`event_id` returns 202 with `{"status": "duplicate"}` and does NOT wake the
agent a second time. This makes the ingest endpoint safe for at-least-once
delivery.

The dedupe window is intentionally in-memory only for v0. After an adapter
restart, the window is empty and a retry-after-restart MAY result in a duplicate
wake. Document this; durable dedupe is a v1 question tied to the same
substrate-inbox decision as missed-event queueing.

---

## Reply event (agent → adapter → external)

```json
{
  "v": 0,
  "in_reply_to": "string — opaque ID of the event being replied to",
  "content": "string",
  "meta": {}
}
```

The adapter delivers the reply by POSTing to a configured callback URL
(per-source or default).

**Delivery loss when the session exits.** Replies are produced by the agent
during a session turn. If the session exits or the harness process dies before
the reply tool fires, the reply is lost — agent-wake does not maintain a durable
outbox in v0. The sender must treat the absence of a reply as inconclusive (the
agent may have acted but crashed before replying) and reconcile via its own
state.

This is a known v0 limitation, not a bug. A durable per-session inbox/outbox is
a v1+ design question deferred to substrate.

---

## HTTP ingest

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

### Request headers

- `X-AgentWake-Source` — required. Must match a configured source name.
- `X-AgentWake-Signature` — required. HMAC-SHA256 of the raw body bytes using
the source's shared secret, formatted as `sha256=<hex>`.
- `X-AgentWake-Event-Id` — optional. If present, populates `event_id` on the
wrapped event (when the body is not already a v0 event). If absent, the adapter
generates a ULID-shaped id. If the body is already a v0 event, the body's
`event_id` wins.

### Responses

- `202 Accepted` — successful ingest.
  Body: `{"status": "queued", "event_id": "<the id used>"}`
- `202 Accepted` (duplicate) — `event_id` seen within the dedupe window.
  Body: `{"status": "duplicate", "event_id": "<id>"}`. No wake is emitted.
- `403 Forbidden` — missing or invalid signature, or unknown source.
  No leakage of whether the source is configured.

### Body wrapping

If the body is a full wake event object (matching the schema), the adapter
forwards it as-is. If the body is arbitrary JSON (e.g., a GitHub webhook
payload), the adapter wraps it:

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

The adapter does not interpret the body. The agent reads it and decides what to
do.

---

## Configuration file

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

- Each source has its own HMAC secret (read from an env var; never written to the
config file).
- `callback_url` is optional. If present, replies POST there. If absent, replies
fail gracefully (log a warning, return "sent" to the harness — replies are
best-effort in v0).

---

## Prior art

- Stripe `Idempotency-Key` headers
- GitHub webhook redelivery IDs
- Centrifugo history & stream recovery
- Temporal Signal-With-Start
