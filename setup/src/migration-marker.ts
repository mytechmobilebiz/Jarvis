import path from "node:path";
import type { WizardContext } from "./context.js";
import { readTextIfExists, writeTextIfChanged } from "./fs-utils.js";

/**
 * "Has this machine been migrated to the 1.4 appliance?"
 *
 * Deliberately its own file rather than a field in setup-state.json (review
 * r1, finding 10). The old decision leaned on the progress file, so a missing
 * or corrupt one — plus an existing profile — opened the dashboard on a 1.3.x
 * machine whose login task was still resurrecting a gateway. A dedicated
 * marker answers exactly one question, and its ABSENCE is the safe default:
 * run the wizard, look for leftovers, decide honestly.
 */
export const MIGRATION_MARKER_FILE = ".jarvis-migration-v1.4.json";
export const MIGRATION_REVISION = 1;

export interface MigrationMarker {
  revision: number;
  /** True when real leftovers were removed, false when there were none. */
  cleaned: boolean;
  at: string;
}

export function migrationMarkerPath(profileDir: string): string {
  return path.join(profileDir, MIGRATION_MARKER_FILE);
}

export async function readMigrationMarker(profileDir: string): Promise<MigrationMarker | undefined> {
  const text = await readTextIfExists(migrationMarkerPath(profileDir));
  if (text === undefined) return undefined;
  try {
    const parsed = JSON.parse(text) as Partial<MigrationMarker>;
    if (parsed.revision !== MIGRATION_REVISION) return undefined;
    return {
      revision: MIGRATION_REVISION,
      cleaned: parsed.cleaned === true,
      at: typeof parsed.at === "string" ? parsed.at : "",
    };
  } catch {
    return undefined;
  }
}

export async function writeMigrationMarker(
  ctx: WizardContext,
  options: { cleaned: boolean; now?: () => Date },
): Promise<void> {
  const now = options.now ?? (() => new Date());
  const marker: MigrationMarker = {
    revision: MIGRATION_REVISION,
    cleaned: options.cleaned,
    at: now().toISOString(),
  };
  await writeTextIfChanged(
    ctx,
    migrationMarkerPath(ctx.profileDir),
    `${JSON.stringify(marker, null, 2)}\n`,
    "migration marker",
  );
}
