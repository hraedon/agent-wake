#!/usr/bin/env bash
set -euo pipefail

# Demo script: sends a test wake event to the running agent-waked daemon.
# Requires AGENT_WAKE_DEMO_SECRET to be set (matching the daemon's config).

if [ -z "${AGENT_WAKE_DEMO_SECRET:-}" ]; then
  echo "ERROR: AGENT_WAKE_DEMO_SECRET must be set. Generate one with:"
  echo "  python3 tools/generate-secret.py"
  echo "Then add it to ~/.config/agent-wake/config.json and export the env var."
  exit 1
fi

DAEMON_URL="${AGENT_WAKE_URL:-http://127.0.0.1:8788}"
SOURCE="${AGENT_WAKE_SOURCE:-demo}"

BODY=$(jq -n \
  --arg event_id "demo-$(date +%s)" \
  --arg source "$SOURCE" \
  '{v: 0, event_id: $event_id, source: $source, kind: "demo", content: "hello from agent-wake demo.sh", wake: true, meta: {}}')
SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$AGENT_WAKE_DEMO_SECRET" | awk '{print $2}')

echo "Sending wake event to $DAEMON_URL ..."
curl -s -X POST "$DAEMON_URL/" \
  -H "Content-Type: application/json" \
  -H "X-AgentWake-Source: $SOURCE" \
  -H "X-AgentWake-Signature: sha256=$SIG" \
  -d "$BODY" | python3 -m json.tool

echo "Done."
