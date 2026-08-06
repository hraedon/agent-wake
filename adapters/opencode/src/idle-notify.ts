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
 * The signing secret comes from the operator's existing secrets
 * provisioning (same user, same trust domain); the inbound wake path
 * continues to hold no secrets.
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
 *
 * Loop prevention: if the configured target source routes back to THIS
 * adapter (the daemon's hello_ack `accepted_sources` is the authority),
 * a published wake would be delivered into an opencode session, whose
 * turn would idle and publish again — an unbounded wake/idle loop that
 * event-id dedupe cannot stop (every cycle mints a fresh id). The
 * adapter therefore refuses to publish when `cfg.source` is one of its
 * own accepted sources. See `setAcceptedSources` / index.ts wiring.
 */
import { createHmac } from "node:crypto";
import { readFileSync, existsSync } from "node:fs";
import { log } from "./log";

export interface IdleNotifyConfig {
  enabled: boolean;
  titleMarker: string;
  source: string;
  identity: string;
  ingestUrl: string;
  /**
   * STARTUP SNAPSHOT of the ordered secret URIs (first resolvable wins).
   * Mirrors the daemon's source vocabulary: after `agent-wake secrets
   * rotate` a source holds `secrets: ["env://..._SECRET_NEW", "env://..."]`
   * — newest first. The daemon verifies against ANY entry in the window,
   * so signing with the first resolvable one is always accepted.
   *
   * Rotation happens while opencode keeps running, so the LIVE list is
   * re-derived from `configPath` on every publish (see
   * `currentSecretUris`); this snapshot is only the fallback when the
   * config file has become unreadable at publish time. Only env:// URIs
   * are resolvable by this adapter today; other backends (vault://,
   * file://) are skipped with a warning.
   */
  secretUris: string[];
  /** Path of the config document secretUris came from; null in tests. */
  configPath: string | null;
  /** Explicit block-level secret_env override — disables live re-derivation. */
  secretEnvOverride: string | null;
  secretsFile: string | null;
  delivery: "live_only" | "next_session" | "managed_session";
}

export class IdleNotifyConfigError extends Error {}

function defaultSecretEnv(source: string): string {
  return `AGENT_WAKE_${source.replace(/[^A-Za-z0-9]+/g, "_").toUpperCase()}_SECRET`;
}

function defaultSecretsFile(): string | null {
  // AGENT_WAKE_SECRETS_ENV is the same override the operator CLI honors
  // (daemon cli/secrets.py _env_file_path) — a custom secrets file set
  // there must not require redundant adapter configuration.
  const override = process.env.AGENT_WAKE_SECRETS_ENV;
  if (override) return override;
  return process.env.HOME ? `${process.env.HOME}/.config/agent-wake/secrets.env` : null;
}

/**
 * Derive the ordered secret-URI list for a source from the same config
 * document the daemon reads, honoring the v0/v1 source-vocabulary
 * spellings (`secrets` list > `secret` uri > `secret_env` name). This is
 * what keeps the adapter working across `agent-wake secrets rotate`,
 * which renames the env var and rewrites the source to list form.
 *
 * v0/v1 ONLY — deliberately. This whole adapter's loader (config.ts)
 * rejects version-2 documents, so notify-on-idle inherits that boundary:
 * the v2 `senders` vocabulary is NOT consulted here, and the loop guard
 * below relies on v1 semantics (routing maps a source name to exactly
 * one adapter, so source-name equality against accepted_sources is the
 * correct loop test). When the adapter grows v2 support, this function
 * and the loop guard must be revisited together.
 */
function secretUrisForSource(raw: any, source: string): string[] {
  const entry = raw?.sources?.[source];
  if (entry && typeof entry === "object") {
    if (Array.isArray(entry.secrets) && entry.secrets.length > 0) {
      return entry.secrets.filter((u: unknown) => typeof u === "string");
    }
    if (typeof entry.secret === "string" && entry.secret) {
      return [entry.secret];
    }
    if (typeof entry.secret_env === "string" && entry.secret_env) {
      return [`env://${entry.secret_env}`];
    }
  }
  // Source not in this config document (or carries no secret spec):
  // legacy derived-name fallback so a minimal adapter-only config works.
  return [`env://${defaultSecretEnv(source)}`];
}

/**
 * The live on-disk config document, re-read per publish. The daemon
 * reloads its config on SIGHUP without re-subscribing adapters, and
 * `agent-wake secrets rotate` rewrites it while opencode keeps running —
 * so EVERY publish-time decision (secret URIs, version boundary, loop
 * routing) must come from the same live read, not startup snapshots
 * (review cycle 3, blocking). null when unreadable/unparsable.
 */
function readLiveConfig(cfg: IdleNotifyConfig): any | null {
  if (!cfg.configPath) return null;
  try {
    return JSON.parse(readFileSync(cfg.configPath, "utf-8"));
  } catch (e: any) {
    log.warn(
      `notify-on-idle: could not re-read ${cfg.configPath} (${e?.message ?? e}); ` +
        `falling back to startup snapshot where safe`
    );
    return null;
  }
}

/**
 * Secret URIs for this publish. An explicit operator override pins the
 * list and skips re-derivation; a live document re-derives (rotation
 * support); an unreadable document falls back to the startup snapshot —
 * better a possibly-stale window entry than dropping the wake outright.
 */
function currentSecretUris(cfg: IdleNotifyConfig, live: any | null): string[] {
  if (cfg.secretEnvOverride) return [`env://${cfg.secretEnvOverride}`];
  if (live) return secretUrisForSource(live, cfg.source);
  return cfg.secretUris;
}

/**
 * Decide whether this publish must be refused, from the LIVE document.
 * Returns a human-readable reason or null to proceed.
 *
 * - Live document with an unsupported version: refuse everything — the
 *   v0/v1 boundary must hold even when the file changes under us.
 * - Live v0/v1 routing entry for the source: authoritative. Refuse iff
 *   it routes to the opencode adapter (wake/idle feedback loop); a
 *   routing change AWAY from opencode un-suppresses publishing even if
 *   the hello_ack snapshot is stale.
 * - No live routing entry (or unreadable document): fall back to the
 *   most recent hello_ack accepted_sources snapshot.
 */
function publishRefusalReason(cfg: IdleNotifyConfig, live: any | null): string | null {
  if (live) {
    const version = live.version ?? 0;
    if (version !== 0 && version !== 1) {
      return `live config at ${cfg.configPath} is version ${JSON.stringify(version)}; notify-on-idle supports v0/v1 only`;
    }
    const route = live.routing?.[cfg.source];
    if (route && typeof route === "object" && typeof route.adapter === "string") {
      if (route.adapter === "opencode") {
        return `live config routes source ${JSON.stringify(cfg.source)} to the opencode adapter — publishing would create a wake/idle feedback loop`;
      }
      return null; // live routing is authoritative and points elsewhere
    }
  }
  if (acceptedSources.has(cfg.source)) {
    return `the daemon routes source ${JSON.stringify(cfg.source)} back to this opencode adapter (accepted_sources) — publishing would create a wake/idle feedback loop`;
  }
  return null;
}

/**
 * Parse the optional `opencode_notify_on_idle` block out of the (already
 * JSON-parsed) agent-wake config document. Returns null when the block is
 * absent or disabled — callers treat null as "feature off".
 */
export function parseIdleNotifyConfig(raw: any, configPath: string | null = null): IdleNotifyConfig | null {
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

  // Explicit block-level secret_env is an operator override; otherwise
  // resolve from the source's own entry so rotation Just Works.
  const secretEnvOverride =
    typeof block.secret_env === "string" && block.secret_env ? block.secret_env : null;
  const secretUris = secretEnvOverride
    ? [`env://${secretEnvOverride}`]
    : secretUrisForSource(raw, source);

  return {
    enabled: true,
    titleMarker: typeof block.title_marker === "string" ? block.title_marker : "[wake]",
    source,
    identity,
    ingestUrl: typeof block.ingest_url === "string" ? block.ingest_url : "http://127.0.0.1:8788/",
    secretUris,
    configPath,
    secretEnvOverride,
    secretsFile:
      block.secrets_file === null
        ? null
        : typeof block.secrets_file === "string"
          ? block.secrets_file
          : defaultSecretsFile(),
    delivery,
  };
}

type FileLookup =
  | { state: "found"; value: string }
  | { state: "empty" } // key present with empty value — explicit tombstone
  | { state: "absent" } // file readable, key not in it
  | { state: "no_file" } // file not configured or does not exist
  | { state: "unreadable" }; // file exists but could not be read — fail closed

function lookupSecretInFile(secretsFile: string | null, name: string): FileLookup {
  if (!secretsFile) return { state: "no_file" };
  if (!existsSync(secretsFile)) return { state: "no_file" };
  let text: string;
  try {
    text = readFileSync(secretsFile, "utf-8");
  } catch {
    return { state: "unreadable" };
  }
  for (const line of text.split("\n")) {
    const m = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/);
    if (!m || m[1] !== name) continue;
    let value = m[2];
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    return value ? { state: "found", value } : { state: "empty" };
  }
  return { state: "absent" };
}

