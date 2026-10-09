import type { GatewayConnection } from "./context.js";

/**
 * The only gateway verification v1.4.0 needs.
 *
 * v1.3.x had to answer "is the thing answering port 8642 OUR gateway?" with
 * process forensics (netstat + Win32_Process command lines, 671 lines of it),
 * because the port was fixed and the key lived in a file two processes could
 * disagree about. Both premises are gone: the HUD mints a fresh 32-byte key per
 * session and passes it to its own child through the spawn environment, on a
 * port it picked. So anything that answers /health/detailed with THAT key on
 * THAT port is, by construction, the child we started — identity and liveness
 * in one authenticated round-trip.
 */
export type GatewayReach = "ok" | "auth-rejected" | "unreachable" | "no-key";

export interface ProbeOptions {
  fetch?: typeof globalThis.fetch;
  timeoutMs?: number;
}

export async function probeGateway(
  connection: GatewayConnection | undefined,
  options: ProbeOptions = {},
): Promise<GatewayReach> {
  if (connection === undefined || connection.key === "") return "no-key";
  const fetchImpl = options.fetch ?? globalThis.fetch;
  const timeoutMs = options.timeoutMs ?? 7_000;
  try {
    const response = await fetchImpl(`http://127.0.0.1:${connection.port}/health/detailed`, {
      headers: { authorization: `Bearer ${connection.key}` },
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (response.status === 401 || response.status === 403) return "auth-rejected";
    return response.ok ? "ok" : "unreachable";
  } catch {
    return "unreachable";
  }
}

/**
 * Either a fixed connection or a getter re-read on every attempt. The getter
 * form is what the supervisor passes, so a mid-wait restart is followed rather
 * than probed against a stale port.
 */
export type ConnectionSource =
  | GatewayConnection
  | undefined
  | (() => GatewayConnection | undefined);

function resolveConnection(source: ConnectionSource): GatewayConnection | undefined {
  return typeof source === "function" ? source() : source;
}

export interface WaitOptions extends ProbeOptions {
  attempts?: number;
  delay?: (ms: number) => Promise<void>;
  intervalMs?: number;
  /** Aborts the wait early — the supervised child died and will not answer. */
  abandoned?: () => boolean;
}

/**
 * The engine takes a few seconds to import and bind, so a single probe right
 * after spawn is a false negative. Retries a bounded number of times and stops
 * immediately if the child is already gone.
 */
export async function waitForGateway(
  connection: ConnectionSource,
  options: WaitOptions = {},
): Promise<GatewayReach> {
  const attempts = options.attempts ?? 60;
  const intervalMs = options.intervalMs ?? 1_000;
  const delay = options.delay ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  let last: GatewayReach = "unreachable";
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (options.abandoned?.() === true) return "unreachable";
    // Re-read the connection EVERY attempt: a restart between two probes moves
    // the engine to a new port, and continuing to poll the old one would report
    // "unreachable" about a process that no longer exists (review r1, #3).
    last = await probeGateway(resolveConnection(connection), options);
    // An authenticated 401 is a settled answer, not a warm-up state: the child
    // read a different key than the one we hold, and waiting cannot fix it.
    if (last === "ok" || last === "auth-rejected" || last === "no-key") return last;
    if (attempt < attempts - 1) await delay(intervalMs);
  }
  return last;
}
