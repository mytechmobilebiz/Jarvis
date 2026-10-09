/**
 * The engine's parent-death guard.
 *
 * `detached: false` does NOT couple a child's lifetime to its parent on either
 * platform (Codex review r1, finding 1): if Electron is force-killed, crashes,
 * or is still shutting down when the OS gives up on it, the Hermes child can
 * survive — and a surviving gateway holding the profile lock is precisely the
 * orphan class v1.4.0 exists to delete.
 *
 * The textbook fixes are a Windows Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
 * and prctl(PDEATHSIG) on Linux. Both need native code, and this app may not
 * ship native modules (LANE-B.md hard rule; the same constraint that put
 * node:sqlite in history.ts). So the engine is started through THIS script
 * instead — a tiny supervisor that Electron runs with ELECTRON_RUN_AS_NODE, and
 * that outlives its own parent only long enough to kill the engine:
 *
 *   Electron main ──stdin pipe──► watchdog ──spawn──► hermes gateway run
 *
 * Two independent liveness signals, because either alone has a hole:
 *   - stdin EOF: the parent holds the write end, so ANY parent death closes it
 *     immediately — no polling delay, works even for SIGKILL.
 *   - a pid poll every 2s: covers the case where stdin is inherited, redirected
 *     or duplicated by something else, and is the belt to the pipe's braces.
 *
 * On either signal (or its own SIGTERM) it kills the engine's whole tree and
 * exits. Worst case the engine outlives a hard parent kill by ~2s, which is
 * bounded, observable, and infinitely better than "forever".
 */
import { spawn, type ChildProcess } from "node:child_process";

interface WatchdogOptions {
  command: string;
  args: string[];
  parentPid: number;
  pollMs: number;
  killGraceMs: number;
}

/**
 * The shutdown protocol, on the control pipe rather than in an exit code.
 *
 * An exit-code sentinel was ambiguous: the watchdog also propagates the
 * engine's own exit code, so a real engine exiting 17 collided with the death
 * certificate (review r3, finding 10). A structured line on stdout cannot
 * collide with anything, and it says which pid it is talking about.
 */
export const WATCHDOG_MARKER = "JARVIS-WATCHDOG";

export interface WatchdogAck {
  event: "engine-dead" | "engine-survived";
  pid: number;
}

export function formatAck(ack: WatchdogAck): string {
  return `${WATCHDOG_MARKER} ${JSON.stringify(ack)}`;
}

/** Parses an acknowledgment out of a watchdog output line, if it is one. */
export function parseAck(line: string): WatchdogAck | undefined {
  const trimmed = line.trim();
  if (!trimmed.startsWith(`${WATCHDOG_MARKER} `)) return undefined;
  try {
    const parsed = JSON.parse(trimmed.slice(WATCHDOG_MARKER.length + 1)) as Partial<WatchdogAck>;
    if (parsed.event !== "engine-dead" && parsed.event !== "engine-survived") return undefined;
    return { event: parsed.event, pid: typeof parsed.pid === "number" ? parsed.pid : 0 };
  } catch {
    return undefined;
  }
}

export function parseWatchdogArgv(argv: string[], env: NodeJS.ProcessEnv): WatchdogOptions {
  const [command, ...args] = argv;
  if (command === undefined || command === "") {
    throw new Error("gateway-watchdog: no command given");
  }
  const parentPid = Number(env.JARVIS_WATCHDOG_PARENT_PID ?? "0");
  return {
    command,
    args,
    parentPid: Number.isInteger(parentPid) && parentPid > 0 ? parentPid : 0,
    pollMs: numberFrom(env.JARVIS_WATCHDOG_POLL_MS, 2_000),
    killGraceMs: numberFrom(env.JARVIS_WATCHDOG_GRACE_MS, 5_000),
  };
}

