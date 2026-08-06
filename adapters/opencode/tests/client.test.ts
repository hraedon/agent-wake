import { afterEach, describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import net from "node:net";
import {
  oneSession,
  runClient,
  ReplyResultBus,
  sendReplyFrame,
  type WakeEvent,
} from "../src/client";

interface MockDaemon {
  server: net.Server;
  socketPath: string;
  connections: net.Socket[];
  received: any[][];
  close: () => Promise<void>;
}

function tmpSocketPath(): { dir: string; path: string } {
  const dir = mkdtempSync(join(tmpdir(), "aw-oc-sock-"));
  return { dir, path: join(dir, "test.sock") };
}

function startMockDaemon(
  socketPath: string,
  onConnect: (socket: net.Socket, received: any[]) => void
): MockDaemon {
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
            received[idx].push({ __raw: line });
          }
        }
      }
    });
    onConnect(socket, received[idx]);
  });

  server.listen(socketPath);

  return {
    server,
    socketPath,
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

describe("oneSession", () => {
  test("hello is the first frame sent", async () => {
    const { dir, path: sockPath } = tmpSocketPath();
    let helloFrame: any = null;
    const daemon = startMockDaemon(sockPath, (socket, received) => {
      // Close the connection after we see the first frame.
      const check = setInterval(() => {
        if (received.length > 0) {
          helloFrame = received[0];
          clearInterval(check);
          socket.end();
        }
      }, 10);
    });

    await oneSession({
      socketPath: sockPath,
      sources: ["test"],
      instance: "test-pid",
      onWake: async () => {},
    });

    await daemon.close();
    rmSync(dir, { recursive: true, force: true });

    expect(helloFrame).not.toBeNull();
    expect(helloFrame.type).toBe("hello");
    expect(helloFrame.v).toBe(1);
    expect(helloFrame.adapter).toBe("opencode");
    expect(helloFrame.instance).toBe("test-pid");
    expect(helloFrame.filters.sources).toEqual(["test"]);
  });

  test("wake frame triggers onWake and then sends ack", async () => {
    const { dir, path: sockPath } = tmpSocketPath();
    const got: WakeEvent[] = [];
    const daemon = startMockDaemon(sockPath, (socket) => {
      // Wait for hello, then send hello_ack + wake.
      setTimeout(() => {
        writeFrame(socket, {
          type: "hello_ack",
          v: 1,
          session_id: "s1",
          accepted_sources: ["test"],
        });
        writeFrame(socket, {
          type: "wake",
          ack_id: "a1",
          event: { v: 0, source: "test", content: "hello", wake: true },
        });
        // Give the client a moment to ack, then close.
        setTimeout(() => socket.end(), 200);
      }, 30);
    });

    await oneSession({
      socketPath: sockPath,
      sources: ["test"],
      onWake: async (e) => {
        got.push(e);
      },
    });

    await daemon.close();
    rmSync(dir, { recursive: true, force: true });

    expect(got.length).toBe(1);
    expect(got[0].content).toBe("hello");
    // The ack frame should appear after hello.
    const frames = daemon.received[0];
    const ack = frames.find((f) => f.type === "ack");
    expect(ack).toBeDefined();
    expect(ack.ack_id).toBe("a1");
  });

  test("ping frame sends pong", async () => {
    const { dir, path: sockPath } = tmpSocketPath();
    const daemon = startMockDaemon(sockPath, (socket) => {
      setTimeout(() => {
        writeFrame(socket, { type: "hello_ack", v: 1, session_id: "s1", accepted_sources: ["t"] });
        writeFrame(socket, { type: "ping" });
        setTimeout(() => socket.end(), 200);
      }, 30);
    });

    await oneSession({
      socketPath: sockPath,
      sources: ["t"],
      onWake: async () => {},
    });

    await daemon.close();
    rmSync(dir, { recursive: true, force: true });

    const frames = daemon.received[0];
    const pong = frames.find((f) => f.type === "pong");
    expect(pong).toBeDefined();
  });

  test("wake handler error sends nack", async () => {
    const { dir, path: sockPath } = tmpSocketPath();
    const daemon = startMockDaemon(sockPath, (socket) => {
      setTimeout(() => {
        writeFrame(socket, { type: "hello_ack", v: 1, session_id: "s1", accepted_sources: ["t"] });
        writeFrame(socket, {
          type: "wake",
          ack_id: "boom",
          event: { v: 0, source: "t", content: "x" },
        });
        setTimeout(() => socket.end(), 200);
      }, 30);
    });

    await oneSession({
      socketPath: sockPath,
      sources: ["t"],
      onWake: async () => {
        throw new Error("channel broken");
      },
    });

    await daemon.close();
    rmSync(dir, { recursive: true, force: true });

    const frames = daemon.received[0];
    const nack = frames.find((f) => f.type === "nack");
    expect(nack).toBeDefined();
    expect(nack.ack_id).toBe("boom");
    expect(nack.reason).toMatch(/channel broken/);
  });

  test("malformed frame is ignored without crashing", async () => {
    const { dir, path: sockPath } = tmpSocketPath();
    const got: WakeEvent[] = [];
    const daemon = startMockDaemon(sockPath, (socket) => {
      setTimeout(() => {
        writeFrame(socket, { type: "hello_ack", v: 1, session_id: "s1", accepted_sources: ["t"] });
        socket.write("not-json\n");
        writeFrame(socket, {
          type: "wake",
          ack_id: "a1",
          event: { v: 0, source: "t", content: "after" },
        });
        setTimeout(() => socket.end(), 200);
      }, 30);
    });

    await oneSession({
      socketPath: sockPath,
      sources: ["t"],
      onWake: async (e) => {
        got.push(e);
      },
    });

    await daemon.close();
    rmSync(dir, { recursive: true, force: true });

    expect(got.length).toBe(1);
    expect(got[0].content).toBe("after");
  });

  test("reply_result frame resolves a pending bus entry", async () => {
    const { dir, path: sockPath } = tmpSocketPath();
    const daemon = startMockDaemon(sockPath, (socket) => {
      setTimeout(() => {
        writeFrame(socket, { type: "hello_ack", v: 1, session_id: "s1", accepted_sources: ["t"] });
        writeFrame(socket, {
          type: "reply_result",
          reply_id: "r1",
          status: "delivered",
          http_status: 200,
        });
        setTimeout(() => socket.end(), 200);
      }, 30);
    });

    const pending = ReplyResultBus.register("r1", 3000);
    await oneSession({
      socketPath: sockPath,
      sources: ["t"],
      onWake: async () => {},
    });
    const result = await pending;

    await daemon.close();
    rmSync(dir, { recursive: true, force: true });

    expect(result.status).toBe("delivered");
    expect(result.http_status).toBe(200);
  });

  test("sendReplyFrame writes onto the live socket", async () => {
    const { dir, path: sockPath } = tmpSocketPath();
    const daemon = startMockDaemon(sockPath, (socket) => {
      setTimeout(() => {
        writeFrame(socket, { type: "hello_ack", v: 1, session_id: "s1", accepted_sources: ["t"] });
      }, 30);
    });

    const session = oneSession({
      socketPath: sockPath,
      sources: ["t"],
      onWake: async () => {},
    });

    // Wait for handshake.
    await sleep(120);
    sendReplyFrame({ type: "reply", reply_id: "r9", source: "t", in_reply_to: "", content: "hi" });
    await sleep(80);

    // Close the connection from the daemon side to end the session.
    for (const c of daemon.connections) c.destroy();
    await session;
    await daemon.close();
    rmSync(dir, { recursive: true, force: true });

    const reply = daemon.received[0].find((f) => f.type === "reply");
    expect(reply).toBeDefined();
    expect(reply.reply_id).toBe("r9");
    expect(reply.content).toBe("hi");
  });

  test("sendReplyFrame throws when not connected", () => {
    expect(() =>
      sendReplyFrame({ type: "reply", reply_id: "x", source: "s", in_reply_to: "", content: "c" })
    ).toThrow(/not connected/);
  });
});

describe("oneSession accepted-sources wiring", () => {
  test("onAcceptedSources fires on hello_ack and onDisconnect fires on close, in order", async () => {
    const { path: sockPath } = tmpSocketPath();
    const events: string[] = [];
    const daemon = startMockDaemon(sockPath, (socket, received) => {
      const check = setInterval(() => {
        if (received.length > 0) {
          clearInterval(check);
          writeFrame(socket, {
            type: "hello_ack",
            session_id: "sess-1",
            accepted_sources: ["demo-opencode"],
          });
          // Give the client a beat to process the ack, then drop the
          // connection — the disconnect callback must follow the ack.
          setTimeout(() => socket.end(), 30);
        }
      }, 10);
    });

    await oneSession({
      socketPath: sockPath,
      sources: ["demo-opencode"],
      onWake: async () => {},
      onAcceptedSources: (sources) => {
        events.push(`ack:${sources.join(",")}`);
      },
      onDisconnect: () => {
        events.push("disconnect");
      },
    });

    expect(events).toEqual(["ack:demo-opencode", "disconnect"]);
    await daemon.close();
  });

  test("onDisconnect fires even when the daemon never acks", async () => {
    const { path: sockPath } = tmpSocketPath();
    const events: string[] = [];
    const daemon = startMockDaemon(sockPath, (socket, received) => {
      const check = setInterval(() => {
        if (received.length > 0) {
          clearInterval(check);
          socket.end(); // close without hello_ack
        }
      }, 10);
    });

    await oneSession({
      socketPath: sockPath,
      sources: ["demo-opencode"],
      onWake: async () => {},
      onAcceptedSources: (sources) => {
        events.push(`ack:${sources.join(",")}`);
      },
      onDisconnect: () => {
        events.push("disconnect");
      },
    });

    expect(events).toEqual(["disconnect"]);
    await daemon.close();
  });
});

describe("runClient", () => {
  test("reconnects after the daemon drops the connection", async () => {
    const { dir, path: sockPath } = tmpSocketPath();
    let connectCount = 0;
    const daemon = startMockDaemon(sockPath, (socket) => {
      connectCount += 1;
      setTimeout(() => {
        writeFrame(socket, {
          type: "hello_ack",
          v: 1,
          session_id: `s${connectCount}`,
          accepted_sources: ["t"],
        });
        setTimeout(() => socket.destroy(), 80);
      }, 20);
    });

    const stop = runClient({
      socketPath: sockPath,
      sources: ["t"],
      onWake: async () => {},
      initialBackoffMs: 50,
      maxBackoffMs: 100,
    });

    await sleep(800);
    await stop();
    await daemon.close();
    rmSync(dir, { recursive: true, force: true });

    expect(connectCount).toBeGreaterThanOrEqual(2);
  });
});

describe("ReplyResultBus", () => {
  test("deliver resolves a registered promise", async () => {
    const pending = ReplyResultBus.register("rA", 2000);
    setTimeout(() => {
      ReplyResultBus.deliver({ reply_id: "rA", status: "delivered" });
    }, 30);
    const result = await pending;
    expect(result.status).toBe("delivered");
  });

  test("times out when no result arrives", async () => {
    const pending = ReplyResultBus.register("rB", 50);
    await expect(pending).rejects.toThrow(/timed out/);
  });

  test("deliver for an unknown reply_id is a no-op", () => {
    expect(() =>
      ReplyResultBus.deliver({ reply_id: "ghost", status: "delivered" })
    ).not.toThrow();
  });
});
