import fs from "node:fs/promises";
import path from "node:path";
import { note } from "./ui.js";
import type { WizardContext } from "./context.js";

export async function pathExists(filePath: string): Promise<boolean> {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

/**
 * "Not there" and "I was not allowed to look" are different answers.
 *
 * `pathExists` flattens both to false, which is right for the copy helpers
 * below and WRONG anywhere a missing file means "nothing to worry about": a
 * permission error would then read as proof of absence. Provider safety needs
 * the distinction — a config it cannot read is exactly the state in which the
 * unsafe route lives (integration review r2, finding 2).
 */
export type PathStatus = "present" | "absent" | "inaccessible";

export async function pathStatus(filePath: string): Promise<PathStatus> {
  try {
    await fs.access(filePath);
    return "present";
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    // ENOENT/ENOTDIR are genuine absence; EACCES/EPERM/EIO/ELOOP and anything
    // unrecognized are "could not tell", and must never be read as absence.
    return code === "ENOENT" || code === "ENOTDIR" ? "absent" : "inaccessible";
  }
}

export async function ensureDir(ctx: WizardContext, dirPath: string): Promise<void> {
  if (ctx.dryRun) {
    note(ctx, `[dry-run] would create directory ${dirPath}.`);
    return;
  }
  await fs.mkdir(dirPath, { recursive: true });
}

export async function copyDirectoryContents(
  ctx: WizardContext,
  sourceDir: string,
  targetDir: string,
): Promise<number> {
  if (!(await pathExists(sourceDir))) return 0;
  const entries = await fs.readdir(sourceDir, { withFileTypes: true });
  if (ctx.dryRun) {
    note(ctx, `[dry-run] would copy ${entries.length} item${entries.length === 1 ? "" : "s"} from ${sourceDir} to ${targetDir}.`);
    return entries.length;
  }
  await fs.mkdir(targetDir, { recursive: true });
  for (const entry of entries) {
    const sourcePath = path.join(sourceDir, entry.name);
    const targetPath = path.join(targetDir, entry.name);
    await fs.cp(sourcePath, targetPath, {
      force: true,
      preserveTimestamps: true,
      recursive: true,
    });
  }
  return entries.length;
}

export async function copyFileIfMissing(
  ctx: WizardContext,
  sourcePath: string,
  targetPath: string,
): Promise<boolean> {
  if (await pathExists(targetPath)) return false;
  if (ctx.dryRun) {
    note(ctx, `[dry-run] would copy ${sourcePath} to ${targetPath}.`);
    return true;
  }
  await fs.mkdir(path.dirname(targetPath), { recursive: true });
  await fs.copyFile(sourcePath, targetPath);
  return true;
}

export async function copyDirectoryContentsIfMissing(
  ctx: WizardContext,
  sourceDir: string,
  targetDir: string,
): Promise<number> {
  if (!(await pathExists(sourceDir))) return 0;
  const entries = await fs.readdir(sourceDir, { withFileTypes: true });
  let copied = 0;
  for (const entry of entries) {
    const sourcePath = path.join(sourceDir, entry.name);
    const targetPath = path.join(targetDir, entry.name);
    copied += entry.isDirectory()
      ? await copyDirectoryContentsIfMissing(ctx, sourcePath, targetPath)
      : Number(await copyFileIfMissing(ctx, sourcePath, targetPath));
  }
  return copied;
}

export async function readTextIfExists(filePath: string): Promise<string | undefined> {
  if (!(await pathExists(filePath))) return undefined;
  return fs.readFile(filePath, "utf8");
}

export async function writeTextIfChanged(
  ctx: WizardContext,
  filePath: string,
  content: string,
  label: string,
): Promise<boolean> {
  const existing = await readTextIfExists(filePath);
  if (existing === content) return false;
  if (ctx.dryRun) {
    note(ctx, `[dry-run] would update ${label} at ${filePath}.`);
    return true;
  }
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  await fs.writeFile(filePath, content, "utf8");
  return true;
}

export async function readJsonIfExists<T>(filePath: string, fallback: T): Promise<T> {
  const text = await readTextIfExists(filePath);
  if (text === undefined) return fallback;
  return JSON.parse(text) as T;
}


/**
 * The setup debug log: technical detail that must be RECORDED but not SPOKEN.
 *
 * The standing copy rule (Peter, 2026-08-14) keeps command lines, paths and
 * raw stderr out of the wizard's customer-visible narration — but support
 * still needs the detail when a step reports a plain-language failure. It
 * lands here, inside the profile, where a support conversation can ask for it.
 * Best-effort by design: a log that cannot be written must never fail a step.
 */
export function setupLogPath(profileDir: string): string {
  return path.join(profileDir, "logs", "setup.log");
}

export async function appendSetupLog(ctx: WizardContext, entry: string): Promise<void> {
  if (ctx.dryRun) return;
  try {
    const target = setupLogPath(ctx.profileDir);
    await fs.mkdir(path.dirname(target), { recursive: true });
    await fs.appendFile(target, `[${new Date().toISOString()}] ${entry.trimEnd()}\n`, "utf8");
  } catch {
    // Recording is best-effort; the plain-language note already told the
    // customer what happened.
  }
}
