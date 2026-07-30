"""Allow ``python -m agent_waked.cli`` to invoke the operator CLI.

The top-level ``agent_waked/__main__.py`` runs the *daemon* server
(``agent_waked.main:main``), so the operator CLI needs its own module entry —
this mirrors the sibling components (acb's ``agent_capability_broker/__main__.py``
dispatches to ``cli.main`` the same way).

This entry point is also the invocation the CLI-contract conformance gate uses
(``sys.executable -m agent_waked.cli …``): invoking by module rather than by
guessing the console-script location (``Path(sys.executable).parent / "agent-wake"``)
means the gate runs the *installed* package and does not break when the
interpreter and the script live in different directories (e.g. ``/usr/bin/python3``
with the script in ``~/.local/bin``).
"""

from agent_waked.cli import main

raise SystemExit(main())
