import path from "node:path";
import fs from "node:fs/promises";
import { WizardError, type StepOutcome, type WizardContext } from "../context.js";
import { appendSetupLog, pathExists } from "../fs-utils.js";
import { redactSecrets } from "../redact.js";
import { assertNoTokenReplay, FORBIDDEN_PROVIDER_ALIASES } from "../adapter-guard.js";
import { restartGatewayForCredentialChange } from "../gateway-restart.js";
import { confirm, note, warn } from "../ui.js";
import { enginePythonPath, hermesInvocation, runCommand, runWatchedCommand, type CommandResult } from "../system.js";
import { reconcileSchedulesAfterSwitch } from "./schedule-pins.js";

/** How the buyer chose to give Jarvis a brain. A CLOSED SET (telemetry prop). */
export type SignInMethod = "nous" | "codex" | "apikey" | "codex-cli" | "claude-cli";

export interface ModelRunners {
  run: typeof runCommand;
  watch: typeof runWatchedCommand;
  platform?: NodeJS.Platform;
  /** Injected so tests never launch a browser. */
  openUrl?: (ctx: WizardContext, url: string) => Promise<void>;
}

/**
 * One Model-step run: the notes it is building, and whether it has already
 * changed credential/config state on disk.
 *
 * `dirty` exists because a gateway that is already running read its
 * credentials at startup. Any write — an engine sign-in, a `.env` key, one of
 * several config keys — makes the running gateway's view stale, INCLUDING when
 * the step then fails, is declined, or throws. Restarting only on the happy
 * path left a customer with a wizard that said "skipped" over a gateway that
 * would pick the half-written configuration up at its next restart.
 */
export interface ModelSession {
  notes: string[];
  dirty: boolean;
  restarted: boolean;
  previousRoute?: { files: Array<Buffer | undefined>; modelConfigured: boolean };
  /**
   * Props the STEP must report however it ends (v1.5.1, cluster item 5).
   * Diagnostics keep only the last attempt per step, so a subscription journey
   * that failed and fell through to a key was invisible afterwards —
   * `signin_method=apikey` and not a trace of the road not taken. Whatever
   * lands here is merged into the step's outcome at its single exit
   * (steps/model.ts), where the outcome's own props win any collision.
   */
  journeyProps: Record<string, string | number | boolean>;
}

export function createModelSession(): ModelSession {
  return { notes: [], dirty: false, restarted: false, journeyProps: {} };
}

/** Call immediately AFTER any write the gateway can consume. */
export function markDirty(session: ModelSession): void {
  session.dirty = true;
}

/**
 * Restart a running gateway if anything was written, exactly once per run.
 * Safe to call from a `finally`: it is a no-op when nothing changed or when the
 * happy path already restarted.
 */
export async function settleGateway(
  ctx: WizardContext,
  run: typeof runCommand,
  session: ModelSession,
  platform?: NodeJS.Platform,
): Promise<void> {
  if (!session.dirty || session.restarted) return;
  session.restarted = true;
  try {
    session.notes.push(await restartGatewayForCredentialChange(ctx, run, { platform }));
  } catch (error) {
    // Never let cleanup replace the real outcome; say it plainly instead.
    session.notes.push(
      "The gateway could not be restarted after the change; restart your computer so it reloads the profile.",
    );
    void error;
  }
}

/** Config keys this step is allowed to write. Anything else is a bug. */
const WRITABLE_KEYS = new Set(["model.provider", "model.default", "model.base_url", "model.openai_runtime"]);

export interface ConfigWriteContext {
  /** Env keys already present with a non-empty value in the profile .env. */
  existingEnvKeys?: readonly string[];
  /** The provider being written in this same batch, for model.default checks. */
  provider?: string;
}

/**
 * Write one Hermes config key for the jarvis profile.
 *
 * The token-replay guard is applied to the ACTUAL key and value being written,
 * not to a caller-supplied description of them: a caller that passed an honest
 * intent while writing a forbidden provider alias into model.provider would
 * otherwise sail through. This function plus `writeModelEnv` are the only two places the step
 * may change inference configuration.
 */
