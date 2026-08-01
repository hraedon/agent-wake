"""WI-003: the doctor must not fail closed on secrets it has no business reading.

The invariant under test is a split of responsibility, not a single behaviour:

- ``load_config`` validates *shape* and never reads secret material.
- the **daemon** refuses to run (and refuses a SIGHUP reload) when it cannot read
  a secret it would sign with — it is the component that signs, so for it an
  unreadable secret is fatal.
- the **doctor** may legitimately run without those secrets (the suite's
  scheduled ``agent-suite-doctor-alert.service`` runs as root with only
  ``/etc/agent-suite/suite.env``), so it reports the limitation honestly and
  takes the daemon's ``GET /`` answer as the verdict — but reports a failure when
  neither it nor the daemon can confirm.
- genuinely broken config still fails loudly on *both* paths.
"""

import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent_waked import main
from agent_waked.config import ConfigError, load_config
from agent_waked.doctor import run_checks
from agent_waked.ingest import create_ingest_app
from agent_waked.router import Router
from agent_waked.secrets.visibility import (
    health_summary,
    source_secret_visibility,
)

SECRET_VAR = "AW_WI003_SECRET"
OTHER_VAR = "AW_WI003_SECRET_TWO"


# ── helpers ───────────────────────────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


class StubDaemon:
    """A stand-in for the daemon's ``GET /`` health document.

    Real-daemon tests live in test_e2e.py; here the point is precisely what the
    doctor does with the *content* of that document, so serving a canned one on
    an ephemeral port keeps the assertions about the doctor rather than about
    daemon startup timing.
    """

    def __init__(self, body: dict | None, status: int = 200) -> None:
        payload = json.dumps(body if body is not None else {}).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def __enter__(self) -> "StubDaemon":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(timeout=5)


def _healthy_health_doc(configured: int = 1) -> dict:
    return {
        "status": "ok",
        "version": "0.1.0",
        "sources": {
            "configured": configured,
            "secrets_unresolved": 0,
            "unresolved": [],
        },
        "adapters": 0,
        "subscribers": {
            "connected": configured,
            "connected_adapters": ["claude"] if configured else [],
            "by_source": {
                "demo": {
                    "subscribers": configured,
                    "adapters": ["claude"] if configured else [],
                    "oldest_age_seconds": 0.0 if configured else None,
                }
            },
            "live_only_sources": ["demo"] if configured else [],
            "live_only_without_subscribers": [],
        },
    }


@pytest.fixture
def isolate_suite(tmp_path, monkeypatch):
    """Keep the regista sub-check out of the way (no suite.env in the sandbox)."""
    monkeypatch.setattr(
        "agent_waked.suite_config.per_user_path", lambda: tmp_path / "no-user.env"
    )
    monkeypatch.setattr(
        "agent_waked.suite_config.system_path", lambda: tmp_path / "no-sys.env"
    )
    # The doctor's durable_state check would otherwise touch the real $HOME.
    monkeypatch.setenv("AGENT_WAKE_STATE_DIR", str(tmp_path / "state"))


def _doctor_config(tmp_path, monkeypatch, port: int, *, sources: dict | None = None) -> Path:
    cfg_path = tmp_path / "config.json"
    _write_config(
        cfg_path,
        {
            "version": 1,
            "listen": {"host": "127.0.0.1", "port": port},
            "sources": sources or {"demo": {"secret_env": SECRET_VAR}},
            "routing": {},
        },
    )
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    return cfg_path


def _check(report: dict, name: str) -> dict:
    return next(c for c in report["checks"] if c["name"] == name)


# ── secrets.visibility unit behaviour ─────────────────────────────────────────


def test_visibility_reports_missing_vars(monkeypatch):
    monkeypatch.delenv(SECRET_VAR, raising=False)
    monkeypatch.setenv(OTHER_VAR, "present")
    cfg = {
        "sources": {
            "gone": {"secret_uris": [f"env://{SECRET_VAR}"]},
            "here": {"secret_uris": [f"env://{OTHER_VAR}"]},
        }
    }
    vis = source_secret_visibility(cfg)
    assert vis.configured == 2
    assert vis.unresolved == ("gone",)
    assert vis.missing_env_vars == (SECRET_VAR,)
    assert vis.all_visible is False


