"""agent-wake doctor — health checks and suite-shaped JSON output.

Implements the suite doctor contract (agent-suite-blueprint.md §2.4,
bootstrap-contract.md §3).  ``agent-wake doctor --json`` emits:

    {
      "component": "agent-wake",
      "version": "0.1.0",
      "ok": true|false,
      "degraded": false,
      "regista": { "reachable": bool, "project": str|null, "chain_ok": bool|null } | null,
      "checks": [ { "name": str, "status": "ok"|"fail"|"warn"|"skip", "detail": str } ]
    }

The umbrella ``agent-suite doctor`` shells this command and folds it into
the aggregate report.

Exit code: 0 if all checks pass, 1 if any check fails.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    from importlib.metadata import version as _pkg_version
    _VERSION = _pkg_version("agent-waked")
except Exception:
    _VERSION = "0.1.0"

from . import suite_config
from .config import DEFAULT_CONFIG_PATH, ConfigError, load_config

# ── check helpers ─────────────────────────────────────────────────────────────


def _check_config_file() -> tuple[str, str]:
    """Check: config.json exists and loads cleanly."""
    config_path_env = os.environ.get("AGENT_WAKE_CONFIG")
    config_path = Path(config_path_env) if config_path_env else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return "fail", f"config file not found: {config_path}"
    try:
        cfg = load_config()
        n_sources = len(cfg.get("sources", {}))
        return "ok", f"loaded {n_sources} source(s) from {config_path}"
    except ConfigError as e:
        return "fail", str(e)
    except Exception as e:
        return "fail", f"unexpected error: {e}"


def _check_ingress_reachable() -> tuple[str, str]:
    """Check: daemon HTTP ingest port responds."""
    try:
        cfg = load_config()
    except Exception:
        return "skip", "config not loadable; cannot probe ingress"

    # Honor AGENT_WAKE_LISTEN_HOST / AGENT_WAKE_LISTEN_PORT so doctor probes
    # the same address the daemon actually binds (the daemon reads these env
    # overrides in main.resolve_listen).
    from .main import resolve_listen
    try:
        host, port = resolve_listen(cfg)
    except Exception:
        host = cfg.get("listen", {}).get("host", "127.0.0.1")
        port = cfg.get("listen", {}).get("port", 8788)

    try:
        with socket.create_connection((host, port), timeout=2.0):
            pass
    except (TimeoutError, ConnectionRefusedError, OSError):
        return "warn", f"daemon not reachable at {host}:{port} (may not be running)"

    try:
        url = f"http://{host}:{port}/"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if 200 <= resp.status < 300:
                body = json.loads(resp.read().decode("utf-8"))
                n_adapters = body.get("adapters", 0)
                return "ok", f"daemon healthy, {n_adapters} adapter(s) connected"
            return "fail", f"daemon returned HTTP {resp.status}"
    except urllib.error.URLError as e:
        return "warn", f"daemon TCP-reachable but HTTP probe failed: {e.reason}"
    except Exception as e:
        return "warn", f"daemon HTTP probe error: {e}"


def _check_auth_configured() -> tuple[str, str]:
    """Check: at least one source has a secret configured (auth is not open).

    Per Plan 004 WI-2.1 AC: an unconfigured-auth state is a named check failure
    so wake can't be deployed accidentally open.
    """
    try:
        cfg = load_config()
    except Exception:
        return "skip", "config not loadable"

    sources = cfg.get("sources", {})
    if not sources:
        return "fail", "no sources configured"

    no_secret = [name for name, info in sources.items()
                 if not info.get("secret_uris")]
    if no_secret:
        return "fail", f"sources without secrets: {', '.join(no_secret)}"

    return "ok", f"{len(sources)} source(s) with secrets configured"


def _check_adapters_installed() -> tuple[str, str]:
    """Check: at least one adapter is actually installed and ready.

    Precise checks (not just "bun on PATH"):
    - claude: ``agent-wake-claude`` console script on PATH.
    - opencode: the opencode plugin's ``dist/index.js`` exists at one of the
      candidate paths the installer probes. ``bun`` being on PATH is not enough
      — the plugin must actually be built.
    """
    installed: list[str] = []
    if shutil.which("agent-wake-claude") is not None:
        installed.append("claude")

    opencode_plugin = _find_opencode_plugin()
    if opencode_plugin is not None:
        # Report presence without echoing the full filesystem path (avoid
        # leaking host layout in shared doctor output).
        installed.append("opencode")

    if not installed:
        return (
            "warn",
            "no adapters installed (install agent-wake-claude or build the "
            "opencode plugin: cd adapters/opencode && bun install && bun run build)",
        )

    return "ok", f"adapters found: {', '.join(installed)}"


def _find_opencode_plugin() -> str | None:
    """Mirror install_harness's plugin path probe so the doctor check is precise."""
    try:
        from .cli.install_harness import _find_opencode_plugin_path
        path = _find_opencode_plugin_path()
        return path if path else None
    except Exception:
        return None


