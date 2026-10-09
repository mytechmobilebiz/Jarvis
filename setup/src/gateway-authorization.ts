import path from "node:path";
import { promises as fs } from "node:fs";
import { pathStatus } from "./fs-utils.js";
import { readMigrationMarker } from "./migration-marker.js";
import { PROVIDER_SAFETY_REVISION } from "./provider-safety-revision.js";
import { SETUP_STATE_VERSION, setupStateFilePath, type SetupProgress } from "./setup-state.js";

/**
 * May an engine be SPAWNED on this machine, right now?
 *
 * One question, one answer, one place. Rounds 1–3 of the review kept finding
 * the same shape of bug: the rule existed, and some path did not ask it — the
 * wizard checked provider safety, the launch path checked something adjacent,
 * "BOOT HUD ANYWAY" checked nothing, and the supervisor's own crash-restart
 * spawned without asking anyone. So the rule now lives here, and the SPAWN
 * SEAM asks it — not the callers.
 *
 * Three things make this reader different from the ordinary one:
 *
 *  1. It is STRICT. `readSetupProgressAt` answers "is there usable progress?"
 *     and turns corrupt JSON, an incompatible version and every read error into
 *     `undefined` — perfectly right for resuming a wizard, and fatal here,
 *     because `undefined` used to read as "fresh machine, nothing owed".
 *  2. Fresh means PROVABLY fresh: the profile directory is genuinely absent
 *     (ENOENT, not "we could not look"). Anything else is an existing machine
 *     that must show its papers.
 *  3. An existing machine must carry the CURRENT provider-safety revision. Not
 *     "no debt recorded" — a positive stamp, written by a pass that completed.
 *     Absence of a stamp is absence of proof.
 *
 * Stopping is never gated. Only spawning is dangerous, and provider safety's
 * remediation depends on being able to stop an engine while a debt stands.
 */

export type GatewayDebtReason =
  | "provider-safety"
  | "provider-safety-restart"
  | "provider-safety-unstamped";

export interface GatewayAuthorization {
  authorized: boolean;
  reasons: readonly GatewayDebtReason[];
  /** Customer-facing sentence; safe for a dialog, a status line or a log. */
  detail: string;
}

export type AuthorizationState =
  /** No profile directory at all: a machine that has never been set up. */
  | { kind: "fresh" }
  /** A profile directory with state we could read and understand. */
  | { kind: "known"; progress: SetupProgress }
  /** Anything else: corrupt, incompatible, unreadable, or a directory we cannot inspect. */
  | { kind: "unreadable"; detail: string };

export interface GatewayAuthorizationSources {
  profileDir: string;
  /** Test seam; production reads the profile directory strictly. */
  readState?: (profileDir: string) => Promise<AuthorizationState>;
  readMarker?: (profileDir: string) => Promise<unknown>;
}

/**
 * The strict reader. Every failure mode is named rather than flattened, and
 * the only "nothing to see here" answer requires the directory to be provably
 * absent.
 */
export async function readAuthorizationState(profileDir: string): Promise<AuthorizationState> {
  const dir = await pathStatus(profileDir);
  if (dir === "absent") return { kind: "fresh" };
  if (dir === "inaccessible") {
    return { kind: "unreadable", detail: "the profile directory could not be opened" };
  }

  const statePath = setupStateFilePath(profileDir);
  const state = await pathStatus(statePath);
  if (state === "absent") {
    // A profile directory with no setup state is not a fresh machine — it is a
    // machine whose state we cannot account for.
    return { kind: "unreadable", detail: "this profile has no setup state" };
  }
  if (state === "inaccessible") {
    return { kind: "unreadable", detail: "the setup state could not be opened" };
  }

  let raw: string;
  try {
    raw = await fs.readFile(statePath, "utf8");
  } catch {
    return { kind: "unreadable", detail: "the setup state could not be read" };
  }
  try {
    const parsed = JSON.parse(raw) as Partial<SetupProgress>;
    if (parsed.version !== SETUP_STATE_VERSION || typeof parsed.steps !== "object" || parsed.steps === null) {
      return { kind: "unreadable", detail: "the setup state is from a different version of JARVIS" };
    }
    return {
      kind: "known",
      progress: {
        version: SETUP_STATE_VERSION,
        steps: { ...parsed.steps },
        completed: parsed.completed === true,
        ...(Number.isInteger(parsed.providerSafetyRev) ? { providerSafetyRev: parsed.providerSafetyRev } : {}),
        ...(parsed.providerSafetyRestartPending === true ? { providerSafetyRestartPending: true } : {}),
        ...(parsed.providerSafetyBlocked === true ? { providerSafetyBlocked: true } : {}),
      },
    };
  } catch {
    return { kind: "unreadable", detail: "the setup state is damaged" };
  }
}

