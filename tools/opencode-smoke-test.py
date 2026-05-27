#!/usr/bin/env python3
"""End-to-end smoke test for the opencode adapter.

Spins up:
  1. agent-waked (with a temp config / socket / port)
  2. opencode serve (with a temp project config registering our plugin)

Then:
  3. Creates a session via opencode's HTTP API.
  4. Signs and POSTs a wake event to the daemon ingest port.
  5. Polls the session's message list, asserting the <wake> tag arrived.

Uses `wake: false` (silent inject — `noReply: true` on the opencode side)
so the test does not depend on a working LLM provider. The wake message
is added to the session as a user message regardless.

Run:
    python3 tools/opencode-smoke-test.py

Exits 0 on PASS, non-zero on FAIL. Prints subprocess logs on failure.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADAPTER_DIST = ROOT / "adapters" / "opencode" / "dist" / "index.js"
DAEMON_VENV = ROOT / "daemon" / ".venv" / "bin" / "agent-waked"


def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, *, timeout: float, interval: float = 0.1) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


def http_get(url: str, *, timeout: float = 5) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def http_post(url: str, body: bytes, headers: dict[str, str], *, timeout: float = 5) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def find_wake_text_in_messages(messages_json: list, needle: str) -> bool:
    """Walk the opencode session.messages payload looking for the needle."""
    for msg in messages_json:
        parts = msg.get("parts") if isinstance(msg, dict) else None
        if not isinstance(parts, list):
            # Older shape: {info, parts} nesting
            info_parts = msg.get("parts") if isinstance(msg, dict) else None
            if isinstance(info_parts, list):
                parts = info_parts
        if parts is None:
            continue
        for part in parts:
            text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str) and needle in text:
                return True
    return False


def main() -> int:
    if not ADAPTER_DIST.exists():
        print(f"FAIL: adapter bundle missing at {ADAPTER_DIST}", file=sys.stderr)
        print("Run `cd adapters/opencode && bun install && bun run build` first.", file=sys.stderr)
        return 2

    if not DAEMON_VENV.exists():
        print(f"FAIL: daemon entrypoint missing at {DAEMON_VENV}", file=sys.stderr)
        print("Run `cd daemon && python3 -m venv .venv && .venv/bin/pip install -e .` first.", file=sys.stderr)
        return 2

    opencode_bin = shutil.which("opencode")
    if not opencode_bin:
        print("FAIL: `opencode` not found on PATH", file=sys.stderr)
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="aw-smoke-"))
    daemon_proc: subprocess.Popen | None = None
    opencode_proc: subprocess.Popen | None = None
    daemon_log = workdir / "daemon.log"
    opencode_log = workdir / "opencode.log"

    try:
        ingest_port = pick_free_port()
        opencode_port = pick_free_port()
        socket_path = workdir / "agent-wake.sock"
        secret_hex = secrets.token_hex(32)

        # ── agent-wake daemon config ─────────────────────────────────
        daemon_cfg = {
            "version": 1,
            "listen": {"host": "127.0.0.1", "port": ingest_port},
            "socket_path": str(socket_path),
            "sources": {
                "smoke": {"secret_env": "AGENT_WAKE_SMOKE_SECRET"},
            },
            "routing": {"smoke": {"adapter": "opencode"}},
        }
        daemon_cfg_path = workdir / "agent-wake.json"
        daemon_cfg_path.write_text(json.dumps(daemon_cfg))

        # ── opencode project config ──────────────────────────────────
        # NOTE: opencode merges this with the user's global config, so
        # provider/MCP setup is inherited. We only need to add our plugin.
        opencode_cfg = {
            "$schema": "https://opencode.ai/config.json",
            "plugin": [str(ADAPTER_DIST)],
        }
        (workdir / "opencode.json").write_text(json.dumps(opencode_cfg))

        env = os.environ.copy()
        env["AGENT_WAKE_SMOKE_SECRET"] = secret_hex
        env["AGENT_WAKE_CONFIG"] = str(daemon_cfg_path)

        # ── start daemon ─────────────────────────────────────────────
        with open(daemon_log, "wb") as f:
            daemon_proc = subprocess.Popen(
                [str(DAEMON_VENV)],
                env=env,
                stdout=f,
                stderr=subprocess.STDOUT,
            )

        if not wait_for(lambda: socket_path.exists(), timeout=5):
            raise RuntimeError("daemon did not create unix socket in 5s")

        # ── start opencode serve ─────────────────────────────────────
        with open(opencode_log, "wb") as f:
            opencode_proc = subprocess.Popen(
                [opencode_bin, "serve", "--port", str(opencode_port), "--hostname", "127.0.0.1"],
                cwd=workdir,
                env=env,
                stdout=f,
                stderr=subprocess.STDOUT,
            )

        base_url = f"http://127.0.0.1:{opencode_port}"

        def opencode_ready() -> bool:
            try:
                status, _ = http_get(f"{base_url}/session", timeout=1)
                return status == 200
            except (urllib.error.URLError, ConnectionError, OSError):
                return False

        if not wait_for(opencode_ready, timeout=20):
            raise RuntimeError(f"opencode serve did not come up on {opencode_port} in 20s")

        # Give the plugin a beat to dial the daemon and complete the
        # hello handshake before we send a wake event.
        time.sleep(3.0)

        # ── create an opencode session ───────────────────────────────
        status, body = http_post(
            f"{base_url}/session",
            body=b"{}",
            headers={"Content-Type": "application/json"},
        )
        if status != 200:
            raise RuntimeError(f"POST /session returned {status}: {body!r}")
        session = json.loads(body)
        session_id = session.get("id")
        if not session_id:
            raise RuntimeError(f"no session id in response: {session!r}")
        print(f"  created session {session_id}")

        # ── send the wake event ──────────────────────────────────────
        event = {
            "v": 0,
            "event_id": f"smoke-{secrets.token_hex(4)}",
            "source": "smoke",
            "kind": "alert",
            "content": "AGENT-WAKE-SMOKE-NEEDLE",
            "meta": {"session_id": session_id},
            "wake": True,  # let opencode add the user message immediately
        }
        event_body = json.dumps(event).encode("utf-8")
        sig = "sha256=" + hmac.new(
            secret_hex.encode(), event_body, hashlib.sha256
        ).hexdigest()

        status, body = http_post(
            f"http://127.0.0.1:{ingest_port}/",
            body=event_body,
            headers={
                "Content-Type": "application/json",
                "X-AgentWake-Source": "smoke",
                "X-AgentWake-Signature": sig,
            },
        )
        if status != 202:
            raise RuntimeError(f"daemon ingest returned {status}: {body!r}")
        ingest_resp = json.loads(body)
        if ingest_resp.get("status") not in {"queued", "duplicate"}:
            raise RuntimeError(f"unexpected ingest status: {ingest_resp!r}")
        print(f"  daemon accepted wake: {ingest_resp}")

        # ── poll session messages for the needle ─────────────────────
        needle = "AGENT-WAKE-SMOKE-NEEDLE"
        deadline = time.time() + 15

        def needle_present() -> bool:
            status, body = http_get(f"{base_url}/session/{session_id}/message", timeout=3)
            if status != 200:
                return False
            try:
                messages = json.loads(body)
            except json.JSONDecodeError:
                return False
            return find_wake_text_in_messages(messages, needle)

        if not wait_for(needle_present, timeout=15):
            # Direct probe: bypass the plugin and POST the same shape
            # the plugin uses, to confirm the HTTP path itself works.
            probe_body = json.dumps(
                {"noReply": False, "parts": [{"type": "text", "text": f"DIRECT-PROBE-{needle}"}]}
            ).encode()
            ps, pb = http_post(
                f"{base_url}/session/{session_id}/prompt_async",
                body=probe_body,
                headers={"Content-Type": "application/json"},
            )
            print(f"  direct prompt_async probe: status={ps} body={pb[:200]!r}", file=sys.stderr)
            time.sleep(1)
            status, body = http_get(f"{base_url}/session/{session_id}/message", timeout=3)
            print(f"  session messages after probe: status={status} body={body[:1500]!r}", file=sys.stderr)
            raise RuntimeError("wake content never appeared in session messages")

        print("PASS: wake event delivered through daemon and into opencode session")

        # ── tier-1 check: meta.target with no subscriber drops cleanly ──
        # No agent has called agent_wake_subscribe, so no session holds
        # any label. An event with `meta.target: <anything>` should be
        # accepted by the daemon (status: queued) but the plugin should
        # log "no session subscribed" and NOT deliver to any session.
        before_msgs_status, before_msgs_body = http_get(
            f"{base_url}/session/{session_id}/message", timeout=3
        )
        before_count = (
            len(json.loads(before_msgs_body))
            if before_msgs_status == 200
            else 0
        )

        target_event = {
            "v": 0,
            "event_id": f"smoke-target-{secrets.token_hex(4)}",
            "source": "smoke",
            "kind": "alert",
            "content": "TARGETED-NEEDLE-SHOULD-NOT-APPEAR",
            "meta": {"target": "nobody-listening"},
            "wake": True,
        }
        tb = json.dumps(target_event).encode("utf-8")
        tsig = "sha256=" + hmac.new(
            secret_hex.encode(), tb, hashlib.sha256
        ).hexdigest()
        status, body = http_post(
            f"http://127.0.0.1:{ingest_port}/",
            body=tb,
            headers={
                "Content-Type": "application/json",
                "X-AgentWake-Source": "smoke",
                "X-AgentWake-Signature": tsig,
            },
        )
        if status != 202:
            raise RuntimeError(
                f"daemon ingest of targeted event returned {status}: {body!r}"
            )
        target_resp = json.loads(body)
        if target_resp.get("status") not in {"queued", "duplicate"}:
            raise RuntimeError(
                f"unexpected ingest status for targeted event: {target_resp!r}"
            )

        # Give the plugin a moment to process and (correctly) drop it.
        time.sleep(2)
        _, after_msgs_body = http_get(
            f"{base_url}/session/{session_id}/message", timeout=3
        )
        after_msgs = json.loads(after_msgs_body)

        # Sanity: the targeted-needle text MUST NOT appear in any
        # message. (The original AGENT-WAKE-SMOKE-NEEDLE may still be
        # there from earlier; we only check the new needle.)
        if find_wake_text_in_messages(after_msgs, "TARGETED-NEEDLE-SHOULD-NOT-APPEAR"):
            raise RuntimeError(
                "targeted event with no subscriber was delivered anyway"
            )
        print(
            "PASS: meta.target with no subscriber dropped cleanly "
            f"(daemon=queued, plugin filtered)"
        )

        return 0

    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        if daemon_log.exists():
            print("---- daemon log ----", file=sys.stderr)
            print(daemon_log.read_text(errors="replace"), file=sys.stderr)
        if opencode_log.exists():
            print("---- opencode log ----", file=sys.stderr)
            print(opencode_log.read_text(errors="replace")[-4000:], file=sys.stderr)
        return 1

    finally:
        for proc in (opencode_proc, daemon_proc):
            if proc is None:
                continue
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
