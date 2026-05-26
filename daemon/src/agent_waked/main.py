"""agent-waked entrypoint, signal handling, lifecycle.

Single asyncio event loop hosts the HTTP ingest server and the unix-socket
server.  ``SIGTERM`` / ``SIGINT`` trigger graceful shutdown with a 5-second
drain cap.
"""

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from aiohttp import web

from .config import ConfigError, load_config
from .ingest import create_ingest_app
from .router import Router
from .socket_server import SocketServer

log = logging.getLogger("agent_waked")

_DRAIN_TIMEOUT = 5


def _resolve_socket_path(cfg: dict) -> Path:
    explicit = cfg.get("socket_path")
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "agent-wake.sock"
    return Path.home() / ".local" / "state" / "agent-wake" / "agent-wake.sock"


async def _run() -> int:
    log_level = os.environ.get("AGENT_WAKE_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    try:
        cfg = load_config()
    except ConfigError as e:
        log.error("config error: %s", e)
        return 1

    listen = cfg.get("listen", {})
    host = listen.get("host", "127.0.0.1")
    port = listen.get("port", 8788)
    sock_path = _resolve_socket_path(cfg)

    router = Router(cfg)

    app = create_ingest_app(cfg, router)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("HTTP ingest listening on %s:%s", host, port)

    socket_server = SocketServer(sock_path, router)
    await socket_server.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()

    log.info("shutting down")
    socket_server.close()
    await asyncio.wait_for(runner.cleanup(), timeout=_DRAIN_TIMEOUT)
    return 0


def main() -> int:
    return asyncio.run(_run())
