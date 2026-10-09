import crypto from "node:crypto";
import net from "node:net";
import { parseAck, type WatchdogAck } from "./gateway-watchdog.js";

/**
 * The gateway as a supervised child of the HUD — v1.4.0's replacement for
 * `hermes gateway install` + a login task / Startup .vbs.
 *
 * What this structurally deletes:
 *  - Stale credentials: the pairing key is minted here and handed to the child
 *    through its spawn environment. There is no file for two processes to
 *    disagree about, so a credential change is just a restart.
 *  - Port squatting: the port is picked free at spawn time, so nothing can
 *    already own it and no second install can collide (bug #28's whole class).
 *  - Orphan gateways: the child dies with the app. Nothing survives to answer
 *    a port after JARVIS quits, and nothing auto-starts behind our back.
 *
 * Engine facts this relies on (verified in the pinned engine source):
 *  - `hermes -p <profile> gateway run` runs in the FOREGROUND and blocks
 *    (hermes_cli/gateway.py:6459 → run_gateway → asyncio.run(start_gateway)).
 *  - The HTTP adapter reads API_SERVER_KEY / API_SERVER_PORT / API_SERVER_HOST
 *    from the process environment (gateway/platforms/api_server.py:848-853) and
 *    only starts at all when a key is present (gateway/config.py:1629-1639).
 *  - Cron runs INSIDE this process (gateway/run.py:20264-20279), so the morning
 *    brief fires as long as the child lives — no second scheduler process.
 *  - The profile `.env` is loaded with override=True (hermes_cli/env_loader.py:
 *    146-148), so a stale API_SERVER_* line in that file would BEAT the spawn
 *    environment. Setup strips those keys; see steps/gateway.ts.
 */
export type GatewayState =
  | "idle"
  | "starting"
  | "running"
  | "restarting"
  | "stopped"
  | "failed"
  /**
   * Refused at the spawn seam: provider-safety or migration debt stands. Not
   * "failed" — nothing went wrong, and nothing was started (review r3, P1).
   */
  | "blocked";

export interface SupervisedChild {
  pid?: number;
  /** The control pipe: closing it is how a shutdown is requested. */
  stdin?: { end(): unknown } | null;
  stdout?: { on(event: "data", listener: (chunk: unknown) => void): unknown } | null;
  stderr?: { on(event: "data", listener: (chunk: unknown) => void): unknown } | null;
  once(event: "exit", listener: (code: number | null, signal: string | null) => void): unknown;
  once(event: "error", listener: (error: Error) => void): unknown;
  kill(signal?: NodeJS.Signals | number): boolean;
}

export type SpawnLike = (
  command: string,
  args: string[],
  options: { env: NodeJS.ProcessEnv; cwd?: string; stdio: "ignore" | "pipe" | Array<"ignore" | "pipe">; windowsHide: boolean; detached: boolean },
) => SupervisedChild;