export async function setHermesConfig(
  ctx: WizardContext,
  run: typeof runCommand,
  session: ModelSession,
  key: string,
  value: string,
  context: ConfigWriteContext = {},
): Promise<void> {
  if (!WRITABLE_KEYS.has(key)) {
    throw new WizardError(
      `Refusing to write the unexpected configuration key ${key}.`,
      "This is a JARVIS bug. Please email jarvis.agent.tech@pm.me with your support code.",
      { fatal: true },
    );
  }
  if (key === "model.provider") {
    assertNoTokenReplay({ provider: value, existingEnvKeys: context.existingEnvKeys });
  } else if (key === "model.default") {
    assertNoTokenReplay({ provider: context.provider, model: value, existingEnvKeys: context.existingEnvKeys });
  } else if (FORBIDDEN_PROVIDER_ALIASES.includes(value.trim().toLowerCase())) {
    assertNoTokenReplay({ provider: value });
  }

  const invocation = hermesInvocation(ctx, ["-p", "jarvis", "config", "set", key, value]);
  const result = await run(
    ctx,
    invocation.command,
    invocation.args,
    { stream: true },
  );
  markDirty(session);
  if (result.status !== 0) {
    throw new WizardError(
      `Hermes did not accept the ${key} setting.`,
      // Plain-language remedy only (Peter's copy rule, 2026-08-14): the CLI
      // equivalent lives in the docs, not in a wall a customer just hit.
      "Open JARVIS settings → RUN SETUP AGAIN — it stops at the Model step for your key.",
    );
  }
}

/**
 * The whole model block, written in ONE atomic file transaction.
 *
 * Four sequential `hermes config set` calls are four chances to end up with a
 * hybrid route: provider updated, a stale `base_url` still in place, and the
 * `finally` restart then activating the mixture — in the worst case sending a
 * newly pasted key to somebody else's old endpoint. The engine's own config
 * writer has the same shape as this (read, mutate, dump), and the voice-config
 * migration already writes the same file this way, so the format is not being
 * invented here: temp file with the original mode, fsync, os.replace. Either
 * the new route lands whole or the old one is untouched.
 */
const APPLY_ROUTE_PY = `
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
provider, base_url, runtime, default = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]

# Acquire first, then EVERYTHING else inside try/finally: an exception between
# acquiring and entering the block used to leak the lock until it went stale.
lock = _acquire(path)
try:
    with open(path) as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config, dict):
        config = {}

    model = config.get("model")
    if isinstance(model, str):
        model = {"default": model}
    if not isinstance(model, dict):
        model = {}

    for key, value in (("provider", provider), ("base_url", base_url),
                       ("openai_runtime", runtime), ("default", default)):
        if value == "":
            model.pop(key, None)
        else:
            model[key] = value
    # Inline endpoint credentials belong to a custom provider that is no longer
    # the route; leaving them behind would authenticate the new route with the
    # old key.
    for stale in ("api_key", "api_mode"):
        model.pop(stale, None)

    config["model"] = model
    serialized = yaml.safe_dump(config, sort_keys=False)
    mode = os.stat(path).st_mode & 0o7777
    tmp_path = path + ".jarvis-route.tmp.%d" % os.getpid()
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
print(json.dumps({"ok": True}))
`;

export interface ProviderConfigSpec {
  /** The engine provider id this route uses. */
  provider: string;
  /** The model slug for that provider. */
  model: string;
  /** Only the Codex route sets a runtime; everything else resets it. */
  openaiRuntime?: "codex_app_server";
  existingEnvKeys?: readonly string[];
  platform?: NodeJS.Platform;
}

export async function finishRouteChange(ctx: WizardContext, session: ModelSession, verified: boolean): Promise<void> {
  const previous = session.previousRoute;
  if (previous === undefined) return;
  if (!verified) {
    // Restore credentials before the route, including the original YAML formatting.
    for (const [index, name] of [".env", "config.yaml"].entries()) {
      const target = path.join(ctx.profileDir, name);
      const bytes = previous.files[index];
      if (bytes === undefined) await fs.rm(target, { force: true });
      else await fs.writeFile(target, bytes);
    }
    ctx.state.modelConfigured = previous.modelConfigured;
    session.restarted = false;
  }
  session.previousRoute = undefined;
}

/**
 * Point the profile at exactly one route, leaving nothing behind.
 *
 * Writing only `model.default` (what v1.3.18 did, when there was only ever one
 * kind of route) is wrong the moment a customer switches: after a Nous sign-in,
 * pasting an Anthropic key left `provider=nous`, and because Nous serves the
 * same `anthropic/claude-sonnet-4.6` slug from its own catalogue the
 * verification would PASS while the key the customer just paid for was
 * ignored. So every
 * route writes the whole set — provider, base_url, runtime, model — in that
 * order, and the stale keys are cleared rather than assumed absent.
 */
