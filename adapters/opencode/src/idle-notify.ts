/**
 * Notify-on-idle: wake another agent when a marked opencode session
 * finishes a turn.
 *
 * Motivation (the original agent-wake use case): a harness — typically a
 * Claude Code session — delegates long work to opencode headlessly
 * (`opencode run …` / `POST /session/{id}/prompt_async`) and should not
 * poll for completion. This module listens for opencode's `session.idle`
 * event and publishes a wake event to the LOCAL agent-waked HTTP ingest,
 * which routes it to the configured source's adapter (e.g. the Claude
 * adapter) like any other wake.
 *
 * Why HTTP ingest and not a socket frame: the daemon↔adapter wire
 * protocol (v1-daemon-spec.md §4.3) has no adapter→daemon event-publish
 * frame — ingest is HTTP-only, gated by HMAC + trigger-identity. Rather
 * than extend the protocol, the adapter signs a normal ingest request.
 * That keeps the daemon's single ingest/gating/dedupe path authoritative.
 * The signing secret is read from the same secrets.env the operator
 * already provisions for the daemon (same user, same trust domain); the
 * inbound wake path continues to hold no secrets.
 *
 * Which sessions notify: only ROOT sessions (no parentID — subagent
 * spawns idle too and would double-fire) whose title contains the
 * configured marker (default "[wake]"). The delegating harness opts a
 * session in by putting the marker in the title it sets at creation
 * (`--title` / session.create body). Every completed turn of a marked
 * session emits one wake — for delegation that is exactly one per
 * submitted turn, which is the desired cadence.
 *
 * Delivery defaults to `next_session`: a live_only wake with no
 * subscriber is DROPPED by the daemon (agent-wake WI-007), and the whole
 * point here is surviving the gap between "reviewer finished" and "the
 * delegating session gets scheduled".
 */
import { createHmac } from "node:crypto";
import { readFileSync } from "node:fs";
import { log } from "./log";

export interface IdleNotifyConfig {
  enabled: boolean;
  titleMarker: string;
  source: string;
  identity: string;
  ingestUrl: string;
  secretEnv: string;
  secretsFile: string | null;
  delivery: "live_only" | "next_session" | "managed_session";
}

export class IdleNotifyConfigError extends Error {}

function defaultSecretEnv(source: string): string {
  return `AGENT_WAKE_${source.replace(/[^A-Za-z0-9]+/g, "_").toUpperCase()}_SECRET`;
}

function defaultSecretsFile(): string | null {
  return process.env.HOME ? `${process.env.HOME}/.config/agent-wake/secrets.env` : null;
}

/**
 * Parse the optional `opencode_notify_on_idle` block out of the (already
 * JSON-parsed) agent-wake config document. Returns null when the block is
 * absent or disabled — callers treat null as "feature off".
 */
export function parseIdleNotifyConfig(raw: any): IdleNotifyConfig | null {
  const block = raw?.opencode_notify_on_idle;
  if (!block) return null;
  if (typeof block !== "object" || Array.isArray(block)) {
    throw new IdleNotifyConfigError("opencode_notify_on_idle must be an object");
  }
  if (block.enabled === false) return null;

  const source = block.source;
  if (typeof source !== "string" || source.length === 0) {
    throw new IdleNotifyConfigError(
      "opencode_notify_on_idle.source is required (the daemon source to publish as, e.g. 'mvmcc03-claude')"
    );
  }
  const identity = block.identity;
  if (typeof identity !== "string" || identity.length === 0) {
    throw new IdleNotifyConfigError(
      "opencode_notify_on_idle.identity is required (must be in the source's allowed_trigger_identities)"
    );
  }

  const delivery = block.delivery ?? "next_session";
  if (!["live_only", "next_session", "managed_session"].includes(delivery)) {
    throw new IdleNotifyConfigError(
      `opencode_notify_on_idle.delivery must be live_only|next_session|managed_session, got ${JSON.stringify(delivery)}`
    );
  }

  return {
    enabled: true,
    titleMarker: typeof block.title_marker === "string" ? block.title_marker : "[wake]",
    source,
    identity,
    ingestUrl: typeof block.ingest_url === "string" ? block.ingest_url : "http://127.0.0.1:8788/",
    secretEnv: typeof block.secret_env === "string" ? block.secret_env : defaultSecretEnv(source),
    secretsFile:
      block.secrets_file === null
        ? null
        : typeof block.secrets_file === "string"
          ? block.secrets_file
          : defaultSecretsFile(),
    delivery,
  };
}

/**
 * Resolve the HMAC secret: the secrets.env FILE wins (KEY=VALUE lines,
 * optional `export ` prefix, optional single/double quotes — the subset
 * the daemon's own docs use), with process env only as a fallback when
 * the file is absent or lacks the key.
 *
 * File-over-env is deliberate and load-bearing: the daemon loads its
 * copy via systemd EnvironmentFile=secrets.env, so the file is the
 * source of truth. This plugin runs inside whatever process hosts
 * opencode — typically a long-lived interactive shell whose environment
 * can carry a STALE copy of the secret from before a rotation (observed
 * in production: tmux shell env predated a secrets.env regeneration and
 * every ingest POST 403'd). Read at send time, not cached, so a rotated
 * secret takes effect without a server restart.
 */
