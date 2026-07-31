# agent-wake v1 — daemon implementation spec

**Status:** canonical implementation spec. Phases 1–6 of the v1 work plan are executed against this document.
**Reader:** an implementing agent (human or LLM) handed one phase at a time. Each phase below is self-contained and ends with explicit acceptance tests. Do not implement out of phase order.
**Prior art:** [`core/schema.md`](../core/schema.md) (v0 wire format, unchanged), [`adapters/claude/src/agent_wake_claude/`](../adapters/claude/src/agent_wake_claude/) (the Python adapter being slimmed).

---

## 1. Why this exists

v0 ran one HTTP listener *inside each adapter*. With ≥2 adapters per host or ≥2 instances of one harness, they collide on the same port. The product fix is a single long-lived daemon that:

- owns the inbound HTTP port (one binder per host/user),
- demultiplexes inbound events to adapter subscribers over a unix socket,
- owns outbound reply HTTP (adapters stop making outbound HTTPS calls).

Adapters become thin clients: subscribe → translate wake events into the harness's native wake primitive → forward reply-tool invocations back to the daemon.

Routing is **source-bound** in v1: each `source` in daemon config names one target adapter. Header-targeted and filter-subscribe routing are explicit non-goals for v1 (Section 12).

---

## 2. Architecture at a glance

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
│ agent-wake-claude (in-process to Claude Code)             │
│                                                           │
│  daemon client (long-lived TCP/unix conn)                 │
│      │                                                    │
│      ├──► hello on connect                                │
│      ├──◄ wake frames ──► channel.emit_wake_event(...)    │
│      └──► reply frames (from the agent_wake_reply tool)   │
└───────────────────────────────────────────────────────────┘
```

The external wire format (between external systems and the daemon) is **unchanged from v0** — see `core/schema.md`. The new contract is the **daemon↔adapter unix-socket protocol** defined in Section 4.

---

## 3. Filesystem layout after v1

New tree (created by this work):

```
agent-wake/
├── daemon/
│   ├── pyproject.toml
│   ├── README.md
│   ├── src/agent_waked/
│   │   ├── __init__.py
│   │   ├── __main__.py
│   │   ├── main.py            # entrypoint, signal handling, lifecycle
│   │   ├── config.py          # daemon config loading
│   │   ├── gating.py          # COPY of adapters/claude/.../gating.py (verbatim)
│   │   ├── ingest.py          # HTTP ingest (refactored from adapter)
│   │   ├── socket_server.py   # unix-socket listener, frame dispatch
│   │   ├── router.py          # source → adapter routing
│   │   ├── outbox.py          # outbound reply delivery
│   │   └── proto.py           # frame types + (de)serialization
│   ├── tests/
│   │   ├── test_gating.py
│   │   ├── test_ingest.py
│   │   ├── test_router.py
│   │   ├── test_proto.py
│   │   ├── test_socket_server.py
│   │   ├── test_outbox.py
│   │   └── test_e2e.py        # full curl → daemon → mock-adapter chain
│   └── systemd/
│       └── agent-waked.service
└── adapters/
    └── claude/                # slimmed in phase 4
