import { Config, SourceConfig } from "./config";
import { verifySignature } from "./gating";

const log = {
  info: (...args: any[]) => console.error("[agent-wake-opencode] INFO", ...args),
  warn: (...args: any[]) => console.error("[agent-wake-opencode] WARN", ...args),
  error: (...args: any[]) => console.error("[agent-wake-opencode] ERR", ...args),
};

function generateEventId(): string {
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}

const recentEventIds: string[] = [];
const MAX_DEDUPE = 256;

export function isDuplicate(eventId: string): boolean {
  if (recentEventIds.includes(eventId)) return true;
  recentEventIds.push(eventId);
  while (recentEventIds.length > MAX_DEDUPE) {
    recentEventIds.shift();
  }
  return false;
}

export function buildWakeEvent(
  bodyBytes: Uint8Array,
  source: string,
  eventIdHeader: string | null
): any {
  let payload: any = null;
  try {
    payload = JSON.parse(new TextDecoder().decode(bodyBytes));
  } catch {
    payload = null;
  }

  if (
    payload &&
    typeof payload === "object" &&
    payload.v === 0 &&
    typeof payload.event_id === "string"
  ) {
    return payload;
  }

  return {
    v: 0,
    event_id: eventIdHeader || generateEventId(),
    source,
    kind: "webhook",
    content: new TextDecoder().decode(bodyBytes),
    meta: {},
    wake: true,
  };
}

export function formatWakeEvent(event: any): string {
  const source = event.source || "";
  const kind = event.kind || "";
  const content = event.content || "";
  return `<wake source="${source}" kind="${kind}">\n${content}\n</wake>`;
}

export function startIngest(
  ctx: any,
  config: Config,
  activeSessions: Set<string>
) {
  const server = Bun.serve({
    hostname: config.host,
    port: config.port,
    async fetch(req: Request): Promise<Response> {
      const url = new URL(req.url);
      if (url.pathname !== "/") {
        return new Response(JSON.stringify({ error: "not found" }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        });
      }

      if (req.method !== "POST") {
        return new Response(JSON.stringify({ error: "method not allowed" }), {
          status: 405,
          headers: { "Content-Type": "application/json" },
        });
      }

      const bodyBytes = new Uint8Array(await req.arrayBuffer());
      const source = req.headers.get("X-AgentWake-Source") || "";
      const signature = req.headers.get("X-AgentWake-Signature") || "";
      const eventIdHeader = req.headers.get("X-AgentWake-Event-Id");

      const sourceCfg: SourceConfig | undefined = config.sources[source];
      if (!sourceCfg) {
        return new Response(
          JSON.stringify({ error: "unknown source or invalid signature" }),
          { status: 403, headers: { "Content-Type": "application/json" } }
        );
      }

      if (!verifySignature(bodyBytes, sourceCfg.secret, signature)) {
        return new Response(
          JSON.stringify({ error: "unknown source or invalid signature" }),
          { status: 403, headers: { "Content-Type": "application/json" } }
        );
      }

      const event = buildWakeEvent(bodyBytes, source, eventIdHeader);
      const eventId = event.event_id;

      if (isDuplicate(eventId)) {
        return new Response(
          JSON.stringify({ status: "duplicate", event_id: eventId }),
          { status: 202, headers: { "Content-Type": "application/json" } }
        );
      }

      if (activeSessions.size === 0) {
        return new Response(
          JSON.stringify({ status: "queued", event_id: eventId }),
          { status: 202, headers: { "Content-Type": "application/json" } }
        );
      }

      for (const sessionId of activeSessions) {
        try {
          await ctx.client.session.prompt({
            path: { id: sessionId },
            body: {
              noReply: !event.wake,
              parts: [{ type: "text", text: formatWakeEvent(event) }],
            },
          });
        } catch (e: any) {
          log.warn(`Failed to prompt session ${sessionId}: ${e.message}`);
        }
      }

      return new Response(
        JSON.stringify({ status: "queued", event_id: eventId }),
        { status: 202, headers: { "Content-Type": "application/json" } }
      );
    },
  });

  log.info(`ingest listening on ${config.host}:${config.port}`);
}
