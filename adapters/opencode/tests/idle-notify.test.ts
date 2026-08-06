import { afterEach, describe, expect, test } from "bun:test";
import { chmodSync, mkdtempSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { createHmac } from "node:crypto";
import {
  parseIdleNotifyConfig,
  resolveSecret,
  shouldNotify,
  buildIdleEvent,
  signBody,
  newEventId,
  postIdleEvent,
  handleSessionIdle,
  setAcceptedSources,
  _resetLoopGuard,
  IdleNotifyConfigError,
  type IdleNotifyConfig,
} from "../src/idle-notify";

function cfgWith(overrides: Partial<IdleNotifyConfig> = {}): IdleNotifyConfig {
  return {
    enabled: true,
    titleMarker: "[wake]",
    source: "demo-claude",
    identity: "human:demo",
    ingestUrl: "http://127.0.0.1:8788/",
    secretUris: ["env://AGENT_WAKE_DEMO_CLAUDE_SECRET"],
    secretsFile: null,
    delivery: "next_session",
    ...overrides,
  };
}

function tmpSecretsFile(content: string): string {
  const dir = mkdtempSync(join(tmpdir(), "aw-oc-idle-"));
  const file = join(dir, "secrets.env");
  writeFileSync(file, content);
  return file;
}

afterEach(() => {
  _resetLoopGuard();
});

describe("parseIdleNotifyConfig", () => {
  test("absent block means feature off", () => {
    expect(parseIdleNotifyConfig({ version: 1 })).toBeNull();
  });

  test("enabled:false means feature off", () => {
    expect(
      parseIdleNotifyConfig({
        opencode_notify_on_idle: { enabled: false, source: "s", identity: "i" },
      })
    ).toBeNull();
  });

  test("requires source and identity", () => {
    expect(() =>
      parseIdleNotifyConfig({ opencode_notify_on_idle: { identity: "i" } })
    ).toThrow(IdleNotifyConfigError);
    expect(() =>
      parseIdleNotifyConfig({ opencode_notify_on_idle: { source: "s" } })
    ).toThrow(IdleNotifyConfigError);
  });

  test("defaults: marker, ingest url, next_session", () => {
    const cfg = parseIdleNotifyConfig({
      opencode_notify_on_idle: { source: "mvmcc03-claude", identity: "mvmcc03-agent" },
    })!;
    expect(cfg.titleMarker).toBe("[wake]");
    expect(cfg.ingestUrl).toBe("http://127.0.0.1:8788/");
    expect(cfg.delivery).toBe("next_session");
  });

  test("rejects unknown delivery mode", () => {
    expect(() =>
      parseIdleNotifyConfig({
        opencode_notify_on_idle: { source: "s", identity: "i", delivery: "sometimes" },
      })
    ).toThrow(/delivery/);
  });

  describe("secret URI resolution (rotation contract)", () => {
    test("post-rotation 'secrets' list from the source entry is used verbatim, newest first", () => {
      const cfg = parseIdleNotifyConfig({
        sources: {
          "demo-claude": {
            secrets: ["env://AGENT_WAKE_DEMO_SECRET_NEW", "env://AGENT_WAKE_DEMO_SECRET"],
          },
        },
        opencode_notify_on_idle: { source: "demo-claude", identity: "i" },
      })!;
      expect(cfg.secretUris).toEqual([
        "env://AGENT_WAKE_DEMO_SECRET_NEW",
        "env://AGENT_WAKE_DEMO_SECRET",
      ]);
    });

    test("source entry 'secret_env' spelling resolves", () => {
      const cfg = parseIdleNotifyConfig({
        sources: { "demo-claude": { secret_env: "AGENT_WAKE_DEMO_SECRET" } },
        opencode_notify_on_idle: { source: "demo-claude", identity: "i" },
      })!;
      expect(cfg.secretUris).toEqual(["env://AGENT_WAKE_DEMO_SECRET"]);
    });

    test("source entry 'secret' uri spelling resolves", () => {
      const cfg = parseIdleNotifyConfig({
        sources: { "demo-claude": { secret: "env://SOME_NAME" } },
        opencode_notify_on_idle: { source: "demo-claude", identity: "i" },
      })!;
      expect(cfg.secretUris).toEqual(["env://SOME_NAME"]);
    });

    test("v2 'senders' vocabulary is honored", () => {
      const cfg = parseIdleNotifyConfig({
        senders: { "demo-claude": { secret_env: "FROM_SENDERS" } },
        opencode_notify_on_idle: { source: "demo-claude", identity: "i" },
      })!;
      expect(cfg.secretUris).toEqual(["env://FROM_SENDERS"]);
    });

    test("explicit block-level secret_env overrides the source entry", () => {
      const cfg = parseIdleNotifyConfig({
        sources: { "demo-claude": { secrets: ["env://ROTATED"] } },
        opencode_notify_on_idle: {
          source: "demo-claude",
          identity: "i",
          secret_env: "OPERATOR_OVERRIDE",
        },
      })!;
      expect(cfg.secretUris).toEqual(["env://OPERATOR_OVERRIDE"]);
    });

    test("unknown source falls back to the derived name", () => {
      const cfg = parseIdleNotifyConfig({
        opencode_notify_on_idle: { source: "mvmcc03-claude", identity: "i" },
      })!;
      expect(cfg.secretUris).toEqual(["env://AGENT_WAKE_MVMCC03_CLAUDE_SECRET"]);
    });
  });
});

describe("resolveSecret", () => {
  test("file wins over a stale env copy", () => {
    process.env.AW_TEST_SECRET_A = "stale-from-env";
    try {
      const file = tmpSecretsFile("AW_TEST_SECRET_A=fresh-from-file\n");
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_A"], secretsFile: file });
      expect(resolveSecret(cfg)).toBe("fresh-from-file");
    } finally {
      delete process.env.AW_TEST_SECRET_A;
    }
  });

  test("env is the fallback when the file is missing", () => {
    process.env.AW_TEST_SECRET_A2 = "from-env";
    try {
      const cfg = cfgWith({
        secretUris: ["env://AW_TEST_SECRET_A2"],
        secretsFile: "/nonexistent",
      });
      expect(resolveSecret(cfg)).toBe("from-env");
    } finally {
      delete process.env.AW_TEST_SECRET_A2;
    }
  });

  test("reads KEY=VALUE, export prefix, and quotes from secrets file", () => {
    const file = tmpSecretsFile(
      ["# comment", "OTHER=zzz", 'export AW_TEST_SECRET_B="s3cret"', ""].join("\n")
    );
    const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_B"], secretsFile: file });
    expect(resolveSecret(cfg)).toBe("s3cret");
  });

  test("walks the rotation window: first uri unresolvable, second resolves", () => {
    const file = tmpSecretsFile("AW_TEST_ROT_OLD=old-but-valid\n");
    const cfg = cfgWith({
      secretUris: ["env://AW_TEST_ROT_NEW", "env://AW_TEST_ROT_OLD"],
      secretsFile: file,
    });
    expect(resolveSecret(cfg)).toBe("old-but-valid");
  });

  test("post-rotation file resolves the NEW name first", () => {
    const file = tmpSecretsFile("AW_TEST_ROT2=old\nAW_TEST_ROT2_NEW=new\n");
    const cfg = cfgWith({
      secretUris: ["env://AW_TEST_ROT2_NEW", "env://AW_TEST_ROT2"],
      secretsFile: file,
    });
    expect(resolveSecret(cfg)).toBe("new");
  });

  test("empty value in file is a tombstone — env NOT consulted for that name", () => {
    process.env.AW_TEST_SECRET_T = "stale";
    try {
      const file = tmpSecretsFile("AW_TEST_SECRET_T=\n");
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_T"], secretsFile: file });
      expect(resolveSecret(cfg)).toBeNull();
    } finally {
      delete process.env.AW_TEST_SECRET_T;
    }
  });

  test("unreadable existing file fails closed (no env fallback)", () => {
    process.env.AW_TEST_SECRET_U = "stale";
    try {
      const file = tmpSecretsFile("AW_TEST_SECRET_U=fresh\n");
      chmodSync(file, 0o000);
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_U"], secretsFile: file });
      expect(resolveSecret(cfg)).toBeNull();
      chmodSync(file, 0o600);
    } finally {
      delete process.env.AW_TEST_SECRET_U;
    }
  });

  test("non-env backends are skipped", () => {
    process.env.AW_TEST_SECRET_V = "from-env";
    try {
      const cfg = cfgWith({
        secretUris: ["vault://kv/agent-wake#demo", "env://AW_TEST_SECRET_V"],
        secretsFile: null,
      });
      expect(resolveSecret(cfg)).toBe("from-env");
    } finally {
      delete process.env.AW_TEST_SECRET_V;
    }
  });

  test("returns null when nothing resolvable", () => {
    const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_MISSING"], secretsFile: null });
    expect(resolveSecret(cfg)).toBeNull();
  });
});

describe("shouldNotify", () => {
  test("requires marker in title", () => {
    const cfg = cfgWith();
    expect(shouldNotify({ id: "s1", title: "[wake] review WI-9" }, cfg)).toBe(true);
    expect(shouldNotify({ id: "s1", title: "review WI-9" }, cfg)).toBe(false);
  });

  test("skips subagent sessions", () => {
    const cfg = cfgWith();
    expect(
      shouldNotify({ id: "s2", title: "[wake] child", parentID: "s1" }, cfg)
    ).toBe(false);
  });

  test("empty marker notifies every root session", () => {
    const cfg = cfgWith({ titleMarker: "" });
    expect(shouldNotify({ id: "s1", title: "anything" }, cfg)).toBe(true);
    expect(shouldNotify({ id: "s2", title: "x", parentID: "s1" }, cfg)).toBe(false);
  });
});

describe("buildIdleEvent / signBody", () => {
  test("event carries source, delivery meta, session id, and export hint", () => {
    const cfg = cfgWith();
    const { body } = buildIdleEvent(
      { id: "ses_x", title: "[wake] t", directory: "/repo" },
      cfg
    );
    const event = JSON.parse(body);
    expect(event.v).toBe(0);
    expect(event.source).toBe("demo-claude");
    expect(event.kind).toBe("task_complete");
    expect(event.wake).toBe(true);
    expect(event.meta.delivery).toBe("next_session");
    expect(event.meta.opencode_session_id).toBe("ses_x");
    expect(event.content).toContain("ses_x");
    expect(event.content).toContain("/repo");
    expect(event.content).toContain("opencode export ses_x");
  });

  test("signature matches openssl-style hmac-sha256 hex", () => {
    const body = '{"v":0,"event_id":"e1"}';
    const expected = createHmac("sha256", "k").update(body).digest("hex");
    expect(signBody(body, "k")).toBe(expected);
  });

  test("event ids are unique across rapid calls", () => {
    const seen = new Set(Array.from({ length: 50 }, () => newEventId()));
    expect(seen.size).toBe(50);
  });
});

describe("postIdleEvent", () => {
  test("posts signed request with source/identity headers and a timeout signal", async () => {
    process.env.AW_TEST_SECRET_C = "topsecret";
    try {
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_C"] });
      let captured: { url: string; init: any } | null = null;
      const fetchImpl = async (url: string, init: any) => {
        captured = { url, init };
        return { status: 202, text: async () => "" };
      };
      const ok = await postIdleEvent({ id: "ses_y", title: "[wake] t" }, cfg, fetchImpl);
      expect(ok).toBe(true);
      expect(captured!.url).toBe("http://127.0.0.1:8788/");
      const headers = captured!.init.headers;
      expect(headers["X-AgentWake-Source"]).toBe("demo-claude");
      expect(headers["X-AgentWake-Identity"]).toBe("human:demo");
      const expectedSig = createHmac("sha256", "topsecret")
        .update(captured!.init.body)
        .digest("hex");
      expect(headers["X-AgentWake-Signature"]).toBe(`sha256=${expectedSig}`);
      expect(captured!.init.signal).toBeInstanceOf(AbortSignal);
    } finally {
      delete process.env.AW_TEST_SECRET_C;
    }
  });

  test("missing secret drops without posting", async () => {
    const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_MISSING"], secretsFile: null });
    let called = false;
    const fetchImpl = async () => {
      called = true;
      return { status: 202, text: async () => "" };
    };
    const ok = await postIdleEvent({ id: "s", title: "[wake]" }, cfg, fetchImpl);
    expect(ok).toBe(false);
    expect(called).toBe(false);
  });

  test("non-2xx ingest response returns false", async () => {
    process.env.AW_TEST_SECRET_D = "k";
    try {
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_D"] });
      const fetchImpl = async () => ({ status: 403, text: async () => "denied" });
      const ok = await postIdleEvent({ id: "s", title: "[wake]" }, cfg, fetchImpl);
      expect(ok).toBe(false);
    } finally {
      delete process.env.AW_TEST_SECRET_D;
    }
  });

  test("refuses to publish as a source routed back to this adapter (loop guard)", async () => {
    process.env.AW_TEST_SECRET_L = "k";
    try {
      setAcceptedSources(["demo-opencode", "demo-claude"]);
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_L"], source: "demo-claude" });
      let called = false;
      const fetchImpl = async () => {
        called = true;
        return { status: 202, text: async () => "" };
      };
      const ok = await postIdleEvent({ id: "s", title: "[wake]" }, cfg, fetchImpl);
      expect(ok).toBe(false);
      expect(called).toBe(false);
    } finally {
      delete process.env.AW_TEST_SECRET_L;
    }
  });

  test("publishes normally when accepted sources do not include the target", async () => {
    process.env.AW_TEST_SECRET_L2 = "k";
    try {
      setAcceptedSources(["demo-opencode"]);
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_L2"], source: "demo-claude" });
      const fetchImpl = async () => ({ status: 202, text: async () => "" });
      const ok = await postIdleEvent({ id: "s", title: "[wake]" }, cfg, fetchImpl);
      expect(ok).toBe(true);
    } finally {
      delete process.env.AW_TEST_SECRET_L2;
    }
  });

  test("suppresses a concurrent duplicate for the same session", async () => {
    process.env.AW_TEST_SECRET_F2 = "k";
    try {
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_F2"] });
      let calls = 0;
      let release: () => void;
      const gate = new Promise<void>((r) => {
        release = r;
      });
      const fetchImpl = async () => {
        calls += 1;
        await gate;
        return { status: 202, text: async () => "" };
      };
      const first = postIdleEvent({ id: "same", title: "[wake]" }, cfg, fetchImpl);
      const second = await postIdleEvent({ id: "same", title: "[wake]" }, cfg, fetchImpl);
      expect(second).toBe(false);
      release!();
      expect(await first).toBe(true);
      expect(calls).toBe(1);
    } finally {
      delete process.env.AW_TEST_SECRET_F2;
    }
  });
});

describe("handleSessionIdle", () => {
  const sdkEnvelope = (data: any) => ({ data, error: undefined, response: { status: 200 } });

  test("fetches session, gates on marker, posts when matched", async () => {
    process.env.AW_TEST_SECRET_E = "k";
    try {
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_E"] });
      let posted = false;
      const fetchImpl = async () => {
        posted = true;
        return { status: 202, text: async () => "" };
      };
      const client = {
        session: {
          get: async () => sdkEnvelope({ id: "ses_z", title: "[wake] review", directory: "/r" }),
        },
      };
      await handleSessionIdle(client, "ses_z", cfg, fetchImpl);
      expect(posted).toBe(true);
    } finally {
      delete process.env.AW_TEST_SECRET_E;
    }
  });

  test("does nothing when config is null (feature off)", async () => {
    let touched = false;
    const client = {
      session: {
        get: async () => {
          touched = true;
          return sdkEnvelope({ id: "s", title: "[wake]" });
        },
      },
    };
    await handleSessionIdle(client, "s", null);
    expect(touched).toBe(false);
  });

  test("skips when title lacks marker", async () => {
    process.env.AW_TEST_SECRET_F = "k";
    try {
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_F"] });
      let posted = false;
      const fetchImpl = async () => {
        posted = true;
        return { status: 202, text: async () => "" };
      };
      const client = {
        session: { get: async () => sdkEnvelope({ id: "s", title: "plain title" }) },
      };
      await handleSessionIdle(client, "s", cfg, fetchImpl);
      expect(posted).toBe(false);
    } finally {
      delete process.env.AW_TEST_SECRET_F;
    }
  });

  test("SDK error envelope is handled without posting", async () => {
    process.env.AW_TEST_SECRET_G = "k";
    try {
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_G"] });
      let posted = false;
      const fetchImpl = async () => {
        posted = true;
        return { status: 202, text: async () => "" };
      };
      const client = {
        session: {
          get: async () => ({ data: undefined, error: { message: "not found" }, response: { status: 404 } }),
        },
      };
      await handleSessionIdle(client, "gone", cfg, fetchImpl);
      expect(posted).toBe(false);
    } finally {
      delete process.env.AW_TEST_SECRET_G;
    }
  });
});
