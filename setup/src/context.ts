import os from "node:os";
import path from "node:path";
import { resolveHermesRoot } from "./hermes-root.js";
import type { SetupIO } from "./io.js";

export type StepStatus = "done" | "skipped" | "failed";

export interface StepOutcome {
  status: StepStatus;
  notes: string[];
  nextActions?: string[];
  fatal?: boolean;
  /**
   * The step could not do its job, but the PRODUCT works without it — voice,
   * whose own copy promises exactly that ("Typed chat, Slack and the terminal
   * are unaffected"), and a Slack tail that saved tokens but could not resolve
   * the home channel (Slack unreachable), which must rerun rather than be
   * retained as done.
   *
   * Such a step is still reported red and still reruns next time, but it does
   * not make the RUN failed, because a run that never completes reopens the
   * wizard on every launch over something the customer was told did not matter
   * (integration review r1, finding 3).
   */
  degraded?: boolean;
  /**
   * Step-authored telemetry properties, merged into the step_finished event.
   * Anonymous by contract: closed-set codes, booleans and counts only — never
   * key material, file paths, host names, or free-form notes.
   */
  props?: Record<string, unknown>;
}

export interface StepSummary extends StepOutcome {
  id: string;
  title: string;
}

export interface WizardState {
  configuredChannels: Record<string, string>;
  modelConfigured: boolean;
  /** Provider picked on the model screen, recorded even when the run later stalls. */
  providerChoice?: string;
  slackConfigured: boolean;
  slackHomeChannelId?: string;
  /** Set only after the Windows Service step verifies the jarvis gateway through its port. */
  gatewayIntegrityVerified?: boolean;
  /**
   * In-run carrier of installer-gateway cleanup debt (F1 review round 18):
   * the durable record lives on disk, but a failed WRITE must still block
   * this run's Start Engine step — the flag rides the context so the block
   * cannot be lost to a full disk.
   */
  gatewayCleanupDebt?: boolean;
}

/**
 * How the wizard reaches the gateway in v1.4.0: it does not install a service
 * and it does not own a process. The HUD supervises the gateway as its own
 * child and hands the wizard this control surface, so credentials and port
 * have exactly one source of truth (the spawn environment).
 */
export interface GatewayConnection {
  port: number;
  key: string;
}

export type GatewayOutcomeState =
  | "ok"
  | "auth-rejected"
  | "unreachable"
  | "crashed"
  | "unavailable"
  /**
   * Refused before anything was started: outstanding provider-safety or
   * migration debt. Distinct from "unavailable" on purpose — "no engine
   * installed" settles a credential change, whereas "we would not start it"
   * settles nothing (integration review r2, finding 1).
   */
  | "blocked";

export interface GatewayVerification {
  state: GatewayOutcomeState;
  connection?: GatewayConnection;
  /** Customer-facing sentence; never carries key material. */
  detail?: string;
  /**
   * True when this call left NOTHING running — a confirmed stop. Load-bearing
   * for provider safety: "blocked, and the old engine is gone" settles the
   * question a credential change asks, while "blocked, still running" does not.
   */
  stopped?: boolean;
}

export interface GatewayControl {
  /** Starts (or reuses) the supervised child and verifies it through its port. */
  ensureRunning(): Promise<GatewayVerification>;
  /** Restarts the child so a credential/config change is picked up. */
  restart(): Promise<GatewayVerification>;
  connection(): GatewayConnection | undefined;
  /**
   * Stop the engine and prove it stopped. NEVER gated by lifecycle debt:
   * stopping is the safe direction, and provider safety's remediation depends
   * on being able to stop an engine precisely while a debt stands (review r3:
   * the gate used to block its own remediation).
   */
  stop?(): Promise<boolean>;
  /**
   * True once this supervisor owns an engine process — INDEPENDENT of whether
   * it has a live connection.
   *
   * The two questions used to share one answer ("does connection() return
   * something"), which is true today but only by accident of ordering. Ownership
   * is what decides who answers a restart, and a supervisor that owns a child
   * whose port is not up yet must not be mistaken for "no supervisor at all"
   * (integration review r1, finding 6).
   */
  owns?(): boolean;
  /**
   * PIDs this process owns — the engine child and its watchdog. The legacy
   * process scan excludes them: their command lines carry the same
   * "hermes … -p jarvis … gateway" text as the 1.3.x gateway it hunts for.
   */
  ownedPids?(): number[];
}

