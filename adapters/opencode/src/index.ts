/**
 * agent-wake opencode plugin — daemon-client edition.
 *
 * Binds no ports. Connects to agent-waked over the unix-socket protocol
 * defined in v1-daemon-spec.md §4. Wake frames arriving from the daemon
 * are delivered to opencode sessions via SDK `session.promptAsync`.
 * Reply tool calls are forwarded to the daemon as `reply` frames; the
 * daemon owns the outbound HTTPS POST to the source's callback URL.
 *
 * Three opencode-specific quirks discovered the hard way and worth
 * preserving here so a future maintainer does not relearn them:
 *
 *   1. **Only the default export is allowed.** opencode iterates every
 *      named export and treats each as a plugin, calling it during
 *      config-hook resolution. A non-Plugin named export (e.g. a test
 *      helper) crashes the loader with
 *      "undefined is not an object (evaluating 'O.config')" — and that
 *      crash silently poisons subsequent session.prompt_async writes
 *      for the lifetime of the server (the symptom is 204 + empty
 *      session.messages, with no error surfaced to the caller).
 *
 *   2. **`session.promptAsync` is the wake primitive.** The synchronous
 *      `session.prompt` blocks until the model finishes responding,
 *      which deadlocks the plugin event loop. The archived plugin used
 *      `session.prompt` and that is why "the wake never arrived."
 *
 *   3. **The SDK does not throw on 4xx.** `promptAsync` returns
 *      `{ data, error, response }` with `error` set on non-2xx. Check
 *      it explicitly — see wake.ts.
 */
import { tool } from "@opencode-ai/plugin";
import { loadConfig, type Config } from "./config";
import { runClient, defaultSocketPath, type WakeEvent } from "./client";
import { deliverWake, type OpencodeClientLike } from "./wake";
import { executeReply } from "./reply";
import {
  handleOpencodeEvent,
  invalidateAcceptedSources,
  setAcceptedSources,
  type IdleNotifyConfig,
} from "./idle-notify";
import {
  subscribe as labelSubscribe,
  unsubscribe as labelUnsubscribe,
  labelsForSession,
  clearSession,
} from "./labels";
import { log } from "./log";

const z = tool.schema;

type PluginContext = any;
type Hooks = any;

let started = false;
let stopClient: (() => Promise<void>) | null = null;
let savedCtx: PluginContext | null = null;
let lastAttemptAt: number = 0;
let notifyOnIdleConfig: IdleNotifyConfig | null = null;
const MIN_RETRY_INTERVAL_MS = 5_000;

function ensureClientStarted(): void {
  if (started) return;
  if (!savedCtx) return;
  const now = Date.now();
  if (lastAttemptAt && now - lastAttemptAt < MIN_RETRY_INTERVAL_MS) return;
  lastAttemptAt = now;
  started = true;
  try {
    const config: Config = loadConfig();
    notifyOnIdleConfig = config.notifyOnIdle;
    const socketPath = config.socketPath ?? defaultSocketPath();
    const sdkClient = savedCtx?.client as OpencodeClientLike | undefined;
    if (!sdkClient?.session) {
      log.warn("plugin context has no session client; wake delivery disabled");
    }
    stopClient = runClient({
      socketPath,
      sources: config.sources,
      onAcceptedSources: setAcceptedSources,
      onDisconnect: invalidateAcceptedSources,
      onWake: async (event: WakeEvent) => {
        if (!sdkClient?.session) {
          log.warn("dropping wake event: opencode client unavailable");
          return;
        }
        await deliverWake(sdkClient, event);
      },
    });
    log.info(
      `daemon client started, socket=${socketPath}, sources=${JSON.stringify(config.sources)}`
    );
  } catch (e: any) {
    started = false;
    log.error(`failed to start daemon client: ${e?.message ?? e}`);
  }
}

const agentWakeReply = tool({
  description:
    "Reply to an external event source. The daemon POSTs the reply to the source's configured callback URL.",
  args: {
    source: z.string().describe("The event source name (from the wake tag's source attribute)"),
    content: z.string().describe("The reply body text"),
    in_reply_to: z
      .string()
      .optional()
      .describe("The event_id being replied to, if known"),
  },
  async execute(args: { source: string; content: string; in_reply_to?: string }) {
    ensureClientStarted();
    return await executeReply(args);
  },
});

