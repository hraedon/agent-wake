"""agent-waked entrypoint, signal handling, lifecycle.

Single asyncio event loop hosts the HTTP ingest server and the unix-socket
server.  ``SIGTERM`` / ``SIGINT`` trigger graceful shutdown with a 5-second
drain cap.  ``SIGHUP`` reloads configuration (sources / routing take effect
immediately; port changes require restart).
"""

import asyncio
import ipaddress
import json as _json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from aiohttp import web

from .config import ConfigError, load_config
from .delivery import HumanDelivery
from .ingest import (
    DELIVERY_DRAIN_TIMEOUT,
    FORCE_DRAIN_TIMEOUT,
    create_ingest_app,
    force_drain_delivery_tasks,
)
from .outbox import Outbox
from .router import Router
from .secrets.resolver import SecretResolver
from .secrets.visibility import source_secret_visibility
from .socket_server import SocketServer
from .store import StoreError, WakeStore, open_store

log = logging.getLogger("agent_waked")

# Shutdown budget. These nest, and they have to: _DRAIN_TIMEOUT bounds the whole
# of ``runner.cleanup()``, inside which aiohttp waits _HANDLER_GRACE for
# in-flight request handlers and then the ingest app's on_cleanup hook drains
# in-flight deliveries for DELIVERY_DRAIN_TIMEOUT. If the outer bound fired
# first, ``wait_for`` would cancel ``runner.cleanup()`` and abandon the very
# handlers and tasks the inner budgets exist to drain — which is what happened
# when the outer bound was 5s and aiohttp's shutdown_timeout was its 60s default.
_HANDLER_GRACE = 2.0
_DRAIN_TIMEOUT = _HANDLER_GRACE + DELIVERY_DRAIN_TIMEOUT + FORCE_DRAIN_TIMEOUT + 1.0

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


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _is_loopback(host: str) -> bool:
    """True if *host* binds only to the local machine.

    Covers the full 127.0.0.0/8 range (not just 127.0.0.1), IPv6 ::1, and the
    ``localhost`` name. Any other address is treated as network-reachable.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_listen(cfg: dict[str, Any]) -> tuple[str, int]:
    """Resolve the HTTP bind address from config + env overrides.

    Precedence: ``AGENT_WAKE_LISTEN_HOST`` / ``AGENT_WAKE_LISTEN_PORT`` env
    overrides (used by the Docker container and operators who want to change
    the bind without editing config.json) > ``cfg["listen"]`` > defaults.

    Raises ``ConfigError`` on a non-integer port so the caller can exit
    cleanly instead of crashing with a traceback.
    """
    listen = cfg.get("listen", {})
    host = os.environ.get("AGENT_WAKE_LISTEN_HOST") or listen.get("host", "127.0.0.1")
    port_env = os.environ.get("AGENT_WAKE_LISTEN_PORT")
    if port_env:
        try:
            port = int(port_env)
        except ValueError as e:
            raise ConfigError(
                f"AGENT_WAKE_LISTEN_PORT={port_env!r} is not a valid port number"
            ) from e
        if not (1 <= port <= 65535):
            raise ConfigError(
                f"AGENT_WAKE_LISTEN_PORT={port} is out of range (1-65535)"
            )
    else:
        port = int(listen.get("port", 8788))
    return host, port


def _warn_non_loopback_bind(host: str, cfg: dict[str, Any], explicit: bool = False) -> None:
    """Warn when the HTTP ingress binds a non-loopback address (BC-002).

    This is advisory, not a hard refusal: binding ``0.0.0.0`` is legitimate
    inside a container or behind a reverse proxy. The point is that it must be
    a deliberate operator decision, never an accidental default. ``explicit``
    indicates the bind came from an ``AGENT_WAKE_LISTEN_HOST`` env override,
    which downgrades an "open" failure to a warning (the operator opted in).
    """
    if _is_loopback(host):
        return
    sources = cfg.get("sources", {})
    all_authed = all(
        isinstance(s, dict) and s.get("secret_uris")
        for s in sources.values()
    )
    if not all_authed and not explicit:
        log.error(
            "SECURITY: HTTP ingress binding non-loopback host %r but one or "
            "more sources lack a configured secret. An open ingress is a "
            "prompt-injection vector; refusing to treat this as safe.",
            host,
        )
    elif not all_authed:
        log.warning(
            "HTTP ingress binding non-loopback host %r (via AGENT_WAKE_LISTEN_HOST) "
            "but one or more sources lack a configured secret. This is an open "
            "ingress — ensure the host-side port mapping is restricted.",
            host,
        )
    else:
        log.warning(
            "HTTP ingress binding non-loopback host %r with authenticated "
            "sources. Ensure this is intentional (container/reverse-proxy) "
            "and the port is reachable only by trusted callers.",
            host,
        )


def _require_resolvable_secrets(cfg: dict[str, Any]) -> bool:
    """True if every env-backed source secret in *cfg* is readable by this process.

    This is the daemon half of the split introduced for WI-003.  ``load_config``
    validates shape and no longer reads secret material, because "can I read
    this secret" is a property of the *asking process*, not of the config — a
    doctor run from the suite's root-owned alert-check cannot read the user's
    ``secrets.env`` and must not therefore call a healthy estate broken.

    The daemon is the component that signs and verifies wake events, so for the
    daemon the answer must be fatal: a source whose key it cannot read is a
    source it would silently fail to authenticate at request time.  Refusing up
    front turns that into one loud line at start.

    This is strictly *stronger* than the check it replaces.  The old eager
    resolution in ``load_config`` fired only for the legacy ``secret_env``
    spelling; a source written as ``"secret": "env://NAME"`` with ``NAME`` unset
    started the daemon happily and failed per request. All three spellings
    normalise to ``secret_uris``, which is what this inspects.

    ``vault://`` URIs are not probed — see ``secrets.visibility`` for why
    startup must not depend on Vault reachability.
    """
    vis = source_secret_visibility(cfg)
    if vis.all_visible:
        return True
    log.error(
        "SECRETS: %d of %d configured source(s) reference secrets that are not "
        "set in this process's environment: %s (missing env var(s): %s). The "
        "daemon signs and verifies wake events, so it cannot run for a source "
        "whose key it cannot read. Check the unit's EnvironmentFile "
        "(~/.config/agent-wake/secrets.env) and that the daemon runs as its owner.",
        len(vis.unresolved),
        vis.configured,
        ", ".join(vis.unresolved),
        ", ".join(vis.missing_env_vars),
    )
    return False


def _reload_config(
    cfg: dict[str, Any],
    router: Router,
    resolver: SecretResolver | None = None,
) -> None:
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

    # Same gate as startup: a reload that introduces a source whose secret this
    # process cannot read would leave the daemon advertising a source it can
    # never authenticate. Keep the previous config, which we know is signable.
    if not _require_resolvable_secrets(new_cfg):
        log.error(
            "config reload rejected: unreadable source secret(s); keeping the "
            "previous config"
        )
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

    log.info(
        "config reloaded: %d sources, routing=%s",
        len(cfg.get("sources", {})),
        bool(cfg.get("routing")),
    )
    # Refresh all cached secrets so vault-mode picks up rotations immediately.
    if resolver is not None:
        import asyncio as _asyncio
        try:
            loop = _asyncio.get_event_loop()
            task = loop.create_task(resolver.refresh_all())

            def _log_refresh_failure(fut: "_asyncio.Future[None]") -> None:
                try:
                    fut.result()
                except _asyncio.CancelledError:
                    log.debug("SIGHUP secret refresh cancelled (shutdown?)")
                except Exception:
                    log.exception("SIGHUP secret refresh failed")

            task.add_done_callback(_log_refresh_failure)
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

    # Refuse to run with a secret we cannot read, before binding anything: the
    # daemon is the authority on its own secrets precisely because it is the one
    # that signs with them.
    if not _require_resolvable_secrets(cfg):
        return 1

    try:
        host, port = resolve_listen(cfg)
    except ConfigError as e:
        log.error("listen error: %s", e)
        return 1
    sock_path = _resolve_socket_path(cfg)

    # BC-002 partial: the daemon's HTTP ingress is HMAC-authenticated but an
    # operator who binds to a non-loopback address exposes the port to the
    # network. Warn loudly (do not refuse — legitimate container/deploy cases
    # bind 0.0.0.0) so it's a deliberate choice, not an accident.
    # An explicit AGENT_WAKE_LISTEN_HOST env override counts as "deliberate"
    # and downgrades the warning to info.
    explicit_bind = bool(os.environ.get("AGENT_WAKE_LISTEN_HOST"))
    _warn_non_loopback_bind(host, cfg, explicit=explicit_bind)

    # Durable state (BC-WAKE-004/012): dedupe window, next-session queue and
    # dead-letter live in one SQLite file. Opening it is a few milliseconds and
    # runs the retention sweep once, so startup stays fast. A store that cannot
    # be opened degrades to the in-memory v0 behaviour rather than refusing to
    # start — signalling is more valuable than durability when the disk is
    # unhappy, and the degradation is logged loudly.
    store: WakeStore | None = None
    if (cfg.get("state") or {}).get("enabled", True):
        try:
            store = open_store(cfg)
            log.info(
                "durable store at %s (dedupe=%d pending=%d dead_letter=%d)",
                store.path,
                store.dedupe_count(),
                store.pending_count(),
                store.dead_letter_count(),
            )
        except StoreError:
            log.exception(
                "durable store unavailable; falling back to in-memory dedupe "
                "(replays are re-admitted after restart, next-session delivery off)"
            )
            store = None
    else:
        log.warning(
            "durable store disabled by config (state.enabled=false): dedupe is "
            "in-memory only and next-session delivery is unavailable"
        )

    router = Router(cfg, store=store)
    resolver = SecretResolver(vault_cfg=cfg.get("vault"))

    # store=store: a human-directed delivery that never lands is dead-lettered
    # rather than leaving only an in-memory health entry lost on restart.
    delivery = HumanDelivery(cfg, resolver, store=store)

    outbox = Outbox(cfg, store=store)
    await outbox.start()

    socket_server = SocketServer(sock_path, router, outbox=outbox)
    await socket_server.start()

    app = create_ingest_app(
        cfg,
        router,
        socket_server=socket_server,
        version=_VERSION,
        resolver=resolver,
        delivery=delivery,
        store=store,
    )
    runner = _make_runner(app)
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
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, AttributeError):
            # Windows ProactorEventLoop doesn't support add_signal_handler.
            # Fall back to signal.signal for SIGINT/SIGTERM.
            signal.signal(sig, lambda s, f: stop_event.set())
    try:
        loop.add_signal_handler(signal.SIGHUP, reload_event.set)
    except (NotImplementedError, AttributeError, OSError):
        # SIGHUP doesn't exist on Windows; no-op.
        pass

    while not stop_event.is_set():
        reload_event.clear()
        stop_task = asyncio.ensure_future(stop_event.wait())
        reload_task = asyncio.ensure_future(reload_event.wait())
        _done, pending = await asyncio.wait(
            [stop_task, reload_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for p in pending:
            p.cancel()
        if reload_event.is_set():
            _reload_config(cfg, router, resolver=resolver)

    log.info("shutting down")
    await _shutdown(
        socket_server=socket_server,
        runner=runner,
        router=router,
        outbox=outbox,
        delivery=delivery,
        store=store,
        app=app,
    )
    return 0


def _make_runner(app: web.Application) -> web.AppRunner:
    """Build the ingest runner with an explicit handler grace period.

    aiohttp's ``shutdown_timeout`` defaults to **60 seconds** — twelve times the
    old whole-shutdown budget. Left at the default, ``_shutdown``'s ``wait_for``
    fired first and cancelled ``runner.cleanup()`` partway through, abandoning
    in-flight handlers and any delivery task they had registered. Setting it
    here is what makes the nested budget in ``_DRAIN_TIMEOUT`` true.
    """
    return web.AppRunner(app, shutdown_timeout=_HANDLER_GRACE)


async def _shutdown(
    *,
    socket_server: SocketServer,
    runner: web.AppRunner,
    router: Router,
    outbox: Outbox,
    delivery: HumanDelivery,
    store: WakeStore | None,
    app: web.Application | None = None,
) -> None:
    """Tear the daemon down in the one order that keeps durability.

    This used to be inline and it used to be wrong: the store was closed
    *before* ``runner.cleanup()``, so an in-flight human delivery still sitting
    in 1s/4s/16s retry backoff either wrote its dead-letter into a closed
    connection (``sqlite3.ProgrammingError``) or was cancelled with nothing
    recorded at all — losing exactly the alerts the dead-letter table exists to
    preserve. Extracted so the ordering is a named, testable invariant rather
    than a property of statement order in a 100-line function.

    1. Stop accepting new work — unix socket, then the HTTP listener.
    2. Let ``runner.cleanup()`` finish in-flight request handlers and then drain
       in-flight deliveries (the ingest app's ``on_cleanup`` hook), so the
       cancellation arm of ``HumanDelivery.deliver`` writes its dead-letter
       while the store is still open.
    3. Force-drain as a backstop. Step 2 is bounded, and a bound can be hit: if
       ``wait_for`` cancels ``runner.cleanup()`` partway through, the on_cleanup
       hook never ran to completion and tasks can still be alive. Cancelling
       them *here*, before the store closes, is what makes "nothing can still
       want the store" true rather than merely likely.
    4. Cancel and await ack waiters while their durable-state callbacks can still
       use the store.
    5. Close the outbound HTTP sessions, once nothing is using them.
    6. Close the store LAST.
    """
    socket_server.close()
    try:
        await asyncio.wait_for(runner.cleanup(), timeout=_DRAIN_TIMEOUT)
    except TimeoutError:
        log.warning("runner cleanup timed out after %.0fs", _DRAIN_TIMEOUT)
    if app is not None:
        await force_drain_delivery_tasks(app)
    await router.shutdown()
    await outbox.close()
    await delivery.close()
    if store is not None:
        store.close()


def main() -> int:
    return asyncio.run(_run())
