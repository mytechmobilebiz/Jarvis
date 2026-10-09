import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { randomUUID } from "node:crypto";
import type { StepOutcome, WizardContext } from "./context.js";
import { readEnvFile } from "./env-file.js";
import type { SetupHooks, SetupResult, StepProgressEvent } from "./engine.js";

export const TELEMETRY_ENDPOINT = "https://jarvis-agent.tech/api/onboarding-events";
// Plain language (Peter's copy rule, 2026-08-14): the honesty of the
// disclosure stays; the environment-variable incantation leaves the narration.
// The technical opt-out (JARVIS_NO_TELEMETRY=1 in the profile .env) is still
// honoured everywhere below; support hands it out on request, and a real
// settings toggle is on the v1.4.1 list.
export const TELEMETRY_NOTICE =
  "Setup sends me anonymous notes about how it went — which steps worked, on what kind of computer. Never your keys, "
  + "names, or anything you write. If you'd rather it didn't, email jarvis.agent.tech@pm.me and we'll switch it off "
  + "with you in a minute.";

const QUEUE_CAP = 20;
const SEND_TIMEOUT_MS = 4000;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

interface TelemetryEvent {
  seq: number;
  name: "setup_started" | "step_finished" | "setup_finished"
    // The setup→HUD birth (spec/v150/TRANSITION-DESIGN.md §13): mode flags
    // and durations only — no customer content, keys, or free text.
    | "hud_transition_started" | "hud_transition_finished";
  at: string;
  step_id?: string;
  status?: string;
  duration_ms?: number;
  props?: Record<string, unknown>;
}

interface TelemetryBatch {
  install_id: string;
  run_id: string;
  app: {
    version: string;
    packaged: boolean;
    platform: string;
    arch: string;
    os_release: string;
  };
  events: TelemetryEvent[];
}

export interface TelemetryOptions {
  appVersion: string;
  endpoint?: string;
  /** Test seam; production uses global fetch. */
  fetchImpl?: typeof fetch;
  /** Test seam; production derives ~/.config/jarvis (APPDATA\jarvis on Windows). */
  configDir?: string;
  env?: NodeJS.ProcessEnv;
  now?: () => number;
}

export interface Telemetry {
  hooks: SetupHooks;
  /** Resolves when every pending send has settled; never rejects. */
  flush(): Promise<void>;
}

// Same location convention as the hermes-root pointer, so all machine-local
// Jarvis bookkeeping lives in one directory.
export function jarvisConfigDir(
  env: NodeJS.ProcessEnv = process.env,
  overrides: { platform?: NodeJS.Platform; homeDir?: string } = {},
): string {
  const platform = overrides.platform ?? process.platform;
  const homeDir = overrides.homeDir ?? os.homedir();
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  const configRoot = platform === "win32"
    ? env.APPDATA?.trim() || pathApi.join(homeDir, "AppData", "Roaming")
    : pathApi.join(homeDir, ".config");
  return pathApi.join(configRoot, "jarvis");
}

function optedOut(value: string | undefined): boolean {
  const clean = (value ?? "").trim().toLowerCase();
  return clean !== "" && clean !== "0" && clean !== "false";
}

async function readJson<T>(filePath: string): Promise<T | undefined> {
  try {
    return JSON.parse(await fs.readFile(filePath, "utf8")) as T;
  } catch {
    return undefined;
  }
}

/**
 * The process-cached id if one was resolved this run, else undefined. Never
 * touches disk or mints — lets a read-only surface (diagnostics:get) agree with
 * a code the setup host already minted into the cache on an unwritable dir.
 */
export function cachedInstallId(configDir: string): string | undefined {
  return installIdByDir.get(configDir);
}

/** Read-only variant for surfaces that must not create files (dry runs). */
export async function readInstallId(configDir: string): Promise<string | undefined> {
  const stored = await readJson<{ installId?: string }>(path.join(configDir, "telemetry.json"));
  return stored?.installId !== undefined && UUID_PATTERN.test(stored.installId)
    ? stored.installId
    : undefined;
}

// One id per (process, configDir). A writable dir persists to telemetry.json so
// every process agrees; an UNwritable dir can't persist, so without this cache
// each independent caller (the support code, the telemetry batch, diagnostics)
// would mint a different UUID and the support code shown to the customer would
// match no telemetry event (Codex review 2026-08-11, P2). The cache makes the
// id stable within the process even when nothing can be written to disk.
const installIdByDir = new Map<string, string>();

