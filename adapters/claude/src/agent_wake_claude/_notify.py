"""Low-level JSON-RPC stdout writer used by all modules that need to send
notifications back to the MCP client (Claude Code)."""

import json
import sys
import threading

_stdout_lock = threading.RLock()


def send(msg: dict) -> None:
    data = json.dumps(msg)
    with _stdout_lock:
        sys.stdout.write(data + "\n")
        sys.stdout.flush()
