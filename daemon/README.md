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

```json
{
  "version": 1,
  "listen": {"host": "127.0.0.1", "port": 8788},
  "socket_path": null,
  "sources": {
    "github-actions": {
      "secret_env": "AGENT_WAKE_GITHUB_SECRET",
      "callback_url": null
    }
  },
  "default_callback_url": null,
  "routing": {
    "github-actions": {"adapter": "claude"}
  }
}
```

Each source's HMAC secret is read from the environment variable named in `secret_env` (never written to the config file). The environment variable must be set wherever the daemon runs.

`routing` is optional. When absent or empty, all sources are delivered to any connected adapter that subscribed to them (legacy single-adapter mode).

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

## Architecture

See [`design/v1-daemon-spec.md`](../design/v1-daemon-spec.md) §2 for the full architecture diagram and §4 for the wire protocol.