export async function loadInstallId(configDir: string): Promise<string> {
  const cached = installIdByDir.get(configDir);
  if (cached !== undefined) return cached;
  const existing = await readInstallId(configDir);
  if (existing !== undefined) {
    installIdByDir.set(configDir, existing);
    return existing;
  }
  const installId = randomUUID();
  try {
    await fs.mkdir(configDir, { recursive: true });
    await fs.writeFile(path.join(configDir, "telemetry.json"), `${JSON.stringify({ installId }, null, 2)}\n`, "utf8");
  } catch {
    // An unwritable config dir still yields a valid id; the cache keeps every
    // caller in this process on that same id.
  }
  installIdByDir.set(configDir, installId);
  return installId;
}

/**
 * True when this run's telemetry will actually send events: not a dry run and
 * not opted out in either the process env or the profile .env. Callers use this
 * to avoid advertising a support code that would correlate to no server-side
 * event (Codex review 2026-08-11, P2).
 */
export async function telemetryEnabled(
  ctx: { dryRun: boolean; profileDir: string },
  env: NodeJS.ProcessEnv = process.env,
): Promise<boolean> {
  if (ctx.dryRun || optedOut(env.JARVIS_NO_TELEMETRY)) return false;
  const profileEnv = await readEnvFile(path.join(ctx.profileDir, ".env")).catch(() => ({} as Record<string, string>));
  return !optedOut(profileEnv.JARVIS_NO_TELEMETRY);
}

/** One POST, one verdict; failures (network, non-2xx, timeout) are quiet. */
async function postTelemetryBatch(send: typeof fetch, endpoint: string, batch: TelemetryBatch): Promise<boolean> {
  try {
    const response = await send(endpoint, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(batch),
      signal: AbortSignal.timeout(SEND_TIMEOUT_MS),
    });
    return response.ok;
  } catch {
    return false;
  }
}

export interface OutOfBandTelemetryEvent {
  name: TelemetryEvent["name"];
  props?: Record<string, unknown>;
}

export interface OutOfBandTelemetryOptions {
  /** The profile whose .env carries the customer-facing opt-out. */
  profileDir: string;
  dryRun?: boolean;
  packaged?: boolean;
  endpoint?: string;
  /** Test seams; production uses global fetch, process.env and Date.now. */
  fetchImpl?: typeof fetch;
  env?: NodeJS.ProcessEnv;
  now?: () => number;
}

// ONE process-wide serialization per queue FILE (integration review r2 P2):
// the wizard-run flusher (createTelemetry) and the out-of-band transition
// enqueuer share the same telemetry-queue.json, and two separate chains let
// them interleave their read-modify-write — duplicating a batch, or deleting
// one the other had just persisted. Every queue access goes through this.
const queueChains = new Map<string, Promise<void>>();
function withQueueLock(queuePath: string, work: () => Promise<void>): Promise<void> {
  const previous = queueChains.get(queuePath) ?? Promise.resolve();
  const next = previous.then(work, work).catch(() => {});
  queueChains.set(queuePath, next);
  return next;
}

/**
 * Telemetry from outside a wizard run (the setup→HUD transition, §13 of the
 * transition design): same opt-out surfaces, same anonymity contract, same
 * queue file, cap and endpoint as the run batches. PERSISTS FIRST — a quit
 * mid-transition loses nothing — then attempts the same single-POST/keep-
 * queued drain as a run flush. Never throws; telemetry must not affect the
 * moment it measures.
 */
