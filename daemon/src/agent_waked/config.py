"""Daemon configuration loading and validation.

Reads ``~/.config/agent-wake/config.json`` (overridable via
``AGENT_WAKE_CONFIG``).  Accepts both v0 and v1 config formats per
Section 5 of the daemon spec.
"""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("agent_waked.config")

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "agent-wake" / "config.json"


class ConfigError(Exception):
    pass


def load_config() -> dict:
    config_path = os.environ.get("AGENT_WAKE_CONFIG")
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH

    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    version = raw.get("version", 0)
    if version not in (0, 1):
        raise ConfigError(f"Unsupported config version {version!r}. Only 0 and 1 are accepted.")

    if version == 0:
        log.warning("config version 0 is deprecated; upgrade to version 1 and add a 'routing' block")

    listen = raw.get("listen", {})
    routing = raw.get("routing", {})
    for source_name, route in routing.items():
        if not isinstance(route, dict):
            raise ConfigError(f"Routing entry for {source_name!r} must be an object.")
        if "adapter" in route and not isinstance(route["adapter"], str):
            raise ConfigError(
                f"Routing entry for {source_name!r}: 'adapter' must be a string."
            )

    cfg = {
        "version": version,
        "listen": {
            "host": listen.get("host", "127.0.0.1"),
            "port": listen.get("port", 8788),
        },
        "socket_path": raw.get("socket_path"),
        "sources": {},
        "default_callback_url": raw.get("default_callback_url"),
        "routing": routing,
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

        cfg["sources"][name] = {
            "secret": secret.encode("utf-8"),
            "callback_url": info.get("callback_url") or cfg["default_callback_url"],
        }

    return cfg
