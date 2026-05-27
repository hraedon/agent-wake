import { afterEach, describe, expect, test } from "bun:test";
import { deliverWake, formatWakeEvent, type OpencodeClientLike } from "../src/wake";
import { subscribe as labelSubscribe, _reset as resetLabels } from "../src/labels";

afterEach(() => resetLabels());

function makeClient(opts: {
  sessions?: any[];
  promptAsyncImpl?: (args: any) => Promise<unknown>;
}): OpencodeClientLike & { calls: any[] } {
  const calls: any[] = [];
  const client = {
    calls,
    session: {
      async list() {
        return { data: opts.sessions ?? [] };
      },
      async promptAsync(args: any) {
        calls.push(args);
        if (opts.promptAsyncImpl) return opts.promptAsyncImpl(args);
        return { ok: true };
      },
    },
  };
  return client;
}

describe("formatWakeEvent", () => {
  test("emits a <wake> tag with escaped attributes and content", () => {
    const text = formatWakeEvent({
      v: 0,
      source: 'git&hub',
      kind: 'web"hook',
      content: "<crash> & burn",
    });
    expect(text).toBe(
      '<wake source="git&amp;hub" kind="web&quot;hook">\n&lt;crash&gt; &amp; burn\n</wake>'
    );
  });

  test("handles missing fields as empty strings", () => {
    const text = formatWakeEvent({});
    expect(text).toBe('<wake source="" kind="">\n\n</wake>');
  });
});

describe("deliverWake", () => {
  test("broadcasts to all live sessions when no session_id meta", async () => {
    const client = makeClient({
      sessions: [{ id: "s1" }, { id: "s2" }, { id: "s3", deletedAt: "yesterday" }],
    });
    const result = await deliverWake(client, {
      v: 0,
      source: "demo",
      content: "ping",
    });
    expect(result.delivered).toBe(2);
    expect(result.sessions.sort()).toEqual(["s1", "s2"]);
    expect(client.calls.length).toBe(2);
    for (const call of client.calls) {
      expect(call.body.noReply).toBe(false);
      expect(call.body.parts[0].text).toContain("<wake");
      expect(call.body.parts[0].text).toContain("ping");
    }
  });

  test("targets a specific session when meta.session_id is set", async () => {
    const client = makeClient({
      sessions: [{ id: "s1" }, { id: "s2" }],
    });
    const result = await deliverWake(client, {
      v: 0,
      source: "demo",
      content: "x",
      meta: { session_id: "s2" },
    });
    expect(result.delivered).toBe(1);
    expect(result.sessions).toEqual(["s2"]);
    expect(client.calls.length).toBe(1);
    expect(client.calls[0].path.id).toBe("s2");
  });

  test("wake:false maps to noReply:true (silent inject)", async () => {
    const client = makeClient({ sessions: [{ id: "s1" }] });
    await deliverWake(client, { v: 0, source: "d", content: "c", wake: false });
    expect(client.calls[0].body.noReply).toBe(true);
  });

  test("wake undefined defaults to noReply:false (turn)", async () => {
    const client = makeClient({ sessions: [{ id: "s1" }] });
    await deliverWake(client, { v: 0, source: "d", content: "c" });
    expect(client.calls[0].body.noReply).toBe(false);
  });

  test("no live sessions returns delivered=0 without throwing", async () => {
    const client = makeClient({ sessions: [] });
    const result = await deliverWake(client, { v: 0, source: "d", content: "c" });
    expect(result.delivered).toBe(0);
    expect(result.sessions).toEqual([]);
  });

  test("uses promptAsync (not prompt) — the archived plugin bug", async () => {
    const client = makeClient({ sessions: [{ id: "s1" }] });
    // Smoking gun: confirm the SDK surface we call is promptAsync.
    const calls: string[] = [];
    const wrapped: OpencodeClientLike = {
      session: {
        list: client.session.list.bind(client.session),
        promptAsync: async (args: any) => {
          calls.push("promptAsync");
          return client.session.promptAsync(args);
        },
      } as any,
    };
    // If anyone refactors to use `prompt`, this test will break.
    expect((wrapped.session as any).prompt).toBeUndefined();
    await deliverWake(wrapped, { v: 0, source: "d", content: "c" });
    expect(calls).toEqual(["promptAsync"]);
  });

  test("promptAsync failure on one session does not block others", async () => {
    const client = makeClient({
      sessions: [{ id: "s1" }, { id: "s2" }],
      promptAsyncImpl: async (args) => {
        if (args.path.id === "s1") throw new Error("nope");
        return {};
      },
    });
    const result = await deliverWake(client, { v: 0, source: "d", content: "c" });
    expect(result.delivered).toBe(1);
    expect(result.sessions).toEqual(["s2"]);
  });

  test("session.list returning a bare array (not {data}) is tolerated", async () => {
    const client: OpencodeClientLike & { calls: any[] } = {
      calls: [],
      session: {
        async list() {
          return [{ id: "x1" }] as any;
        },
        async promptAsync(args: any) {
          (client as any).calls.push(args);
          return {};
        },
      },
    };
    const result = await deliverWake(client, { v: 0, source: "d", content: "c" });
    expect(result.delivered).toBe(1);
    expect(result.sessions).toEqual(["x1"]);
  });

  test("session.list throwing is treated as no live sessions", async () => {
    const client: OpencodeClientLike = {
      session: {
        async list() {
          throw new Error("server unreachable");
        },
        async promptAsync() {
          return {};
        },
      },
    };
    const result = await deliverWake(client, { v: 0, source: "d", content: "c" });
    expect(result.delivered).toBe(0);
  });
});

