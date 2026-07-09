"""Shared test fixtures for the agent-wake daemon tests.

The CI/offline sandbox has no DNS resolver, so webhook-URL SSRF validation
(which resolves hostnames to check against forbidden IP ranges) is mocked
here to return a public address by default. Dedicated SSRF tests patch
``_resolve_hostname`` themselves to assert rejection of forbidden ranges.
"""

import pytest

import agent_waked.config as cfg_module

_PUBLIC_IP = "93.184.216.34"  # example.com — a real public address


@pytest.fixture(autouse=True)
def _mock_dns_resolver(monkeypatch):
    """Default: resolve every webhook hostname to a public IP.

    SSRF-specific tests override this by patching ``_resolve_hostname``
    after this fixture applies, returning forbidden addresses to assert
    rejection.
    """
    monkeypatch.setattr(
        cfg_module, "_resolve_hostname", lambda host: [_PUBLIC_IP]
    )
