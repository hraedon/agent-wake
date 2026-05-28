"""agent-wake CLI — secret lifecycle management and operator tooling."""

import sys


def main() -> int:
    """Entry point for the ``agent-wake`` console script."""
    from .secrets import build_parser

    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
