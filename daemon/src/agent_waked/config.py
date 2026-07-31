"""Daemon configuration loading and validation.

Reads ``~/.config/agent-wake/config.json`` (overridable via
``AGENT_WAKE_CONFIG``).  Accepts v0, v1 and v2 config formats.

Two vocabularies, one model
---------------------------
v2 (WI-006) splits what v0/v1 called a *source* into the three concepts it was
actually carrying — see :mod:`agent_waked.addressing` for why:

  ``senders``      credential: HMAC secret + trigger-identity allowlist
  ``principals``   actor identity (``principal_id``) + out-of-band channels
  ``destinations`` an adapter, optionally one session of it, with its own cap
  ``routes``       sender → principal → destination(s)

A v0/v1 file (``sources`` + ``routing`` + ``delivery``) still loads, unchanged
and without a warning about anything the operator has to do today: it is
translated into the v2 model by :func:`addressing.derive_from_legacy` at load,
so exactly one model reaches the rest of the daemon.  The output dict also
keeps ``sources`` / ``routing`` / ``delivery`` as aliases of the normalised
data, because a rolling upgrade has an old ``agent-wake doctor`` reading a new
daemon's ``/health`` and vice versa.

Mixing the two vocabularies for the *same* concept (``sources`` and ``senders``
in one file) is a hard error with a migration message: there is no correct way
to merge two spellings of the same table, and silently preferring one is how a
credential quietly stops being enforced.  ``agent-wake config migrate`` prints
the v2 form of an existing file.

Secret forms accepted per-sender (mutually exclusive):
  - ``secret_env``: legacy — env-var name; normalised to
    ``secret_uris`` = ["env://<name>"].
  - ``secret``: single URI string (``env://`` or ``vault://``).
  - ``secrets``: list of URI strings (rotation window).

Loading validates **shape only** and never reads secret material: whether a
referenced secret is actually readable depends on which process is asking, so it
is not a property of the config.  ``secrets.visibility`` answers that question
and ``main._require_resolvable_secrets`` is where it is fatal (the daemon signs,
so the daemon is the one that must be able to read).

Output shape for each sender:
  ``cfg["senders"][name]`` = ``{"secret_uris": [...], "callback_url": ...,
  "identity": ..., "allowed_trigger_identities": ...}``
"""

import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import addressing, netguard

log = logging.getLogger("agent_waked.config")

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "agent-wake" / "config.json"

_VALID_SCHEMES = {"env", "vault"}

_VALID_CHANNEL_KINDS = {"webhook", "email"}

#: Highest config format this build understands.
CURRENT_CONFIG_VERSION = 2