def _check_allowlist_present() -> tuple[str, str]:
    """Check: sources with principal_id have an identity allowlist."""
    try:
        cfg = load_config()
    except Exception:
        return "skip", "config not loadable"

    sources = cfg.get("sources", {})
    with_principal = {name: info for name, info in sources.items()
                      if info.get("principal_id")}
    without_allowlist = [name for name, info in with_principal.items()
                         if not info.get("allowed_trigger_identities")]

    if without_allowlist:
        return "warn", (
            f"sources with principal_id but no allowed_trigger_identities: "
            f"{', '.join(without_allowlist)} (any authenticated sender accepted)"
        )

    if with_principal:
        return "ok", f"{len(with_principal)} source(s) with identity allowlist"

    return "warn", "no sources have principal_id configured (identity layer inactive)"


def _check_delivery_health() -> tuple[str, str]:
    """Check: human-directed delivery channels are healthy (Plan 005 WI-1.2).

    If no principals are configured, this is a skip (delivery not deployed).
    If principals are configured but some lack delivery channels, warn.
    If the daemon is running, probe its health endpoint for live failure
    status (dead receivers, unknown principals).
    """
    try:
        cfg = load_config()
    except Exception:
        return "skip", "config not loadable"

    delivery = cfg.get("delivery", {})
    if not delivery:
        return "skip", "no delivery channels configured (Plan 005 not deployed)"

    empty = [pid for pid, channels in delivery.items() if not channels]
    if empty:
        return "warn", f"principals without delivery channels: {', '.join(empty)}"

    # Probe the daemon's health endpoint for live delivery status.
    listen = cfg.get("listen", {})
    host = listen.get("host", "127.0.0.1")
    port = listen.get("port", 8788)
    try:
        from .main import resolve_listen
        host, port = resolve_listen(cfg)
    except Exception:
        pass

    try:
        url = f"http://{host}:{port}/"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if 200 <= resp.status < 300:
                body = json.loads(resp.read().decode("utf-8"))
                if isinstance(body, dict):
                    delivery_status = body.get("delivery")
                    if isinstance(delivery_status, dict):
                        failing = delivery_status.get("failing_channels", [])
                        unknown = delivery_status.get("unknown_principals", [])
                        lost = delivery_status.get("lost_alerts") or 0
                        # A lost alert is a *fail*, not a warn: an alert that
                        # could not even be dead-lettered is gone, and the only
                        # other trace of it is one ERROR line in the daemon log.
                        if lost:
                            last = delivery_status.get("last_lost_alert") or "?"
                            return "fail", (
                                f"{lost} human alert(s) could not be dead-lettered "
                                f"and are permanently lost (most recent: {last}) — "
                                "the durable store was unavailable; grep the daemon "
                                "log for 'LOST ALERT'"
                            )
                        parts: list[str] = []
                        if failing:
                            chans = ", ".join(
                                f"{f.get('channel', '?')}→{f.get('principal_id', '?')}"
                                for f in failing
                            )
                            parts.append(
                                f"delivery channel failures: {chans} "
                                "(see daemon logs for detail)"
                            )
                        if unknown:
                            parts.append(
                                "events targeted unknown principals: "
                                f"{', '.join(unknown)}"
                            )
                        if parts:
                            return "warn", "; ".join(parts)
                        n = len(delivery)
                        return "ok", f"{n} principal(s), delivery healthy"
        return (
            "ok",
            f"{len(delivery)} delivery channel(s) configured "
            f"(daemon health endpoint returned HTTP {resp.status})",
        )
    except Exception:
        return (
            "ok",
            f"{len(delivery)} delivery channel(s) configured "
            "(daemon not running; live health unchecked)",
        )


