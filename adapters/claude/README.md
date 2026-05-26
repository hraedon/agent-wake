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

```bash
claude --dangerously-load-development-channels server:agent-wake-claude
```

Claude Code spawns `agent-wake-claude` as a subprocess.  The adapter
connects to the daemon over the unix socket and relays wake events as
MCP channel notifications.

If the daemon is not running when the adapter starts, it logs a
reconnect warning and retries with exponential backoff (1 s → 30 s cap).
It does not crash.

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
to the `default_callback_url`.

## Test

```bash
pytest tests/ -v
```
