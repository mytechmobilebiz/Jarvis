import path from "node:path";
import { type StepOutcome, type WizardContext } from "../context.js";
import { readEnvFile, removeEnv, upsertEnv } from "../env-file.js";
import { pathExists, readTextIfExists } from "../fs-utils.js";
import { readModelDefault } from "./profile.js";
import type { SecretPromptGuide } from "../io.js";
import { anthropicSecretShape, classifyKeyShape, claudeCodeCredentialsPresent } from "../provider-safety.js";
import { confirm, note, promptSecret, select, warn } from "../ui.js";
import { gitBashPresent, openUrl as defaultOpenUrl, runCommand, runWatchedCommand } from "../system.js";
import {
  applyProviderConfig,
  makeRouteInert,
  finishRouteChange,
  createModelSession,
  markDirty,
  settleGateway,
  verifyAndFinish,
  type ModelRunners,
  type ModelSession,
  type SignInMethod,
  type VerifyOptions,
} from "./model-config.js";
import {
  BRAIN_VERIFY_SCRUB_ENV,
  CODEX_ASK_AGAIN,
  repairManagedCodex,
  runBrainVendor,
  writeBrainConfigWithRecovery,
  type BrainFlowDeps,
} from "./model-brain.js";

type ProviderKey = "anthropic" | "openrouter" | "openai";

/**
 * Default model slugs, and why they are the 4.6 family (F3, ZIP-TRIAGE-260814
 * + live fact-finding 2026-08-15):
 *
 * `anthropic/claude-sonnet-5` was NOT stale — the engine strips the provider
 * prefix (anthropic_adapter.normalize_model_name) and `claude-sonnet-5` is a
 * valid current Anthropic model ID, verified live against /v1/models. But
 * model availability is ACCOUNT-scoped: the same slug that passed Peter's
 * walkthrough 404'd on a customer's key (Kevin Dunn), whose /v1/models
 * listing evidently does not carry it — which is also why his context cap
 * read 48k (unknown model → the engine's 200k fallback × 4 × 0.06).
 *
 * A hard-coded default that gates setup completion must be a model EVERY key
 * can serve. `claude-sonnet-4.6` is: it is in every account's model list, it
 * is in the pinned engine's own catalog (model_metadata.py — so the 1M
 * context window resolves without a live probe and nothing truncates), and it
 * is the default the engine's own model picker ships. Dots, not dashes, on
 * purpose: the engine normalizes `claude-sonnet-4.6` → `claude-sonnet-4-6`
 * on the Anthropic wire, and OpenRouter's catalog uses the dotted form
 * as-is. A drift guard in model.test.ts pins each Claude default to the
 * vendored engine's catalog.
 */
export const PROVIDERS: Record<ProviderKey, {
  label: string;
  envKey: string;
  model: string;
  configProvider?: string;
}> = {
  anthropic: {
    label: "Anthropic",
    envKey: "ANTHROPIC_API_KEY",
    model: "anthropic/claude-sonnet-4.6",
  },
  openrouter: {
    label: "OpenRouter",
    envKey: "OPENROUTER_API_KEY",
    model: "anthropic/claude-sonnet-4.6",
  },
  openai: {
    label: "OpenAI",
    envKey: "OPENAI_API_KEY",
    model: "gpt-5.5",
    configProvider: "openai-api",
  },
};

/** Exported for the default-slug pin + engine-catalog drift tests. */
export const DEFAULT_PROVIDER_MODELS: Record<ProviderKey, string> = {
  anthropic: PROVIDERS.anthropic.model,
  openrouter: PROVIDERS.openrouter.model,
  openai: PROVIDERS.openai.model,
};

/**
 * Buyers who stall here overwhelmingly do not have a key yet — they have never
 * minted one. Shown on the hub page rather than a per-provider deep link so the
 * URL a customer reads is the URL they land on.
 */
/**
 * Shown before the Anthropic key prompt when Claude Code is signed in here.
 *
 * Action first: a customer mid-setup reads the first line and acts, so the
 * instruction comes before the hazard and the mechanism comes last for whoever
 * wants it. The `sk-ant-api` / `sk-ant-oat` prefixes stay because they are
 * load-bearing and a customer can eyeball them.
 */