#: Legacy key → v2 key, for the "you cannot use both" check and the migration
#: hint.  Order matters only for message readability.
_VOCABULARY_PAIRS = (
    ("sources", "senders"),
    ("routing", "routes"),
    ("delivery", "principals"),
)


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

    for nested_field in ("sources", "routing", "senders", "destinations", "principals"):
        nested = raw.get(nested_field)
        if nested is not None and not isinstance(nested, dict):
            raise ConfigError(
                f"Config file {path}: '{nested_field}' must be an object "
                f"(got {type(nested).__name__})."
            )

    version = raw.get("version", 0)
    if version not in (0, 1, 2):
        raise ConfigError(
            f"Unsupported config version {version!r}. This build understands "
            f"0, 1 and {CURRENT_CONFIG_VERSION}."
        )

    if version == 0:
        log.warning(
            "config version 0 is deprecated; "
            "upgrade to version 1 and add a 'routing' block"
        )

    _reject_mixed_vocabulary(raw, path)

    listen = _validate_listen_block(raw.get("listen"))

    uses_v2 = any(new in raw for _, new in _VOCABULARY_PAIRS)

    if uses_v2:
        routing: dict[str, Any] = {}
        sources = raw.get("senders", {})
        if not sources:
            raise ConfigError(
                "At least one sender must be configured ('senders' is empty)."
            )
    else:
        routing = raw.get("routing", {})
        for source_name, route in routing.items():
            if not isinstance(route, dict):
                raise ConfigError(f"Routing entry for {source_name!r} must be an object.")
            if "adapter" in route and not isinstance(route["adapter"], str):
                raise ConfigError(
                    f"Routing entry for {source_name!r}: 'adapter' must be a string."
                )
        sources = raw.get("sources", {})
        if not sources:
            raise ConfigError("At least one source must be configured.")

    noun = "Sender" if uses_v2 else "Source"

    vault_cfg = raw.get("vault")

    cfg: dict[str, Any] = {
        "version": version,
        "listen": {
            "host": listen.get("host", "127.0.0.1"),
            "port": listen.get("port", 8788),
        },
        "socket_path": raw.get("socket_path"),
        "senders": {},
        "default_callback_url": raw.get("default_callback_url"),
        "routing": routing,
        "vault": vault_cfg,
        "state": _validate_state_block(raw.get("state")),
    }
    # ``sources`` is the same object as ``senders``, not a copy: an alias that
    # can drift is worse than no alias, because half the daemon would then
    # authenticate against a stale credential table.
    cfg["sources"] = cfg["senders"]

    needs_vault = False

    for name, info in sources.items():
        if not isinstance(info, dict):
            raise ConfigError(f"{noun} {name!r} must be an object.")

        has_secret_env = "secret_env" in info
        has_secret = "secret" in info
        has_secrets = "secrets" in info

        count = sum([has_secret_env, has_secret, has_secrets])
        if count == 0:
            raise ConfigError(
                f"{noun} {name!r} must have exactly one of: "
                "'secret_env', 'secret', or 'secrets'."
            )
        if count > 1:
            raise ConfigError(
                f"{noun} {name!r}: 'secret_env', 'secret', and 'secrets' "
                "are mutually exclusive — use exactly one."
            )

        if has_secret_env:
            # Legacy form: convert to URI so the resolver can handle it uniformly.
            #
            # The env var is deliberately NOT read here. Loading config validates
            # *shape*; reading secret material is the resolver's job, and only the
            # daemon — the component that actually signs — needs it. Resolving
            # eagerly made ``load_config`` fail closed in every process that
            # lacked the daemon's environment, which made ``agent-wake doctor``
            # report a perfectly healthy estate red from the suite's root-owned
            # scheduled alert-check (WI-003). It was also asymmetric: the
            # equivalent ``"secret": "env://NAME"`` spelling was never eagerly
            # resolved, so the strictness depended on which spelling an operator
            # happened to use.
            #
            # The daemon's refusal to run on a secret it cannot read now lives in
            # ``main._require_resolvable_secrets`` (startup *and* SIGHUP reload),
            # which covers all three secret forms rather than only this one.
            secret_env = info["secret_env"]
            if not isinstance(secret_env, str) or not secret_env:
                raise ConfigError(
                    f"{noun} {name!r}: 'secret_env' must be a non-empty string."
                )
            secret_uris = [f"env://{secret_env}"]

        elif has_secret:
            uri = info["secret"]
            if not isinstance(uri, str) or not uri:
                raise ConfigError(
                    f"{noun} {name!r}: 'secret' must be a non-empty string URI."
                )
            _parse_uri(uri, name)
            secret_uris = [uri]

        else:  # has_secrets
            uris = info["secrets"]
            if not isinstance(uris, list) or not uris:
                raise ConfigError(
                    f"{noun} {name!r}: 'secrets' must be a non-empty list of URI strings."
                )
            secret_uris = []
            for u in uris:
                if not isinstance(u, str):
                    raise ConfigError(
                        f"{noun} {name!r}: each entry in 'secrets' must be a string."
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
                    f"{noun} {name!r}: 'allowed_target_principals' must be a "
                    f"non-empty list of principal_id strings."
                )
            for t in allowed_targets:
                if not isinstance(t, str) or not t:
                    raise ConfigError(
                        f"{noun} {name!r}: each entry in "
                        f"'allowed_target_principals' must be a non-empty string."
                    )

        # A bare string here would silently become a *substring* allowlist:
        # gating.check_trigger_identity does ``header not in allowed``, so
        # "alice" as a string admits the sender "ali". Reject the shape at
        # load rather than weakening authentication at request time.
        allowed_identities = info.get("allowed_trigger_identities")
        if allowed_identities is not None:
            if not isinstance(allowed_identities, list):
                raise ConfigError(
                    f"{noun} {name!r}: 'allowed_trigger_identities' must be a "
                    f"list of principal_id strings (got "
                    f"{type(allowed_identities).__name__}). A bare string "
                    f"would match any sender whose identity is a substring "
                    f"of it."
                )
            for ident in allowed_identities:
                if not isinstance(ident, str) or not ident:
                    raise ConfigError(
                        f"{noun} {name!r}: each entry in "
                        f"'allowed_trigger_identities' must be a non-empty "
                        f"string."
                    )

        identity = info.get("identity", info.get("principal_id"))
        if identity is not None and (not isinstance(identity, str) or not identity):
            raise ConfigError(
                f"{noun} {name!r}: 'identity' must be a non-empty principal_id "
                f"string."
            )

        cfg["senders"][name] = {
            "secret_uris": secret_uris,
            "callback_url": info.get("callback_url") or cfg["default_callback_url"],
            # ``identity`` is the v2 spelling; ``principal_id`` is kept as an
            # alias so the doctor's allowlist check and any operator tooling
            # that reads it keep working across the rename.
            "identity": identity,
            "principal_id": identity,
            "allowed_trigger_identities": allowed_identities,
            "allowed_target_principals": allowed_targets,
        }

    if needs_vault and not vault_cfg:
        raise ConfigError(
            f"One or more {noun.lower()}s use vault:// URIs but no 'vault' "
            f"block is present in config."
        )

    if vault_cfg is not None:
        _validate_vault_block(vault_cfg)

    # ── the addressing model ────────────────────────────────────────────────
    #
    # Exactly one of two paths runs, and both end in the same four normalised
    # keys.  Anything downstream of here sees only the v2 vocabulary.
    if uses_v2:
        principals_raw = raw.get("principals") or {}
        principals = _validate_principals_v2(principals_raw)
        destinations = _validate_destinations_block(
            raw.get("destinations"), principals
        )
        routes = _validate_routes_block(
            raw.get("routes"), cfg["senders"], destinations, principals
        )
        delivery_alias = {
            pid: entry["channels"] for pid, entry in principals.items()
        }
    else:
        # Plan 005 WI-1.1 spelling: top-level ``delivery`` is a principal_id →
        # channels table.  Under v2 the same data is ``principals[pid].channels``.
        delivery_raw = raw.get("delivery")
        delivery_alias = (
            _validate_channel_table(delivery_raw) if delivery_raw is not None else {}
        )
        derived = addressing.derive_from_legacy(
            cfg["senders"], cfg["routing"], delivery_alias
        )
        destinations = derived["destinations"]
        routes = derived["routes"]
        principals = derived["principals"]

    if _channel_table_needs_vault(delivery_alias):
        needs_vault = True
        if not vault_cfg:
            raise ConfigError(
                "One or more delivery channels use vault:// URIs but no "
                "'vault' block is present in config."
            )

    cfg["principals"] = principals
    cfg["destinations"] = destinations
    cfg["routes"] = routes
    cfg["delivery"] = delivery_alias

    return cfg


