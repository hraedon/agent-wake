"""WI-026 meta-guard: prove agent-wake's conformance gate *runs*, not *skips*.

The silent-skip bug (2026-07-24) bit three components: their
``test_cli_conformance.py`` did ``pytest.importorskip("agent_suite.conformance")``
against a module that was never installed, so every case skipped, CI stayed
green, and zero contract was enforced. A skipped gate is indistinguishable from a
passing one in a green build — the canonical "fails open" hazard.

``assert_cases_declared`` (dogfooded in ``test_cli_conformance.py``) catches the
"module loaded but a dimension is empty" half of the class. This file catches the
other half — "the whole module skipped" — by running the conformance module as a
subprocess and asserting at least one case *passed* (not all-skipped).

The guard is factored into a pure function (``require_gate_ran``) so a deny-case
can prove it rejects an all-skip summary — not a tautology over string matching.
An end-to-end falsifier builds a tiny module that importorskip's a bogus name and
confirms the guard flags it. Mirrors agent-suite's ``tests/test_conformance_meta_guard.py``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

conformance = pytest.importorskip("agent_suite.conformance")
ConformanceGateError = conformance.ConformanceGateError

# daemon/tests/<this file> → daemon root.
DAEMON_ROOT = Path(__file__).resolve().parents[1]
CONFORMANCE_TEST = DAEMON_ROOT / "tests" / "test_cli_conformance.py"

# A pytest -q summary line ends with the timing suffix "in <number>s". Anchoring
# to that suffix — rather than scanning the whole captured stream — means a
# count-like token printed by a test under examination (e.g. a CLI emitting JSON
# with a "passed" field) cannot be mistaken for pytest's own summary. Counts are
# parsed from the LAST summary line only.
_SUMMARY_LINE_RE = re.compile(r"^(?P<line>.*?\bin \d+(?:\.\d+)?s)\s*$", re.MULTILINE)
_COUNT_RE = {
    "passed": re.compile(r"(\d+)\s+passed"),
    "skipped": re.compile(r"(\d+)\s+skipped"),
    "failed": re.compile(r"(\d+)\s+failed"),
    "error": re.compile(r"(\d+)\s+errors?\b"),
}


def _summary_counts(output: str) -> dict[str, int]:
    """Parse pytest counts from the LAST summary line in ``output``.

    Returns zeros when no summary line is present (a crashed/aborted pytest that
    never printed one) — ``require_gate_ran`` treats that as "did not run".
    """
    summary_matches = list(_SUMMARY_LINE_RE.finditer(output))
    line = summary_matches[-1].group("line") if summary_matches else ""
    counts: dict[str, int] = {}
    for key, pattern in _COUNT_RE.items():
        m = pattern.search(line)
        counts[key] = int(m.group(1)) if m else 0
    return counts


def require_gate_ran(
    output: str,
    *,
    exit_code: int = 0,
    minimum_passed: int = 1,
) -> dict[str, int]:
    """Meta-guard: assert the conformance gate ran at least ``minimum_passed``
    cases AND exited cleanly.

    Two independent signals, both required:
    - ``exit_code == 0`` — pytest exited success. A nonzero exit (failures,
      collection error, "no tests ran" exit-5, interrupt) means the gate did not
      pass cleanly, whatever the summary says.
    - ``passed >= minimum_passed`` parsed from the last summary line — catches
      the importorskip class, where pytest exits 0 but every case skipped.

    Raises ``ConformanceGateError`` with the exit code, parsed counts, and a
    short output fragment so the failure is debuggable. Returns the counts.
    """
    counts = _summary_counts(output)
    fragment = output.strip().splitlines()[-1] if output.strip() else "<no output>"
    if exit_code != 0:
        raise ConformanceGateError(
            f"conformance gate did not exit cleanly (exit {exit_code}); "
            f"counts={counts}; last line: {fragment!r}"
        )
    if counts["passed"] < minimum_passed:
        raise ConformanceGateError(
            f"conformance gate ran {counts['passed']} case(s) (minimum "
            f"{minimum_passed}); {counts['skipped']} skipped. An all-skip, "
            f"zero-pass run means importorskip fired against a missing/wrong kit "
            f"module — the gate enforced nothing. See docs/cli-contract.md §7 "
            f"(WI-026). Last line: {fragment!r}"
        )
    return counts


def _run_pytest(test_path: Path) -> subprocess.CompletedProcess[str]:
    """Run pytest on ``test_path`` with color disabled and return the result.

    Color is forced off (``--color=no`` + ``NO_COLOR``/``PY_COLORS`` overrides)
    so ANSI escape sequences can never corrupt the summary-line parse. Runs with
    ``cwd=DAEMON_ROOT`` so the daemon's ``pytest.ini`` ``pythonpath = ["src"]``
    applies in a source checkout; in the built-wheel lane the package is installed
    and the pythonpath is a harmless no-op.
    """
    env = {
        **os.environ,
        "NO_COLOR": "1",
        "PY_COLORS": "0",
    }
    env.pop("FORCE_COLOR", None)
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", str(test_path),
            "-q", "-p", "no:cacheprovider", "--no-header", "--color=no",
        ],
        cwd=DAEMON_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    return proc


def test_conformance_gate_runs_at_least_one_case() -> None:
    """The live conformance module must exit 0 and pass >=1 case (not all-skip).

    If a future kit rename or layout change makes ``importorskip`` fire again,
    one of the two signals in ``require_gate_ran`` goes red — the structural
    cure for the 2026-07-24 bug.
    """
    proc = _run_pytest(CONFORMANCE_TEST)
    counts = require_gate_ran(
        proc.stdout + proc.stderr, exit_code=proc.returncode, minimum_passed=1
    )
    assert counts["passed"] >= 1, counts


def test_require_gate_ran_accepts_a_clean_passing_summary() -> None:
    """Positive control: a clean exit-0 run with a passing summary passes."""
    counts = require_gate_ran(
        "..........                               [100%]\n10 passed in 1.24s\n",
        exit_code=0,
    )
    assert counts["passed"] == 10


def test_require_gate_ran_rejects_an_all_skip_summary() -> None:
    """Deny case: an exit-0 all-skip summary (importorskip fired) is rejected."""
    with pytest.raises(ConformanceGateError, match="importorskip"):
        require_gate_ran(
            "s.....                                   [100%]\n5 skipped in 0.5s\n",
            exit_code=0,
        )


def test_require_gate_ran_rejects_nonzero_exit() -> None:
    """Deny case: a nonzero exit is rejected even if a 'passed' count appears —
    a passing test's stdout printing count-like text must not mask a failure."""
    with pytest.raises(ConformanceGateError, match="did not exit cleanly"):
        require_gate_ran(
            "1 passed in 0.5s\n",  # decoy summary that looks healthy
            exit_code=1,
        )


