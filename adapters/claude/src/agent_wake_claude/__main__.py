"""Entry point for agent-wake Claude adapter.

Loads config, starts the HTTP ingest listener in a background thread, then runs
the MCP stdio server in the foreground thread.
"""

import logging
import sys

from .config import ConfigError, load_config
from .ingest import start_listener
from .server import main as server_main
from .channel import emit_wake_event

logger = logging.getLogger("agent_wake_claude")


def main() -> None:
    try:
        config = load_config()
    except ConfigError as e:
        logger.error("config error: %s", e)
        sys.exit(1)

    start_listener(config, emit_wake_event)
    server_main()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    main()
