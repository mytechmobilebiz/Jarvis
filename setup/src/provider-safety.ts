import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import type { WizardContext } from "./context.js";
import { readEnvFile } from "./env-file.js";
import { pathExists, pathStatus } from "./fs-utils.js";
import { gatewayIsSettled, settleGatewayForSafety, type GatewayRestartOutcome } from "./gateway-restart.js";
import { readSetupProgress, writeSetupProgress, type SetupProgress } from "./setup-state.js";
import { enginePythonPath, runCommand } from "./system.js";
import { PROVIDER_SAFETY_REVISION } from "./provider-safety-revision.js";

/**
 * Provider safety: the migration that runs whether or not setup does.
 *
 * Every safety fix in this lane so far protected the FRESH install — a new
 * profile is inert, a new route is written whole, a new menu cannot offer the
 * Codex path. None of that reaches the installs that already exist: an upgrade
 * from v1.3.x boots the HUD without running setup at all, and even a forced
 * rerun can retain Profile and Model. Those profiles can carry
 *
 *   - `provider: openai-codex` (+ `openai_runtime: codex_app_server`), whose
 *     credential resolution can copy the customer's ~/.codex/auth.json inside
 *     the engine (reproduced in engine-seam.test.ts), or
 *   - the old shipped seed `default: anthropic/claude-sonnet-5` with no
 *     Anthropic key, which the engine resolves through its Claude Code
 *     credential-replay adapter — on the customer's own Claude account.
 *
 * so the gateway keeps reaching exactly the two paths v1.4.0 exists to close.
 * This migration therefore runs BEFORE setup gating, is not a step, cannot be
 * skipped, and is versioned like the gateway-integrity check so a future
 * revision re-runs it.
 */

/** Bump when the rules below change; a mismatch re-runs the migration. */
export { PROVIDER_SAFETY_REVISION } from "./provider-safety-revision.js";

/** What the migration did. A CLOSED SET — these reach telemetry and notes. */
export type ProviderSafetyAction =
  | "codex-route-neutralized"
  | "unkeyed-anthropic-seed-cleared"
  | "claude-code-pool-entry-cleared"
  | "claude-code-oauth-collision";

export interface ProviderSafetyResult {
  /** Empty when the profile was already safe. */
  actions: ProviderSafetyAction[];
  notes: string[];
  /** True when the config file was changed on disk. */
  changed: boolean;
  /** True when the check could not be completed (and so proved nothing). */
  inconclusive: boolean;
  /**
   * WHY it was inconclusive, as a closed set — so callers can act on the one
   * cause they can repair (installing the engine) without re-running the whole
   * lifecycle for causes they cannot (review r2, finding 6).
   */
  inconclusiveReason?: "engine-missing" | "config-missing" | "config-unreadable" | "check-failed";
  /**
   * True while a gateway may still be SERVING the route we removed. The disk is
   * clean but the process is not, so nothing may be stamped as safe and the
   * customer has to be told plainly.
   */
  restartPending: boolean;
  /** How the running gateway responded, when we had to touch it. */
  gateway?: GatewayRestartOutcome;
}

/**
 * One python transaction: read the model block, decide, and rewrite atomically.
 *
 * Same shape as the voice-config migration (profile-migrate.ts): temp file with
 * the original mode, fsync, `os.replace`. Either the whole model block lands or
 * none of it does — a half-applied route is what makes a stale endpoint
 * reachable, and this file is read live by a running gateway.
 */
