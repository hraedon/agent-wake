"""agent-wake doctor — health checks and suite-shaped JSON output.

Implements the suite doctor contract (agent-suite-blueprint.md §2.4,
bootstrap-contract.md §3).  ``agent-wake doctor --json`` emits:

    {
      "component": "agent-wake",
      "version": "0.1.0",
      "ok": true|false,
      "degraded": false,
      "regista": { "reachable": bool, "project": str|null } | null,
      "checks": [ { "name": str, "status": "pass"|"fail"|"warn"|"skip", "detail": str } ]
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
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    from importlib.metadata import version as _pkg_version
    _VERSION = _pkg_version("agent-waked")
except Exception:
    _VERSION = "0.1.0"

from .config import DEFAULT_CONFIG_PATH, load_config
from .config import ConfigError
from . import suite_config


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
        return "pass", f"loaded {n_sources} source(s) from {config_path}"
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

    listen = cfg.get("listen", {})
    host = listen.get("host", "127.0.0.1")
    port = listen.get("port", 8788)

    # Quick TCP connect check (faster than full HTTP probe)
    try:
        with socket.create_connection((host, port), timeout=2.0):
            pass
    except (ConnectionRefusedError, OSError, socket.timeout):
        return "warn", f"daemon not reachable at {host}:{port} (may not be running)"

    # HTTP health probe
    try:
        url = f"http://{host}:{port}/"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if 200 <= resp.status < 300:
                body = json.loads(resp.read().decode("utf-8"))
                n_adapters = body.get("adapters", 0)
                return "pass", f"daemon healthy, {n_adapters} adapter(s) connected"
            return "fail", f"daemon returned HTTP {resp.status}"
    except urllib.error.URLError as e:
        return "warn", f"daemon TCP-reachable but HTTP probe failed: {e.reason}"
    except Exception as e:
        return "warn", f"daemon HTTP probe error: {e}"


def _check_auth_configured() -> tuple[str, str]:
    """Check: at least one source has a secret configured (auth is not open)..

    Per Plan 004 WI-2.1 AC: an unconfigured-auth state is a named check failure
    so wake can't be deployed accidentally open.
    """
    try:
        cfg = load_config()
    except Exception:
        return "skip", "config not loadable"

    sources = cfg.get("sources", {})
    if not sources:
        return "fail", "no sources configured — daemon will reject all events"

    # Every source must have secret_uris (the config parser enforces this, but
    # double-check here for defense in depth).
    no_secret = [name for name, info in sources.items()
                 if not info.get("secret_uris")]
    if no_secret:
        return "fail", f"sources without secrets: {', '.join(no_secret)}"

    return "pass", f"{len(sources)} source(s) with secrets configured"


def _check_adapters_installed() -> tuple[str, str]:
    """Check: at least one adapter is installed (on PATH)."""
    claude = shutil.which("agent-wake-claude") is not None
    # opencode adapter is a JS plugin, not a CLI binary — check for bun
    opencode = shutil.which("bun") is not None

    installed: list[str] = []
    if claude:
        installed.append("claude")
    if opencode:
        installed.append("opencode(bun)")

    if not installed:
        return "warn", "no adapters installed (install agent-wake-claude or the opencode plugin)"

    return "pass", f"adapters found: {', '.join(installed)}"


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
        return "pass", f"{len(with_principal)} source(s) with identity allowlist"

    return "warn", "no sources have principal_id configured (identity layer inactive)"


def _check_regista() -> dict[str, Any] | None:
    """Check regista connectivity if suite config is present.

    Returns the regista sub-object for the doctor JSON, or None if the
    suite config is not in use.
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
        return {
            "reachable": True,
            "project": project,
            "detail": f"Postgres at {pg_host}:{pg_port} is TCP-reachable",
        }
    except Exception as e:
        return {
            "reachable": False,
            "project": project,
            "detail": f"Postgres unreachable: {e}",
        }


# ── doctor runner ──────────────────────────────────────────────────────────────


def run_checks() -> dict[str, Any]:
    """Run all health checks and return the suite-shaped doctor JSON."""
    checks: list[dict[str, str]] = []

    for name, fn in [
        ("config_present", _check_config_file),
        ("ingress_reachable", _check_ingress_reachable),
        ("auth_configured", _check_auth_configured),
        ("adapters_installed", _check_adapters_installed),
        ("allowlist_present", _check_allowlist_present),
    ]:
        status, detail = fn()
        checks.append({"name": name, "status": status, "detail": detail})

    regista = _check_regista()

    # Overall ok: no check is "fail" (warn is acceptable — degraded but not broken)
    ok = all(c["status"] != "fail" for c in checks)
    # Degraded if any check is "warn" but none failed
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
        symbol = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "skip": "SKIP"}.get(status, "?")
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


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="agent-wake doctor",
        description="agent-wake health check",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON to stdout (for suite-doctor aggregation)",
    )
    args = parser.parse_args()

    report = run_checks()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_text(report))

    return 0 if report.get("ok") else 1
