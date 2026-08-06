/**
 * Lightweight config loader for the opencode daemon-client adapter.
 *
 * The daemon owns HMAC secrets and the HTTP listener. The adapter only
 * needs the list of source names (for the `hello.filters.sources`
 * handshake) and an optional `socket_path` override.
 *
 * Accepts v0 and v1 config files. v0's `listen`/`port` are tolerated but
 * unused; a warning is logged if they are set.
 */
import { readFileSync } from "fs";
import { log } from "./log";
import { parseIdleNotifyConfig, type IdleNotifyConfig } from "./idle-notify";

export interface Config {
  version: number;
  socketPath: string | null;
  sources: string[];
  /** Optional notify-on-idle block; null when absent or disabled. */
  notifyOnIdle: IdleNotifyConfig | null;
}

export class ConfigError extends Error {}

function defaultConfigPath(): string {
  if (!process.env.HOME) {
    throw new ConfigError("$HOME is not set; set AGENT_WAKE_CONFIG instead");
  }
  return `${process.env.HOME}/.config/agent-wake/config.json`;
}

export function loadConfig(path?: string): Config {
  const configPath = path || process.env.AGENT_WAKE_CONFIG || defaultConfigPath();

  let raw: any;
  try {
    raw = JSON.parse(readFileSync(configPath, "utf-8"));
  } catch (e: any) {
    throw new ConfigError(`Failed to load config from ${configPath}: ${e.message}`);
  }

  const version = raw.version ?? 0;
  if (version !== 0 && version !== 1) {
    throw new ConfigError(
      `Unsupported config version ${JSON.stringify(version)}. Only 0 and 1 are accepted.`
    );
  }

  const listen = raw.listen || {};
  const host = listen.host || "127.0.0.1";
  const port = listen.port || 8788;
  if (host !== "127.0.0.1" || port !== 8788) {
    log.warn(
      "host/port are no longer used by the adapter; the daemon owns the HTTP listener"
    );
  }

  const sourcesRaw = raw.sources || {};
  const sources = Object.keys(sourcesRaw);
  if (sources.length === 0) {
    throw new ConfigError("At least one source must be configured.");
  }

  return {
    version,
    socketPath: raw.socket_path ?? null,
    sources,
    // Pass the resolved path so notify-on-idle can re-derive the secret
    // URI list at publish time — rotation rewrites this document while
    // opencode keeps running.
    notifyOnIdle: parseIdleNotifyConfig(raw, configPath),
  };
}
