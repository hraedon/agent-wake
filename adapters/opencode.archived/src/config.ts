export interface SourceConfig {
  secret: Uint8Array;
  callback_url?: string | null;
}

export interface Config {
  version: number;
  host: string;
  port: number;
  sources: Record<string, SourceConfig>;
  default_callback_url: string | null;
}

import { readFileSync } from "fs";

const DEFAULT_CONFIG_PATH = (() => {
  if (!process.env.HOME) {
    throw new Error("$HOME is not set; set AGENT_WAKE_CONFIG instead");
  }
  return `${process.env.HOME}/.config/agent-wake/config.json`;
})();

export function loadConfig(): Config {
  const configPath = process.env.AGENT_WAKE_CONFIG || DEFAULT_CONFIG_PATH;

  let raw: any;
  try {
    raw = JSON.parse(readFileSync(configPath, "utf-8"));
  } catch (e: any) {
    throw new Error(`Failed to load config from ${configPath}: ${e.message}`);
  }

  if (raw.version !== 0) {
    throw new Error("Unsupported config version. Only version 0 is supported.");
  }

  const listen = raw.listen || {};
  const config: Config = {
    version: 0,
    host: listen.host || "127.0.0.1",
    port: listen.port || 8788,
    sources: {},
    default_callback_url: raw.default_callback_url || null,
  };

  const sources = raw.sources || {};
  if (Object.keys(sources).length === 0) {
    throw new Error("At least one source must be configured.");
  }

  for (const [name, info] of Object.entries(sources)) {
    const src = info as any;
    const secretEnv = src.secret_env;
    if (!secretEnv || typeof secretEnv !== "string") {
      throw new Error(`Source ${name} must have a 'secret_env' string field.`);
    }
    const secret = process.env[secretEnv];
    if (!secret) {
      throw new Error(
        `Source ${name} references secret_env ${secretEnv} which is not set`
      );
    }
    config.sources[name] = {
      secret: new TextEncoder().encode(secret),
      callback_url: src.callback_url || config.default_callback_url,
    };
  }

  return config;
}
