#!/usr/bin/env bash
set -euo pipefail

# Demo script for agent-wake-claude adapter.
# Assumes the adapter is already running as a Claude Code channel plugin.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== agent-wake-claude curl demo ==="

# Generate a test secret
SECRET=$(python3 "$SCRIPT_DIR/../../../tools/generate-secret.py")
echo "Generated secret: $SECRET"

# Write a temporary config file
CONFIG_DIR=$(mktemp -d)
CONFIG_FILE="$CONFIG_DIR/config.json"
cat > "$CONFIG_FILE" <<EOF
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
EOF

export AGENT_WAKE_CONFIG="$CONFIG_FILE"
export AGENT_WAKE_DEMO_SECRET="$SECRET"

BODY='{"v": 0, "event_id": "01HZXPDEMO0000000000000001", "source": "demo", "kind": "alert", "content": "hello from agent-wake", "wake": true, "meta": {}}'
SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')

echo "Sending wake event..."
curl -s -X POST http://127.0.0.1:8788/ \
  -H "Content-Type: application/json" \
  -H "X-AgentWake-Source: demo" \
  -H "X-AgentWake-Signature: sha256=$SIG" \
  -d "$BODY" | python3 -m json.tool

rm -rf "$CONFIG_DIR"

echo "Done."
