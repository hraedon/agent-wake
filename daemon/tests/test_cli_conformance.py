"""agent-wake's CLI run through the CLI contract v1 conformance kit (Plan 018 WI-2).

The kit is the centrally versioned package ``agent-suite-conformance``, consumed
pinned as ``agent-suite-conformance==1.0.0`` from PyPI (Plan 019 B1) via the
``[dev]`` extra — never copied, never imported by runtime code. These are
agent-wake's component-side fixtures against its own CLI.

Import note: the published ``agent-suite-conformance`` wheel ships the kit at
``agent_suite/conformance/`` as a PEP-420 namespace package, so the import path
is ``agent_suite.conformance`` (the same path the umbrella agent-suite uses).
agent-wake does not install the umbrella package; the wheel alone provides this
namespace.

Scope note (Plan 019 B3 / WI-023, the safe first pass — mirrors acb/cairn):

- **§2/§3** via an ``ErrorCase``: ``secrets list --json`` against a missing config
  is a documented operational failure — the top-level boundary emits a
  ``CONFIG_ERROR`` envelope on stdout with exit 1 (this pass adds that boundary
  and the ``--json`` flag; previously the secrets surface had no envelope path).
- **§2** via a ``UsageCase``: an unknown verb exits 2 (native argparse).
- **§4** via a ``BrokenPipeCase``: closing ``doctor --json``'s stdout early
  produces no traceback (the boundary swallows ``BrokenPipeError`` by construction,
  not by luck of small output).

A ``SuccessCase`` is honestly omitted: ``doctor --json`` is a health *reporter* —
it emits a valid report and exits 1 when the box is merely unconfigured, which is
neither a clean exit-0 success nor an operational-error envelope (same as
cairn/acb). ``install-harness --dry-run`` returns exit 2 as a load-bearing
"would-install" signal and is deliberately left untouched (like acb's act-path),
so no case is asserted over it here.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

# Installed by the [dev] extra as a pinned PyPI dep (fails loudly in CI, never a
# silent skip); importorskip keeps a kit-less local checkout from erroring. The
# published wheel ships this as the agent_suite.conformance PEP-420 namespace.
conformance = pytest.importorskip("agent_suite.conformance")

BrokenPipeCase = conformance.BrokenPipeCase
ErrorCase = conformance.ErrorCase
UsageCase = conformance.UsageCase
run_broken_pipe_case = conformance.run_broken_pipe_case
run_error_case = conformance.run_error_case
run_usage_case = conformance.run_usage_case

# The ``agent-wake`` console script installed next to this interpreter.
_AW = str(Path(sys.executable).parent / "agent-wake")

# An empty HOME so probes read no real agent-wake config/adapters.
_EMPTY_HOME = tempfile.mkdtemp(prefix="agent-wake-conformance-home-")

# A config path that cannot resolve, forcing the documented operational error.
_MISSING_CONFIG = "/nonexistent/agent-wake-conformance/config.json"


ERROR_CASES = [
    # A missing config is an operational failure: `secrets list --json` emits the
    # contract envelope on stdout with exit 1, code CONFIG_ERROR.
    ErrorCase(
        name="secrets-list-missing-config",
        argv=(_AW, "secrets", "list", "--json"),
        expect_code="CONFIG_ERROR",
        env={"HOME": _EMPTY_HOME, "AGENT_WAKE_CONFIG": _MISSING_CONFIG},
    ),
]

USAGE_CASES = [
    UsageCase(name="unknown-verb", argv=(_AW, "bogusverb")),
]

BROKEN_PIPE_CASES = [
    BrokenPipeCase(
        name="doctor-broken-pipe",
        argv=(_AW, "doctor", "--json"),
        env={"HOME": _EMPTY_HOME, "AGENT_WAKE_CONFIG": _MISSING_CONFIG},
    ),
]


@pytest.mark.parametrize("case", ERROR_CASES, ids=lambda c: c.name)
def test_error_conformance(case: ErrorCase) -> None:
    assert run_error_case(case) == []


@pytest.mark.parametrize("case", USAGE_CASES, ids=lambda c: c.name)
def test_usage_conformance(case: UsageCase) -> None:
    assert run_usage_case(case) == []


@pytest.mark.parametrize("case", BROKEN_PIPE_CASES, ids=lambda c: c.name)
def test_broken_pipe_conformance(case: BrokenPipeCase) -> None:
    assert run_broken_pipe_case(case) == []