def test_visibility_ignores_vault_uris(monkeypatch):
    """Vault reachability is a runtime condition, not this process's environment."""
    cfg = {"sources": {"v": {"secret_uris": ["vault://kv/wake#value"]}}}
    vis = source_secret_visibility(cfg)
    assert vis.all_visible is True
    assert vis.unresolved == ()


def test_health_summary_carries_no_env_var_names(monkeypatch):
    """``GET /`` is unauthenticated: counts and source names only, never var names."""
    monkeypatch.delenv(SECRET_VAR, raising=False)
    cfg = {"sources": {"demo": {"secret_uris": [f"env://{SECRET_VAR}"]}}}
    summary = health_summary(cfg)
    assert summary == {
        "configured": 1,
        "secrets_unresolved": 1,
        "unresolved": ["demo"],
    }
    assert SECRET_VAR not in json.dumps(summary)


# ── the daemon reports its own answer on GET / ────────────────────────────────


@pytest.mark.asyncio
async def test_health_endpoint_reports_source_secret_state(monkeypatch):
    monkeypatch.setenv(SECRET_VAR, "s3kr1t")
    cfg = {
        "sources": {"demo": {"secret_uris": [f"env://{SECRET_VAR}"], "callback_url": None}},
        "routing": {},
    }
    app = create_ingest_app(cfg, Router(cfg))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/")
        assert resp.status == 200
        body = await resp.json()
    finally:
        await client.close()

    assert body["sources"] == {
        "configured": 1,
        "secrets_unresolved": 0,
        "unresolved": [],
    }
    assert body["status"] == "degraded"
    assert body["subscribers"] == {
        "connected": 0,
        "connected_adapters": [],
        "by_source": {
            "demo": {"subscribers": 0, "adapters": [], "oldest_age_seconds": None}
        },
        "live_only_sources": ["demo"],
        "live_only_without_subscribers": ["demo"],
    }
    # No secret value, and no env-var name, on the unauthenticated port.
    raw = json.dumps(body)
    assert "s3kr1t" not in raw
    assert SECRET_VAR not in raw


# ── doctor: secrets absent, daemon reachable and healthy ──────────────────────


def test_doctor_does_not_fail_when_secrets_invisible_but_daemon_healthy(
    tmp_path, monkeypatch, isolate_suite
):
    """The bug: this reported ok=false on a perfectly healthy estate."""
    monkeypatch.delenv(SECRET_VAR, raising=False)
    with StubDaemon(_healthy_health_doc()) as daemon:
        _doctor_config(tmp_path, monkeypatch, daemon.port)
        report = run_checks()

    assert report["ok"] is True, report["checks"]

    # The config itself is fine — an unset env var is not a malformed config.
    assert _check(report, "config_present")["status"] == "ok"

    # And the checks that used to collapse to "config not loadable" now run.
    for name in ("ingress_reachable", "auth_configured", "allowlist_present"):
        check = _check(report, name)
        assert check["status"] != "skip", (name, check)
        assert "config not loadable" not in check["detail"]

    # The limitation is named, and it is a skip: a warn would set degraded=true
    # and re-create the hourly cry-wolf one severity lower.
    secrets_check = _check(report, "secrets_resolvable")
    assert secrets_check["status"] == "skip"
    detail = secrets_check["detail"]
    assert "not visible from this context" in detail
    assert SECRET_VAR in detail          # actionable: says which var to set
    assert "daemon reports 1 source(s)" in detail
    assert "authority" in detail


def test_doctor_not_degraded_by_invisible_secrets(tmp_path, monkeypatch, isolate_suite):
    """A skip must not set ``degraded``; other checks may, this one must not."""
    monkeypatch.delenv(SECRET_VAR, raising=False)
    with StubDaemon(_healthy_health_doc()) as daemon:
        _doctor_config(tmp_path, monkeypatch, daemon.port)
        report = run_checks()
    assert _check(report, "secrets_resolvable")["status"] == "skip"
    warned = [c["name"] for c in report["checks"] if c["status"] == "warn"]
    assert "secrets_resolvable" not in warned


# ── doctor: secrets absent AND daemon unreachable → still a failure ───────────


def test_doctor_fails_when_secrets_invisible_and_daemon_unreachable(
    tmp_path, monkeypatch, isolate_suite
):
    monkeypatch.delenv(SECRET_VAR, raising=False)
    _doctor_config(tmp_path, monkeypatch, _free_port())  # nothing listening
    report = run_checks()

    assert report["ok"] is False
    check = _check(report, "secrets_resolvable")
    assert check["status"] == "fail"
    assert "could not be asked" in check["detail"]
    assert "cannot be confirmed from either source" in check["detail"]