const CLAUDE_CODE_PRESENT_WARNING = [
  "Claude Code is signed in on this computer.",
  "Paste an Anthropic API key — the kind that starts sk-ant-api. JARVIS will use exactly that key.",
  "Don't paste a Claude subscription token (sk-ant-oat): JARVIS would use Claude Code's sign-in instead of your "
  + "key, and refreshing it can knock Claude Code itself down to pay-per-use. That's a bug in the engine JARVIS "
  + "runs on, and it hits Claude Code, not JARVIS.",
  "You can also paste an OpenAI or OpenRouter key instead — JARVIS works the same way with those.",
] as const;

const KEY_GUIDE: SecretPromptGuide = {
  text: "Don't have a key yet? 2-minute guide:",
  linkText: "jarvis-agent.tech/guides/api-keys",
  url: "https://jarvis-agent.tech/guides/api-keys",
};

/** How many blank answers the unskippable key prompt takes before failing honestly. */
export const MAX_KEY_ATTEMPTS = 5;

export interface ModelOverrides {
  run?: typeof runCommand;
  watch?: typeof runWatchedCommand;
  platform?: NodeJS.Platform;
  openUrl?: (ctx: WizardContext, url: string) => Promise<void>;
  brain?: BrainFlowDeps;
}

/**
 * The v1.6 Brain step. Screen 1 is always the first customer interaction — in
 * particular, no saved route is cleared or rewritten before its recovery
 * choice is visible.
 */
export async function model(
  ctx: WizardContext,
  overrides: ModelOverrides = {},
): Promise<StepOutcome> {
  const runners: ModelRunners = {
    run: overrides.run ?? runCommand,
    watch: overrides.watch ?? runWatchedCommand,
    platform: overrides.platform,
    openUrl: overrides.openUrl,
  };

  // One session per run so that ANY write — a sign-in, a key, a config key —
  // is followed by a gateway restart, including on the decline, failure and
  // throw paths. A gateway that keeps serving the pre-change credentials is the
  // stale-gateway class this product has already shipped four fixes for.
  const session = createModelSession();
  try {
    const outcome = await runBrainStep(ctx, runners, session, overrides.brain);
    return { ...outcome, props: { ...session.journeyProps, ...outcome.props } };
  } catch (error) {
    // A step that DIES still knows what it was doing. Without this the
    // rendered failure carries no brain_* props at all, and support cannot
    // see which subscription route was being set up when it broke (Codex
    // review r5). The error's own props win, as everywhere else.
    // ANY error, not just a WizardError: an EACCES reading the profile is
    // exactly as fatal to the step, and exactly as much in need of saying
    // which route was being set up (Codex review r6).
    if (typeof error === "object" && error !== null && Object.keys(session.journeyProps).length > 0) {
      const carrier = error as { props?: Record<string, string | number | boolean> };
      carrier.props = { ...session.journeyProps, ...carrier.props };
    }
    throw error;
  } finally {
    await settleGateway(ctx, runners.run, session, runners.platform);
  }
}

type BrainChoice = "chatgpt" | "claude" | "neither" | "keep" | "advanced";

interface ConfiguredBrain {
  label: string;
  method: Extract<SignInMethod, "apikey" | "codex-cli" | "claude-cli">;
}

