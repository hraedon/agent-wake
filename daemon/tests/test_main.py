"""Tests for agent_waked.main lifecycle helpers — non-loopback bind advisory (BC-002)
and listen-address resolution."""

import logging

import pytest

from agent_waked.config import ConfigError
from agent_waked.main import _warn_non_loopback_bind, _is_loopback, resolve_listen


# ── _warn_non_loopback_bind ───────────────────────────────────────────────────


def _cfg(authed: bool) -> dict:
    src = {"secret_uris": ["env://X"]} if authed else {"secret_uris": []}
    return {"sources": {"demo": src}}


def test_loopback_bind_no_warning(caplog):
    """127.0.0.1 bind never logs a security warning."""
    with caplog.at_level(logging.WARNING, logger="agent_waked"):
        _warn_non_loopback_bind("127.0.0.1", _cfg(authed=False))
    assert not caplog.records


def test_non_loopback_open_bind_logs_error(caplog):
    """Non-loopback + unauthenticated source → ERROR (operator must notice)."""
    with caplog.at_level(logging.ERROR, logger="agent_waked"):
        _warn_non_loopback_bind("0.0.0.0", _cfg(authed=False))
    assert any("SECURITY" in r.message for r in caplog.records)


def test_non_loopback_authed_bind_warns(caplog):
    """Non-loopback + authenticated sources → WARN (legitimate container/proxy case)."""
    with caplog.at_level(logging.WARNING, logger="agent_waked"):
        _warn_non_loopback_bind("0.0.0.0", _cfg(authed=True))
    assert any("non-loopback" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


def test_explicit_open_bind_downgrades_to_warning(caplog):
    """An env-overridden bind downgrades an open ingress from ERROR to WARN."""
    with caplog.at_level(logging.WARNING, logger="agent_waked"):
        _warn_non_loopback_bind("0.0.0.0", _cfg(authed=False), explicit=True)
    assert any("AGENT_WAKE_LISTEN_HOST" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


# ── _is_loopback — full 127.0.0.0/8 range ─────────────────────────────────────


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "127.255.255.255", "::1", "localhost"])
def test_is_loopback_true_for_loopback_hosts(host):
    assert _is_loopback(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.1", "10.0.0.1", "::"])
def test_is_loopback_false_for_non_loopback(host):
    assert _is_loopback(host) is False


# ── resolve_listen — env override + validation ────────────────────────────────


def test_resolve_listen_defaults(monkeypatch):
    monkeypatch.delenv("AGENT_WAKE_LISTEN_HOST", raising=False)
    monkeypatch.delenv("AGENT_WAKE_LISTEN_PORT", raising=False)
    host, port = resolve_listen({"listen": {"host": "127.0.0.1", "port": 9000}})
    assert host == "127.0.0.1"
    assert port == 9000


def test_resolve_listen_env_overrides(monkeypatch):
    monkeypatch.setenv("AGENT_WAKE_LISTEN_HOST", "0.0.0.0")
    monkeypatch.setenv("AGENT_WAKE_LISTEN_PORT", "7000")
    host, port = resolve_listen({"listen": {"host": "127.0.0.1", "port": 9000}})
    assert host == "0.0.0.0"
    assert port == 7000


def test_resolve_listen_bad_port_raises_configerror(monkeypatch):
    """A non-numeric AGENT_WAKE_LISTEN_PORT raises ConfigError, not a traceback."""
    monkeypatch.setenv("AGENT_WAKE_LISTEN_PORT", "abc")
    with pytest.raises(ConfigError):
        resolve_listen({})


def test_resolve_listen_port_out_of_range_raises(monkeypatch):
    monkeypatch.setenv("AGENT_WAKE_LISTEN_PORT", "99999")
    with pytest.raises(ConfigError):
        resolve_listen({})