export interface SupervisorOptions {
  executable: string;
  /**
   * Returns extra environment for the child, recomputed at every spawn. Used to
   * pin the Anthropic credential precedence; see childEnv().
   */
  anthropicKeyGuard?: () => NodeJS.ProcessEnv;
  /** Arguments the executable itself needs, before the engine's own. */
  executableArgs?: string[];
  hermesRoot: string;
  profile?: string;
  spawn: SpawnLike;
  key: string;
  port: number;
  platform?: NodeJS.Platform;
  /**
   * The child's base environment. A function is called at EVERY spawn (crash
   * restarts included) so a PATH augmented after the supervisor was built —
   * or one that must be rebuilt for a cold boot — is what the engine gets.
   */
  baseEnv?: NodeJS.ProcessEnv | (() => NodeJS.ProcessEnv);
  delay?: (ms: number) => Promise<void>;
  now?: () => number;
  log?: (message: string) => void;
  onState?: (state: GatewayState) => void;
  /**
   * Asked at the spawn seam, every spawn, freshly read from disk. See
   * gateway-authorization.ts — stopping is never gated, only starting.
   */
  authorize?: () => Promise<{ authorized: boolean; reasons: readonly string[]; detail: string }>;
  /** Consecutive fast crashes before the supervisor gives up and says so. */
  maxQuickFailures?: number;
  backoffMs?: number[];
  /** Uptime that counts as "it really ran", resetting the crash counter. */
  healthyUptimeMs?: number;
  /** Windows needs a tree kill; injected so tests never shell out. */
  killTree?: (pid: number) => Promise<void>;
  /**
   * Picks a fresh port before each restart. A crash-loop is usually a lost race
   * for the port we reserved, so retrying on the SAME port would loop forever.
   */
  nextPort?: () => Promise<number>;
  /**
   * Runs the engine through the parent-death watchdog (gateway-watchdog.ts)
   * instead of spawning it directly, so a force-killed or crashed HUD cannot
   * leave a gateway behind. Omitted only in unit tests that fake the spawn.
   */
  launcher?: { execPath: string; script: string };
  /** Liveness probe for stop() verification; injected in tests. */
  isAlive?: (pid: number) => boolean;
  /**
   * Must match JARVIS_WATCHDOG_GRACE_MS given to the watchdog: the supervisor
   * derives its own shutdown deadline from it.
   */
  watchdogGraceMs?: number;
}

export interface GatewaySupervisor {
  /** Resolves once the first child has been spawned (lifecycle is serialized). */
  start(): Promise<void>;
  /**
   * Stops the engine and VERIFIES it is gone. Resolves false when the process
   * is still alive at the deadline — the caller must report that honestly
   * rather than printing "stopped" over a surviving gateway.
   */
  stop(): Promise<boolean>;
  /**
   * Increments on every spawn. Readiness probes and verification are keyed to
   * it: a restart between "probe started" and "probe answered" must invalidate
   * the answer, not be believed about the new process (review r1, finding 3).
   */
  generation(): number;
  /** Restarts the child, optionally with a new pairing key. */
  restart(key?: string): Promise<boolean>;
  state(): GatewayState;
  connection(): { port: number; key: string };
  /** Consecutive crash restarts since the last healthy run; for diagnostics. */
  restarts(): number;
  /**
   * The child's last output lines. v1.3.x threw this away, so a gateway that
   * refused to start looked like a bare "exit code 1" to support — the engine
   * prints exactly why (a rival gateway owning the profile, a bad config) and
   * that sentence is worth more than any guess we could make.
   */
  lastOutput(): string[];
  pid(): number | undefined;
  /** Why the last spawn was refused, when state() is "blocked". */
  blockedDetail(): string | undefined;
}

const DEFAULT_BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 16_000, 30_000];

export function mintPairingKey(): string {
  // Same mint as v1.3.x's setup/src/steps/api-server.ts:28 — 32 random bytes,
  // comfortably past the engine's has_usable_secret(min_length=16) guard.
  return crypto.randomBytes(32).toString("base64url");
}

/**
 * Picks a port the OS says is free right now. There is an unavoidable moment
 * between closing this probe socket and the engine binding it; a loser simply
 * crashes at startup and the supervisor retries with a NEW port, which is a far
 * smaller failure surface than a fixed 8642 that another install owns forever.
 */
export async function pickFreePort(
  listen: () => net.Server = () => net.createServer(),
): Promise<number> {
  return new Promise<number>((resolve, reject) => {
    const server = listen();
    server.once("error", reject);
    server.listen({ port: 0, host: "127.0.0.1" }, () => {
      const address = server.address();
      const port = typeof address === "object" && address !== null ? address.port : 0;
      server.close((error) => {
        if (error !== undefined && error !== null) reject(error);
        else if (port === 0) reject(new Error("Unable to reserve a local port for the JARVIS engine."));
        else resolve(port);
      });
    });
  });
}