export function resolveSecret(cfg: IdleNotifyConfig): string | null {
  const fromFile = readSecretFromFile(cfg);
  if (fromFile) return fromFile;
  return process.env[cfg.secretEnv] || null;
}

function readSecretFromFile(cfg: IdleNotifyConfig): string | null {
  if (!cfg.secretsFile) return null;
  let text: string;
  try {
    text = readFileSync(cfg.secretsFile, "utf-8");
  } catch {
    return null;
  }
  for (const line of text.split("\n")) {
    const m = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/);
    if (!m || m[1] !== cfg.secretEnv) continue;
    let value = m[2];
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    return value || null;
  }
  return null;
}

/** Monotonic-enough unique id: time prefix + random suffix (daemon dedupes on it). */
export function newEventId(): string {
  const rand = createHmac("sha256", String(Math.random()))
    .update(String(process.hrtime.bigint()))
    .digest("hex")
    .slice(0, 16);
  return `ocidle-${Date.now().toString(36)}-${rand}`;
}

export interface IdleSessionInfo {
  id: string;
  title: string;
  parentID?: string | null;
  directory?: string | null;
}

/** Gate: root session + title marker (empty marker means "notify for every root session"). */
export function shouldNotify(info: IdleSessionInfo, cfg: IdleNotifyConfig): boolean {
  if (info.parentID) return false;
  if (cfg.titleMarker.length === 0) return true;
  return (info.title ?? "").includes(cfg.titleMarker);
}

export function buildIdleEvent(info: IdleSessionInfo, cfg: IdleNotifyConfig): {
  body: string;
  eventId: string;
} {
  const eventId = newEventId();
  const where = info.directory ? ` in ${info.directory}` : "";
  const event = {
    v: 0,
    event_id: eventId,
    source: cfg.source,
    kind: "task_complete",
    content:
      `opencode session ${info.id} ("${info.title}")${where} is idle. ` +
      `Fetch the result with: opencode export ${info.id}`,
    wake: true,
    meta: {
      delivery: cfg.delivery,
      opencode_session_id: info.id,
    },
  };
  return { body: JSON.stringify(event), eventId };
}

export function signBody(body: string, secret: string): string {
  return createHmac("sha256", secret).update(body).digest("hex");
}

/** Injectable fetch so tests don't need a live daemon. */
export type FetchLike = (url: string, init: any) => Promise<{ status: number; text(): Promise<string> }>;

export async function postIdleEvent(
  info: IdleSessionInfo,
  cfg: IdleNotifyConfig,
  fetchImpl: FetchLike = fetch as unknown as FetchLike
): Promise<boolean> {
  const secret = resolveSecret(cfg);
  if (!secret) {
    log.warn(
      `notify-on-idle: no secret available (env ${cfg.secretEnv}, file ${cfg.secretsFile ?? "<none>"}); dropping wake for session ${info.id}`
    );
    return false;
  }

  const { body, eventId } = buildIdleEvent(info, cfg);
  const sig = signBody(body, secret);
  try {
    const res = await fetchImpl(cfg.ingestUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-AgentWake-Source": cfg.source,
        "X-AgentWake-Identity": cfg.identity,
        "X-AgentWake-Signature": `sha256=${sig}`,
      },
      body,
    });
    if (res.status >= 200 && res.status < 300) {
      log.info(
        `notify-on-idle: published ${eventId} for session ${info.id} (status ${res.status}, delivery ${cfg.delivery})`
      );
      return true;
    }
    const detail = await res.text().catch(() => "");
    log.warn(
      `notify-on-idle: ingest rejected ${eventId} for session ${info.id}: status ${res.status} ${detail.slice(0, 200)}`
    );
    return false;
  } catch (e: any) {
    log.warn(`notify-on-idle: ingest POST failed for session ${info.id}: ${e?.message ?? e}`);
    return false;
  }
}

/**
 * SDK surface needed here. `session.get` follows the same
 * {data, error, response} envelope convention as the rest of the SDK
 * (see wake.ts docstring) — check `.error`, never assume throw-on-4xx.
 */
export interface SessionGetClientLike {
  session: {
    get: (opts: { path: { id: string } }) => Promise<any>;
  };
}

export async function handleSessionIdle(
  client: SessionGetClientLike | undefined,
  sessionID: string,
  cfg: IdleNotifyConfig | null,
  fetchImpl?: FetchLike
): Promise<void> {
  if (!cfg) return;
  if (!client?.session?.get) {
    log.warn("notify-on-idle: opencode client unavailable; cannot inspect idle session");
    return;
  }
  let info: IdleSessionInfo | null = null;
  try {
    const result: any = await client.session.get({ path: { id: sessionID } });
    const data = result?.data ?? result;
    if (result && typeof result === "object" && "error" in result && result.error) {
      log.warn(
        `notify-on-idle: session.get non-2xx for ${sessionID}: ${JSON.stringify(result.error).slice(0, 200)}`
      );
      return;
    }
    if (data && typeof data.id === "string") {
      info = {
        id: data.id,
        title: typeof data.title === "string" ? data.title : "",
        parentID: data.parentID ?? null,
        directory: data.directory ?? data?.location?.directory ?? null,
      };
    }
  } catch (e: any) {
    log.warn(`notify-on-idle: session.get threw for ${sessionID}: ${e?.message ?? e}`);
    return;
  }
  if (!info) return;
  if (!shouldNotify(info, cfg)) return;
  await postIdleEvent(info, cfg, fetchImpl);
}
