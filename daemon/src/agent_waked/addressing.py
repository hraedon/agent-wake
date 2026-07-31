"""The addressing model: senders, principals, destinations, routes (WI-006).

Why this module exists
---------------------
Up to v1 the daemon had exactly one addressing noun — a ``sources`` entry —
and it carried three unrelated things at once.  On the live mvmcc03 config a
single entry held ``secret_env`` **and** ``allowed_trigger_identities``
(credential), ``principal_id`` (actor identity) **and** ``callback_url``
(reply destination), while ``routing`` was keyed on that same name to pick an
adapter.  Every question that needed one of the three separately had nowhere
to hang the answer:

* *which session of a multi-session adapter?* — no session-level addressee
  existed, so the opencode adapter fanned out to all of them (BC-001);
* *deliver to more than one place?* — a name-keyed table maps to exactly one
  adapter (BC-WAKE-022);
* *cap connections per adapter?* — no per-destination object to cap
  (BC-WAKE-010);
* *who is the peer on the way out?* — replies, label subscriptions and
  permission relays had no principal-scoped notion of the peer, which is why
  they are still unauthenticated (BC-WAKE-008/017/018).

The three nouns
---------------
**Sender** — a *credential*, and nothing else.  An HMAC key (one or more URIs
during a rotation window) plus the set of trigger identities allowed to use
it, plus where a reply to it goes.  Box configuration: it says who may inject
an event, never who gets woken.

**Principal** — an *actor identity*, spelled with the estate's ``principal_id``
vocabulary (``agent:…`` / ``human:…`` / ``key:sha256:…``) so attribution lines
up with regista and cairn.  A principal owns zero or more out-of-band delivery
channels (webhook / email — what v1 called the top-level ``delivery`` block)
and is named by the destinations that belong to it.

**Destination** — one *addressable place*: an adapter, optionally narrowed to a
single session of that adapter, belonging to a principal, with its own
connection cap.  This is the object BC-001, BC-WAKE-010 and BC-WAKE-012 were
all missing.

**Route** — the authorization edge, expressed sender → principal →
destination(s).  A sender may reach exactly the destinations its routes name;
there is no implicit "any adapter that asked for this name" except in the
legacy compatibility shape derived below.

Normalisation, not two code paths
---------------------------------
``config.load_config`` always emits the v2 vocabulary, deriving it from a v0/v1
``sources`` + ``routing`` + ``delivery`` file when that is what is on disk (see
:func:`derive_from_legacy`).  The accessors here also fall back to deriving on
the fly, because unit tests and the legacy inline-secret ingest path build
config dicts by hand; a predicate that only works on ``load_config`` output is
a predicate that silently changes behaviour depending on who built the dict.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("agent_waked.addressing")

#: Marks a route the daemon synthesised from a legacy ``routing`` block rather
#: than one an operator wrote.  Load-bearing for authorization: migration must
#: never *grant* authority the old file did not grant, so out-of-band delivery
#: authorization (``meta.target``) ignores legacy-origin routes.  See
#: :func:`out_of_band_principals`.
ORIGIN_LEGACY = "legacy"
ORIGIN_CONFIG = "config"

#: ``destination.adapter = None`` means "whichever adapter claimed this name",
#: the v1 §5.4 rule-2 behaviour for a source with no ``routing`` entry.
ANY_ADAPTER = None


class AddressingError(Exception):
    """A config-shape problem in the addressing model.

    Carries a stable ``code`` so ingest can map it to an HTTP status without
    string-matching a message.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Destination:
    """One addressable place an event can be delivered to."""

    name: str
    adapter: str | None = ANY_ADAPTER
    session: str | None = None
    principal: str | None = None
    max_connections: int | None = None

    @property
    def session_scoped(self) -> bool:
        return self.session is not None

    def accepts_adapter(self, adapter: str) -> bool:
        return self.adapter is None or self.adapter == adapter

    def to_frame(self) -> dict[str, Any]:
        """The ``destination`` block carried on a ``wake`` frame.

        Session is what closes BC-001: an adapter that multiplexes sessions can
        finally be told *which* one, instead of guessing or broadcasting.
        """
        out: dict[str, Any] = {"name": self.name}
        if self.adapter is not None:
            out["adapter"] = self.adapter
        if self.session is not None:
            out["session"] = self.session
        if self.principal is not None:
            out["principal"] = self.principal
        return out


@dataclass(frozen=True)
class Route:
    """An authorization edge: this sender may reach these destinations."""

    sender: str
    principal: str | None = None
    destinations: tuple[str, ...] = field(default_factory=tuple)
    origin: str = ORIGIN_CONFIG


# ── derivation from the legacy vocabulary ─────────────────────────────────────