def test_doctor_fails_when_daemon_health_omits_source_state(
    tmp_path, monkeypatch, isolate_suite
):
    """An older daemon that cannot answer is not an answer — unknown is not green."""
    monkeypatch.delenv(SECRET_VAR, raising=False)
    with StubDaemon({"status": "ok", "version": "0.1.0", "adapters": 0}) as daemon:
        _doctor_config(tmp_path, monkeypatch, daemon.port)
        report = run_checks()

    assert report["ok"] is False
    check = _check(report, "secrets_resolvable")
    assert check["status"] == "fail"
    assert "does not report source secret state" in check["detail"]


def test_doctor_fails_when_daemon_reports_unresolved_secrets(
    tmp_path, monkeypatch, isolate_suite
):
    """The daemon is the authority in *both* directions."""
    monkeypatch.delenv(SECRET_VAR, raising=False)
    doc = _healthy_health_doc()
    doc["sources"] = {"configured": 1, "secrets_unresolved": 1, "unresolved": ["demo"]}
    with StubDaemon(doc) as daemon:
        _doctor_config(tmp_path, monkeypatch, daemon.port)
        report = run_checks()

    assert report["ok"] is False
    check = _check(report, "secrets_resolvable")
    assert check["status"] == "fail"
    assert "demo" in check["detail"]


def test_doctor_fails_when_daemon_reports_unresolved_even_if_visible_here(
    tmp_path, monkeypatch, isolate_suite
):
    """Locally readable secrets do not override the signer's own report."""
    monkeypatch.setenv(SECRET_VAR, "visible-here")
    doc = _healthy_health_doc()
    doc["sources"] = {"configured": 1, "secrets_unresolved": 1, "unresolved": ["demo"]}
    with StubDaemon(doc) as daemon:
        _doctor_config(tmp_path, monkeypatch, daemon.port)
        report = run_checks()

    assert report["ok"] is False
    assert _check(report, "secrets_resolvable")["status"] == "fail"


# ── doctor: secrets present → unchanged behaviour ─────────────────────────────


def test_doctor_ok_when_secrets_present(tmp_path, monkeypatch, isolate_suite):
    monkeypatch.setenv(SECRET_VAR, "s3kr1t")
    with StubDaemon(_healthy_health_doc()) as daemon:
        _doctor_config(tmp_path, monkeypatch, daemon.port)
        report = run_checks()

    assert report["ok"] is True
    check = _check(report, "secrets_resolvable")
    assert check["status"] == "ok"
    assert "1 source secret(s) readable" in check["detail"]
    assert _check(report, "config_present")["status"] == "ok"
    assert _check(report, "auth_configured")["status"] == "ok"


def test_doctor_ok_when_secrets_present_and_daemon_down(
    tmp_path, monkeypatch, isolate_suite
):
    """Secrets readable here is a complete answer; the daemon need not be up."""
    monkeypatch.setenv(SECRET_VAR, "s3kr1t")
    _doctor_config(tmp_path, monkeypatch, _free_port())
    report = run_checks()
    assert _check(report, "secrets_resolvable")["status"] == "ok"


# ── daemon startup must still refuse an unreadable secret ─────────────────────


def test_daemon_gate_refuses_unreadable_secret(monkeypatch, caplog):
    monkeypatch.delenv(SECRET_VAR, raising=False)
    cfg = {"sources": {"demo": {"secret_uris": [f"env://{SECRET_VAR}"]}}}
    assert main._require_resolvable_secrets(cfg) is False
    assert "SECRETS" in caplog.text
    assert SECRET_VAR in caplog.text


def test_daemon_gate_accepts_readable_secret(monkeypatch):
    monkeypatch.setenv(SECRET_VAR, "s3kr1t")
    cfg = {"sources": {"demo": {"secret_uris": [f"env://{SECRET_VAR}"]}}}
    assert main._require_resolvable_secrets(cfg) is True


