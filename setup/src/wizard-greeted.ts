import fs from "node:fs/promises";
import path from "node:path";

/**
 * The durable "has the wizard ever opened here" marker.
 *
 * The voice greeting distinguishes the FIRST-EVER open (full time-of-day
 * greeting) from every later one (short re-greeting). That distinction used to
 * ride readSetupProgressAt(), which returns `undefined` for missing, corrupt,
 * unreadable AND unsupported state alike — so every failure mode replayed the
 * first-ever greeting, and closing the wizard before any step persisted state
 * replayed it too (Codex mechanism review 2026-08-14, finding 3).
 *
 * This marker lives in the app config dir (jarvisConfigDir), NOT the profile:
 * purging or rebuilding the profile must not make JARVIS re-introduce himself.
 * On ANY doubt the answer is "seen" — repeating the short greeting is mildly
 * dull; repeating the grand first-meeting one is a butler with amnesia.
 */
const MARKER_FILE = "wizard-greeted.json";

export function wizardGreetedPath(configDir: string): string {
  return path.join(configDir, MARKER_FILE);
}

/** True when the wizard has ever opened on this machine; errors count as seen. */
export async function wizardGreeted(configDir: string): Promise<boolean> {
  try {
    await fs.access(wizardGreetedPath(configDir));
    return true;
  } catch (error) {
    // Only a clean "no such file" means first-ever. EACCES, ENOTDIR, EIO —
    // anything that leaves the question open — counts as seen.
    return (error as NodeJS.ErrnoException).code !== "ENOENT";
  }
}

/** Record the open. Best effort: a full disk must not take the wizard down. */
export async function markWizardGreeted(configDir: string): Promise<void> {
  try {
    await fs.mkdir(configDir, { recursive: true });
    await fs.writeFile(
      wizardGreetedPath(configDir),
      `${JSON.stringify({ greetedAt: new Date().toISOString() })}\n`,
      "utf8",
    );
  } catch {
    // Nothing: the worst outcome is one repeated greeting on the next open.
  }
}
