"""agent-waked entrypoint, signal handling, lifecycle.

Single asyncio event loop hosts the HTTP ingest server and the unix-socket
server.  ``SIGTERM`` / ``SIGINT`` trigger graceful shutdown with a 5-second
drain cap.  ``SIGHUP`` reloads configuration (sources / routing take effect
immediately; port changes require restart).
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
from .outbox import Outbox
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


def _reload_config(cfg: dict, router: Router, outbox: Outbox) -> None:
    """SIGHUP handler: reload config, update shared state in-place.

    Per spec §6.3:
    - New ports require restart (log and ignore).
    - New sources/routing take effect immediately.
    - Existing subscribers stay connected; accepted_sources is recomputed
      on next hello_ack only.
    """
    try:
        new_cfg = load_config()
    except ConfigError as e:
        log.error("config reload failed: %s", e)
        return

    old_listen = cfg.get("listen", {})
    new_listen = new_cfg.get("listen", {})
    if old_listen != new_listen:
        log.warning(
            "listen address change (%s -> %s) requires restart; keeping %s",
            old_listen,
            new_listen,
            old_listen,
        )
        new_cfg["listen"] = dict(old_listen)

    cfg.clear()
    cfg.update(new_cfg)

    log.info("config reloaded: %d sources, routing=%s", len(cfg.get("sources", {})), bool(cfg.get("routing")))


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

    outbox = Outbox(cfg)
    await outbox.start()

    app = create_ingest_app(cfg, router)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("HTTP ingest listening on %s:%s", host, port)

    socket_server = SocketServer(sock_path, router, outbox=outbox)
    await socket_server.start()

    stop_event = asyncio.Event()
    reload_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)
    loop.add_signal_handler(signal.SIGHUP, reload_event.set)

    while not stop_event.is_set():
        reload_event.clear()
        stop_task = asyncio.ensure_future(stop_event.wait())
        reload_task = asyncio.ensure_future(reload_event.wait())
        done, pending = await asyncio.wait(
            [stop_task, reload_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for p in pending:
            p.cancel()
        if reload_event.is_set():
            _reload_config(cfg, router, outbox)

    log.info("shutting down")
    socket_server.close()
    await outbox.close()
    await asyncio.wait_for(runner.cleanup(), timeout=_DRAIN_TIMEOUT)
    return 0


def main() -> int:
    return asyncio.run(_run())
