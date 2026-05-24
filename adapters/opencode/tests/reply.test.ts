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

describe("postReply", () => {
  test("returns graceful message when no callback_url configured", () => {
    const config = makeConfig({
      demo: { secret: new TextEncoder().encode("shhh"), callback_url: null },
    });
    const result = postReply(config, "demo", "hello there");
    expect(result).toContain("no callback_url");
  });

  test("returns graceful message when source is unknown and no default", () => {
    const config = makeConfig({});
    const result = postReply(config, "unknown_source", "hello");
    expect(result).toContain("no callback_url");
  });

  test("returns graceful message when source has no callback and default is null", () => {
    const config = makeConfig({
      demo: { secret: new TextEncoder().encode("shhh"), callback_url: null },
    }, null);
    const result = postReply(config, "demo", "any content");
    expect(result).toContain("no callback_url");
  });

  test("returns sent when source callback_url is present", () => {
    const config = makeConfig({
      demo: { secret: new TextEncoder().encode("shhh"), callback_url: "http://127.0.0.1:1/source" },
    });
    const result = postReply(config, "demo", "hello");
    expect(result).toBe("sent");
  });

  test("returns sent when using default_callback_url", () => {
    const config = makeConfig(
      { demo: { secret: new TextEncoder().encode("shhh"), callback_url: null } },
      "http://127.0.0.1:1/default"
    );
    const result = postReply(config, "demo", "hello");
    expect(result).toBe("sent");
  });
});