"""Shared test fixtures for the agent-wake daemon tests.

The CI/offline sandbox has no DNS resolver, so webhook-URL SSRF validation
(which resolves hostnames to check against forbidden IP ranges) is mocked
here to return a public address by default — at both check points, config
load (``config._resolve_hostname``) and delivery time
(``netguard.aresolve_hostname``). Dedicated SSRF tests patch those themselves
to assert rejection of forbidden ranges.

The daemon also has durable state now, so the state directory is pinned for
the whole session — see ``_hermetic_state``.
"""

import pytest

import agent_waked.config as cfg_module
from agent_waked import netguard as netguard_module

_PUBLIC_IP = "93.184.216.34"  # example.com — a real public address


@pytest.fixture(autouse=True)
def _hermetic_state(tmp_path_factory, monkeypatch):
    """Pin the durable state directory away from the operator's real one.

    Without this, any test that opens a store without an explicit override
    falls through to ``~/.local/state/agent-wake/state.db`` — the operator's
    live dedupe window, next-session queue and dead-letter table. A test run
    then mutates real delivery state, and a dedupe row written by a test can
    suppress a genuine event afterwards.

    Session-wide rather than per-test on purpose: the leak came from tests
    that never mention the store at all, so opting in per test is exactly the
    thing that fails silently. Tests that need a specific location (including
    the subprocess ones, which pass it through the child env) still set it
    themselves and win, since they apply after this.
    """
    monkeypatch.setenv(
        "AGENT_WAKE_STATE_DIR", str(tmp_path_factory.mktemp("wake-state"))
    )


@pytest.fixture(autouse=True)
def _mock_dns_resolver(monkeypatch):
    """Default: resolve every webhook hostname to a public IP.

    Both check points are stubbed: ``config._resolve_hostname`` (config load)
    and ``netguard.aresolve_hostname`` (the per-request delivery-time guard).
    SSRF-specific tests override these after this fixture applies, returning
    forbidden addresses to assert rejection.
    """
    monkeypatch.setattr(
        cfg_module, "_resolve_hostname", lambda host: [_PUBLIC_IP]
    )

    async def _aresolve(host: str) -> list[str]:
        return [_PUBLIC_IP]

    monkeypatch.setattr(netguard_module, "aresolve_hostname", _aresolve)
