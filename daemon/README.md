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

Each source's HMAC secret is read from the environment variable named in `secret_env` (never written to the config file).

`routing` is optional. When absent or empty, all sources are delivered to any connected adapter that subscribed to them (legacy single-adapter mode).

## Run

```shell
python -m agent_waked
```

Or, after install, the `agent-waked` console script.

Send `SIGTERM` or `SIGINT` for graceful shutdown (5-second drain cap).

## systemd

Install the user unit:

```shell
cp systemd/agent-waked.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-waked
```

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
