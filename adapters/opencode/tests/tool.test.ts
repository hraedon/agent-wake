import { describe, expect, test } from "bun:test";
import { postReply } from "../src/reply";
import type { Config, SourceConfig } from "../src/config";

function makeConfig(sources: Record<string, SourceConfig>, defaultCallbackUrl: string | null = null): Config {
  return {
    version: 0,
    host: "127.0.0.1",
    port: 8788,
    sources,
    default_callback_url: defaultCallbackUrl,
  };
}

describe("agent_wake_reply tool logic", () => {
  test("returns graceful message when no callback_url configured", () => {
    const config = makeConfig({
      demo: { secret: new TextEncoder().encode("shhh"), callback_url: null },
    });
    const result = postReply(config, "demo", "hello there");
    expect(result).toContain("no callback_url");
  });

  test("returns sent when source callback_url is present", () => {
    const config = makeConfig({
      demo: { secret: new TextEncoder().encode("shhh"), callback_url: "http://127.0.0.1:1/cb" },
    });
    const result = postReply(config, "demo", "test reply");
    expect(result).toBe("sent");
  });

  test("returns sent when using default_callback_url fallback", () => {
    const config = makeConfig(
      { demo: { secret: new TextEncoder().encode("shhh"), callback_url: null } },
      "http://127.0.0.1:1/default"
    );
    const result = postReply(config, "demo", "fallback reply");
    expect(result).toBe("sent");
  });

  test("posts correct payload to callback server", async () => {
    let receivedBody: any = null;
    const server = Bun.serve({
      hostname: "127.0.0.1",
      port: 0,
      async fetch(req: Request) {
        receivedBody = await req.json();
        return new Response("ok", { status: 200 });
      },
    });

    try {
      const callbackUrl = `http://127.0.0.1:${server.port}/cb`;
      const config = makeConfig({
        demo: {
          secret: new TextEncoder().encode("shhh"),
          callback_url: callbackUrl,
        },
      });

      postReply(config, "demo", "test content", "evt-99");

      await new Promise((resolve) => setTimeout(resolve, 500));
      expect(receivedBody).not.toBeNull();
      expect(receivedBody.v).toBe(0);
      expect(receivedBody.in_reply_to).toBe("evt-99");
      expect(receivedBody.content).toBe("test content");
      expect(receivedBody.meta).toEqual({});
    } finally {
      server.stop();
    }
  });
});