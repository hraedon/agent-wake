/**
 * Per-session label registry for routing wake events to specific
 * opencode sessions.
 *
 * Design: see /projects/agent-wake/design/self-register-plan.md (tier 1).
 *
 * Storage is in-memory only. On opencode restart, all subscriptions are
 * lost — agents must re-register on their next turn. A
 * `~/.local/state/agent-wake/labels.json` persistence layer is a
 * follow-up; do not add it without first deciding GC semantics for
 * stale labels claimed by sessions that no longer exist.
 *
 * Self-attestation: callers (the tool handlers in index.ts) receive
 * the calling `sessionID` from opencode's `ToolContext`; they cannot
 * forge it, so this module's API takes `sessionID` as a parameter
 * rather than trying to discover it.
 *
 * Auth: labels are UNAUTHENTICATED in v1. Any session can claim any
 * label. Threat model is misconfiguration, not adversary. See
 * design/self-register-plan.md §Auth.
 */

const sessionToLabels: Map<string, Set<string>> = new Map();
const labelToSessions: Map<string, Set<string>> = new Map();

function getOrCreate<K, V>(map: Map<K, V>, key: K, factory: () => V): V {
  let v = map.get(key);
  if (v === undefined) {
    v = factory();
    map.set(key, v);
  }
  return v;
}

/** Register `sessionID` under `label`. Idempotent. */
export function subscribe(sessionID: string, label: string): void {
  if (!sessionID || !label) return;
  getOrCreate(sessionToLabels, sessionID, () => new Set()).add(label);
  getOrCreate(labelToSessions, label, () => new Set()).add(sessionID);
}

/**
 * Drop `label` from `sessionID`. If `label` is undefined, drop all
 * labels held by that session.
 */
export function unsubscribe(sessionID: string, label?: string): string[] {
  if (!sessionID) return [];
  const held = sessionToLabels.get(sessionID);
  if (!held) return [];

  const toRemove = label === undefined ? [...held] : held.has(label) ? [label] : [];
  for (const l of toRemove) {
    held.delete(l);
    const sessions = labelToSessions.get(l);
    if (sessions) {
      sessions.delete(sessionID);
      if (sessions.size === 0) labelToSessions.delete(l);
    }
  }
  if (held.size === 0) sessionToLabels.delete(sessionID);
  return toRemove;
}

/** Drop all labels held by `sessionID`. Returns the labels that were removed. */
export function clearSession(sessionID: string): string[] {
  return unsubscribe(sessionID);
}

/** All session IDs currently subscribed to `label`. */
export function sessionsWithLabel(label: string): string[] {
  const s = labelToSessions.get(label);
  return s ? [...s] : [];
}

/** All labels held by `sessionID`, sorted for stable display. */
export function labelsForSession(sessionID: string): string[] {
  const s = sessionToLabels.get(sessionID);
  return s ? [...s].sort() : [];
}

/** Test-only: wipe all state. */
export function _reset(): void {
  sessionToLabels.clear();
  labelToSessions.clear();
}
