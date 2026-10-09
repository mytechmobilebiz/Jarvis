import { lstat } from "node:fs/promises";
import path from "node:path";
import type { WizardContext } from "./context.js";
import { readEnvFile, upsertEnv } from "./env-file.js";
import { classifyKeyShape } from "./provider-safety.js";

/**
 * Cross-version key carry (re-cut #5, Peter: "JARVIS never asks for a key he
 * already has").
 *
 * Some 1.3.x installs kept the provider key in the ENGINE tree's own `.env`
 * (`~/.hermes/hermes-agent/.env`) — a class-A directory the earlier-install
 * cleanup deletes unconditionally. Before that wipe, the ONE customer-authored
 * value worth keeping is lifted into the profile `.env`, where the Model
 * step's saved-key auto-verify picks it up like any other saved key.
 *
 * Hard rules, in the order they bind:
 *  - LIFT ONE VALUE, NOT ARTIFACTS. Only the three provider key names below
 *    are ever read; nothing else from the old tree survives through this.
 *  - SHAPE-GATED by the one classification authority (classifyKeyShape):
 *    only an API-key-shaped ("plausible") value is carried. OAuth/
 *    subscription-shaped tokens are NEVER carried — that is the credential
 *    provider safety exists to refuse — and neither is anything malformed
 *    or unfamiliar: a value that would have earned a question at paste time
 *    does not get to skip the question by arriving in a file.
 *  - THE PROFILE WINS. A key already saved in the profile `.env` is never
 *    overwritten by an older one.
 *  - CARRY IS POSSESSION, NOT TRUST. Nothing here marks anything configured;
 *    the carried key still faces the assembled-token verify in the Model
 *    step, which is the only thing that ever completes that step.
 */
const CARRYABLE_ENV_KEYS = ["ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"] as const;

export interface CarryReport {
  /** Env key names lifted into the profile (never the values). */
  carried: string[];
  /** Env key names found but refused, with the plain reason (never values). */
  refused: string[];
}

export async function liftLegacyProviderKeys(ctx: WizardContext): Promise<CarryReport> {
  const report: CarryReport = { carried: [], refused: [] };
  if (ctx.dryRun) return report;

  const legacyEnvPath = path.join(ctx.hermesAgentDir, ".env");
  // A symlinked .env is not the old install's file — it points somewhere the
  // cleanup would never delete, so there is nothing to rescue and following
  // it would read a file outside the tree being wiped.
  const info = await lstat(legacyEnvPath).catch(() => undefined);
  if (info === undefined || !info.isFile() || info.isSymbolicLink()) return report;

  const legacy: Record<string, string> = await readEnvFile(legacyEnvPath).catch(() => ({}));
  const profileEnvPath = path.join(ctx.profileDir, ".env");
  const profile: Record<string, string> = await readEnvFile(profileEnvPath).catch(() => ({}));

  for (const envKey of CARRYABLE_ENV_KEYS) {
    const value = (legacy[envKey] ?? "").trim();
    if (value === "") continue;
    if ((profile[envKey] ?? "").trim() !== "") {
      report.refused.push(`${envKey}: the profile already has one — kept the newer`);
      continue;
    }
    const shape = classifyKeyShape(envKey, value);
    if (shape.verdict !== "plausible") {
      report.refused.push(`${envKey}: not API-key-shaped (${shape.verdict}) — never carried`);
      continue;
    }
    await upsertEnv(ctx, profileEnvPath, { [envKey]: value }, { secretKeys: [envKey] });
    report.carried.push(envKey);
  }
  return report;
}