def _check_regista() -> dict[str, Any] | None:
    """Check regista connectivity if suite config is present.

    Returns the regista sub-object for the doctor JSON, or None if the
    suite config is not in use. Per bootstrap-contract.md §3, the shape is
    ``{reachable, project, chain_ok}``.
    """
    if not suite_config.is_suite_configured():
        return None

    reg = suite_config.regista_config()
    dsn = reg.get("dsn")
    project = reg.get("project")

    if not dsn:
        return {
            "reachable": False,
            "project": project,
            "chain_ok": None,
            "detail": "REGISTA_DSN not set in suite.env",
        }

    # Attempt TCP connect to the Postgres host
    try:
        from urllib.parse import urlparse
        parsed = urlparse(dsn)
        pg_host = parsed.hostname or "localhost"
        pg_port = parsed.port or 5432
        with socket.create_connection((pg_host, pg_port), timeout=3.0):
            pass
        # agent-wake doesn't sign events, so chain_ok is not directly verifiable
        # here; report None (not applicable) rather than a misleading bool.
        return {
            "reachable": True,
            "project": project,
            "chain_ok": None,
            "detail": f"Postgres at {pg_host}:{pg_port} is TCP-reachable",
        }
    except Exception as e:
        return {
            "reachable": False,
            "project": project,
            "chain_ok": None,
            "detail": f"Postgres unreachable: {e}",
        }


# ── doctor runner ──────────────────────────────────────────────────────────────


def _check_durable_state() -> tuple[str, str]:
    """Check: the durable store is openable, and surface the dead-letter backlog.

    A non-empty dead-letter table is the operator-visible half of BC-WAKE-012:
    deliveries that permanently failed are no longer invisible, so the doctor
    reports them as a warning with the command that lists them.
    """
    try:
        cfg = load_config()
    except Exception:
        cfg = {}

    if not (cfg.get("state") or {}).get("enabled", True):
        return "warn", (
            "durable store disabled (state.enabled=false): dedupe is in-memory "
            "only and next-session delivery is unavailable"
        )

    from .store import StoreError, open_store, resolve_state_path

    path = resolve_state_path(cfg)
    if not path.exists():
        return "skip", (
            f"no durable state yet at {path} (created on first daemon start)"
        )
    try:
        store = open_store(cfg)
    except StoreError as e:
        return "fail", f"durable store unusable: {e}"
    try:
        dead = store.dead_letter_count()
        pending = store.pending_count()
        dedupe = store.dedupe_count()
    finally:
        store.close()

    detail = (
        f"dedupe={dedupe} pending={pending} dead_letter={dead}"
    )
    if dead:
        return "warn", (
            f"{detail}; inspect with `agent-wake dead-letter list`"
        )
    return "ok", detail


def run_checks() -> dict[str, Any]:
    """Run all health checks and return the suite-shaped doctor JSON."""
    checks: list[dict[str, str]] = []

    for name, fn in [
        ("config_present", _check_config_file),
        ("ingress_reachable", _check_ingress_reachable),
        ("auth_configured", _check_auth_configured),
        ("adapters_installed", _check_adapters_installed),
        ("allowlist_present", _check_allowlist_present),
        ("delivery_health", _check_delivery_health),
        ("durable_state", _check_durable_state),
    ]:
        status, detail = fn()
        checks.append({"name": name, "status": status, "detail": detail})

    regista = _check_regista()

    ok = all(c["status"] != "fail" for c in checks)
    degraded = any(c["status"] == "warn" for c in checks) and ok

    return {
        "component": "agent-wake",
        "version": _VERSION,
        "ok": ok,
        "degraded": degraded,
        "regista": regista,
        "checks": checks,
    }


def format_text(report: dict[str, Any]) -> str:
    """Format the doctor report as human-readable text."""
    lines: list[str] = []
    lines.append("agent-wake doctor — health check")
    lines.append("")

    for check in report.get("checks", []):
        status = check["status"]
        symbol = {"ok": "PASS", "fail": "FAIL", "warn": "WARN", "skip": "SKIP"}.get(status, "?")
        lines.append(f"  [{symbol:4}] {check['name']}: {check['detail']}")

    reg = report.get("regista")
    if reg is not None:
        lines.append("")
        reachable = reg.get("reachable", False)
        symbol = "PASS" if reachable else "FAIL"
        lines.append(f"  [{symbol:4}] regista: {reg.get('detail', '')}")
        if reg.get("project"):
            lines.append(f"         project: {reg['project']}")

    lines.append("")
    if report.get("ok"):
        if report.get("degraded"):
            lines.append("Result: OK (degraded — warnings present)")
        else:
            lines.append("Result: OK")
    else:
        lines.append("Result: FAIL")

    return "\n".join(lines)
