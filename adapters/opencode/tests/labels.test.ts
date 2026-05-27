import { afterEach, describe, expect, test } from "bun:test";
import {
  subscribe,
  unsubscribe,
  clearSession,
  sessionsWithLabel,
  labelsForSession,
  _reset,
} from "../src/labels";

afterEach(() => _reset());

describe("labels registry", () => {
  test("subscribe then status reflects the label", () => {
    subscribe("ses_1", "oncall");
    expect(labelsForSession("ses_1")).toEqual(["oncall"]);
    expect(sessionsWithLabel("oncall")).toEqual(["ses_1"]);
  });

  test("subscribe is idempotent", () => {
    subscribe("ses_1", "oncall");
    subscribe("ses_1", "oncall");
    expect(labelsForSession("ses_1")).toEqual(["oncall"]);
    expect(sessionsWithLabel("oncall")).toEqual(["ses_1"]);
  });

  test("two sessions can hold the same label (fan-out)", () => {
    subscribe("ses_1", "oncall");
    subscribe("ses_2", "oncall");
    const matches = sessionsWithLabel("oncall").sort();
    expect(matches).toEqual(["ses_1", "ses_2"]);
  });

  test("one session can hold multiple labels", () => {
    subscribe("ses_1", "oncall");
    subscribe("ses_1", "build-bot");
    expect(labelsForSession("ses_1")).toEqual(["build-bot", "oncall"]);
  });

  test("unsubscribe with a label drops only that label", () => {
    subscribe("ses_1", "oncall");
    subscribe("ses_1", "build-bot");
    const removed = unsubscribe("ses_1", "oncall");
    expect(removed).toEqual(["oncall"]);
    expect(labelsForSession("ses_1")).toEqual(["build-bot"]);
    expect(sessionsWithLabel("oncall")).toEqual([]);
  });

  test("unsubscribe with no label drops every label held by the session", () => {
    subscribe("ses_1", "oncall");
    subscribe("ses_1", "build-bot");
    const removed = unsubscribe("ses_1").sort();
    expect(removed).toEqual(["build-bot", "oncall"]);
    expect(labelsForSession("ses_1")).toEqual([]);
    expect(sessionsWithLabel("oncall")).toEqual([]);
    expect(sessionsWithLabel("build-bot")).toEqual([]);
  });

  test("unsubscribe a label the session does not hold returns empty", () => {
    subscribe("ses_1", "oncall");
    const removed = unsubscribe("ses_1", "build-bot");
    expect(removed).toEqual([]);
    expect(labelsForSession("ses_1")).toEqual(["oncall"]);
  });

  test("clearSession drops everything for that session only", () => {
    subscribe("ses_1", "oncall");
    subscribe("ses_2", "oncall");
    const dropped = clearSession("ses_1");
    expect(dropped).toEqual(["oncall"]);
    expect(labelsForSession("ses_1")).toEqual([]);
    expect(sessionsWithLabel("oncall")).toEqual(["ses_2"]);
  });

  test("removing the last session holding a label cleans up the label index", () => {
    subscribe("ses_1", "oncall");
    unsubscribe("ses_1", "oncall");
    expect(sessionsWithLabel("oncall")).toEqual([]);
  });

  test("empty sessionID or empty label are silently ignored", () => {
    subscribe("", "oncall");
    subscribe("ses_1", "");
    expect(sessionsWithLabel("oncall")).toEqual([]);
    expect(labelsForSession("ses_1")).toEqual([]);
  });

  test("labelsForSession returns labels sorted for stable display", () => {
    subscribe("ses_1", "zebra");
    subscribe("ses_1", "apple");
    subscribe("ses_1", "mango");
    expect(labelsForSession("ses_1")).toEqual(["apple", "mango", "zebra"]);
  });
});
