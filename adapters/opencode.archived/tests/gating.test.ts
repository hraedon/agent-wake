import { describe, expect, test } from "bun:test";
import { createHmac } from "crypto";
import { verifySignature } from "../src/gating";

function hmacSign(secret: string, body: Uint8Array): string {
  return (
    "sha256=" +
    createHmac("sha256", secret).update(body).digest("hex")
  );
}

describe("verifySignature", () => {
  test("accepts valid HMAC-SHA256 signature", () => {
    const body = new TextEncoder().encode('{"hello":"world"}');
    const sig = hmacSign("supersecret", body);
    const secret = new TextEncoder().encode("supersecret");
    expect(verifySignature(body, secret, sig)).toBe(true);
  });

  test("rejects invalid signature", () => {
    const body = new TextEncoder().encode('{"hello":"world"}');
    const secret = new TextEncoder().encode("supersecret");
    expect(verifySignature(body, secret, "sha256=deadbeef")).toBe(false);
  });

  test("rejects missing signature", () => {
    const body = new TextEncoder().encode("x");
    const secret = new TextEncoder().encode("y");
    expect(verifySignature(body, secret, "")).toBe(false);
  });

  test("rejects unknown signature format", () => {
    const body = new TextEncoder().encode("x");
    const secret = new TextEncoder().encode("y");
    expect(verifySignature(body, secret, "md5=abc")).toBe(false);
  });

  test("rejects wrong hex length", () => {
    const body = new TextEncoder().encode("x");
    const secret = new TextEncoder().encode("y");
    expect(verifySignature(body, secret, "sha256=abcd")).toBe(false);
  });

  test("rejects non-hex characters in signature", () => {
    const body = new TextEncoder().encode("x");
    const secret = new TextEncoder().encode("y");
    expect(
      verifySignature(
        body,
        secret,
        "sha256=zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
      )
    ).toBe(false);
  });

  test("rejects signature with wrong secret", () => {
    const body = new TextEncoder().encode('{"hello":"world"}');
    const sig = hmacSign("correct", body);
    const wrongSecret = new TextEncoder().encode("wrong");
    expect(verifySignature(body, wrongSecret, sig)).toBe(false);
  });
});