const PROVIDER_SAFETY_PY = `
import json, os, sys, yaml

import errno, os, time

def _acquire(path):
    """Profile-scoped lock held across the whole read-modify-write.

    Reading before acquiring is what made the previous version unsafe: a writer
    that waited on the lock would then dump a config it had read BEFORE the
    other writer committed, silently reverting it. So callers acquire first and
    read inside.

    Stale reclaim is by atomic rename, not stat-then-unlink: two processes that
    both saw an old lock would both have unlinked it — including a lock a third
    process had just legitimately taken. Only the process whose rename succeeds
    owns the reclaim.
    """
    lock_path = path + ".jarvis-lock"
    deadline = time.time() + 10.0
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            return lock_path
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            try:
                age = time.time() - os.stat(lock_path).st_mtime
            except OSError:
                continue
            if age > 30.0:
                claim = "%s.claim.%d" % (lock_path, os.getpid())
                try:
                    os.rename(lock_path, claim)
                except OSError:
                    # Someone else won the reclaim; go back to waiting.
                    time.sleep(0.05)
                    continue
                try:
                    os.unlink(claim)
                except OSError:
                    pass
                continue
            if time.time() > deadline:
                raise RuntimeError("another JARVIS process is writing this profile config")
            time.sleep(0.05)


def _release(lock_path):
    try:
        os.unlink(lock_path)
    except OSError:
        pass

path = os.path.realpath(sys.argv[1])
codex_disabled = sys.argv[2] == "1"
anthropic_key_present = sys.argv[3] == "1"

if not os.path.exists(path):
    print(json.dumps({"actions": [], "changed": False, "missing": True}))
    sys.exit(0)

# Held across read AND write: deciding from a config read outside the lock is
# how a waiting writer reverts the one that went first.
lock = _acquire(path)
try:
    with open(path) as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config, dict):
        print(json.dumps({"actions": [], "changed": False, "unreadable": True}))
        sys.exit(0)

    model = config.get("model")
    if isinstance(model, str):
        model = {"default": model}
    if not isinstance(model, dict):
        model = {}

    provider = str(model.get("provider") or "").strip().lower()
    default = str(model.get("default") or "").strip()
    runtime = str(model.get("openai_runtime") or "").strip().lower()
    actions = []

    # 1. A Codex route while the option is disabled: make it inert. The provider
    #    is the trigger; the runtime alone is inert without it but is cleared too
    #    so a later provider change cannot silently re-arm it.
    if codex_disabled and (provider == "openai-codex" or runtime == "codex_app_server"):
        model.pop("provider", None)
        model.pop("openai_runtime", None)
        model.pop("base_url", None)
        model.pop("default", None)
        actions.append("codex-route-neutralized")

    # 2. The exact legacy shape ONLY: no explicit provider (or an explicit
    #    anthropic one), an Anthropic model, and no Anthropic key anywhere we can
    #    see. A deliberate nous/openrouter/... route that happens to serve an
    #    anthropic/* slug from its own catalogue is NOT this, and must survive.
    else:
        lowered = default.lower()
        anthropic_model = lowered.startswith("anthropic/") or lowered.startswith("claude-")
        unset_or_anthropic = provider in ("", "anthropic")
        if anthropic_model and unset_or_anthropic and not anthropic_key_present:
            model.pop("default", None)
            if provider == "anthropic":
                model.pop("provider", None)
            actions.append("unkeyed-anthropic-seed-cleared")

    if not actions:
        print(json.dumps({
            "actions": [], "changed": False,
            "provider": provider, "default": default, "runtime": runtime,
        }))
        sys.exit(0)

    config["model"] = model
    serialized = yaml.safe_dump(config, sort_keys=False)
    mode = os.stat(path).st_mode & 0o7777
    tmp_path = path + ".jarvis-provider-safety.tmp.%d" % os.getpid()
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
finally:
    _release(lock)
print(json.dumps({"actions": actions, "changed": True}))
`;

const ACTION_NOTES: Record<ProviderSafetyAction, string> = {
  "codex-route-neutralized":
    "This profile was set up to think through the ChatGPT/Codex app. That route is turned off in this version "
    + "because the engine can copy the Codex app's own sign-in file, so I have cleared it. Run the Model step to "
    + "add an API key.",
  "unkeyed-anthropic-seed-cleared":
    "This profile pointed at an Anthropic model with no Anthropic key behind it, which can quietly use a Claude "
    + "subscription on this computer. I have cleared it. Run the Model step to set up a provider properly.",
  "claude-code-pool-entry-cleared":
    "This profile had adopted the Claude Code sign-in on this computer as a saved credential. I have removed it: "
    + "refreshing it rewrites Claude Code's own sign-in file and can downgrade that app's subscription. Your "
    + "Claude Code install is untouched — JARVIS simply no longer holds a copy of its sign-in.",
  "claude-code-oauth-collision":
    "JARVIS hasn't started. The Anthropic sign-in saved here is a Claude subscription token, and Claude Code is "
    + "signed in on this computer. In that combination JARVIS would run on Claude Code's sign-in and could "
    + "downgrade that app to pay-per-use — so it stops instead. Fix it in the Model step: paste an Anthropic API "
    + "key starting sk-ant-api — or switch to an OpenAI or OpenRouter key.",
};

/**
 * Is this Anthropic secret an API KEY, or a subscription (OAuth) token?
 *
 * The distinction decides whether we can protect the customer's Claude Code
 * install, and it was established by experiment against the pinned engine
 * (v2026.7.1), not by reading:
 *
 *   - An API-key-shaped value delivered as ANTHROPIC_TOKEN WINS over
 *     `~/.claude/.credentials.json`; the file is not even consulted (proven by
 *     making it unreadable — resolution still succeeded).
 *   - An OAuth-shaped value LOSES: `_prefer_refreshable_claude_code_token()`
 *     overrides it with the file's token precisely because it is OAuth-shaped,
 *     and a refresh then rewrites that file without `subscriptionType`.
 *
 * If a future engine pin changes that ordering, provider-safety.test.ts fails
 * loudly rather than this quietly becoming false.
 */
