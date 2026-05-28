"""Daemon configuration loading and validation.

Reads ``~/.config/agent-wake/config.json`` (overridable via
``AGENT_WAKE_CONFIG``).  Accepts both v0 and v1 config formats per
Section 5 of the daemon spec.

Secret forms accepted per-source (mutually exclusive):
  - ``secret_env``: legacy — env-var name; eagerly resolved here for
    backwards-compat, converted to ``secret_uris`` = ["env://<name>"].
  - ``secret``: single URI string (``env://`` or ``vault://``).
  - ``secrets``: list of URI strings (rotation window).

Output shape for each source:
  ``cfg["sources"][name]`` = ``{"secret_uris": [...], "callback_url": ...}``
"""

import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("agent_waked.config")

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "agent-wake" / "config.json"

_VALID_SCHEMES = {"env", "vault"}


class ConfigError(Exception):
    pass


def _parse_uri(uri: str, source_name: str) -> str:
    """Validate a secret URI and return it unchanged."""
    try:
        parsed = urlparse(uri)
    except Exception as e:
        raise ConfigError(
            f"Source {source_name!r}: invalid URI {uri!r}: {e}"
        )
    if parsed.scheme not in _VALID_SCHEMES:
        raise ConfigError(
            f"Source {source_name!r}: unsupported URI scheme {parsed.scheme!r} "
            f"in {uri!r}. Supported schemes: {sorted(_VALID_SCHEMES)}"
        )
    if not parsed.netloc and not parsed.path:
        raise ConfigError(
            f"Source {source_name!r}: URI {uri!r} is missing a host/path."
        )
    return uri


def _has_vault_uri(uris: list[str]) -> bool:
    return any(u.startswith("vault://") for u in uris)


def load_config() -> dict[str, Any]:
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

    vault_cfg = raw.get("vault")

    cfg: dict[str, Any] = {
        "version": version,
        "listen": {
            "host": listen.get("host", "127.0.0.1"),
            "port": listen.get("port", 8788),
        },
        "socket_path": raw.get("socket_path"),
        "sources": {},
        "default_callback_url": raw.get("default_callback_url"),
        "routing": routing,
        "vault": vault_cfg,
    }

    sources = raw.get("sources", {})
    if not sources:
        raise ConfigError("At least one source must be configured.")

    needs_vault = False

    for name, info in sources.items():
        if not isinstance(info, dict):
            raise ConfigError(f"Source {name!r} must be an object.")

        has_secret_env = "secret_env" in info
        has_secret = "secret" in info
        has_secrets = "secrets" in info

        count = sum([has_secret_env, has_secret, has_secrets])
        if count == 0:
            raise ConfigError(
                f"Source {name!r} must have exactly one of: "
                "'secret_env', 'secret', or 'secrets'."
            )
        if count > 1:
            raise ConfigError(
                f"Source {name!r}: 'secret_env', 'secret', and 'secrets' "
                "are mutually exclusive — use exactly one."
            )

        if has_secret_env:
            # Legacy form: convert to URI so the resolver can handle it uniformly.
            secret_env = info["secret_env"]
            if not isinstance(secret_env, str) or not secret_env:
                raise ConfigError(f"Source {name!r}: 'secret_env' must be a non-empty string.")
            # Eagerly validate env var is set (preserves old behaviour).
            secret_val = os.environ.get(secret_env)
            if secret_val is None:
                raise ConfigError(
                    f"Source {name!r} references secret_env {secret_env!r} which is not set"
                )
            secret_uris = [f"env://{secret_env}"]

        elif has_secret:
            uri = info["secret"]
            if not isinstance(uri, str) or not uri:
                raise ConfigError(f"Source {name!r}: 'secret' must be a non-empty string URI.")
            _parse_uri(uri, name)
            secret_uris = [uri]

        else:  # has_secrets
            uris = info["secrets"]
            if not isinstance(uris, list) or not uris:
                raise ConfigError(
                    f"Source {name!r}: 'secrets' must be a non-empty list of URI strings."
                )
            secret_uris = []
            for u in uris:
                if not isinstance(u, str):
                    raise ConfigError(
                        f"Source {name!r}: each entry in 'secrets' must be a string."
                    )
                _parse_uri(u, name)
                secret_uris.append(u)

        if _has_vault_uri(secret_uris):
            needs_vault = True

        cfg["sources"][name] = {
            "secret_uris": secret_uris,
            "callback_url": info.get("callback_url") or cfg["default_callback_url"],
            "principal_id": info.get("principal_id"),
            "allowed_trigger_identities": info.get("allowed_trigger_identities"),
        }

    if needs_vault and not vault_cfg:
        raise ConfigError(
            "One or more sources use vault:// URIs but no 'vault' block is present in config."
        )

    if vault_cfg is not None:
        _validate_vault_block(vault_cfg)

    return cfg


def _validate_vault_block(vault: object) -> None:
    if not isinstance(vault, dict):
        raise ConfigError("'vault' config block must be an object.")
    if not isinstance(vault.get("addr"), str) or not vault["addr"]:
        raise ConfigError("'vault.addr' must be a non-empty string.")
    auth = vault.get("auth")
    if auth is None:
        raise ConfigError("'vault.auth' is required.")
    if not isinstance(auth, dict):
        raise ConfigError("'vault.auth' must be an object.")
    method = auth.get("method")
    if method != "approle":
        raise ConfigError(
            f"'vault.auth.method' must be 'approle' (got {method!r})."
        )
    if not isinstance(auth.get("role_id"), str) or not auth["role_id"]:
        raise ConfigError("'vault.auth.role_id' must be a non-empty string.")
    if not isinstance(auth.get("secret_id_file"), str) or not auth["secret_id_file"]:
        raise ConfigError("'vault.auth.secret_id_file' must be a non-empty string.")
