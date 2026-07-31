"""Guard: the repo-root SUITE.lock declares this repo's real identity.

``SUITE.lock`` exists so the umbrella's cross-repo lock-agreement check
(``agent-suite scripts/check-lock-agreement.py --strict``) can verify
agent-wake's identity instead of reporting a missing lock. That check compares
our ``[component].version`` against the umbrella's ``[components.agent-wake]``
entry, which agent-suite derives from the ``agent-waked`` distribution — i.e.
``daemon/pyproject.toml``'s version.

So the file is only useful while it is *true*. A version bump in
daemon/pyproject.toml that forgets SUITE.lock turns a merely-absent lock into an
IDENTITY_MISMATCH failure in the umbrella — a worse state than not having the
file at all. This test is what keeps the two in step, in the repo that owns
both, since the umbrella has no way to fix our drift for us.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _suite_lock() -> dict:
    with (_REPO_ROOT / "SUITE.lock").open("rb") as f:
        return tomllib.load(f)


def _daemon_pyproject() -> dict:
    with (_REPO_ROOT / "daemon" / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


def test_suite_lock_exists_and_parses():
    lock = _suite_lock()
    assert "component" in lock, "SUITE.lock needs a [component] block"


def test_suite_lock_component_name_is_the_repo_name():
    assert _suite_lock()["component"]["name"] == "agent-wake"


def test_suite_lock_version_tracks_the_daemon_distribution():
    """The umbrella derives agent-wake's version from the agent-waked dist."""
    lock_version = _suite_lock()["component"]["version"]
    daemon_version = _daemon_pyproject()["project"]["version"]
    assert lock_version == daemon_version, (
        f"SUITE.lock [component].version is {lock_version!r} but "
        f"daemon/pyproject.toml is {daemon_version!r}. Bump both in the same "
        f"commit, or the umbrella lock-agreement check reports an identity "
        f"mismatch for this repo."
    )


def test_suite_lock_declares_no_spine():
    """agent-wake has no regista dependency, so it must not claim a spine pin.

    A ``[spine]`` block here would be compared against the umbrella's regista
    pin and would have to be maintained for a dependency we do not have.
    """
    assert "spine" not in _suite_lock()
