"""agent-wake CLI — secret lifecycle, doctor, and harness install."""

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level ``agent-wake`` parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="agent-wake",
        description="agent-wake operator CLI",
    )
    sub = parser.add_subparsers(dest="command")

    # secrets — delegated to the secrets module
    from .secrets import _build_secrets_parser
    _build_secrets_parser(sub)

    # doctor
    doctor_p = sub.add_parser("doctor", help="Health check (suite-shaped)")
    doctor_p.add_argument(
        "--json",
        action="store_true",
        help="Output JSON to stdout (for suite-doctor aggregation)",
    )
    doctor_p.set_defaults(func=_cmd_doctor)

    # install-harness
    from .install_harness import _build_install_harness_parser
    _build_install_harness_parser(sub)

    return parser


def _cmd_doctor(args: argparse.Namespace) -> int:
    from ..doctor import run_checks, format_text
    import json

    report = run_checks()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_text(report))
    return 0 if report.get("ok") else 1


def main() -> int:
    """Entry point for the ``agent-wake`` console script."""
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
