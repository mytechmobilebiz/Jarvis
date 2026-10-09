import { spawn } from "node:child_process";
import { execFile } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import type {
  GatewayAuthorization,
  GatewayConnection,
  GatewayControl,
  GatewayVerification,
} from "@jarvis/setup/engine";
import { engineHostEnvironment, waitForGateway, type GatewayReach } from "@jarvis/setup/engine";
import { removeLocalApi, writeLocalApi } from "./local-api.js";
import {
  createGatewaySupervisor,
  mintPairingKey,
  pickFreePort,
  type GatewayState,
  type GatewaySupervisor,
  type SpawnLike,
} from "./gateway-supervisor.js";

/**
 * The HUD's owned gateway. One object holds the whole lifecycle the product
 * used to spread across a login task, a profile .env, a settings.json field and
 * a 671-line port-forensics step.
 */
export interface HudGateway extends GatewayControl {
  stop(): Promise<boolean>;
  status(): GatewayStatus;
  /** Fired whenever the live connection or state changes (renderer broadcast). */
  onChange(listener: (status: GatewayStatus) => void): void;
}

export interface GatewayStatus {
  /** The child's last output lines — the support-facing "why did it die". */
  output?: string[];
  state: GatewayState | "unavailable";
  port: number;
  /** True only after an authenticated round-trip through the port. */
  verified: boolean;
  /** Never carries key material — safe for the renderer and diagnostics. */
  detail?: string;
}

export interface HudGatewayOptions {
  /** Test seam; production reads the profile per spawn. */
  anthropicKeyGuard?: () => NodeJS.ProcessEnv;
  /**
   * The engine's base environment, read per spawn. Production defaults to the
   * wizard's PATH-augmented environment (engineHostEnvironment) so a cold
   * Finder/Dock boot — launchd's bare PATH, no wizard hand-off in this
   * process — still lets the engine find the ChatGPT/Claude apps it thinks
   * with. Tests inject a fixed one.
   */
  baseEnv?: () => NodeJS.ProcessEnv;
  hermesRoot: string;
  /**
   * Where `local-api.json` is published, when this build publishes one. The
   * HUD passes the jarvis profile directory; tests pass a scratch dir. Absent
   * means "publish nothing", which is what the smoke harnesses want.
   */
  profileDir?: string;
  /** Stamped into local-api.json so a reader knows which build wrote it. */
  appVersion?: string;
  profile?: string;
  platform?: NodeJS.Platform;
  spawn?: SpawnLike;
  fetch?: typeof globalThis.fetch;
  exists?: (filePath: string) => boolean;
  pickPort?: () => Promise<number>;
  mintKey?: () => string;
  log?: (message: string) => void;
  killTree?: (pid: number) => Promise<void>;
  /** Test seam for the post-spawn health wait. */
  wait?: typeof waitForGateway;
  waitAttempts?: number;
  /**
   * Runs the engine through the parent-death watchdog. Production passes
   * Electron's own binary + the compiled watchdog script; tests omit it.
   */
  launcher?: { execPath: string; script: string };
  /**
   * Asked before EVERY start and restart, and re-read from durable state each
   * time. Enforcing lifecycle debt here rather than at call sites is the whole
   * fix for integration review r2 finding 1: "BOOT HUD ANYWAY" started an
   * engine straight past a debt every other caller was checking, and any future
   * caller would have had to remember the same rules.
   */
  authorize?: () => Promise<GatewayAuthorization>;
  /** Test seam: crash-restart backoff, so a test need not wait a real second. */
  backoffMs?: number[];
}

/**
 * HOW the HUD starts the engine, in the same two shapes the wizard installs.
 *
 * A wheelhouse install (Windows) has the venv's console script — byte for byte
 * what v1.3 and run 7 spawn. A site install (macOS, v1.4.0) has no console
 * script, so it is the vendored interpreter running the engine's own entry
 * point; its libraries sit in that interpreter's site-packages, so there is no
 * PYTHONPATH to set and the spawn environment is unchanged.
 *
 * The caller appends `-p <profile> gateway run` to `args` either way.
 */
export interface GatewayCommand {
  executable: string;
  args: string[];
}

