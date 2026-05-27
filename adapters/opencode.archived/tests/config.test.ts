import { describe, expect, test } from "bun:test";
import { loadConfig } from "../src/config";
import type { Config } from "../src/config";
import { mkdirSync, writeFileSync, rmSync } from "fs";
import { mkdtempSync } from "fs";
import { join } from "path";
import { tmpdir } from "os";

function writeTmpConfig(data: Record<string, any>): string {
  const dir = mkdtempSync(join(tmpdir(), "aw-test-"));
  const file = join(dir, "config.json");
  writeFileSync(file, JSON.stringify(data));
  return file;
}

function cleanup(path: string) {
  try {
    rmSync(join(path, ".."), { recursive: true, force: true });
  } catch {}
}

function withEnv(env: Record<string, string>, fn: () => void) {
  const saved: Record<string, string | undefined> = {};
  for (const k of Object.keys(env)) {
    saved[k] = process.env[k];
    process.env[k] = env[k];
  }
  try {
    fn();
  } finally {
    for (const k of Object.keys(env)) {
      if (saved[k] === undefined) {
        delete process.env[k];
      } else {
        process.env[k] = saved[k]!;
      }
    }
  }
}

describe("loadConfig", () => {
  test("loads a valid config with sources and resolves secrets", () => {
    const configPath = writeTmpConfig({
      version: 0,
      listen: { host: "127.0.0.1", port: 8788 },
      sources: {
        demo: { secret_env: "DEMO_SECRET", callback_url: "http://example.com/cb" },
      },
      default_callback_url: null,
    });
    withEnv({ AGENT_WAKE_CONFIG: configPath, DEMO_SECRET: "shhh" }, () => {
      const cfg = loadConfig();
      expect(cfg.version).toBe(0);
      expect(cfg.host).toBe("127.0.0.1");
      expect(cfg.port).toBe(8788);
      expect(cfg.sources["demo"]).toBeDefined();
      expect(new TextDecoder().decode(cfg.sources["demo"].secret)).toBe("shhh");
      expect(cfg.sources["demo"].callback_url).toBe("http://example.com/cb");
    });
  });

  test("rejects config with wrong version", () => {
    const configPath = writeTmpConfig({ version: 99, sources: {} });
    withEnv({ AGENT_WAKE_CONFIG: configPath }, () => {
      expect(() => loadConfig()).toThrow(/version/i);
    });
  });

  test("rejects config with missing secret env var", () => {
    const configPath = writeTmpConfig({
      version: 0,
      listen: {},
      sources: {
        demo: { secret_env: "MISSING_SECRET" },
      },
    });
    withEnv({ AGENT_WAKE_CONFIG: configPath }, () => {
      expect(() => loadConfig()).toThrow(/not set/i);
    });
  });

  test("rejects config with no sources", () => {
    const configPath = writeTmpConfig({
      version: 0,
      sources: {},
    });
    withEnv({ AGENT_WAKE_CONFIG: configPath }, () => {
      expect(() => loadConfig()).toThrow(/at least one source/i);
    });
  });

  test("falls back to default host and port when listen is missing", () => {
    const configPath = writeTmpConfig({
      version: 0,
      listen: {},
      sources: {
        demo: { secret_env: "DEMO_SECRET" },
      },
    });
    withEnv({ AGENT_WAKE_CONFIG: configPath, DEMO_SECRET: "shhh" }, () => {
      const cfg = loadConfig();
      expect(cfg.host).toBe("127.0.0.1");
      expect(cfg.port).toBe(8788);
    });
  });

  test("uses default_callback_url when source has none", () => {
    const configPath = writeTmpConfig({
      version: 0,
      listen: {},
      sources: {
        demo: { secret_env: "DEMO_SECRET" },
      },
      default_callback_url: "http://fallback.example.com/cb",
    });
    withEnv({ AGENT_WAKE_CONFIG: configPath, DEMO_SECRET: "shhh" }, () => {
      const cfg = loadConfig();
      expect(cfg.sources["demo"].callback_url).toBe("http://fallback.example.com/cb");
    });
  });

  test("rejects config file that does not exist", () => {
    withEnv({ AGENT_WAKE_CONFIG: "/tmp/aw-nonexistent-config.json" }, () => {
      expect(() => loadConfig()).toThrow(/Failed to load config/i);
    });
  });

  test("rejects source without secret_env field", () => {
    const configPath = writeTmpConfig({
      version: 0,
      listen: {},
      sources: {
        demo: {},
      },
    });
    withEnv({ AGENT_WAKE_CONFIG: configPath }, () => {
      expect(() => loadConfig()).toThrow(/secret_env/i);
    });
  });
});