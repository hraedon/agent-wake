"""Tests for SIGHUP config reload (phase 5 acceptance).

Covers:
- New source becomes addressable after reload.
- Port change is ignored with a warning.
- Invalid config on reload keeps previous config.
- Integration: subprocess receives SIGHUP, picks up new source.
- Integration: port change after SIGHUP, old port stays bound.
"""

import asyncio
import hashlib
import hmac
import json
import os
import signal
import sys
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_waked.config import ConfigError
from agent_waked.main import _reload_config
from agent_waked.outbox import Outbox
from agent_waked.router import Router


def _hmac_sig(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def _write_config(
    path: Path,
    sources: dict,
    listen: dict | None = None,
    socket_path: str | None = None,
    routing: dict | None = None,
) -> None:
    cfg: dict = {
        "version": 1,
        "listen": listen or {"host": "127.0.0.1", "port": 18788},
        "socket_path": socket_path,
        "sources": sources,
        "default_callback_url": None,
        "routing": routing or {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg))


# ── unit tests for _reload_config ────────────────────────────────────

class TestReloadConfig:
    def test_new_source_appears_in_config(self):
        with patch("agent_waked.main.load_config") as mock_load:
            mock_load.return_value = {
                "version": 1,
                "listen": {"host": "127.0.0.1", "port": 8788},
                "sources": {
                    "source-a": {"secret": b"s1"},
                    "source-b": {"secret": b"s2"},
                },
                "routing": {},
            }

            live_cfg: dict = {
                "version": 1,
                "listen": {"host": "127.0.0.1", "port": 8788},
                "sources": {"source-a": {"secret": b"s1"}},
                "routing": {},
            }
            router = Router(live_cfg)
            outbox = Outbox(live_cfg)

            _reload_config(live_cfg, router)

            assert "source-b" in live_cfg["sources"]
            assert "source-a" in live_cfg["sources"]

    def test_port_change_ignored(self):
        with patch("agent_waked.main.load_config") as mock_load:
            mock_load.return_value = {
                "version": 1,
                "listen": {"host": "127.0.0.1", "port": 9999},
                "sources": {"source-a": {"secret": b"s1"}},
                "routing": {},
            }

            live_cfg: dict = {
                "version": 1,
                "listen": {"host": "127.0.0.1", "port": 8788},
                "sources": {"source-a": {"secret": b"s1"}},
                "routing": {},
            }
            router = Router(live_cfg)
            outbox = Outbox(live_cfg)

            _reload_config(live_cfg, router)

            assert live_cfg["listen"]["port"] == 8788

    def test_bad_config_keeps_previous(self):
        live_cfg: dict = {
            "version": 1,
            "listen": {"host": "127.0.0.1", "port": 8788},
            "sources": {"source-a": {"secret": b"s1"}},
            "routing": {},
        }
        router = Router(live_cfg)
        outbox = Outbox(live_cfg)

        with patch("agent_waked.main.load_config", side_effect=ConfigError("bad")):
            _reload_config(live_cfg, router)

        assert "source-a" in live_cfg["sources"]
        assert live_cfg["listen"]["port"] == 8788

    def test_routing_change_applies(self):
        with patch("agent_waked.main.load_config") as mock_load:
            mock_load.return_value = {
                "version": 1,
                "listen": {"host": "127.0.0.1", "port": 8788},
                "sources": {
                    "source-a": {"secret": b"s1"},
                    "source-b": {"secret": b"s2"},
                },
                "routing": {
                    "source-a": {"adapter": "claude"},
                },
            }

            live_cfg: dict = {
                "version": 1,
                "listen": {"host": "127.0.0.1", "port": 8788},
                "sources": {"source-a": {"secret": b"s1"}},
                "routing": {},
            }
            router = Router(live_cfg)
            outbox = Outbox(live_cfg)

            _reload_config(live_cfg, router)

            assert live_cfg["routing"]["source-a"]["adapter"] == "claude"
            assert "source-b" in live_cfg["sources"]

    def test_reload_logs_port_warning(self, caplog):
        import logging

        with patch("agent_waked.main.load_config") as mock_load:
            mock_load.return_value = {
                "version": 1,
                "listen": {"host": "0.0.0.0", "port": 9999},
                "sources": {"source-a": {"secret": b"s1"}},
                "routing": {},
            }

            live_cfg: dict = {
                "version": 1,
                "listen": {"host": "127.0.0.1", "port": 8788},
                "sources": {"source-a": {"secret": b"s1"}},
                "routing": {},
            }
            router = Router(live_cfg)
            outbox = Outbox(live_cfg)

            with caplog.at_level(logging.WARNING, logger="agent_waked"):
                _reload_config(live_cfg, router)

            assert any("requires restart" in r.message for r in caplog.records)


# ── integration: SIGHUP via subprocess ───────────────────────────────

_TEST_PORT = 18788


@pytest.mark.asyncio
async def test_sighup_new_source_addressable(tmp_path):
    """Phase 5 acceptance test 3: add source to config, SIGHUP, source
    becomes addressable."""
    secret_a = "TEST_SIGHUP_A"
    secret_b = "TEST_SIGHUP_B"
    env = {secret_a: "sa", secret_b: "sb"}

    config_path = tmp_path / "config.json"
    sock_path = tmp_path / "aw.sock"
    _write_config(
        config_path,
        sources={"source-a": {"secret_env": secret_a}},
        listen={"host": "127.0.0.1", "port": _TEST_PORT},
        socket_path=str(sock_path),
    )

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "agent_waked",
        env={**os.environ, **env, "AGENT_WAKE_CONFIG": str(config_path),
             # Prevent host env overrides from changing the bind the test
             # asserts against (main.resolve_listen honors these).
             "AGENT_WAKE_LISTEN_HOST": "",
             "AGENT_WAKE_LISTEN_PORT": ""},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        await asyncio.sleep(1.5)
        assert proc.returncode is None, "daemon crashed on startup"

        body_a = json.dumps({
            "v": 0, "event_id": "e1", "source": "source-a",
            "kind": "alert", "content": "before", "meta": {}, "wake": True,
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{_TEST_PORT}/",
            data=body_a,
            headers={
                "X-AgentWake-Source": "source-a",
                "X-AgentWake-Signature": _hmac_sig(b"sa", body_a),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 202

        body_b_before = json.dumps({
            "v": 0, "event_id": "e2", "source": "source-b",
            "kind": "alert", "content": "new", "meta": {}, "wake": True,
        }).encode()
        req_b = urllib.request.Request(
            f"http://127.0.0.1:{_TEST_PORT}/",
            data=body_b_before,
            headers={
                "X-AgentWake-Source": "source-b",
                "X-AgentWake-Signature": _hmac_sig(b"sb", body_b_before),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req_b) as resp:
                assert False, "source-b should not be known before reload"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        _write_config(
            config_path,
            sources={
                "source-a": {"secret_env": secret_a},
                "source-b": {"secret_env": secret_b},
            },
            listen={"host": "127.0.0.1", "port": _TEST_PORT},
            socket_path=str(sock_path),
        )

        proc.send_signal(signal.SIGHUP)
        await asyncio.sleep(0.5)

        body_b = json.dumps({
            "v": 0, "event_id": "e3", "source": "source-b",
            "kind": "alert", "content": "after reload", "meta": {}, "wake": True,
        }).encode()
        req_b2 = urllib.request.Request(
            f"http://127.0.0.1:{_TEST_PORT}/",
            data=body_b,
            headers={
                "X-AgentWake-Source": "source-b",
                "X-AgentWake-Signature": _hmac_sig(b"sb", body_b),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req_b2) as resp:
            data = json.loads(resp.read())
            assert resp.status == 202
            assert data["status"] in ("queued", "no_subscriber")

    finally:
        if proc.returncode is None:
            proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=5)


@pytest.mark.asyncio
async def test_sighup_port_change_old_port_stays_bound(tmp_path):
    """Phase 5 acceptance test 4: changing listen.port and SIGHUP logs a
    warning; the old port stays bound."""
    secret_env = "TEST_SIGHUP_PORT"
    env = {secret_env: "ps"}

    config_path = tmp_path / "config.json"
    sock_path = tmp_path / "aw.sock"
    _write_config(
        config_path,
        sources={"source-a": {"secret_env": secret_env}},
        listen={"host": "127.0.0.1", "port": _TEST_PORT},
        socket_path=str(sock_path),
    )

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "agent_waked",
        env={**os.environ, **env, "AGENT_WAKE_CONFIG": str(config_path),
             "AGENT_WAKE_LISTEN_HOST": "",
             "AGENT_WAKE_LISTEN_PORT": ""},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        await asyncio.sleep(1.5)
        assert proc.returncode is None

        _write_config(
            config_path,
            sources={"source-a": {"secret_env": secret_env}},
            listen={"host": "127.0.0.1", "port": 19999},
            socket_path=str(sock_path),
        )

        proc.send_signal(signal.SIGHUP)
        await asyncio.sleep(0.5)

        body = json.dumps({
            "v": 0, "event_id": "e-pt", "source": "source-a",
            "kind": "alert", "content": "port test", "meta": {}, "wake": True,
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{_TEST_PORT}/",
            data=body,
            headers={
                "X-AgentWake-Source": "source-a",
                "X-AgentWake-Signature": _hmac_sig(b"ps", body),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 202

    finally:
        if proc.returncode is None:
            proc.terminate()
        stderr = await asyncio.wait_for(proc.stderr.read(), timeout=5)
        await asyncio.wait_for(proc.wait(), timeout=5)

    stderr_text = stderr.decode("utf-8", errors="replace")
    assert "requires restart" in stderr_text
