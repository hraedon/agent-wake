#!/usr/bin/env python3
"""End-to-end fakechat demo for agent-wake-claude adapter.

Launches the adapter in a subprocess, mocks stdin/stdout to complete MCP
initialization, then POSTs a wake event to the HTTP listener and checks that
a channel notification was emitted.

This effectively dry-runs the full adapter (HTTP ingest -> HMAC -> wrap ->
channel notification emit) without requiring a live Claude Code session.
It is the closest thing to acceptance-criterion §9 item 8 that can run in
CI / on a dev box without the harness.

To drive against a real Claude Code session instead (manual hand-test):

  1. Generate a secret:
       python tools/generate-secret.py
     export it as $AGENT_WAKE_DEMO_SECRET.
  2. Write a config to ~/.config/agent-wake/config.json with a `demo`
     source whose secret_env is AGENT_WAKE_DEMO_SECRET (see
     adapters/claude/.env.example and adapters/claude/README.md).
  3. Launch Claude Code with the adapter loaded:
       claude --dangerously-load-development-channels server:agent-wake-claude
  4. Send a wake event with the curl one-liner in adapters/claude/README.md
     and confirm the <channel ...> tag appears in the agent's context.

Run mode: this script always launches its own adapter subprocess; it does
not talk to a running adapter. Run as `python tools/fakechat-test.py` and
expect `PASS: fakechat-test end-to-end` on success."""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import hmac
import hashlib
import select

HERE = os.path.dirname(__file__)
PROJECT_ROOT = os.path.dirname(HERE)


def _find_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> None:
    import socket
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except (ConnectionRefusedError, OSError):
            time.sleep(0.05)
    raise RuntimeError(f"Port {host}:{port} not ready after {timeout}s")


def main():
    secret = os.urandom(32).hex()
    port = _find_free_port()

    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, "config.json")
        with open(config_path, "w") as f:
            json.dump({
                "version": 0,
                "listen": {"host": "127.0.0.1", "port": port},
                "sources": {
                    "demo": {"secret_env": "DEMO_SECRET", "callback_url": None},
                },
                "default_callback_url": None,
            }, f)

        env = os.environ.copy()
        env["AGENT_WAKE_CONFIG"] = config_path
        env["DEMO_SECRET"] = secret
        venv_py_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
        venv_site = os.path.join(PROJECT_ROOT, ".venv", "lib", venv_py_ver, "site-packages")
        env["PYTHONPATH"] = os.path.join(PROJECT_ROOT, "adapters", "claude", "src")
        if os.path.isdir(venv_site):
            env["PYTHONPATH"] = env["PYTHONPATH"] + os.pathsep + venv_site

        adapter_root = os.path.join(PROJECT_ROOT, "adapters", "claude")
        proc = subprocess.Popen(
            [sys.executable, "-m", "agent_wake_claude"],
            cwd=adapter_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        try:
            # === MCP initialize handshake ===
            init = json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            }) + "\n"
            proc.stdin.write(init.encode())
            proc.stdin.flush()

            resp = proc.stdout.readline().decode().strip()
            if not resp:
                err = proc.stderr.read(4096).decode()
                print(f"FAIL: No initialize response. stderr: {err}", file=sys.stderr)
                sys.exit(1)
            msg = json.loads(resp)
            assert msg.get("result", {}).get("serverInfo", {}).get("name") == "agent-wake"
            assert "claude/channel" in msg["result"]["capabilities"]["experimental"]

            # notifications/initialized
            proc.stdin.write(
                (json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode()
            )
            proc.stdin.flush()

            # Wait for HTTP listener to become ready (retry with backoff)
            _wait_for_port("127.0.0.1", port, timeout=5)

            # === POST wake event ===
            body = json.dumps({
                "v": 0,
                "event_id": "01HZXPDEMO0000000000000001",
                "source": "demo",
                "kind": "alert",
                "content": "hello from fakechat-test",
                "meta": {},
                "wake": True,
            })

            sig = "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/",
                data=body.encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-AgentWake-Source": "demo",
                    "X-AgentWake-Signature": sig,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
                assert data["status"] == "queued", f"Unexpected status: {data}"

            # Wait briefly for the notification to appear on stdout
            time.sleep(0.3)

            # Drain stdout for channel notification
            lines = []
            while True:
                ready, _, _ = select.select([proc.stdout], [], [], 0.5)
                if not ready:
                    break
                line = proc.stdout.readline().decode().strip()
                if not line:
                    break
                lines.append(line)

            channel_lines = [l for l in lines if "notifications/claude/channel" in l]
            if not channel_lines:
                print(f"FAIL: No channel notification emitted. Captured stdout lines: {lines}", file=sys.stderr)
                sys.exit(1)

            payload = json.loads(channel_lines[0])
            params = payload["params"]
            assert params["content"] == "hello from fakechat-test"
            assert params["meta"]["source"] == "demo"
            assert params["meta"]["kind"] == "alert"

            print("PASS: fakechat-test end-to-end")

        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    main()
