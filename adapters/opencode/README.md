# agent-wake-opencode

opencode plugin for agent-wake external event signaling.

## Setup

1. Install dependencies and build:
   ```bash
   cd adapters/opencode
   bun install     # or npm install
   bun run build   # or npm run build
   ```

2. Generate a shared HMAC secret:
   ```bash
   python ../../tools/generate-secret.py
   ```

3. Create a config file at `~/.config/agent-wake/config.json`.

4. Add the built plugin to your opencode configuration:
   ```json
   {
     "plugins": [
       "./adapters/opencode/dist/index.js"
     ]
   }
   ```

## Sending a test wake event

Run the demo script in `examples/demo.sh` or craft a curl manually:

```bash
BODY='{"v":0,"event_id":"01HZXPDEMO0000000000000002","source":"demo","kind":"alert","content":"hello","wake":true}'
SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')
curl -X POST http://127.0.0.1:8789/ \
  -H "Content-Type: application/json" \
  -H "X-AgentWake-Source: demo" \
  -H "X-AgentWake-Signature: sha256=$SIG" \
  -d "$BODY"
```

## Session tracking

The plugin tracks active sessions via `session.created` / `session.deleted` hooks.
Wake events are delivered to **all** active sessions by default. Per-session routing
is deferred to v1.

## Silent inject

opencode supports `noReply: true` when `wake` is false, so silent-inject events
are delivered without triggering an agent turn.

## Known v0 limitations

- In-memory deduplication only; restarts clear the window.
- Replies are best-effort and lost if the session exits before the tool fires.
- No permission relay (opencode lacks the equivalent primitive).