async function runBrainStep(
  ctx: WizardContext,
  runners: ModelRunners,
  session: ModelSession,
  deps: BrainFlowDeps | undefined,
): Promise<StepOutcome> {
  for (;;) {
    const configured = await configuredBrain(ctx);
    if (configured?.method === "codex-cli") await repairManagedCodex(ctx, deps, runners.platform);
    const choices = [
      { key: "chatgpt" as const, hotkey: "1", label: "ChatGPT (Plus, Pro or Business)", description: "use the plan you already have" },
      { key: "claude" as const, hotkey: "2", label: "Claude (Pro or Max)", description: "use the plan you already have" },
      { key: "neither" as const, hotkey: "3", label: "Neither yet", description: "compare the two plans" },
      ...(configured === undefined ? [] : [{
        key: "keep" as const,
        hotkey: "4",
        label: `Keep the brain I have (${configured.label})`,
        description: "check it once and keep going",
      }]),
      {
        key: "advanced" as const,
        hotkey: configured === undefined ? "4" : "5",
        label: "Advanced: use an API key instead",
        description: "for developer billing",
      },
    ];
    const choice = await select<BrainChoice>(
      ctx,
      "Which of these do you already pay for, sir?\nJARVIS thinks with the plan you already have. No API key, no separate bill.",
      choices,
      "chatgpt",
    );

    if (ctx.dryRun) return noBrainOutcome(session.notes, true);
    const platform = runners.platform ?? process.platform;
    if (platform === "win32" && (choice === "claude" || choice === "advanced"
      || (choice === "keep" && configured !== undefined && configured.method !== "codex-cli"))) {
      const present = await (deps?.gitBashPresent?.() ?? gitBashPresent(ctx, process.env, { platform }));
      session.journeyProps.git_bash_present = present;
      if (!present) {
        warn(ctx, "JARVIS runs commands through Git for Windows, which is not installed yet. Install it from git-scm.com/download/win with the default options, then continue.");
      }
    }
    if (choice === "chatgpt" || choice === "claude") {
      ctx.state.providerChoice = choice === "chatgpt" ? "codex-cli" : "claude-cli";
    }
    if (choice === "advanced") {
      let outcome: StepOutcome | undefined;
      try {
        outcome = await apiKeyFlow(ctx, runners, session, undefined, deps);
      } finally {
        await finishRouteChange(ctx, session, outcome?.status === "done");
      }
      if (outcome !== undefined) return outcome;
      continue;
    }
    if (choice === "neither") {
      const later = await neitherYet(ctx, runners);
      if (later) return noBrainOutcome(session.notes);
      continue;
    }
    if (choice === "keep" && configured !== undefined) {
      if (readModelProvider(await readTextIfExists(path.join(ctx.profileDir, "config.yaml")) ?? "") === "claude-cli") {
        const outcome = await runBrainVendor(ctx, runners, session, "claude", deps);
        if (outcome !== undefined) return outcome;
        continue;
      }
      const keepOptions: VerifyOptions = {
        session,
        method: configured.method,
        platform: runners.platform,
        maxAttempts: 1,
        remedy: "So I returned to the Brain choices.",
        scrubEnv: configured.method === "apikey" ? undefined : BRAIN_VERIFY_SCRUB_ENV,
        successNote: `Your ${configured.label} answered — I kept it, sir.`,
        props: { brain_kept: true },
      };
      let kept = await verifyAndFinish(ctx, runners.run, keepOptions);
      if (kept.status !== "done" && configured.method === "codex-cli" && await confirm(ctx, CODEX_ASK_AGAIN, true)) {
        kept = await verifyAndFinish(ctx, runners.run, keepOptions);
      }
      if (kept.status === "done") return kept;
      ctx.state.modelConfigured = false;
      warn(ctx, "That brain did not answer, sir. Choose what you'd like me to try next.");
      continue;
    }

    const outcome = await runBrainVendor(ctx, runners, session, choice === "claude" ? "claude" : "codex", deps);
    if (outcome !== undefined) return outcome;
  }
}

async function neitherYet(ctx: WizardContext, runners: ModelRunners): Promise<boolean> {
  for (;;) {
    const choice = await select<"chatgpt" | "claude" | "continue" | "later">(
      ctx,
      "JARVIS needs one of these to think, sir. ChatGPT Plus is $20 a month, Claude Pro is $20 a month — either one runs JARVIS with nothing else to pay.",
      [
        { key: "chatgpt", hotkey: "1", label: "Get ChatGPT", description: "open ChatGPT" },
        { key: "claude", hotkey: "2", label: "Get Claude", description: "open Claude" },
        { key: "continue", hotkey: "3", label: "I've subscribed — continue", description: "return to the first question" },
        { key: "later", hotkey: "4", label: "Set this up later", description: "finish the rest of setup" },
      ],
      "chatgpt",
    );
    if (choice === "continue") return false;
    if (choice === "later") return true;
    const url = choice === "chatgpt" ? "https://chatgpt.com/" : "https://claude.ai/";
    const open = runners.openUrl ?? (async (context: WizardContext, target: string) => {
      const options: { platform?: NodeJS.Platform; run: ModelRunners["run"] } = { run: runners.run };
      if (runners.platform !== undefined) options.platform = runners.platform;
      await defaultOpenUrl(context, target, options);
    });
    await open(ctx, url).catch(() => warn(ctx, "I couldn't open that page, sir. You can try again."));
  }
}

