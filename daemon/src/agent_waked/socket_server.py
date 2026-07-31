"""Unix-domain socket server for daemon↔adapter communication.

Spec reference: v1-daemon-spec.md §4.  Binds a unix-domain stream
socket, accepts up to 16 concurrent connections, performs the
hello / hello_ack handshake, and dispatches inbound frames.
"""

import asyncio
import fcntl
import json
import logging
import os
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ulid import ULID

from . import addressing
from .proto import (
    MAX_FRAME_SIZE,
    BadFrameError,
    FrameTooLargeError,
    encode_frame,
    validate_frame,
)

if TYPE_CHECKING:
    from .outbox import Outbox
    from .router import Router

log = logging.getLogger("agent_waked.socket_server")

MAX_CONNECTIONS = 16
PING_INTERVAL = 30.0
PONG_TIMEOUT = 10.0

#: How many consecutive missed pongs to forgive while a delivery is in flight
#: (BC-011).  A wake event that starts a model turn can keep an adapter's event
#: loop busy for minutes; killing that connection because a ping went unanswered
#: is the liveness check deciding a *delivery* question. Bounded rather than
#: unconditional: a peer that died mid-delivery would otherwise never be reaped,
#: so tolerance is 4 intervals (~2 minutes) and then the connection goes.
MAX_BUSY_MISSED_PONGS = 4