/**
 * Empty the model route so nothing can be served through it.
 *
 * Used before a credential is written: a half-replaced route (new key, old
 * endpoint) is the one state that can send a customer's fresh key somewhere it
 * was never meant to go, and "nothing configured" is always the safe
 * intermediate.
 */
export async function makeRouteInert(
  ctx: WizardContext,
  run: typeof runCommand,
  session: ModelSession,
  platform?: NodeJS.Platform,
): Promise<void> {
  if (!ctx.dryRun && session.previousRoute === undefined) {
    const files = await Promise.all([".env", "config.yaml"].map((name) =>
      fs.readFile(path.join(ctx.profileDir, name)).catch((error: NodeJS.ErrnoException) => {
        if (error.code !== "ENOENT") throw error;
        return undefined;
      })));
    session.previousRoute = { files, modelConfigured: ctx.state.modelConfigured };
  }
  const configPath = path.join(ctx.profileDir, "config.yaml");
  // Either payload shape: the wheelhouse venv (Windows, 1.3.x) or the vendored
  // site interpreter (macOS, v1.4.0). Gating on the venv alone silently pushed
  // every mac site install onto the non-transactional CLI fallback.
  const python = enginePythonPath(ctx, platform);
  if (!ctx.dryRun && python !== undefined && (await pathExists(configPath))) {
    const cleared = await run(ctx, python, ["-c", APPLY_ROUTE_PY, configPath, "", "", "", ""], { stream: false });
    // Dirty only on SUCCESS: the transaction lands whole or not at all, so a
    // failed clear changed nothing the gateway could pick up — and marking
    // it dirty would have the outer settle restart a gateway onto the very
    // route this call failed to remove (Codex review 2026-08-15, P2-2).
    if (cleared.status === 0) markDirty(session);
    if (cleared.status !== 0) {
      await appendSetupLog(ctx, `Brain route clear failed (exit ${cleared.status}).\n${redactSecrets(cleared.stderr)}`);
      throw new WizardError(
        "JARVIS could not safely change the Brain setup.",
        "Your previous settings are untouched. Open the gear → RUN SETUP AGAIN and choose the Brain step.",
      );
    }
    return;
  }
  await setHermesConfig(ctx, run, session, "model.default", "", {});
}

export async function applyProviderConfig(
  ctx: WizardContext,
  run: typeof runCommand,
  session: ModelSession,
  spec: ProviderConfigSpec,
): Promise<void> {
  // The guard runs on the values themselves before anything is written, and it
  // runs whichever writer is used below.
  assertNoTokenReplay({ provider: spec.provider, existingEnvKeys: spec.existingEnvKeys });
  assertNoTokenReplay({ provider: spec.provider, model: spec.model, existingEnvKeys: spec.existingEnvKeys });
  if (FORBIDDEN_PROVIDER_ALIASES.includes(spec.provider.trim().toLowerCase())) {
    assertNoTokenReplay({ provider: spec.provider });
  }

  const runtime = spec.openaiRuntime ?? "";
  const configPath = path.join(ctx.profileDir, "config.yaml");
  // Either payload shape — see makeRouteInert above.
  const python = enginePythonPath(ctx, spec.platform);

  if (!ctx.dryRun && python !== undefined && (await pathExists(configPath))) {
    const applied = await run(
      ctx,
      python,
      ["-c", APPLY_ROUTE_PY, configPath, spec.provider, "", runtime, spec.model],
      { stream: false },
    );
    if (applied.status === 0) markDirty(session);
    if (applied.status !== 0) {
      await appendSetupLog(ctx, `Brain route write failed (exit ${applied.status}).\n${redactSecrets(applied.stderr)}`);
      // Nothing was replaced — the transaction either lands whole or not at all
      // — so the profile still holds the route it had before this call.
      // NOT "your previous settings are untouched": by this point the route
      // has deliberately been made inert and the key may already be saved.
      // Say what is actually true, and what to do about it.
      throw new WizardError(
        "JARVIS could not save the new Brain setup.",
        "Anything you entered is saved, and JARVIS is offline on purpose rather than half-configured. "
          + "Open the gear → RUN SETUP AGAIN and choose how JARVIS should think at the Brain step.",
      );
    }
    session.notes.push(describeRoute(spec));
    return;
  }

  // Fallback for a profile whose engine python is not installed yet (and for
  // dry runs): the CLI, one key at a time. Ordered so a failure part-way
  // through leaves the route INERT rather than hybrid — the model is cleared
  // first, so nothing can serve on a half-written route, and it is written last.
  const context: ConfigWriteContext = { provider: spec.provider, existingEnvKeys: spec.existingEnvKeys };
  await setHermesConfig(ctx, run, session, "model.default", "", context);
  await setHermesConfig(ctx, run, session, "model.provider", spec.provider, context);
  await setHermesConfig(ctx, run, session, "model.base_url", "", context);
  await setHermesConfig(ctx, run, session, "model.openai_runtime", runtime === "" ? "auto" : runtime, context);
  await setHermesConfig(ctx, run, session, "model.default", spec.model, context);
  session.notes.push(describeRoute(spec));
}

