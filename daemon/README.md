# agent-waked

Central demultiplexer for [agent-wake](https://github.com/anomalyco/agent-wake) external event signaling.

## What it does

- Owns the inbound HTTP port (default `127.0.0.1:8788`) for wake-event ingest.
- Demultiplexes inbound events to adapter subscribers over a unix-domain socket.
- Owns outbound reply delivery (adapters stop making direct HTTPS calls).

## Install

```shell
pip install -e .
```

Or, for development with test dependencies:

```shell
pip install -e ".[dev]"
```

After install, the `agent-waked` console script is available. For a user-level install that systemd can find:

```shell
pip install --user -e .
# Binary lands at ~/.local/bin/agent-waked (matches the systemd unit)
```

## Configure

Configuration lives at `~/.config/agent-wake/config.json` (override via `AGENT_WAKE_CONFIG`).

### Adding a source (recommended: use the CLI)

```bash
agent-wake secrets add demo --backend env
```

This generates a 32-byte random secret, writes it to `~/.config/agent-wake/secrets.env`, updates `config.json`, and prints the secret value once:

```
Secret added for source 'demo'.
  Backend : env
  Env var : AGENT_WAKE_DEMO_SECRET
  Env file: /home/you/.config/agent-wake/secrets.env

Store this secret value — it will not be shown again:
  <64 hex chars>

Give this value to the sender of wake events for this source.
```

Give the printed hex value to whoever will be sending HMAC-signed POST requests for that source.

### Manual config (legacy / advanced)

You can still write config.json by hand. Three secret forms are accepted per source (mutually exclusive):

```jsonc
{
  "version": 1,
  "listen": {"host": "127.0.0.1", "port": 8788},
  "sources": {
    // Legacy: env-var name (still fully supported)
    "demo": { "secret_env": "AGENT_WAKE_DEMO_SECRET" },

    // New: single URI reference
    "github-actions": { "secret": "env://AGENT_WAKE_GITHUB_SECRET" },

    // New: rotation window (any-of HMAC verification)
    "regista": {
      "secrets": [
        "vault://secret/data/agent-wake/regista#current",
        "vault://secret/data/agent-wake/regista#previous"
      ]
    }
  },
  "routing": {
    "github-actions": {"adapter": "claude"}
  }
}
```

`routing` is optional. When absent or empty, all sources are delivered to any connected adapter that subscribed to them.

### Vault backend

For sources stored in HashiCorp Vault, add a `vault` block to config.json and install the optional extra:

```bash
pip install -e ".[vault]"
```

```jsonc
{
  "vault": {
    "addr": "https://vault.internal:8200",
    "namespace": "engineering",
    "auth": {
      "method": "approle",
      "role_id": "your-role-id",
      "secret_id_file": "~/.config/agent-wake/vault-secret-id"
    }
  }
}
```

Then add a vault-backed source:

```bash
agent-wake secrets add myapp --backend vault --path secret/data/agent-wake/myapp --key value
```

### Listing sources

```bash
agent-wake secrets list
```

Output (no secret material printed):

```
SOURCE                BACKEND   N_SECRETS   SECRET_URIS
----------------------------------------------------------------------
demo                  env       1           env://AGENT_WAKE_DEMO_SECRET
```

### Rotating a secret

```bash
agent-wake secrets rotate demo
```

This generates a new secret, prepends it to `secrets: [...]` (auto-promoting from single to list), trims to 2 entries (current + previous), prints the new value once, and sends SIGHUP to the running daemon. Both old and new secrets verify during the transition window.

After the next rotation, the previous secret is dropped. The key flows for this are covered in [`docs/secret-management.md`](docs/secret-management.md).

### Removing a source

```bash
agent-wake secrets remove demo
```

Removes the source from `config.json` and cleans up the env-file entry.

## Run

### Foreground

```shell
agent-waked
```

Or, without installing: `python -m agent_waked`.

Send `SIGTERM` or `SIGINT` for graceful shutdown (5-second drain cap).

### systemd (recommended)

Install the user unit:

```shell
mkdir -p ~/.config/systemd/user
cp systemd/agent-waked.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-waked
```

Check status:

```shell
systemctl --user status agent-waked
```

View logs:

```shell
journalctl --user -u agent-waked -f
```

The unit file sets `PYTHONUNBUFFERED=1` so logs appear immediately in the journal.

## Live config reload (SIGHUP)

Send `SIGHUP` to reload configuration without restarting:

```shell
systemctl --user kill -s SIGHUP agent-waked
# Or, for a foreground process:
kill -HUP $(pgrep -f agent-waked)
```

What changes on reload:

| Change | Effect |
|---|---|
| New source added | Addressable immediately |
| Source removed | Returns 403 on next ingest |
| Routing changed | New events use updated routing |
| `listen.host` / `listen.port` changed | **Ignored** — logged as warning, requires restart |
| `socket_path` changed | **Ignored** — logged as warning, requires restart |

Existing adapter connections are preserved. Their `accepted_sources` is recomputed only on the next `hello_ack` (not retroactively pushed).

If the new config file is invalid, the reload is skipped and an error is logged. The daemon continues running with the previous config.

## Debug

**Daemon won't start — "another agent-waked instance holds …":**
Another process holds the lock file (`<socket_path>.lock`). Stop the existing instance first, or remove the stale lock file if the previous process crashed:

```shell
systemctl --user stop agent-waked
# Or manually:
rm $XDG_RUNTIME_DIR/agent-wake.sock.lock
```

**"Config file not found":**
Set `AGENT_WAKE_CONFIG` or create `~/.config/agent-wake/config.json`.

**Adapter can't connect:**
Verify the socket file exists and has mode `0600`:
```shell
ls -la $XDG_RUNTIME_DIR/agent-wake.sock
```

Check that the adapter's `socket_path` config (if set) matches the daemon's.

**Increase log verbosity:**
```shell
AGENT_WAKE_LOG_LEVEL=DEBUG agent-waked
# Or for systemd:
systemctl --user edit agent-waked
# Add:
# [Service]
# Environment=AGENT_WAKE_LOG_LEVEL=DEBUG
systemctl --user restart agent-waked
```

**`no_subscriber` in logs:**
An event was accepted but no adapter was subscribed for that source. This is a normal 202 response — the sender should not retry (it would just produce duplicates). Start the adapter and verify it subscribes to the correct source names.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `AGENT_WAKE_CONFIG` | `~/.config/agent-wake/config.json` | Config file path |
| `AGENT_WAKE_LOG_LEVEL` | `INFO` | Log verbosity |
| `XDG_RUNTIME_DIR` | — | Unix socket discovery |

## Testing

```shell
pytest -q
```

## Suite integration

agent-wake is a Tier 2 suite component (optional plumbing). When deployed
as part of the agent suite, it conforms to the suite config and doctor
contracts.

### Suite config (suite.env)

agent-wake reads the canonical `REGISTA_*` env vars from the suite config
file (`suite.env`), with the standard precedence:

1. Process environment
2. Per-user `~/.config/agent-suite/suite.env` (override via `$AGENT_SUITE_CONFIG`)
3. System `/etc/agent-suite/suite.env`

This is informational for agent-wake today — the daemon's own config is
`config.json`, not `suite.env`. The suite integration surfaces in `doctor`
and `install-harness`.

### Doctor (`agent-wake doctor`)

Run health checks:

```bash
agent-wake doctor            # human-readable
agent-wake doctor --json     # suite-shaped JSON for aggregation
```

Checks performed:

| Check | Description |
|---|---|
| `config_present` | config.json exists and loads cleanly |
| `ingress_reachable` | daemon HTTP port responds |
| `auth_configured` | at least one source has a secret (not open) |
| `adapters_installed` | at least one adapter binary on PATH |
| `allowlist_present` | sources with principal_id have identity allowlists |

When suite.env is present, the doctor also reports regista connectivity.

### Dossier human notifications

Dossier's authenticated notification mode emits an HMAC-signed v0 wake event
with `wake: false` and the destination principal in `meta.target`. Configure a
dedicated source and explicitly allow its service identity and human targets:

```json
{
  "version": 1,
  "sources": {
    "dossier": {
      "secret_uris": ["env://DOSSIER_WAKE_SECRET"],
      "allowed_trigger_identities": ["service:dossier"],
      "allowed_target_principals": ["human:reviewer"]
    }
  },
  "routing": {},
  "delivery": {
    "human:reviewer": {
      "email": {
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "from_addr": "agent-suite@example.com",
        "to_addr": "reviewer@example.com",
        "use_tls": true,
        "secret_uri": "env://SMTP_PASSWORD"
      }
    }
  }
}
```

The dossier process uses the same secret through
`DOSSIER_NOTIFICATION_SECRET_REF` (for example
`env:DOSSIER_WAKE_SECRET`), sets `DOSSIER_NOTIFICATION_SOURCE=dossier`, and
sets `DOSSIER_NOTIFICATION_IDENTITY=service:dossier`. Ingress authenticates
the exact body, checks the service-identity allowlist, then checks the target
allowlist before dispatching. The event remains eligible for human delivery
without prompting an attached agent session.

### Install-harness (`agent-wake install-harness`)

Wire wake adapters into harness configs (part of the suite bootstrap):

```bash
agent-wake install-harness claude          # wire claude adapter
agent-wake install-harness opencode        # wire opencode plugin
agent-wake install-harness all             # wire both
agent-wake install-harness claude --dry-run  # preview changes
agent-wake install-harness claude --uninstall  # remove wiring
```

This sets `AGENT_WAKE_CONFIG` in the harness env block and registers the
opencode plugin path. It is idempotent (re-running is a no-op) and
preserves existing harness config (merge, not overwrite).

### Container deployment

```bash
cd daemon
docker build -t agent-waked .
docker run -d --name agent-waked \
  -p 127.0.0.1:8788:8788 \
  -v ~/.config/agent-wake:/config:ro \
  -e AGENT_WAKE_CONFIG=/config/config.json \
  agent-waked
```

### Windows Service

```bash
pip install -e ".[windows]"
python daemon/windows_service.py install
python daemon/windows_service.py start
```

## Architecture

See [`design/v1-daemon-spec.md`](../design/v1-daemon-spec.md) §2 for the full architecture diagram and §4 for the wire protocol.

## Known limitations

- **Live sessions only.** Wake events are delivered to currently-connected adapters. If no adapter is subscribed for a source, the event returns `202 no_subscriber` and is not queued. There is no durable inbox — missed events are lost. A per-session inbox (Signal-With-Start semantics) is a v1.5+ consideration tied to regista's durable storage.
- **In-memory dedupe.** The 4096-event dedupe window is lost on daemon restart. A retry-after-restart may produce a duplicate wake. Durable dedupe is deferred to v1 with regista's involvement.
- **No retries on reply delivery.** The outbox POSTs once with a 30-second timeout. Permanent failures are logged at warning level. Durable reply retry is a v1.1+ concern.
- **Single Unix user per daemon.** The unix socket is protected by filesystem mode (`0600`). Any local process running as the daemon's user can subscribe as any adapter family. Per-user isolation is a v2 concern (see `design/identity-and-multi-user.md`).
- **Identity allowlists are unauthenticated.** The `X-AgentWake-Identity` header is trusted after HMAC verification of the source. The identity is the source's declared `principal_id`, not a cryptographic proof from the sender. Full cryptographic identity (Ed25519 signatures per sender) requires regista BC-196 and is a v2 feature.