```

Modified existing files (phase 4):

```
adapters/claude/src/agent_wake_claude/
├── __main__.py                # no longer starts an HTTP listener
├── client.py                  # NEW: daemon-socket client (replaces ingest.py)
├── ingest.py                  # DELETED
├── gating.py                  # DELETED (lives in daemon now)
├── reply.py                   # SLIMMED: sends reply frame to daemon, not HTTPS
└── config.py                  # extended with socket_path
```

Do not delete `ingest.py` and `gating.py` until phase 4. Phase 1 *copies* `gating.py` into the daemon (verbatim) so phase 4 can then delete the adapter copy.

---

## 4. Daemon ↔ adapter wire protocol

### 4.1 Transport

Unix-domain stream socket. Default path order (first writable wins):

1. `$XDG_RUNTIME_DIR/agent-wake.sock` (when `$XDG_RUNTIME_DIR` is set)
2. `~/.local/state/agent-wake/agent-wake.sock` (create the parent dir if missing)

Mode `0700` on the parent directory. Socket file mode `0600`. The daemon `unlink()`s a stale socket on startup if the path is a socket and no process holds it.

### 4.2 Framing

Newline-delimited JSON. One JSON object per `\n`-terminated line. Frames are bidirectional on the same connection — server and client both send frames asynchronously.

- UTF-8 only. No BOM.
- Frames have a top-level `"type"` string discriminator.
- Unknown frame types: log a warning, ignore. Forward compatibility.
- Lines longer than 1 MiB: close the connection with code `frame_too_large`.

### 4.3 Frame catalogue

All frames carry `"type"`. Other fields per type:

#### 4.3.1 `hello` (adapter → daemon, first frame)

```json
{
  "type": "hello",
  "v": 1,
  "adapter": "claude",
  "instance": "pid-12345",
  "destinations": ["claude-main"],
  "filters": {"sources": ["github-actions", "telegram-bot"]}
}
```

- `v` — protocol version. v1 only accepts `1`. Reject any other with `error` frame `version_unsupported` and close.
- `adapter` — adapter family name. v1 known values: `"claude"`. Unknown values are accepted (forward compat).
- `instance` — opaque, adapter-supplied. Used by daemon for logging and to disambiguate multiple connections from the same family.
- `destinations` — **added by config v2.** Destination names this connection serves. A claim is honoured only if the destination exists and its configured `adapter` matches this connection's — a socket peer asking for somebody else's destination is not authorization. A connection that serves a session-scoped destination is how the daemon knows which session to wake (BC-001).
- `filters.sources` — list of source names this adapter is willing to receive. Still accepted on its own, which is what keeps already-deployed adapters working: the daemon resolves those names to destinations (under the v0/v1 translation they *are* destination names). If a routed event's destination is not served by any connection, the daemon reports `no_subscriber` (see 5.4).
- At least one of `destinations` / `filters.sources` must be present. A hello claiming nothing would hold a connection slot and never be routed anything, which reads as a hung adapter rather than the misconfiguration it is.

#### 4.3.2 `hello_ack` (daemon → adapter, response to hello)

```json
{
  "type": "hello_ack",
  "v": 1,
  "session_id": "01J...",
  "accepted_sources": ["github-actions", "telegram-bot"],
  "accepted_destinations": ["claude-main"]
}
```

- `session_id` — ULID minted by the daemon. Echoed in subsequent log lines on both sides.
- `accepted_sources` — intersection of the adapter's `filters.sources` and sources configured in daemon config that target this adapter. Surfaces config drift early. Unchanged from v1, deliberately: it is what deployed adapters read.
- `accepted_destinations` — **added by config v2.** The destination names this connection will actually be sent events for. An empty list against a non-empty claim is the loud signal that a destination was misspelled or belongs to another adapter.

If admitting this connection would exceed a destination's `max_connections`, the daemon sends `error` with code `connection_limit` naming the destination and closes, rather than admitting a connection that cannot be honoured (BC-WAKE-010).

After receiving `hello_ack` the adapter is "subscribed" and may receive `wake` frames.

#### 4.3.3 `wake` (daemon → adapter)

```json
{
  "type": "wake",
  "ack_id": "01J...",
  "destination": {
    "name": "oc-review",
    "adapter": "opencode",
    "session": "ses_01H...",
    "principal": "agent:mvmcc03-claude"
  },
  "event": {
    "v": 0,
    "event_id": "...",
    "source": "github-actions",
    "kind": "webhook",
    "content": "...",
    "meta": {},
    "wake": true
  }
}
```

- `event` — verbatim v0 wake event (Section 3 of `core/schema.md`), apart from the identity keys the daemon stamps at ingest. The daemon does not otherwise mutate `event` between ingest and delivery.
- `ack_id` — daemon-minted, distinct from `event.event_id`. The adapter MUST send `ack` with this id within 5 seconds.
- `destination` — **added by config v2.** Who this copy of the event is addressed to. `name` is always present; `adapter`, `session` and `principal` appear when configured. `session` is what closes BC-001: a multi-session adapter is told which session to wake instead of broadcasting for lack of an addressee. `principal` is the daemon-authoritative answer to "whose attention was requested", which cannot live on `event.meta` because a fan-out gives one event a different addressee per recipient.

An adapter that ignores `destination` behaves exactly as it did under v1, which is why the field is optional and why the deployed Claude adapter needed no change.

One event addressed to several destinations produces one `wake` frame per destination, each with its own `ack_id`, delivered in config order (BC-WAKE-022).

#### 4.3.4 `ack` (adapter → daemon)

```json
{"type": "ack", "ack_id": "01J..."}
```

Acknowledges receipt and successful handoff to the harness's wake primitive (not necessarily user-visible reply). The daemon uses acks only for logging in v1; durable retry is a v1.1+ concern.

#### 4.3.5 `nack` (adapter → daemon)

```json
{"type": "nack", "ack_id": "01J...", "reason": "channel_unavailable"}
```

Sent when the adapter cannot deliver to the harness (e.g., MCP stdout closed). Logged. No retry in v1.

#### 4.3.6 `reply` (adapter → daemon)

```json
{
  "type": "reply",
  "source": "github-actions",
  "destination": "oc-review",
  "in_reply_to": "01J...",
  "content": "string"
}
```

The agent invoked the `agent_wake_reply` tool. The adapter forwards the args here. The daemon looks up the sender's `callback_url` and POSTs (see Section 8).

- `destination` — **added by config v2**, optional. Which addressee is replying. When present it MUST be a destination this connection serves, or the daemon answers with a non-fatal `error` frame (`unauthorized_destination`) and drops the reply: without that check an adapter could attribute its reply to another principal. The daemon carries the destination and its principal into the outbound payload's `meta`, which is the "who is the peer on the way out" the outbound-auth items (BC-WAKE-008/017/018) were blocked on. It does **not** yet authenticate the outbound request — that is deliberately the next piece of work, sequenced after addressing so it is not built against a model that cannot name its peer.

#### 4.3.7 `reply_result` (daemon → adapter)

```json
{
  "type": "reply_result",
  "reply_id": "01J...",
  "status": "delivered",
  "http_status": 200,
  "error": null
}
```

- `reply_id` is set by the adapter on the `reply` frame; the daemon echoes it. (Add `"reply_id"` to the `reply` frame in 4.3.6 — yes, this is required; treat 4.3.6 as carrying `"reply_id"` too.)
- `status` ∈ {`"delivered"`, `"failed"`, `"no_callback"`}.

The adapter surfaces `status` (and `error` if set) as the tool's return text to the agent.

#### 4.3.8 `error` (either direction)

```json
{"type": "error", "code": "...", "message": "...", "fatal": true}
```

Codes (v1):

- `version_unsupported` — peer sent an unrecognised protocol version. Fatal.
- `bad_frame` — malformed JSON or missing required field. Fatal.
- `frame_too_large` — line exceeded 1 MiB. Fatal.
- `unknown_source` — daemon-side; `reply.source` not configured. Non-fatal; mirrored back as `reply_result.status="failed"`.
- `unauthenticated` — adapter sent non-hello frame before completing handshake. Fatal.

If `fatal: true`, the sender closes the connection after writing the frame.

#### 4.3.9 `ping` (daemon → adapter)

```json
{"type": "ping"}
```

Sent by the daemon every 30 seconds as an application-layer heartbeat (see §4.4). The adapter MUST reply with a `pong` frame promptly.

#### 4.3.10 `pong` (adapter → daemon)

```json
{"type": "pong"}
```

Reply to a `ping` frame. Any adapter that ignores `ping` will be disconnected by the daemon on the next heartbeat check.

### 4.4 Liveness

- 30-second idle TCP keepalive (`SO_KEEPALIVE`). On stream closure, daemon drops the subscription. Adapter reconnects with backoff (Section 9.3).
- Application-layer heartbeat: the daemon sends a `ping` frame (§4.3.9) every 30 seconds. The adapter MUST reply with a `pong` frame (§4.3.10) within the same interval. If the daemon receives no `pong` by the next heartbeat check, it closes the connection. This is a v1 change (not v1.1); the original spec draft deferred this but the implementation shipped it to detect stale connections on Unix-domain sockets, where TCP keepalive alone is sometimes insufficient.


---

## 5. Daemon configuration

### 5.1 Path

Same as v0: `~/.config/agent-wake/config.json` (override via `AGENT_WAKE_CONFIG`).

Backward compatible: v0 config files load unchanged if `routing` is absent (Section 5.4 default rule). The daemon emits a deprecation warning at startup when no `routing` block is present.

### 5.2 Full example

```json
{
  "version": 1,
  "listen": {"host": "127.0.0.1", "port": 8788},
  "socket_path": null,
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
  "default_callback_url": null,
  "routing": {
    "github-actions": {"adapter": "claude"},
    "telegram-bot": {"adapter": "claude"}
  }
}
```

### 5.3 Field semantics

- `version` — `1` for the daemon. `0` is also accepted (v0 compat); the daemon logs a one-line deprecation note and treats the file as if `version` were 1 with an empty `routing` block.
- `listen.host` / `listen.port` — HTTP ingest bind. Default `127.0.0.1:8788`.
- `socket_path` — explicit override for the unix-socket path. `null` means use the discovery rule in Section 4.1.
- `sources` — unchanged from v0. Superseded by `senders` in config v2 (Section 5.5); still accepted indefinitely.
- `routing` — map from source name to adapter target. v1 supports only `{"adapter": "<name>"}`. An empty or missing `routing` block means "route all sources to any connected adapter that subscribed to them" (legacy single-adapter mode). When multiple adapters claim the same source under legacy mode, the daemon picks the most recently subscribed one and logs a warning. Superseded by `destinations` + `routes` in config v2.

### 5.4 Routing resolution

For an inbound event from sender `S`:

1. Resolve `S`'s routes to a destination set (Section 5.5). Under the v0/v1 translation this is the single destination named after the source, so the result is identical to what v1 computed.
2. If the event carries `meta.destination` or `meta.principal`, intersect. An empty intersection is HTTP 403 with `reason: destination_not_routed` / `principal_not_routed` — the sender narrowed to somewhere it has no route to, which is its own error and is reported.
3. For each destination, find the connection serving it whose `hello.adapter` matches the destination's configured `adapter` (any adapter, if unconfigured). If several, pick the most recently subscribed — the v1 rule-2 tiebreak, now rare because session scoping means sibling sessions claim different destinations.
4. Send one `wake` frame per destination found. Destinations with no live connection are either queued for that destination (when the event asked for durable delivery) or dropped.

The response is HTTP 202 with `queued` if **any** destination was delivered — a partial fan-out is not a failure, and reporting one would make senders re-send what already landed. `queued_next_session` when nothing was live but something was persisted; `no_subscriber` otherwise.

`no_subscriber` is a successful HTTP 202 — at-least-once delivery from the sender's perspective is preserved; the daemon dropped the event because no one was listening. It is also deliberately indistinguishable from "that destination is not routed to you", so a sender cannot enumerate the box's destinations.

### 5.5 Config v2: the addressing model

Config version 2 (WI-006) replaces the fused `sources` entry with the three concepts it was carrying, plus an explicit authorization edge:

- `senders` — credential only: `secret_env` / `secret` / `secrets`, `allowed_trigger_identities`, `callback_url`, `allowed_target_principals`, and an optional `identity` (who *asks*, stamped as `meta.trigger_identity`).
- `principals` — `principal_id` → `{"channels": {...}}`; the out-of-band delivery table v1 kept at the top level as `delivery`.
- `destinations` — name → `{"adapter", "session", "principal", "max_connections"}`. One addressable place. `session` requires `adapter`, since a session identifier only means something within one adapter.
- `routes` — a **list** of `{"sender", "principal"?, "destinations"?}`. A list rather than a map because a name-keyed table can hold exactly one target per key, which is why fan-out needed a schema change to exist at all. `principal` alone means "every destination that principal owns".

Every reference is validated at load: a route to a destination that does not exist would accept events and wake nobody. Mixing vocabularies for one table (`sources` and `senders` together) is a hard error — there is no safe merge, and silently preferring one spelling drops a credential.

`agent-wake config show` prints the resolved model; `agent-wake config migrate` prints the v2 form of a v0/v1 file. The translation is identity-preserving (a source name becomes both a sender and a destination name) and authority-preserving: out-of-band delivery is granted only by `allowed_target_principals` and never derived from routes, so migrating cannot mint egress authority the old file lacked.

---

## 6. Daemon process model

### 6.1 Threads / async model

Use stdlib `asyncio`. Single event loop. No threading. (The legacy adapter used `threading` because it had to coexist with synchronous MCP stdio; the daemon has no such constraint.)

### 6.2 Subsystems and their lifecycle

| Subsystem | Owns | Started at | Stopped at |
|---|---|---|---|
| HTTP ingest | `aiohttp` Application bound to `listen.host:listen.port` | startup | SIGTERM |
| Unix socket server | `asyncio.start_unix_server` on socket path | startup | SIGTERM |
| Router | in-memory map `source → subscriber` (no I/O) | n/a | n/a |
| Outbox | `aiohttp.ClientSession` for reply POSTs | startup | SIGTERM |

`aiohttp` is the only external dependency. Pin to a specific minor in `pyproject.toml`.

### 6.3 Signals

- `SIGTERM`, `SIGINT` — graceful: close listening sockets, drain in-flight requests with 5-second cap, close all unix-socket connections, exit 0.
- `SIGHUP` — config reload. Re-parse `~/.config/agent-wake/config.json`. New ports require restart (log and ignore the change). New sources/routing take effect immediately. Existing subscribers stay connected; their `accepted_sources` is recomputed on next `hello_ack` only (not retroactively pushed).

### 6.4 Single-instance guarantee

On startup, attempt to bind the unix socket path. If it exists and is held by a live process, exit 1 with a clear error. If it exists but is dead, `unlink` and retry. Use `flock` on a sidecar file (`<socket_path>.lock`) for the live-process check.

---

## 7. HTTP ingest (daemon side)

Lift the request-handling logic from `adapters/claude/src/agent_wake_claude/ingest.py`. Translate from stdlib `http.server` to `aiohttp` handlers. Preserve semantics exactly:

- Routes:
  - `POST /` — wake event ingest.
  - `404` for everything else. (The `/permission/verdict` route in the legacy code is Claude-specific and stays in the adapter. Do not port it.)
- Headers and signature verification: identical to v0.
- Body wrapping and pass-through rules: identical to v0 (`_build_wake_event` logic).
- Dedupe: keep the in-memory 256-id FIFO from `ingest.py`. Promote `_recent_event_ids` and `_DEDUPE_LOCK` into a small `Dedupe` class so it's testable in isolation.
- Source mismatch: same warn-log + generic 500 response.

The only behavioural addition is that after a successful gate+dedupe, the handler calls `router.deliver(event)` (Section 5.4 routing) and returns one of:

- `202 {"status":"queued","event_id":...}` — delivered to a subscriber.
- `202 {"status":"duplicate","event_id":...}` — seen in dedupe window.
- `202 {"status":"no_subscriber","event_id":...}` — accepted, but no adapter matched.

`no_subscriber` is 202 not 503 because the sender retrying would just produce duplicates; the event is unrouteable until an adapter subscribes.

---

## 8. Outbox (daemon side)

When a `reply` frame arrives:

1. Look up `sources[reply.source].callback_url` or fall back to `default_callback_url`.
2. If neither is set, return `reply_result {"status":"no_callback"}` immediately.
3. POST a v0 reply object (Section "Reply event" of `core/schema.md`):
   ```json
   {"v": 0, "in_reply_to": "<reply.in_reply_to>", "content": "<reply.content>", "meta": {}}
   ```
   `Content-Type: application/json`, 30-second timeout, no retries in v1.
4. On 2xx response: `reply_result {"status":"delivered","http_status":<code>}`.
5. On non-2xx or exception: `reply_result {"status":"failed","http_status":<code or null>,"error":"<short message>"}`.

Per-source reply latency: aggregate to a one-line INFO log after each POST (`source=X status=Y http=Z duration_ms=W`). No metrics export in v1.

---

## 9. Adapter changes (claude)

### 9.1 What goes away

Delete these files entirely:

- `adapters/claude/src/agent_wake_claude/ingest.py`
- `adapters/claude/src/agent_wake_claude/gating.py`

Their logic now lives in the daemon. The HTTP listener at 8788 is no longer the adapter's responsibility.

### 9.2 What stays

- `channel.py` — unchanged. Emits MCP `notifications/claude/channel` for a wake event dict.
- `permission.py` — unchanged (separate concern, Claude Code permission flow). The `/permission/verdict` endpoint may continue to be served by the adapter on a different port if you wish; that's orthogonal to this work and out of scope. v1 default: leave permission.py alone, it does not bind a port today (verify).
- `_notify.py`, `server.py` — unchanged.
- `__init__.py` — unchanged.

### 9.3 What's new: `client.py`

Single new file. Pseudocode:

```python
# adapters/claude/src/agent_wake_claude/client.py
"""Daemon client: maintains a unix-socket connection to agent-waked,
receives wake frames, dispatches to channel.emit_wake_event."""