export function gatewayCommand(
  hermesRoot: string,
  platform: NodeJS.Platform,
  exists: (filePath: string) => boolean = (filePath) => fs.existsSync(filePath),
): GatewayCommand | undefined {
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  const venv = platform === "win32"
    ? pathApi.join(hermesRoot, "hermes-agent", "venv", "Scripts", "hermes.exe")
    : pathApi.join(hermesRoot, "hermes-agent", "venv", "bin", "hermes");
  if (exists(venv)) return { executable: venv, args: [] };
  const python = platform === "win32"
    ? pathApi.join(hermesRoot, "python", "python.exe")
    : pathApi.join(hermesRoot, "python", "bin", "python3");
  if (exists(python)) {
    return { executable: python, args: ["-c", "from hermes_cli.main import main; main()"] };
  }
  return undefined;
}

/** Kept for callers that only need a path to show or to test for. */
export function gatewayExecutablePath(hermesRoot: string, platform: NodeJS.Platform): string {
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  return platform === "win32"
    ? pathApi.join(hermesRoot, "hermes-agent", "venv", "Scripts", "hermes.exe")
    : pathApi.join(hermesRoot, "hermes-agent", "venv", "bin", "hermes");
}


/**
 * Pin the Anthropic credential precedence for the engine we are about to spawn.
 *
 * Read fresh from the profile every time, because the risk appears without any
 * change to our own state: the customer installs Claude Code long after setup,
 * and from that moment the engine would prefer THEIR sign-in over the key they
 * pasted — and rewrite that app's credential file when it refreshes it
 * (upstream #83338). Delivering the key as ANTHROPIC_TOKEN makes the engine
 * resolve ours instead, verified against engine v2026.7.1.
 *
 * Only for an API-KEY-shaped secret. An OAuth-shaped one loses this contest by
 * design in the engine, so mirroring it would be a false promise; provider
 * safety refuses to start the engine in that combination instead.
 */
export function anthropicKeyGuardFor(hermesRoot: string, profile?: string): NodeJS.ProcessEnv {
  const profileDir = path.join(hermesRoot, "profiles", profile ?? "jarvis");
  for (const candidate of [path.join(profileDir, ".env"), path.join(hermesRoot, ".env")]) {
    let contents: string;
    try {
      contents = fs.readFileSync(candidate, "utf8");
    } catch {
      continue;
    }
    const match = /^\s*ANTHROPIC_API_KEY\s*=\s*"?([^"\r\n]+)"?\s*$/m.exec(contents);
    const key = match?.[1]?.trim() ?? "";
    if (key === "") continue;
    // Shape check mirrors setup/src/provider-safety.ts (anthropicSecretShape).
    if (!/^sk-ant-api\d*-/.test(key)) return {};
    return { ANTHROPIC_TOKEN: key };
  }
  return {};
}