export interface WizardContext {
  dryRun: boolean;
  packaged: boolean;
  platform?: NodeJS.Platform;
  arch?: NodeJS.Architecture;
  /** The version shown by this shipped setup host. */
  version?: string;
  io: SetupIO;
  /**
   * Fires when the customer closes the wizard (or interrupts the terminal).
   *
   * Long-lived children — the device-code logins above all — must die with the
   * window that started them. Without this, closing setup mid-sign-in left an
   * OAuth poller running that could persist credentials minutes later, for a
   * setup the customer had already walked away from.
   */
  signal?: AbortSignal;
  homeDir: string;
  repoRoot: string;
  hermesRoot: string;
  hermesAgentDir: string;
  profileDir: string;
  state: WizardState;
  summary: StepSummary[];
  /**
   * Root of the bundled offline engine payload, when this build carries one.
   * Undefined in a source checkout, which keeps the network install path for
   * developers and Linux.
   */
  vendorRoot?: string;
  /** Present only when a supervisor (the HUD) is hosting this run. */
  gateway?: GatewayControl;
  /**
   * Lane C (voice): lets a step hand the host something that owns an OS
   * resource — currently the voice self-test's child processes — so quitting
   * mid-run can tear it down. Returns an unregister function. Optional: the
   * CLI wizard supplies none and steps must cope without it.
   */
  registerDisposable?: (disposable: { dispose(): void }) => () => void;
}

export class WizardError extends Error {
  readonly hint: string;
  readonly fatal: boolean;
  /**
   * Diagnostics a step had already earned before it threw (v1.5.1). A step
   * that dies mid-way still knows things support needs — WHICH subscription
   * route was being set up when the config write failed, say — and without
   * this the rendered failure carries none of it (Codex review r5).
   */
  props?: Record<string, string | number | boolean>;

  constructor(message: string, hint: string, options?: { fatal?: boolean }) {
    super(message);
    this.name = "WizardError";
    this.hint = hint;
    this.fatal = options?.fatal ?? false;
  }
}

export function createContext(options: {
  dryRun: boolean;
  packaged?: boolean;
  version?: string;
  io: SetupIO;
  repoRoot: string;
  signal?: AbortSignal;
  vendorRoot?: string;
  gateway?: GatewayControl;
  /** Lane C (voice): see WizardContext.registerDisposable. */
  registerDisposable?: (disposable: { dispose(): void }) => () => void;
}): WizardContext {
  const homeDir = os.homedir();
  const hermesRoot = resolveHermesRoot();
  const hermesAgentDir = path.join(hermesRoot, "hermes-agent");
  const profileDir = path.join(hermesRoot, "profiles", "jarvis");

  return {
    dryRun: options.dryRun,
    packaged: options.packaged ?? false,
    platform: process.platform,
    arch: process.arch,
    version: options.version ?? "unknown",
    io: options.io,
    signal: options.signal,
    ...(options.registerDisposable === undefined ? {} : { registerDisposable: options.registerDisposable }),
    homeDir,
    repoRoot: options.repoRoot,
    hermesRoot,
    hermesAgentDir,
    profileDir,
    ...(options.vendorRoot === undefined ? {} : { vendorRoot: options.vendorRoot }),
    ...(options.gateway === undefined ? {} : { gateway: options.gateway }),
    state: {
      configuredChannels: {},
      modelConfigured: false,
      slackConfigured: false,
    },
    summary: [],
  };
}

export function profilePath(ctx: WizardContext, ...parts: string[]): string {
  return path.join(ctx.profileDir, ...parts);
}

/**
 * Ctrl+C pressed at a raw-mode prompt.
 *
 * In raw mode the terminal hands us `\u0003` as data: the OS never raises
 * SIGINT, so the process-level handler that ends the run cleanly is not
 * involved at all. Rejecting an ordinary WizardError there made a cancelled
 * step look like a failed one — exit 1 at a gate, and at a secret prompt a
 * non-fatal failure that let setup carry on changing the machine. This type
 * carries the intent so the wizard can end the way an OS interrupt would.
 */
export class SetupCancelledError extends Error {
  readonly cancelled = true;

  constructor(message = "Setup was stopped.") {
    super(message);
    this.name = "SetupCancelledError";
  }
}

export function isSetupCancelled(error: unknown): boolean {
  return error instanceof SetupCancelledError
    || (typeof error === "object" && error !== null && (error as { cancelled?: unknown }).cancelled === true);
}
