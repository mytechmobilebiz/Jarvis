/**
 * Shared by the setup renderer and its tests. Must stay free of Node and
 * Electron imports: setup.html loads the compiled renderer as an ES module from
 * a file:// page, where a bare specifier like "@jarvis/setup/engine" cannot
 * resolve. A near-identical copy lives in setup/src/secret-text.ts for the CLI
 * wizard; keep the two in step.
 */

// Every secret the wizard collects (Anthropic/OpenAI/OpenRouter keys, Slack
// xoxb-/xapp- tokens) is a whitespace-free ASCII token. Copying one out of a
// browser routinely drags along a newline, a non-breaking space or a zero-width
// character, and interior ones survive .trim() all the way into the .env, where
// they surface six steps later as an unexplained 401.
const NOISE = /[\s\u00ad\u00a0\u180e\u200b-\u200f\u2060-\u2064\ufeff]/g;

export function sanitizeSecret(value: string): string {
  return value.replace(NOISE, "");
}

/** Live confirmation that a masked key landed whole, without unmasking it. */
export function secretHint(value: string): string {
  const clean = sanitizeSecret(value);
  if (clean === "") return "";
  const count = `${clean.length} character${clean.length === 1 ? "" : "s"} captured`;
  if (!clean.startsWith("sk-ant-") && !clean.startsWith("sk-proj-")) return count;
  return `${count} · ${clean.slice(0, 12)}…${clean.slice(-4)}`;
}
