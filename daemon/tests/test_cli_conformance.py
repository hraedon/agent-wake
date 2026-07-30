"""agent-wake's CLI run through the CLI contract v1 conformance kit (Plan 018 WI-2).

The kit is the centrally versioned package ``agent-suite-conformance``, consumed
pinned as ``agent-suite-conformance==1.1.0`` (Plan 019 B1 / WI-023) via the
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
  ``CONFIG_ERROR`` envelope on stdout with exit 1.
- **§2** via a ``UsageCase``: an unknown verb exits 2 (native argparse).
- **§4** via a ``BrokenPipeCase``: closing ``doctor --json``'s stdout early
  produces no traceback (the boundary swallows ``BrokenPipeError`` by construction,
  not by luck of small output).

A ``SuccessCase`` is honestly omitted: ``doctor --json`` is a health *reporter* —
it emits a valid report and exits 1 when the box is merely unconfigured, which is
neither a clean exit-0 success nor an operational-error envelope (same as
cairn/acb). ``install-harness --dry-run`` returns exit 2 as a load-bearing
"would-install" signal. That is an explicit, UNRESOLVED conflict between two
contract documents, not a per-component bug: the general CLI contract §2 (WI-021,
ratified 2026-07-20) says a successful dry-run exits 0, whereas the install-harness
contract §4 normatively assigns exit 2 to "supported dry-run completed without
action." agent-wake follows the install-harness contract here, consistent with acb
(whose tests assert ``rc == 2`` on dry-run, e.g.
``test_install_harness_supported_json_dry_run_uses_contract_exit_two``). Flipping
agent-wake alone would diverge it from that contract and from acb, so the conflict
is deliberately left untouched in this slice; it must be reconciled at the contract
level in agent-suite (decide which document wins, and if §2 wins, amend
install-harness-contract §4 and change all four implementing components
atomically). No conformance case is therefore asserted over the dry-run verb here.

Meta-guards (WI-026, kit 1.1.0): two layers keep this gate from failing open.
``assert_cases_declared`` below fails collection loudly if a declared dimension
empties; ``test_conformance_meta_guard.py`` runs this module as a subprocess and
asserts at least one case *passed* (the layer that catches a whole-module
``importorskip`` skip).
"""

from __future__ import annotations

import sys
import tempfile

import pytest

# Installed by the [dev] extra as a pinned PyPI dep (fails loudly in CI, never a
# silent skip); importorskip keeps a kit-less local checkout from erroring. The
# published wheel ships this as the agent_suite.conformance PEP-420 namespace.
conformance = pytest.importorskip("agent_suite.conformance")

assert_cases_declared = conformance.assert_cases_declared
BrokenPipeCase = conformance.BrokenPipeCase
ErrorCase = conformance.ErrorCase
UsageCase = conformance.UsageCase
run_broken_pipe_case = conformance.run_broken_pipe_case
run_error_case = conformance.run_error_case
run_usage_case = conformance.run_usage_case

# Invoke the CLI by module (the family pattern — acb uses ``-m
# agent_capability_broker``, agent-notes uses ``-m agent_notes.cli``) rather than
# by guessing the console-script path next to the interpreter. This runs the
# *installed* package and stays correct when the interpreter and the script live
# in different directories (see agent_waked/cli/__main__.py).
_AW = (sys.executable, "-m", "agent_waked.cli")

# An empty HOME so probes read no real agent-wake config/adapters.
_EMPTY_HOME = tempfile.mkdtemp(prefix="agent-wake-conformance-home-")

# A config path that cannot resolve, forcing the documented operational error.
_MISSING_CONFIG = "/nonexistent/agent-wake-conformance/config.json"


ERROR_CASES = [
    # A missing config is an operational failure: `secrets list --json` emits the
    # contract envelope on stdout with exit 1, code CONFIG_ERROR.
    ErrorCase(
        name="secrets-list-missing-config",
        argv=(*_AW, "secrets", "list", "--json"),
        expect_code="CONFIG_ERROR",
        env={"HOME": _EMPTY_HOME, "AGENT_WAKE_CONFIG": _MISSING_CONFIG},
    ),
]

USAGE_CASES = [
    UsageCase(name="unknown-verb", argv=(*_AW, "bogusverb")),
]

BROKEN_PIPE_CASES = [
    BrokenPipeCase(
        name="doctor-broken-pipe",
        argv=(*_AW, "doctor", "--json"),
        env={"HOME": _EMPTY_HOME, "AGENT_WAKE_CONFIG": _MISSING_CONFIG},
    ),
]

# WI-026 meta-guard: fail collection loudly if any declared contract dimension
# empties. A zero-case dimension enforces nothing and — because this module is the
# kit-importing surface — would be indistinguishable from a pass in green CI.
#
# ``success`` is deliberately NOT passed: there is no honest SuccessCase (see the
# scope note above — doctor is a health reporter, not a clean exit-0 verb). Passing
# an empty ``success=[]`` would raise at collection and break the legitimate
# omission, so we guard exactly the three dimensions this component declares. The
# whole-module-skip class (which this guard cannot reach, because importorskip
# fires first) is covered by test_conformance_meta_guard.py.
assert_cases_declared(
    minimum=1,
    error=ERROR_CASES,
    usage=USAGE_CASES,
    broken_pipe=BROKEN_PIPE_CASES,
)


@pytest.mark.parametrize("case", ERROR_CASES, ids=lambda c: c.name)
def test_error_conformance(case: ErrorCase) -> None:
    assert run_error_case(case) == []


@pytest.mark.parametrize("case", USAGE_CASES, ids=lambda c: c.name)
def test_usage_conformance(case: UsageCase) -> None:
    assert run_usage_case(case) == []


@pytest.mark.parametrize("case", BROKEN_PIPE_CASES, ids=lambda c: c.name)
def test_broken_pipe_conformance(case: BrokenPipeCase) -> None:
    assert run_broken_pipe_case(case) == []
