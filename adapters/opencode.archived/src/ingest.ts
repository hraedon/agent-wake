import { Config, SourceConfig } from "./config";
import { verifySignature } from "./gating";

const log = {
  info: (...args: any[]) => console.error("[agent-wake-opencode] INFO", ...args),
  warn: (...args: any[]) => console.error("[agent-wake-opencode] WARN", ...args),
  error: (...args: any[]) => console.error("[agent-wake-opencode] ERR", ...args),
};

function generateEventId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
  const ts = Date.now().toString(36);
  return `${ts}${hex.slice(0, 16)}`;
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
    const claimedSource = payload.source;
    if (claimedSource !== undefined && claimedSource !== source) {
      const err = new Error("source mismatch") as Error & {
        headerSource?: string;
        bodySource?: string;
        sourceMismatch?: boolean;
      };
      err.headerSource = source;
      err.bodySource = String(claimedSource);
      err.sourceMismatch = true;
      throw err;
    }
    if (payload.source === undefined) {
      payload.source = source;
    }
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
  const esc = (s: string) =>
    s
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  return `<wake source="${esc(source)}" kind="${esc(kind)}">\n${esc(content)}\n</wake>`;
}

export function startIngest(
  ctx: any,
  config: Config,
  discoverSessions: () => Promise<string[]>
): any {
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

      let event: any;
      try {
        event = buildWakeEvent(bodyBytes, source, eventIdHeader);
      } catch (e: any) {
        if (e && e.sourceMismatch) {
          log.warn(
            `source mismatch: header=${JSON.stringify(e.headerSource)} body=${JSON.stringify(e.bodySource)}`
          );
          return new Response(JSON.stringify({ error: "source mismatch" }), {
            status: 500,
            headers: { "Content-Type": "application/json" },
          });
        }
        log.error(`buildWakeEvent failed: ${e?.message ?? e}`);
        return new Response(JSON.stringify({ error: "internal error" }), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        });
      }
      const eventId = event.event_id;

      if (isDuplicate(eventId)) {
        return new Response(
          JSON.stringify({ status: "duplicate", event_id: eventId }),
          { status: 202, headers: { "Content-Type": "application/json" } }
        );
      }

      const sessionIds = await discoverSessions();
      if (sessionIds.length === 0) {
        return new Response(
          JSON.stringify({ status: "no_active_sessions", event_id: eventId }),
          { status: 202, headers: { "Content-Type": "application/json" } }
        );
      }

      for (const sessionId of sessionIds) {
        ctx.client.session
          .prompt({
            path: { id: sessionId },
            body: {
              noReply: !event.wake,
              parts: [{ type: "text", text: formatWakeEvent(event) }],
            },
          })
          .then(() => log.info(`delivered to session ${sessionId}`))
          .catch((e: any) =>
            log.warn(`failed to prompt session ${sessionId}: ${e?.message ?? e}`)
          );
      }

      return new Response(
        JSON.stringify({ status: "queued", event_id: eventId, sessions: sessionIds.length }),
        { status: 202, headers: { "Content-Type": "application/json" } }
      );
    },
  });

  log.info(`ingest listening on ${config.host}:${config.port}`);
  return server;
}