export function createHudGateway(options: HudGatewayOptions): HudGateway {
  const platform = options.platform ?? process.platform;
  const exists = options.exists ?? ((filePath: string) => fs.existsSync(filePath));
  const pickPort = options.pickPort ?? (() => pickFreePort());
  const mintKey = options.mintKey ?? mintPairingKey;
  const log = options.log ?? (() => {});
  const wait = options.wait ?? waitForGateway;
  const spawnImpl: SpawnLike = options.spawn
    ?? ((command, args, spawnOptions) => spawn(command, args, spawnOptions));
  const killTree = options.killTree ?? defaultKillTree;
  const listeners: Array<(status: GatewayStatus) => void> = [];

  let supervisor: GatewaySupervisor | undefined;
  let verified = false;
  // A crash cannot clean up after itself, so the first thing a new supervisor
  // does is remove any file the last one left behind — before it can spawn
  // anything a reader might attribute to that file.
  const startupCleanup = options.profileDir === undefined
    ? Promise.resolve()
    : removeLocalApi(options.profileDir).catch(() => {});
  let detail: string | undefined;
  let starting: Promise<GatewayVerification> | undefined;
  /**
   * ONE readiness promise per generation. Every caller — the state listener,
   * ensureRunning(), restart() — awaits the same promise, so a single spawn
   * can never be probed twice (review r3, finding 8).
   */
  const probes = new Map<number, Promise<GatewayReach>>();

  const status = (): GatewayStatus => {
    const output = supervisor?.lastOutput() ?? [];
    return {
      // "blocked" reaches the renderer as itself: a customer told "unavailable"
      // goes looking for a broken install, when the truth is that JARVIS
      // refused to start something it could not vouch for (review r3, P2).
      state: supervisor?.state() ?? "unavailable",
      port: supervisor?.connection().port ?? 0,
      verified,
      ...(detail === undefined ? {} : { detail }),
      ...(output.length === 0 ? {} : { output: output.slice(-10) }),
    };
  };

  const emit = (): void => {
    const snapshot = status();
    for (const listener of listeners) listener(snapshot);
  };

  /**
   * The local API is published ONLY here, and only for a verified engine: this
   * function runs after the authenticated readiness probe, so a file on disk
   * means an engine answered on that port with that key. Every other state
   * removes it — including a crash-restart in progress, where the honest answer
   * to "where is the local API?" is "nowhere, for a moment".
   */
  /**
   * Publications are SERIALIZED. Two fire-and-forget filesystem operations can
   * land in the opposite order to the events that requested them, and the
   * losing order here is the dangerous one: a write completing after a stop
   * leaves a record for an engine that is gone.
   */
  let localApiWork: Promise<void> = startupCleanup;
  const queueLocalApi = (operation: () => Promise<void>): Promise<void> => {
    localApiWork = localApiWork.then(operation, operation).catch(() => {});
    return localApiWork;
  };

  const publish = (state: GatewayVerification["state"]): Promise<void> => {
    if (options.profileDir === undefined) return Promise.resolve();
    const profileDir = options.profileDir;
    const connection = supervisor?.connection();
    const pid = supervisor?.pid() ?? process.pid;
    if (state === "ok" && connection !== undefined) {
      return queueLocalApi(() => writeLocalApi(profileDir, {
        base_url: `http://127.0.0.1:${connection.port}/v1`,
        port: connection.port,
        key: connection.key,
        pid,
        app_version: options.appVersion ?? "unknown",
      }).catch((error: unknown) => {
        log(`gateway: could not publish the local API file (${String(error)})`);
      }));
    }
    return queueLocalApi(() => removeLocalApi(profileDir));
  };

  const verification = (
    state: GatewayVerification["state"],
    text?: string,
    extra: { stopped?: boolean } = {},
  ): GatewayVerification => {
    detail = text;
    verified = state === "ok";
    publish(state);
    emit();
    return {
      state,
      ...(supervisor === undefined ? {} : { connection: supervisor.connection() }),
      ...(text === undefined ? {} : { detail: text }),
      ...(extra.stopped === undefined ? {} : { stopped: extra.stopped }),
    };
  };

  const reachToState = (reach: GatewayReach): GatewayVerification["state"] => {
    if (reach === "ok") return "ok";
    if (reach === "auth-rejected") return "auth-rejected";
    return "unreachable";
  };

  /**
   * One authenticated readiness probe for the CURRENT generation, at most one
   * in flight, publishing only that generation's result. This is what makes an
   * autonomous crash-restart visible to the renderer again.
   */
  const readinessFor = (live: GatewaySupervisor, generation: number): Promise<GatewayReach> => {
    const existing = probes.get(generation);
    if (existing !== undefined) return existing;
    const probe = wait(() => live.connection(), {
      ...(options.fetch === undefined ? {} : { fetch: options.fetch }),
      ...(options.waitAttempts === undefined ? {} : { attempts: options.waitAttempts }),
      abandoned: () => live.state() === "failed" || live.generation() !== generation,
    });
    // Kept until the generation retires rather than dropped on settle, so a
    // second caller for the SAME spawn reuses the answer instead of starting a
    // fresh wait (review r4, the residual double-wait).
    for (const key of probes.keys()) {
      if (key < generation) probes.delete(key);
    }
    probes.set(generation, probe);
    return probe;
  };

  /**
   * The background readiness run for a generation the supervisor started by
   * itself (a crash-restart). Publishes only if that generation is still the
   * live one.
   */
  const probeCurrentGeneration = async (): Promise<void> => {
    const live = supervisor;
    if (live === undefined) return;
    const generation = live.generation();
    const reach = await readinessFor(live, generation);
    if (live.generation() !== generation || supervisor !== live) return;
    if (reach === "ok") verification("ok");
  };

  /**
   * The one gate. Both entry points below pass through it, so there is no path
   * — present or future — that starts an engine while the machine owes safety
   * or migration work.
   */
  const authorized = async (): Promise<GatewayVerification | undefined> => {
    if (options.authorize === undefined) return undefined;
    const decision = await options.authorize().catch(() => ({
      authorized: false,
      reasons: ["unreadable-state" as const],
      detail: "JARVIS could not check whether it is safe to start its engine, so it did not start it.",
    }));
    if (decision.authorized) return undefined;
    log(`gateway: refused to start — ${decision.reasons.join(",")}`);
    return verification("blocked", decision.detail);
  };

  const ensureRunning = async (): Promise<GatewayVerification> => {
    if (starting !== undefined) return starting;
    const attempt = (async (): Promise<GatewayVerification> => {
      const refusal = await authorized();
      if (refusal !== undefined) {
        // Nothing is started, and anything already running is left exactly as
        // it is: this is a refusal, not a stop.
        return refusal;
      }
      const command = gatewayCommand(options.hermesRoot, platform, exists);
      const executable = command?.executable ?? "";
      if (command === undefined) {
        supervisor = undefined;
        return verification(
          "unavailable",
          "The JARVIS engine is not installed yet, so there is nothing to start.",
        );
      }

      // "blocked" belongs in this list: the supervisor holds no child, and the
      // outer authorization above has just succeeded — so the refusal it is
      // remembering is stale. Leaving it out made a debt that cleared after an
      // autonomous refusal unrecoverable without restarting the app
      // (integration review r4, finding 1).
      if (
        supervisor === undefined
        || supervisor.state() === "failed"
        || supervisor.state() === "stopped"
        || supervisor.state() === "blocked"
      ) {
        // Generation numbering restarts with each supervisor, so a probe cached
        // under generation 1 of the PREVIOUS one would answer for generation 1
        // of this one — a stale "unreachable" reported for an engine that is
        // answering perfectly well.
        probes.clear();
        supervisor = createGatewaySupervisor({
          // The SEAM gate: every spawn this supervisor ever performs — including
          // its own crash restarts — passes through it.
          ...(options.authorize === undefined ? {} : { authorize: options.authorize }),
          ...(options.backoffMs === undefined ? {} : { backoffMs: options.backoffMs }),
          executable,
          ...(command.args.length === 0 ? {} : { executableArgs: command.args }),
          anthropicKeyGuard: options.anthropicKeyGuard ?? (() => anthropicKeyGuardFor(options.hermesRoot, options.profile)),
          baseEnv: options.baseEnv ?? (() => engineHostEnvironment(options.hermesRoot, { platform })),
          hermesRoot: options.hermesRoot,
          ...(options.profile === undefined ? {} : { profile: options.profile }),
          platform,
          spawn: spawnImpl,
          key: mintKey(),
          port: await pickPort(),
          nextPort: pickPort,
          killTree,
          log,
          ...(options.launcher === undefined ? {} : { launcher: options.launcher }),
          onState: (next) => {
            // Any state that is not a live, already-verified process invalidates
            // verification — including an autonomous crash-restart, which used
            // to leave `verified` true while the engine was gone (r1, #3).
            if (next !== "running") {
              verified = false;
              // Before the next spawn, not after it: a reader must never see a
              // plausible file for an engine that is being replaced.
              void publish("unavailable");
            }
            emit();
            // EVERY spawn gets exactly one readiness probe, including the ones
            // the supervisor performs by itself after a crash. Without this a
            // recovered engine stayed invisible to the renderer until the whole
            // app was restarted (review r2, finding 4).
            if (next === "running" && !verified) void probeCurrentGeneration();
          },
        });
        await supervisor.start();
      } else if (supervisor.state() === "running" && verified) {
        return verification("ok");
      }

      if (supervisor.state() === "blocked") {
        // The spawn seam refused. Nothing was started; nothing was stopped.
        return verification("blocked", supervisor.blockedDetail(), { stopped: false });
      }

      const live = supervisor;
      // A restart during the wait means the answer describes a process that no
      // longer exists, so the wait is re-run against the new generation. Bounded
      // (never recursive — recursion here would re-enter the `starting` guard
      // and deadlock) so a crash-loop cannot spin this forever.
      let reach: GatewayReach = "unreachable";
      let startGeneration = live.generation();
      for (let round = 0; round < 3; round += 1) {
        const generation = live.generation();
        startGeneration = generation;
        // The SAME promise the state listener awaits — one probe per spawn.
        reach = await readinessFor(live, generation);
        if (live.generation() === generation || live.state() === "failed") break;
      }
      // Never accept a trailing "ok" that belongs to a superseded generation.
      if (reach === "ok" && live.generation() === startGeneration) return verification("ok");
      if (reach === "ok") return verification("unreachable", "The JARVIS engine restarted while it was being checked.");
      if (live.state() === "failed") {
        // The engine explains itself on the way out ("a gateway is already
        // running under launchd for this profile", a bad config, a missing
        // dependency). Passing its own sentence through beats "exit code 1",
        // which is all v1.3.x ever had to work with.
        const said = live.lastOutput().slice(-3).join(" ");
        return verification(
          "crashed",
          said === ""
            ? "The JARVIS engine stopped every time it was started."
            : `The JARVIS engine stopped every time it was started. It said: ${said}`,
        );
      }
      if (reach === "auth-rejected") {
        // Auth is settled, not a warm-up state: the process is up but holds a
        // different key, so leaving it running would keep a gateway we cannot
        // talk to on the profile.
        await live.stop();
        return verification(
          "auth-rejected",
          "The JARVIS engine started but rejected this session's key — a stale API_SERVER_KEY in the profile .env overrides the one JARVIS supplies.",
        );
      }
      // Startup deadline. The child is alive but never bound its port (a config
      // prompt, a wedged import). Leaving it would hold the profile lock while
      // answering nothing, so it is stopped before the failure is reported —
      // and the report says whether that stop was confirmed.
      const stopped = await live.stop();
      return verification(
        "unreachable",
        stopped
          ? "The JARVIS engine started but never answered on its port, so it was stopped."
          : "The JARVIS engine started but never answered on its port, and it did not confirm shutdown. Restart this computer if the problem repeats.",
      );
    })();
    starting = attempt;
    try {
      const result = await attempt;
      // The advertisement is on disk (or gone) BEFORE the caller is told the
      // engine is ready (or not): an integration that reads the moment JARVIS
      // says "ready" must find the file, not a race.
      await localApiWork;
      return result;
    } finally {
      starting = undefined;
    }
  };

  return {
    ensureRunning,
    async restart(): Promise<GatewayVerification> {
      if (supervisor === undefined) return ensureRunning();
      verified = false;
      // STOP → AUTHORIZE → START, in that order, inside the supervisor: the
      // stop always happens (it is never dangerous), and only the spawn is
      // gated. A refusal therefore leaves the machine with no engine rather
      // than the old one still serving a configuration nobody could vouch for.
      const restarted = await supervisor.restart(mintKey());
      // Whatever happens below, the old record is already gone: the state
      // listener removed it when the previous child stopped.
      await localApiWork;
      if (!restarted && supervisor.state() === "blocked") {
        return verification(
          "blocked",
          supervisor.blockedDetail(),
          // The old engine IS gone — supervisor.restart() stops before it asks.
          { stopped: true },
        );
      }
      if (!restarted) {
        return verification(
          "crashed",
          "The previous JARVIS engine did not shut down, so a new one was not started beside it. Restart this computer if the problem repeats.",
        );
      }
      const live = supervisor;
      const generation = live.generation();
      const reach = await readinessFor(live, generation);
      // A restart that was itself superseded must not publish its "ok" over
      // whatever is running now (review r3, finding 8).
      if (reach === "ok" && live.generation() === generation && supervisor === live) {
        const verified = verification("ok");
        await localApiWork;
        return verified;
      }
      if (reach === "ok") {
        return verification("unreachable", "The JARVIS engine restarted again while it was being checked.");
      }
      await live.stop();
      return verification(reachToState(reach), "The JARVIS engine did not come back after the restart.");
    },
    connection(): GatewayConnection | undefined {
      return supervisor?.connection();
    },
    /**
     * Ownership, independent of whether a connection has been published yet:
     * once a supervisor exists it owns whatever it spawned, including a child
     * that is still starting or has just crashed and is being restarted.
     */
    owns(): boolean {
      return supervisor !== undefined;
    },
    /**
     * The processes this HUD is responsible for, for the setup-side legacy scan
     * to exclude. With the watchdog in play this is the watchdog's pid and the
     * engine is its child — so the scan matches on ancestry as well as identity.
     */
    ownedPids(): number[] {
      const pid = supervisor?.pid();
      return [process.pid, ...(pid === undefined ? [] : [pid])];
    },
    /** Resolves false when the engine could not be proven gone. */
    async stop(): Promise<boolean> {
      const gone = (await supervisor?.stop()) ?? true;
      verified = false;
      // Awaited, and behind the same queue: the record must be gone before the
      // caller is told the engine is.
      await publish("unavailable");
      if (!gone) detail = "The JARVIS engine did not confirm shutdown.";
      emit();
      return gone;
    },
    status,
    onChange(listener): void {
      listeners.push(listener);
    },
  };
}

async function defaultKillTree(pid: number): Promise<void> {
  await new Promise<void>((resolve) => {
    execFile("taskkill.exe", ["/PID", String(pid), "/T", "/F"], () => resolve());
  });
}