describe("deliverWake — label routing (meta.target)", () => {
  test("delivers only to sessions subscribed to the target label", async () => {
    const client = makeClient({
      sessions: [{ id: "s1" }, { id: "s2" }, { id: "s3" }],
    });
    labelSubscribe("s2", "oncall");
    const result = await deliverWake(client, {
      v: 0,
      source: "d",
      content: "page",
      meta: { target: "oncall" },
    });
    expect(result.delivered).toBe(1);
    expect(result.sessions).toEqual(["s2"]);
    // session.list should NOT have been the targeting source — the
    // calls all go to s2 directly.
    expect(client.calls.length).toBe(1);
    expect(client.calls[0].path.id).toBe("s2");
  });

  test("fans out when multiple sessions hold the same label", async () => {
    const client = makeClient({ sessions: [{ id: "s1" }, { id: "s2" }, { id: "s3" }] });
    labelSubscribe("s1", "team");
    labelSubscribe("s3", "team");
    const result = await deliverWake(client, {
      v: 0,
      source: "d",
      content: "all-hands",
      meta: { target: "team" },
    });
    expect(result.delivered).toBe(2);
    expect(result.sessions.sort()).toEqual(["s1", "s3"]);
  });

  test("drops the event when no session is subscribed to the target", async () => {
    const client = makeClient({ sessions: [{ id: "s1" }, { id: "s2" }] });
    // No subscribers.
    const result = await deliverWake(client, {
      v: 0,
      event_id: "evt-1",
      source: "d",
      content: "c",
      meta: { target: "nobody-listening" },
    });
    expect(result.delivered).toBe(0);
    expect(result.sessions).toEqual([]);
    expect(client.calls.length).toBe(0);
  });

  test("meta.target wins over meta.session_id when both are set", async () => {
    const client = makeClient({ sessions: [{ id: "s1" }, { id: "s2" }] });
    labelSubscribe("s2", "oncall");
    const result = await deliverWake(client, {
      v: 0,
      source: "d",
      content: "c",
      meta: { target: "oncall", session_id: "s1" },
    });
    expect(result.delivered).toBe(1);
    expect(result.sessions).toEqual(["s2"]);
  });

  test("empty meta.target falls through to legacy paths", async () => {
    const client = makeClient({ sessions: [{ id: "s1" }] });
    const result = await deliverWake(client, {
      v: 0,
      source: "d",
      content: "c",
      meta: { target: "" },
    });
    // Empty string is treated as "no target" → broadcast.
    expect(result.delivered).toBe(1);
    expect(result.sessions).toEqual(["s1"]);
  });

  test("untargeted events still reach unlabeled sessions (broadcast preserved)", async () => {
    const client = makeClient({ sessions: [{ id: "s1" }, { id: "s2" }] });
    labelSubscribe("s2", "oncall");
    // No target on the event — should broadcast to BOTH s1 and s2,
    // ignoring the label registry entirely.
    const result = await deliverWake(client, { v: 0, source: "d", content: "c" });
    expect(result.delivered).toBe(2);
    expect(result.sessions.sort()).toEqual(["s1", "s2"]);
  });
});
