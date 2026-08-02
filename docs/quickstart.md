# Quickstart: receive a wake in two minutes

This path runs a local daemon and Claude Code adapter. It requires Python 3.11+
and Claude Code 2.1.80 or newer.

## 1. Install and configure

From the repository root:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e daemon/ -e adapters/claude/
agent-wake secrets add quickstart --backend env
```

The last command creates the source configuration and stores a generated HMAC
secret in `~/.config/agent-wake/secrets.env`. It prints the secret once; do not
paste it into `config.json`.

## 2. Start the daemon and adapter

In terminal 1, from the repository root:

```bash
. .venv/bin/activate
set -a
. ~/.config/agent-wake/secrets.env
set +a
agent-waked
```

In terminal 2:

```bash
. .venv/bin/activate
claude --dangerously-load-development-channels server:agent-wake-claude
```

Keep both processes running.

## 3. Send the first wake

In terminal 3, from the repository root:

```bash
set -a
. ~/.config/agent-wake/secrets.env
set +a
BODY='{"v":0,"event_id":"quickstart-001","source":"quickstart","kind":"alert","content":"hello from agent-wake","meta":{},"wake":true}'
SIG=$(printf %s "$BODY" | openssl dgst -sha256 -hmac "$AGENT_WAKE_QUICKSTART_SECRET" | cut -d' ' -f2)
curl --fail-with-body -X POST http://127.0.0.1:8788/ \
  -H 'Content-Type: application/json' \
  -H 'X-AgentWake-Source: quickstart' \
  -H "X-AgentWake-Signature: sha256=$SIG" \
  --data "$BODY"
```

The response should contain `"status":"queued"`, and the Claude Code session
should receive `hello from agent-wake` as a channel event.

If the response says `no_subscriber`, wait for the adapter to log that it
subscribed, then send a new event with a different `event_id`. If the request is
forbidden, confirm terminal 1 loaded `secrets.env` before starting the daemon.