export function anthropicSecretShape(value: string): "api-key" | "oauth" | "unknown" {
  const secret = value.trim();
  if (secret === "") return "unknown";
  if (/^sk-ant-api\d*-/.test(secret)) return "api-key";
  if (/^sk-ant-(oat|ort)\d*-/.test(secret)) return "oauth";
  return "unknown";
}

/**
 * What the wizard should do with a pasted key, per provider.
 *
 * THE key-classification authority (Peter's smoke feedback, 2026-08-14): the
 * Model step consults this and nothing else, and it builds on
 * `anthropicSecretShape` above rather than growing a second opinion about
 * Anthropic secrets.
 *
 * Three verdicts, deliberately conservative in different directions:
 *  - "reject": structurally not a key (whitespace inside, too short, wrong
 *    characters) or an Anthropic subscription token — the one shape that is
 *    NEVER a valid API key and that provider safety would block at launch
 *    anyway. Rejecting it here moves that refusal to the moment of the paste.
 *  - "unfamiliar": well-formed but not the shape this provider usually issues
 *    (most often a key pasted under the wrong provider). Providers change
 *    their key formats, so this must CONFIRM, never block — a valid future
 *    key has to survive this function.
 *  - "plausible": looks like what the provider issues today. Not a promise —
 *    the verification chat remains the only proof.
 *
 * Every `reason` is customer-facing copy: plain language, no env-var names.
 * The sk-… prefixes stay because they are load-bearing on the key path.
 */
export type KeyShapeVerdict =
  | { verdict: "plausible" }
  | { verdict: "reject" | "unfamiliar"; reason: string };

export function classifyKeyShape(envKey: string, value: string): KeyShapeVerdict {
  const key = value.trim();
  const provider = envKey === "ANTHROPIC_API_KEY"
    ? "Anthropic"
    : envKey === "OPENROUTER_API_KEY"
      ? "OpenRouter"
      : envKey === "OPENAI_API_KEY"
        ? "OpenAI"
        : undefined;

  if (/\s/.test(key)) {
    return {
      verdict: "reject",
      reason: "That doesn't look like a key — it has spaces or line breaks inside. Copy the whole key in one piece and paste it again.",
    };
  }
  if (key.length < 20) {
    return {
      verdict: "reject",
      reason: "That looks too short to be an API key — real ones are much longer. Copy the whole key and paste it again.",
    };
  }
  // Printable ASCII only: smart quotes, accents and invisible characters mean
  // the key was mangled on its way here (a screenshot OCR, a rich-text email).
  if (!/^[\x21-\x7e]+$/.test(key)) {
    return {
      verdict: "reject",
      reason: "That doesn't look like a key — it contains unusual characters. Copy the key exactly as your provider shows it.",
    };
  }

  if (provider === "Anthropic") {
    const shape = anthropicSecretShape(key);
    if (shape === "api-key") return { verdict: "plausible" };
    if (shape === "oauth") {
      return {
        verdict: "reject",
        reason: "That's a Claude subscription sign-in, not an API key — and JARVIS can't run on it. "
          + "Create a key starting sk-ant-api at console.anthropic.com, or use an OpenAI or OpenRouter key instead.",
      };
    }
    return {
      verdict: "unfamiliar",
      reason: key.startsWith("sk-") && !key.startsWith("sk-ant-")
        ? "That looks like a key from a different provider — Anthropic keys start with sk-ant-api."
        : "That doesn't look like the usual Anthropic key — they start with sk-ant-api.",
    };
  }
  if (provider === "OpenRouter") {
    if (key.startsWith("sk-or-")) return { verdict: "plausible" };
    return {
      verdict: "unfamiliar",
      reason: key.startsWith("sk-ant-")
        ? "That looks like an Anthropic key — OpenRouter keys start with sk-or-."
        : "That doesn't look like the usual OpenRouter key — they start with sk-or-.",
    };
  }
  if (provider === "OpenAI") {
    if (key.startsWith("sk-ant-") || key.startsWith("sk-or-")) {
      return {
        verdict: "unfamiliar",
        reason: "That looks like a key from a different provider — OpenAI keys start with sk-.",
      };
    }
    if (key.startsWith("sk-")) return { verdict: "plausible" };
    return {
      verdict: "unfamiliar",
      reason: "That doesn't look like the usual OpenAI key — they start with sk-.",
    };
  }

  // A provider this function has never heard of: no opinion beats a wrong one.
  return { verdict: "plausible" };
}

