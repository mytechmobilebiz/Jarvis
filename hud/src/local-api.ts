import { promises as fs } from "node:fs";
import path from "node:path";

/**
 * The one supported way to find JARVIS's local API.
 *
 * v1.3.x documented a fixed port and a key a customer copied out of the profile
 * `.env`. v1.4.0 mints both at every spawn — which killed the pairing-drift
 * class, and also killed the recipe an integration customer had written against
 * it. This file is the replacement: JARVIS publishes where it is listening, and
 * an integration reads it.
 *
 * Rules that keep it honest:
 *
 *  - It is written ONLY after the authenticated readiness probe has passed. A
 *    file that exists means an engine answered on that port with that key.
 *  - It is removed before a replacement child is spawned and on every clean
 *    stop, so a reader can never see a stale-but-plausible entry. A brief
 *    absence during a restart is the correct answer to "where is it?", not a
 *    bug.
 *  - JARVIS never READS it. That is the whole point: a second source of truth
 *    for the port and key is exactly the drift this release removed, so
 *    local-api.test.ts fails the build if any reader appears in our code.
 *  - A crash cannot be tidied up by the process that crashed, so the file
 *    carries the supervising process's `pid` for a reader to check, and the
 *    supervisor deletes any leftover before its first spawn.
 */
export const LOCAL_API_FILE = "local-api.json";

export interface LocalApiRecord {
  /** Ready to concatenate an endpoint onto: `${base_url}/chat/completions`. */
  base_url: string;
  port: number;
  key: string;
  /**
   * The JARVIS-side process supervising the engine. If it is gone, the engine
   * is gone: the watchdog exists precisely so the two cannot outlive each
   * other. Readers should treat a dead pid as "no local API".
   */
  pid: number;
  app_version: string;
  written_at: string;
}

export function localApiPath(profileDir: string): string {
  return path.join(profileDir, LOCAL_API_FILE);
}

/**
 * Atomic, owner-only. A reader must never catch a half-written file, and the
 * key inside is a credential: 0600 on POSIX, and on Windows the file inherits
 * the profile directory's ACL (the same protection the profile `.env` beside it
 * has always had — documented in SETUP.md rather than pretended otherwise).
 */
export async function writeLocalApi(
  profileDir: string,
  record: Omit<LocalApiRecord, "written_at"> & { written_at?: string },
): Promise<void> {
  const target = localApiPath(profileDir);
  const temporary = `${target}.tmp-${process.pid}`;
  const payload: LocalApiRecord = {
    base_url: record.base_url,
    port: record.port,
    key: record.key,
    pid: record.pid,
    app_version: record.app_version,
    written_at: record.written_at ?? new Date().toISOString(),
  };
  await fs.writeFile(temporary, `${JSON.stringify(payload, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
  try {
    // writeFile's mode is a CREATE mode: an existing temp file keeps its old
    // permissions, so this is not redundant.
    await fs.chmod(temporary, 0o600);
  } catch {
    // Windows has no POSIX mode bits; the directory ACL governs there.
  }
  await fs.rename(temporary, target);
}

/** Removing it is never an error: "not there" is the state we wanted. */
export async function removeLocalApi(profileDir: string): Promise<void> {
  await fs.rm(localApiPath(profileDir), { force: true });
}
