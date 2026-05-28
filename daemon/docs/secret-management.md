# Secret management in agent-waked

Reference for operators who need to understand the full secret lifecycle, URI semantics, rotation flow, and SIGHUP behaviour.

## Overview

Each inbound source is authenticated via HMAC-SHA256. The shared secret is referenced in `config.json` as a URI — not stored inline. The daemon resolves the URI at startup and caches the result.

Three URI schemes are supported:

| Scheme | Format | Backend |
|---|---|---|
| `env://` | `env://VAR_NAME` | Process environment |
| `vault://` | `vault://PATH#KEY[?ttl=Xm]` | HashiCorp Vault (KV v2) |

## Config forms

All three forms below are valid and mutually exclusive per source:

```jsonc
// 1. Legacy (still fully supported)
{ "secret_env": "AGENT_WAKE_DEMO_SECRET" }

// 2. Single URI (env or vault)
{ "secret": "env://AGENT_WAKE_DEMO_SECRET" }
{ "secret": "vault://secret/data/agent-wake/demo#current" }

// 3. Rotation window — any-of verification, first entry is current
{ "secrets": ["env://AGENT_WAKE_DEMO_NEW", "env://AGENT_WAKE_DEMO_OLD"] }
```

## URI grammar

### env://

```
env://VAR_NAME[?ttl=...]
```

- `VAR_NAME` is the environment variable to read.
- `ttl` is accepted but ignored (env vars are static within a process).
- Cache TTL = ∞ (never expires until `refresh_all` is called on SIGHUP).
- On SIGHUP, the value is re-read — useful if `EnvironmentFile` was edited and the unit restarted since the daemon started.

### vault://

```
vault://PATH#KEY[?ttl=15m]
```

- `PATH` = KV v2 mount + secret path (e.g. `secret/data/agent-wake/src`).
- `KEY` = field name inside the secret data dict (e.g. `current`).
- `ttl` = Go-style duration (`30s`, `5m`, `1h`). Default: 15 minutes.
- Lease-based (dynamic) secrets: TTL = ⅔ × `lease_duration` (overrides `?ttl=`).

KV path convention: the first component of `PATH` is the mount point. For a default Vault install with KV v2 at `secret/`:

```
vault://secret/data/agent-wake/myapp#value
       ^^^^^^  ^^^^^^^^^^^^^^^^^^^^^^^^^ ^^^^
       mount   path                       key
```

## Cache TTL and refresh

| Backend | Default TTL | On SIGHUP |
|---|---|---|
| env | ∞ | Re-read from env var |
| vault-static | 15 min | Re-fetch from Vault |
| vault-dynamic | ⅔ × lease | Re-fetch, new lease |

Per-source override via `?ttl=` query in the URI. The cache key is the full URI string.

## Vault auth: AppRole

The only supported auth method in v1 is AppRole.

- `role_id` lives in `config.json` (non-secret).
- `secret_id` lives in a mode-600 file at `auth.secret_id_file`.
- Login happens lazily at first use. On `Forbidden` / `InvalidRequest`, the backend re-logs-in and retries once.

Hardening: `systemd-creds` integration is documented as a future improvement (Plan 002 out-of-scope). For now, ensure `secret_id_file` is mode 600:

```bash
chmod 600 ~/.config/agent-wake/vault-secret-id
```

## Rotation flow

**Why two-secret windows?**
Senders need time to update their stored secret. The rotation window keeps both current and previous valid, so a brief overlap during rollover doesn't cause 403s.

**Step-by-step:**

```
Before rotate:
  config: { "secret": "env://AGENT_WAKE_DEMO_SECRET" }
  env file: AGENT_WAKE_DEMO_SECRET=<old>

After first rotate:
  config: { "secrets": ["env://AGENT_WAKE_DEMO_NEW", "env://AGENT_WAKE_DEMO_SECRET"] }
  env file: AGENT_WAKE_DEMO_SECRET=<old>
             AGENT_WAKE_DEMO_NEW=<new>
  → both <old> and <new> verify

After second rotate:
  config: { "secrets": ["env://AGENT_WAKE_DEMO_NEW_1", "env://AGENT_WAKE_DEMO_NEW"] }
  → <old> is dropped; only <new> and <new_1> verify
```

**CLI:**
```bash
agent-wake secrets rotate demo
```

The CLI:
1. Generates a new 32-byte secret.
2. Writes it to `secrets.env` (new env-var name).
3. Prepends new URI to `secrets: []`, trims to 2.
4. Atomically saves `config.json`.
5. Sends SIGHUP to the running daemon (no restart needed).
6. Prints the new secret once.

## SIGHUP integration

The daemon already handles SIGHUP for config reload. The resolver's `refresh_all()` is called during reload, so Vault secrets are re-fetched immediately without restart.

For env-backed secrets, `refresh_all` re-reads the env vars — useful after `systemctl --user restart agent-waked` with an updated `EnvironmentFile`.

To trigger manually:
```bash
systemctl --user kill -s SIGHUP agent-waked
```

## Env file

`secrets.env` (`~/.config/agent-wake/secrets.env`) stores env-backend secrets in `VAR=value` format. It is:

- Created automatically by `agent-wake secrets add` / `rotate`.
- Mode 600 enforced on every write.
- Never committed to version control (add to `.gitignore`).

The daemon does not read this file directly. It must be loaded by the systemd unit via `EnvironmentFile`:

```ini
[Service]
EnvironmentFile=-%h/.config/agent-wake/secrets.env
```

The leading `-` makes the file optional (daemon won't fail if it doesn't exist yet).

## Backwards compatibility

The legacy `"secret_env": "VAR_NAME"` form remains fully supported. It is internally converted to `"secret_uris": ["env://VAR_NAME"]` by the config parser and resolved by the env backend. No migration needed for existing configs.