export function createGatewaySupervisor(options: SupervisorOptions): GatewaySupervisor {
  const platform = options.platform ?? process.platform;
  const profile = options.profile ?? "jarvis";
  const delay = options.delay ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  const now = options.now ?? (() => Date.now());
  const log = options.log ?? (() => {});
  const backoff = options.backoffMs ?? DEFAULT_BACKOFF_MS;
  const maxQuickFailures = options.maxQuickFailures ?? 5;
  const healthyUptimeMs = options.healthyUptimeMs ?? 20_000;
  // The watchdog polls for graceMs*3 before giving up; every supervisor
  // deadline is derived from the SAME number so a configured grace can never
  // outlive the deadline that is supposed to contain it (review r3, #10).
  const watchdogGraceMs = options.watchdogGraceMs ?? 5_000;

  const isAlive = options.isAlive ?? ((pid: number) => {
    try {
      process.kill(pid, 0);
      return true;
    } catch (error) {
      return (error as NodeJS.ErrnoException).code === "EPERM";
    }
  });

  let key = options.key;
  let port = options.port;
  let child: SupervisedChild | undefined;
  let state: GatewayState = "idle";
  let blockedDetail: string | undefined;
  let stopping = false;
  let quickFailures = 0;
  let startedAt = 0;
  let generation = 0;
  let lastExitCode: number | null = null;
  // Bumped by every explicit lifecycle operation. Anything asynchronous that
  // was in flight when it changed is stale by definition and must abandon.
  let epoch = 0;
  // Bumped SYNCHRONOUSLY the moment an explicit stop/restart is requested —
  // before it reaches the queue. A pending autonomous restart captures this
  // value and abandons if it changed, so an explicit operation supersedes a
  // queued crash-restart instead of both spawning (review r3, finding 2).
  let cancelGeneration = 0;
  // Serializes start/stop/restart so two callers can never interleave a kill
  // with a spawn (review r2, finding 3).
  let lifecycle: Promise<unknown> = Promise.resolve();
  let exited: Promise<void> = Promise.resolve();
  const output: string[] = [];
  /** Resolves when the watchdog reports what happened to the engine. */
  let ackResolve: ((ack: WatchdogAck) => void) | undefined;
  let lastAck: WatchdogAck | undefined;
  /**
   * A pipe delivers BYTES, not lines: an acknowledgment can arrive split across
   * two chunks, and parsing each chunk independently turned a perfectly good
   * shutdown into a reported failure (review r4, reproduced). Buffer until a
   * newline, then parse whole lines only.
   */
  let controlBuffer = "";
  const readControl = (chunk: unknown): void => {
    controlBuffer += String(chunk);
    let newline = controlBuffer.indexOf("\n");
    while (newline !== -1) {
      const line = controlBuffer.slice(0, newline);
      controlBuffer = controlBuffer.slice(newline + 1);
      const ack = parseAck(line);
      if (ack !== undefined) {
        lastAck = ack;
        ackResolve?.(ack);
      }
      newline = controlBuffer.indexOf("\n");
    }
    // A pathological producer must not grow this without bound.
    if (controlBuffer.length > 64 * 1024) controlBuffer = controlBuffer.slice(-1024);
  };
  const remember = (chunk: unknown): void => {
    for (const line of String(chunk).split(/\r?\n/)) {
      const text = line.trim();
      if (text === "") continue;
      output.push(text);
      if (output.length > 40) output.shift();
    }
  };

  const setState = (next: GatewayState): void => {
    if (state === next) return;
    state = next;
    options.onState?.(next);
  };

  /**
   * The engine's environment, rebuilt for EVERY spawn — including crash
   * restarts — because the thing it protects can appear at any time.
   *
   * `anthropicKeyGuard` mirrors an API-key-shaped ANTHROPIC_API_KEY into
   * ANTHROPIC_TOKEN. Measured against the pinned engine (v2026.7.1): the
   * engine resolves `~/.claude/.credentials.json` ABOVE the profile's
   * ANTHROPIC_API_KEY, so a customer who pasted a key AND has Claude Code
   * signed in runs on their Claude account — and refreshing it rewrites that
   * app's sign-in file without `subscriptionType`, downgrading a product they
   * bought elsewhere (upstream #83338). Delivered as ANTHROPIC_TOKEN the key
   * wins outright: with the credentials file made unreadable, resolution still
   * succeeded, which is what "not consulted" means.
   *
   * It is computed per spawn, from the profile as it is NOW: the customer who
   * installs Claude Code months after setup is exactly who this is for.
   */
  const childEnv = (): NodeJS.ProcessEnv => ({
    ...(typeof options.baseEnv === "function" ? options.baseEnv() : (options.baseEnv ?? process.env)),
    ...(options.anthropicKeyGuard?.() ?? {}),
    HERMES_HOME: options.hermesRoot,
    HERMES_PROFILE: profile,
    API_SERVER_ENABLED: "true",
    API_SERVER_HOST: "127.0.0.1",
    API_SERVER_PORT: String(port),
    API_SERVER_KEY: key,
    // The engine's own launchers set this; keeping it explicit stops a stray
    // console window on Windows and keeps output line-buffered for our log.
    PYTHONUNBUFFERED: "1",
  });

  /**
   * THE spawn seam. Every path that has ever started an engine goes through
   * this function — explicit start, explicit restart, and the autonomous
   * crash-restart that used to call it without asking anyone (review r3, P1).
   * So the authorization question is asked HERE, freshly read from disk each
   * time: a debt that appears mid-run keeps the child down from the next spawn
   * onwards.
   */
  const spawnChild = async (): Promise<boolean> => {
    if (options.authorize !== undefined) {
      const decision = await options.authorize().catch(() => ({
        authorized: false,
        reasons: ["unreadable-state" as const],
        detail: "JARVIS could not check whether it is safe to start its engine, so it did not start it.",
      }));
      if (!decision.authorized) {
        blockedDetail = decision.detail;
        setState("blocked");
        log(`gateway: refused to start — ${decision.reasons.join(",")}`);
        return false;
      }
    }
    blockedDetail = undefined;
    setState(state === "idle" || state === "stopped" ? "starting" : "restarting");
    startedAt = now();
    generation += 1;
    // --replace: take over the profile's gateway if a stale one is still
    // registered (the engine SIGTERMs it, then SIGKILLs after 10s). NOT
    // --force: when the engine reports a service-managed gateway it is a real
    // 1.3.x leftover, and running a second gateway on the same profile writes
    // the same databases concurrently. Migration removes those registrations;
    // until it has, failing honestly beats corrupting the customer's data.
    // The engine's own arguments, after whatever the COMMAND needs first: a
    // venv console script needs nothing, while a site install's interpreter
    // needs `-c <entry point>` (macOS, v1.4.0). Both end up spawning the same
    // program with the same arguments after that.
    const engineArgs = [...(options.executableArgs ?? []), "-p", profile, "gateway", "run", "--replace"];
    // Through the watchdog, so a force-killed HUD cannot leave a gateway
    // behind (review r1, finding 1). stdin is a PIPE we hold open: its EOF is
    // the watchdog's instant parent-death signal.
    const launched = options.launcher === undefined
      ? { command: options.executable, args: engineArgs, env: childEnv() }
      : {
          command: options.launcher.execPath,
          args: [options.launcher.script, options.executable, ...engineArgs],
          env: {
            ...childEnv(),
            ELECTRON_RUN_AS_NODE: "1",
            JARVIS_WATCHDOG_PARENT_PID: String(process.pid),
            // ONE effective grace value. Both sides defaulting independently
            // meant a configured watchdog grace could silently exceed the
            // supervisor deadline meant to contain it (review r4).
            JARVIS_WATCHDOG_GRACE_MS: String(watchdogGraceMs),
          },
        };
    const spawned = options.spawn(launched.command, launched.args, {
      env: launched.env,
      stdio: options.launcher === undefined ? ["ignore", "pipe", "pipe"] : ["pipe", "pipe", "pipe"],
      windowsHide: true,
      // Never detached: a detached child survives an app crash and becomes the
      // orphan gateway this whole design exists to eliminate.
      detached: false,
    });
    child = spawned;
    output.length = 0;
    lastAck = undefined;
    controlBuffer = "";
    // With a watchdog, stdout is the ACK control pipe and stderr carries the
    // engine's own words. Without one, both are just engine output.
    spawned.stdout?.on("data", options.launcher === undefined ? remember : readControl);
    spawned.stderr?.on("data", remember);
    setState("running");
    log(`gateway: started on port ${port}`);

    const spawnedGeneration = generation;
    lastExitCode = null;
    let settled = false;
    exited = new Promise<void>((resolve) => {
      const finish = (reason: string): void => {
        if (settled) return;
        settled = true;
        // Only the active generation owns `child`; a superseded process exiting
        // must not drop the handle to its live replacement (r2, finding 3).
        if (spawnedGeneration === generation) child = undefined;
        resolve();
        void handleExit(reason, spawnedGeneration);
      };
      spawned.once("error", (error: Error) => {
        finish(`spawn error: ${error.message}`);
      });
      spawned.once("exit", (code: number | null, signal: string | null) => {
        lastExitCode = code;
        finish("exit code=" + String(code) + " signal=" + String(signal));
      });
    });
    return true;
  };

  /**
   * An autonomous (crash) restart. It runs INSIDE the lifecycle queue like
   * every other lifecycle operation, and re-checks epoch AND generation after
   * every await — the review reproduced three children from a stale handler
   * that had passed its epoch check before awaiting nextPort() (r3, #2).
   */
  const handleExit = (reason: string, exitedGeneration: number): void => {
    if (exitedGeneration !== generation) {
      log(`gateway: a superseded engine (generation ${exitedGeneration}) exited; ignoring`);
      return;
    }
    if (stopping) {
      setState("stopped");
      log(`gateway: stopped (${reason})`);
      return;
    }
    const uptime = now() - startedAt;
    if (uptime >= healthyUptimeMs) quickFailures = 0;
    quickFailures += 1;
    if (quickFailures > maxQuickFailures) {
      setState("failed");
      log(`gateway: gave up after ${quickFailures - 1} failed restarts (${reason})`);
      return;
    }
    const wait = backoff[Math.min(quickFailures - 1, backoff.length - 1)];
    setState("restarting");
    log(`gateway: ${reason}; restarting in ${wait}ms (attempt ${quickFailures})`);

    const scheduledUnder = cancelGeneration;
    void serialize(async () => {
      // Everything below re-validates after EVERY await. `serialize` bumped the
      // epoch for this operation, so the guard compares against that value.
      const mine = epoch;
      const stale = (): boolean =>
        stopping
        || epoch !== mine
        || generation !== exitedGeneration
        || cancelGeneration !== scheduledUnder;
      await delay(wait);
      if (stale()) {
        log("gateway: a superseded restart timer expired; ignoring it");
        return;
      }
      if (options.nextPort !== undefined) {
        let nextPort: number | undefined;
        try {
          nextPort = await options.nextPort();
        } catch {
          // A failed port probe is not a reason to leave the customer without
          // an engine; the current port is retried instead.
        }
        if (stale()) {
          log("gateway: the restart was superseded while picking a port; ignoring it");
          return;
        }
        if (nextPort !== undefined) port = nextPort;
      }
      if (stale()) return;
      // A crash restart re-reads the debt like any other spawn: if one appeared
      // while this engine was running, the child stays down and the state says
      // why (review r3, P1).
      await spawnChild();
    });
  };

  /**
   * Stops the engine and PROVES it stopped.
   *
   * With a watchdog, the protocol is: close the control pipe (its stdin), then
   * wait for the watchdog's STRUCTURED acknowledgment that the engine is dead.
   * The supervisor never runs taskkill itself and never treats an exit code,
   * a signal, or a timeout as success (review r3, finding 3) — those are all
   * "we do not know", and "we do not know" is a failure here.
   */
  const killChild = async (): Promise<boolean> => {
    const active = child;
    if (active === undefined) return true;
    const pid = active.pid;

    if (options.launcher !== undefined) {
      const ack = new Promise<WatchdogAck>((resolve) => {
        if (lastAck !== undefined) resolve(lastAck);
        else ackResolve = resolve;
      });
      // Closing the pipe IS the shutdown request; the watchdog owns the kill.
      try {
        active.stdin?.end();
      } catch {
        // Already closed — the watchdog is on its way out anyway.
      }
      // Derived from the watchdog's OWN grace (it polls for graceMs*3), never
      // a hard-coded number that a configured grace could exceed (r3, #10).
      const deadlineMs = watchdogGraceMs * 4;
      const timeout = Symbol("timeout");
      const outcome = await Promise.race([
        ack,
        delay(deadlineMs).then(() => timeout),
      ]);
      ackResolve = undefined;
      if (outcome === timeout) {
        log("gateway: no shutdown acknowledgment from the watchdog");
        // Last resort so a wedged watchdog is not left running — but this is
        // still reported as a FAILED shutdown, because nothing confirmed the
        // engine died.
        try {
          active.kill("SIGKILL");
        } catch {
          // Already gone.
        }
        return false;
      }
      const acknowledged = outcome as WatchdogAck;
      if (acknowledged.event !== "engine-dead") {
        log(`gateway: the watchdog reported the engine survived (pid ${acknowledged.pid})`);
        return false;
      }
      await Promise.race([exited, delay(2_000)]);
      return true;
    }

    // No watchdog (unit tests, and the direct-spawn path): we are killing the
    // engine ourselves, so its own exit is the answer.
    try {
      active.kill("SIGTERM");
    } catch {
      // Already gone between the check and the signal.
    }
    const forced = delay(watchdogGraceMs).then(() => {
      if (child === active) {
        try {
          active.kill("SIGKILL");
        } catch {
          // Already gone.
        }
      }
    });
    await Promise.race([exited, forced]);
    if (child === active) {
      await Promise.race([exited, delay(2_000)]);
      if (child === active && pid !== undefined && isAlive(pid)) return false;
    }
    return true;
  };

  /** Every lifecycle operation runs alone, in order, under a fresh epoch. */
  const serialize = <T>(operation: () => Promise<T>): Promise<T> => {
    const run = lifecycle.then(() => {
      epoch += 1;
      return operation();
    });
    lifecycle = run.catch(() => {});
    return run;
  };

  return {
    async start(): Promise<void> {
      await serialize(async () => {
        if (child !== undefined) return;
        stopping = false;
        quickFailures = 0;
        await spawnChild();
      });
    },
    async stop(): Promise<boolean> {
      cancelGeneration += 1;
      return serialize(async () => {
        stopping = true;
        const gone = await killChild();
        setState("stopped");
        if (!gone) log("gateway: the engine did not confirm shutdown");
        return gone;
      });
    },
    async restart(nextKey?: string): Promise<boolean> {
      cancelGeneration += 1;
      return serialize(async () => {
        stopping = true;
        const gone = await killChild();
        if (!gone) {
          // Spawning a second engine beside a survivor would put two gateways
          // on one profile — the DB-corruption shape the engine itself refuses.
          stopping = false;
          log("gateway: refusing to restart while the previous engine is still alive");
          return false;
        }
        if (nextKey !== undefined) key = nextKey;
        stopping = false;
        quickFailures = 0;
        // stop → authorize → start. The stop above already happened and is
        // never gated; only this spawn is, so a refusal here leaves the machine
        // with NO engine rather than the old one still serving (review r3: the
        // refusal-without-stop is what deadlocked provider safety).
        return spawnChild();
      });
    },
    state: () => state,
    blockedDetail: () => blockedDetail,
    generation: () => generation,
    connection: () => ({ port, key }),
    restarts: () => quickFailures,
    lastOutput: () => [...output],
    pid: () => child?.pid,
  };
}
