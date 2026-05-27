import { Config } from "./config";

export async function postReply(config: Config, source: string, content: string, inReplyTo?: string): Promise<string> {
  const sourceCfg = config.sources[source];
  const callbackUrl = sourceCfg?.callback_url || config.default_callback_url;

  if (!callbackUrl) {
    return "sent (no callback_url configured)";
  }

  const payload = {
    v: 0,
    in_reply_to: inReplyTo || "",
    content,
    meta: {},
  };

  try {
    await fetch(callbackUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (e: any) {
    console.error("[agent-wake-opencode] Reply delivery failed:", e);
    return `reply delivery failed: ${e.message || e}`;
  }
  return "sent";
}