const AUTHORIZED: GatewayAuthorization = { authorized: true, reasons: [], detail: "" };

export async function authorizeGatewayStart(
  sources: GatewayAuthorizationSources,
): Promise<GatewayAuthorization> {
  const readState = sources.readState ?? readAuthorizationState;
  const readMarker = sources.readMarker ?? readMigrationMarker;

  let state: AuthorizationState;
  try {
    state = await readState(sources.profileDir);
  } catch {
    state = { kind: "unreadable", detail: "the setup state could not be read" };
  }

  // A machine with no profile at all owes nothing: there is no provider
  // configured, so there is nothing unsafe to talk to. The wizard — not this
  // gate — stands in front of it.
  if (state.kind === "fresh") return AUTHORIZED;

  // An UNREADABLE state is not fresh, and this is the one place the clean-slate
  // change had to stop short. Setup treats unreadable as "start over", and it
  // should: it moves the file aside and runs. But this gate cannot, because the
  // file it could not read is exactly where a provider-safety debt is RECORDED
  // — `providerSafetyBlocked`, a pending restart, or the stamp proving the
  // migration ran. Authorizing on unreadable would let a profile that owes
  // remediation start an engine with an old Codex credential-import or an
  // unkeyed Anthropic route: the precise thing provider safety exists to stop.
  //
  // So it refuses with the SAME reason and the SAME remedy as a missing stamp —
  // "run setup" — and running setup now purges, rewrites the state and clears
  // it. The customer is one wizard run from working, never stuck: which is what
  // the clean-slate change was actually for.
  if (state.kind === "unreadable") {
    return {
      authorized: false,
      reasons: ["provider-safety-unstamped"],
      detail: describe(["provider-safety-unstamped"]),
    };
  }

  const reasons: GatewayDebtReason[] = [];
  const { progress } = state;
  if (progress.providerSafetyBlocked === true) reasons.push("provider-safety");
  if (progress.providerSafetyRestartPending === true) reasons.push("provider-safety-restart");
  // A positive, current stamp or nothing: "no recorded debt" is not proof that
  // the check ever ran (review r3, P1 — the healthy fixture had no stamp).
  if (progress.providerSafetyRev !== PROVIDER_SAFETY_REVISION) reasons.push("provider-safety-unstamped");

  if (reasons.length === 0) return AUTHORIZED;
  return { authorized: false, reasons, detail: describe(reasons) };
}

function describe(reasons: readonly GatewayDebtReason[]): string {
  void reasons;
  // One remaining reason, one sentence. Every other debt this gate used to
  // enforce was removed in v1.4.0's clean-slate change: leftovers get deleted
  // rather than blocking, and an unreadable state means "set up from zero".
  return "JARVIS has not started its engine: this profile's AI provider could not be confirmed safe to use. "
    + "Run setup and complete the Model step — paste an API key from Anthropic, OpenAI or OpenRouter.";
}

/** Where the strict reader looks, exported so tests and diagnostics agree. */
export function authorizationStatePath(profileDir: string): string {
  return path.join(profileDir, path.basename(setupStateFilePath(profileDir)));
}
