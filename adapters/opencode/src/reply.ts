import { Config } from "./config";

export function postReply(config: Config, source: string, content: string, inReplyTo?: string): string {
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

  const promise = fetch(callbackUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  promise.catch((e) => {
    console.error("[agent-wake-opencode] Reply delivery failed:", e);
  });
  return "sent";
}
