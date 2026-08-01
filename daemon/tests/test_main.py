"""Tests for agent_waked.main lifecycle helpers — non-loopback bind advisory (BC-002)
and listen-address resolution."""

import asyncio
import logging

import pytest

from agent_waked.config import ConfigError
from agent_waked.main import (
    _is_loopback,
    _shutdown,
    _warn_non_loopback_bind,
    resolve_listen,
)

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


# ── shutdown ordering (WI-001 M3) ─────────────────────────────────────────────


class _Recorder:
    """Records the order in which the daemon tears its pieces down."""

    def __init__(self) -> None:
        self.order: list[str] = []


class _FakeSocketServer:
    def __init__(self, rec):
        self._rec = rec

    def close(self):
        self._rec.order.append("socket_server.close")


class _FakeRunner:
    def __init__(self, rec, *, hang: bool = False):
        self._rec = rec
        self._hang = hang

    async def cleanup(self):
        self._rec.order.append("runner.cleanup")
        if self._hang:
            await asyncio.sleep(30)


class _FakeRouter:
    def __init__(self, rec):
        self._rec = rec

    async def shutdown(self):
        self._rec.order.append("router.shutdown")


class _FakeOutbox:
    def __init__(self, rec):
        self._rec = rec

    async def close(self):
        self._rec.order.append("outbox.close")


class _FakeDelivery:
    def __init__(self, rec):
        self._rec = rec

    async def close(self):
        self._rec.order.append("delivery.close")


class _FakeStore:
    def __init__(self, rec):
        self._rec = rec

    def close(self):
        self._rec.order.append("store.close")


async def _run_shutdown(rec, **overrides):
    kwargs = {
        "socket_server": _FakeSocketServer(rec),
        "runner": _FakeRunner(rec),
        "router": _FakeRouter(rec),
        "outbox": _FakeOutbox(rec),
        "delivery": _FakeDelivery(rec),
        "store": _FakeStore(rec),
    }
    kwargs.update(overrides)
    await _shutdown(**kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_store_is_closed_after_the_http_runner_is_drained():
    """The durability invariant: nothing may want the store after it closes.

    The old order closed the store first, so a delivery still in retry backoff
    dead-lettered into a closed connection (ProgrammingError) or was cancelled
    with nothing recorded at all.
    """
    rec = _Recorder()
    await _run_shutdown(rec)
    assert rec.order == [
        "socket_server.close",
        "runner.cleanup",
        "router.shutdown",
        "outbox.close",
        "delivery.close",
        "store.close",
    ]
    assert rec.order.index("runner.cleanup") < rec.order.index("store.close")
    assert rec.order.index("delivery.close") < rec.order.index("store.close")


@pytest.mark.asyncio
async def test_a_hung_runner_cleanup_still_closes_the_store(caplog):
    """A drain that overruns must not strand the store open (or the process)."""
    rec = _Recorder()
    with caplog.at_level(logging.WARNING, logger="agent_waked.main"):
        await _run_shutdown(rec, runner=_FakeRunner(rec, hang=True))
    assert "runner cleanup timed out" in caplog.text
    assert rec.order[-1] == "store.close"


@pytest.mark.asyncio
async def test_shutdown_without_a_store_is_fine():
    """The store degrades to None when the disk is unhappy; shutdown must cope."""
    rec = _Recorder()
    await _run_shutdown(rec, store=None)
    assert "store.close" not in rec.order
    assert rec.order[-1] == "delivery.close"