class ClientConnection:
    """One subscribed adapter connection: the *connection* lifecycle only.

    The *delivery* lifecycle deliberately lives in the router (BC-011).  v1
    conflated them — the heartbeat closed a connection on a missed pong with no
    idea whether it had just handed that adapter a wake event to work on — and
    the fix is not a shared counter but an owner for each question: the socket
    server owns "is this peer alive", the router owns "does this peer owe me an
    ack", and the heartbeat asks the router before acting.
    """

    def __init__(
        self,
        session_id: str,
        adapter: str,
        instance: str,
        sources: list[str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        destinations: list[str] | None = None,
    ):
        self.session_id = session_id
        self.adapter = adapter
        self.instance = instance
        self.sources = sources
        self.destinations = list(
            destinations if destinations is not None else sources
        )
        self._reader = reader
        self._writer = writer
        self.pending_ping: bool = False
        #: Consecutive missed pongs forgiven because a delivery was outstanding.
        self.busy_missed_pongs: int = 0

    async def send_frame(self, frame: dict[str, Any]) -> None:
        self._writer.write(encode_frame(frame))
        await self._writer.drain()

    def close(self) -> None:
        try:
            self._writer.close()
        except Exception:
            pass


async def _read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    try:
        line = await reader.readline()
    except ValueError:
        raise FrameTooLargeError()
    if not line:
        raise ConnectionError("peer closed")
    if len(line) > MAX_FRAME_SIZE:
        raise FrameTooLargeError()
    try:
        return json.loads(line)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        raise BadFrameError()


class SocketServer:
    def __init__(
        self,
        socket_path: Path,
        router: "Router",
        outbox: "Outbox | None" = None,
    ):
        self._socket_path = socket_path
        self._lock_path = Path(str(socket_path) + ".lock")
        self._router = router
        self._outbox = outbox
        self._server: asyncio.Server | None = None
        self._lock_fd: int | None = None
        self._connections: dict[str, ClientConnection] = {}
        self._heartbeat_task: asyncio.Task[None] | None = None

    @property
    def connections(self) -> dict[str, ClientConnection]:
        return self._connections

    async def start(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        lock_fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(lock_fd)
            raise RuntimeError(
                f"another agent-waked instance holds {self._lock_path}; exiting"
            )
        self._lock_fd = lock_fd

        try:
            self._socket_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeError(
                f"cannot remove stale socket {self._socket_path}: {exc}"
            ) from exc

        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            str(self._socket_path),
            limit=MAX_FRAME_SIZE * 2,
        )
        os.chmod(self._socket_path, 0o600)
        log.info("unix socket bound at %s", self._socket_path)

        self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        """Send pings every PING_INTERVAL; close connections that don't pong.

        A missed pong on a connection with an **in-flight delivery** is forgiven
        up to ``MAX_BUSY_MISSED_PONGS`` times (BC-011).  A wake event whose whole
        purpose is to start a model turn can occupy a single-threaded adapter for
        far longer than the ping interval, and killing it there means the
        liveness probe reaps the healthy connection it just woke — the event is
        lost and the operator sees "heartbeat timeout" for an adapter that was
        doing exactly what it was asked.  Bounded, because unconditional
        forgiveness would leak a peer that died mid-delivery.
        """
        while True:
            await asyncio.sleep(PING_INTERVAL)
            _time.monotonic()
            stale: list[str] = []
            for session_id, conn in list(self._connections.items()):
                if conn.pending_ping:
                    in_flight = self._router.in_flight_for(session_id)
                    if in_flight and conn.busy_missed_pongs < MAX_BUSY_MISSED_PONGS:
                        conn.busy_missed_pongs += 1
                        log.info(
                            "heartbeat: session_id=%s missed a pong with %d "
                            "delivery(ies) in flight; tolerating (%d/%d)",
                            session_id,
                            in_flight,
                            conn.busy_missed_pongs,
                            MAX_BUSY_MISSED_PONGS,
                        )
                        continue
                    # No pong received since last ping — dead connection
                    log.warning(
                        "heartbeat timeout session_id=%s in_flight=%d, closing",
                        session_id,
                        in_flight,
                    )
                    stale.append(session_id)
                    continue
                try:
                    await conn.send_frame({"type": "ping"})
                    conn.pending_ping = True
                except Exception:
                    log.warning(
                        "heartbeat ping failed session_id=%s, closing",
                        session_id,
                    )
                    stale.append(session_id)
            for session_id in stale:
                # Close the writer to trigger _handle_connection's finally block,
                # which owns the _connections pop and router unsubscribe.
                if session_id in self._connections:
                    self._connections[session_id].close()

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        session_id = str(ULID())
        log.info("new connection pending, session_id=%s", session_id)
        conn: ClientConnection | None = None

        try:
            conn = await self._handshake(session_id, reader, writer)
        except BadFrameError:
            await self._send_error(writer, "bad_frame", "malformed JSON", fatal=True)
            writer.close()
            return
        except FrameTooLargeError:
            await self._send_error(writer, "frame_too_large", "line exceeded 1 MiB", fatal=True)
            writer.close()
            return
        except Exception as exc:
            log.warning("handshake failed for session_id=%s: %s", session_id, exc)
            writer.close()
            return

        assert conn is not None
        # Next-session delivery (Plan 006 Phase 1): the handshake is the
        # "session start" signal, so anything queued for this subscriber's
        # sources is handed over before the frame loop begins. Failures here
        # must not kill an otherwise healthy connection — the rows stay queued.
        try:
            await self._router.drain_pending(session_id)
        except Exception:
            log.exception("drain_pending failed session_id=%s", session_id)

        try:
            await self._frame_loop(conn)
        except ConnectionError:
            pass
        except Exception as exc:
            log.warning("connection error session_id=%s: %s", session_id, exc)
        finally:
            self._router.unsubscribe(session_id)
            self._connections.pop(session_id, None)
            conn.close()
            log.info("connection closed, session_id=%s", session_id)

    async def _handshake(
        self,
        session_id: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> ClientConnection:
        frame = await _read_frame(reader)
        ftype = frame.get("type")

        if ftype != "hello":
            await self._send_error(
                writer, "unauthenticated", "first frame must be hello", fatal=True
            )
            raise ConnectionError("expected hello")

        err = validate_frame(frame)
        if err:
            await self._send_error(writer, err, f"invalid hello: {err}", fatal=True)
            raise ConnectionError(f"hello validation: {err}")

        if len(self._connections) >= MAX_CONNECTIONS:
            await self._send_error(
                writer, "connection_limit", f"max {MAX_CONNECTIONS} connections", fatal=True
            )
            raise ConnectionError("connection limit reached")

        sources = frame.get("filters", {}).get("sources", []) or []
        claimed = frame.get("destinations")
        adapter = frame["adapter"]
        instance = frame["instance"]

        destinations = self._router.destinations_for_hello(adapter, sources, claimed)

        # Per-destination connection cap (BC-WAKE-010). The global
        # MAX_CONNECTIONS above is a process-resource bound; this is a
        # *per-addressee* bound, which is the thing the v1 model could not
        # express — one chatty adapter filling all 16 slots starved every other
        # destination and there was no object to hang a smaller number on.
        over = self._destinations_over_cap(destinations)
        if over:
            detail = ", ".join(f"{name} (max {cap})" for name, cap in over)
            await self._send_error(
                writer,
                "connection_limit",
                f"destination connection cap reached: {detail}",
                fatal=True,
            )
            raise ConnectionError(f"destination cap reached: {detail}")

        conn = ClientConnection(
            session_id=session_id,
            adapter=adapter,
            instance=instance,
            sources=sources,
            reader=reader,
            writer=writer,
            destinations=destinations,
        )
        self._connections[session_id] = conn
        self._router.subscribe(
            session_id, adapter, instance, sources, conn, destinations=destinations
        )

        accepted = self._router.accepted_sources_for(adapter, sources)

        ack = {
            "type": "hello_ack",
            "v": 1,
            "session_id": session_id,
            "accepted_sources": accepted,
            "accepted_destinations": destinations,
        }
        await conn.send_frame(ack)
        log.info(
            "adapter subscribed session_id=%s adapter=%s instance=%s "
            "accepted_sources=%s accepted_destinations=%s",
            session_id,
            adapter,
            instance,
            accepted,
            destinations,
        )
        return conn

    def _destinations_over_cap(
        self, destinations: list[str]
    ) -> list[tuple[str, int]]:
        """Destinations whose ``max_connections`` this connection would exceed.

        Checked before the connection is registered, so the count it compares
        against is the set of *already-admitted* subscribers.
        """
        table = addressing.destination_table(self._router.config)
        over: list[tuple[str, int]] = []
        for name in destinations:
            dest = table.get(name)
            if dest is None or dest.max_connections is None:
                continue
            if self._router.destination_load(name) >= dest.max_connections:
                over.append((name, dest.max_connections))
        return over

    async def _frame_loop(self, conn: ClientConnection) -> None:
        while True:
            try:
                frame = await _read_frame(conn._reader)
            except FrameTooLargeError:
                await self._send_error(
                    conn._writer,
                    "frame_too_large",
                    "line exceeded 1 MiB",
                    fatal=True,
                )
                raise
            except BadFrameError:
                await self._send_error(
                    conn._writer, "bad_frame", "malformed JSON", fatal=True
                )
                raise

            ftype = frame.get("type")
            err = validate_frame(frame)
            if err:
                await self._send_error(
                    conn._writer, err, f"invalid {ftype}: {err}", fatal=True
                )
                raise ConnectionError(f"invalid frame: {err}")

            if ftype == "reply":
                await self._handle_reply(conn, frame)
            elif ftype in ("ack", "nack"):
                log.info(
                    "received %s from session_id=%s ack_id=%s",
                    ftype,
                    conn.session_id,
                    frame.get("ack_id"),
                )
                self._router.resolve_ack(frame.get("ack_id", ""), ftype)
            elif ftype == "pong":
                conn.pending_ping = False
                conn.busy_missed_pongs = 0
                log.debug("pong from session_id=%s", conn.session_id)
            else:
                log.warning(
                    "unexpected frame type %s from session_id=%s",
                    ftype,
                    conn.session_id,
                )

    async def _handle_reply(
        self, conn: ClientConnection, frame: dict[str, Any]
    ) -> None:
        if self._outbox is None:
            log.error("reply received but no outbox configured")
            return
        try:
            source = frame["source"]
            reply_id = frame["reply_id"]
            in_reply_to = frame["in_reply_to"]
            content = frame["content"]
        except KeyError as exc:
            await self._send_error(
                conn._writer, "bad_frame", f"reply missing field {exc}", fatal=False
            )
            return
        try:
            result = await self._outbox.deliver(
                source=source,
                reply_id=reply_id,
                in_reply_to=in_reply_to,
                content=content,
            )
        except Exception as exc:
            log.warning("reply delivery failed: %s", exc)
            result = {
                "reply_id": reply_id,
                "status": "failed",
                "http_status": None,
                "error": str(exc),
            }
        result_frame = {"type": "reply_result"}
        result_frame.update(result)
        await conn.send_frame(result_frame)

    async def _send_error(
        self,
        writer: asyncio.StreamWriter,
        code: str,
        message: str,
        *,
        fatal: bool = True,
    ) -> None:
        frame = {"type": "error", "code": code, "message": message, "fatal": fatal}
        try:
            writer.write(encode_frame(frame))
            await writer.drain()
        except Exception:
            pass

    def close(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        if self._server is not None:
            self._server.close()
        for conn in list(self._connections.values()):
            conn.close()
        self._connections.clear()
        # Do NOT release the flock here — hold it until process exit so
        # a second instance cannot acquire the lock during the drain window.
        # The OS releases the lock when the file descriptor is closed on exit.
        if self._socket_path.exists():
            try:
                self._socket_path.unlink()
            except Exception:
                pass
