/**
 * Daemon client: maintains a unix-socket connection to agent-waked,
 * receives wake frames, hands them to a wake-handler, and forwards
 * reply frames from the agent_wake_reply tool.
 *
 * Spec reference: v1-daemon-spec.md §4 (wire protocol) and §9.3
 * (slimmed claude adapter — this is the TypeScript port for opencode).
 *
 * Stdlib `net` is used (not Bun.connect) so the client is testable
 * under either runtime.
 */
import net from "node:net";
import path from "node:path";
import os from "node:os";
import { log } from "./log";

export const PROTOCOL_VERSION = 1;

export interface WakeEvent {
  v?: number;
  event_id?: string;
  source?: string;
  kind?: string;
  content?: string;
  meta?: Record<string, unknown>;
  wake?: boolean;
}

export type WakeHandler = (event: WakeEvent) => void | Promise<void>;

export interface ReplyResult {
  reply_id: string;
  status: "delivered" | "failed" | "no_callback";
  http_status?: number | null;
  error?: string | null;
}

export interface ClientOptions {
  socketPath: string;
  sources: string[];
  instance?: string;
  onWake: WakeHandler;
  /**
   * Called with hello_ack's accepted_sources on every (re)subscribe —
   * the daemon's authoritative list of sources routed to this adapter.
   * idle-notify uses it as a feedback-loop guard.
   */
  onAcceptedSources?: (sources: string[]) => void;
  /**
   * Called when the daemon connection closes. Between this and the next
   * hello_ack there is NO confirmed routing answer — idle-notify uses it
   * to invalidate its accepted_sources confirmation (fail closed).
   */
  onDisconnect?: () => void;
  /** Initial backoff in ms (defaults to 1000). Tests use a small value. */
  initialBackoffMs?: number;
  /** Maximum backoff in ms (defaults to 30000). */
  maxBackoffMs?: number;
}

export function defaultSocketPath(): string {
  const xdg = process.env.XDG_RUNTIME_DIR;
  if (xdg) return path.join(xdg, "agent-wake.sock");
  return path.join(os.homedir(), ".local", "state", "agent-wake", "agent-wake.sock");
}

/**
 * Promise-based bus for reply_result frames keyed by reply_id.
 * The tool handler awaits the promise; the client resolves it when
 * the matching reply_result arrives (or rejects on timeout).
 */
export class ReplyResultBus {
  private static pending = new Map<
    string,
    {
      resolve: (r: ReplyResult) => void;
      reject: (e: Error) => void;
      timer: ReturnType<typeof setTimeout>;
    }
  >();

  static register(replyId: string, timeoutMs = 35000): Promise<ReplyResult> {
    return new Promise<ReplyResult>((resolve, reject) => {
      const timer = setTimeout(() => {
        ReplyResultBus.pending.delete(replyId);
        reject(new Error("timed out"));
      }, timeoutMs);
      ReplyResultBus.pending.set(replyId, { resolve, reject, timer });
    });
  }

  static deliver(frame: ReplyResult): void {
    const entry = ReplyResultBus.pending.get(frame.reply_id);
    if (!entry) return;
    ReplyResultBus.pending.delete(frame.reply_id);
    clearTimeout(entry.timer);
    entry.resolve(frame);
  }

  /** Test-only. */
  static _clear(): void {
    for (const entry of ReplyResultBus.pending.values()) {
      clearTimeout(entry.timer);
    }
    ReplyResultBus.pending.clear();
  }
}

/**
 * Module-scoped connection state. There is at most one active socket
 * to the daemon. `sendReplyFrame` writes to whatever is current.
 */
let currentSocket: net.Socket | null = null;

export function sendReplyFrame(frame: object): void {
  if (!currentSocket || currentSocket.destroyed) {
    throw new Error("not connected to daemon");
  }
  currentSocket.write(encodeFrame(frame));
}

function encodeFrame(frame: object): string {
  return JSON.stringify(frame) + "\n";
}

