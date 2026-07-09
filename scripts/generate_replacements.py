#!/usr/bin/env python3
"""Generate a git-filter-repo --replace-text file from the identifier denylist.

Reads ``.identifiers-denylist.local`` (gitignored) and writes a
``replacements.txt`` that ``git filter-repo --replace-text`` consumes. Each
non-comment token gets ``==>REDACTED`` appended.

The output file contains raw identifier values — do NOT commit it. The
canonical workflow (docs/publication-gate.md §2):

    python3 scripts/generate_replacements.py > /tmp/replacements.txt
    git filter-repo --dry-run --replace-text /tmp/replacements.txt --force
    # review the diff; if clean, no scrub is needed
    # if leaks found: git filter-repo --replace-text /tmp/replacements.txt

Exits non-zero if the denylist is missing or empty.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DENYLIST = REPO_ROOT / ".identifiers-denylist.local"


def main() -> int:
    if not DENYLIST.exists():
        print(
            f"error: {DENYLIST.name} not found — create it first "
            "(see docs/publication-gate.md §3)",
            file=sys.stderr,
        )
        return 1

    rules: list[str] = []
    for line in DENYLIST.read_text(encoding="utf-8").splitlines():
        content = line.split("#", 1)[0].strip()
        if content:
            rules.append(f"{content}==>REDACTED")

    if not rules:
        print(f"error: {DENYLIST.name} contains no usable tokens", file=sys.stderr)
        return 1

    print("\n".join(rules))
    print(f"\n# {len(rules)} replacement rules generated from {DENYLIST.name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