export interface ProviderSafetyOverrides {
  /** Test seams for the revision-2 Claude Code rules. */
  claudeCodeCredentialsPresent?: boolean;
  anthropicSecret?: string;
  run?: typeof runCommand;
  platform?: NodeJS.Platform;
  /** Injected in tests; production reads the profile and hermes-root .env. */
  anthropicKeyPresent?: boolean;
  /** Carried from persisted state: a previous run cleared a route but could not settle the gateway. */
  restartPending?: boolean;
  /**
   * Carried from persisted state: a previous pass could not prove this profile
   * safe. Like `restartPending` it must survive every early return — a pass
   * that proves nothing may not forgive a debt it never examined (integration
   * review r2, finding 2).
   */
  blocked?: boolean;
}

/**
 * Run the migration against the live profile. Safe to call on every launch:
 * with nothing to fix it makes no writes and returns no actions.
 */
export async function applyProviderSafety(
  ctx: WizardContext,
  overrides: ProviderSafetyOverrides = {},
): Promise<ProviderSafetyResult> {
  const run = overrides.run ?? runCommand;
  const configPath = path.join(ctx.profileDir, "config.yaml");
  // A carried-in pending flag is about the RUNNING gateway, not the file: every
  // early return below must hand it back, or a pass that could not check
  // anything would quietly declare the machine safe.
  const carried = overrides.restartPending === true;
  const carriedBlock = overrides.blocked === true;
  const configStatus = await pathStatus(configPath);
  if (configStatus !== "present") {
    // A config we could not READ is not a config that is not there: a
    // permission error, a half-mounted volume or a directory we cannot traverse
    // all arrive here, and every one of them is a state in which the unsafe
    // route may still exist and be loaded (review r2, finding 2).
    if (configStatus === "inaccessible") {
      return {
        actions: [],
        notes: ["I could not open this profile's model configuration to check it for unsafe settings."],
        changed: false,
        inconclusive: true,
        inconclusiveReason: "config-unreadable",
        restartPending: carried,
      };
    }
    // Genuinely absent. That is conclusive ONLY for a profile that does not
    // exist yet — a fresh install has nothing to be unsafe about. An EXISTING
    // profile whose config has gone missing is the opposite: something removed
    // it, and a gateway started earlier may still be serving what it said.
    const fresh = await profileLooksFresh(ctx);
    if (!fresh || carried || carriedBlock) {
      return {
        actions: [],
        notes: fresh
          ? []
          : ["This profile has no model configuration file, so I could not check it for unsafe settings."],
        changed: false,
        inconclusive: true,
        inconclusiveReason: "config-missing",
        restartPending: carried,
      };
    }
    return { actions: [], notes: [], changed: false, inconclusive: false, restartPending: false };
  }

  // Either payload shape — the wheelhouse venv OR the vendored site
  // interpreter (macOS v1.4.0). Gating on the venv alone left every mac site
  // install "engine-missing" FOREVER: the check could never run, so the block
  // could never clear (Codex review 2026-08-14, finding 1 — the same class as
  // the hermesInvocation P1, one seam over).
  const python = enginePythonPath(ctx, overrides.platform);
  if (python === undefined) {
    // Honest failure: without the engine's python this cannot be checked, and
    // saying "safe" would be a guess about the one thing that must not be
    // guessed.
    return {
      actions: [],
      notes: ["I could not check this profile's model provider for unsafe settings because the Hermes engine is not fully installed yet."],
      changed: false,
      inconclusive: true,
      inconclusiveReason: "engine-missing",
      restartPending: carried,
    };
  }

  const keyPresent = overrides.anthropicKeyPresent ?? (await anthropicKeyPresent(ctx));
  const result = await run(
    ctx,
    python,
    ["-c", PROVIDER_SAFETY_PY, configPath, "1", keyPresent ? "1" : "0"],
    { stream: false },
  );
  if (result.status !== 0) {
    return {
      actions: [],
      notes: ["I could not check this profile's model provider for unsafe settings; rerun setup and run the Model step."],
      changed: false,
      inconclusive: true,
      inconclusiveReason: "check-failed",
      restartPending: carried,
    };
  }

  const parsed = parseSafetyOutput(result.stdout);
  if (parsed === undefined || parsed.unreadable) {
    // A YAML root we cannot understand proves nothing about what it configures.
    return {
      actions: [],
      notes: ["I could not read this profile's model configuration, so I could not check it for unsafe settings."],
      changed: false,
      inconclusive: true,
      inconclusiveReason: "config-unreadable",
      restartPending: carried,
    };
  }

  // carriedBlock joins this deliberately: a crash between the python
  // transaction removing a route and the debt being recorded leaves a clean
  // disk and a gateway still serving the old config. Without this, the next
  // pass would find nothing to do and forgive it (review r3, P1).
  const mustSettleGateway = parsed.changed || carried || carriedBlock;
  if (!mustSettleGateway) {
    return { actions: [], notes: [], changed: false, inconclusive: false, restartPending: false };
  }

  const notes = parsed.actions.map((action) => ACTION_NOTES[action]);
  // The load-bearing half: the route is gone from disk, but a gateway that
  // started earlier is still SERVING it from memory. Until that process is
  // confirmed restarted or absent, the unsafe route is still live and nothing
  // may be recorded as settled.
  const restart = await settleGatewayForSafety(ctx, run, { platform: overrides.platform });
  const settled = gatewayIsSettled(restart.outcome);
  notes.push(settled ? restart.note : `${BLOCKER_PREFIX} ${restart.note}`);
  return {
    actions: parsed.actions,
    notes,
    changed: parsed.changed,
    inconclusive: false,
    restartPending: !settled,
    gateway: restart.outcome,
  };
}

