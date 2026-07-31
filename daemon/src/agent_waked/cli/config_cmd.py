"""``agent-wake config`` — inspect and migrate the addressing model.

Two subcommands, both read-only on the operator's file:

``config migrate``
    Print the v2 form of a v0/v1 config on stdout.  Nothing is written unless
    ``--write`` is given, and ``--write`` refuses to clobber: the daemon accepts
    the legacy file indefinitely, so there is no deadline that justifies editing
    a live config in place without the operator looking at the diff first.

``config show``
    Print the *resolved* addressing model — senders, principals, destinations,
    routes and what each sender can reach.  The one command that answers "who
    can wake what on this box" without reading three tables and joining them by
    hand, which is the question the fused ``sources`` entry made hard to ask.

Neither command reads secret material.  ``secret_env`` / ``secret`` / ``secrets``
are copied through as the *references* they are, so the output is safe to paste
into a ticket — the same property ``secrets.visibility`` relies on.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .. import addressing
from ..config import (
    CURRENT_CONFIG_VERSION,
    DEFAULT_CONFIG_PATH,
    ConfigError,
    load_config,
)


def _build_config_parsers(sub: argparse._SubParsersAction[Any]) -> None:
    config_p = sub.add_parser(
        "config", help="Inspect or migrate the daemon config"
    )
    config_sub = config_p.add_subparsers(dest="config_command")

    migrate_p = config_sub.add_parser(
        "migrate",
        help=f"Print the v{CURRENT_CONFIG_VERSION} form of a v0/v1 config",
    )
    migrate_p.add_argument(
        "path",
        nargs="?",
        help=f"Config file to read (default: $AGENT_WAKE_CONFIG or "
        f"{DEFAULT_CONFIG_PATH})",
    )
    migrate_p.add_argument(
        "--write",
        metavar="DEST",
        help="Write the migrated config to DEST instead of stdout. Refuses to "
        "overwrite an existing file.",
    )
    migrate_p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    migrate_p.set_defaults(func=_cmd_migrate)

    show_p = config_sub.add_parser(
        "show", help="Print the resolved addressing model"
    )
    show_p.add_argument(
        "--json", action="store_true", help="Output JSON instead of human text"
    )
    show_p.set_defaults(func=_cmd_show)


def migrate_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """Return the v2 form of a raw v0/v1 config document.

    Operates on the *raw* document rather than ``load_config`` output on purpose:
    the migrated file must carry the operator's own secret references and
    comments-as-keys through unchanged, not the loader's normalised
    ``secret_uris`` form, or migrating would rewrite the credential spelling as
    a side effect of reformatting the addressing.
    """
    if any(new in raw for _, new in (("sources", "senders"),)):
        raise ConfigError(
            "This config already uses the v2 'senders' vocabulary; there is "
            "nothing to migrate."
        )

    sources = raw.get("sources") or {}
    routing = raw.get("routing") or {}
    delivery = raw.get("delivery") or {}
    derived = addressing.derive_from_legacy(sources, routing, delivery)

    senders: dict[str, Any] = {}
    for name, info in sources.items():
        entry = {k: v for k, v in info.items() if k != "principal_id"}
        # ``principal_id`` on a sender meant "the identity to stamp as
        # trigger_identity". Under v2 that is ``identity``, and the destination
        # carries the *addressee's* principal — the two jobs the one field was
        # doing. Preserve the value under the name that matches the job.
        if info.get("principal_id"):
            entry["identity"] = info["principal_id"]
        senders[name] = entry

    out: dict[str, Any] = {"version": CURRENT_CONFIG_VERSION}
    for passthrough in (
        "listen",
        "socket_path",
        "default_callback_url",
        "state",
        "vault",
    ):
        if passthrough in raw:
            out[passthrough] = raw[passthrough]
    out["senders"] = senders
    out["principals"] = derived["principals"]
    out["destinations"] = {
        # Drop the null session: an explicit ``"session": null`` in a generated
        # file reads like a setting somebody chose, when it means "any session".
        name: {k: v for k, v in info.items() if v is not None}
        for name, info in derived["destinations"].items()
    }
    out["routes"] = [
        {"sender": r["sender"], "destinations": r["destinations"]}
        for r in derived["routes"]
    ]
    return out


def conflation_notes(migrated: dict[str, Any]) -> list[str]:
    """Flag senders whose migrated ``identity`` is really an addressee.

    The tell: a sender's ``identity`` equal to the principal of the destination
    it routes to means the v1 ``principal_id`` was naming *who gets woken*, not
    *who is asking* — so every event from that sender was attributed to its own
    addressee.  This is exactly what the live mvmcc03 config does, and it is the
    one thing a mechanical migration cannot fix for the operator: only they know
    which identity they meant.

    Advisory only.  The value is preserved as-is so behaviour does not change
    under them, and the authenticated ``X-AgentWake-Identity`` header already
    takes precedence when a sender sends one.
    """
    notes: list[str] = []
    destinations = migrated.get("destinations") or {}
    for sender_name, entry in (migrated.get("senders") or {}).items():
        identity = entry.get("identity")
        if not identity:
            continue
        owned = [
            name
            for name, dest in destinations.items()
            if dest.get("principal") == identity
        ]
        if owned:
            notes.append(
                f"sender {sender_name!r} has identity {identity!r}, which is "
                f"also the principal of destination(s) {', '.join(sorted(owned))}. "
                f"A sender's identity is who *asks* for a wake; a destination's "
                f"principal is whose attention is requested. If this sender is "
                f"triggered by a human or a service, set 'identity' to that "
                f"principal_id (or drop it and let the "
                f"X-AgentWake-Identity header supply it) so events stop being "
                f"attributed to their own addressee."
            )
    return notes


def _cmd_migrate(args: argparse.Namespace) -> int:
    import os

    path = (
        Path(args.path)
        if args.path
        else Path(os.environ.get("AGENT_WAKE_CONFIG") or DEFAULT_CONFIG_PATH)
    )
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # Deliberately not chained: JSONDecodeError retains the whole document
        # in .doc, which may carry secrets.
        raise ConfigError(
            f"Config file {path} is not valid JSON near line {exc.lineno}: "
            f"{exc.msg}"
        ) from None
    if not isinstance(raw, dict):
        raise ConfigError(f"Config file {path} must be a JSON object.")

    migrated = migrate_raw(raw)
    rendered = json.dumps(migrated, indent=2) + "\n"

    for note in conflation_notes(migrated):
        # stderr, so stdout stays a valid JSON document you can redirect.
        print(f"note: {note}", file=sys.stderr)

    if args.write:
        dest = Path(args.write)
        if dest.exists():
            raise ConfigError(
                f"{dest} already exists; refusing to overwrite. The daemon "
                f"accepts the v1 file indefinitely, so review the migration "
                f"before replacing anything."
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rendered, encoding="utf-8")
        # 0600: a config carries secret *references* and routing, and the
        # original is 0600 on every deployment we ship.
        dest.chmod(0o600)
        print(f"wrote {dest}")
        return 0

    print(rendered, end="")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    cfg = load_config()
    senders = addressing.sender_table(cfg)
    destinations = addressing.destination_table(cfg)
    principals = addressing.principal_table(cfg)
    routes = addressing.route_table(cfg)

    reach = {
        name: [d.name for d in addressing.routed_destinations(cfg, name)]
        for name in senders
    }

    if args.json:
        print(
            json.dumps(
                {
                    "version": cfg.get("version"),
                    "senders": {
                        name: {
                            "secret_uris": info.get("secret_uris"),
                            "identity": info.get("identity"),
                            "allowed_trigger_identities": info.get(
                                "allowed_trigger_identities"
                            ),
                            "reaches": reach[name],
                            "out_of_band_principals": sorted(
                                addressing.out_of_band_principals(cfg, name) or []
                            ),
                        }
                        for name, info in senders.items()
                    },
                    "principals": {
                        pid: {"channels": sorted(entry.get("channels") or {})}
                        for pid, entry in principals.items()
                    },
                    "destinations": {
                        d.name: {
                            "adapter": d.adapter,
                            "session": d.session,
                            "principal": d.principal,
                            "max_connections": d.max_connections,
                        }
                        for d in destinations.values()
                    },
                    "routes": [
                        {
                            "sender": r.sender,
                            "principal": r.principal,
                            "destinations": list(r.destinations),
                            "origin": r.origin,
                        }
                        for r in routes
                    ],
                },
                indent=2,
            )
        )
        return 0

    lines: list[str] = [f"addressing model (config version {cfg.get('version')})", ""]
    lines.append("senders (credential + trigger-identity allowlist)")
    for name, info in senders.items():
        allow = info.get("allowed_trigger_identities") or []
        lines.append(f"  {name}")
        lines.append(f"    identity   {info.get('identity') or '(none declared)'}")
        lines.append(
            f"    triggers   {', '.join(allow) if allow else 'any authenticated sender'}"
        )
        lines.append(f"    reaches    {', '.join(reach[name]) or '(nothing)'}")
        oob = addressing.out_of_band_principals(cfg, name)
        lines.append(
            f"    out-of-band {', '.join(sorted(oob)) if oob else 'denied'}"
        )
    lines.append("")
    lines.append("destinations (adapter, optionally one session, with its cap)")
    for d in destinations.values():
        bits = [f"adapter={d.adapter or 'any'}"]
        if d.session:
            bits.append(f"session={d.session}")
        if d.principal:
            bits.append(f"principal={d.principal}")
        if d.max_connections is not None:
            bits.append(f"max_connections={d.max_connections}")
        lines.append(f"  {d.name}: {'  '.join(bits)}")
    lines.append("")
    lines.append("principals (actor identity + out-of-band channels)")
    for pid, entry in principals.items():
        channels = sorted(entry.get("channels") or {})
        lines.append(f"  {pid}: {', '.join(channels) or 'no out-of-band channels'}")
    if any(r.origin == addressing.ORIGIN_LEGACY for r in routes):
        lines.append("")
        lines.append(
            "note: these routes were derived from a v0/v1 'routing' block, not "
            "written by hand. Run 'agent-wake config migrate' to see the v2 "
            "form of this file, including any conflated sender identities."
        )
    print("\n".join(lines))
    return 0
