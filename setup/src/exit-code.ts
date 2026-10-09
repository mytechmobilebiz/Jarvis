import type { SetupResult } from "./engine.js";

/**
 * How a wizard run maps to a process exit status.
 *
 * Extracted so the interrupted case is testable: a cancelled run must NOT look
 * like a clean success to whatever launched the wizard (a shell script, an
 * installer, CI), and 130 is the conventional "terminated by SIGINT" status.
 */
export function exitCodeFor(result: Pick<SetupResult, "failed" | "cancelled">): number {
  if (result.cancelled === true) return 130;
  return result.failed ? 1 : 0;
}
