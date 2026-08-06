import { afterEach, describe, expect, test } from "bun:test";
import { chmodSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
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
  invalidateAcceptedSources,
  noteSessionActivity,
  handleOpencodeEvent,
  activitySessionId,
  _activeSessionCount,
  _resetActivity,
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
    configPath: null,
    secretEnvOverride: null,
    secretsFile: null,
    delivery: "next_session",
    ...overrides,
  };
}

function tmpConfigFile(data: Record<string, any>): string {
  const dir = mkdtempSync(join(tmpdir(), "aw-oc-cfg-"));
  const file = join(dir, "config.json");
  writeFileSync(file, JSON.stringify(data));
  return file;
}

function readFileSyncStr(path: string): string {
  return readFileSync(path, "utf-8");
}

function tmpSecretsFile(content: string): string {
  const dir = mkdtempSync(join(tmpdir(), "aw-oc-idle-"));
  const file = join(dir, "secrets.env");
  writeFileSync(file, content);
  return file;
}

afterEach(() => {
  _resetLoopGuard();
  _resetActivity();
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

    test("v2 'senders' vocabulary is NOT consulted (adapter is v0/v1 only)", () => {
      const cfg = parseIdleNotifyConfig({
        senders: { "demo-claude": { secret_env: "FROM_SENDERS" } },
        opencode_notify_on_idle: { source: "demo-claude", identity: "i" },
      })!;
      // Falls back to the derived name — the loader rejects v2 documents,
      // so senders entries must never silently feed secret resolution.
      expect(cfg.secretUris).toEqual(["env://AGENT_WAKE_DEMO_CLAUDE_SECRET"]);
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

  test("survives TWO rotations without restart: URI list re-derived from config at publish time", () => {
    // Startup: source has the original single secret_env.
    const configPath = tmpConfigFile({
      version: 1,
      sources: { "demo-claude": { secret_env: "AW_ROTLIVE_ORIG" } },
      opencode_notify_on_idle: { source: "demo-claude", identity: "i" },
    });
    const secretsFile = tmpSecretsFile("AW_ROTLIVE_ORIG=v0\n");
    const cfg = {
      ...parseIdleNotifyConfig(JSON.parse(readFileSyncStr(configPath)), configPath)!,
      secretsFile,
    };
    expect(resolveSecret(cfg)).toBe("v0");

    // Rotation 1: rotate prepends NEW, keeps ORIG (window of 2), appends to secrets.env.
    writeFileSync(
      configPath,
      JSON.stringify({
        version: 1,
        sources: {
          "demo-claude": { secrets: ["env://AW_ROTLIVE_NEW", "env://AW_ROTLIVE_ORIG"] },
        },
        opencode_notify_on_idle: { source: "demo-claude", identity: "i" },
      })
    );
    writeFileSync(secretsFile, "AW_ROTLIVE_ORIG=v0\nAW_ROTLIVE_NEW=v1\n");
    expect(resolveSecret(cfg)).toBe("v1");

    // Rotation 2: window is now [NEW_1, NEW] — ORIG is out. A startup
    // snapshot would still sign with v0 and be rejected; live re-read
    // must pick v2.
    writeFileSync(
      configPath,
      JSON.stringify({
        version: 1,
        sources: {
          "demo-claude": { secrets: ["env://AW_ROTLIVE_NEW_1", "env://AW_ROTLIVE_NEW"] },
        },
        opencode_notify_on_idle: { source: "demo-claude", identity: "i" },
      })
    );
    writeFileSync(
      secretsFile,
      "AW_ROTLIVE_ORIG=v0\nAW_ROTLIVE_NEW=v1\nAW_ROTLIVE_NEW_1=v2\n"
    );
    expect(resolveSecret(cfg)).toBe("v2");
  });

  test("explicit secret_env override pins the name across config rewrites", () => {
    const configPath = tmpConfigFile({
      version: 1,
      sources: { "demo-claude": { secrets: ["env://AW_PIN_ROTATED"] } },
      opencode_notify_on_idle: { source: "demo-claude", identity: "i", secret_env: "AW_PIN_FIXED" },
    });
    const secretsFile = tmpSecretsFile("AW_PIN_FIXED=pinned\nAW_PIN_ROTATED=rotated\n");
    const cfg = {
      ...parseIdleNotifyConfig(JSON.parse(readFileSyncStr(configPath)), configPath)!,
      secretsFile,
    };
    expect(resolveSecret(cfg)).toBe("pinned");
  });

  test("unreadable config at publish time falls back to the startup snapshot", () => {
    const secretsFile = tmpSecretsFile("AW_SNAP_ORIG=snapshot-value\n");
    const cfg = cfgWith({
      secretUris: ["env://AW_SNAP_ORIG"],
      configPath: "/nonexistent/config.json",
      secretsFile,
    });
    expect(resolveSecret(cfg)).toBe("snapshot-value");
  });

  test("AGENT_WAKE_SECRETS_ENV sets the default secrets file (CLI parity)", () => {
    const file = tmpSecretsFile("AW_CLIPARITY=via-override\n");
    process.env.AGENT_WAKE_SECRETS_ENV = file;
    try {
      const cfg = parseIdleNotifyConfig({
        sources: { "demo-claude": { secret_env: "AW_CLIPARITY" } },
        opencode_notify_on_idle: { source: "demo-claude", identity: "i" },
      })!;
      expect(cfg.secretsFile).toBe(file);
      expect(resolveSecret(cfg)).toBe("via-override");
    } finally {
      delete process.env.AGENT_WAKE_SECRETS_ENV;
    }
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
      setAcceptedSources(["demo-opencode"]);
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
    setAcceptedSources(["demo-opencode"]);
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
      setAcceptedSources(["demo-opencode"]);
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

  test("live config flipping to v2 refuses publishing (boundary holds under SIGHUP reload)", async () => {
    process.env.AW_TEST_SECRET_V2 = "k";
    try {
      setAcceptedSources(["demo-opencode"]);
      const configPath = tmpConfigFile({
        version: 1,
        sources: { "demo-claude": { secret_env: "AW_TEST_SECRET_V2" } },
        routing: { "demo-claude": { adapter: "claude" } },
        opencode_notify_on_idle: { source: "demo-claude", identity: "i" },
      });
      const cfg = cfgWith({
        secretUris: ["env://AW_TEST_SECRET_V2"],
        configPath,
        source: "demo-claude",
      });
      let calls = 0;
      const fetchImpl = async () => {
        calls += 1;
        return { status: 202, text: async () => "" };
      };
      expect(await postIdleEvent({ id: "s1", title: "[wake]" }, cfg, fetchImpl)).toBe(true);
      expect(calls).toBe(1);

      // The daemon SIGHUP-reloads this document without re-subscribing us.
      writeFileSync(
        configPath,
        JSON.stringify({
          version: 2,
          senders: { "demo-claude": { secret_env: "AW_TEST_SECRET_V2" } },
        })
      );
      expect(await postIdleEvent({ id: "s2", title: "[wake]" }, cfg, fetchImpl)).toBe(false);
      expect(calls).toBe(1);
    } finally {
      delete process.env.AW_TEST_SECRET_V2;
    }
  });

  test("live routing change TO opencode refuses even with a stale accepted_sources snapshot", async () => {
    process.env.AW_TEST_SECRET_R1 = "k";
    try {
      setAcceptedSources(["demo-opencode"]); // stale: does not include demo-claude
      const configPath = tmpConfigFile({
        version: 1,
        sources: { "demo-claude": { secret_env: "AW_TEST_SECRET_R1" } },
        routing: { "demo-claude": { adapter: "opencode" } },
        opencode_notify_on_idle: { source: "demo-claude", identity: "i" },
      });
      const cfg = cfgWith({
        secretUris: ["env://AW_TEST_SECRET_R1"],
        configPath,
        source: "demo-claude",
      });
      let called = false;
      const fetchImpl = async () => {
        called = true;
        return { status: 202, text: async () => "" };
      };
      expect(await postIdleEvent({ id: "s", title: "[wake]" }, cfg, fetchImpl)).toBe(false);
      expect(called).toBe(false);
    } finally {
      delete process.env.AW_TEST_SECRET_R1;
    }
  });

  test("routing away on disk does NOT override the snapshot (file may be unapplied); a fresh hello_ack does", async () => {
    process.env.AW_TEST_SECRET_R2 = "k";
    try {
      // Daemon's last confirmed answer says demo-claude routes to us…
      setAcceptedSources(["demo-claude"]);
      // …while the on-disk file says it routes elsewhere. The file could
      // be a pending or rejected reload — the daemon may still apply the
      // old routing, so publishing must stay refused.
      const configPath = tmpConfigFile({
        version: 1,
        sources: { "demo-claude": { secret_env: "AW_TEST_SECRET_R2" } },
        routing: { "demo-claude": { adapter: "claude" } },
        opencode_notify_on_idle: { source: "demo-claude", identity: "i" },
      });
      const cfg = cfgWith({
        secretUris: ["env://AW_TEST_SECRET_R2"],
        configPath,
        source: "demo-claude",
      });
      let called = false;
      const fetchImpl = async () => {
        called = true;
        return { status: 202, text: async () => "" };
      };
      expect(await postIdleEvent({ id: "s", title: "[wake]" }, cfg, fetchImpl)).toBe(false);
      expect(called).toBe(false);

      // A fresh hello_ack (resubscribe) confirming the change clears it.
      setAcceptedSources(["demo-opencode"]);
      expect(await postIdleEvent({ id: "s2", title: "[wake]" }, cfg, fetchImpl)).toBe(true);
    } finally {
      delete process.env.AW_TEST_SECRET_R2;
    }
  });

  test("no live routing entry falls back to the accepted_sources snapshot", async () => {
    process.env.AW_TEST_SECRET_R3 = "k";
    try {
      setAcceptedSources(["demo-claude"]);
      const configPath = tmpConfigFile({
        version: 1,
        sources: { "demo-claude": { secret_env: "AW_TEST_SECRET_R3" } },
        opencode_notify_on_idle: { source: "demo-claude", identity: "i" },
      });
      const cfg = cfgWith({
        secretUris: ["env://AW_TEST_SECRET_R3"],
        configPath,
        source: "demo-claude",
      });
      let called = false;
      const fetchImpl = async () => {
        called = true;
        return { status: 202, text: async () => "" };
      };
      expect(await postIdleEvent({ id: "s", title: "[wake]" }, cfg, fetchImpl)).toBe(false);
      expect(called).toBe(false);
    } finally {
      delete process.env.AW_TEST_SECRET_R3;
    }
  });

  test("refuses before the first hello_ack (fail closed pre-ack)", async () => {
    process.env.AW_TEST_SECRET_P1 = "k";
    try {
      // No setAcceptedSources call: startup state, empty snapshot,
      // nothing confirmed. Even a clean-looking live config must not
      // publish.
      const configPath = tmpConfigFile({
        version: 1,
        sources: { "demo-claude": { secret_env: "AW_TEST_SECRET_P1" } },
        routing: { "demo-claude": { adapter: "claude" } },
        opencode_notify_on_idle: { source: "demo-claude", identity: "i" },
      });
      const cfg = cfgWith({
        secretUris: ["env://AW_TEST_SECRET_P1"],
        configPath,
        source: "demo-claude",
      });
      let called = false;
      const fetchImpl = async () => {
        called = true;
        return { status: 202, text: async () => "" };
      };
      expect(await postIdleEvent({ id: "s", title: "[wake]" }, cfg, fetchImpl)).toBe(false);
      expect(called).toBe(false);
    } finally {
      delete process.env.AW_TEST_SECRET_P1;
    }
  });

  test("disconnect invalidates confirmation until the next hello_ack", async () => {
    process.env.AW_TEST_SECRET_P2 = "k";
    try {
      setAcceptedSources(["demo-opencode"]);
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_P2"], source: "demo-claude" });
      const fetchImpl = async () => ({ status: 202, text: async () => "" });
      expect(await postIdleEvent({ id: "s1", title: "[wake]" }, cfg, fetchImpl)).toBe(true);

      invalidateAcceptedSources();
      expect(await postIdleEvent({ id: "s2", title: "[wake]" }, cfg, fetchImpl)).toBe(false);

      setAcceptedSources(["demo-opencode"]);
      expect(await postIdleEvent({ id: "s3", title: "[wake]" }, cfg, fetchImpl)).toBe(true);
    } finally {
      delete process.env.AW_TEST_SECRET_P2;
    }
  });

  test("apply→edit-without-apply: observed dangerous routing latches until an ack excludes the source", async () => {
    process.env.AW_TEST_SECRET_P3 = "k";
    try {
      // Ack excludes demo-claude (stale relative to what follows).
      setAcceptedSources(["demo-opencode"]);
      const configPath = tmpConfigFile({
        version: 1,
        sources: { "demo-claude": { secret_env: "AW_TEST_SECRET_P3" } },
        routing: { "demo-claude": { adapter: "opencode" } }, // applied revision
        opencode_notify_on_idle: { source: "demo-claude", identity: "i" },
      });
      const cfg = cfgWith({
        secretUris: ["env://AW_TEST_SECRET_P3"],
        configPath,
        source: "demo-claude",
      });
      let calls = 0;
      const fetchImpl = async () => {
        calls += 1;
        return { status: 202, text: async () => "" };
      };
      // Observed dangerous → refused and latched.
      expect(await postIdleEvent({ id: "s1", title: "[wake]" }, cfg, fetchImpl)).toBe(false);

      // File edited back WITHOUT a (successful) reload — daemon may still
      // route demo-claude to opencode. Both naive guards would clear here;
      // the latch must hold.
      writeFileSync(
        configPath,
        JSON.stringify({
          version: 1,
          sources: { "demo-claude": { secret_env: "AW_TEST_SECRET_P3" } },
          routing: { "demo-claude": { adapter: "claude" } },
          opencode_notify_on_idle: { source: "demo-claude", identity: "i" },
        })
      );
      expect(await postIdleEvent({ id: "s2", title: "[wake]" }, cfg, fetchImpl)).toBe(false);
      expect(calls).toBe(0);

      // Fresh hello_ack that still includes the source keeps everything refused…
      setAcceptedSources(["demo-claude"]);
      expect(await postIdleEvent({ id: "s3", title: "[wake]" }, cfg, fetchImpl)).toBe(false);

      // …and one that excludes it (daemon-confirmed safe) clears the latch.
      setAcceptedSources(["demo-opencode"]);
      expect(await postIdleEvent({ id: "s4", title: "[wake]" }, cfg, fetchImpl)).toBe(true);
      expect(calls).toBe(1);
    } finally {
      delete process.env.AW_TEST_SECRET_P3;
    }
  });

  test("suppresses a concurrent duplicate for the same session", async () => {
    process.env.AW_TEST_SECRET_F2 = "k";
    try {
      setAcceptedSources(["demo-opencode"]);
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
      setAcceptedSources(["demo-opencode"]);
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
      noteSessionActivity("ses_z");
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
      noteSessionActivity("s");
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
      noteSessionActivity("gone");
      await handleSessionIdle(client, "gone", cfg, fetchImpl);
      expect(posted).toBe(false);
    } finally {
      delete process.env.AW_TEST_SECRET_G;
    }
  });
});

describe("startup replay guard", () => {
  const sdkEnvelope = (data: any) => ({ data, error: undefined, response: { status: 200 } });

  test("idle for a session never observed active is ignored (no session.get, no post)", async () => {
    process.env.AW_TEST_SECRET_R = "k";
    try {
      setAcceptedSources(["demo-opencode"]);
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_R"] });
      let fetched = false;
      let posted = false;
      const client = {
        session: {
          get: async () => {
            fetched = true;
            return sdkEnvelope({ id: "ses_old", title: "[wake] yesterday's review" });
          },
        },
      };
      const fetchImpl = async () => {
        posted = true;
        return { status: 202, text: async () => "" };
      };
      // No noteSessionActivity: this is opencode replaying a historical idle
      // at harness startup, which re-published nine stale wakes in production.
      await handleSessionIdle(client, "ses_old", cfg, fetchImpl);
      expect(fetched).toBe(false);
      expect(posted).toBe(false);
    } finally {
      delete process.env.AW_TEST_SECRET_R;
    }
  });

  test("a session observed active notifies exactly once per turn", async () => {
    process.env.AW_TEST_SECRET_R2 = "k";
    try {
      setAcceptedSources(["demo-opencode"]);
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_R2"] });
      let posts = 0;
      const client = {
        session: { get: async () => sdkEnvelope({ id: "ses_live", title: "[wake] now" }) },
      };
      const fetchImpl = async () => {
        posts += 1;
        return { status: 202, text: async () => "" };
      };

      noteSessionActivity("ses_live");
      await handleSessionIdle(client, "ses_live", cfg, fetchImpl);
      expect(posts).toBe(1);

      // A duplicate idle with no new activity must not re-notify.
      await handleSessionIdle(client, "ses_live", cfg, fetchImpl);
      expect(posts).toBe(1);

      // The next turn marks activity again and earns its own wake.
      noteSessionActivity("ses_live");
      await handleSessionIdle(client, "ses_live", cfg, fetchImpl);
      expect(posts).toBe(2);
    } finally {
      delete process.env.AW_TEST_SECRET_R2;
    }
  });

  test("activity marks are per session", async () => {
    process.env.AW_TEST_SECRET_R3 = "k";
    try {
      setAcceptedSources(["demo-opencode"]);
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_R3"] });
      let posts = 0;
      const client = {
        session: { get: async () => sdkEnvelope({ id: "ses_a", title: "[wake] a" }) },
      };
      const fetchImpl = async () => {
        posts += 1;
        return { status: 202, text: async () => "" };
      };
      noteSessionActivity("ses_a");
      await handleSessionIdle(client, "ses_b", cfg, fetchImpl);
      expect(posts).toBe(0);
      await handleSessionIdle(client, "ses_a", cfg, fetchImpl);
      expect(posts).toBe(1);
    } finally {
      delete process.env.AW_TEST_SECRET_R3;
    }
  });
});

describe("handleOpencodeEvent — real event sequences", () => {
  const sdkEnvelope = (data: any) => ({ data, error: undefined, response: { status: 200 } });

  function harness(title = "[wake] task") {
    const calls = { get: 0, post: 0 };
    const client = {
      session: {
        get: async () => {
          calls.get += 1;
          return sdkEnvelope({ id: "ses_1", title });
        },
      },
    };
    const fetchImpl = async () => {
      calls.post += 1;
      return { status: 202, text: async () => "" };
    };
    return { client, fetchImpl, calls };
  }

  const statusEvt = (type: string) => ({
    type: "session.status",
    properties: { sessionID: "ses_1", status: { type } },
  });
  const idleEvt = { type: "session.idle", properties: { sessionID: "ses_1" } };

  test("session.status{idle} does NOT count as activity", () => {
    expect(activitySessionId(statusEvt("idle"))).toBeNull();
    expect(activitySessionId(statusEvt("busy"))).toBe("ses_1");
    expect(activitySessionId(statusEvt("retry"))).toBe("ses_1");
  });

  test("startup replay (status{idle} then session.idle) notifies nothing", async () => {
    process.env.AW_TEST_SECRET_H1 = "k";
    try {
      setAcceptedSources(["demo-opencode"]);
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_H1"] });
      const { client, fetchImpl, calls } = harness();
      // Exactly what opencode emits when replaying a historical session.
      await handleOpencodeEvent(client, statusEvt("idle"), cfg, fetchImpl);
      await handleOpencodeEvent(client, idleEvt, cfg, fetchImpl);
      expect(calls.get).toBe(0);
      expect(calls.post).toBe(0);
    } finally {
      delete process.env.AW_TEST_SECRET_H1;
    }
  });

  test("a real turn (busy → idle status → session.idle) notifies exactly once", async () => {
    process.env.AW_TEST_SECRET_H2 = "k";
    try {
      setAcceptedSources(["demo-opencode"]);
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_H2"] });
      const { client, fetchImpl, calls } = harness();
      await handleOpencodeEvent(client, statusEvt("busy"), cfg, fetchImpl);
      await handleOpencodeEvent(client, statusEvt("idle"), cfg, fetchImpl);
      await handleOpencodeEvent(client, idleEvt, cfg, fetchImpl);
      expect(calls.post).toBe(1);
      // A duplicate idle with no new work must stay silent.
      await handleOpencodeEvent(client, idleEvt, cfg, fetchImpl);
      expect(calls.post).toBe(1);
    } finally {
      delete process.env.AW_TEST_SECRET_H2;
    }
  });

  test("message events also mark activity", async () => {
    process.env.AW_TEST_SECRET_H3 = "k";
    try {
      setAcceptedSources(["demo-opencode"]);
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_H3"] });
      const { client, fetchImpl, calls } = harness();
      await handleOpencodeEvent(
        client,
        { type: "message.updated", properties: { info: { sessionID: "ses_1" } } },
        cfg,
        fetchImpl
      );
      await handleOpencodeEvent(client, idleEvt, cfg, fetchImpl);
      expect(calls.post).toBe(1);

      await handleOpencodeEvent(
        client,
        { type: "message.part.updated", properties: { part: { sessionID: "ses_1" } } },
        cfg,
        fetchImpl
      );
      await handleOpencodeEvent(client, idleEvt, cfg, fetchImpl);
      expect(calls.post).toBe(2);
    } finally {
      delete process.env.AW_TEST_SECRET_H3;
    }
  });

  test("session lifecycle events are not activity", async () => {
    process.env.AW_TEST_SECRET_H4 = "k";
    try {
      setAcceptedSources(["demo-opencode"]);
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_H4"] });
      const { client, fetchImpl, calls } = harness();
      for (const type of ["session.created", "session.updated", "session.compacted"]) {
        await handleOpencodeEvent(
          client,
          { type, properties: { info: { id: "ses_1" }, sessionID: "ses_1" } },
          cfg,
          fetchImpl
        );
      }
      await handleOpencodeEvent(client, idleEvt, cfg, fetchImpl);
      expect(calls.post).toBe(0);
    } finally {
      delete process.env.AW_TEST_SECRET_H4;
    }
  });

  test("deletion forgets a pending activity mark", async () => {
    process.env.AW_TEST_SECRET_H5 = "k";
    try {
      setAcceptedSources(["demo-opencode"]);
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_H5"] });
      const { client, fetchImpl, calls } = harness();
      await handleOpencodeEvent(client, statusEvt("busy"), cfg, fetchImpl);
      expect(_activeSessionCount()).toBe(1);
      await handleOpencodeEvent(
        client,
        { type: "session.deleted", properties: { info: { id: "ses_1" } } },
        cfg,
        fetchImpl
      );
      expect(_activeSessionCount()).toBe(0);
      await handleOpencodeEvent(client, idleEvt, cfg, fetchImpl);
      expect(calls.post).toBe(0);
    } finally {
      delete process.env.AW_TEST_SECRET_H5;
    }
  });

  test("orphaned activity marks are bounded (LRU eviction)", async () => {
    process.env.AW_TEST_SECRET_H6 = "k";
    try {
      setAcceptedSources(["demo-opencode"]);
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_H6"] });
      const { client, fetchImpl } = harness();
      // Sessions that go busy and never idle or get deleted — stalled runs,
      // lost terminal events, a server killed mid-turn.
      for (let i = 0; i < 700; i += 1) {
        await handleOpencodeEvent(
          client,
          { type: "session.status", properties: { sessionID: `ses_orphan_${i}`, status: { type: "busy" } } },
          cfg,
          fetchImpl
        );
      }
      expect(_activeSessionCount()).toBeLessThanOrEqual(512);

      // Assert real behaviour, not just parsing: the newest session still
      // holds its mark and notifies, while the oldest was evicted and stays
      // silent.
      const newestClient = {
        session: {
          get: async () => sdkEnvelope({ id: "ses_orphan_699", title: "[wake] newest" }),
        },
      };
      let posts = 0;
      const countingFetch = async () => {
        posts += 1;
        return { status: 202, text: async () => "" };
      };
      await handleOpencodeEvent(
        newestClient,
        { type: "session.idle", properties: { sessionID: "ses_orphan_699" } },
        cfg,
        countingFetch
      );
      expect(posts).toBe(1);

      await handleOpencodeEvent(
        newestClient,
        { type: "session.idle", properties: { sessionID: "ses_orphan_0" } },
        cfg,
        countingFetch
      );
      expect(posts).toBe(1);
    } finally {
      delete process.env.AW_TEST_SECRET_H6;
    }
  });

  test("repeat activity refreshes recency so a busy session is not evicted", async () => {
    process.env.AW_TEST_SECRET_H7 = "k";
    try {
      setAcceptedSources(["demo-opencode"]);
      const cfg = cfgWith({ secretUris: ["env://AW_TEST_SECRET_H7"] });
      const { client, fetchImpl, calls } = harness();
      const busy = (id: string) => ({
        type: "session.status",
        properties: { sessionID: id, status: { type: "busy" } },
      });
      await handleOpencodeEvent(client, busy("ses_1"), cfg, fetchImpl);
      for (let i = 0; i < 400; i += 1) {
        await handleOpencodeEvent(client, busy(`ses_filler_${i}`), cfg, fetchImpl);
        // Keep ses_1 fresh, as a genuinely active session would be.
        await handleOpencodeEvent(client, busy("ses_1"), cfg, fetchImpl);
      }
      for (let i = 400; i < 900; i += 1) {
        await handleOpencodeEvent(client, busy(`ses_filler_${i}`), cfg, fetchImpl);
        await handleOpencodeEvent(client, busy("ses_1"), cfg, fetchImpl);
      }
      await handleOpencodeEvent(client, idleEvt, cfg, fetchImpl);
      expect(calls.post).toBe(1);
    } finally {
      delete process.env.AW_TEST_SECRET_H7;
    }
  });

  test("feature disabled records no activity at all (no unbounded growth)", async () => {
    const { client, fetchImpl, calls } = harness();
    await handleOpencodeEvent(client, statusEvt("busy"), null, fetchImpl);
    await handleOpencodeEvent(
      client,
      { type: "message.updated", properties: { info: { sessionID: "ses_x" } } },
      null,
      fetchImpl
    );
    expect(_activeSessionCount()).toBe(0);
    expect(calls.post).toBe(0);
  });
});