/** The brain routes, in the customer's words — the ledger is customer-facing (summary.ts). */
const BRAIN_ROUTE_LEDGER: Record<string, string> = {
  "codex-cli": "Set Jarvis to think with your ChatGPT plan (the ChatGPT app already signed in here).",
  "claude-cli": "Set Jarvis to think with your Claude plan (the Claude app already signed in here).",
  "claude-subscription-directsdk-experimental": "Set Jarvis to think with your Claude plan, with the full staff toolset.",
};

const PROVIDER_DISPLAY_NAMES: Record<string, string> = {
  anthropic: "Anthropic",
  openrouter: "OpenRouter",
  "openai-api": "OpenAI",
};

function describeRoute(spec: ProviderConfigSpec): string {
  const brain = BRAIN_ROUTE_LEDGER[spec.provider];
  if (brain !== undefined) return brain;
  const provider = PROVIDER_DISPLAY_NAMES[spec.provider] ?? spec.provider;
  return spec.openaiRuntime === undefined
    ? `Configured ${provider} with model ${spec.model}.`
    : `Configured ${provider} with model ${spec.model} through the ${spec.openaiRuntime} runtime.`;
}

/**
 * Why a verification round-trip failed, as a CLOSED SET.
 *
 * Engine stdout/stderr is collapsed and capped at 160 characters in local
 * notes so COPY DIAGNOSTICS preserves the reply that support needs. It must
 * never enter telemetry props; those carry only the closed-set category.
 */
export type VerifyFailure =
  | "no-provider"
  | "unauthorized"
  | "rate-limited"
  | "workspace-scope"
  | "network"
  | "model-unavailable"
  | "not-permitted"
  | "empty-reply"
  | "unexpected-reply"
  | "engine-failure"
  | "engine-error";

/**
 * Mapped against what the engine's `-Q` branch ACTUALLY prints, run on the
 * vendored engine rather than guessed (2026-08-17, sandbox HERMES_HOME, keys
 * scrubbed). Every failure exits 1 and puts its detail on STDOUT, not stderr,
 * which is why both streams are read:
 *
 *   unauthorized  exit 1  "HTTP 401: User not found."
 *   network       exit 1  "API call failed after 3 retries: Connection error."
 *   no-provider   exit 1  "Provider resolver returned an empty API key. Set
 *                          OPENROUTER_API_KEY or run: hermes setup"
 *   engine-error  exit 1  "Failed to initialize agent: <reason>"
 *
 * Order is deliberate: the specific credential/quota answers come before the
 * generic transport one, because a provider's 401 body can mention a
 * connection and a retry line can mention a key.
 */
