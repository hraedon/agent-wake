"""``agent-wake secrets`` subcommand implementation.

Manages the secret lifecycle for agent-wake sources.

Commands:
  add    — generate a new secret, write it to a backend, update config.json
  rotate — prepend a new secret to the rotation window, trim to 2 entries
  list   — tabular summary of all sources (no secret material)
  remove — drop a source from config.json (and optionally the backend store)
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from ..config import DEFAULT_CONFIG_PATH, ConfigError, load_config

_DEFAULT_SECRETS_ENV_FILE = Path.home() / ".config" / "agent-wake" / "secrets.env"


# ── argparse wiring ───────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level parser (back-compat for tests that call directly).

    New code should use ``agent_waked.cli.build_parser`` instead.
    """
    parser = argparse.ArgumentParser(
        prog="agent-wake",
        description="agent-wake operator CLI",
    )
    sub = parser.add_subparsers(dest="command")
    _build_secrets_parser(sub)
    return parser


def _build_secrets_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add the ``secrets`` subcommand to an existing subparsers action."""
    secrets_parser = sub.add_parser("secrets", help="Manage source secrets")
    secrets_sub = secrets_parser.add_subparsers(dest="secrets_command")

    # --- list ---
    list_p = secrets_sub.add_parser("list", help="List all sources (no secret values)")
    list_p.add_argument(
        "--json",
        action="store_true",
        help="Emit sources as a JSON document (no secret material)",
    )

    # --- add ---
    add_p = secrets_sub.add_parser("add", help="Add a secret for a new source")
    add_p.add_argument("source", help="Source name")
    add_p.add_argument(
        "--backend",
        choices=["env", "vault"],
        required=True,
        help="Secret backend",
    )
    add_p.add_argument(
        "--name",
        help="Env-var name (env backend only; defaults to AGENT_WAKE_<SOURCE>_SECRET)",
    )
    add_p.add_argument(
        "--path",
        help="Vault KV path (vault backend only)",
    )
    add_p.add_argument(
        "--key",
        default="value",
        help="Vault field name (vault backend only; default: 'value')",
    )

    # --- rotate ---
    rot_p = secrets_sub.add_parser("rotate", help="Rotate a source's secret")
    rot_p.add_argument("source", help="Source name")

    # --- remove ---
    rem_p = secrets_sub.add_parser("remove", help="Remove a source")
    rem_p.add_argument("source", help="Source name")

    # Wire dispatch
    secrets_parser.set_defaults(func=_cmd_secrets_help)
    list_p.set_defaults(func=_cmd_list)
    add_p.set_defaults(func=_cmd_add)
    rot_p.set_defaults(func=_cmd_rotate)
    rem_p.set_defaults(func=_cmd_remove)


def _cmd_secrets_help(args: argparse.Namespace) -> int:
    print("Usage: agent-wake secrets {list,add,rotate,remove}", file=sys.stderr)
    return 1


# ── helpers ───────────────────────────────────────────────────────────────────


def _config_path() -> Path:
    env = os.environ.get("AGENT_WAKE_CONFIG")
    return Path(env) if env else DEFAULT_CONFIG_PATH


def _load_raw_config() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        raise ConfigError(
            f"config file not found at {path}. "
            "Run 'agent-wake init' to create one (future feature)."
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


def _save_raw_config(data: dict[str, Any]) -> None:
    """Atomically write config (temp file + rename), validate before commit."""
    path = _config_path()
    # Validate by asking the parser to parse the new config from a temp file.
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        suffix=".tmp",
        delete=False,
    ) as tmp:
        json.dump(data, tmp, indent=2)
        tmp_path = Path(tmp.name)

    # Validate: set env so load_config picks up the temp file.
    old_env = os.environ.get("AGENT_WAKE_CONFIG")
    os.environ["AGENT_WAKE_CONFIG"] = str(tmp_path)
    try:
        load_config()
    except ConfigError as e:
        tmp_path.unlink(missing_ok=True)
        print(f"Error: new config would be invalid — aborting: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if old_env is None:
            os.environ.pop("AGENT_WAKE_CONFIG", None)
        else:
            os.environ["AGENT_WAKE_CONFIG"] = old_env

    tmp_path.replace(path)


def _gen_secret() -> tuple[str, bytes]:
    """Generate a 32-byte secret. Returns (hex_str, raw_bytes)."""
    raw = secrets.token_bytes(32)
    return raw.hex(), raw


def _sighup_daemon() -> None:
    """Send SIGHUP to the running agent-waked process, if any."""
    try:
        import subprocess
        result = subprocess.run(
            ["pgrep", "-f", "agent.waked"],
            capture_output=True,
            text=True,
        )
        pids = [int(p) for p in result.stdout.strip().split() if p.strip().isdigit()]
        for pid in pids:
            try:
                os.kill(pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
        if pids:
            print(f"Sent SIGHUP to agent-waked (pid {', '.join(str(p) for p in pids)}).")
    except Exception as e:
        print(f"Note: could not send SIGHUP to daemon: {e}", file=sys.stderr)


def _infer_backend(source_cfg: dict[str, Any]) -> str:
    """Infer the backend of an existing source from its secret URIs."""
    uris: list[str] = source_cfg.get("secret_uris", [])
    if not uris:
        return "unknown"
    first = uris[0]
    if first.startswith("env://"):
        return "env"
    if first.startswith("vault://"):
        return "vault"
    return "unknown"


# ── env-file helpers ──────────────────────────────────────────────────────────


def _env_file_path() -> Path:
    env = os.environ.get("AGENT_WAKE_SECRETS_ENV")
    return Path(env) if env else _DEFAULT_SECRETS_ENV_FILE


def _env_file_read() -> dict[str, str]:
    path = _env_file_path()
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def _env_file_append(var_name: str, value: str) -> None:
    path = _env_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    # Enforce 600
    path.chmod(0o600)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{var_name}={value}\n")


def _env_file_update_line(var_name: str, value: str) -> None:
    """Replace the line for var_name in the env file, or append if absent."""
    path = _env_file_path()
    if not path.exists():
        _env_file_append(var_name, value)
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    new_lines: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{var_name}="):
            new_lines.append(f"{var_name}={value}")
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(f"{var_name}={value}")
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _env_file_remove_line(var_name: str) -> None:
    path = _env_file_path()
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    new_lines = [l for l in lines if not l.strip().startswith(f"{var_name}=")]
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# ── commands ──────────────────────────────────────────────────────────────────


def _summarize_sources(sources: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a per-source summary (backend, count, URIs) with no secret material."""
    rows: list[dict[str, Any]] = []
    for name, info in sorted(sources.items()):
        if not isinstance(info, dict):
            continue
        if "secret_env" in info:
            backend = "env"
            uris = [f"env://{info['secret_env']}"]
        elif "secret" in info:
            uri = info["secret"]
            backend = "env" if uri.startswith("env://") else "vault"
            uris = [uri]
        elif "secrets" in info:
            uris_list: list[str] = info["secrets"]
            backend = "env" if uris_list and uris_list[0].startswith("env://") else "vault"
            uris = uris_list
        else:
            backend = "unknown"
            uris = []
        rows.append({"source": name, "backend": backend, "n_secrets": len(uris), "secret_uris": uris})
    return rows


