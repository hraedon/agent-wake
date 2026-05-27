# Substrate webhook → agent-wake

This guide shows how to connect substrate's webhook push delivery to
agent-wake so that workflow events wake your agent session.

## Prerequisites

- Substrate running with a Postgres database
- agent-wake daemon running (`agent-waked`)
- An adapter (Claude Code or opencode) connected to the daemon

## Steps

### 1. Generate a shared secret

```bash
python3 /path/to/agent-wake/tools/generate-secret.py
```

Export it:

```bash
export SUBSTRATE_WEBHOOK_SECRET=<hex-from-above>
```

### 2. Configure agent-wake

Add a `substrate` source to `~/.config/agent-wake/config.json`:

```json
{
  "version": 1,
  "listen": {"host": "127.0.0.1", "port": 8788},
  "sources": {
    "substrate": {
      "secret_env": "SUBSTRATE_WEBHOOK_SECRET",
      "callback_url": "http://127.0.0.1:9999/v1/reply"
    }
  },
  "routing": {
    "substrate": {"adapter": "claude"}
  }
}
```

Restart the daemon (or send SIGHUP to reload config).

### 3. Register the webhook in substrate

```python
import os
from substrate import Substrate

sub = Substrate("postgresql://user:pass@localhost:5432/mydb")
sub.register_webhook(
    url="http://127.0.0.1:8788/",
    headers={
        "X-AgentWake-Source": "substrate",
    },
    sign_secret=os.environ["SUBSTRATE_WEBHOOK_SECRET"].encode(),
    # Optional: filter to specific transitions
    # transitions=["finish", "deploy"],
)
```

### 4. Test it

Transition a work item in substrate. The daemon receives the event,
verifies the HMAC signature, and delivers it to your agent session as
a wake event.

## How it works

```
substrate event (transition)
  │
  ▼
deliver_webhooks() — matches filter, computes HMAC-SHA256
  │
  ▼
POST http://127.0.0.1:8788/
  Headers: X-AgentWake-Source: substrate
           X-AgentWake-Signature: sha256=<hmac>
  Body: {event_id, work_item_id, transition, payload, ...}
  │
  ▼
agent-waked: gating (HMAC verify) → dedupe → router → unix socket
  │
  ▼
adapter: wake event delivered to agent session
```

## Reply flow

If the agent replies (via `agent_wake_reply` tool), the daemon POSTs
the reply to the source's `callback_url`. For substrate, this is
typically the sidecar's reply endpoint.

## Troubleshooting

- **403 from daemon**: Check that `SUBSTRATE_WEBHOOK_SECRET` matches
  in both substrate's webhook registration and agent-wake's config.
- **`no_subscriber`**: The adapter isn't connected or isn't subscribed
  to the `substrate` source. Check the adapter's hello handshake logs.
- **`duplicate`**: The same `event_id` was already seen within the
  dedup window (4096 entries). This is normal on retry.