/** One connect/handshake/dispatch session. Resolves when the socket closes. */
export function oneSession(opts: ClientOptions): Promise<void> {
  return new Promise<void>((resolve) => {
    const socket = net.createConnection({ path: opts.socketPath });
    currentSocket = socket;
    let buffer = "";
    let helloSent = false;

    const cleanup = () => {
      if (currentSocket === socket) currentSocket = null;
      try {
        opts.onDisconnect?.();
      } catch (e: any) {
        log.warn(`onDisconnect handler failed: ${e?.message ?? e}`);
      }
      resolve();
    };

    socket.on("connect", () => {
      const hello = {
        type: "hello",
        v: PROTOCOL_VERSION,
        adapter: "opencode",
        instance: opts.instance ?? `pid-${process.pid}`,
        filters: { sources: opts.sources },
      };
      socket.write(encodeFrame(hello));
      helloSent = true;
    });

    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf-8");
      let nl: number;
      while ((nl = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, nl);
        buffer = buffer.slice(nl + 1);
        if (!line) continue;
        let frame: any;
        try {
          frame = JSON.parse(line);
        } catch {
          log.warn("malformed frame from daemon, ignoring");
          continue;
        }
        handleFrame(socket, frame, opts).catch((e) =>
          log.warn(`frame handler threw: ${e?.message ?? e}`)
        );
      }
    });

    socket.on("error", (e) => {
      if (!helloSent) {
        log.warn(`daemon connect failed: ${e.message}`);
      } else {
        log.warn(`daemon socket error: ${e.message}`);
      }
    });

    socket.on("close", cleanup);
  });
}

async function handleFrame(
  socket: net.Socket,
  frame: any,
  opts: ClientOptions
): Promise<void> {
  const t = frame?.type;
  if (t === "hello_ack") {
    log.info(
      `subscribed, session_id=${frame.session_id} accepted_sources=${JSON.stringify(frame.accepted_sources)}`
    );
    if (opts.onAcceptedSources && Array.isArray(frame.accepted_sources)) {
      try {
        opts.onAcceptedSources(frame.accepted_sources.filter((s: unknown) => typeof s === "string"));
      } catch (e: any) {
        log.warn(`onAcceptedSources handler failed: ${e?.message ?? e}`);
      }
    }
  } else if (t === "wake") {
    const ackId = frame.ack_id;
    const event = frame.event ?? {};
    try {
      await opts.onWake(event);
      socket.write(encodeFrame({ type: "ack", ack_id: ackId }));
    } catch (e: any) {
      log.warn(`onWake failed: ${e?.message ?? e}`);
      socket.write(
        encodeFrame({ type: "nack", ack_id: ackId, reason: String(e?.message ?? e) })
      );
    }
  } else if (t === "reply_result") {
    ReplyResultBus.deliver(frame as ReplyResult);
  } else if (t === "ping") {
    socket.write(encodeFrame({ type: "pong" }));
  } else if (t === "error") {
    log.error(`daemon error: ${JSON.stringify(frame)}`);
    if (frame.fatal) socket.end();
  } else if (typeof t === "string") {
    log.warn(`unknown frame type from daemon: ${t}`);
  } else {
    log.warn("frame missing type, ignoring");
  }
}

/**
 * Long-lived connect-and-retry loop. Returns a stop function that
 * tears down the current connection and prevents reconnect.
 */
export function runClient(opts: ClientOptions): () => Promise<void> {
  let stopped = false;
  let activeSocket: net.Socket | null = null;
  const initial = opts.initialBackoffMs ?? 1000;
  const max = opts.maxBackoffMs ?? 30000;

  const loop = async () => {
    let backoff = initial;
    while (!stopped) {
      const start = Date.now();
      const wrapped: ClientOptions = {
        ...opts,
        onWake: opts.onWake,
      };
      const sessionPromise = oneSession(wrapped);
      activeSocket = currentSocket;
      try {
        await sessionPromise;
      } catch (e: any) {
        log.warn(`session ended with error: ${e?.message ?? e}`);
      }
      if (stopped) return;
      // If we stayed connected for a while, reset backoff.
      if (Date.now() - start > 5000) backoff = initial;
      log.warn(`daemon connection lost; reconnecting in ${backoff}ms`);
      await sleep(backoff);
      backoff = Math.min(backoff * 2, max);
    }
  };

  const runner = loop();

  return async () => {
    stopped = true;
    if (currentSocket) currentSocket.destroy();
    if (activeSocket && !activeSocket.destroyed) activeSocket.destroy();
    await runner;
  };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