export function enqueueOutOfBandTelemetry(
  configDir: string,
  appVersion: string,
  events: OutOfBandTelemetryEvent[],
  options: OutOfBandTelemetryOptions,
): Promise<void> {
  const work = async (): Promise<void> => {
    const env = options.env ?? process.env;
    if (events.length === 0 || options.dryRun === true || optedOut(env.JARVIS_NO_TELEMETRY)) return;
    const profileEnv = await readEnvFile(path.join(options.profileDir, ".env")).catch(() => ({} as Record<string, string>));
    if (optedOut(profileEnv.JARVIS_NO_TELEMETRY)) return;

    const now = options.now ?? Date.now;
    const send = options.fetchImpl ?? fetch;
    const endpoint = options.endpoint ?? env.JARVIS_TELEMETRY_URL ?? TELEMETRY_ENDPOINT;
    const queuePath = path.join(configDir, "telemetry-queue.json");
    const batch: TelemetryBatch = {
      install_id: await loadInstallId(configDir),
      run_id: randomUUID(),
      app: {
        version: appVersion,
        packaged: options.packaged === true,
        platform: process.platform,
        arch: process.arch,
        os_release: os.release(),
      },
      events: events.map((event, index) => ({
        seq: index + 1,
        at: new Date(now()).toISOString(),
        name: event.name,
        ...(event.props !== undefined && Object.keys(event.props).length > 0 ? { props: event.props } : {}),
      })),
    };

    const writeQueue = async (batches: TelemetryBatch[]): Promise<void> => {
      try {
        await fs.mkdir(configDir, { recursive: true });
        await fs.writeFile(queuePath, `${JSON.stringify(batches.slice(-QUEUE_CAP))}\n`, "utf8");
      } catch {
        // Telemetry must never surface an error into the transition.
      }
    };

    const queued = (await readJson<TelemetryBatch[]>(queuePath)) ?? [];
    const batches = Array.isArray(queued) ? queued : [];
    batches.push(batch);
    // Persist before attempting delivery, exactly like a run flush.
    await writeQueue(batches);

    const failed: TelemetryBatch[] = [];
    for (let index = 0; index < batches.length; index += 1) {
      if (!(await postTelemetryBatch(send, endpoint, batches[index]))) {
        failed.push(...batches.slice(index));
        break;
      }
    }
    if (failed.length === 0) {
      await fs.rm(queuePath, { force: true }).catch(() => {});
    } else {
      await writeQueue(failed);
    }
  };
  return withQueueLock(path.join(configDir, "telemetry-queue.json"), work);
}

/**
 * Anonymous by construction: the install id is a random UUID stored only on the
 * customer's machine, never derived from or linked to their license, email, or
 * sale token. Events carry step outcomes and coarse platform facts — key
 * material and free-form step notes never enter a batch.
 */
