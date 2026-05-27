import { loadConfig, Config } from "./config";
import { startIngest } from "./ingest";
import { tool } from "@opencode-ai/plugin";
import { postReply } from "./reply";

const z = tool.schema;

type PluginContext = any;
type Hooks = any;

let pluginConfig: Config | null = null;
let pluginCtx: PluginContext | null = null;
let ingestStarted = false;

const log = {
  info: (...args: any[]) => console.error("[agent-wake-opencode] INFO", ...args),
  warn: (...args: any[]) => console.error("[agent-wake-opencode] WARN", ...args),
  error: (...args: any[]) => console.error("[agent-wake-opencode] ERR", ...args),
};

const agentWakeReply = tool({
  description:
    "Reply to an external event source. The reply is POSTed to the source's callback URL.",
  args: {
    source: z.string().describe("The event source name (from the wake tag's source attribute)"),
    content: z.string().describe("The reply body text"),
    in_reply_to: z.string().optional().describe("The event_id being replied to, if known"),
  },
  async execute(args, context) {
    if (!pluginConfig) {
      return "agent-wake not configured";
    }
    return await postReply(pluginConfig, args.source, args.content, args.in_reply_to);
  },
});

async function discoverSessions(): Promise<string[]> {
  if (!pluginCtx?.client?.session) return [];
  try {
    const result = await pluginCtx.client.session.list();
    const sessions = (result as any)?.data ?? result;
    if (!Array.isArray(sessions)) return [];
    return sessions
      .filter((s: any) => s && s.id && s.deletedAt === undefined)
      .map((s: any) => s.id);
  } catch (e: any) {
    log.warn(`session.list failed: ${e?.message ?? e}`);
    return [];
  }
}

export { discoverSessions };

export default async function plugin(ctx: PluginContext): Promise<Hooks> {
  pluginConfig = loadConfig();
  pluginCtx = ctx;

  if (!ingestStarted) {
    try {
      startIngest(ctx, pluginConfig, discoverSessions);
      ingestStarted = true;
    } catch (e: any) {
      log.warn(`ingest server failed to start (port in use?): ${e?.message ?? e}`);
    }
  }

  return {
    event: async ({ event }: { event: any }) => {
      if (event.type === "session.idle" || event.type === "session.created") {
        log.info(`event: ${event.type}`);
      }
    },
    tool: {
      agent_wake_reply: agentWakeReply,
    },
  };
}
