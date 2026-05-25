import { describe, expect, test } from "bun:test";
import { formatWakeEvent } from "../src/ingest";

// Minimal parser: extract the opening tag attrs and the inner text from
// the formatted <wake>...</wake> output. We assert there are no raw
// XML-control characters in attributes or in the inner text — i.e. that
// escaping converted them to entities so the tag is well-formed.

function extractTagAndBody(s: string): {
  openTag: string;
  body: string;
  closeTag: string;
} {
  // The format is: <wake source="..." kind="...">\n<body>\n</wake>
  const match = s.match(/^(<wake [^>]*>)\n([\s\S]*)\n(<\/wake>)$/);
  if (!match) throw new Error(`output does not match <wake>...</wake>: ${s}`);
  return { openTag: match[1], body: match[2], closeTag: match[3] };
}

describe("formatWakeEvent XML escaping", () => {
  test("escapes &, <, >, \", ' in source attribute", () => {
    const out = formatWakeEvent({
      source: `evil&"<>'attr`,
      kind: "alert",
      content: "ok",
    });
    const { openTag, body, closeTag } = extractTagAndBody(out);
    // openTag must close cleanly with exactly one '>' at the end and contain
    // no unescaped '<' or unescaped '"' inside the attribute value.
    expect(closeTag).toBe("</wake>");
    expect((openTag.match(/>/g) || []).length).toBe(1);
    // Extract source attribute value
    const srcMatch = openTag.match(/source="([^"]*)"/);
    expect(srcMatch).not.toBeNull();
    const srcVal = srcMatch![1];
    expect(srcVal).not.toContain("<");
    expect(srcVal).not.toContain(">");
    expect(srcVal).not.toContain('"');
    // Entities present
    expect(srcVal).toContain("&amp;");
    expect(srcVal).toContain("&lt;");
    expect(srcVal).toContain("&gt;");
    expect(srcVal).toContain("&quot;");
    expect(srcVal).toContain("&#39;");
    expect(body).toBe("ok");
  });

  test("escapes special chars in kind attribute", () => {
    const out = formatWakeEvent({
      source: "demo",
      kind: `"><script>`,
      content: "ok",
    });
    const { openTag } = extractTagAndBody(out);
    const kindMatch = openTag.match(/kind="([^"]*)"/);
    expect(kindMatch).not.toBeNull();
    const kindVal = kindMatch![1];
    expect(kindVal).not.toContain("<");
    expect(kindVal).not.toContain(">");
    expect(kindVal).not.toContain('"');
    expect(kindVal).toContain("&lt;script&gt;");
  });

  test("escapes special chars in content body", () => {
    const out = formatWakeEvent({
      source: "demo",
      kind: "alert",
      content: `</wake><wake source="injected" kind="evil">payload`,
    });
    const { openTag, body, closeTag } = extractTagAndBody(out);
    expect(openTag).toBe('<wake source="demo" kind="alert">');
    expect(closeTag).toBe("</wake>");
    // Attacker cannot break out of the wake tag.
    expect(body).not.toContain("</wake>");
    expect(body).not.toContain("<wake");
    expect(body).toContain("&lt;/wake&gt;");
    expect(body).toContain("&lt;wake");
  });

  test("output is well-formed: exactly one opening and one closing wake tag", () => {
    const out = formatWakeEvent({
      source: "demo",
      kind: "alert",
      content: "</wake></wake></wake>",
    });
    const openCount = (out.match(/<wake[ >]/g) || []).length;
    const closeCount = (out.match(/<\/wake>/g) || []).length;
    expect(openCount).toBe(1);
    expect(closeCount).toBe(1);
  });

  test("handles all five XML entities together with backwards-compat fields", () => {
    const out = formatWakeEvent({
      source: "&",
      kind: "<",
      content: `>"'`,
    });
    expect(out).toContain('source="&amp;"');
    expect(out).toContain('kind="&lt;"');
    expect(out).toContain("&gt;&quot;&#39;");
  });
});