def _cmd_list(args: argparse.Namespace) -> int:
    raw = _load_raw_config()
    sources: dict[str, Any] = raw.get("sources", {})

    if getattr(args, "json", False):
        print(json.dumps({"ok": True, "sources": _summarize_sources(sources)}, indent=2))
        return 0

    if not sources:
        print("No sources configured.")
        return 0

    print(f"{'SOURCE':<20}  {'BACKEND':<8}  {'N_SECRETS':<10}  SECRET_URIS")
    print("-" * 70)
    for row in _summarize_sources(sources):
        uris = row["secret_uris"]
        uri_display = ", ".join(uris[:2])
        if row["n_secrets"] > 2:
            uri_display += f" (+{row['n_secrets']-2} more)"
        print(f"{row['source']:<20}  {row['backend']:<8}  {row['n_secrets']:<10}  {uri_display}")
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    raw = _load_raw_config()
    source: str = args.source
    backend: str = args.backend

    sources: dict[str, Any] = raw.setdefault("sources", {})

    if source in sources:
        print(
            f"Error: source {source!r} already exists. "
            "Use 'agent-wake secrets rotate' to add a new secret.",
            file=sys.stderr,
        )
        return 1

    hex_val, _ = _gen_secret()

    if backend == "env":
        var_name: str = args.name or f"AGENT_WAKE_{source.upper().replace('-', '_')}_SECRET"
        # Check env file for existing entry
        existing = _env_file_read()
        if var_name in existing:
            print(
                f"Error: env var {var_name!r} already exists in {_env_file_path()}. "
                "Remove it first or use --name to pick a different var name.",
                file=sys.stderr,
            )
            return 1
        _env_file_append(var_name, hex_val)
        sources[source] = {"secret": f"env://{var_name}"}
        _save_raw_config(raw)
        print(f"Secret added for source {source!r}.")
        print(f"  Backend : env")
        print(f"  Env var : {var_name}")
        print(f"  Env file: {_env_file_path()}")
        print()
        print("Store this secret value — it will not be shown again:")
        print(f"  {hex_val}")
        print()
        print("Give this value to the sender of wake events for this source.")
        return 0

    elif backend == "vault":
        if not args.path:
            print("Error: --path is required for --backend vault", file=sys.stderr)
            return 1
        vault_path: str = args.path
        key: str = args.key

        raw_cfg = raw.get("vault")
        if not raw_cfg:
            print(
                "Error: 'vault' config block is required for --backend vault. "
                "Add it to config.json first.",
                file=sys.stderr,
            )
            return 1

        try:
            _vault_write(raw_cfg, vault_path, key, hex_val)
        except Exception as e:
            print(f"Error writing to Vault: {e}", file=sys.stderr)
            return 1

        uri = f"vault://{vault_path}#{key}"
        sources[source] = {"secret": uri}
        _save_raw_config(raw)
        print(f"Secret added for source {source!r}.")
        print(f"  Backend   : vault")
        print(f"  Vault path: {vault_path}")
        print(f"  Field     : {key}")
        print()
        print("Store this secret value — it will not be shown again:")
        print(f"  {hex_val}")
        print()
        print("Give this value to the sender of wake events for this source.")
        return 0

    print(f"Unknown backend: {backend}", file=sys.stderr)
    return 1


