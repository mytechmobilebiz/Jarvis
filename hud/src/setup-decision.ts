import { promises as fs } from "node:fs";
import path from "node:path";
import { PROFILE_MIGRATE_REVISION, type SetupProgress } from "@jarvis/setup/engine";

/**
 * Files only a successful `hermes profile install` leaves in the profile
 * directory. Everything else the wizard touches early — .env, operations.md,
 * knowledge/, skills/, cron/, demo/ — is also written by steps that run *after*
 * a failed profile install, so none of them prove setup ever finished.
 */
const INSTALLED_PROFILE_MARKERS = ["SOUL.md", "config.yaml"];

export interface SetupDecisionInput {
  forced: boolean;
  packagedContentMissing: boolean;
  profileDir: string;
  readProgress: (profileDir: string) => Promise<SetupProgress | undefined>;
  exists: (filePath: string) => Promise<boolean>;
  /**
   * Whether this machine has been through the 1.4 migration. Its own marker,
   * not a field in the progress file: a missing or corrupt progress file on a
   * 1.3.x machine used to open the dashboard while a login task was still
   * resurrecting the old gateway (review r1, finding 10).
   */
  migrated: (profileDir: string) => Promise<boolean>;
}

export async function shouldRunSetupFor(input: SetupDecisionInput): Promise<boolean> {
  // Forced beats everything, including missing packaged content: the caller
  // forces setup for reasons that make opening the dashboard UNSAFE (a cleared
  // provider route, a safety check that could not run), and "we could not stage
  // the content" is a reason to stop, never a reason to carry on as normal
  // (integration review r1, finding 2).
  if (input.forced) return true;

  // The migration gate comes next and applies to every shape of existing
  // install — progress file present, absent or corrupt. Until this machine has
  // been migrated, the wizard runs: it is the only path that looks for 1.3.x
  // leftovers before anything starts an engine beside them. It, too, outranks
  // missing content: an unmigrated machine with a damaged install must not
  // quietly open a dashboard beside a 1.3.x gateway.
  if (!(await input.migrated(input.profileDir))) return true;

  if (input.packagedContentMissing) return false;

  const progress = await input.readProgress(input.profileDir);
  if (progress !== undefined) {
    return !progress.completed || progress.profileMigrateRev !== PROFILE_MIGRATE_REVISION;
  }

  // No readable state file: either an install that predates it (pre-v1.3.0) or a
  // modern run that died — or corrupted its state — mid-wizard. Boot the
  // dashboard only for a profile Hermes actually installed *and* keyed.
  return !(await installedProfileLooksComplete(input));
}

async function installedProfileLooksComplete(input: SetupDecisionInput): Promise<boolean> {
  const required = [".env", ...INSTALLED_PROFILE_MARKERS];
  const present = await Promise.all(
    required.map((name) => input.exists(path.join(input.profileDir, name))),
  );
  return present.every(Boolean);
}

export type LaunchMode = "setup" | "dashboard" | "blocked";

export interface LaunchDecisionInput {
  /**
   * Provider safety could not prove this profile safe, or the 1.3.x migration
   * has not run. Both mean: opening normally is the unsafe outcome.
   */
  lifecycleForcesSetup: boolean;
  /** What the ordinary decision (progress file, install markers) asked for. */
  wantsSetup: boolean;
  /** Whether the content the wizard needs is actually on disk. */
  contentStaged: boolean;
}

/**
 * The three ways a launch can end, kept as one pure function because the bug it
 * exists to prevent was a composition bug: a staging failure quietly rewrote a
 * forced setup into a dashboard launch, which then started an engine — the one
 * outcome the force existed to prevent (integration review r1, finding 2).
 *
 * "blocked" is the state v1.3.x had no name for: we cannot run setup (its
 * content is missing) and we may not open normally, so the app opens, says why,
 * and starts nothing.
 */
export function decideLaunchMode(input: LaunchDecisionInput): LaunchMode {
  if (!input.lifecycleForcesSetup && !input.wantsSetup) return "dashboard";
  if (input.contentStaged) return "setup";
  return input.lifecycleForcesSetup ? "blocked" : "dashboard";
}

export async function fileExists(filePath: string): Promise<boolean> {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

/**
 * May the app LEAVE the setup surface and open the HUD?
 *
 * Peter's flow contract (2026-08-14): a completed setup means a working
 * JARVIS, and the Model step is REQUIRED — no path may reach a running HUD
 * while it stands failed. The finish panel already withholds its button, but
 * a button is UI; this is the contract, and the main process enforces it at
 * the one door every path goes through (the setup:launch-hud IPC):
 *
 *  - BOOT HUD / BOOT HUD ANYWAY -> launchHud() -> here.
 *  - a stale or hostile renderer calling the IPC directly -> here.
 *  - resume: a failed required step keeps progress.completed=false, so the
 *    next launch reopens the wizard (shouldRunSetupFor above).
 *  - RUN SETUP AGAIN: rerunAll re-runs every step, required ones included.
 *
 * Optional failures (voice on a machine that cannot speak is `degraded`) do
 * NOT hold the door: that is exactly what "anyway" exists for.
 */
export const REQUIRED_STEP_IDS = ["model"] as const;

export interface FinishedRunLike {
  steps: { id: string; status: string }[];
}

export function mayLeaveSetup(record: FinishedRunLike | undefined): { allowed: boolean; reason?: string } {
  if (record === undefined) {
    // No finished run: the only honest exits from a run in flight are quitting
    // (safe, resumable) or letting it finish. Nothing may boot past it.
    return { allowed: false, reason: "Setup has not finished yet, sir — let it run, or close this window and I will pick up where we left off." };
  }
  // CONNECT SLACK runs only slack/gateway/summary on an install whose Model
  // step already passed; it never plans another Model step.
  if (record.steps.some((step) => step.id === "slack")) return { allowed: true };
  for (const required of REQUIRED_STEP_IDS) {
    // Present AND finished: "done" is verified work, "skipped" is a step that
    // looked and found its work already true (a configured route). A FAILED
    // row refuses — and so does an ABSENT one: a run that ended fatally
    // before reaching a required step has a record with no row at all, and
    // that is not a completed setup either (final verification 2026-08-14).
    const row = record.steps.find((step) => step.id === required);
    if (row === undefined || (row.status !== "done" && row.status !== "skipped")) {
      return {
        allowed: false,
        reason: "There is no office to open yet, sir — I still need my thinking set up. Use RETRY SETUP and we will get it done.",
      };
    }
  }
  return { allowed: true };
}