def derive_from_legacy(
    sources: dict[str, Any],
    routing: dict[str, Any],
    delivery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Translate a v0/v1 ``sources`` + ``routing`` + ``delivery`` file to v2.

    The name mapping is deliberately identity-preserving: **a legacy source
    name becomes both a sender name and a destination name.**  That is not
    laziness, it is what makes the migration invisible to everything already
    deployed — a connected adapter's ``hello.filters.sources`` is a list of
    those same names, the ``hello_ack.accepted_sources`` it gets back is
    unchanged, and rows already in the durable ``pending`` queue (keyed on
    ``source``) address the right destination without rewriting their payloads.

    ``routing[s].adapter`` becomes ``destinations[s].adapter``.  A source with
    no routing entry becomes a destination with ``adapter = None``, preserving
    the v1 §5.4 rule-2 "any adapter that claimed it" behaviour.  Legacy
    ``principal_id`` becomes the destination's principal *and* the sender's own
    ``identity`` — see the note in :func:`trigger_identity_for` about why that
    single field was doing both jobs and doing at least one of them wrong.
    """
    senders: dict[str, Any] = {}
    destinations: dict[str, Any] = {}
    routes: list[dict[str, Any]] = []

    for name, info in sources.items():
        if not isinstance(info, dict):
            continue
        principal = info.get("principal_id")
        senders[name] = dict(info)
        route_entry = routing.get(name) if isinstance(routing, dict) else None
        adapter = None
        if isinstance(route_entry, dict):
            adapter = route_entry.get("adapter")
        destinations[name] = {
            "adapter": adapter,
            "session": None,
            "principal": principal,
        }
        routes.append(
            {
                "sender": name,
                "principal": principal,
                "destinations": [name],
                "origin": ORIGIN_LEGACY,
            }
        )

    principals: dict[str, Any] = {}
    for pid, channels in (delivery or {}).items():
        principals[pid] = {"channels": channels}
    # A principal named only by a destination still exists as an actor even
    # with no out-of-band channels; materialise it so `principal_table` is the
    # single answer to "is this a known principal".
    for dest in destinations.values():
        pid = dest.get("principal")
        if pid and pid not in principals:
            principals[pid] = {"channels": {}}

    return {
        "senders": senders,
        "destinations": destinations,
        "principals": principals,
        "routes": routes,
    }


# ── accessors (work on normalised *or* hand-built config dicts) ───────────────


def _legacy_view(cfg: dict[str, Any]) -> dict[str, Any]:
    return derive_from_legacy(
        cfg.get("sources") or {},
        cfg.get("routing") or {},
        cfg.get("delivery") or {},
    )


def sender_table(cfg: dict[str, Any]) -> dict[str, Any]:
    """Every configured sender credential, keyed by sender name."""
    senders = cfg.get("senders")
    if isinstance(senders, dict):
        return senders
    sources = cfg.get("sources")
    return sources if isinstance(sources, dict) else {}


def sender(cfg: dict[str, Any], name: str) -> dict[str, Any] | None:
    entry = sender_table(cfg).get(name)
    return entry if isinstance(entry, dict) else None


def principal_table(cfg: dict[str, Any]) -> dict[str, Any]:
    """Every known principal, keyed by ``principal_id``."""
    principals = cfg.get("principals")
    if isinstance(principals, dict):
        return principals
    derived: dict[str, Any] = _legacy_view(cfg)["principals"]
    return derived


def principal_channels(cfg: dict[str, Any], principal_id: str) -> dict[str, Any] | None:
    """Out-of-band delivery channels for *principal_id*, or None if unknown.

    Reads the v2 ``principals[pid].channels`` and falls back to the v1
    top-level ``delivery`` table, so a deployment that has not migrated keeps
    exactly the delivery behaviour it had.
    """
    principals = cfg.get("principals")
    if isinstance(principals, dict):
        entry = principals.get(principal_id)
        if not isinstance(entry, dict):
            return None
        channels = entry.get("channels")
        return channels if isinstance(channels, dict) else {}
    legacy = cfg.get("delivery")
    if isinstance(legacy, dict):
        entry = legacy.get(principal_id)
        return entry if isinstance(entry, dict) else None
    return None


def destination_table(cfg: dict[str, Any]) -> dict[str, Destination]:
    """Every configured destination, keyed by destination name."""
    raw = cfg.get("destinations")
    if not isinstance(raw, dict):
        raw = _legacy_view(cfg)["destinations"]
    out: dict[str, Destination] = {}
    for name, info in raw.items():
        if isinstance(info, Destination):
            out[name] = info
            continue
        if not isinstance(info, dict):
            continue
        out[name] = Destination(
            name=name,
            adapter=info.get("adapter"),
            session=info.get("session"),
            principal=info.get("principal"),
            max_connections=info.get("max_connections"),
        )
    return out


def destination(cfg: dict[str, Any], name: str) -> Destination | None:
    return destination_table(cfg).get(name)


def route_table(cfg: dict[str, Any]) -> list[Route]:
    """Every route, in config order."""
    raw = cfg.get("routes")
    if not isinstance(raw, list):
        raw = _legacy_view(cfg)["routes"]
    out: list[Route] = []
    for entry in raw:
        if isinstance(entry, Route):
            out.append(entry)
            continue
        if not isinstance(entry, dict):
            continue
        dests = entry.get("destinations") or []
        if isinstance(dests, str):
            dests = [dests]
        out.append(
            Route(
                sender=str(entry.get("sender", "")),
                principal=entry.get("principal"),
                destinations=tuple(str(d) for d in dests),
                origin=str(entry.get("origin", ORIGIN_CONFIG)),
            )
        )
    return out


# ── resolution ───────────────────────────────────────────────────────────────


def _expand(cfg: dict[str, Any], route: Route) -> list[Destination]:
    """Destinations a single route names.

    ``destinations`` is explicit; a route with only ``principal`` means "every
    destination belonging to that principal", which is the ergonomic form for
    "wake me wherever I am" and the reason the shape is sender → principal →
    destination(s) rather than sender → destination.
    """
    table = destination_table(cfg)
    if route.destinations:
        return [table[d] for d in route.destinations if d in table]
    if route.principal:
        return [d for d in table.values() if d.principal == route.principal]
    return []


def routed_destinations(cfg: dict[str, Any], sender_name: str) -> list[Destination]:
    """Every destination *sender_name* is authorised to reach, de-duplicated.

    Order is config order with duplicates dropped, so an operator can predict
    fan-out order from the file (BC-WAKE-022).
    """
    seen: dict[str, Destination] = {}
    for route in route_table(cfg):
        if route.sender not in (sender_name, "*"):
            continue
        for dest in _expand(cfg, route):
            seen.setdefault(dest.name, dest)
    return list(seen.values())


def out_of_band_principals(cfg: dict[str, Any], sender_name: str) -> set[str] | None:
    """Principals *sender_name* may address out-of-band (``meta.target``).

    Returns ``None`` when the sender declares no authorization at all, which the
    caller must treat as default-deny — the Plan 005 contract, unchanged.

    Deliberately **not** derived from routes, even though routes are what
    authorise in-band delivery and even though both are "reach this principal".
    The two are different capabilities and only one of them leaves the box:
    waking an adapter session over a 0600 unix socket is local, while an
    out-of-band channel sends a real email or POSTs to a URL the daemon resolves
    — the surface ``netguard`` and the SSRF guards exist for.  Collapsing them
    would buy one fewer key in a config file and pay for it by turning every
    in-band route into egress authority, including on migration, where a v1
    ``routing`` entry would silently become permission to email a human.
    Migrations may preserve authority; they may not mint it.
    """
    entry = sender(cfg, sender_name) or {}
    declared = entry.get("allowed_target_principals")
    if isinstance(declared, list) and declared:
        return {str(p) for p in declared}
    return None


def resolve(
    cfg: dict[str, Any],
    sender_name: str,
    *,
    destination_name: str | None = None,
    principal_id: str | None = None,
) -> list[Destination]:
    """Resolve the destination set an event from *sender_name* addresses.

    The sender's routes are the outer bound; ``destination_name`` and
    ``principal_id`` (from ``meta.destination`` / ``meta.principal``) only ever
    *narrow* it.  A narrowing that falls outside the routed set is an
    authorization failure, not an empty result — otherwise a sender could probe
    for the existence of destinations it may not reach by watching whether it
    gets ``no_subscriber`` or a 403.

    Raises :class:`AddressingError` with codes ``destination_not_routed`` /
    ``principal_not_routed``.
    """
    allowed = routed_destinations(cfg, sender_name)

    if destination_name is not None:
        match = [d for d in allowed if d.name == destination_name]
        if not match:
            raise AddressingError(
                "destination_not_routed",
                f"sender {sender_name!r} has no route to destination "
                f"{destination_name!r}",
            )
        allowed = match

    if principal_id is not None:
        match = [d for d in allowed if d.principal == principal_id]
        if not match:
            raise AddressingError(
                "principal_not_routed",
                f"sender {sender_name!r} has no route to principal "
                f"{principal_id!r}",
            )
        allowed = match

    return allowed


def trigger_identity_for(
    cfg: dict[str, Any], sender_name: str, asserted: str | None
) -> str | None:
    """The ``meta.trigger_identity`` to stamp: *who asked for this wake*.

    ``core/schema.md`` defines the field as "``principal_id`` of the source
    that sent the event", and v1 implemented it as the source's static
    ``principal_id``.  On the one real deployment that produced a wrong answer:
    source ``mvmcc03-claude`` carried ``principal_id:
    "agent:mvmcc03-claude"`` — the identity of the agent being *woken* — while
    its ``allowed_trigger_identities`` were ``human:itadmin`` and
    ``mvmcc03-agent``, the identities that actually trigger it.  Every event
    was therefore attributed to its own addressee.  That is the conflation
    WI-006 is about, visible in production data.

    So: the authenticated ``X-AgentWake-Identity`` header wins when present —
    it is the identity the allowlist just checked, i.e. the one the daemon has
    actually authorised — and the sender's declared ``identity`` is the
    fallback for senders that do not assert one.  Deployments with no header
    and a legacy ``principal_id`` keep the value they had.
    """
    if asserted:
        return asserted
    entry = sender(cfg, sender_name) or {}
    identity = entry.get("identity") or entry.get("principal_id")
    return str(identity) if identity else None
