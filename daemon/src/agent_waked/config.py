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

import ipaddress
import json
import logging
import os
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("agent_waked.config")

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "agent-wake" / "config.json"

_VALID_SCHEMES = {"env", "vault"}

_VALID_CHANNEL_KINDS = {"webhook", "email"}

# RFC 6598 shared address space (CGNAT) — is_private is False for this range,
# so it must be checked explicitly to prevent SSRF via a CGNAT-internal target.
_CGNAT_RANGE = ipaddress.ip_network("100.64.0.0/10")


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

    with open(path, encoding="utf-8") as f:
        try:
            raw = json.load(f)
        except json.JSONDecodeError as e:
            # Do not chain the original exception: JSONDecodeError retains
            # the full document text in .doc, which may carry secrets or
            # sensitive metadata. Surface only line/msg.
            raise ConfigError(
                f"Config file {path} is not valid JSON near line {e.lineno}: {e.msg}"
            )

    if not isinstance(raw, dict):
        raise ConfigError(
            f"Config file {path} must be a JSON object (got {type(raw).__name__})."
        )

    for nested_field in ("sources", "routing"):
        nested = raw.get(nested_field)
        if nested is not None and not isinstance(nested, dict):
            raise ConfigError(
                f"Config file {path}: '{nested_field}' must be an object "
                f"(got {type(nested).__name__})."
            )

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
        "state": _validate_state_block(raw.get("state")),
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

        # Plan 005: delivery authorization — a source may only deliver to
        # principals it explicitly declares. Default-deny: if a source posts
        # an event with meta.target but has no allowed_target_principals, the
        # delivery is rejected (403), not silently routed.
        allowed_targets = info.get("allowed_target_principals")
        if allowed_targets is not None:
            if not isinstance(allowed_targets, list) or not allowed_targets:
                raise ConfigError(
                    f"Source {name!r}: 'allowed_target_principals' must be a "
                    f"non-empty list of principal_id strings."
                )
            for t in allowed_targets:
                if not isinstance(t, str) or not t:
                    raise ConfigError(
                        f"Source {name!r}: each entry in "
                        f"'allowed_target_principals' must be a non-empty string."
                    )

        cfg["sources"][name] = {
            "secret_uris": secret_uris,
            "callback_url": info.get("callback_url") or cfg["default_callback_url"],
            "principal_id": info.get("principal_id"),
            "allowed_trigger_identities": info.get("allowed_trigger_identities"),
            "allowed_target_principals": allowed_targets,
        }

    if needs_vault and not vault_cfg:
        raise ConfigError(
            "One or more sources use vault:// URIs but no 'vault' block is present in config."
        )

    if vault_cfg is not None:
        _validate_vault_block(vault_cfg)

    # Plan 005 WI-1.1: delivery routing table (principal_id → channels).
    delivery_raw = raw.get("delivery")
    if delivery_raw is not None:
        delivery = _validate_principals_block(delivery_raw)
        if _principals_need_vault(delivery):
            needs_vault = True
        if needs_vault and not vault_cfg:
            raise ConfigError(
                "One or more delivery channels use vault:// URIs but no "
                "'vault' block is present in config."
            )
        cfg["delivery"] = delivery
    else:
        cfg["delivery"] = {}

    return cfg


_VALID_DELIVERY_MODES = {"live_only", "next_session", "managed_session"}

_STATE_INT_FIELDS = (
    "dedupe_ttl_seconds",
    "dedupe_max_rows",
    "pending_ttl_seconds",
    "pending_max_rows",
    "pending_max_attempts",
)


def _validate_state_block(state: object) -> dict[str, Any]:
    """Validate the optional ``state`` block (durable store: BC-WAKE-004/012).

    All keys are optional; an absent block means "defaults" and, crucially,
    ``default_delivery = live_only`` so existing deployments keep the
    documented v0 semantics until an operator opts in.
    """
    if state is None:
        return {}
    if not isinstance(state, dict):
        raise ConfigError("'state' must be an object.")

    result: dict[str, Any] = {}

    state_dir = state.get("dir")
    if state_dir is not None:
        if not isinstance(state_dir, str) or not state_dir:
            raise ConfigError("'state.dir' must be a non-empty path string.")
        result["dir"] = state_dir

    enabled = state.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("'state.enabled' must be a boolean.")
    result["enabled"] = enabled

    for field in _STATE_INT_FIELDS:
        val = state.get(field)
        if val is None:
            continue
        if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
            raise ConfigError(f"'state.{field}' must be a positive integer.")
        result[field] = val

    mode = state.get("default_delivery")
    if mode is not None:
        if mode not in _VALID_DELIVERY_MODES:
            raise ConfigError(
                f"'state.default_delivery' must be one of "
                f"{sorted(_VALID_DELIVERY_MODES)} (got {mode!r})."
            )
        result["default_delivery"] = mode

    return result


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


