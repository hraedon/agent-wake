# agent-wake-claude

Claude Code channel plugin for agent-wake external event signaling.

This adapter connects to the **agent-waked daemon** over a unix socket
and receives wake events.  It does **not** bind any TCP port — the
daemon owns the HTTP ingest endpoint (`localhost:8788`).

## Prerequisites

The agent-waked daemon must be running.  See the top-level README for
daemon installation and configuration.

## Install

From the repo root:

```bash
cd adapters/claude
pip install -e .
```

This creates an `agent-wake-claude` command on your PATH.

For development (includes pytest):

```bash
pip install -e ".[dev]"
```

## Configure

The adapter reads the same config file as the daemon:
`~/.config/agent-wake/config.json` (or `AGENT_WAKE_CONFIG`).

```json
{
  "version": 1,
  "listen": {"host": "127.0.0.1", "port": 8788},
  "socket_path": null,
  "sources": {
    "demo": {
      "secret_env": "AGENT_WAKE_DEMO_SECRET",
      "callback_url": null
    }
  },
  "default_callback_url": null
}
```

The adapter only uses `sources` (source names for the hello handshake)
and `socket_path` (daemon socket path).  HMAC secrets, callback URLs,
and listen host/port are daemon-side concerns.

## Run as a Claude Code channel

Three things must all be true, and Claude Code reports none of them
unless you ask for debug output (`--debug-file <path>`, then grep for
`Channel notifications`):

1. **The server is registered as an MCP server.** The `--channels` flag
   selects among registered servers; it does not itself define one:

   ```bash
   claude mcp add agent-wake-claude -s user -- ~/.local/bin/agent-wake-claude
   ```

2. **Channels are enabled by org policy.** On Team/Enterprise accounts
   the default is off, and the only symptom is a debug line reading
   `channels not enabled by org policy`. Enable it in managed settings:

   ```bash
   sudo mkdir -p /etc/claude-code
   echo '{"channelsEnabled": true}' | sudo tee /etc/claude-code/managed-settings.json
   ```

3. **The session is started with the dev-channel flag**, and the
   first-run confirmation prompt is accepted (an unattended session sits
   at that prompt forever, which looks exactly like a silent failure):

   ```bash
   claude --dangerously-load-development-channels server:agent-wake-claude
   ```

A healthy startup logs `MCP server "agent-wake-claude": Channel
notifications registered` and the daemon logs `adapter subscribed
... adapter=claude`. If the daemon logs neither that nor any delivery
while `agent-wake pending list` grows, the leg is broken — see the
router's silent-accumulation warning (WI-011).

Claude Code spawns `agent-wake-claude` as a subprocess.  The adapter
connects to the daemon over the unix socket and relays wake events as
MCP channel notifications.

The MCP `initialize` reply pins protocol revision `2025-03-26` rather
than echoing the client's offer: Claude Code's channel registration
skips connections that negotiated a modern revision "with no unsolicited
notification path", which silently disables every wake (WI-011).

If the daemon is not running when the adapter starts, it logs a
reconnect warning and retries with exponential backoff (1 s → 30 s cap).
It does not crash.

Send `SIGHUP` to the adapter process to reload permission-relay config and
environment-backed signing values without restarting Claude Code. An invalid
replacement is rejected and the last valid config remains active. Source
subscriptions and `socket_path` changes still require an adapter restart.

## Send a test wake event

With the daemon running:

```bash
BODY='{"v":0,"event_id":"01HZXPDEMO0000000000000001","source":"demo","kind":"alert","content":"hello","wake":true,"meta":{}}'
SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$AGENT_WAKE_DEMO_SECRET" | awk '{print $2}')
curl -s -X POST http://127.0.0.1:8788/ \
  -H "Content-Type: application/json" \
  -H "X-AgentWake-Source: demo" \
  -H "X-AgentWake-Signature: sha256=$SIG" \
  -d "$BODY"
```

## Silent inject (`wake: false`)

Claude Code's channel protocol has no `noReply` equivalent: `params` is exactly
`{content, meta}`, and an event delivered to an idle session starts a turn. It
does, however, guarantee that notifications arriving while Claude is busy are
"delivered together on the next turn".

So this adapter implements silent inject as **deferred inject**. A `wake: false`
event is buffered (never dropped, and still acked to the daemon) and emitted at
the next moment a turn is provably already happening:

- immediately before the next `wake: true` event — that turn is happening anyway;
- on a `tools/call` request, which Claude can only issue mid-turn;
- on a permission relay request, which only fires mid-turn.

Flushed events carry `silent="true"`, `flush_reason="..."` and `deferred_ms` as
`<channel>` attributes, so the model can tell context from a fresh alert.

The limitation, stated plainly: if silent events keep arriving and no turn ever
happens, the buffer would grow without bound. At 64 buffered events the adapter
flushes anyway and *does* wake the session, tagged `flush_reason="overflow"`.

All of this is declared machine-readably on `initialize` under
`capabilities.experimental["agent-wake/silent_inject"]` — mode, reason,
flush triggers, overflow behaviour, and `drops_events: false`.

## Reply tool

The adapter exposes a single MCP tool: `agent_wake_reply`.

Arguments:
- `source` (required): the source name used for callback routing.
- `content` (required): text body of the reply.
- `in_reply_to` (optional): the `event_id` being replied to.

The reply is forwarded to the daemon, which POSTs it to the configured
callback URL.  If no callback is configured, the tool returns a graceful
green-path message.

## Permission relay

The adapter forwards Claude Code `notifications/claude/channel/permission_request`
to the `default_callback_url`. When `WAKE_HMAC_SECRET` is configured, requests
are signed using the shared [callback HMAC contract](../../docs/hmac-signing.md).
Ingress source secrets are not used for permission signing.

## Test

```bash
pytest tests/ -v
```
