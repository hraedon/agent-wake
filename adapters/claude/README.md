# agent-wake-claude

Claude Code channel plugin for agent-wake external event signaling.

## Setup

1. Install the package:
   ```bash
   cd adapters/claude
   uv pip install -e .
   ```

2. Generate a shared HMAC secret:
   ```bash
   python ../../tools/generate-secret.py
   ```
   Put the output into an environment variable (e.g., `AGENT_WAKE_DEMO_SECRET`).

3. Create a config file at `~/.config/agent-wake/config.json` (or point to it via
   `AGENT_WAKE_CONFIG`). See `../../core/schema.md` for the config shape.

## Running as a Claude Code plugin

Start Claude Code with the channels flag:

```bash
claude --dangerously-load-development-channels server:agent-wake
```

Make sure `agent-wake-claude` is on your `PATH` so Claude Code can spawn it.

## Sending a test wake event

Run the demo script in `examples/demo.sh` or craft a curl manually:

```bash
BODY='{"v":0,"event_id":"01HZXPDEMO0000000000000001","source":"demo","kind":"alert","content":"hello","wake":true}'
SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')
curl -X POST http://127.0.0.1:8788/ \
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

If no `callback_url` is configured for the source, the tool returns a graceful
green-path message noting that the reply was not delivered externally.

## Permission relay

The adapter forwards Claude Code `notifications/claude/channel/permission_request`
to the `default_callback_url`. Verdicts are POSTed back by the external system to
`/permission/verdict` on the adapter's HTTP listener:

```json
{"request_id": "abcde", "behavior": "allow"}
```

## Known v0 limitations

- Silent inject (`wake: false`) is silently dropped because Claude Code channels
always trigger a turn.
- In-memory deduplication only; restarts clear the window.
- Replies are best-effort and lost if the session exits before the tool fires.
