"""Outbound-target address guard (SSRF).

One place that answers "may the daemon open a connection to this address?".
Before this module the forbidden-range predicate lived only in
``config._assert_safe_webhook_url``, i.e. it ran **once, at config load**.
That leaves a DNS-rebinding window open for the entire life of the process:
a hostname that resolved to a public address at boot can resolve to
``169.254.169.254`` an hour later, and the delivery path never looked again.

``WebhookChannel`` now re-checks on the request path via ``acheck_url``, so
the exposure shrinks from "until the daemon restarts" to the microseconds
between our resolution and aiohttp's own. Closing that last gap requires
pinning the connection to the address we validated (a custom resolver /
connector); see the module docstring of ``channels/webhook.py`` for why that
is deliberately not done yet.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

# RFC 6598 shared address space (CGNAT) — ``is_private`` is False for this
# range, so it must be checked explicitly to prevent SSRF via a
# CGNAT-internal target.
CGNAT_RANGE = ipaddress.ip_network("100.64.0.0/10")


def is_forbidden_address(addr: str) -> bool:
    """True if *addr* is a loopback/private/reserved address.

    An address that does not parse as an IP is not forbidden here — the
    caller decides what to do with garbage from the resolver.

    IPv4-mapped IPv6 forms (``::ffff:100.64.0.1``, and its equally valid
    spellings ``::ffff:6440:1`` and ``0:0:0:0:0:ffff:100.64.0.1``) are unwrapped
    to the v4 address before the range tests. This is not hygiene, it was a live
    bypass: ``ip in CGNAT_RANGE`` compares an ``IPv6Address`` against an
    ``IPv4Network``, and ``_BaseNetwork.__contains__`` returns False on a
    version mismatch rather than raising. CPython's own mapped-address handling
    rescued ``is_private`` (so ``::ffff:127.0.0.1`` and
    ``::ffff:169.254.169.254`` were correctly refused) but not CGNAT, because
    100.64.0.0/10 is not in ``_private_networks``. The mapped form is a routable
    alias — the connection lands on 100.64.0.1 all the same.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or ip in CGNAT_RANGE
    )


def forbidden_address(addrs: list[str]) -> str | None:
    """Return the first forbidden address in *addrs*, or None if all are OK."""
    for addr in addrs:
        if is_forbidden_address(addr):
            return addr
    return None


def resolve_hostname(host: str) -> list[str]:
    """Resolve *host* to its IP address strings (blocking).

    Module-level so tests can monkeypatch DNS (the offline sandbox has no
    resolver).
    """
    infos = socket.getaddrinfo(host, None)
    return [str(info[4][0]) for info in infos]


async def aresolve_hostname(host: str) -> list[str]:
    """Resolve *host* without blocking the event loop.

    ``socket.getaddrinfo`` can block for the full resolver timeout; on the
    delivery path that would stall every other coroutine, so go through the
    loop's threadpool-backed resolver instead.
    """
    infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    return [str(info[4][0]) for info in infos]


async def acheck_url(url: str) -> str | None:
    """Return a reason string if *url* is an unsafe outbound target, else None.

    Errors are returned rather than raised: the delivery path turns them into
    a ``{"status": "failed"}`` result, it does not crash.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"disallowed scheme {parsed.scheme!r}"
    host = parsed.hostname
    if not host:
        return "URL has no hostname"
    try:
        addrs = await aresolve_hostname(host)
    except OSError as exc:
        return f"hostname {host!r} does not resolve: {exc}"
    if not addrs:
        return f"hostname {host!r} resolved to no addresses"
    bad = forbidden_address(addrs)
    if bad is not None:
        return (
            f"hostname {host!r} resolves to forbidden address {bad} "
            f"(loopback/private/reserved)"
        )
    return None


__all__ = [
    "CGNAT_RANGE",
    "acheck_url",
    "aresolve_hostname",
    "forbidden_address",
    "is_forbidden_address",
    "resolve_hostname",
]
