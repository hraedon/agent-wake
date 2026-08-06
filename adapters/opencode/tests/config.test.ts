import { describe, expect, test } from "bun:test";
import { mkdtempSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { loadConfig, ConfigError } from "../src/config";

function writeTmpConfig(data: Record<string, any>): string {
  const dir = mkdtempSync(join(tmpdir(), "aw-oc-test-"));
  const file = join(dir, "config.json");
  writeFileSync(file, JSON.stringify(data));
  return file;
}

describe("loadConfig", () => {
  test("accepts a v1 config with sources", () => {
    const p = writeTmpConfig({
      version: 1,
      listen: { host: "127.0.0.1", port: 8788 },
      socket_path: "/tmp/test.sock",
      sources: { demo: { secret_env: "DEMO" } },
    });
    const cfg = loadConfig(p);
    expect(cfg.version).toBe(1);
    expect(cfg.socketPath).toBe("/tmp/test.sock");
    expect(cfg.sources).toEqual(["demo"]);
  });

  test("accepts a v0 config (legacy)", () => {
    const p = writeTmpConfig({
      version: 0,
      listen: {},
      sources: { a: { secret_env: "X" }, b: { secret_env: "Y" } },
    });
    const cfg = loadConfig(p);
    expect(cfg.version).toBe(0);
    expect(cfg.socketPath).toBeNull();
    expect(cfg.sources.sort()).toEqual(["a", "b"]);
  });

  test("rejects unsupported version", () => {
    const p = writeTmpConfig({ version: 99, sources: { a: {} } });
    expect(() => loadConfig(p)).toThrow(ConfigError);
  });

  test("rejects empty sources", () => {
    const p = writeTmpConfig({ version: 1, sources: {} });
    expect(() => loadConfig(p)).toThrow(/at least one source/i);
  });

  test("rejects missing file", () => {
    expect(() => loadConfig("/tmp/aw-oc-missing.json")).toThrow(/Failed to load/i);
  });

  test("does not require secret_env (daemon owns secrets)", () => {
    const p = writeTmpConfig({
      version: 1,
      sources: { demo: {} },
    });
    const cfg = loadConfig(p);
    expect(cfg.sources).toEqual(["demo"]);
  });

  test("notifyOnIdle is null when the block is absent", () => {
    const p = writeTmpConfig({ version: 1, sources: { demo: {} } });
    expect(loadConfig(p).notifyOnIdle).toBeNull();
  });

  test("notifyOnIdle parses through the public loader with configPath and source-derived URIs", () => {
    const p = writeTmpConfig({
      version: 1,
      sources: {
        demo: { secret_env: "X" },
        "demo-claude": { secrets: ["env://ROT_NEW", "env://ROT_OLD"] },
      },
      opencode_notify_on_idle: { source: "demo-claude", identity: "i" },
    });
    const cfg = loadConfig(p);
    expect(cfg.notifyOnIdle).not.toBeNull();
    expect(cfg.notifyOnIdle!.source).toBe("demo-claude");
    expect(cfg.notifyOnIdle!.configPath).toBe(p);
    expect(cfg.notifyOnIdle!.secretUris).toEqual(["env://ROT_NEW", "env://ROT_OLD"]);
  });

  test("v2 documents are rejected by the loader — notify-on-idle cannot run against v2", () => {
    const p = writeTmpConfig({
      version: 2,
      senders: { demo: { secret_env: "X" } },
      opencode_notify_on_idle: { source: "demo", identity: "i" },
    });
    expect(() => loadConfig(p)).toThrow(ConfigError);
  });
});
