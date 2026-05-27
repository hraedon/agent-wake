import { createHmac } from "crypto";

export function verifySignature(
  body: Uint8Array,
  secret: Uint8Array,
  signatureHeader: string
): boolean {
  if (!signatureHeader) return false;

  const expected = signatureHeader.trim();
  if (!expected.startsWith("sha256=")) return false;

  const expectedHex = expected.slice(7);
  if (expectedHex.length !== 64) return false;

  const expectedBytes = new Uint8Array(32);
  for (let i = 0; i < 64; i += 2) {
    const byte = parseInt(expectedHex.substring(i, i + 2), 16);
    if (Number.isNaN(byte)) return false;
    expectedBytes[i / 2] = byte;
  }

  const hmac = createHmac("sha256", secret).update(body).digest();

  if (hmac.length !== expectedBytes.length) return false;
  let result = 0;
  for (let i = 0; i < hmac.length; i++) {
    result |= hmac[i] ^ expectedBytes[i];
  }
  return result === 0;
}