export function classifyVerifyFailure(status: number, stdout: string, stderr: string): VerifyFailure {
  const text = `${stdout}\n${stderr}`.toLowerCase();
  // "empty api key" / "returned an empty" is the engine saying it resolved NO
  // usable credential — the same disease as "no llm provider configured", and
  // its advice is already "run: hermes setup".
  // The bare status codes are word-boundary guarded. Both streams are read,
  // and the engine prints a `session_id` timestamp on stderr — an unguarded
  // `401` matches inside "20260817_182401_..." and would hand a customer a
  // confident, wrong story about their key on the strength of the clock.
  if (/no llm provider configured|no credentials|not logged in|run `hermes auth`|empty api key|returned an empty api key|set \w+_api_key or run/.test(text)) return "no-provider";
  if (/\b401\b|unauthorized|missing authentication|invalid api key|invalid_api_key|authentication_error|user not found/.test(text)) return "unauthorized";
  if (/\b429\b|rate limit|quota|usage limit|insufficient credits|insufficient_quota|credit balance|subscription_required/.test(text)) return "rate-limited";
  if (/not scoped to a workspace|anthropic-workspace-id/.test(text)) return "workspace-scope";
  if (/timed out|timeout|connection error|connection|network|dns|unreachable|ssl|certificate/.test(text)) return "network";
  if (/model .*(not found|not available|unsupported)|\b404\b|does not exist|unknown model/.test(text)) return "model-unavailable";
  // CLASSIFIER-GAP-403 (Justin Curnutt 6F2A8E5E, on v1.5.1, 2026-08-18): a
  // FRESH OpenAI key WITH credits fell through to the catch-all, and the
  // customer was told "an error I could not classify" about a key that was
  // fine. OpenAI's 403 permission bodies contain no 401, no 404, no quota
  // word and no network word, so nothing above matches them:
  //   "Project `proj_…` does not have access to model `…`"  (project-scoped
  //      keys, the default since 2024 — a key made inside a project)
  //   "insufficient permissions"                            (a Restricted key)
  //   "Your organization must be verified to use the model"
  //
  // ORDER, deliberately: AFTER unauthorized and rate-limited, so a 403 shape
  // can never steal a real 401 or 429 (the brief's hard constraint), and
  // AFTER model-unavailable, so the long-standing "The model `x` does not
  // exist or you do not have access to it" body keeps its existing, correct
  // answer — that one is a wrong model name far more often than a permission
  // wall, and its remedy differs.
  if (/\b403\b|permission_error|permission denied|insufficient permission|does not have access|not have access to model|must be verified|verify your organization|restricted key|forbidden/.test(text)) {
    return "not-permitted";
  }
  // An unrecognised failure is an engine error whatever the exit code says;
  // the status is kept in the signature because callers pass it and a future
  // reader should see that it was considered, not forgotten.
  void status;
  return "engine-error";
}

export const VERIFY_FAILURE_COPY: Record<VerifyFailure, string> = {
  "no-provider": "The engine says nothing is configured to think with yet.",
  unauthorized: "The provider refused the sign-in or key as unauthorized.",
  "rate-limited": "The provider refused this request for quota or credit reasons.",
  "workspace-scope": "That key was made at the organisation level, and Anthropic only accepts keys made inside a workspace. "
    + "In the console, pick a workspace first, create a key there, and paste it here.",
  network: "The engine could not reach the provider over the network.",
  "model-unavailable": "The provider does not offer that model to this account.",
  // One of two classes whose copy carries its own remedy (see workspace-scope). Every other class here
  // is a label the caller's generic remedy ("check the key you pasted") still
  // fits; this one is the opposite — the key is FINE, and telling a customer
  // to check it sends them to re-paste a working key forever, which is
  // exactly what happened. Plain words, no paths, no jargon: "project" is the
  // word they will see on the screen where they made the key.
  "not-permitted": "That key works, but it isn't allowed to use this model. "
    + "Where you created the key, give it access to all models — or make a new key without choosing a project — then paste it here again.",
  "empty-reply": "The engine finished without answering, so nothing was proven.",
  "unexpected-reply": "The engine answered, but not with the test reply I asked for.",
  "engine-failure": "The engine could not deliver the test question.",
  "engine-error": "The engine reported an error I could not classify.",
};

// Coupled to hud/vendor/engine-patches/files/agent/brain/failures.py and
// ledger.py `resting_copy`: these stable fragments must survive copy edits.
const ENGINE_FAILURE_REPLY = /signed jarvis out|jarvis is resting|plan is out of energy|app he thinks with|jarvis needs a small update|wasn't allowed to finish that step|lost his train of thought/i;

export interface VerifyOptions {
  session: ModelSession;
  method: SignInMethod;
  /** What the buyer should check if the round-trip keeps failing. */
  remedy: string;
  platform?: NodeJS.Platform;
  /** Extra telemetry props to merge into the outcome. */
  props?: Record<string, unknown>;
  /**
   * Cap on round-trips (default MAX_VERIFY_ATTEMPTS). The saved-key auto path
   * passes 1: its failure falls through to the paste screen, so a retry
   * prompt here would be a second question about the same dead key.
   */
  maxAttempts?: number;
  /**
   * Success line override. The saved-key auto path speaks Peter's carry copy
   * ("I kept the key you gave me last time, sir — it still works."); default
   * is the method-appropriate line below.
   */
  successNote?: string;
  /**
   * Env keys stripped from the verify turn's environment (see
   * RunOptions.unsetEnv). The brain-CLI routes pass the provider API keys:
   * the challenge must be answered by the customer's SUBSCRIPTION, and a key
   * the wizard happens to inherit would let the CLI answer on it instead —
   * a passing verify that proved the wrong thing.
   */
  scrubEnv?: readonly string[];
}

