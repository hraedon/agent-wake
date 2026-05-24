import { describe, expect, test, beforeAll, afterAll } from "bun:test";
import { verifySignature } from "../src/gating";
import { loadConfig, type Config, type SourceConfig } from "../src/config";
import { startIngest } from "../src/ingest";

function hmacSign(secret: string, body: Uint8Array): string {
  const crypto = require("crypto");
  return "sha256=" + crypto.createHmac("sha256", secret).update(body).digest("hex");
}

function makeConfig(port: number): Config {
  return {
    version: 0,
    host: "127.0.0.1",
    port,
    sources: {
      demo: {
        secret: new TextEncoder().encode("shhh"),
        callback_url: null,
      },
    },
    default_callback_url: null,
  };
}

describe("HTTP ingest endpoint", () => {
  const receivedEvents: any[] = [];
  let server: any;
  const testPort = 18789;

  beforeAll(() => {
    const ctx = {
      client: {
        session: {
          prompt: async (opts: any) => {
            receivedEvents.push(opts);
          },
        },
      },
    };
    const activeSessions = new Set<string>(["session-1"]);
    server = startIngest(ctx, makeConfig(testPort), activeSessions);
  });

  afterAll(() => {
    if (server && server.stop) {
      server.stop();
    }
  });

  test("accepts a valid wake event and delivers to session", async () => {
    const body = JSON.stringify({
      v: 0,
      event_id: "evt-int-001",
      source: "demo",
      kind: "alert",
      content: "integration test",
      meta: {},
      wake: true,
    });
    const bodyBytes = new TextEncoder().encode(body);
    const sig = hmacSign("shhh", bodyBytes);

    const res = await fetch(`http://127.0.0.1:${testPort}/`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-AgentWake-Source": "demo",
        "X-AgentWake-Signature": sig,
      },
      body,
    });

    expect(res.status).toBe(202);
    const data = await res.json();
    expect(data.status).toBe("queued");
    expect(data.event_id).toBe("evt-int-001");

    await new Promise((r) => setTimeout(r, 200));
    expect(receivedEvents.length).toBeGreaterThan(0);
    const call = receivedEvents[receivedEvents.length - 1];
    expect(call.path.id).toBe("session-1");
    expect(call.body.noReply).toBe(false);
    expect(call.body.parts[0].type).toBe("text");
    expect(call.body.parts[0].text).toContain("integration test");
  });

  test("rejects unknown source with 403", async () => {
    const body = JSON.stringify({ v: 0, event_id: "evt-403", source: "unknown", kind: "alert", content: "x", wake: true });
    const sig = hmacSign("shhh", new TextEncoder().encode(body));

    const res = await fetch(`http://127.0.0.1:${testPort}/`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-AgentWake-Source": "unknown",
        "X-AgentWake-Signature": sig,
      },
      body,
    });

    expect(res.status).toBe(403);
  });

  test("rejects invalid signature with 403", async () => {
    const body = JSON.stringify({ v: 0, event_id: "evt-badsig", source: "demo", kind: "alert", content: "x", wake: true });

    const res = await fetch(`http://127.0.0.1:${testPort}/`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-AgentWake-Source": "demo",
        "X-AgentWake-Signature": "sha256=deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
      },
      body,
    });

    expect(res.status).toBe(403);
  });

  test("deduplicates event_id within window", async () => {
    const body = JSON.stringify({
      v: 0,
      event_id: "evt-dedup-int",
      source: "demo",
      kind: "alert",
      content: "dedup test",
      wake: true,
      meta: {},
    });
    const sig = hmacSign("shhh", new TextEncoder().encode(body));

    const res1 = await fetch(`http://127.0.0.1:${testPort}/`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-AgentWake-Source": "demo",
        "X-AgentWake-Signature": sig,
      },
      body,
    });
    expect(res1.status).toBe(202);
    const data1 = await res1.json();
    expect(data1.status).toBe("queued");

    const res2 = await fetch(`http://127.0.0.1:${testPort}/`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-AgentWake-Source": "demo",
        "X-AgentWake-Signature": sig,
      },
      body,
    });
    expect(res2.status).toBe(202);
    const data2 = await res2.json();
    expect(data2.status).toBe("duplicate");
  });

  test("wraps arbitrary JSON as webhook event", async () => {
    const raw = JSON.stringify({ action: "opened", number: 42 });
    const sig = hmacSign("shhh", new TextEncoder().encode(raw));

    const res = await fetch(`http://127.0.0.1:${testPort}/`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-AgentWake-Source": "demo",
        "X-AgentWake-Signature": sig,
        "X-AgentWake-Event-Id": "evt-wrapped-int",
      },
      body: raw,
    });

    expect(res.status).toBe(202);
    const data = await res.json();
    expect(data.event_id).toBe("evt-wrapped-int");
  });

  test("returns 404 for unknown paths", async () => {
    const res = await fetch(`http://127.0.0.1:${testPort}/unknown`);
    expect(res.status).toBe(404);
  });

  test("returns 405 for GET", async () => {
    const res = await fetch(`http://127.0.0.1:${testPort}/`);
    expect(res.status).toBe(405);
  });
});