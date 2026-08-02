"""Entry point for agent-wake Claude adapter.

Spec reference: v1-daemon-spec.md §9.5.

Runs the daemon client (asyncio) in the main thread and the MCP
stdio server in a background thread.  Exits when stdin closes
(Claude Code shut down) or on SIGTERM / SIGINT.
"""

import asyncio
import logging
import signal
import sys
import threading

from .client import run_client
from .config import ConfigError, load_config, reload_config
from .server import main as server_main

logger = logging.getLogger("agent_wake_claude")


def main() -> None:
    try:
        config = load_config()
    except ConfigError as e:
        logger.error("config error: %s", e)
        sys.exit(1)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    client_task = loop.create_task(run_client(config))

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, client_task.cancel)
    try:
        loop.add_signal_handler(signal.SIGHUP, _reload_config)
    except (AttributeError, NotImplementedError, OSError):
        pass

    mcp_thread = threading.Thread(target=server_main, daemon=True)
    mcp_thread.start()

    try:
        loop.run_until_complete(client_task)
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()


def _reload_config() -> None:
    try:
        reload_config()
    except ConfigError as e:
        logger.error("config reload failed: %s", e)
    else:
        logger.info("config reloaded")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    main()
