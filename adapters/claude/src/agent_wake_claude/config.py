"""Configuration loading for agent-wake Claude adapter.

Accepts both v0 and v1 config formats.  The slimmed adapter no longer
resolves HMAC secrets (the daemon owns that); it only extracts source
names for the hello handshake and socket_path for the daemon connection.
"""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("agent_wake_claude.config")

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "agent-wake" / "config.json"
_cached_config: dict | None = None


class ConfigError(Exception):
    pass


def load_config() -> dict:
    global _cached_config
    if _cached_config is not None:
        return _cached_config

    config_path = os.environ.get("AGENT_WAKE_CONFIG")
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH

    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    version = raw.get("version", 0)
    if version not in (0, 1):
        raise ConfigError(
            f"Unsupported config version {version!r}. Only 0 and 1 are accepted."
        )

    listen = raw.get("listen", {})
    host = listen.get("host", "127.0.0.1")
    port = listen.get("port", 8788)

    if host != "127.0.0.1" or port != 8788:
        log.warning(
            "host/port are no longer used by the adapter; "
            "the daemon owns the HTTP listener"
        )

    sources_raw = raw.get("sources", {})
    if not sources_raw:
        raise ConfigError("At least one source must be configured.")

    config = {
        "version": version,
        "host": host,
        "port": port,
        "socket_path": raw.get("socket_path"),
        "sources": {name: {} for name in sources_raw},
        "default_callback_url": raw.get("default_callback_url"),
    }

    _cached_config = config
    return config
