/**
 * Deliver a wake event into one or more opencode sessions.
 *
 * Why `promptAsync` and not `prompt`:
 *   `session.prompt` blocks until the model finishes responding. The
 *   archived plugin called it from a plugin-hook context, which deadlocks
 *   the plugin event loop while a session is actively replying. The SDK
 *   exposes `session.promptAsync` ("start if needed and return
 *   immediately") which is the correct primitive for waking a session
 *   from outside.
 *
 * Session targeting (in precedence order):
 *   1. If `event.meta.target` is a string, deliver to every session
 *      that has self-registered for that label via the
 *      `agent_wake_subscribe` tool. Empty match → drop with a warning;
 *      the event is the sender's signal that something should happen
 *      but the operator has not wired any session to listen for it.
 *   2. Else if `event.meta.session_id` is a string, deliver only to
 *      that session. (Legacy direct-id path. Callers using `target`
 *      should prefer it; this remains for backwards compat.)
 *   3. Else, broadcast to every session returned by
 *      `client.session.list()` (legacy broadcast).
 *
 * If both `meta.target` and `meta.session_id` are set, `target` wins
 * and a warning is logged.
 *
 * `wake: false` maps to `noReply: true` (silent-inject, no agent turn).
 *
 * Per-session label registry lives in `src/labels.ts`. Routing labels
 * are unauthenticated in v1; see `design/self-register-plan.md` §Auth.
 */
import { log } from "./log";
import type { WakeEvent } from "./client";
import { sessionsWithLabel } from "./labels";

export function formatWakeEvent(event: WakeEvent): string {
  const source = event.source ?? "";
  const kind = event.kind ?? "";
  const content = event.content ?? "";
  const esc = (s: string) =>
    String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  return `<wake source="${esc(source)}" kind="${esc(kind)}">\n${esc(content)}\n</wake>`;
}

/**
 * SDK shape. `list` and `promptAsync` are wrappers from `@opencode-ai/sdk`
 * that, by default, do NOT throw on non-2xx — they return
 * `{ data, error, response }`. We must check `.error` explicitly; the
 * archived plugin did not, which is one reason "the wake never arrived"
 * was indistinguishable from "the wake arrived but the SDK 4xx'd."
 */
export interface OpencodeClientLike {
  session: {
    list: (opts?: any) => Promise<any>;
    promptAsync: (opts: {
      path: { id: string };
      body: { noReply?: boolean; parts: Array<{ type: "text"; text: string }> };
      query?: { directory?: string };
    }) => Promise<{ data?: unknown; error?: unknown; response?: { status?: number } } | any>;
  };
}

async function discoverSessions(client: OpencodeClientLike): Promise<string[]> {
  try {
    const result: any = await client.session.list();
    // The SDK wrapper returns {data, error, response}; the test mocks
    // return either a bare array or a {data: [...]} envelope. Handle all.
    const sessions = Array.isArray(result)
      ? result
      : Array.isArray(result?.data)
        ? result.data
        : [];
    if (!Array.isArray(sessions)) return [];
    return sessions
      .filter((s: any) => s && typeof s.id === "string" && !s.deletedAt)
      .map((s: any) => s.id);
  } catch (e: any) {
    log.warn(`session.list failed: ${e?.message ?? e}`);
    return [];
  }
}

export async function deliverWake(
  client: OpencodeClientLike,
  event: WakeEvent
): Promise<{ delivered: number; sessions: string[] }> {
  const targets: string[] = [];
  const target = event.meta?.target;
  const sessionId = event.meta?.session_id;

  if (typeof target === "string" && target.length > 0) {
    if (typeof sessionId === "string" && sessionId.length > 0) {
      log.warn(
        `event has both meta.target='${target}' and meta.session_id='${sessionId}'; target wins`
      );
    }
    const subscribed = sessionsWithLabel(target);
    if (subscribed.length === 0) {
      log.warn(
        `no session subscribed to label '${target}', dropping event_id=${event.event_id ?? "?"}`
      );
      return { delivered: 0, sessions: [] };
    }
    targets.push(...subscribed);
  } else if (typeof sessionId === "string" && sessionId.length > 0) {
    targets.push(sessionId);
  } else {
    const live = await discoverSessions(client);
    targets.push(...live);
  }

  if (targets.length === 0) {
    log.warn(
      `no live opencode sessions for event_id=${event.event_id ?? "?"} source=${event.source ?? "?"}`
    );
    return { delivered: 0, sessions: [] };
  }

  const text = formatWakeEvent(event);
  const noReply = event.wake === false;
  const delivered: string[] = [];

  await Promise.all(
    targets.map(async (sessionId) => {
      try {
        const result: any = await client.session.promptAsync({
          path: { id: sessionId },
          body: { noReply, parts: [{ type: "text", text }] },
        });
        // SDK wrappers default to ThrowOnError=false: a 4xx returns
        // {error, response} rather than throwing. Surface it.
        if (result && typeof result === "object" && "error" in result && result.error) {
          const status = result.response?.status ?? "?";
          const errStr =
            typeof result.error === "string"
              ? result.error
              : JSON.stringify(result.error);
          log.warn(
            `promptAsync non-2xx for session ${sessionId}: status=${status} error=${errStr}`
          );
          return;
        }
        delivered.push(sessionId);
        log.info(`delivered to session ${sessionId}`);
      } catch (e: any) {
        log.warn(`promptAsync threw for session ${sessionId}: ${e?.message ?? e}`);
      }
    })
  );

  return { delivered: delivered.length, sessions: delivered };
}