export const NO_BRAIN_LINE = "Brain: not set up yet — open the gear → RUN SETUP AGAIN when you have ChatGPT or Claude";

function noBrainOutcome(notes: string[], dryRun = false): StepOutcome {
  notes.push(dryRun ? `[dry-run] ${NO_BRAIN_LINE}` : NO_BRAIN_LINE);
  return { status: "done", notes, props: { signin_method: "none", signin_result: dryRun ? "dry-run" : "not-set-up" } };
}

async function configuredBrain(ctx: WizardContext): Promise<ConfiguredBrain | undefined> {
  const configText = await readTextIfExists(path.join(ctx.profileDir, "config.yaml"));
  if (configText === undefined || readModelDefault(configText) === undefined) return undefined;
  const provider = readModelProvider(configText);
  if (provider === "codex-cli") return { label: "ChatGPT", method: "codex-cli" };
  if (provider === "claude-cli" || provider === "claude-subscription-directsdk-experimental") return { label: "Claude", method: "claude-cli" };
  const routed = await routedProviderKey(ctx);
  if (routed === undefined || routed === "unattributed") return undefined;
  const env = await readEnvFile(path.join(ctx.profileDir, ".env"));
  const value = (env[PROVIDERS[routed].envKey] ?? "").trim();
  if (value === "" || classifyKeyShape(PROVIDERS[routed].envKey, value).verdict !== "plausible") return undefined;
  return { label: `${PROVIDERS[routed].label} API key`, method: "apikey" };
}

/** Prompt-free automatic-pass check: retained brains earn one real answer. */
export async function verifyRetainedBrain(
  ctx: WizardContext,
  overrides: Pick<ModelOverrides, "run" | "platform" | "brain"> = {},
): Promise<StepOutcome> {
  const configured = await configuredBrain(ctx);
  if (configured === undefined) {
    // "Set this up later" is deliberately a completed Brain step. There is
    // no route to trust or spend a turn on, so retain that explicit outcome.
    ctx.state.modelConfigured = false;
    return { status: "done", notes: [NO_BRAIN_LINE], props: { signin_method: "none", signin_result: "not-set-up" } };
  }
  if (configured.method === "codex-cli") await repairManagedCodex(ctx, overrides.brain, overrides.platform);
  const session = createModelSession();
  const outcome = await verifyAndFinish(ctx, overrides.run ?? runCommand, {
    session,
    method: configured.method,
    platform: overrides.platform,
    maxAttempts: 1,
    remedy: "Open the gear → RUN SETUP AGAIN and choose the Brain step.",
    scrubEnv: configured.method === "apikey" ? undefined : BRAIN_VERIFY_SCRUB_ENV,
    successNote: `Your ${configured.label} answered — I kept it, sir.`,
    props: { retained_brain_verified: true },
  });
  if (outcome.status !== "done") ctx.state.modelConfigured = false;
  return outcome;
}

/**
 * The v1.3.18 key flow, unchanged in behaviour and demoted to third place.
 * Every config write now passes the token-replay guard (see adapter-guard.ts),
 * which is why the Anthropic branch can never be configured key-less.
 */
