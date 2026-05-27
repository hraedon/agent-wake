import { afterEach, describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import net from "node:net";
import { oneSession, ReplyResultBus } from "../src/client";
import { executeReply } from "../src/reply";

function tmpSocketPath(): { dir: string; path: string } {
  const dir = mkdtempSync(join(tmpdir(), "aw-oc-reply-"));
  return { dir, path: join(dir, "test.sock") };
}

function startDaemon(
  socketPath: string,
  handler: (socket: net.Socket, received: any[]) => void
) {
  const connections: net.Socket[] = [];
  const received: any[][] = [];
  const server = net.createServer((socket) => {
    const idx = received.push([]) - 1;
    connections.push(socket);
    let buf = "";
    socket.on("data", (chunk) => {
      buf += chunk.toString("utf-8");
      let nl: number;
      while ((nl = buf.indexOf("\n")) !== -1) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (line) {
          try {
            received[idx].push(JSON.parse(line));
          } catch {
            /* ignore */
          }
        }
      }
    });
    handler(socket, received[idx]);
  });
  server.listen(socketPath);
  return {
    server,
    connections,
    received,
    close: () =>
      new Promise<void>((resolve) => {
        for (const c of connections) c.destroy();
        server.close(() => resolve());
      }),
  };
}

function writeFrame(socket: net.Socket, frame: object): void {
  socket.write(JSON.stringify(frame) + "\n");
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

afterEach(() => {
  ReplyResultBus._clear();
});

describe("executeReply", () => {
  test("rejects missing required fields", async () => {
    const r1 = await executeReply({ source: "", content: "x" });
    expect(r1).toMatch(/missing required fields/);
    const r2 = await executeReply({ source: "x", content: "" });
    expect(r2).toMatch(/missing required fields/);
  });

  test("not connected to daemon returns error", async () => {
    const result = await executeReply({ source: "demo", content: "hi" });
    expect(result).toMatch(/reply delivery failed/);
    expect(result).toMatch(/not connected/);
  });

  test("delivered status returns 'sent'", async () => {
    const { dir, path: sockPath } = tmpSocketPath();
    const daemon = startDaemon(sockPath, (socket, received) => {
      // Mirror reply frames as reply_result=delivered.
      const check = setInterval(() => {
        for (const frame of received) {
          if (frame.type === "reply" && !frame.__handled) {
            frame.__handled = true;
            writeFrame(socket, {
              type: "reply_result",
              reply_id: frame.reply_id,
              status: "delivered",
              http_status: 200,
            });
          }
        }
      }, 10);
      socket.on("close", () => clearInterval(check));
      setTimeout(() => {
        writeFrame(socket, {
          type: "hello_ack",
          v: 1,
          session_id: "s1",
          accepted_sources: ["demo"],
        });
      }, 20);
    });

    const session = oneSession({
      socketPath: sockPath,
      sources: ["demo"],
      onWake: async () => {},
    });

    await sleep(80);
    const result = await executeReply({ source: "demo", content: "hello" });
    expect(result).toBe("sent");

    for (const c of daemon.connections) c.destroy();
    await session;
    await daemon.close();
    rmSync(dir, { recursive: true, force: true });
  });

  test("no_callback status returns the no-callback message", async () => {
    const { dir, path: sockPath } = tmpSocketPath();
    const daemon = startDaemon(sockPath, (socket, received) => {
      const check = setInterval(() => {
        for (const frame of received) {
          if (frame.type === "reply" && !frame.__handled) {
            frame.__handled = true;
            writeFrame(socket, {
              type: "reply_result",
              reply_id: frame.reply_id,
              status: "no_callback",
            });
          }
        }
      }, 10);
      socket.on("close", () => clearInterval(check));
      setTimeout(() => {
        writeFrame(socket, {
          type: "hello_ack",
          v: 1,
          session_id: "s1",
          accepted_sources: ["demo"],
        });
      }, 20);
    });

    const session = oneSession({
      socketPath: sockPath,
      sources: ["demo"],
      onWake: async () => {},
    });

    await sleep(80);
    const result = await executeReply({ source: "demo", content: "hi" });
    expect(result).toBe("sent (no callback_url configured)");

    for (const c of daemon.connections) c.destroy();
    await session;
    await daemon.close();
    rmSync(dir, { recursive: true, force: true });
  });

  test("failed status returns the error", async () => {
    const { dir, path: sockPath } = tmpSocketPath();
    const daemon = startDaemon(sockPath, (socket, received) => {
      const check = setInterval(() => {
        for (const frame of received) {
          if (frame.type === "reply" && !frame.__handled) {
            frame.__handled = true;
            writeFrame(socket, {
              type: "reply_result",
              reply_id: frame.reply_id,
              status: "failed",
              error: "callback returned 500",
            });
          }
        }
      }, 10);
      socket.on("close", () => clearInterval(check));
      setTimeout(() => {
        writeFrame(socket, {
          type: "hello_ack",
          v: 1,
          session_id: "s1",
          accepted_sources: ["demo"],
        });
      }, 20);
    });

    const session = oneSession({
      socketPath: sockPath,
      sources: ["demo"],
      onWake: async () => {},
    });

    await sleep(80);
    const result = await executeReply({ source: "demo", content: "hi" });
    expect(result).toMatch(/reply delivery failed/);
    expect(result).toMatch(/callback returned 500/);

    for (const c of daemon.connections) c.destroy();
    await session;
    await daemon.close();
    rmSync(dir, { recursive: true, force: true });
  });
});
