// Untrusted text (titles, chat, AI output) must only reach the page as React text, never parsed as HTML.
// Covers the shadcn components in components/ui too: some (chart, for one) inject HTML and would need a look first.
import { readdirSync, readFileSync } from "node:fs";
import { expect, test } from "vitest";

test("no source file renders HTML from strings", () => {
  const dir = new URL(".", import.meta.url);
  const sources = readdirSync(dir, { recursive: true, encoding: "utf8" }).filter((f) => /\.tsx?$/.test(f) && !f.includes(".test."));
  expect(sources.length).toBeGreaterThan(10);
  for (const f of sources) {
    const code = readFileSync(new URL(f.replaceAll("\\", "/"), dir), "utf8");
    expect(code, f).not.toMatch(/dangerouslySetInnerHTML|innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(/);
  }
});