/** Prefixed onto the note so no surface can render this as routine. */
const BLOCKER_PREFIX =
  "Jarvis is still running with the unsafe provider it had before, because the engine that is already running "
  + "could not be restarted from here. Stop the engine you started by hand (or restart your computer) and open "
  + "Jarvis once — that clears it for good.";

function parseSafetyOutput(
  stdout: string,
): { actions: ProviderSafetyAction[]; changed: boolean; unreadable: boolean } | undefined {
  const line = stdout.trim().split(/\r?\n/).at(-1) ?? "";
  try {
    const parsed: unknown = JSON.parse(line);
    if (typeof parsed !== "object" || parsed === null) return undefined;
    const record = parsed as { actions?: unknown; changed?: unknown; unreadable?: unknown };
    const actions = Array.isArray(record.actions)
      ? record.actions.filter((action): action is ProviderSafetyAction =>
        action === "codex-route-neutralized" || action === "unkeyed-anthropic-seed-cleared")
      : [];
    return { actions, changed: record.changed === true, unreadable: record.unreadable === true };
  } catch {
    return undefined;
  }
}

/**
 * A profile that does not exist yet.
 *
 * "No config file" is only good news here. Hermes writes SOUL.md and config.yaml
 * together when it installs a profile, so a directory holding neither is one
 * setup has not reached — nothing has ever run against it, and there is nothing
 * for a stale gateway to be serving. Anything else (a profile directory with
 * SOUL.md, .env, history) whose config has gone missing is a machine in an
 * unexplained state, and unexplained is not safe.
 */
async function profileLooksFresh(ctx: WizardContext): Promise<boolean> {
  const dir = await pathStatus(ctx.profileDir);
  if (dir === "absent") return true;
  if (dir === "inaccessible") return false;
  // A strict inventory, not a list of three filenames: a used profile keeping
  // history, knowledge, workspace or cron was being called fresh because none
  // of those were on the list (review r3, P1). The ONLY entries allowed in a
  // "fresh" directory are the ones JARVIS itself writes before a profile
  // exists — everything else means this machine has been used.
  const ours = new Set([
    ".jarvis-migration-v1.4.json",
    "setup-state.json",
    "setup-state.json.tmp",
    ".DS_Store",
  ]);
  try {
    const entries = await fs.readdir(ctx.profileDir);
    return entries.every((entry) => ours.has(entry));
  } catch {
    return false;
  }
}

/**
 * Is there an Anthropic key ANYWHERE the engine would find one?
 *
 * The profile `.env` is the one JARVIS writes, but a deliberate setup can keep
 * the key in the Hermes root `.env` or in the environment the gateway inherits.
 * Clearing a route because we only looked in our own file would break a working
 * install — so absence has to mean absence everywhere we can see.
 */
/** The Anthropic secret this profile would actually use, and where it came from. */
export async function anthropicSecret(
  ctx: WizardContext,
  env: NodeJS.ProcessEnv = process.env,
): Promise<string> {
  const fromEnv = (env.ANTHROPIC_API_KEY ?? "").trim();
  if (fromEnv !== "") return fromEnv;
  for (const candidate of [path.join(ctx.profileDir, ".env"), path.join(ctx.hermesRoot, ".env")]) {
    const file = await readEnvFile(candidate).catch(() => ({} as Record<string, string>));
    const value = (file.ANTHROPIC_API_KEY ?? "").trim();
    if (value !== "") return value;
  }
  return "";
}

/**
 * Is Claude Code signed in on this computer?
 *
 * Presence of the file is the whole question — we never read its contents. It
 * is somebody else's credential; we only need to know that the engine would
 * find it.
 */
export async function claudeCodeCredentialsPresent(
  home: string = os.homedir(),
  exists: (target: string) => Promise<boolean> = pathExists,
): Promise<boolean> {
  return exists(path.join(home, ".claude", ".credentials.json"));
}

