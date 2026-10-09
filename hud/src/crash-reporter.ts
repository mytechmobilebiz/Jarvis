import { mkdirSync, readdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { recordCrash, type CrashReason } from "@jarvis/setup/engine";

/**
 * Visibility into JARVIS dying, without a server. A main-process hard death
 * (Task Manager kill, OS crash, power loss) cannot be caught from inside the
 * process it kills, so it is detected on the NEXT start: each instance writes a
 * pid-keyed session marker at boot and deletes it on a clean quit. At the next
 * start, any marker whose process is no longer alive belonged to a session that
 * never cleaned up — a dirty shutdown. Pid-keying keeps two concurrent instances
 * from mistaking each other's healthy marker for a crash or deleting it. Renderer
 * and child-process deaths, which the main process CAN observe, are recorded
 * directly. Everything is surfaced only via Copy Diagnostics — there is no crash
 * endpoint yet (a deliberate follow-up).
 */

interface CrashInfo {
  appVersion: string;
  platform: string;
}

const MARKER_PREFIX = "session-active-";
const MARKER_SUFFIX = ".json";

function ownMarkerPath(configDir: string): string {
  return path.join(configDir, `${MARKER_PREFIX}${process.pid}${MARKER_SUFFIX}`);
}

/**
 * The pid lives in the FILENAME, which is authoritative for the liveness check:
 * a marker still being written (partial/corrupt JSON) must not read as a dead
 * process and get a healthy, concurrently-starting instance falsely reported.
 */
function pidFromMarkerName(name: string): number {
  const match = /^session-active-(\d+)\.json$/.exec(name);
  return match === null ? Number.NaN : Number(match[1]);
}

/**
 * Written synchronously on purpose: an async writeFile could still be in flight
 * when a fast quit runs clearSessionMarkerSync, then land afterward and leave a
 * stale marker that reads as a phantom crash next boot. Sync closes that window.
 */
export function markSessionStartSync(configDir: string, info: CrashInfo): void {
  try {
    mkdirSync(configDir, { recursive: true });
    writeFileSync(
      ownMarkerPath(configDir),
      `${JSON.stringify({ pid: process.pid, appVersion: info.appVersion, at: new Date().toISOString() }, null, 2)}\n`,
      "utf8",
    );
  } catch {
    // A machine we cannot write a marker on simply forgoes crash detection.
  }
}

/**
 * Synchronous so it completes on every clean-exit path, including the fast
 * before-quit branch where Electron exits the instant the handler returns and
 * the Windows session-end that fires on shutdown/restart/logout. Removes only
 * THIS instance's marker.
 */
export function clearSessionMarkerSync(configDir: string): void {
  try {
    rmSync(ownMarkerPath(configDir), { force: true });
  } catch {
    // Nothing to clear, or an unwritable dir — the next detect is best-effort.
  }
}

function processAlive(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0); // signal 0 probes existence without touching the process
    return true;
  } catch (error) {
    // EPERM means the process exists but is owned by another user.
    return (error as NodeJS.ErrnoException).code === "EPERM";
  }
}

/**
 * Scan pid-keyed markers; every one whose process is gone is a dirty shutdown —
 * recorded once (attributed to the version stored IN the marker, so an upgrade
 * doesn't blame the new build for the old build's crash) and then removed so it
 * can never be counted twice. Markers of still-running instances are left alone.
 * Returns how many dirty shutdowns were recorded.
 */
export async function detectDirtyShutdown(configDir: string, info: CrashInfo): Promise<number> {
  let names: string[];
  try {
    names = readdirSync(configDir).filter(
      (name) => name.startsWith(MARKER_PREFIX) && name.endsWith(MARKER_SUFFIX),
    );
  } catch {
    return 0; // no config dir yet → first ever launch, nothing to detect
  }

  let recorded = 0;
  for (const name of names) {
    const pid = pidFromMarkerName(name);
    // A live pid (a running instance, or our own not-yet-written marker) owns a
    // healthy marker — leave it be.
    if (Number.isInteger(pid) && pid > 0 && pid !== process.pid && processAlive(pid)) {
      continue;
    }

    // Atomically claim the dead marker by renaming it out from under any other
    // instance scanning the same one: rename is exclusive, so exactly one
    // process records the crash, and the marker leaves the scan set in one step.
    const full = path.join(configDir, name);
    const claim = `${full}.crashed-${process.pid}`;
    try {
      renameSync(full, claim);
    } catch {
      continue; // another instance claimed it first, or it already vanished
    }

    let priorVersion: string | undefined;
    try {
      const parsed = JSON.parse(readFileSync(claim, "utf8")) as { appVersion?: unknown };
      // The version is validated/sanitized by recordCrash; pass it through as-is.
      if (typeof parsed.appVersion === "string") priorVersion = parsed.appVersion;
    } catch {
      // A corrupt marker still proves a session existed and never cleaned up.
    }

    await recordCrash(configDir, {
      reason: "dirty-shutdown",
      appVersion: priorVersion ?? info.appVersion,
      platform: info.platform,
    });
    recorded += 1;
    try {
      rmSync(claim, { force: true });
    } catch {
      // Leftover claim files don't match the marker scan pattern, so a rare
      // failed cleanup is harmless litter, never a double-count.
    }
  }
  return recorded;
}

/**
 * Record a renderer/child death the main process just observed. `detail` is
 * Electron's own closed-set reason code ("crashed" | "oom" | "killed" | …),
 * which is anonymous (and re-validated in recordCrash); the caller must drop
 * "clean-exit" before calling here.
 */
export async function recordProcessGone(
  configDir: string,
  reason: CrashReason,
  detail: string | undefined,
  info: CrashInfo,
): Promise<void> {
  await recordCrash(configDir, { reason, detail, appVersion: info.appVersion, platform: info.platform });
}
