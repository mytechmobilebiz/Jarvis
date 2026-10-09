/**
 * Typed yes/no parsing for the terminal wizard.
 *
 * The old rule accepted only "y" and "yes"; anything else fell through to the
 * negative branch. A buyer who answered a run-this-step question with "Run it."
 * — the exact phrasing the GUI wizard echoes back at them — was told the step
 * had been skipped as instructed. Everything conversational a person actually
 * types at a [Y/n] prompt is treated as the answer it plainly is, and anything
 * genuinely ambiguous falls back to the prompt's default rather than guessing.
 */

const AFFIRMATIVE = new Set([
  "y",
  "yes",
  "yeah",
  "yep",
  "yup",
  "ya",
  "sure",
  "ok",
  "okay",
  "k",
  "run",
  "run it",
  "runit",
  "do it",
  "go",
  "go ahead",
  "please",
  "yes please",
  "proceed",
  "continue",
  "confirm",
  "affirmative",
  "true",
  "1",
]);

const NEGATIVE = new Set([
  "n",
  "no",
  "nope",
  "nah",
  "skip",
  "skip it",
  "later",
  "not now",
  "cancel",
  "stop",
  "negative",
  "false",
  "0",
]);

/**
 * Lowercases and reduces to words: punctuation becomes a separator so that
 * "yes, run it" and "Run it." normalize the same way a person means them.
 */
export function normalizeAnswer(answer: string): string {
  return answer
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s]+/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * True for yes, false for no, undefined when the reply means neither — the
 * caller then applies the prompt's own default instead of inventing an answer.
 */
export function readYesNo(answer: string): boolean | undefined {
  const normalized = normalizeAnswer(answer);
  if (normalized === "") return undefined;
  if (AFFIRMATIVE.has(normalized)) return true;
  if (NEGATIVE.has(normalized)) return false;

  // "yes, run it" / "no thanks" — the decisive word leads the sentence.
  const first = normalized.split(" ", 1)[0] ?? "";
  if (AFFIRMATIVE.has(first)) return true;
  if (NEGATIVE.has(first)) return false;
  return undefined;
}
