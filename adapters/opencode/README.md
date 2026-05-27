# agent-wake-opencode

opencode plugin for [agent-wake](../..). Daemon-client edition.

This plugin **does not** bind a port. It connects to `agent-waked` over a
unix socket and delivers wake events into live opencode sessions via the
SDK's `session.promptAsync`. The reply tool forwards `agent_wake_reply`
calls to the daemon, which owns the outbound HTTPS POST.

## Why this replaces the archived adapter

The previous opencode adapter (`adapters/opencode.archived/`) bound its
own HTTP port (8788), collided with the Claude adapter when both ran on
the same host, and called the **synchronous** `client.session.prompt(…)`
to wake sessions. That call blocks until the agent finishes responding,
which deadlocked the plugin event loop and manifested as "the event
never arrives." This rewrite uses `session.promptAsync` ("start if
needed and return immediately"), which is the correct wake primitive.
See the [Quirks](#quirks-worth-knowing) section for two more landmines
that took longer to find than the headline bug.

## Install

Prerequisite: `agent-waked` is running and `~/.config/agent-wake/config.json`
exists (see [`daemon/README.md`](../../daemon/README.md)).

```bash
cd adapters/opencode
bun install
bun run build
```

Then register the plugin in your opencode config — the path to
`dist/index.js` becomes a `plugin` entry in opencode's config (see
opencode plugin docs).

## Configuration

The adapter reads the same config file as the daemon
(`~/.config/agent-wake/config.json`, override with `AGENT_WAKE_CONFIG`).
It only uses:

- `sources` — the list of source names (used in the daemon `hello`
  handshake to subscribe to events).
- `socket_path` — optional explicit unix-socket path. If unset, the
  adapter resolves `$XDG_RUNTIME_DIR/agent-wake.sock` or falls back to
  `~/.local/state/agent-wake/agent-wake.sock`.

HMAC secrets and callback URLs are owned by the daemon. The adapter
never sees them.

## Session targeting

By default a wake event is delivered to **every** live opencode session
(via `session.list` → `session.promptAsync` per session). If the event
carries `meta.session_id`, only that session is targeted.

`wake: true` (default) triggers a turn. `wake: false` injects silently
(`noReply: true`) for the next turn.

## Reply tool

The plugin registers a tool named `agent_wake_reply`:

```
agent_wake_reply(source: string, content: string, in_reply_to?: string)
```

Behaviour:

- `"sent"` — daemon POSTed successfully (2xx).
- `"sent (no callback_url configured)"` — source has no callback URL.
- `"reply delivery failed: <reason>"` — daemon returned a non-2xx,
  the request timed out, or the daemon connection is down.

## Tests

```bash
bun test
```

Tests run against a mock daemon (an in-process unix-socket server) and
a mock opencode SDK client. There is no live opencode dependency in
the unit suite.

## End-to-end smoke test

`tools/opencode-smoke-test.py` (at the repo root) runs the full chain
against a real `opencode serve`:

```bash
# from the agent-wake repo root, with `opencode` on PATH:
python3 tools/opencode-smoke-test.py
```

The script spins up a temporary `agent-waked` daemon and an isolated
`opencode serve`, registers this plugin, creates a session via the
opencode HTTP API, POSTs an HMAC-signed wake event to the daemon's
ingest port, then polls `/session/{id}/message` for the `<wake>` tag.
PASS on delivery, FAIL with daemon + opencode logs on the failure
case. Not wired into CI (opencode binary isn't available there);
it's the gate before tagging.

## Quirks worth knowing

Three things were learned the hard way during this rewrite — preserved
in code comments and called out here so a future maintainer doesn't
relearn them.

1. **Only the default export is allowed.** opencode iterates every
   named export and treats each as a plugin, calling it during
   config-hook resolution. A non-Plugin named export (e.g. a test
   helper) crashes the loader with
   `undefined is not an object (evaluating 'O.config')` — and that
   crash silently poisons subsequent `session.prompt_async` writes for
   the lifetime of the server (symptom: 204 + empty session.messages,
   no error surfaced to the caller).

2. **`session.promptAsync` is the wake primitive.** The synchronous
   `session.prompt` blocks until the model finishes responding, which
   deadlocks the plugin event loop. The archived plugin used
   `session.prompt`; that is why "the wake never arrived."

3. **The SDK does not throw on 4xx.** `promptAsync` returns
   `{ data, error, response }` with `error` set on non-2xx. Check it
   explicitly — `await client.session.promptAsync(...)` succeeding does
   NOT mean the request succeeded.
