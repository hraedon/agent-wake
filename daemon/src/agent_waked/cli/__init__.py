"""agent-wake CLI — secret lifecycle, doctor, and harness install."""

import argparse
import json
import os
import sys

from ..config import ConfigError
from ..store import StoreError


def emit_error(
    code: str,
    message: str,
    *,
    use_json: bool,
    detail: str | None = None,
    retryable: bool = False,
    exit_code: int = 1,
) -> int:
    """Report an operational error per suite CLI contract v1 §3 and return the code.

    Under ``--json`` the common error envelope is the single stdout document;
    otherwise the human ``error:`` message goes to *stderr*. No path prints an
    error and exits 0. ``exit_code`` defaults to 1 — the operational-error slot in
    the taxonomy (0 success, 2 usage). The envelope shape is validated by
    ``agent_suite.conformance`` in the tests; it is reproduced here so runtime
    code never depends on the dev-only kit.
    """
    if use_json:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": code,
                        "message": message,
                        "detail": detail,
                        "retryable": retryable,
                        "partial": None,
                    },
                },
                indent=2,
            )
        )
    else:
        print(f"error: {message}", file=sys.stderr)
        if detail:
            print(f"  {detail}", file=sys.stderr)
    return exit_code


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

    # dead-letter / pending — operator visibility over the durable store
    from .queue import _build_queue_parsers
    _build_queue_parsers(sub)

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


def _dispatch(argv: list[str]) -> int:
    """Parse *argv* and run the selected subcommand (no error boundary)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    return int(args.func(args))


def main(argv: list[str] | None = None) -> int:
    """Entry point and last-resort error boundary (CLI contract §3/§4).

    argparse's usage errors raise ``SystemExit`` (exit 2) straight through — a
    ``BaseException``, not caught here, so the usage taxonomy is preserved. Any
    other uncaught exception becomes a contract envelope instead of a traceback:
    a ``ConfigError`` (missing/invalid config) maps to ``CONFIG_ERROR``, a
    ``StoreError`` (durable state unreadable) to ``STORE_ERROR``; anything
    else is reported as ``INTERNAL_ERROR``. A closed downstream pipe is swallowed
    the CPython way so the interpreter's final flush can't re-raise (§4).
    """
    raw = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in raw
    try:
        return _dispatch(raw)
    except BrokenPipeError:
        # A downstream reader closed the pipe (e.g. `agent-wake ... | head`).
        # Redirect stdout to devnull so the final flush at interpreter exit can't
        # raise, and exit without a traceback (§4).
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 1
    except ConfigError as exc:
        return emit_error("CONFIG_ERROR", str(exc), use_json=json_mode)
    except StoreError as exc:
        # The durable store could not be opened (bad path, permissions, corrupt
        # file). Retryable: fixing the path or permissions makes the same
        # command work.
        return emit_error(
            "STORE_ERROR", str(exc), use_json=json_mode, retryable=True
        )
    except Exception as exc:  # last-resort boundary: never surface a traceback
        return emit_error(
            "INTERNAL_ERROR",
            f"unexpected {exc.__class__.__name__}: {exc}",
            use_json=json_mode,
        )


if __name__ == "__main__":
    sys.exit(main())