function numberFrom(value: string | undefined, fallback: number): number {
  const parsed = Number(value ?? "");
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

export function parentIsAlive(pid: number, kill: (p: number, signal: 0) => void = process.kill): boolean {
  if (pid <= 0) return true; // No parent to watch: never self-terminate on a guess.
  try {
    kill(pid, 0);
    return true;
  } catch (error) {
    // EPERM means the process exists but belongs to someone else — alive.
    return (error as NodeJS.ErrnoException).code === "EPERM";
  }
}

export interface WatchdogRuntime {
  spawn: typeof spawn;
  /** Liveness probe; injected so tests never depend on real pids. */
  alive?: (pid: number) => boolean;
  platform: NodeJS.Platform;
  now: () => number;
  exit: (code: number) => void;
  log: (line: string) => void;
  /**
   * Writes the structured acknowledgment on the control pipe (stdout) and
   * resolves once the bytes have actually been handed to the OS. Exiting
   * before that flush loses the death certificate on a pipe (review r4).
   */
  ack: (line: string) => Promise<void>;
}

function processAlive(pid: number, runtime: WatchdogRuntime): boolean {
  if (runtime.alive !== undefined) return runtime.alive(pid);
  return parentIsAlive(pid);
}

export function runWatchdog(options: WatchdogOptions, runtime: WatchdogRuntime): { stop: () => void } {
  const child: ChildProcess = runtime.spawn(options.command, options.args, {
    // The engine's own stdout is redirected to OUR stderr: stdout is reserved
    // for the acknowledgment protocol and must carry nothing else.
    stdio: ["ignore", 2, "inherit"],
    windowsHide: true,
    detached: false,
  });

  let terminating = false;
  const timers: NodeJS.Timeout[] = [];

  /**
   * Kills the engine and does not exit until the OS agrees it is gone.
   *
   * The watchdog exiting is what the supervisor watches, so exiting early —
   * on a timer, or because taskkill was merely *launched* — would report a
   * shutdown that did not happen (review r2, finding 2). Every path here ends
   * either in a verified-dead engine or in a loud non-zero exit.
   */
  const killTree = (): void => {
    if (terminating) return;
    terminating = true;
    const pid = child.pid;
    if (pid === undefined) {
      runtime.exit(0);
      return;
    }

    const confirmDeadThenExit = (deadline: number): void => {
      const check = setInterval(() => {
        if (!processAlive(pid, runtime)) {
          clearInterval(check);
          // THE death certificate. The supervisor accepts nothing else — and
          // it must be FLUSHED before this process leaves.
          void runtime.ack(formatAck({ event: "engine-dead", pid })).then(() => runtime.exit(0));
          return;
        }
        if (runtime.now() >= deadline) {
          clearInterval(check);
          runtime.log(`gateway-watchdog: engine pid ${pid} did not die`);
          void runtime.ack(formatAck({ event: "engine-survived", pid })).then(() => runtime.exit(1));
        }
      }, 200);
      timers.push(check);
    };

    if (runtime.platform === "win32") {
      // The watchdog owns taskkill END TO END (review r3, finding 3): the
      // supervisor never runs it, so there is no path where a failed kill is
      // followed by a "stopped" report. Failures are logged and the liveness
      // poll below decides the outcome either way.
      const killer = runtime.spawn("taskkill.exe", ["/PID", String(pid), "/T", "/F"], {
        stdio: "ignore",
        windowsHide: true,
      });
      killer.once("error", (error: Error) => {
        runtime.log(`gateway-watchdog: taskkill could not run: ${error.message}`);
      });
      killer.once("exit", (code: number | null) => {
        if (code !== 0) runtime.log(`gateway-watchdog: taskkill exited ${String(code)}`);
      });
    } else {
      try {
        child.kill("SIGTERM");
      } catch {
        // Already gone.
      }
      timers.push(
        setTimeout(() => {
          try {
            child.kill("SIGKILL");
          } catch {
            // Already gone.
          }
        }, options.killGraceMs),
      );
    }
    // Liveness, not a timer, decides when this process may leave.
    confirmDeadThenExit(runtime.now() + options.killGraceMs * 3);
  };

  child.once("exit", (code, signal) => {
    for (const timer of timers) clearTimeout(timer);
    // The engine died on its own: still a death certificate, so a shutdown
    // racing an ordinary exit is reported honestly rather than as a timeout.
    //
    // A DELIBERATE kill is a success, whatever the corpse's exit code says.
    // POSIX made that automatic (a signalled process reports a signal, not a
    // code); Windows has no signals, so `taskkill /F` leaves exit code 1 and
    // the watchdog was propagating it — reporting failure for every clean
    // shutdown on Windows (CI run 31688337494).
    const exitCode = terminating ? 0 : signal === null ? (code ?? 0) : 0;
    if (child.pid === undefined) {
      runtime.exit(exitCode);
      return;
    }
    void runtime
      .ack(formatAck({ event: "engine-dead", pid: child.pid }))
      .then(() => runtime.exit(exitCode));
  });
  child.once("error", (error) => {
    runtime.log(`gateway-watchdog: could not start the engine: ${error.message}`);
    runtime.exit(127);
  });

  const poll = setInterval(() => {
    if (!parentIsAlive(options.parentPid)) {
      runtime.log("gateway-watchdog: parent process is gone; stopping the engine");
      clearInterval(poll);
      killTree();
    }
  }, options.pollMs);
  timers.push(poll);

  return { stop: killTree };
}

/** True when this module was executed as a script rather than imported. */
function isEntrypoint(): boolean {
  return process.env.JARVIS_WATCHDOG_PARENT_PID !== undefined;
}

if (isEntrypoint()) {
  const options = parseWatchdogArgv(process.argv.slice(2), process.env);
  const runtime: WatchdogRuntime = {
    spawn,
    platform: process.platform,
    now: () => Date.now(),
    exit: (code: number) => process.exit(code),
    log: (line: string) => process.stderr.write(`${line}\n`),
    // stdout is the control pipe: only acknowledgments go there, so the
    // supervisor can trust what it reads. Engine output goes to stderr. The
    // write callback is awaited before exit so the bytes cannot be lost.
    ack: (line: string) =>
      new Promise<void>((resolve) => {
        process.stdout.write(`${line}\n`, () => resolve());
      }),
  };
  const watchdog = runWatchdog(options, runtime);

  // The parent holds the write end of our stdin. Any parent death — including
  // SIGKILL, which no handler can intercept — closes it, and we hear about it
  // immediately instead of waiting for the next poll.
  process.stdin.on("end", () => {
    runtime.log("gateway-watchdog: parent pipe closed; stopping the engine");
    watchdog.stop();
  });
  process.stdin.on("close", () => watchdog.stop());
  process.stdin.on("error", () => watchdog.stop());
  process.stdin.resume();

  for (const signal of ["SIGTERM", "SIGINT", "SIGHUP"] as const) {
    process.on(signal, () => watchdog.stop());
  }
}