def _validate_principals_block(principals: object) -> dict[str, dict[str, dict[str, Any]]]:
    """Validate the ``principals`` routing table (Plan 005 WI-1.1).

    Each principal maps ``principal_id`` → one or more delivery channel
    configs.  Channel configs are validated per-kind by ``_validate_channel``.
    Returns a normalised copy.
    """
    if not isinstance(principals, dict):
        raise ConfigError(
            "'delivery' must be an object mapping principal_id → channels."
        )

    result: dict[str, dict[str, dict[str, Any]]] = {}
    for pid, channels in principals.items():
        if not isinstance(pid, str) or not pid:
            raise ConfigError("principal_id keys must be non-empty strings.")
        if not isinstance(channels, dict):
            raise ConfigError(f"Principal {pid!r}: channels must be an object.")
        validated: dict[str, dict[str, Any]] = {}
        for kind, cfg in channels.items():
            if kind not in _VALID_CHANNEL_KINDS:
                raise ConfigError(
                    f"Principal {pid!r}: unknown channel kind {kind!r}. "
                    f"Supported: {sorted(_VALID_CHANNEL_KINDS)}"
                )
            if not isinstance(cfg, dict):
                raise ConfigError(
                    f"Principal {pid!r}: channel {kind!r} must be an object."
                )
            validated[kind] = _validate_channel(kind, cfg, pid)
        result[pid] = validated
    return result


def _validate_channel(
    kind: str, cfg: dict[str, Any], pid: str
) -> dict[str, Any]:
    """Validate a single delivery channel config (returns a normalised copy)."""
    if kind == "webhook":
        return _validate_webhook_channel(cfg, pid)
    if kind == "email":
        return _validate_email_channel(cfg, pid)
    raise ConfigError(
        f"Principal {pid!r}: unknown channel kind {kind!r}. "
        f"Supported: {sorted(_VALID_CHANNEL_KINDS)}"
    )


def _resolve_hostname(host: str) -> list[str]:
    """Resolve a hostname to its IP address strings.

    Module-level so tests can monkeypatch DNS (the offline sandbox has no
    resolver). Returns the list of resolved address strings.
    """
    infos = socket.getaddrinfo(host, None)
    return [str(info[4][0]) for info in infos]


def _assert_safe_webhook_url(url: str, pid: str) -> None:
    """Reject webhook URLs that resolve to loopback/private/reserved ranges (SSRF).

    A webhook URL is an outbound target an authenticated source can steer. Without
    this check, a sender could route wake deliveries to ``127.0.0.1``,
    ``169.254.169.254`` (cloud metadata), or internal services. The hostname is
    resolved and every resolved address is checked against forbidden ranges.
    An unresolvable hostname is rejected (the webhook would be unusable and
    could mask a rebinding attack).
    """
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise ConfigError(f"Principal {pid!r} webhook: invalid URL {url!r}: {e}")
    host = parsed.hostname
    if not host:
        raise ConfigError(f"Principal {pid!r} webhook: URL {url!r} has no hostname.")
    try:
        addrs = _resolve_hostname(host)
    except OSError as e:
        raise ConfigError(
            f"Principal {pid!r} webhook: hostname {host!r} does not resolve: {e}"
        )
    if not addrs:
        raise ConfigError(
            f"Principal {pid!r} webhook: hostname {host!r} resolved to no addresses."
        )
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
            or ip in _CGNAT_RANGE
        ):
            raise ConfigError(
                f"Principal {pid!r} webhook: URL hostname {host!r} resolves to a "
                f"forbidden address {addr} (loopback/private/reserved). Outbound "
                f"webhooks must target a public host."
            )


def _validate_webhook_channel(cfg: dict[str, Any], pid: str) -> dict[str, Any]:
    url = cfg.get("url")
    if not isinstance(url, str) or not url:
        raise ConfigError(f"Principal {pid!r} webhook: 'url' is required.")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ConfigError(
            f"Principal {pid!r} webhook: URL must use http or https "
            f"(got scheme {parsed.scheme!r})."
        )
    _assert_safe_webhook_url(url, pid)

    secret_uri = cfg.get("secret_uri")
    if not isinstance(secret_uri, str) or not secret_uri:
        raise ConfigError(
            f"Principal {pid!r} webhook: 'secret_uri' is required "
            "(signing secret for the outbound webhook)."
        )
    _parse_uri(secret_uri, f"{pid}/webhook")

    return {"url": url, "secret_uri": secret_uri}


def _validate_email_channel(cfg: dict[str, Any], pid: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in ("smtp_host", "from_addr", "to_addr"):
        val = cfg.get(field)
        if not isinstance(val, str) or not val:
            raise ConfigError(f"Principal {pid!r} email: '{field}' is required.")
        result[field] = val

    port = cfg.get("smtp_port", 587)
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise ConfigError(
            f"Principal {pid!r} email: 'smtp_port' must be an integer 1-65535."
        )
    result["smtp_port"] = port

    use_tls = cfg.get("use_tls", True)
    if not isinstance(use_tls, bool):
        raise ConfigError(f"Principal {pid!r} email: 'use_tls' must be a boolean.")
    result["use_tls"] = use_tls

    secret_uri = cfg.get("secret_uri")
    if secret_uri is not None:
        if not isinstance(secret_uri, str) or not secret_uri:
            raise ConfigError(
                f"Principal {pid!r} email: 'secret_uri' must be a non-empty string."
            )
        _parse_uri(secret_uri, f"{pid}/email")
        result["secret_uri"] = secret_uri

    return result


def _principals_need_vault(principals: dict[str, dict[str, dict[str, Any]]]) -> bool:
    """True if any delivery channel references a vault:// secret URI."""
    for channels in principals.values():
        for cfg in channels.values():
            secret_uri = cfg.get("secret_uri")
            if isinstance(secret_uri, str) and secret_uri.startswith("vault://"):
                return True
    return False