def _cmd_rotate(args: argparse.Namespace) -> int:
    raw = _load_raw_config()
    source: str = args.source
    sources: dict[str, Any] = raw.get("sources", {})

    if source not in sources:
        print(f"Error: source {source!r} not found in config.", file=sys.stderr)
        return 1

    info: dict[str, Any] = sources[source]
    existing_uris = _source_to_uris(info)
    if not existing_uris:
        print(
            f"Error: source {source!r} has no resolvable URI (backend unsupported for rotate).",
            file=sys.stderr,
        )
        return 1

    # Determine backend from the first URI.
    first_uri = existing_uris[0]
    hex_val, _ = _gen_secret()

    if first_uri.startswith("env://"):
        var_name = f"AGENT_WAKE_{source.upper().replace('-', '_')}_SECRET_NEW"
        # Pick a name that doesn't collide.
        existing_env = _env_file_read()
        n = 0
        candidate = var_name
        while candidate in existing_env:
            n += 1
            candidate = f"{var_name}_{n}"
        var_name = candidate
        _env_file_append(var_name, hex_val)
        new_uri = f"env://{var_name}"

    elif first_uri.startswith("vault://"):
        from urllib.parse import urlparse
        parsed = urlparse(first_uri)
        vault_path = (parsed.netloc + parsed.path).lstrip("/")
        key = parsed.fragment.split("?")[0] if "?" in parsed.fragment else parsed.fragment

        raw_cfg = raw.get("vault")
        if not raw_cfg:
            print("Error: 'vault' config block missing.", file=sys.stderr)
            return 1
        try:
            _vault_write(raw_cfg, vault_path, key, hex_val)
        except Exception as e:
            print(f"Error writing to Vault: {e}", file=sys.stderr)
            return 1
        new_uri = first_uri  # same path, key updated in place

    else:
        print(f"Error: don't know how to rotate backend for URI {first_uri!r}.", file=sys.stderr)
        return 1

    # Build new URI list: prepend new, keep at most one previous, trim to 2.
    new_uris = [new_uri] + existing_uris[:1]

    # Promote to list form in config.
    sources[source] = {k: v for k, v in info.items() if k not in ("secret", "secret_env", "secrets")}
    sources[source]["secrets"] = new_uris

    _save_raw_config(raw)

    print(f"Secret rotated for source {source!r}.")
    print(f"  Current URI : {new_uri}")
    print(f"  Previous URI: {existing_uris[0]}")
    print()
    print("Store this new secret value — it will not be shown again:")
    print(f"  {hex_val}")
    print()
    print("Give this value to the sender of wake events for this source.")
    print("The previous secret remains valid until the next rotation.")

    _sighup_daemon()
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    raw = _load_raw_config()
    source: str = args.source
    sources: dict[str, Any] = raw.get("sources", {})

    if source not in sources:
        print(f"Error: source {source!r} not found in config.", file=sys.stderr)
        return 1

    info: dict[str, Any] = sources[source]
    uris = _source_to_uris(info)

    # Clean up env file entries.
    for uri in uris:
        if uri.startswith("env://"):
            from urllib.parse import urlparse
            var_name = urlparse(uri).netloc
            if var_name:
                _env_file_remove_line(var_name)

    del sources[source]

    # Also clean up routing if present.
    routing: dict[str, Any] = raw.get("routing", {})
    routing.pop(source, None)

    _save_raw_config(raw)
    print(f"Source {source!r} removed from config.")
    return 0