def _reject_mixed_vocabulary(raw: dict[str, Any], path: Path) -> None:
    """Refuse a file that spells the same table both ways.

    There is no safe merge.  Preferring ``senders`` would silently drop a
    ``sources`` credential (a sender that stops being accepted, i.e. events
    disappear); preferring ``sources`` would silently drop the new one.  Both
    failures are quiet, and one of them is a security regression, so this is a
    hard error that names the fix.
    """
    conflicts = [
        (old, new) for old, new in _VOCABULARY_PAIRS if old in raw and new in raw
    ]
    if not conflicts:
        return
    pairs = "; ".join(f"{old!r} and {new!r}" for old, new in conflicts)
    raise ConfigError(
        f"Config file {path} mixes the v1 and v2 addressing vocabularies: "
        f"{pairs}. These are two spellings of the same table and cannot be "
        f"merged safely. Keep one: run 'agent-wake config migrate' to print "
        f"the v2 form of this file, or delete the v2 key(s) to stay on v1 "
        f"(v1 keeps working, and is translated internally)."
    )


def _validate_destinations_block(
    raw: object, principals: dict[str, Any]
) -> dict[str, Any]:
    """Validate the v2 ``destinations`` table.

    A destination is *the* per-addressee object the v1 model lacked, so this is
    where a per-addressee resource limit finally has somewhere to live
    (``max_connections``, BC-WAKE-010) and where session scoping is declared
    (``session``, BC-001).
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("'destinations' must be an object keyed by destination name.")
    out: dict[str, Any] = {}
    for name, info in raw.items():
        if not isinstance(name, str) or not name:
            raise ConfigError("destination names must be non-empty strings.")
        if not isinstance(info, dict):
            raise ConfigError(f"Destination {name!r} must be an object.")

        adapter = info.get("adapter")
        if adapter is not None and (not isinstance(adapter, str) or not adapter):
            raise ConfigError(
                f"Destination {name!r}: 'adapter' must be a non-empty string "
                f"(or omitted, meaning any adapter that claims this name)."
            )

        session = info.get("session")
        if session is not None and (not isinstance(session, str) or not session):
            raise ConfigError(
                f"Destination {name!r}: 'session' must be a non-empty session "
                f"identifier string (or omitted for any session of the adapter)."
            )
        if session is not None and adapter is None:
            raise ConfigError(
                f"Destination {name!r}: 'session' requires 'adapter' — a session "
                f"identifier is only meaningful within one adapter."
            )

        principal = info.get("principal")
        if principal is not None:
            if not isinstance(principal, str) or not principal:
                raise ConfigError(
                    f"Destination {name!r}: 'principal' must be a non-empty "
                    f"principal_id string."
                )
            if principal not in principals:
                raise ConfigError(
                    f"Destination {name!r} names principal {principal!r}, which "
                    f"is not declared in 'principals'. Add it (channels may be "
                    f"empty: {{\"{principal}\": {{}}}}) so attribution and "
                    f"out-of-band delivery agree on one table."
                )

        cap = info.get("max_connections")
        if cap is not None:
            if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
                raise ConfigError(
                    f"Destination {name!r}: 'max_connections' must be a positive "
                    f"integer (got {cap!r})."
                )

        out[name] = {
            "adapter": adapter,
            "session": session,
            "principal": principal,
            "max_connections": cap,
        }
    return out


def _validate_routes_block(
    raw: object,
    senders: dict[str, Any],
    destinations: dict[str, Any],
    principals: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate the v2 ``routes`` list (sender → principal → destination(s)).

    A list, not a name-keyed map, and that is the point: the v1 table could
    express exactly one adapter per source name, which is precisely why
    multi-target fan-out (BC-WAKE-022) needed a schema change to exist at all.
    Several routes may share a sender, and one route may name several
    destinations; both compose into the sender's reachable set.

    Every reference is checked at load.  A route naming a destination that does
    not exist is a silent black hole at request time — the sender gets a
    cheerful 202 and nothing is ever woken.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError(
            "'routes' must be a list of {sender, principal?, destinations?} objects."
        )
    out: list[dict[str, Any]] = []
    for idx, entry in enumerate(raw):
        where = f"routes[{idx}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be an object.")

        sender_name = entry.get("sender")
        if not isinstance(sender_name, str) or not sender_name:
            raise ConfigError(f"{where}: 'sender' is required and must be a string.")
        if sender_name != "*" and sender_name not in senders:
            raise ConfigError(
                f"{where}: sender {sender_name!r} is not declared in 'senders'."
            )

        principal = entry.get("principal")
        if principal is not None:
            if not isinstance(principal, str) or not principal:
                raise ConfigError(f"{where}: 'principal' must be a non-empty string.")
            if principal not in principals:
                raise ConfigError(
                    f"{where}: principal {principal!r} is not declared in "
                    f"'principals'."
                )

        dests = entry.get("destinations")
        if dests is None:
            dests = []
        if isinstance(dests, str):
            dests = [dests]
        if not isinstance(dests, list):
            raise ConfigError(
                f"{where}: 'destinations' must be a list of destination names."
            )
        for d in dests:
            if not isinstance(d, str) or not d:
                raise ConfigError(
                    f"{where}: each entry in 'destinations' must be a non-empty "
                    f"string."
                )
            if d not in destinations:
                raise ConfigError(
                    f"{where}: destination {d!r} is not declared in "
                    f"'destinations'. A route to a destination that does not "
                    f"exist accepts events and wakes nobody."
                )

        if not dests and principal is None:
            raise ConfigError(
                f"{where}: a route needs 'destinations', 'principal', or both. "
                f"'principal' alone means every destination belonging to that "
                f"principal."
            )
        if principal is not None and dests:
            mismatched = [
                d for d in dests if destinations[d].get("principal") != principal
            ]
            if mismatched:
                raise ConfigError(
                    f"{where}: principal {principal!r} does not own "
                    f"destination(s) {', '.join(sorted(mismatched))}. Either "
                    f"drop 'principal' (the destinations already carry it) or "
                    f"fix the destination's own 'principal'."
                )

        out.append(
            {
                "sender": sender_name,
                "principal": principal,
                "destinations": list(dests),
                "origin": addressing.ORIGIN_CONFIG,
            }
        )
    return out


def _validate_principals_v2(raw: object) -> dict[str, Any]:
    """Validate the v2 ``principals`` table.

    ``principals[pid]`` = ``{"channels": {...}}`` where channels is the same
    per-kind webhook/email config the v1 top-level ``delivery`` block held.  An
    empty object is legitimate and common: a principal that only ever gets woken
    in-band through a destination has no out-of-band channels at all, but it is
    still an actor whose identity destinations and routes refer to.
    """
    if not isinstance(raw, dict):
        raise ConfigError(
            "'principals' must be an object mapping principal_id → "
            "{channels: {...}}."
        )
    out: dict[str, Any] = {}
    for pid, entry in raw.items():
        if not isinstance(pid, str) or not pid:
            raise ConfigError("principal_id keys must be non-empty strings.")
        if entry is None:
            out[pid] = {"channels": {}}
            continue
        if not isinstance(entry, dict):
            raise ConfigError(f"Principal {pid!r} must be an object.")
        unknown = set(entry) - {"channels"}
        if unknown:
            raise ConfigError(
                f"Principal {pid!r}: unknown key(s) {', '.join(sorted(unknown))}. "
                f"Delivery channels go under 'channels'."
            )
        channels = entry.get("channels")
        if channels is None:
            out[pid] = {"channels": {}}
            continue
        if not isinstance(channels, dict):
            raise ConfigError(f"Principal {pid!r}: 'channels' must be an object.")
        out[pid] = {"channels": _validate_channels_for(pid, channels)}
    return out


_VALID_DELIVERY_MODES = {"live_only", "next_session", "managed_session"}

_STATE_INT_FIELDS = (
    "dedupe_ttl_seconds",
    "dedupe_max_rows",
    "pending_ttl_seconds",
    "pending_max_rows",
    "pending_max_attempts",
    "dead_letter_ttl_seconds",
    "dead_letter_max_rows",
)


def _validate_listen_block(listen: object) -> dict[str, Any]:
    """Validate the optional ``listen`` block.

    Loud here because the alternative is quiet: an unparseable port used to reach
    ``resolve_listen`` as a ``ValueError`` (an uncaught traceback in the daemon)
    and to reach the doctor as an ``OSError`` from ``create_connection``, which
    the doctor reported as the benign-sounding "daemon not reachable". A port
    that cannot be a port is a config defect and should be named as one.
    """
    if listen is None:
        return {}
    if not isinstance(listen, dict):
        raise ConfigError(
            f"'listen' must be an object (got {type(listen).__name__})."
        )
    host = listen.get("host")
    if host is not None and (not isinstance(host, str) or not host):
        raise ConfigError("'listen.host' must be a non-empty string.")
    port = listen.get("port")
    if port is not None:
        if not isinstance(port, int) or isinstance(port, bool):
            raise ConfigError(
                f"'listen.port' must be an integer (got {type(port).__name__}: "
                f"{port!r})."
            )
        if not (1 <= port <= 65535):
            raise ConfigError(f"'listen.port' {port} is out of range (1-65535).")
    return listen


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


def _validate_channel_table(principals: object) -> dict[str, dict[str, dict[str, Any]]]:
    """Validate a ``principal_id`` → channels table (the v1 ``delivery`` block).

    Under v2 the same per-principal channel map lives at
    ``principals[pid].channels`` and is validated by the shared
    :func:`_validate_channels_for`.  One validator, two spellings — so a
    deployment cannot get stricter or laxer checking by choosing a vocabulary.
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
        result[pid] = _validate_channels_for(pid, channels)
    return result