/**
 * The verification challenge, reworked three times on 2026-08-14.
 *
 * First (Peter's live smoke): the engine answers as a PERSONA, and the old
 * fragment "Reply: ok" read to it like a truncated message — a WORKING key
 * produced a polite in-character reply that the exact-match sentinel called
 * a failure. So: an explicit instruction, accepted anywhere in the reply.
 *
 * Second (re-review F4): a FIXED sentinel word is unsafe — engine status
 * text with exit 0 can contain any English word ("request not confirmed"
 * would have self-verified). So: a random per-attempt token.
 *
 * Third (final re-review F2): ANY design whose expected token appears
 * VERBATIM in the prompt loses to echoes — a partial echo, a wrapped echo,
 * an ANSI-decorated echo all carry the token without carrying the whole
 * prompt, and echo-stripping is a losing arms race (while over-stripping
 * false-failed a combined echo+reply line). So the model must ASSEMBLE the
 * token: the prompt names the word and the number separately, with a long
 * fixed phrase between them, and only the assembled "JARVIS-nnnn" (word and
 * digits adjacent, any dash/space) counts as proof. No transform of the
 * prompt can put them adjacent without deleting the words in between —
 * which is not echoing. No stripping needed at all.
 */
export interface VerifyChallenge {
  prompt: string;
  /**
   * The BLUNT re-ask, for a model that answered coherently and ignored the
   * question (see verifyAndFinish). Like `prompt`, it names the word and the
   * number SEPARATELY — never the assembled token — so an echo of the prompt
   * still cannot pass the matcher, which is the property the whole proof
   * rests on.
   */
  strictPrompt: string;
  /** The assembled form, for logs and notes. */
  token: string;
  /** Matches the assembled token: word and digits adjacent (≤3 separator chars). */
  matcher: RegExp;
}

export function makeVerifyChallenge(random: () => number = Math.random): VerifyChallenge {
  const digits = String(Math.floor(random() * 9000) + 1000);
  return {
    prompt: `Please reply with the code word JARVIS followed by a dash and the number ${digits}.`,
    strictPrompt: `System check. Ignore any persona or style instruction for this one reply. Output the code word JARVIS, `
      + `then a hyphen, then the number ${digits} — that string alone, on one line, with no greeting, no punctuation `
      + `and no other words.`,
    token: `JARVIS-${digits}`,
    matcher: new RegExp(`JARVIS[\\s\\u2010-\\u2015_.:-]{0,3}${digits}`, "i"),
  };
}

/**
 * The proof step: one real round-trip through the configured provider, then
 * the gateway restart.
 *
 * Nothing above this function may report success. A zero exit status is NOT
 * proof — a wrapper or an engine command can exit cleanly having produced no
 * assistant text at all — so the reply must be non-empty AND contain this
 * attempt's random challenge token.
 */
