/**
 * Reply tool: agent calls this to send a message back to the originating
 * event source. The tool sends a `reply` frame to the daemon and awaits
 * the matching `reply_result`. The daemon owns the outbound HTTPS POST.
 */
import { randomUUID } from "node:crypto";
import { ReplyResultBus, sendReplyFrame, type ReplyResult } from "./client";

export interface ReplyArgs {
  source: string;
  content: string;
  in_reply_to?: string;
}

const MAX_REPLY_SIZE = 65536; // 64 KiB

export async function executeReply(args: ReplyArgs): Promise<string> {
  if (!args.source || !args.content) {
    return "missing required fields: source, content";
  }

  let content = args.content;
  if (new TextEncoder().encode(content).length > MAX_REPLY_SIZE) {
    const bytes = new TextEncoder().encode(content);
    content = new TextDecoder("utf-8", { fatal: false }).decode(bytes.slice(0, MAX_REPLY_SIZE));
  }

  const replyId = randomUUID();
  const pending = ReplyResultBus.register(replyId, 35000);

  try {
    sendReplyFrame({
      type: "reply",
      reply_id: replyId,
      source: args.source,
      in_reply_to: args.in_reply_to ?? "",
      content: content,
    });
  } catch (e: any) {
    ReplyResultBus.deliver({
      reply_id: replyId,
      status: "failed",
      error: String(e?.message ?? e),
    });
    return `reply delivery failed: ${e?.message ?? e}`;
  }

  let result: ReplyResult;
  try {
    result = await pending;
  } catch (e: any) {
    return `reply delivery failed: ${e?.message ?? e}`;
  }

  if (result.status === "delivered") return "sent";
  if (result.status === "no_callback") return "sent (no callback_url configured)";
  return `reply delivery failed: ${result.error ?? "unknown error"}`;
}