/**
 * Resolve the HMAC secret by walking `secretUris` in order and returning
 * the first resolvable value. Per env:// name, the secrets.env FILE wins
 * and process env is only a fallback when the file does not exist or
 * lacks the key entirely.
 *
 * File-over-env is deliberate and load-bearing: the daemon loads its
 * copy via systemd EnvironmentFile=secrets.env, so the file is the
 * source of truth. This plugin runs inside whatever process hosts
 * opencode — typically a long-lived interactive shell whose environment
 * can carry a STALE copy of the secret from before a rotation (observed
 * in production: tmux shell env predated a secrets.env regeneration and
 * every ingest POST 403'd). Consequently:
 *   - key present but EMPTY in the file → treated as an explicit
 *     tombstone; env is NOT consulted for that name.
 *   - file exists but cannot be read → fail closed for that name (no
 *     env fallback) — a permission error must not silently downgrade to
 *     stale-env signing.
 * Read at send time, not cached, so a rotated secret takes effect
 * without a server restart.
 */
export function resolveSecret(
  cfg: IdleNotifyConfig,
  live: any | null = readLiveConfig(cfg)
): string | null {
  for (const uri of currentSecretUris(cfg, live)) {
    if (!uri.startsWith("env://")) {
      log.warn(`notify-on-idle: unsupported secret backend ${JSON.stringify(uri)}; skipping`);
      continue;
    }
    const name = uri.slice("env://".length);
    if (!name) continue;
    const fromFile = lookupSecretInFile(cfg.secretsFile, name);
    switch (fromFile.state) {
      case "found":
        return fromFile.value;
      case "empty":
      case "unreadable":
        if (fromFile.state === "unreadable") {
          log.warn(
            `notify-on-idle: secrets file ${cfg.secretsFile} exists but is unreadable; failing closed for ${name}`
          );
        }
        continue; // no env fallback for this name
      case "absent":
      case "no_file": {
        const fromEnv = process.env[name];
        if (fromEnv) return fromEnv;
        continue;
      }
    }
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

const POST_TIMEOUT_MS = 10_000;

/**
 * Sources the daemon routes to THIS adapter, per the most recent
 * hello_ack. Publishing as one of these would deliver the wake straight
 * back into opencode — see the loop-prevention note in the module
 * docstring. index.ts updates this on every (re)subscribe.
 *
 * v1 semantics only: under v0/v1, routing maps a source name to exactly
 * one adapter, so name equality is the correct loop test. Under the v2
 * sender/destination split the comparison would be between different
 * namespaces — but this adapter's loader rejects v2 configs outright,
 * so that combination cannot arise (see secretUrisForSource docstring).
 */
let acceptedSources: ReadonlySet<string> = new Set();
const refusalReasonsLogged = new Set<string>();

export function setAcceptedSources(sources: readonly string[]): void {
  acceptedSources = new Set(sources);
  refusalReasonsLogged.clear();
}

/** Test-only. */
export function _resetLoopGuard(): void {
  acceptedSources = new Set();
  refusalReasonsLogged.clear();
}

/** Sessions with a POST already in flight — suppress concurrent duplicates. */
const inFlight = new Set<string>();

export async function postIdleEvent(
  info: IdleSessionInfo,
  cfg: IdleNotifyConfig,
  fetchImpl: FetchLike = fetch as unknown as FetchLike
): Promise<boolean> {
  const live = readLiveConfig(cfg);
  const refusal = publishRefusalReason(cfg, live);
  if (refusal) {
    if (!refusalReasonsLogged.has(refusal)) {
      refusalReasonsLogged.add(refusal);
      log.error(`notify-on-idle: refusing to publish — ${refusal}`);
    }
    return false;
  }

  if (inFlight.has(info.id)) {
    log.warn(`notify-on-idle: publish already in flight for session ${info.id}; skipping duplicate`);
    return false;
  }

  const secret = resolveSecret(cfg, live);
  if (!secret) {
    log.warn(
      `notify-on-idle: no signing secret resolvable from ${JSON.stringify(cfg.secretUris)} ` +
        `(file ${cfg.secretsFile ?? "<none>"}); dropping wake for session ${info.id}`
    );
    return false;
  }

  const { body, eventId } = buildIdleEvent(info, cfg);
  const sig = signBody(body, secret);
  inFlight.add(info.id);
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
      signal: AbortSignal.timeout(POST_TIMEOUT_MS),
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
  } finally {
    inFlight.delete(info.id);
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