// Self-registration tools. The tool's `execute(args, context)` receives
// `context.sessionID` from opencode — that is the self-attestation: a
// session can only register itself. Labels are in-memory only and
// unauthenticated; see src/labels.ts and design/self-register-plan.md.

const agentWakeSubscribe = tool({
  description:
    "Subscribe this opencode session to a routing label. External wake events whose meta.target matches the label will be delivered to this session.",
  args: {
    label: z
      .string()
      .min(1)
      .describe("The routing label to claim (e.g. 'oncall', 'build-bot')"),
  },
  async execute(args: { label: string }, context: { sessionID: string }) {
    labelSubscribe(context.sessionID, args.label);
    const all = labelsForSession(context.sessionID);
    return `subscribed session ${context.sessionID} to '${args.label}' (now holding: ${JSON.stringify(all)})`;
  },
});

const agentWakeUnsubscribe = tool({
  description:
    "Remove a routing label from this session, or all labels if no label is given.",
  args: {
    label: z
      .string()
      .optional()
      .describe("The label to drop. Omit to drop all labels held by this session."),
  },
  async execute(args: { label?: string }, context: { sessionID: string }) {
    const removed = labelUnsubscribe(context.sessionID, args.label);
    if (removed.length === 0) {
      return `session ${context.sessionID} held no matching labels`;
    }
    const remaining = labelsForSession(context.sessionID);
    return `dropped ${JSON.stringify(removed)} from session ${context.sessionID} (still holding: ${JSON.stringify(remaining)})`;
  },
});

const agentWakeStatus = tool({
  description:
    "Show the routing labels this session currently holds. Note: labels are not persisted across opencode restarts.",
  args: {},
  async execute(_args: {}, context: { sessionID: string }) {
    const labels = labelsForSession(context.sessionID);
    if (labels.length === 0) {
      return `session ${context.sessionID} holds no labels`;
    }
    return `session ${context.sessionID} labels: ${JSON.stringify(labels)}`;
  },
});

function extractDeletedSessionId(input: any): string | null {
  // opencode's event payload shape varies across versions. Be liberal:
  // accept event.properties.info.id, event.properties.sessionID, or
  // event.sessionID. Returns null if not a delete or shape unknown.
  const evt = input?.event ?? input;
  if (!evt || typeof evt !== "object") return null;
  if (evt.type !== "session.deleted") return null;
  const props = evt.properties ?? {};
  const id =
    (typeof props?.info?.id === "string" && props.info.id) ||
    (typeof props?.sessionID === "string" && props.sessionID) ||
    (typeof evt.sessionID === "string" && evt.sessionID) ||
    null;
  return id || null;
}

export default async function plugin(ctx: PluginContext): Promise<Hooks> {
  savedCtx = ctx;
  setImmediate(() => ensureClientStarted());
  return {
    event: async (input: any) => {
      ensureClientStarted();
      const deletedId = extractDeletedSessionId(input);
      if (deletedId) {
        const dropped = clearSession(deletedId);
        if (dropped.length > 0) {
          log.info(
            `gc: dropped labels ${JSON.stringify(dropped)} for deleted session ${deletedId}`
          );
        }
      }
      const evt = input?.event ?? input;
      // Activity tracking + idle notification both live in idle-notify so the
      // exact event sequence opencode emits can be tested end to end.
      // Fire-and-forget: never let a notify failure break event handling.
      void handleOpencodeEvent(savedCtx?.client, evt, notifyOnIdleConfig).catch(
        (e: any) => log.warn(`notify-on-idle: unhandled error: ${e?.message ?? e}`)
      );
    },
    tool: {
      agent_wake_reply: agentWakeReply,
      agent_wake_subscribe: agentWakeSubscribe,
      agent_wake_unsubscribe: agentWakeUnsubscribe,
      agent_wake_status: agentWakeStatus,
    },
  };
}

// CRITICAL: do not add named exports here — see module docstring,
// quirk 1. Move test helpers to a separate file if you need them.