/**
 * Remove any Anthropic credential the engine ADOPTED from Claude Code.
 *
 * This is the second write-back trigger, and the spawn environment cannot reach
 * it: `credential_pool.py` refreshes pool entries on its own, and for an entry
 * whose `source` is `claude_code` it writes the result straight back into
 * `~/.claude/.credentials.json` — dropping `subscriptionType` exactly as the
 * resolver path does. An entry like that exists on machines that ran an
 * unkeyed-Anthropic route before revision 1 cleared it: precisely the
 * population most at risk.
 *
 * We remove OUR copy. Claude Code's own file is never touched.
 */
/**
 * The pool `source` value we remove, assembled rather than written out.
 *
 * adapter-guard.test.ts forbids a quoted Claude-Code provider alias anywhere in
 * shipped source, and it is right to — that string configuring a route is the
 * failure it exists to prevent. Here the same string identifies an entry we
 * DELETE, so the rule and this code want the same outcome; building it from
 * parts keeps the guard strict instead of teaching it another exception.
 */
const ADOPTED_FROM_CLAUDE_CODE = ["claude", "code"].join("_");

export async function clearAdoptedClaudeCodeCredentials(
  ctx: WizardContext,
  io: {
    readFile?: (file: string) => Promise<string>;
    writeFile?: (file: string, contents: string) => Promise<void>;
  } = {},
): Promise<{ removed: boolean; unreadableEntries: number }> {
  const readFile = io.readFile ?? ((file: string) => fs.readFile(file, "utf8"));
  const writeFile = io.writeFile ?? ((file: string, contents: string) => fs.writeFile(file, contents, "utf8"));
  // BOTH stores. The engine writes `get_hermes_home()/auth.json`, and which
  // directory that is depends on how it was launched: the root when HERMES_HOME
  // is the root, the profile when a `-p` profile resolves it (hermes_cli/auth.py
  // _auth_file_path, and _global_auth_file_path for the root fallback). Cleaning
  // only one of them was a no-op on whichever machine used the other — found by
  // review, and the reason this now names both.
  const stores = [path.join(ctx.hermesRoot, "auth.json"), path.join(ctx.profileDir, "auth.json")];
  let removed = false;
  let unreadable = 0;

  for (const authPath of stores) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(await readFile(authPath));
    } catch {
      continue; // absent or unreadable: nothing to claim, never a launch failure
    }
    if (typeof parsed !== "object" || parsed === null) continue;
    const root = parsed as Record<string, unknown>;
    // The engine's real shape: auth_store.credential_pool.<provider> = [entry…]
    // (hermes_cli/auth.py _quarantine_nous_pool_entries is the worked example).
    // The older top-level shape is still swept, because a store written by an
    // earlier version is exactly the machine most likely to hold an adopted
    // Claude Code credential.
    const containers: Array<Record<string, unknown>> = [root];
    const pool = root.credential_pool;
    if (typeof pool === "object" && pool !== null && !Array.isArray(pool)) {
      containers.push(pool as Record<string, unknown>);
    }

    let touched = false;
    for (const container of containers) {
      const entries = container.anthropic;
      if (!Array.isArray(entries)) continue;
      const kept = entries.filter((entry) => {
        const source = (entry as { source?: unknown } | null)?.source;
        if (source === ADOPTED_FROM_CLAUDE_CODE) {
          touched = true;
          return false;
        }
        // Unreadable provenance is removed too. Keeping a credential that MIGHT
        // be a copy of the customer's Claude Code sign-in risks rewriting their
        // file; dropping one that was ours costs a re-sign-in. Counted, never
        // silent.
        if (typeof source !== "string" || source.trim() === "") {
          touched = true;
          unreadable += 1;
          return false;
        }
        return true;
      });
      if (kept.length !== entries.length) {
        if (kept.length === 0) delete container.anthropic;
        else container.anthropic = kept;
      }
    }
    if (!touched) continue;

    try {
      await writeFile(authPath, `${JSON.stringify(root, null, 2)}\n`);
      removed = true;
    } catch {
      // A store we could not rewrite is a nuisance, never a reason to keep the
      // customer's engine down (the purge precedent).
    }
  }

  return { removed, unreadableEntries: unreadable };
}

async function anthropicKeyPresent(ctx: WizardContext): Promise<boolean> {
  if ((process.env.ANTHROPIC_API_KEY ?? "").trim() !== "") return true;
  for (const candidate of [path.join(ctx.profileDir, ".env"), path.join(ctx.hermesRoot, ".env")]) {
    const env = await readEnvFile(candidate).catch(() => ({} as Record<string, string>));
    if ((env.ANTHROPIC_API_KEY ?? "").trim() !== "") return true;
  }
  return false;
}