import asyncio
import json
import logging
import os
from pathlib import Path

from .config import load_config
from .channel import emit_wake_event

log = logging.getLogger("agent_wake_claude.client")

PROTOCOL_VERSION = 1

def _socket_path() -> Path:
    cfg = load_config()
    if cfg.get("socket_path"):
        return Path(cfg["socket_path"])
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "agent-wake.sock"
    return Path.home() / ".local" / "state" / "agent-wake" / "agent-wake.sock"

async def run_client():
    cfg = load_config()
    sources = list(cfg["sources"].keys())
    backoff = 1.0
    while True:
        try:
            await _one_session(_socket_path(), sources)
            backoff = 1.0
        except Exception as e:
            log.warning("daemon connection lost: %s; reconnecting in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

async def _one_session(sock_path, sources):
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    hello = {"type": "hello", "v": PROTOCOL_VERSION, "adapter": "claude",
             "instance": f"pid-{os.getpid()}", "filters": {"sources": sources}}
    writer.write((json.dumps(hello) + "\n").encode())
    await writer.drain()
    while True:
        line = await reader.readline()
        if not line:
            return
        frame = json.loads(line)
        t = frame.get("type")
        if t == "hello_ack":
            log.info("subscribed, session_id=%s accepted_sources=%s",
                     frame.get("session_id"), frame.get("accepted_sources"))
        elif t == "wake":
            ack_id = frame.get("ack_id")
            event = frame.get("event", {})
            try:
                emit_wake_event(event)
                ack = {"type": "ack", "ack_id": ack_id}
            except Exception as e:
                log.warning("emit failed: %s", e)
                ack = {"type": "nack", "ack_id": ack_id, "reason": str(e)}
            writer.write((json.dumps(ack) + "\n").encode())
            await writer.drain()
        elif t == "reply_result":
            # Bridged into the agent_wake_reply tool — see reply.py
            ReplyResultBus.deliver(frame)
        elif t == "error":
            log.error("daemon error: %s", frame)
            if frame.get("fatal"):
                return
```

Use stdlib `asyncio` to integrate with the existing stdio MCP server. `__main__.py` launches `run_client()` in a background asyncio task via `asyncio.run_coroutine_threadsafe` or by running the MCP server inside the loop (whichever is less invasive to `server.py`).

### 9.4 Reply tool

`reply.py.handle_reply_tool_call` no longer makes HTTPS calls directly. It:

1. Generates a `reply_id` (ULID).
2. Writes a `reply` frame onto the daemon connection.
3. Awaits the matching `reply_result` via `ReplyResultBus` (in-memory `asyncio.Future` keyed by `reply_id`, 35-second timeout — daemon's 30-second outbox limit + 5s slack).
4. Returns the result text to the agent (verbatim mappings: `delivered` → `"sent"`, `no_callback` → `"sent (no callback_url configured)"`, `failed` → `"reply delivery failed: <error>"`).

### 9.5 `__main__.py`

Remove `start_listener` import and call. Add asyncio runtime that hosts both the MCP stdio server and `client.run_client()`. Preserve existing SIGTERM/SIGINT handling.

### 9.6 `config.py`

Add `socket_path` to the loaded config dict (default `None`). Keep `host`/`port` for backward compatibility, but neither is consumed by the slimmed adapter — they are read only to forward to the daemon for users who haven't restarted yet. Log a one-line deprecation warning at adapter startup if `host`/`port` are set in the config (the adapter no longer binds them).

---

## 10. Phase plan

Each phase ends with explicit acceptance tests. Do not advance phases until the current one passes.

### Phase 1 — daemon skeleton

**Implement:**

- `daemon/pyproject.toml` declaring `agent-waked` console-script entrypoint, Python ≥3.11, `aiohttp` pinned, `python-ulid` pinned.
- `daemon/src/agent_waked/__main__.py` and `main.py` — entrypoint, signal handlers, lifecycle.
- `daemon/src/agent_waked/config.py` — load and validate per Section 5.
- `daemon/src/agent_waked/proto.py` — frame encode/decode, `validate_frame(frame, expected_types)`.
- `daemon/src/agent_waked/socket_server.py` — bind unix socket, accept connections, parse frames line-by-line, dispatch to a registered handler. No router yet.
- `daemon/src/agent_waked/ingest.py` — stub: HTTP server bound but `POST /` returns `501 Not Implemented` for now.
- `daemon/systemd/agent-waked.service` — user unit, Type=simple, Restart=on-failure.
- README.

**Acceptance:**

1. `pip install -e daemon/` then `python -m agent_waked` exits 0 within 5s of receiving SIGTERM.
2. Daemon binds the configured `listen.host:listen.port` and the unix socket path; `lsof -p <pid>` shows both.
3. Manual `nc -U <socket> <<< '{"type":"hello","v":1,"adapter":"claude","instance":"t","filters":{"sources":["x"]}}'` receives `hello_ack` and stays connected.
4. Sending `{"type":"hello","v":99,...}` receives `error` with `code=version_unsupported`, then the socket closes.
5. `POST /` returns 501.
6. `daemon/tests/test_proto.py` and `daemon/tests/test_socket_server.py` are green. They cover: every frame type round-trips, malformed JSON closes connection with `bad_frame`, oversize line closes with `frame_too_large`, version-mismatched hello produces `version_unsupported`.

### Phase 2 — ingest + routing

**Implement:**

- Promote `_build_wake_event`, `_recent_event_ids`, `_DEDUPE_LOCK`, signature verification from `adapters/claude/src/agent_wake_claude/ingest.py` into `daemon/src/agent_waked/ingest.py` and `daemon/src/agent_waked/gating.py` (verbatim where possible; the only translation is stdlib http.server → aiohttp).
- `daemon/src/agent_waked/router.py` — `Router` class. Methods: `subscribe(session_id, adapter, instance, sources)`, `unsubscribe(session_id)`, `deliver(event) -> "queued" | "no_subscriber"`. Holds a writer reference per session for delivery. Routing resolution per Section 5.4.
- Wire the ingest handler to call `Router.deliver`.
- Wire the unix-socket server to call `Router.subscribe` after a valid `hello`, send `hello_ack`, and `Router.unsubscribe` on disconnect.

**Acceptance:**

1. With a connected mock adapter that subscribes to `["test"]`, `curl -X POST http://localhost:8788/ -H 'X-AgentWake-Source: test' -H 'X-AgentWake-Signature: sha256=<correct>' -d '{"v":0,...}'` returns 202 `queued` and the mock adapter receives a `wake` frame whose `event.event_id` matches.
2. The same curl with an unknown source returns 403.
3. The same curl with no adapter connected returns 202 `no_subscriber`.
4. A duplicate event_id returns 202 `duplicate` and does NOT produce a second `wake` frame.
5. `daemon/tests/test_router.py`, `daemon/tests/test_ingest.py`, `daemon/tests/test_gating.py` all green. Test `test_gating.py` is a byte-for-byte port of the existing `adapters/claude/tests/test_gating.py`; assert no behavioural change.

### Phase 3 — outbox + reply

**Implement:**

- `daemon/src/agent_waked/outbox.py` — `Outbox` class wrapping a `aiohttp.ClientSession`. `async def deliver(source: str, reply_id: str, in_reply_to: str, content: str) -> dict` returns the `reply_result` payload (without the `"type"` field).
- Wire the socket server: on `reply` frame, look up the source config, call `Outbox.deliver`, send `reply_result` back to the originating connection.
- `daemon/tests/test_outbox.py` — start an aiohttp test server, configure a callback to it, send a reply, assert delivery payload and `reply_result.status="delivered"`.

**Acceptance:**

1. With a configured callback URL (use `python -m http.server` + a tiny capture script for the test, or aiohttp test server), sending a `reply` frame results in an HTTPS POST to the callback whose body is `{"v":0,"in_reply_to":"...","content":"...","meta":{}}` and a `reply_result` frame with `status=delivered`.
2. With no callback configured, the reply frame produces `status=no_callback` and no HTTP egress.
3. Callback returning 500 produces `status=failed`.
4. Callback timing out produces `status=failed` with an error message mentioning timeout. Timeout is 30s ± 1s.

### Phase 4 — claude adapter slim-down

**Implement:**

- Delete `adapters/claude/src/agent_wake_claude/ingest.py` and `gating.py`.
- Add `adapters/claude/src/agent_wake_claude/client.py` per Section 9.3.
- Update `adapters/claude/src/agent_wake_claude/__main__.py` per Section 9.5.
- Update `adapters/claude/src/agent_wake_claude/reply.py` per Section 9.4.
- Update `adapters/claude/src/agent_wake_claude/config.py` per Section 9.6.
- Update `adapters/claude/pyproject.toml`: drop dependencies that were ingest-only (none expected — verify). Add nothing new; the client uses only stdlib.
- Update `adapters/claude/README.md` to describe the daemon-client model and remove "binds port 8788" wording.
- Delete `adapters/claude/tests/test_gating.py` and `adapters/claude/tests/test_ingest.py` (now in the daemon). Add `adapters/claude/tests/test_client.py` covering: hello frame is the first write, wake frame triggers `emit_wake_event`, ack is sent post-emit, malformed daemon frame is ignored without crashing, reconnect-on-disconnect with backoff.

**Acceptance:**

1. With the daemon running (phases 1–3) and the slimmed adapter installed, the end-to-end smoke test in `adapters/claude/examples/demo.sh` succeeds: the curl gets 202 `queued` and the adapter's channel layer emits a notification line on stdout. (Use a stubbed channel `send()` in tests; for manual smoke, the existing demo flow is fine.)
2. The adapter does NOT bind any TCP port. `ss -ltnp | grep <adapter_pid>` is empty.
3. If the daemon is not running, the adapter starts, logs a reconnect warning every backoff interval, and continues to run (does not crash). When the daemon starts, the adapter connects within one backoff interval.
4. The `agent_wake_reply` tool returns `"sent"` for a successful callback delivery (with daemon running) and `"sent (no callback_url configured)"` when no callback is configured.

### Phase 5 — lifecycle / packaging

**Implement:**

- Finalise `daemon/systemd/agent-waked.service` and document installation:
  ```
  [Unit]
  Description=agent-wake daemon
  After=network.target

  [Service]
  Type=simple
  ExecStart=%h/.local/bin/agent-waked
  Restart=on-failure
  RestartSec=2s
  Environment=PYTHONUNBUFFERED=1

  [Install]
  WantedBy=default.target
  ```
  Install path: `~/.config/systemd/user/agent-waked.service`. Document `systemctl --user daemon-reload && systemctl --user enable --now agent-waked`.
- SIGHUP config reload behaviour per Section 6.3.
- Daemon README sections: install, configure, run, debug.
- Top-level `agent-wake/README.md`: rewrite the "Quick start" so the daemon is the first install step.

**Acceptance:**

1. `systemctl --user enable --now agent-waked` brings the daemon up. `systemctl --user status agent-waked` shows active running.
2. `systemctl --user restart agent-waked` produces no errors and the adapter (if running) reconnects within 5 seconds.
3. Editing `~/.config/agent-wake/config.json` to add a new source, then `systemctl --user kill -s SIGHUP agent-waked`, makes that source addressable in <2 seconds without restart.
4. Changing `listen.port` and sending SIGHUP logs a warning that the port change requires restart and the old port stays bound.

### Phase 6 — end-to-end test + final docs

**Implement:**

- `daemon/tests/test_e2e.py` — spins up the daemon in a subprocess on an ephemeral port and ephemeral socket path, connects a mock adapter, sends an HTTP wake event, asserts the wake frame, sends an ack, sends a reply, asserts an HTTPS POST to a stub callback. Cleans up subprocess.
- Update top-level `agent-wake/README.md` to remove the "Status" deprecation block from the prior session (the daemon now exists) and replace with the production architecture diagram from Section 2 here.
- Update `core/schema.md` to reference the daemon as the canonical ingest endpoint owner.
- Update `examples/github-actions-webhook.md` if its `localhost:8788` reference now needs context about the daemon owning that port.
- Restore the `typescript` job in `.github/workflows/ci.yml`? **No** — opencode adapter is still archived. Leave that for a later phase.

**Acceptance:**

1. `cd daemon && pytest -q` runs all phases' tests including e2e green.
2. `cd adapters/claude && pytest -q` runs the slimmed adapter tests green.
3. A first-time reader can follow `README.md` from "I have nothing installed" to "I just received a wake event in Claude Code" without inspecting source.

---

## 11. Non-functional requirements (all phases)

- **No new dependencies in the adapter.** `client.py` uses only stdlib. Justification: the adapter must remain installable on minimal Python environments.
- **Daemon dependencies pinned.** `aiohttp` and `python-ulid` to a known-good minor in `pyproject.toml`.
- **Logging.** Daemon logs to stderr at INFO by default. `AGENT_WAKE_LOG_LEVEL` env var controls verbosity. One line per ingest, one line per delivery, one line per reply. No PII in log lines except `source` names.
- **No retries** in v1 anywhere. At-least-once delivery is preserved on the inbound side (sender retries); outbound is fire-once-with-30s-timeout. Durable retry is v1.1+.
- **No persistence** in v1. Dedupe window, routing table, in-flight reply futures: all in-memory. Daemon restart loses the dedupe window (documented).
- **No authentication on the unix socket** beyond filesystem mode (`0600`). Any local process running as the daemon's user can subscribe as any adapter family. Defence-in-depth (PID validation, named sockets per-adapter) is v1.1+.
- **Code style.** Match existing claude adapter: type hints, `from typing import ...` only where needed (Python 3.11+), `logging` via `getLogger`. No third-party formatter beyond what's in `pyproject.toml` today.
- **Tests.** `pytest` only. No `unittest` mocking unless absolutely necessary. Use `pytest-asyncio` for async tests; pin it in dev-deps.

---

## 12. Explicit non-goals for v1

These come up. They are not in scope:

- **Header-targeted routing** (`X-AgentWake-Target` header). Senders cannot pick a specific adapter. Source-bound only.
- **Filter-subscribe routing** with overlapping subscriptions / fan-out to multiple adapters.
- **Durable dedupe / inbox.** In-memory only.
- **Reply-outbox persistence.** Replies lost on daemon crash.
- **Multi-user / multi-tenant.** Single Unix user per daemon. See `design/identity-and-multi-user.md` for the longer story; that work is independent.
- **Authentication of unix-socket peers.** Filesystem mode only.
- **opencode adapter.** Archived in `adapters/opencode.archived/`. Reimplementing as a daemon client is its own work, intentionally deferred.
- **gemini, codex adapters.** New adapter scaffolding is welcome after the daemon is stable, not concurrent with this work.
- **TLS on the HTTP ingest.** Front with a reverse proxy if exposed.

---

## 13. Open questions / decisions deferred to first encounter

These have a default but warrant a second look during implementation:

- **Wake frame max size.** 1 MiB per line is generous. If a webhook payload exceeds that, the daemon currently returns a 4xx on ingest? **Decision:** add `body_too_large` ingest-side at 1 MiB and surface as 413. Defer if not implemented in phase 2; treat as known limitation.
- **Reply timeout configurability.** 30s hardcoded in phase 3. If a deployer wants longer, they can fork. Add config field in v1.1 if asked.
- **Connection cap on the unix socket.** None in v1. A misbehaving adapter could spawn unlimited connections. Daemon will OOM. **Mitigation:** add a sanity cap of 16 simultaneous subscribers in phase 1's `socket_server.py`. Exceed → reject with `error`.

---

## 14. Glossary

- **Daemon** — the new `agent-waked` process.
- **Adapter** — `agent-wake-claude` (or future `agent-wake-gemini`, etc). Per-harness client of the daemon.
- **Source** — opaque name for an event origin (e.g. `github-actions`). Authenticated by its HMAC secret.
- **Subscriber** — a connected adapter after a successful `hello`/`hello_ack` exchange. Identified by daemon-minted `session_id`.
- **Wake event** — v0 event object on the wire from external systems and from daemon to adapter.
- **Reply event** — v0 reply object on the wire from adapter to daemon to external callback.