def _run_daemon_briefly(
    cfg_path: Path, tmp_path: Path, env_extra: dict
) -> "subprocess.CompletedProcess[str]":
    import os

    env = {**os.environ}
    # Start from an environment that definitely lacks the test secrets, so a pass
    # can never come from an inherited variable.
    env.pop(SECRET_VAR, None)
    env.pop(OTHER_VAR, None)
    env["AGENT_WAKE_CONFIG"] = str(cfg_path)
    env["AGENT_WAKE_STATE_DIR"] = str(tmp_path / "state")
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "agent_waked"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_daemon_startup_refuses_unresolvable_secret(tmp_path):
    """The real daemon process, not a helper: exit 1 and never binds."""
    cfg_path = tmp_path / "config.json"
    port = _free_port()
    _write_config(
        cfg_path,
        {
            "version": 1,
            "listen": {"host": "127.0.0.1", "port": port},
            "socket_path": str(tmp_path / "aw.sock"),
            "sources": {"demo": {"secret_env": SECRET_VAR}},
            "routing": {},
        },
    )
    proc = _run_daemon_briefly(cfg_path, tmp_path, {})
    assert proc.returncode == 1, proc.stderr
    assert SECRET_VAR in proc.stderr
    assert "cannot run for a source whose key it cannot read" in proc.stderr
    # It refused *before* binding: nothing is listening and no socket was made.
    assert not (tmp_path / "aw.sock").exists()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        with pytest.raises(OSError):
            s.connect(("127.0.0.1", port))


def test_daemon_startup_refuses_uri_form_unresolvable_secret(tmp_path):
    """The gate is stronger than the check it replaced.

    The old eager resolution in ``load_config`` fired only for the legacy
    ``secret_env`` spelling; ``"secret": "env://NAME"`` with NAME unset started
    the daemon happily and failed per request. All three spellings normalise to
    ``secret_uris``, which is what the gate inspects.
    """
    cfg_path = tmp_path / "config.json"
    _write_config(
        cfg_path,
        {
            "version": 1,
            "listen": {"host": "127.0.0.1", "port": _free_port()},
            "socket_path": str(tmp_path / "aw.sock"),
            "sources": {"demo": {"secret": f"env://{SECRET_VAR}"}},
            "routing": {},
        },
    )
    proc = _run_daemon_briefly(cfg_path, tmp_path, {})
    assert proc.returncode == 1, proc.stderr
    assert SECRET_VAR in proc.stderr


def test_sighup_reload_rejects_unreadable_secret(tmp_path, monkeypatch):
    """A reload must not introduce a source the daemon could never authenticate."""
    monkeypatch.setenv(SECRET_VAR, "s3kr1t")
    monkeypatch.delenv(OTHER_VAR, raising=False)
    cfg_path = tmp_path / "config.json"
    _write_config(
        cfg_path,
        {
            "version": 1,
            "listen": {"host": "127.0.0.1", "port": 8788},
            "sources": {"demo": {"secret_env": SECRET_VAR}},
            "routing": {},
        },
    )
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    cfg = load_config()
    router = Router(cfg)

    # Now add a source whose secret this process cannot read.
    _write_config(
        cfg_path,
        {
            "version": 1,
            "listen": {"host": "127.0.0.1", "port": 8788},
            "sources": {
                "demo": {"secret_env": SECRET_VAR},
                "newcomer": {"secret_env": OTHER_VAR},
            },
            "routing": {},
        },
    )
    main._reload_config(cfg, router)
    assert list(cfg["sources"]) == ["demo"], "reload must have been rejected"


def test_sighup_reload_accepts_readable_secret(tmp_path, monkeypatch):
    monkeypatch.setenv(SECRET_VAR, "s3kr1t")
    monkeypatch.setenv(OTHER_VAR, "also-s3kr1t")
    cfg_path = tmp_path / "config.json"
    base = {
        "version": 1,
        "listen": {"host": "127.0.0.1", "port": 8788},
        "sources": {"demo": {"secret_env": SECRET_VAR}},
        "routing": {},
    }
    _write_config(cfg_path, base)
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    cfg = load_config()
    router = Router(cfg)

    base["sources"]["newcomer"] = {"secret_env": OTHER_VAR}
    _write_config(cfg_path, base)
    main._reload_config(cfg, router)
    assert sorted(cfg["sources"]) == ["demo", "newcomer"]


# ── genuinely broken config still fails loudly, on both paths ─────────────────