def _validate_channels_for(
    pid: str, channels: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Validate one principal's delivery channels; returns a normalised copy."""
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
    return validated


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

    Kept as a module-level indirection so tests can monkeypatch DNS (the
    offline sandbox has no resolver). Returns the list of resolved address
    strings.
    """
    return netguard.resolve_hostname(host)


def _assert_safe_webhook_url(url: str, pid: str) -> None:
    """Reject webhook URLs that resolve to loopback/private/reserved ranges (SSRF).

    A webhook URL is an outbound target an authenticated source can steer. Without
    this check, a sender could route wake deliveries to ``127.0.0.1``,
    ``169.254.169.254`` (cloud metadata), or internal services. The hostname is
    resolved and every resolved address is checked against forbidden ranges.
    An unresolvable hostname is rejected (the webhook would be unusable and
    could mask a rebinding attack).

    This runs at **config load only**. It is a fail-fast check on operator
    input, not the delivery-time guard: a hostname can be re-pointed at an
    internal address after the daemon has started. ``WebhookChannel.deliver``
    re-runs the same range predicate (``netguard.acheck_url``) before every
    POST, which is where rebinding is actually mitigated.
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
    bad = netguard.forbidden_address(addrs)
    if bad is not None:
        raise ConfigError(
            f"Principal {pid!r} webhook: URL hostname {host!r} resolves to a "
            f"forbidden address {bad} (loopback/private/reserved). Outbound "
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


def _channel_table_needs_vault(
    principals: dict[str, dict[str, dict[str, Any]]],
) -> bool:
    """True if any delivery channel references a vault:// secret URI."""
    for channels in principals.values():
        for cfg in channels.values():
            secret_uri = cfg.get("secret_uri")
            if isinstance(secret_uri, str) and secret_uri.startswith("vault://"):
                return True
    return False