async function apiKeyFlow(
  ctx: WizardContext,
  runners: ModelRunners,
  session: ModelSession,
  /** What the route said BEFORE the brain journey made it inert (see model()). */
  attributed?: ProviderKey | "unattributed",
  deps: BrainFlowDeps = {},
): Promise<StepOutcome | undefined> {
  const { notes } = session;
  const envPath = path.join(ctx.profileDir, ".env");
  let env = await readEnvFile(envPath);

  // "JARVIS never asks for a key he already has" (Peter, 2026-08-14, after
  // minting a NEW Anthropic key with a saved one a button away — the old
  // screen's empty field and "don't have a key yet?" link visually screamed
  // go-get-a-key while "a key is already saved" hid in a parenthetical).
  // When a saved key can be attributed to one provider unambiguously, no key
  // UI appears at all: the verify runs by itself, narrated. Possession is not
  // trust — the assembled-token verify is never skipped, for carried keys
  // least of all.
  let savedKeyFailedFor: ProviderKey | undefined;
  const auto = ctx.dryRun ? undefined : await savedKeyCandidate(ctx, env, attributed);
  if (auto !== undefined) {
    const saved = PROVIDERS[auto];
    ctx.state.providerChoice = auto;
    // The deliberate-switch affordance: a small "use a different key" control
    // during the auto wait. Without it, a customer with a WORKING saved key
    // could never reach the paste screen again.
    const escape = ctx.io.offerEscape?.("Use a different key instead");
    let escaped = false;
    escape?.taken.then(() => {
      escaped = true;
    });
    try {
      ctx.io.note("One moment, sir — trying the key you gave me earlier.");
      const routeWritten = await writeBrainConfigWithRecovery(ctx, async () => {
        await makeRouteInert(ctx, runners.run, session);
        await removeEnv(ctx, envPath, ["JARVIS_DEMO"]);
        const envNow = await readEnvFile(envPath);
        await applyProviderConfig(ctx, runners.run, session, {
          provider: PROVIDERS[auto].configProvider ?? auto,
          model: saved.model,
          platform: runners.platform,
          existingEnvKeys: Object.entries(envNow)
            .filter(([, value]) => (value ?? "").trim() !== "")
            .map(([name]) => name),
        });
        return true;
      }, deps);
      if (routeWritten !== true) return undefined;
      // The escape may have been taken during the writes above — skip the
      // long verify rather than making the customer sit through proving a
      // key they just said they want to replace.
      if (!escaped) {
        const outcome = await verifyAndFinish(ctx, runners.run, {
          session,
          method: "apikey",
          platform: runners.platform,
          // ONE attempt, no retry prompt: failure falls through to the paste
          // screen below, which is the retry.
          maxAttempts: 1,
          successNote: "I kept the key you gave me last time, sir — it still works.",
          // Past-tense on purpose: this line lands in the shared notes and
          // survives into the final ledger even when a fresh key later
          // succeeds — "let's use a fresh key" would read as an open action
          // in a finished summary (re-cut #5 review, P3).
          remedy: "So I asked for a fresh key.",
          props: { saved_key_auto: true },
        });
        if (outcome.status === "done" && !escaped) return outcome;
        if (outcome.status !== "done") savedKeyFailedFor = auto;
      }
    } finally {
      escape?.retract();
      if (escaped || savedKeyFailedFor !== undefined) await finishRouteChange(ctx, session, false);
    }
    if (savedKeyFailedFor !== undefined) {
      ctx.state.modelConfigured = false;
      warn(ctx, "The key you gave me earlier stopped working, sir — let's set up a fresh one.");
    }
    // Fall through to the normal flow: either the customer asked for a
    // different key (saved one may still be fine) or the saved key failed
    // (no dead KEEP SAVED KEY button for a key that just failed).
    env = await readEnvFile(envPath);
  }

  // Telemetry: the discarded auto attempt must not vanish (review, P2).
  const autoFailureProps = savedKeyFailedFor !== undefined ? { saved_key_auto_failed: true } : {};

  const provider = await select<ProviderKey>(
    ctx,
    "Choose the model provider Jarvis should use.",
    [
      { key: "anthropic", hotkey: "1", label: "Anthropic", description: "default Claude lane" },
      { key: "openrouter", hotkey: "2", label: "OpenRouter", description: "one key for many models" },
      { key: "openai", hotkey: "3", label: "OpenAI", description: "use existing OpenAI billing" },
    ],
    "anthropic",
  );

  const selected = PROVIDERS[provider];
  ctx.state.providerChoice = provider;
  const existingKey = env[selected.envKey];
  const hasExistingKey = existingKey !== undefined && existingKey !== "";
  // A saved key that JUST FAILED its verify is not an offer: no "leave blank
  // to keep it" hint, no KEEP SAVED KEY button, and a blank submission never
  // completes the step with it (Peter's walkthrough, re-cut #5).
  const savedKeyUsable = hasExistingKey && savedKeyFailedFor !== provider;
  const hasOtherProviderKey = ctx.state.modelConfigured
    || Object.values(PROVIDERS).some(({ envKey }) => envKey !== selected.envKey && (env[envKey] ?? "").trim() !== "");
  const promptMessage = savedKeyFailedFor === provider
    ? `${selected.label} API key — the saved key stopped working, so I need a fresh one`
    : `${selected.label} API key${hasExistingKey ? " (a key is already saved - leave blank to keep it)" : ""}`;
  // Claude Code on this machine changes what a pasted Anthropic secret MEANS,
  // so the customer hears it before they paste, not after. See provider-safety:
  // an sk-ant-api key is used as given; an sk-ant-oat subscription token loses
  // to Claude Code's own sign-in inside the engine, and refreshing that sign-in
  // can knock Claude Code down to pay-per-use.
  if (provider === "anthropic" && await claudeCodeCredentialsPresent()) {
    for (const line of CLAUDE_CODE_PRESENT_WARNING) ctx.io.note(line);
  }
  // After an auto-verify failure NOTHING routed counts as current setup —
  // the saved key failed verification, and "keep current setup" for
  // ANY provider would carry a known-failed configuration through the launch
  // gate as skipped (re-cut #5 review, P1).
  const routedAlready = savedKeyFailedFor === undefined
    && (ctx.state.modelConfigured || await hasConfiguredRoute(ctx));
  // The submit button must never be a live control that does nothing: with a
  // saved key (or an already-configured provider) an EMPTY submission means
  // something real and the button says what in plain words; with neither, the
  // GUI disables submit until a key is typed (Peter's smoke feedback,
  // 2026-08-14).
  const secretOptions = savedKeyUsable
    ? { emptySubmitLabel: "KEEP SAVED KEY" }
    : routedAlready && savedKeyFailedFor !== provider
      ? { emptySubmitLabel: "KEEP CURRENT SETUP" }
      : undefined;
  let key = (await promptSecret(ctx, promptMessage, KEY_GUIDE, secretOptions)).trim();
  // UNSKIPPABLE (Peter's flow ruling, 2026-08-14): there is no "finish setup
  // without a brain" path. A profile that already has a working route (or a
  // saved key for this provider) may leave the field blank; otherwise the step
  // keeps asking. The way out is closing the wizard — setup saves its progress
  // and offers this step again next time. Bounded, not infinite: a frontend
  // that auto-answers (a scripted run, a stuck GUI) must not spin forever, so
  // repeated blanks — or repeated unusable pastes — end in an HONEST FAILURE,
  // never a quiet "continue without a brain". Dry-run falls through on its
  // defaults as before.
  //
  // A NON-BLANK paste goes through classifyKeyShape (provider-safety.ts, the
  // one key-classification authority): structural gibberish and Anthropic
  // subscription tokens are refused in plain words at the moment of the paste;
  // an unfamiliar-but-well-formed shape gets a QUESTION, never a wall —
  // provider key formats drift, and a valid future key must survive this.
  for (let attempts = 0; ; ) {
    if (key === "") {
      if (savedKeyFailedFor !== provider && (hasExistingKey || routedAlready || ctx.dryRun)) break;
      attempts += 1;
      if (attempts >= MAX_KEY_ATTEMPTS) {
        notes.push(
          "No key was entered, so this step is unfinished and Jarvis stays offline. "
          + "Run the Model step again from JARVIS settings when you have your key — the guide takes about two minutes.",
        );
        return { status: "failed", notes, props: { signin_method: "apikey", signin_result: "cancelled", ...autoFailureProps } };
      }
      warn(ctx, "Jarvis cannot think without a key — paste one to continue. If you need to stop, close this window: setup keeps your progress and picks up right here next time.");
    } else {
      const shape = classifyKeyShape(selected.envKey, key);
      if (shape.verdict === "plausible") break;
      if (shape.verdict === "unfamiliar" && await confirm(ctx, `${shape.reason} Use it anyway?`, false)) break;
      if (shape.verdict === "reject") warn(ctx, shape.reason);
      attempts += 1;
      if (attempts >= MAX_KEY_ATTEMPTS) {
        notes.push(
          "The key entered doesn't look usable, so this step is unfinished and Jarvis stays offline. "
          + "Copy the whole key from your provider's console, then run the Model step again from JARVIS settings.",
        );
        return { status: "failed", notes, props: { signin_method: "apikey", signin_result: "cancelled", ...autoFailureProps } };
      }
    }
    key = (await promptSecret(ctx, promptMessage, KEY_GUIDE, secretOptions)).trim();
  }

  if (key === "" && !hasExistingKey) {
    const routed = await hasConfiguredRoute(ctx);
    notes.push(routed
      ? "No new key entered; the provider already configured here is left as it is."
      : hasOtherProviderKey
        // A saved key for ANOTHER provider is not a route: with `model: {}` the
        // profile still has nothing to run on, and calling that "configured
        // model live" was a claim about state nobody had written.
        ? "No key entered. A key for a different provider is saved, but no provider is configured, so Jarvis stays "
          + "offline until the Model step configures and verifies one."
        : noProviderKeyNote(selected.envKey));
    return {
      status: "skipped",
      notes,
      props: routed || hasOtherProviderKey
        ? { signin_method: "apikey", ...autoFailureProps }
        : { signin_method: "apikey", signin_result: "cancelled", ...autoFailureProps },
    };
  }

  // Clear the route before replacing credentials; restore both unless verification passes.
  const cleared = await writeBrainConfigWithRecovery(ctx, async () => {
    await makeRouteInert(ctx, runners.run, session);
    return true;
  }, deps);
  if (cleared !== true) return undefined;

  if (key === "") {
    await removeEnv(ctx, envPath, ["JARVIS_DEMO"]);
    notes.push(`Kept the existing ${selected.envKey}.`);
  } else {
    await upsertEnv(
      ctx,
      envPath,
      { [selected.envKey]: key },
      { secretKeys: [selected.envKey] },
    );
    markDirty(session);
    await removeEnv(ctx, envPath, ["JARVIS_DEMO"]);
    notes.push(`${selected.envKey} stored in the profile .env.`);
  }

  // An OAUTH-SHAPED value in ANTHROPIC_API_KEY is never a valid API key — it
  // is a Claude subscription token, the exact credential provider safety
  // refuses to serve (it can put the engine on the customer's Claude account
  // and rewrite Claude Code's own sign-in file). Leaving it behind made the
  // collision remedy a LOOP: "switch to an OpenAI key" stored the new key,
  // but the stale token kept every subsequent launch blocked (Codex review
  // 2026-08-14, finding 2). Removed whenever a new key lands, whatever
  // provider it is for; an api-key-shaped Anthropic value is never touched.
  if (key !== "" && selected.envKey !== "ANTHROPIC_API_KEY") {
    const savedAnthropic = ((await readEnvFile(envPath)).ANTHROPIC_API_KEY ?? "").trim();
    if (anthropicSecretShape(savedAnthropic) === "oauth") {
      await removeEnv(ctx, envPath, ["ANTHROPIC_API_KEY"]);
      markDirty(session);
      notes.push(
        "I also removed a Claude subscription sign-in that was saved here — it was blocking JARVIS from starting, "
        + "and it could have affected the Claude app's own plan. Your new key is what JARVIS uses now.",
      );
    }
  }

  // The guard needs to know a key really is in place before an Anthropic model
  // is written: an Anthropic route with no key is exactly how the engine falls
  // through to its Claude-Code credential-replay adapter.
  const envAfterWrite = await readEnvFile(envPath);
  const routeWritten = await writeBrainConfigWithRecovery(ctx, async () => {
    await applyProviderConfig(ctx, runners.run, session, {
      provider: PROVIDERS[provider].configProvider ?? provider,
      model: selected.model,
      platform: runners.platform,
      existingEnvKeys: Object.entries(envAfterWrite)
        .filter(([, value]) => (value ?? "").trim() !== "")
        .map(([name]) => name),
    });
    return true;
  }, deps);
  if (routeWritten !== true) return undefined;

  return verifyAndFinish(ctx, runners.run, {
    session,
    method: "apikey",
    platform: runners.platform,
    remedy: "Check the provider key, then rerun the wizard or the verification chat.",
    // The discarded auto attempt's failure signal rides the FINAL outcome —
    // telemetry only sees one step_finished (re-cut #5 review, P2).
    props: autoFailureProps,
  });
}