_BROKEN_CONFIGS: list[tuple[str, object, str]] = [
    ("malformed_json", "{ not json at all", "not valid JSON"),
    (
        "source_with_no_auth",
        {"version": 1, "sources": {"demo": {"principal_id": "agent:x"}}},
        "must have exactly one of",
    ),
    (
        "bare_string_allowlist",
        {
            "version": 1,
            "sources": {
                "demo": {
                    "secret_env": SECRET_VAR,
                    "principal_id": "agent:x",
                    "allowed_trigger_identities": "human:itadmin",
                }
            },
        },
        "must be a list",
    ),
    (
        "unsupported_secret_scheme",
        {"version": 1, "sources": {"demo": {"secret": "file:///etc/shadow"}}},
        "unsupported URI scheme",
    ),
    (
        "no_sources",
        {"version": 1, "sources": {}},
        "At least one source",
    ),
    (
        "vault_uri_without_vault_block",
        {"version": 1, "sources": {"demo": {"secret": "vault://kv/wake#value"}}},
        "no 'vault' block",
    ),
    (
        "sources_not_an_object",
        {"version": 1, "sources": []},
        "must be an object",
    ),
    (
        "unparseable_listen_port",
        {
            "version": 1,
            "listen": {"host": "127.0.0.1", "port": "eight-seven-eight-eight"},
            "sources": {"demo": {"secret_env": SECRET_VAR}},
        },
        "'listen.port' must be an integer",
    ),
    (
        "listen_not_an_object",
        {
            "version": 1,
            "listen": "127.0.0.1:8788",
            "sources": {"demo": {"secret_env": SECRET_VAR}},
        },
        "'listen' must be an object",
    ),
    (
        "listen_port_out_of_range",
        {
            "version": 1,
            "listen": {"port": 99999},
            "sources": {"demo": {"secret_env": SECRET_VAR}},
        },
        "out of range",
    ),
]


@pytest.mark.parametrize(
    "label,payload,expected",
    _BROKEN_CONFIGS,
    ids=[c[0] for c in _BROKEN_CONFIGS],
)
def test_load_config_still_fails_loudly(label, payload, expected, tmp_path, monkeypatch):
    monkeypatch.setenv(SECRET_VAR, "s3kr1t")
    cfg_path = tmp_path / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        cfg_path.write_text(payload)
    else:
        cfg_path.write_text(json.dumps(payload))
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    with pytest.raises(ConfigError, match=expected):
        load_config()


@pytest.mark.parametrize(
    "label,payload,expected",
    _BROKEN_CONFIGS,
    ids=[c[0] for c in _BROKEN_CONFIGS],
)
def test_doctor_still_fails_loudly_on_broken_config(
    label, payload, expected, tmp_path, monkeypatch, isolate_suite
):
    monkeypatch.setenv(SECRET_VAR, "s3kr1t")
    cfg_path = tmp_path / "config.json"
    if isinstance(payload, str):
        cfg_path.write_text(payload)
    else:
        cfg_path.write_text(json.dumps(payload))
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(cfg_path))
    report = run_checks()
    assert report["ok"] is False
    assert _check(report, "config_present")["status"] == "fail"


def test_daemon_startup_still_fails_loudly_on_malformed_json(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{ not json at all")
    proc = _run_daemon_briefly(cfg_path, tmp_path, {})
    assert proc.returncode == 1, proc.stderr
    assert "not valid JSON" in proc.stderr


def test_daemon_startup_still_fails_loudly_on_source_without_auth(tmp_path):
    cfg_path = tmp_path / "config.json"
    _write_config(
        cfg_path,
        {"version": 1, "sources": {"demo": {"principal_id": "agent:x"}}},
    )
    proc = _run_daemon_briefly(cfg_path, tmp_path, {})
    assert proc.returncode == 1, proc.stderr
    assert "must have exactly one of" in proc.stderr


def test_daemon_startup_still_fails_loudly_on_bad_listen_port(tmp_path):
    cfg_path = tmp_path / "config.json"
    _write_config(
        cfg_path,
        {
            "version": 1,
            "listen": {"host": "127.0.0.1", "port": 8788},
            "sources": {"demo": {"secret_env": SECRET_VAR}},
        },
    )
    proc = _run_daemon_briefly(
        cfg_path,
        tmp_path,
        {SECRET_VAR: "s3kr1t", "AGENT_WAKE_LISTEN_PORT": "not-a-port"},
    )
    assert proc.returncode == 1, proc.stderr
    assert "not a valid port number" in proc.stderr