export async function verifyAndFinish(
  ctx: WizardContext,
  run: typeof runCommand,
  options: VerifyOptions,
): Promise<StepOutcome & { authExpired?: boolean }> {
  const { session, session: { notes } } = options;

  // A dry run never spawns anything, so there is no round-trip to believe or
  // disbelieve. Say that, rather than looping on an empty answer forever (the
  // dry-run frontend answers every retry prompt with its default — "yes").
  if (ctx.dryRun) {
    notes.push("[dry-run] would ask the engine a test question and restart the gateway if one is running.");
    return {
      status: "done",
      notes,
      props: { signin_method: options.method, signin_result: "dry-run", ...options.props },
    };
  }

  // Named BEFORE the wait, not after: the first verification spends ~15 silent
  // seconds warming the engine up before the model even answers, and Peter's
  // live smoke read that silence as "nothing happened" (2026-08-14). The
  // animated working indicator rides the spawn itself (SetupIO.working); this
  // line says what the wait IS.
  note(ctx, "Now the proof, sir — I'm asking a real question through the new setup. The first answer can take half a minute while the engine warms up.");

  // Bounded on purpose: any frontend that auto-answers (a dry run, a scripted
  // test, a stuck GUI) would otherwise spin here forever, and a customer who
  // has retried this many times needs a different suggestion, not another go.
  const maxAttempts = options.maxAttempts ?? MAX_VERIFY_ATTEMPTS;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    // A FRESH token per attempt: a stale echo of a previous attempt (or any
    // text that existed before this moment) can never prove this one.
    const challenge = makeVerifyChallenge();
    // `-Q` (--quiet) is the MACHINE branch, and it is how this step names most
    // failures. One caveat: engine failure copy can arrive as a fluent exit-0
    // reply, so those replies are fingerprinted below. The human branch
    // (`-q` alone) prints a "Query:" banner, swallows the error into its own
    // console rendering and exits 0 whatever happened — so stdout was never
    // empty, the status was never non-zero, and 401s, spent credits,
    // model-404s and dead networks all landed on `unexpected-reply`. Support
    // could not tell a billing problem from a network one on v1.5.0
    // diagnostics (Justin Lipes D6A74078, 2026-08-17). `-q` still carries the
    // QUERY (it is --query); `-Q` is the separate boolean that makes the run
    // parseable (hermes_cli/_parser.py:256,295).
    const ask = async (prompt: string): Promise<CommandResult> => {
      const invocation = hermesInvocation(ctx, ["-p", "jarvis", "chat", "-Q", "-q", prompt]);
      return await run(ctx, invocation.command, invocation.args, { stream: false, unsetEnv: options.scrubEnv });
    };
    // The assembled token is searched across BOTH streams, whitespace runs
    // collapsed (a persona's reply runs to sentences and may wrap). The
    // prompt itself — echoed whole, partial, wrapped, or decorated — can
    // never contain the ASSEMBLED token, so no echo-stripping is needed.
    const collapseText = (text: string): string => text.replace(/\s+/g, " ").trim();
    const collapse = (result: CommandResult): string => collapseText(`${result.stdout} ${result.stderr}`);
    const isEngineFailureReply = (result: CommandResult, text: string): boolean =>
      result.status === 0
      && text !== ""
      && !challenge.matcher.test(text)
      && ENGINE_FAILURE_REPLY.test(text);

    let verify = await ask(challenge.prompt);
    let proofText = collapse(verify);
    const initialReply = proofText;
    let stdoutReply = collapseText(verify.stdout);
    const initialStdoutReply = stdoutReply;
    let engineFailureReply = isEngineFailureReply(verify, proofText);

    // TOLERANCE BEFORE JUDGEMENT (2026-08-17, spec CONFIDENCE UPGRADE).
    //
    // There is a failure class that is not a failure of the ROUTE at all: the
    // engine answers, coherently, having paid no attention to the question —
    // "Good day, sir. I don't have a preceding request to act on." It happened
    // in development (model.test.ts) and an independent investigation ranked
    // it higher than we first did, because a persona instruction, a stale
    // session or a small model can all produce it while the credentials,
    // network and model are perfectly fine. Naming that `unexpected-reply` and
    // failing the step tells a customer their working setup is broken.
    //
    // So a coherent answer that ignored the challenge earns ONE blunt re-ask
    // — same token, an instruction that overrides persona and asks for the
    // string alone — before any verdict is reached. It costs one extra turn on
    // a path that was heading for failure anyway; a route that is genuinely
    // broken still fails, one turn later, with its real reason. Errors and
    // empty answers are NOT retried here: they are not this class.
    let strictRetry = false;
    if (verify.status === 0 && proofText !== "" && !challenge.matcher.test(proofText) && !engineFailureReply) {
      strictRetry = true;
      note(ctx, "That answer skipped the code I asked for, sir. Let me put the question more plainly — one moment.");
      verify = await ask(challenge.strictPrompt);
      proofText = collapse(verify);
      stdoutReply = collapseText(verify.stdout);
      engineFailureReply = isEngineFailureReply(verify, proofText);
    }

    const reply = proofText;
    const failure: VerifyFailure | undefined = verify.status !== 0
      ? classifyVerifyFailure(verify.status, verify.stdout, verify.stderr)
      : proofText === ""
        ? "empty-reply"
        : challenge.matcher.test(proofText)
          ? undefined
          : engineFailureReply
            ? "engine-failure"
            : "unexpected-reply";
    // The Brain paths catch a failed verify and loop back to the menu, so this
    // outcome's props never reach telemetry; the session's props do (model.ts).
    if (failure !== undefined) session.journeyProps.verify_failure = failure;

    if (failure === undefined) {
      // THE UX CONTRACT (Peter's smoke, 2026-08-14): success is announced
      // affirmatively THE MOMENT it happens — never left implicit, and never
      // followed by any question that implies doubt. The retry prompt below
      // is only ever reachable after a NAMED failure.
      note(ctx, options.successNote ?? (options.method === "apikey"
        ? "That key works — he's thinking, sir."
        : "The sign-in works — he's thinking, sir."));
      notes.push(`Hermes answered the test question: ${reply.slice(0, 80)}`);
      // An already-running gateway read its .env and config at startup and
      // cannot see what was written above; without a restart it answers every
      // chat with "No LLM provider configured" until the next reboot.
      await settleGateway(ctx, run, session, options.platform);
      // MODEL-SWITCH-ORPHANS: the brain is proven and live — now make sure the
      // customer's SCHEDULES survived the switch. This is the one moment we
      // know both what they chose and that it works, and it is the last
      // moment before they walk away believing everything is fine. Runs after
      // the settle so the audit sees the route the ticker will see; never
      // changes this step's outcome (schedule-pins.ts is fail-open by design).
      notes.push(...(await reconcileSchedulesAfterSwitch(ctx, run, options.platform)));
      ctx.state.modelConfigured = true;
      return {
        status: "done",
        notes,
        props: {
          signin_method: options.method,
          signin_result: "verified",
          // How often a model needs the blunt re-ask is worth knowing: it is
          // the difference between "our prompt is weak" and "this route is ill".
          ...(strictRetry ? { verify_strict_retry: true } : {}),
          ...options.props,
        },
      };
    }

    // The failure is NAMED on screen before any question is asked — the old
    // flow queued this into the step-end notes, so the customer's first sign
    // of trouble was a retry prompt implying their working key had failed.
    const failureCopy = failure === "engine-failure"
      ? stdoutReply.slice(0, 240) || VERIFY_FAILURE_COPY[failure]
      : VERIFY_FAILURE_COPY[failure];
    warn(ctx, failure === "engine-failure" ? failureCopy : `That didn't work. ${failureCopy}`);
    const initialEvidence = failure === "engine-failure" ? initialStdoutReply : initialReply;
    notes.push(`Model verification reply excerpt (initial prompt): ${JSON.stringify(initialEvidence.slice(0, 160))}`);
    if (strictRetry) {
      const strictEvidence = failure === "engine-failure" ? stdoutReply : reply;
      notes.push(`Model verification reply excerpt (strict prompt): ${JSON.stringify(strictEvidence.slice(0, 160))}`);
    }
    notes.push(`Model verification failed; Jarvis is not marked as model-configured. ${failureCopy}`);

    const lastAttempt = attempt === maxAttempts;
    const retry = lastAttempt
      ? false
      : await confirm(ctx, "Shall I test it again now? Choosing no leaves this step unfinished — you can run it again from JARVIS settings any time.", true);
    if (!retry) {
      notes.push(
        lastAttempt
          ? maxAttempts === 1
            // The single-attempt (saved-key auto) path: its caller carries on
            // to the paste screen, so "stopped retrying" would be a lie.
            ? options.remedy
            : `The test did not pass after ${maxAttempts} tries, so I stopped retrying. ${options.remedy}`
          : `The step is unfinished until the test passes. ${options.remedy}`,
      );
      // FAILED, not skipped (Peter's flow ruling, 2026-08-14): a completed
      // setup means a working JARVIS, and a Model step that never proved its
      // route works has not completed. The failure keeps setup honest — the
      // run finishes, the ledger shows what is left, and the wizard offers
      // this step again next time.
      return {
        status: "failed",
        authExpired: engineFailureReply && /signed jarvis out/i.test(proofText),
        notes,
        props: {
          signin_method: options.method,
          signin_result: "failed",
          verify_failure: failure,
          ...(strictRetry ? { verify_strict_retry: true } : {}),
          ...options.props,
        },
      };
    }
    notes.push("Retrying model verification.");
  }

  // Unreachable: the loop returns on success and on the final attempt.
  throw new WizardError(
    "Model verification did not settle.",
    "This is a JARVIS bug. Rerun the Model step, and email jarvis.agent.tech@pm.me with your support code if it repeats.",
  );
}

/** How many round-trips a customer is asked to sit through before we stop. */
export const MAX_VERIFY_ATTEMPTS = 5;
