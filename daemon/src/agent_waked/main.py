"""agent-waked entrypoint, signal handling, lifecycle.

Single asyncio event loop hosts the HTTP ingest server and the unix-socket
server.  ``SIGTERM`` / ``SIGINT`` trigger graceful shutdown with a 5-second
drain cap.  ``SIGHUP`` reloads configuration (sources / routing take effect
immediately; port changes require restart).
"""

import asyncio
import json as _json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from aiohttp import web

from .config import ConfigError, load_config
from .ingest import create_ingest_app
from .outbox import Outbox
from .router import Router
from .secrets.resolver import SecretResolver
from .socket_server import SocketServer

log = logging.getLogger("agent_waked")

_DRAIN_TIMEOUT = 5

try:
    from importlib.metadata import version as _pkg_version
    _VERSION = _pkg_version("agent-waked")
except Exception:
    _VERSION = "0.1.0"


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1] is not None:
            entry["exc"] = self.formatException(record.exc_info)
        return _json.dumps(entry, separators=(",", ":"))


def _resolve_socket_path(cfg: dict[str, Any]) -> Path:
    explicit = cfg.get("socket_path")
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "agent-wake.sock"
    return Path.home() / ".local" / "state" / "agent-wake" / "agent-wake.sock"


def _reload_config(cfg: dict[str, Any], router: Router, resolver: SecretResolver | None = None) -> None:
    """SIGHUP handler: reload config, update shared state in-place.

    Per spec §6.3:
    - New ports require restart (log and ignore).
    - socket_path changes require restart (log and ignore).
    - New sources/routing take effect immediately.
    - Existing subscribers stay connected; accepted_sources is recomputed
      on next hello_ack only.
    """
    try:
        new_cfg = load_config()
    except ConfigError as e:
        log.error("config reload failed: %s", e)
        return

    if not new_cfg.get("sources"):
        log.error("config reload rejected: no sources in new config")
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

    old_socket_path = cfg.get("socket_path")
    new_socket_path = new_cfg.get("socket_path")
    if old_socket_path != new_socket_path:
        log.warning(
            "socket_path change (%s -> %s) requires restart; keeping %s",
            old_socket_path,
            new_socket_path,
            old_socket_path,
        )
        new_cfg["socket_path"] = old_socket_path

    # Atomic swap: update in-place, then remove keys no longer present.
    # This avoids the window where cfg is empty between clear() and update().
    cfg.update(new_cfg)
    for key in list(cfg.keys()):
        if key not in new_cfg:
            del cfg[key]

    log.info("config reloaded: %d sources, routing=%s", len(cfg.get("sources", {})), bool(cfg.get("routing")))
    # Refresh all cached secrets so vault-mode picks up rotations immediately.
    if resolver is not None:
        import asyncio as _asyncio
        try:
            loop = _asyncio.get_event_loop()
            loop.create_task(resolver.refresh_all())
        except RuntimeError:
            pass  # No running loop (e.g. unit tests calling _reload_config directly)


async def _run() -> int:
    log_level = os.environ.get("AGENT_WAKE_LOG_LEVEL", "INFO").upper()
    log_format = os.environ.get("AGENT_WAKE_LOG_FORMAT", "text")

    if log_format == "json":
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_JsonFormatter(datefmt="%Y-%m-%dT%H:%M:%S%z"))
        logging.basicConfig(
            level=getattr(logging, log_level, logging.INFO),
            handlers=[handler],
        )
    else:
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
    resolver = SecretResolver(vault_cfg=cfg.get("vault"))

    outbox = Outbox(cfg)
    await outbox.start()

    socket_server = SocketServer(sock_path, router, outbox=outbox)
    await socket_server.start()

    app = create_ingest_app(
        cfg,
        router,
        socket_server=socket_server,
        version=_VERSION,
        resolver=resolver,
    )
    runner = web.AppRunner(app)
    try:
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
    except Exception:
        await runner.cleanup()
        raise
    log.info("HTTP ingest listening on %s:%s", host, port)

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
            _reload_config(cfg, router, resolver=resolver)

    log.info("shutting down")
    socket_server.close()
    await outbox.close()
    try:
        await asyncio.wait_for(runner.cleanup(), timeout=_DRAIN_TIMEOUT)
    except asyncio.TimeoutError:
        log.warning("runner cleanup timed out after %ds", _DRAIN_TIMEOUT)
    return 0


def main() -> int:
    return asyncio.run(_run())
