import fs from "node:fs/promises";
import path from "node:path";
import { pathExists, readJsonIfExists } from "./fs-utils.js";
import type { StepStatus, WizardContext } from "./context.js";

export const SETUP_STATE_VERSION = 1;
export const GATEWAY_INTEGRITY_REVISION = 1;
// v1.6.2: codex file-tools helper
export const PROFILE_MIGRATE_REVISION = 4;

export interface SetupProgress {
  version: number;
  steps: Record<string, StepStatus>;
  /** True once a full run reached the end without a failed step. */
  completed: boolean;
  /** Successful through-the-port gateway verification revision. */
  gatewayIntegrityRev?: number;
  /**
   * Revision of the provider-safety migration this profile has passed. A
   * mismatch re-runs it regardless of step progress or a completed setup —
   * unsafe legacy routes live in profiles that never run setup again.
   */
  providerSafetyRev?: number;
  /**
   * Revision of the live profile-config migration this profile has passed. A
   * mismatch re-runs setup even when every step is done and setup is complete.
   */
  profileMigrateRev?: number;
  /**
   * True when a route was cleared on disk but the gateway that had already
   * LOADED it could not be restarted. The disk looks safe from then on, so
   * without this flag the next launch would find nothing to do and the old
   * route would keep being served until something happened to restart it.
   */
  providerSafetyRestartPending?: boolean;
  /**
   * Write-ahead safety debt: set BEFORE a provider-safety pass runs and cleared
   * only by one that proved the profile safe.
   *
   * The revision stamp alone could not express "we tried and could not tell".
   * An inconclusive pass — a 1.3.x profile whose old engine python is missing,
   * an unreadable config, a crash mid-check — used to leave no durable trace, so
   * setup carried on, installed the vendored engine, retained an already-done
   * Model step and started a gateway against a config nobody had been able to
   * read (integration review r1, finding 1). While this is set, no gateway may
   * be started and setup is forced.
   */
  providerSafetyBlocked?: boolean;
}

export function setupStateFilePath(profileDir: string): string {
  return path.join(profileDir, "setup-state.json");
}

export function setupStatePath(ctx: WizardContext): string {
  return setupStateFilePath(ctx.profileDir);
}

/** Undefined when no readable state of this version exists, which reads as "never ran". */
export async function readSetupProgressAt(profileDir: string): Promise<SetupProgress | undefined> {
  try {
    const stored = await readJsonIfExists<Partial<SetupProgress> | undefined>(
      setupStateFilePath(profileDir),
      undefined,
    );
    if (stored === undefined) return undefined;
    if (stored.version !== SETUP_STATE_VERSION || typeof stored.steps !== "object" || stored.steps === null) {
      return undefined;
    }
    const gatewayIntegrityRev = Number.isInteger(stored.gatewayIntegrityRev)
      ? stored.gatewayIntegrityRev
      : undefined;
    const providerSafetyRev = Number.isInteger(stored.providerSafetyRev)
      ? stored.providerSafetyRev
      : undefined;
    const profileMigrateRev = Number.isInteger(stored.profileMigrateRev)
      ? stored.profileMigrateRev
      : undefined;
    const restartPending = stored.providerSafetyRestartPending === true;
    const safetyBlocked = stored.providerSafetyBlocked === true;
    return {
      version: SETUP_STATE_VERSION,
      steps: { ...stored.steps },
      completed: stored.completed === true,
      ...(gatewayIntegrityRev === undefined ? {} : { gatewayIntegrityRev }),
      ...(providerSafetyRev === undefined ? {} : { providerSafetyRev }),
      ...(profileMigrateRev === undefined ? {} : { profileMigrateRev }),
      ...(restartPending ? { providerSafetyRestartPending: true } : {}),
      ...(safetyBlocked ? { providerSafetyBlocked: true } : {}),
    };
  } catch {
    return undefined;
  }
}

export async function readSetupProgress(ctx: WizardContext): Promise<SetupProgress> {
  return (
    (await readSetupProgressAt(ctx.profileDir)) ?? {
      version: SETUP_STATE_VERSION,
      steps: {},
      completed: false,
    }
  );
}

/**
 * No write happens before `hermes profile install` has created the profile
 * directory: Hermes treats ANY existing target dir as an existing profile and
 * refuses to install over it without --force, so pre-creating the dir here
 * would break every fresh install at the profile step. The write is atomic
 * (temp + rename), preventing a truncated file from becoming
 * indistinguishable from "setup never ran".
 */
export async function writeSetupProgress(ctx: WizardContext, progress: SetupProgress): Promise<void> {
  if (ctx.dryRun) return;
  if (!(await pathExists(ctx.profileDir))) return;
  const target = setupStatePath(ctx);
  const tmpPath = `${target}.tmp`;
  await fs.writeFile(tmpPath, `${JSON.stringify(progress, null, 2)}\n`, "utf8");
  await fs.rename(tmpPath, target);
}

/**
 * Replace a progress object's contents in place.
 *
 * `Object.assign` copies properties but cannot REMOVE one, so a run that
 * adopted a safety pass's state kept any key that pass had deleted — a settled
 * `providerSafetyRestartPending` came straight back and was written to disk
 * again, making the next launch restart the gateway for no reason. Adoption
 * therefore has to be a replacement, not a merge.
 */
export function adoptProgress(target: SetupProgress, source: SetupProgress): SetupProgress {
  for (const key of Object.keys(target) as (keyof SetupProgress)[]) {
    delete target[key];
  }
  Object.assign(target, source);
  return target;
}