export function createTelemetry(ctx: WizardContext, options: TelemetryOptions): Telemetry | undefined {
  const env = options.env ?? process.env;
  if (ctx.dryRun || optedOut(env.JARVIS_NO_TELEMETRY)) return undefined;

  const now = options.now ?? Date.now;
  const send = options.fetchImpl ?? fetch;
  const endpoint = options.endpoint ?? env.JARVIS_TELEMETRY_URL ?? TELEMETRY_ENDPOINT;
  const configDir = options.configDir ?? jarvisConfigDir(env);
  const queuePath = path.join(configDir, "telemetry-queue.json");
  const runId = randomUUID();
  const startedAt = new Map<string, number>();
  const runStarted = now();
  const pending: TelemetryEvent[] = [];
  let seq = 0;
  let started = false;
  let providerReported = false;
  // Cached so an unwritable config dir still yields ONE id per run — separate
  // loadInstallId calls would otherwise mint a fresh UUID per flush.
  let installId: string | undefined;
  const getInstallId = async (): Promise<string> => {
    installId ??= await loadInstallId(configDir);
    return installId;
  };

  const record = (event: Omit<TelemetryEvent, "seq" | "at">): void => {
    seq += 1;
    pending.push({ seq, at: new Date(now()).toISOString(), ...event });
  };

  const postBatch = (batch: TelemetryBatch): Promise<boolean> => postTelemetryBatch(send, endpoint, batch);

  const doFlush = async (): Promise<void> => {
    // The profile .env is the customer-facing opt-out surface; honoring it at
    // send time also silences batches queued before the flag was added.
    const profileEnv = await readEnvFile(path.join(ctx.profileDir, ".env")).catch(() => ({} as Record<string, string>));
    if (optedOut(profileEnv.JARVIS_NO_TELEMETRY)) {
      pending.length = 0;
      await fs.rm(queuePath, { force: true }).catch(() => {});
      return;
    }

    const writeQueue = async (batches: TelemetryBatch[]): Promise<void> => {
      try {
        await fs.mkdir(configDir, { recursive: true });
        await fs.writeFile(queuePath, `${JSON.stringify(batches.slice(-QUEUE_CAP))}\n`, "utf8");
      } catch {
        // Telemetry must never surface an error into setup.
      }
    };

    // An interrupted model step never fires stepFinished, so a chosen provider
    // would vanish with the abandoned run — exactly the signal this telemetry
    // exists for. Stamp it onto the last unsent event instead.
    if (!providerReported && ctx.state.providerChoice !== undefined && pending.length > 0) {
      const last = pending[pending.length - 1];
      last.props = { ...last.props, provider: ctx.state.providerChoice };
      providerReported = true;
    }

    const queued = (await readJson<TelemetryBatch[]>(queuePath)) ?? [];
    const batches = Array.isArray(queued) ? queued : [];
    if (pending.length > 0) {
      batches.push({
        install_id: await getInstallId(),
        run_id: runId,
        app: {
          version: options.appVersion,
          packaged: ctx.packaged,
          platform: process.platform,
          arch: process.arch,
          os_release: os.release(),
        },
        events: pending.splice(0, pending.length),
      });
    }
    if (batches.length === 0) return;

    // Persist before attempting delivery: a window closed or process killed
    // mid-send must not lose events — the next run drains the queue instead.
    await writeQueue(batches);

    // One failed POST means the endpoint is unreachable; keep the rest queued
    // instead of eating a timeout per batch — a full queue would otherwise
    // hold the CLI open for minutes after setup finishes.
    const failed: TelemetryBatch[] = [];
    for (let index = 0; index < batches.length; index += 1) {
      if (!(await postBatch(batches[index]))) {
        failed.push(...batches.slice(index));
        break;
      }
    }
    if (failed.length === 0) {
      await fs.rm(queuePath, { force: true }).catch(() => {});
    } else {
      await writeQueue(failed);
    }
  };

  // The shared per-file lock (r2 P2), not a private chain: an out-of-band
  // transition enqueue racing this flush would otherwise interleave on the
  // same queue file.
  const flush = (): Promise<void> => withQueueLock(queuePath, doFlush);

  const hooks: SetupHooks = {
    stepStarted(event: StepProgressEvent): void {
      startedAt.set(event.id, now());
      if (!started) {
        started = true;
        record({ name: "setup_started", props: { resumed: event.previousStatus !== undefined } });
      }
    },
    stepFinished(event: StepProgressEvent & { outcome: StepOutcome }): void {
      const begun = startedAt.get(event.id);
      // Steps opt in to their own diagnostics (see StepOutcome.props) — this is
      // how a failed Hermes pin becomes visible without a support round-trip.
      const props: Record<string, unknown> = { ...event.outcome.props };
      if (event.retained === true) props.retained = true;
      if (event.id === "model") {
        if (ctx.state.providerChoice !== undefined) {
          props.provider = ctx.state.providerChoice;
          providerReported = true;
        }
        props.model_verified = ctx.state.modelConfigured;
      }
      record({
        name: "step_finished",
        step_id: event.id,
        status: event.outcome.status,
        duration_ms: begun === undefined ? undefined : Math.max(0, now() - begun),
        ...(Object.keys(props).length > 0 ? { props } : {}),
      });
      // The provider screen is the funnel point that matters most; sending here
      // captures runs abandoned between choosing a provider and finishing.
      if (event.id === "model") void flush();
    },
    finished(result: SetupResult): void {
      const steps = Object.fromEntries(result.summary.map((step) => [step.id, step.status]));
      record({
        name: "setup_finished",
        status: result.failed ? "failed" : "done",
        duration_ms: Math.max(0, now() - runStarted),
        props: {
          failed: result.failed,
          ...(ctx.state.providerChoice !== undefined ? { provider: ctx.state.providerChoice } : {}),
          model_configured: ctx.state.modelConfigured,
          slack_configured: ctx.state.slackConfigured || steps.slack === "done",
          steps,
        },
      });
      // No flush here: entry points await flush() right after runSetup, and a
      // second chained attempt would retry a just-failed queue for nothing.
    },
  };

  return { hooks, flush };
}

export interface TelemetryWiring {
  hooks: SetupHooks;
  /**
   * Resolves once every recorded batch is sent or queued to disk. Entry points
   * must await this after runSetup — the process (or Electron shutdown) will
   * not wait for a fire-and-forget send, and the final batch would be lost.
   */
  flush: () => Promise<void>;
}

/**
 * Wraps entry-point hooks with telemetry recording and prints the disclosure
 * line. When telemetry is off (dry run or opt-out) the hooks pass through
 * unchanged and flush is a no-op.
 */
export function withTelemetry(
  ctx: WizardContext,
  hooks: SetupHooks,
  options: TelemetryOptions,
): TelemetryWiring {
  const telemetry = createTelemetry(ctx, options);
  if (telemetry === undefined) return { hooks, flush: async () => {} };
  ctx.io.note(TELEMETRY_NOTICE);
  return {
    flush: telemetry.flush,
    hooks: {
      stepStarted(event) {
        telemetry.hooks.stepStarted?.(event);
        hooks.stepStarted?.(event);
      },
      stepFinished(event) {
        telemetry.hooks.stepFinished?.(event);
        hooks.stepFinished?.(event);
      },
      finished(result) {
        telemetry.hooks.finished?.(result);
        hooks.finished?.(result);
      },
    },
  };
}
