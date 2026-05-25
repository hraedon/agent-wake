"""Entry point for agent-wake Claude adapter.

Loads config, starts the HTTP ingest listener in a background thread, then runs
the MCP stdio server in the foreground thread. Handles SIGTERM/SIGINT for
graceful shutdown of the HTTP listener.
"""

import logging
import signal
import sys

from .config import ConfigError, load_config
from .ingest import start_listener
from .server import main as server_main
from .channel import emit_wake_event

logger = logging.getLogger("agent_wake_claude")

_ingest_server = None


def _shutdown(signum, frame):
    logger.info("received signal %s, shutting down", signum)
    if _ingest_server is not None:
        _ingest_server.shutdown()
    sys.exit(0)


def main() -> None:
    global _ingest_server

    try:
        config = load_config()
    except ConfigError as e:
        logger.error("config error: %s", e)
        sys.exit(1)

    _ingest_server = start_listener(config, emit_wake_event, return_server=True)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server_main()
    except KeyboardInterrupt:
        pass
    finally:
        if _ingest_server is not None:
            _ingest_server.shutdown()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    main()