def test_require_gate_ran_ignores_count_like_test_output() -> None:
    """Deny case: count-like strings printed by a test under examination must not
    be parsed as pytest's summary. Here the real summary is all-skip; a stray
    '1 passed' earlier in the stream must not produce a false accept."""
    noisy = "some CLI printed: 1 passed, awesome\n5 skipped in 0.5s\n"
    with pytest.raises(ConformanceGateError, match="importorskip"):
        require_gate_ran(noisy, exit_code=0)


def test_require_gate_ran_rejects_when_no_summary_printed() -> None:
    """Deny case: a crashed pytest that printed no summary is rejected."""
    with pytest.raises(ConformanceGateError):
        require_gate_ran("Fatal Python error: Segmentation fault\n", exit_code=-11)


def test_meta_guard_detects_a_real_importorskip_skip(tmp_path: Path) -> None:
    """End-to-end falsifier: a module that importorskip's a bogus name fails to
    collect, and ``require_gate_ran`` flags it.

    When importorskip fires at module level, the module's test functions are
    never collected — running that file in isolation yields an all-skip / no-run
    outcome (pytest exits 5). ``require_gate_ran`` rejects it via either signal:
    the nonzero exit, or — on a pytest version that exits 0 here — the zero-pass
    count. Either way the skip class is caught, not silently green.
    """
    bogus = tmp_path / "test_skips_silently.py"
    bogus.write_text(
        "import pytest\n"
        "pytest.importorskip('agent_suite.conformance.this_does_not_exist_xyz')\n"
        "def test_dummy() -> None:\n"
        "    assert False  # would fail if it ran; it must not run\n"
    )
    proc = _run_pytest(bogus)
    with pytest.raises(ConformanceGateError) as exc:
        require_gate_ran(proc.stdout + proc.stderr, exit_code=proc.returncode, minimum_passed=1)
    msg = str(exc.value)
    # The detection is robust: it must raise, and the message must name either
    # the nonzero exit or the zero-pass/importorskip condition.
    assert "did not exit cleanly" in msg or "importorskip" in msg, msg
