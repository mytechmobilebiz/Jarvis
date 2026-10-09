/**
 * Device-code sign-in output parsing.
 *
 * Both sign-in flows JARVIS drives are RFC 8628 device flows run BY THE ENGINE
 * — JARVIS never talks to a portal itself and never handles a token. The engine
 * prints the verification URL and the user code to stdout and then blocks,
 * polling. Setup reads those two values back out of the child's output so a
 * graphical frontend can show them (and open the browser) instead of asking a
 * non-technical buyer to read a terminal.
 *
 * The two printed shapes, verified against the pinned engine (v2026.7.1,
 * checkout at ~/.hermes/hermes-agent):
 *
 *   Nous  — hermes_cli/auth.py:7727-7731 (`_nous_device_code_login`)
 *     To continue:
 *       1. Open: https://portal.nousresearch.com/device?user_code=ABCD-1234
 *       2. If prompted, enter code: ABCD-1234
 *
 *   Codex — hermes_cli/auth.py:7114-7120 (`_codex_device_code_login`)
 *     To continue, follow these steps:
 *       1. Open this URL in your browser:
 *          https://auth.openai.com/codex/device
 *       2. Enter this code:
 *          ABCD-1234
 *
 * Note the Nous flow prints a `Portal: <base url>` banner line BEFORE the
 * verification URL (auth.py:7701), so "first URL in the output" is the wrong
 * rule — the label is what identifies the line.
 */

export interface DeviceCodeInfo {
  /** The verification URL the customer must open. */
  url: string;
  /** The short code the customer types into that page. */
  code: string;
}

/** ESC [ … m — the engine colours the URL and the code (auth.py:7116-7119). */
const ANSI = /\u001B\[[0-9;]*[A-Za-z]/g;

/** Colour codes make the engine's output pretty in a terminal and unparseable here. */
export function stripAnsi(text: string): string {
  return text.replace(ANSI, "");
}

const URL_IN_LINE = /(https?:\/\/[^\s"'<>]+)/;
/** Portal user codes are short, upper-case and hyphenated; a sentence is not a code. */
const CODE_SHAPE = /^[A-Za-z0-9]{4,8}-[A-Za-z0-9]{4,8}$/;

/**
 * Pull the verification URL and user code out of engine sign-in output.
 * Returns undefined until BOTH are present: a half-parsed prompt shown to a
 * customer is worse than no prompt, because they cannot finish the flow.
 */
export function parseDeviceCode(text: string): DeviceCodeInfo | undefined {
  const lines = stripAnsi(text).split(/\r?\n/);
  let url: string | undefined;
  let code: string | undefined;

  const nextNonEmpty = (from: number): string | undefined => {
    for (let index = from; index < lines.length; index += 1) {
      const candidate = lines[index].trim();
      if (candidate !== "") return candidate;
    }
    return undefined;
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];

    if (url === undefined && /\bopen\b/i.test(line)) {
      url = URL_IN_LINE.exec(line)?.[1] ?? URL_IN_LINE.exec(nextNonEmpty(index + 1) ?? "")?.[1];
    }

    if (code === undefined && /\b(?:enter|code)\b.*\bcode\b/i.test(line)) {
      const colon = line.lastIndexOf(":");
      const tail = colon === -1 ? "" : line.slice(colon + 1).trim();
      const candidate = tail !== "" ? tail : (nextNonEmpty(index + 1) ?? "");
      if (CODE_SHAPE.test(candidate)) code = candidate;
    }
  }

  return url !== undefined && code !== undefined ? { url, code } : undefined;
}

/**
 * Why a sign-in ended, as a CLOSED SET for telemetry. Never stderr, never a
 * portal message: those carry account identifiers and home-directory paths.
 */
export type SignInResult =
  | "verified"
  | "dry-run"
  | "cancelled"
  | "timeout"
  | "denied"
  | "no-plan"
  | "network"
  | "permission"
  | "engine-missing"
  | "cli-missing"
  | "cli-signed-out"
  | "failed";

/**
 * Classify a failed sign-in from the engine's own words.
 *
 * Order matters, and it is the whole point. A previous version matched a bare
 * /timeout/ and told customers with a flaky network that "the sign-in page
 * timed out", and matched a bare /denied/ so a filesystem "Permission denied"
 * became "the sign-in was refused" — confident, specific, and wrong. Transport
 * and OS failures are therefore recognised BEFORE the OAuth vocabulary, and
 * only the protocol's own error codes are read as protocol outcomes
 * (RFC 8628 `access_denied` / `expired_token`, and the engine's own
 * "Timed out waiting for device authorization", auth.py:4557).
 *
 * Anything unrecognised is "failed": an honest shrug beats a wrong diagnosis.
 */
export function classifySignInFailure(status: number, output: string): SignInResult {
  const text = stripAnsi(output).toLowerCase();

  // The engine exits 130 on Ctrl+C in both device flows (auth.py:7148).
  if (status === 130 || /login cancelled|keyboardinterrupt/.test(text)) return "cancelled";
  // Node reports a missing executable as 127 before any engine output exists.
  if (status === 127 || /command not found|no such file or directory.*hermes|enoent/.test(text)) {
    return "engine-missing";
  }
  if (/permission denied|eacces|operation not permitted/.test(text)) return "permission";
  if (
    /connecterror|connecttimeout|connection (refused|reset|aborted)|network is unreachable/.test(text)
    || /name or service not known|temporary failure in name resolution|nodename nor servname|getaddrinfo/.test(text)
    || /ssl|certificate verify failed|read timed out|proxy/.test(text)
  ) {
    return "network";
  }
  if (/subscription_required|subscribe here|no paid access|insufficient credits/.test(text)) return "no-plan";
  if (/\baccess_denied\b|authorization_declined|\buser denied\b|declined the request/.test(text)) return "denied";
  if (/\bexpired_token\b|timed out waiting for device authorization|login timed out|code has expired/.test(text)) {
    return "timeout";
  }
  return "failed";
}
