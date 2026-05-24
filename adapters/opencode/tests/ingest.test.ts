import { describe, expect, test } from "bun:test";

const {
  buildWakeEvent,
  isDuplicate: _isDuplicate,
  formatWakeEvent,
} = require("../src/ingest");

describe("buildWakeEvent", () => {
  test("passes through a valid v0 wake event", () => {
    const body = new TextEncoder().encode(
      JSON.stringify({
        v: 0,
        event_id: "evt-001",
        source: "demo",
        kind: "alert",
        content: "hello",
        meta: {},
        wake: true,
      })
    );
    const event = buildWakeEvent(body, "demo", null);
    expect(event.v).toBe(0);
    expect(event.event_id).toBe("evt-001");
    expect(event.kind).toBe("alert");
  });

  test("wraps arbitrary JSON as a webhook event", () => {
    const body = new TextEncoder().encode(
      JSON.stringify({ action: "opened", number: 42 })
    );
    const event = buildWakeEvent(body, "gh", "evt-wrapped");
    expect(event.v).toBe(0);
    expect(event.event_id).toBe("evt-wrapped");
    expect(event.source).toBe("gh");
    expect(event.kind).toBe("webhook");
    expect(event.content).toBe('{"action":"opened","number":42}');
    expect(event.wake).toBe(true);
    expect(event.meta).toEqual({});
  });

  test("generates an event_id when header is absent for wrapped events", () => {
    const body = new TextEncoder().encode('{"foo":"bar"}');
    const event = buildWakeEvent(body, "demo", null);
    expect(event.event_id).toBeTruthy();
    expect(event.kind).toBe("webhook");
  });

  test("uses header event_id for wrapped events when provided", () => {
    const body = new TextEncoder().encode('{"foo":"bar"}');
    const event = buildWakeEvent(body, "demo", "from-header-123");
    expect(event.event_id).toBe("from-header-123");
  });

  test("handles non-JSON body by wrapping as string content", () => {
    const body = new TextEncoder().encode("plain text body");
    const event = buildWakeEvent(body, "demo", "evt-raw");
    expect(event.kind).toBe("webhook");
    expect(event.content).toBe("plain text body");
    expect(event.event_id).toBe("evt-raw");
  });
});

describe("isDuplicate", () => {
  test("returns false for new event ids", () => {
    expect(_isDuplicate("new-id-unique-test-1")).toBe(false);
  });

  test("returns true for duplicate event ids within the window", () => {
    _isDuplicate("dup-id-test-2");
    expect(_isDuplicate("dup-id-test-2")).toBe(true);
  });

  test("evicts old ids when window exceeds max", () => {
    const evictId = "evict-me-test-3";
    _isDuplicate(evictId);
    for (let i = 0; i < 260; i++) {
      _isDuplicate(`filler-${i}-test-3`);
    }
    expect(_isDuplicate(evictId)).toBe(false);
  });
});

describe("formatWakeEvent", () => {
  test("formats a wake event as an XML-like tag", () => {
    const event = {
      v: 0,
      event_id: "evt-1",
      source: "github-actions",
      kind: "alert",
      content: "build failed on main",
      meta: {},
      wake: true,
    };
    const formatted = formatWakeEvent(event);
    expect(formatted).toContain('source="github-actions"');
    expect(formatted).toContain('kind="alert"');
    expect(formatted).toContain("build failed on main");
    expect(formatted).toContain("<wake");
    expect(formatted).toContain("</wake>");
  });

  test("handles missing fields gracefully", () => {
    const event = {
      v: 0,
      event_id: "e",
      source: "",
      kind: "",
      content: "",
      meta: {},
      wake: false,
    };
    const formatted = formatWakeEvent(event);
    expect(formatted).toContain('source=""');
    expect(formatted).toContain('kind=""');
  });
});