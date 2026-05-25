import { loadConfig, Config } from "./config";
import { startIngest } from "./ingest";
import { tool } from "@opencode-ai/plugin";
import { postReply } from "./reply";

const z = tool.schema;

type PluginContext = any;
type Hooks = any;

const activeSessions = new Set<string>();

let pluginConfig: Config | null = null;

function trackSession(event: any) {
  const id = event?.session?.id;
  if (id) activeSessions.add(id);
}

function untrackSession(event: any) {
  const id = event?.session?.id;
  if (id) activeSessions.delete(id);
}

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
    return postReply(pluginConfig, args.source, args.content, args.in_reply_to);
  },
});

export default async function plugin(ctx: PluginContext): Promise<Hooks> {
  pluginConfig = loadConfig();
  startIngest(ctx, pluginConfig, activeSessions);

  return {
    "session.created": (event: any) => trackSession(event),
    "session.deleted": (event: any) => untrackSession(event),
    tool: {
      agent_wake_reply: agentWakeReply,
    },
  };
}