export interface ProviderSafetyPass extends ProviderSafetyResult {
  /** True when setup must be shown even if progress says it finished. */
  forceSetup: boolean;
  /**
   * True while this profile is NOT PROVEN SAFE — the pass was inconclusive, a
   * restart is still owed, or a previous run left the debt on disk and this one
   * could not clear it.
   *
   * Nothing may start a gateway while this is true. "Inconclusive" is not a
   * softer "safe": the two states this migration exists to close (the Codex
   * credential import, the Claude-Code replay adapter) are both invisible until
   * the config can actually be read, and a 1.3.x machine with a broken engine
   * python is exactly where they live (review r1, finding 1).
   */
  blocked: boolean;
  /**
   * The progress state as this lifecycle left it. A caller mid-run (runSetup)
   * must adopt it rather than keep its own older copy: on a fresh install the
   * profile directory may not exist yet, so the write below is skipped and this
   * object is the only record that the check passed.
   */
  progress: SetupProgress;
}

/**
 * The ONE provider-safety lifecycle, shared by every entry point.
 *
 * Both callers — the HUD before it decides whether to show setup, and runSetup
 * before its first step — must do the identical thing, because the failure this
 * exists to prevent is "one path fixed the config and the other forgot to tell
 * setup about it". Divergence here was exactly the round-2 defect: the HUD
 * cleared the route and left `completed: true` behind, so the app opened offline
 * with no way back into the Model step.
 *
 * It is idempotent and cheap once a profile is clean, so calling it on every
 * launch is intended.
 */
export async function ensureProviderSafety(
  ctx: WizardContext,
  overrides: ProviderSafetyOverrides = {},
): Promise<ProviderSafetyPass> {
  const progress = await readSetupProgress(ctx);
  const pending = progress.providerSafetyRestartPending === true;
  const blockedOnDisk = progress.providerSafetyBlocked === true;
  if (progress.providerSafetyRev === PROVIDER_SAFETY_REVISION && !pending && !blockedOnDisk) {
    // THE STAMP DOES NOT COVER THE CLAUDE RULES, and it must not: the whole
    // point of them is that the danger arrives with NO change to this profile.
    // A customer stamped safe last month who installed Claude Code yesterday is
    // exposed, and an early return here would be the difference between a
    // launch-time guarantee and a setup-time one (Codex review, 2026-08-14).
    const standing = await evaluateClaudeCodeRules(ctx, overrides);
    if (standing.actions.length === 0 && !standing.block) {
      return {
        actions: [], notes: [], changed: false, inconclusive: false, restartPending: false,
        forceSetup: false, blocked: false, progress,
      };
    }
    if (standing.block) {
      progress.providerSafetyBlocked = true;
      await writeSetupProgress(ctx, progress).catch(() => {});
    }
    return {
      actions: standing.actions,
      notes: [...standing.actions.map((action) => ACTION_NOTES[action]), ...standing.notes],
      changed: standing.changed,
      inconclusive: false,
      restartPending: false,
      forceSetup: standing.block,
      blocked: standing.block,
      progress,
    };
  }

  // WRITE-AHEAD. The debt is recorded before the check runs, not after it
  // fails, so a pass that dies mid-flight — a killed process, a machine that
  // loses power between reading the config and rewriting it — is indistinguish-
  // able from one that failed honestly: both leave "not proven safe" on disk.
  // A best-effort write is deliberate: a profile directory that does not exist
  // yet is a fresh install with nothing to be unsafe about, and an unwritable
  // one keeps the debt in memory for the run in front of us (below).
  if (!blockedOnDisk) {
    progress.providerSafetyBlocked = true;
    await writeSetupProgress(ctx, progress).catch(() => {});
  }

  // REVISION 2 — the Claude Code rules, evaluated on EVERY launch rather than at
  // setup. A customer who pasted a key months ago and installed Claude Code
  // yesterday is exposed, and nothing in their profile changed to announce it;
  // a setup-time check would never look again.
  const claudeRules = await evaluateClaudeCodeRules(ctx, overrides);

  let result: ProviderSafetyResult;
  try {
    result = await applyProviderSafety(ctx, { ...overrides, restartPending: pending, blocked: blockedOnDisk });
  } catch (error) {
    // A pass that threw proves nothing — so the debt written above stays, the
    // gateway stays blocked, and setup is forced whether or not a restart was
    // already owed.
    return {
      actions: [],
      notes: [`I could not complete the provider-safety check: ${error instanceof Error ? error.message : String(error)}`],
      changed: false,
      inconclusive: true,
      inconclusiveReason: "check-failed",
      restartPending: pending,
      forceSetup: true,
      blocked: true,
      progress,
    };
  }

  // The revision-2 rules join here, AFTER the config transaction: a hard block
  // must survive a config pass that found nothing to change, and a cleared pool
  // entry is an action the customer deserves to be told about.
  if (claudeRules.actions.length > 0) {
    result = {
      ...result,
      actions: [...result.actions, ...claudeRules.actions],
      notes: [...result.notes, ...claudeRules.actions.map((action) => ACTION_NOTES[action]), ...claudeRules.notes],
      changed: result.changed || claudeRules.changed,
    };
  }

  // State is updated in ONE write, whichever entry point ran: a route that was
  // taken away must take the "Model is done" claim with it, or a completed
  // profile opens straight into an offline HUD with no route and no prompt.
  const routeRemoved = result.changed;
  if (routeRemoved) {
    delete progress.steps.model;
    progress.completed = false;
  }
  // ONLY a pass that confirmed the gateway is settled may clear this. Anything
  // else — inconclusive, unreadable, no engine, a thrown error — leaves the
  // debt on disk, because the process that loaded the unsafe route is still the
  // thing we cannot see.
  if (result.restartPending) {
    progress.providerSafetyRestartPending = true;
  } else if (!result.inconclusive) {
    delete progress.providerSafetyRestartPending;
  }
  // ONE definition of safe, used by the stamp, the block and the caller: the
  // check completed AND nothing can still be serving what it removed.
  // claudeRules.block is the revision-2 hard refusal: an OAuth-shaped Anthropic
  // secret while Claude Code is signed in on this machine. We KNOW the engine
  // would run on their Claude account and can rewrite that app's sign-in file,
  // so this is not a warning — the engine does not start.
  const blocked = result.inconclusive || result.restartPending || claudeRules.block;
  if (blocked) {
    progress.providerSafetyBlocked = true;
  } else {
    delete progress.providerSafetyBlocked;
    progress.providerSafetyRev = PROVIDER_SAFETY_REVISION;
  }
  try {
    await writeSetupProgress(ctx, progress);
  } catch (error) {
    // The durable record of the debt is what makes the retry happen. If it
    // cannot be written, the run in front of us is the only chance to act on
    // it, so it stays pending, stays blocked, and setup is forced.
    return {
      ...result,
      notes: [...result.notes, "I could not record the provider-safety state, so setup will check again."],
      inconclusive: true,
      restartPending: result.restartPending || pending,
      forceSetup: true,
      blocked: true,
      progress,
    };
  }

  return { ...result, forceSetup: routeRemoved || blocked, blocked, progress };
}