# ── vault write helper ────────────────────────────────────────────────────────


def _vault_write(vault_cfg: dict[str, Any], path: str, key: str, value: str) -> None:
    try:
        import hvac  # type: ignore[import-untyped]
    except ImportError:
        raise RuntimeError("hvac is required for vault backend. pip install agent-waked[vault]")

    import os as _os
    from pathlib import Path as _Path

    auth: dict[str, Any] = vault_cfg["auth"]
    sid_file = _Path(_os.path.expanduser(auth["secret_id_file"]))
    secret_id = sid_file.read_text(encoding="utf-8").strip()

    client = hvac.Client(
        url=vault_cfg["addr"],
        namespace=vault_cfg.get("namespace"),
    )
    client.auth.approle.login(
        role_id=auth["role_id"],
        secret_id=secret_id,
    )

    parts = path.split("/", 1)
    mount = parts[0]
    secret_path = parts[1] if len(parts) > 1 else ""
    client.secrets.kv.v2.create_or_update_secret(
        path=secret_path,
        secret={key: value},
        mount_point=mount,
    )


# ── internal helpers ──────────────────────────────────────────────────────────


def _source_to_uris(info: dict[str, Any]) -> list[str]:
    """Extract URIs from a raw source config dict (any form)."""
    if "secret_env" in info:
        return [f"env://{info['secret_env']}"]
    if "secret" in info:
        return [info["secret"]]
    if "secrets" in info:
        return list(info["secrets"])
    return []
