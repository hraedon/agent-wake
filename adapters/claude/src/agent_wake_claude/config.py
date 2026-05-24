"""Configuration loading for agent-wake Claude adapter."""

import json
import os
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "agent-wake" / "config.json"
_cached_config: dict | None = None
# NOTE: Config is cached on first load and never invalidated.
# Changing the config file requires restarting the adapter.


class ConfigError(Exception):
    """Raised when the configuration is invalid or missing required values."""


def load_config() -> dict:
    """Load and validate the agent-wake configuration file.

    Reads JSON from AGENT_WAKE_CONFIG env var or the default config path.
    Resolves secret_env references to actual secrets via os.environ.
    """
    global _cached_config
    if _cached_config is not None:
        return _cached_config

    config_path = os.environ.get("AGENT_WAKE_CONFIG")
    if config_path:
        path = Path(config_path)
    else:
        path = DEFAULT_CONFIG_PATH

    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if raw.get("version") != 0:
        raise ConfigError("Unsupported config version. Only version 0 is supported.")

    listen = raw.get("listen", {})
    config = {
        "version": 0,
        "host": listen.get("host", "127.0.0.1"),
        "port": listen.get("port", 8788),
        "sources": {},
        "default_callback_url": raw.get("default_callback_url"),
    }

    sources = raw.get("sources", {})
    if not sources:
        raise ConfigError("At least one source must be configured.")

    for name, info in sources.items():
        if not isinstance(info, dict):
            raise ConfigError(f"Source {name!r} must be an object.")

        secret_env = info.get("secret_env")
        if not secret_env or not isinstance(secret_env, str):
            raise ConfigError(f"Source {name!r} must have a 'secret_env' string field.")

        secret = os.environ.get(secret_env)
        if secret is None:
            raise ConfigError(
                f"Source {name!r} references secret_env {secret_env!r} which is not set"
            )

        config["sources"][name] = {
            "secret": secret.encode("utf-8"),
            "callback_url": info.get("callback_url") or config["default_callback_url"],
        }

    _cached_config = config
    return config