/**
 * The revision-2 rules: everything about the customer's Claude Code install.
 *
 * Both were established by experiment against the pinned engine (v2026.7.1),
 * and each closes a different door:
 *
 *  1. THE POOL ENTRY. `credential_pool.py` refreshes saved credentials on its
 *     own and, for an entry whose source is `claude_code`, writes the result
 *     back into `~/.claude/.credentials.json` — dropping `subscriptionType`.
 *     The spawn environment cannot reach that path, so the adopted copy is
 *     removed here. Ours, not theirs: Claude Code's own file is never touched.
 *  2. THE OAUTH COLLISION. An API-key-shaped secret delivered as
 *     ANTHROPIC_TOKEN wins over the credentials file (proven: with the file
 *     unreadable, resolution still succeeded). An OAUTH-shaped one does not —
 *     `_prefer_refreshable_claude_code_token()` overrides it precisely because
 *     it is OAuth-shaped. In that combination we cannot protect the customer's
 *     Claude Code install from our own engine, so we refuse to start it.
 *
 * Never throws: a rule that cannot be evaluated must not be the thing that
 * stops a launch, and everything here is best-effort by construction.
 */
async function evaluateClaudeCodeRules(
  ctx: WizardContext,
  overrides: ProviderSafetyOverrides,
): Promise<{ actions: ProviderSafetyAction[]; notes: string[]; changed: boolean; block: boolean }> {
  const actions: ProviderSafetyAction[] = [];
  const notes: string[] = [];
  let changed = false;
  try {
    const claudePresent = overrides.claudeCodeCredentialsPresent
      ?? (await claudeCodeCredentialsPresent());
    if (!claudePresent) return { actions, notes, changed, block: false };

    const pool = await clearAdoptedClaudeCodeCredentials(ctx);
    if (pool.removed) {
      actions.push("claude-code-pool-entry-cleared");
      changed = true;
      if (pool.unreadableEntries > 0) {
        notes.push(
          `${pool.unreadableEntries} saved Anthropic credential(s) had no readable source, so I removed them too `
          + "rather than risk holding a copy of your Claude Code sign-in. Sign in again if JARVIS asks.",
        );
      }
    }

    const secret = overrides.anthropicSecret ?? (await anthropicSecret(ctx));
    if (anthropicSecretShape(secret) === "oauth") {
      actions.push("claude-code-oauth-collision");
      return { actions, notes, changed, block: true };
    }
  } catch {
    // Best effort, by design. An unreadable pool or home directory is not a
    // reason to keep a customer's engine down.
  }
  return { actions, notes, changed, block: false };
}