/**
 * The one provider whose saved key the step may verify WITHOUT asking.
 *
 * Deliberately narrow: a configured route names its provider, and only THAT
 * provider's key auto-runs — quietly switching a route to a different
 * provider because its key happens to look nicer is a config change nobody
 * asked for. With no route at all (a fresh install holding a carried key),
 * exactly one provider with a usable key is unambiguous; zero or several
 * means the customer chooses. "Usable" means classifyKeyShape says
 * "plausible" — an OAuth-shaped Anthropic value is the credential provider
 * safety exists to refuse, and an unfamiliar shape earned a question when it
 * was pasted, so neither ever auto-runs.
 */
async function savedKeyCandidate(
  ctx: WizardContext,
  env: Record<string, string>,
  attributed?: ProviderKey | "unattributed",
): Promise<ProviderKey | undefined> {
  const usable = (providerKey: ProviderKey): boolean => {
    const envKey = PROVIDERS[providerKey].envKey;
    const value = (env[envKey] ?? "").trim();
    return value !== "" && classifyKeyShape(envKey, value).verdict === "plausible";
  };
  // A route on disk, or the one that WAS on disk before the brain journey
  // made it inert this run: either way only THAT provider's key auto-runs.
  // THREE states, kept apart (Codex review r5, 2026-08-15): "no route" →
  // exactly-one-usable-key may auto-run; "route with an API provider" → only
  // that provider; "route with some OTHER prefix" (a brain route, a custom
  // slug) → nothing auto-runs, the customer chooses — a saved key must never
  // quietly replace a route it was not attributed to.
  const routed = (await routedProviderKey(ctx)) ?? attributed;
  if (routed === "unattributed") return undefined;
  if (routed !== undefined) return usable(routed) ? routed : undefined;
  const candidates = (Object.keys(PROVIDERS) as ProviderKey[]).filter(usable);
  return candidates.length === 1 ? candidates[0] : undefined;
}

