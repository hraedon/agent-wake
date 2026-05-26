# GitHub Actions webhook → agent-wake example

This example shows how to send CI events (e.g., "build failed on main") from a
GitHub Actions workflow to your local agent-wake adapter so your agent is woken
and can respond.

---

## Prerequisites

1. The `agent-waked` daemon is running on a machine reachable from GitHub
   Actions (or via a tunnel like ngrok for local testing).  The daemon owns the
   HTTP ingest port (default `127.0.0.1:8788`).  See the
   [main README](../README.md#quick-start) for installation.
2. You have a shared HMAC secret generated:
   ```bash
   python tools/generate-secret.py
   ```
3. Your `~/.config/agent-wake/config.json` includes a `github-actions` source:
   ```json
   {
     "version": 1,
     "listen": {"host": "127.0.0.1", "port": 8788},
     "sources": {
       "github-actions": {
         "secret_env": "AGENT_WAKE_GITHUB_SECRET",
         "callback_url": null
       }
     },
     "routing": {}
   }
   ```
4. The `AGENT_WAKE_GITHUB_SECRET` environment variable is set wherever the
   daemon runs.

---

## Option A: Inline curl in your workflow

Add a step at the end of your `.github/workflows/ci.yml`:

```yaml
      - name: Notify agent-wake on failure
        if: failure()
        run: |
          BODY=$(jq -n \
            --arg event_id "gh-$(date +%s)" \
            --arg source "github-actions" \
            --arg content "Build failed on ${GITHUB_REF_NAME}: ${{ github.event.head_commit.message }}" \
            '{v: 0, event_id: $event_id, source: $source, kind: "alert", content: $content, wake: true, meta: {}}')
          SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "${AGENT_WAKE_SECRET}" | awk '{print $2}')
          curl -s -X POST "${AGENT_WAKE_URL}/" \
            -H "Content-Type: application/json" \
            -H "X-AgentWake-Source: github-actions" \
            -H "X-AgentWake-Signature: sha256=$SIG" \
            -d "$BODY"
        env:
          AGENT_WAKE_URL: ${{ secrets.AGENT_WAKE_URL }}
          AGENT_WAKE_SECRET: ${{ secrets.AGENT_WAKE_SECRET }}
```

Store `AGENT_WAKE_URL` and `AGENT_WAKE_SECRET` in your repository's **Actions
secrets** (`Settings → Secrets and variables → Actions`).

---

## Option B: Reusable composite action

If you want to call agent-wake from many workflows, create a composite action
(`.github/actions/agent-wake-notify/action.yml`):

```yaml
name: 'Notify agent-wake'
description: 'Send a wake event to the agent-wake ingest endpoint'
inputs:
  url:
    description: 'agent-wake ingest URL'
    required: true
  secret:
    description: 'HMAC shared secret'
    required: true
  source:
    description: 'Event source name'
    required: true
    default: 'github-actions'
  kind:
    description: 'Event kind'
    required: true
    default: 'alert'
  content:
    description: 'Event content (human-readable)'
    required: true
runs:
  using: "composite"
  steps:
    - run: |
        BODY=$(jq -n \
          --arg event_id "gh-$(date +%s)-${GITHUB_RUN_ID}" \
          --arg source "${{ inputs.source }}" \
          --arg kind "${{ inputs.kind }}" \
          --arg content "${{ inputs.content }}" \
          '{v: 0, event_id: $event_id, source: $source, kind: $kind, content: $content, wake: true, meta: {}}')
        SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "${{ inputs.secret }}" | awk '{print $2}')
        curl -s -X POST "${"{{ inputs.url }}"}/" \
          -H "Content-Type: application/json" \
          -H "X-AgentWake-Source: ${"{{ inputs.source }}"}" \
          -H "X-AgentWake-Signature: sha256=$SIG" \
          -d "$BODY"
      shell: bash
```

Usage in a workflow:

```yaml
      - uses: ./.github/actions/agent-wake-notify
        if: failure()
        with:
          url: ${{ secrets.AGENT_WAKE_URL }}
          secret: ${{ secrets.AGENT_WAKE_SECRET }}
          content: "Build failed on ${{ github.ref_name }}"
```

---

## Security notes

- Rotate the `AGENT_WAKE_SECRET` regularly.
- Do **not** log the secret or the raw curl command in Actions logs.
- Pin the ingest URL to HTTPS in production (even though the default is
  `127.0.0.1:8788` for local testing).
- Treat the ingest endpoint as a prompt-injection vector: any system that can
  sign a valid HMAC can wake your agent and feed it arbitrary content.

---

## See also

- [`core/schema.md`](../../core/schema.md) — wire format
- [`adapters/claude/README.md`](../../adapters/claude/README.md) — Claude adapter setup
