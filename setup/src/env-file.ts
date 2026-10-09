import path from "node:path";
import { assertNoTokenReplay } from "./adapter-guard.js";
import { ensureDir, pathExists, readTextIfExists, writeTextIfChanged } from "./fs-utils.js";
import { note } from "./ui.js";
import type { WizardContext } from "./context.js";

export type EnvMap = Record<string, string>;

export async function readEnvFile(filePath: string): Promise<EnvMap> {
  const text = await readTextIfExists(filePath);
  if (text === undefined) return {};
  const values: EnvMap = {};
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (line === "" || line.startsWith("#")) continue;
    const equals = line.indexOf("=");
    if (equals === -1) continue;
    const key = line.slice(0, equals).trim();
    const value = line.slice(equals + 1).trim();
    values[key] = unquoteEnvValue(value);
  }
  return values;
}

/**
 * Write env values into a profile `.env`.
 *
 * The token-replay guard runs on the ACTUAL keys being written, here in the
 * writer rather than in any one caller: this is the only place JARVIS puts a
 * credential into a profile, so it is the only place that can promise a
 * Claude-subscription token never lands in one — from this step or any future
 * one.
 */
export async function upsertEnv(
  ctx: WizardContext,
  filePath: string,
  updates: EnvMap,
  options: { secretKeys?: string[] } = {},
): Promise<void> {
  assertNoTokenReplay({ envKeys: Object.keys(updates) });
  await ensureDir(ctx, path.dirname(filePath));
  const existing = (await readTextIfExists(filePath)) ?? "";
  const next = upsertEnvText(existing, updates);
  if (ctx.dryRun) {
    const keys = Object.keys(updates).map((key) =>
      options.secretKeys?.includes(key) ? `${key}=<secret>` : `${key}=<set>`,
    );
    note(ctx, `[dry-run] would write ${keys.join(", ")} to ${filePath}.`);
    return;
  }
  await writeTextIfChanged(ctx, filePath, next, ".env");
}

export async function removeEnv(
  ctx: WizardContext,
  filePath: string,
  keys: string[],
): Promise<void> {
  const existing = await readTextIfExists(filePath);
  if (existing === undefined) return;
  const next = removeEnvText(existing, keys);
  if (next === existing) return;
  await writeTextIfChanged(ctx, filePath, next, ".env");
}

export function upsertEnvText(existing: string, updates: EnvMap): string {
  const lines = existing.split(/\r?\n/);
  const seen = new Set<string>();
  const next = lines.map((line) => {
    const equals = line.indexOf("=");
    if (equals === -1 || line.trim().startsWith("#")) return line;
    const key = line.slice(0, equals).trim();
    if (!Object.prototype.hasOwnProperty.call(updates, key)) return line;
    seen.add(key);
    return `${key}=${quoteEnvValue(updates[key] ?? "")}`;
  });

  for (const [key, value] of Object.entries(updates)) {
    if (!seen.has(key)) next.push(`${key}=${quoteEnvValue(value)}`);
  }

  while (next.length > 0 && next[next.length - 1] === "") next.pop();
  return `${next.join("\n")}\n`;
}

export function removeEnvText(existing: string, keys: string[]): string {
  const removedKeys = new Set(keys);
  let removed = false;
  const next = existing.split(/\r?\n/).filter((line) => {
    const equals = line.indexOf("=");
    if (equals === -1 || line.trim().startsWith("#")) return true;
    const key = line.slice(0, equals).trim();
    if (!removedKeys.has(key)) return true;
    removed = true;
    return false;
  });

  if (!removed) return existing;
  while (next.length > 0 && next[next.length - 1] === "") next.pop();
  return next.length === 0 ? "" : `${next.join("\n")}\n`;
}

export async function envFileExists(ctx: WizardContext): Promise<boolean> {
  return pathExists(path.join(ctx.profileDir, ".env"));
}

function quoteEnvValue(value: string): string {
  if (/^[A-Za-z0-9_./:@%+=,-]*$/.test(value)) return value;
  return JSON.stringify(value);
}

function unquoteEnvValue(value: string): string {
  if (value.startsWith('"') && value.endsWith('"')) {
    try {
      const parsed = JSON.parse(value) as unknown;
      if (typeof parsed === "string") return parsed;
    } catch {
      // Keep accepting hand-written dotenv quotes that are not JSON strings.
    }
    return value.slice(1, -1);
  }
  if (value.startsWith("'") && value.endsWith("'")) return value.slice(1, -1);
  return value;
}
