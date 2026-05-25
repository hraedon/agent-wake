# agent-wake-opencode

opencode plugin for agent-wake external event signaling.

## Install

From the repo root:

```bash
cd adapters/opencode
bun install
bun run build
```

This produces `dist/index.js`.

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
     "listen": {"host": "127.0.0.1", "port": 8789},
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

3. Add the plugin to your opencode config (`~/.config/opencode/opencode.json`):

   ```json
   {
     "plugin": [
       "/absolute/path/to/agent-wake/adapters/opencode/dist/index.js"
     ]
   }
   ```

## Send a test wake event

```bash
BODY='{"v":0,"event_id":"01HZXPDEMO0000000000000002","source":"demo","kind":"alert","content":"hello","wake":true,"meta":{}}'
SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$AGENT_WAKE_DEMO_SECRET" | awk '{print $2}')
curl -s -X POST http://127.0.0.1:8789/ \
  -H "Content-Type: application/json" \
  -H "X-AgentWake-Source: demo" \
  -H "X-AgentWake-Signature: sha256=$SIG" \
  -d "$BODY"
```

Or run the demo script:

```bash
bash examples/demo.sh
```

## Session tracking

The plugin tracks active sessions via `session.created` / `session.deleted` hooks.
Wake events are delivered to **all** active sessions by default. Per-session routing
is deferred to v1.

## Silent inject

opencode supports `noReply: true` when `wake` is false, so silent-inject events
are delivered without triggering an agent turn.

## Reply tool

The adapter registers an `agent_wake_reply` tool that the agent can call to
send a reply back to the event source. Replies are POSTed to the source's
`callback_url` (best-effort in v0).

## Test

```bash
bun test
npx tsc --noEmit
```

## Known v0 limitations

- In-memory deduplication only; restarts clear the window.
- Replies are best-effort and lost if the session exits before the tool fires.
- No permission relay (opencode lacks the equivalent primitive).
