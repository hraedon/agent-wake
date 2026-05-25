# agent-wake-opencode

opencode plugin for agent-wake external event signaling.

> **Package manager:** bun. `bun.lockb` is the source of truth.
> `package-lock.json` is npm-incidental and gitignored.

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

## Secret management

The adapter uses per-source HMAC-SHA256 shared secrets. Secrets live in
environment variables (referenced from `config.json` via `secret_env`) and
must never be written to `config.json` itself.

**Generate a secret:**

```bash
python ../../tools/generate-secret.py
```

This prints a 64-character hex string from `secrets.token_hex(32)`. Set it
as the environment variable named in your config's `secret_env` field. A
template is provided at `.env.example` — copy it to `.env` and fill in
values. Both `.env` files are gitignored.

**Rotation (v0):**

v0 has no zero-downtime rotation. To rotate a secret:

1. Stop the adapter (and opencode if it's running).
2. Generate a new secret with `generate-secret.py`.
3. Update the env var in `.env` (and re-export it in the running shell).
4. Restart opencode so the plugin reloads with the new secret.
5. Update all senders (GitHub Actions, webhooks, etc.) to use the new
   secret. Senders signing with the old secret will be rejected with 403.

Any in-flight events signed with the old secret are dropped; senders
should retry per the schema's retry guidance.

**File permissions:**

If you store secrets in an env file rather than your shell rc, set its
permissions to `0600`:

```bash
chmod 600 .env
```

The adapter does not enforce this — it reads from `process.env`. The
0600 advisory applies to the file you source the env from.

## Known v0 limitations

- In-memory deduplication only; restarts clear the window.
- Replies are best-effort and lost if the session exits before the tool fires.
- No permission relay (opencode lacks the equivalent primitive).
