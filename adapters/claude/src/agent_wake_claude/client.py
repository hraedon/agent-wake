"""Daemon client: maintains a unix-socket connection to agent-waked,
receives wake frames, dispatches to channel.emit_wake_event.

Spec reference: v1-daemon-spec.md §9.3.
"""

import asyncio
import json
import logging
import os
import threading
from pathlib import Path

from .channel import emit_wake_event

log = logging.getLogger("agent_wake_claude.client")

PROTOCOL_VERSION = 1


def _socket_path(config: dict) -> Path:
    explicit = config.get("socket_path")
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "agent-wake.sock"
    return Path.home() / ".local" / "state" / "agent-wake" / "agent-wake.sock"


class ReplyResultBus:
    """Thread-safe bus for delivering reply_result frames to the
    synchronous reply tool handler running in the MCP thread."""

    _pending: dict[str, list] = {}
    _lock = threading.Lock()

    @classmethod
    def register(cls, reply_id: str) -> None:
        evt = threading.Event()
        with cls._lock:
            cls._pending[reply_id] = [evt, None]

    @classmethod
    def deliver(cls, frame: dict) -> None:
        reply_id = frame.get("reply_id")
        with cls._lock:
            entry = cls._pending.pop(reply_id, None)
        if entry is not None:
            entry[1] = frame
            entry[0].set()

    @classmethod
    def wait_result(cls, reply_id: str, timeout: float = 35.0) -> dict | None:
        with cls._lock:
            entry = cls._pending.get(reply_id)
        if entry is None:
            return None
        if entry[0].wait(timeout):
            return entry[1]
        with cls._lock:
            cls._pending.pop(reply_id, None)
        return None


_state_lock = threading.Lock()
_current_writer: asyncio.StreamWriter | None = None
_event_loop: asyncio.AbstractEventLoop | None = None


def send_reply_frame(frame: dict) -> None:
    """Send a reply frame from the synchronous MCP handler thread."""
    with _state_lock:
        writer = _current_writer
        loop = _event_loop
    if writer is None or loop is None:
        raise RuntimeError("not connected to daemon")
    future = asyncio.run_coroutine_threadsafe(_async_send(writer, frame), loop)
    future.result(timeout=5)


async def _async_send(writer: asyncio.StreamWriter, frame: dict) -> None:
    writer.write((json.dumps(frame) + "\n").encode())
    await writer.drain()


async def run_client(config: dict) -> None:
    sources = list(config.get("sources", {}).keys())
    sock_path = _socket_path(config)
    backoff = 1.0
    while True:
        try:
            await _one_session(sock_path, sources)
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(
                "daemon connection lost: %s; reconnecting in %.1fs", e, backoff
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def _one_session(sock_path: Path, sources: list[str]) -> None:
    global _current_writer, _event_loop

    reader, writer = await asyncio.open_unix_connection(str(sock_path))

    with _state_lock:
        _current_writer = writer
        _event_loop = asyncio.get_running_loop()

    try:
        hello = {
            "type": "hello",
            "v": PROTOCOL_VERSION,
            "adapter": "claude",
            "instance": f"pid-{os.getpid()}",
            "filters": {"sources": sources},
        }
        writer.write((json.dumps(hello) + "\n").encode())
        await writer.drain()

        while True:
            line = await reader.readline()
            if not line:
                return
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                log.warning("malformed frame from daemon, ignoring")
                continue

            t = frame.get("type")
            if t == "hello_ack":
                log.info(
                    "subscribed, session_id=%s accepted_sources=%s",
                    frame.get("session_id"),
                    frame.get("accepted_sources"),
                )
            elif t == "wake":
                await _handle_wake(writer, frame)
            elif t == "reply_result":
                ReplyResultBus.deliver(frame)
            elif t == "error":
                log.error("daemon error: %s", frame)
                if frame.get("fatal"):
                    return
            else:
                log.warning("unknown frame type from daemon: %s", t)
    finally:
        with _state_lock:
            if _current_writer is writer:
                _current_writer = None


async def _handle_wake(
    writer: asyncio.StreamWriter, frame: dict
) -> None:
    ack_id = frame.get("ack_id")
    event = frame.get("event", {})
    try:
        emit_wake_event(event)
        ack = {"type": "ack", "ack_id": ack_id}
    except Exception as e:
        log.warning("emit failed: %s", e)
        ack = {"type": "nack", "ack_id": ack_id, "reason": str(e)}
    writer.write((json.dumps(ack) + "\n").encode())
    await writer.drain()
