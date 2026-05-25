# agent-wake-claude

Claude Code channel plugin for agent-wake external event signaling.

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

1. Generate a shared HMAC secret:
   ```bash
   python ../../tools/generate-secret.py
   ```
   Export it as an environment variable (e.g., `export AGENT_WAKE_DEMO_SECRET=<output>`).

2. Create `~/.config/agent-wake/config.json` (or set `AGENT_WAKE_CONFIG` to a custom path):

   ```json
   {
     "version": 0,
     "listen": {"host": "127.0.0.1", "port": 8788},
     "sources": {
       "demo": {
         "secret_env": "AGENT_WAKE_DEMO_SECRET",
         "callback_url": null
       }
     },
     "default_callback_url": null
   }
   ```

   See [`core/schema.md`](../../core/schema.md) for the full config spec and
   [`core/examples/config.json`](../../core/examples/config.json) for a ready-to-copy example.

## Run as a Claude Code channel

```bash
claude --dangerously-load-development-channels server:agent-wake-claude
```

Claude Code spawns `agent-wake-claude` as a subprocess and communicates via
JSON-RPC over stdio (the MCP channels research-preview protocol).

## Send a test wake event

```bash
BODY='{"v":0,"event_id":"01HZXPDEMO0000000000000001","source":"demo","kind":"alert","content":"hello","wake":true,"meta":{}}'
SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$AGENT_WAKE_DEMO_SECRET" | awk '{print $2}')
curl -s -X POST http://127.0.0.1:8788/ \
  -H "Content-Type: application/json" \
  -H "X-AgentWake-Source: demo" \
  -H "X-AgentWake-Signature: sha256=$SIG" \
  -d "$BODY"
```

Or run the demo script:

```bash
bash examples/demo.sh
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

## Test

```bash
pytest tests/ -v
python ../../tools/fakechat-test.py
```

## Known v0 limitations

- Silent inject (`wake: false`) is silently dropped because Claude Code channels
  always trigger a turn.
- In-memory deduplication only; restarts clear the window.
- Replies are best-effort and lost if the session exits before the tool fires.