/**
 * What the profile's route says about auto-verification: the API-key
 * provider it names; "unattributed" when a route exists whose provider is
 * not one of ours (brain routes, custom slugs); undefined when there is no
 * route at all.
 */
async function routedProviderKey(ctx: WizardContext): Promise<ProviderKey | "unattributed" | undefined> {
  const configText = await readTextIfExists(path.join(ctx.profileDir, "config.yaml"));
  if (configText === undefined) return undefined;
  const routedModel = readModelDefault(configText);
  if (routedModel === undefined) return undefined;
  const configuredProvider = readModelProvider(configText);
  if (configuredProvider === "openai") return "openai"; // v1.4.0–1.5.x legacy route
  return (Object.keys(PROVIDERS) as ProviderKey[])
    .find((key) => (PROVIDERS[key].configProvider ?? key) === configuredProvider) ?? "unattributed";
}

/** Read `model.provider` without mistaking another YAML block's provider. */
export function readModelProvider(configText: string): string | undefined {
  const lines = configText.split(/\r?\n/);
  const modelIndex = lines.findIndex((line) => /^model:\s*(\{\s*\})?\s*$/.test(line));
  if (modelIndex === -1) return undefined;
  for (let index = modelIndex + 1; index < lines.length; index += 1) {
    const line = lines[index];
    if (line.trim() === "" || line.trimStart().startsWith("#")) continue;
    if (!/^\s/.test(line)) return undefined;
    const match = /^\s+provider:\s*(.*)$/.exec(line);
    if (match === null) continue;
    const value = match[1].trim().replace(/^["']|["']$/g, "");
    return value === "" ? undefined : value;
  }
  return undefined;
}

/**
 * Is the installed engine the patched vendored one (hud/vendor/engine-patches)?
 * The brain modules are new files the patch series adds; their presence in
 * the installed engine tree is the honest capability check — a build flag
 * cannot know which engine a zip customer's machine ended up with.
 */
export async function brainEngineInstalled(ctx: WizardContext): Promise<boolean> {
  return pathExists(path.join(ctx.hermesAgentDir, "agent", "brain", "providers.py"));
}

/** Does the profile actually name a provider and a model to run on? */
async function hasConfiguredRoute(ctx: WizardContext): Promise<boolean> {
  const configText = await readTextIfExists(path.join(ctx.profileDir, "config.yaml"));
  if (configText === undefined) return false;
  return readModelDefault(configText) !== undefined;
}

function noProviderKeyNote(subject: string): string {
  return `${subject} was not provided, so Jarvis stays offline until one is added. `
    + "Open the gear → RUN SETUP AGAIN and choose the Brain step when you're ready.";
}
