import { WizardError, isSetupCancelled, type StepOutcome, type StepStatus, type StepSummary, type WizardContext } from "./context.js";
import {
  GATEWAY_INTEGRITY_REVISION,
  PROFILE_MIGRATE_REVISION,
  adoptProgress,
  readSetupProgress,
  writeSetupProgress,
  type SetupProgress,
} from "./setup-state.js";
import { ensureProviderSafety, PROVIDER_SAFETY_REVISION } from "./provider-safety.js";
import { probeLegacyFootprint } from "./purge.js";
import { composeRoles } from "./steps/compose-roles.js";
import { gateway } from "./steps/gateway.js";
import { hello } from "./steps/hello.js";
import { hermes } from "./steps/hermes.js";
import { installApp } from "./steps/install-app.js";
import { migrate } from "./steps/migrate.js";
import { model, verifyRetainedBrain } from "./steps/model.js";
import { migrateProfileTerminalCwd, migrateProfileVoiceConfig } from "./profile-migrate.js";
import { preflight } from "./steps/preflight.js";
import { profile } from "./steps/profile.js";
import { slack } from "./steps/slack.js";
import { summary } from "./steps/summary.js";
import { voice } from "./steps/voice.js";
import { withFileExplorerAddressBarHint } from "./ui.js";
import { removeHermesRootPointer, writeHermesRootPointer } from "./hermes-root-pointer.js";
import { isPlatformDefaultHermesRoot } from "./hermes-root.js";
import { writeMigrationMarker } from "./migration-marker.js";
import { readVendorManifest } from "./vendor.js";
import { vendoredEngineNeedsInstall } from "./vendored-engine.js";

export { createContext, WizardError } from "./context.js";
export type {
  GatewayConnection,
  GatewayControl,
  GatewayOutcomeState,
  GatewayVerification,
  StepOutcome,
  StepStatus,
  StepSummary,
  WizardContext,
  WizardState,
} from "./context.js";
export { authorizeGatewayStart } from "./gateway-authorization.js";
export type { GatewayAuthorization, GatewayDebtReason } from "./gateway-authorization.js";
export { probeGateway, waitForGateway } from "./gateway-probe.js";
export type { GatewayReach } from "./gateway-probe.js";
export { readMigrationMarker, writeMigrationMarker } from "./migration-marker.js";
export type { MigrationMarker } from "./migration-marker.js";
export { readVendorManifest, resolveVendorRoot } from "./vendor.js";
export type { VendorManifest } from "./vendor.js";
export { installVendoredEngine } from "./vendored-engine.js";
export { readEnvFile } from "./env-file.js";
export type { EnvMap } from "./env-file.js";
export { resolveHermesRoot } from "./hermes-root.js";
export { engineHostEnvironment } from "./system.js";
export type { CommandStream, DeviceCodePrompt, DeviceCodeStatus, EscapeOffer, SecretPromptGuide, SecretPromptOptions, SetupChoice, SetupIO } from "./io.js";
export { assertNoTokenReplay, FORBIDDEN_ENV_KEYS, FORBIDDEN_PROVIDER_ALIASES } from "./adapter-guard.js";
export { classifySignInFailure, parseDeviceCode } from "./device-code.js";
export type { DeviceCodeInfo, SignInResult } from "./device-code.js";
export { NO_BRAIN_LINE } from "./steps/model.js";
export { redactSecrets } from "./redact.js";
export { applyProviderSafety, classifyKeyShape, ensureProviderSafety, PROVIDER_SAFETY_REVISION } from "./provider-safety.js";
export type { KeyShapeVerdict, ProviderSafetyAction, ProviderSafetyPass, ProviderSafetyResult } from "./provider-safety.js";
export { markWizardGreeted, wizardGreeted, wizardGreetedPath } from "./wizard-greeted.js";
export { PROFILE_MIGRATE_REVISION, readSetupProgress, readSetupProgressAt, setupStateFilePath, setupStatePath } from "./setup-state.js";
export type { SetupProgress } from "./setup-state.js";
export { TELEMETRY_NOTICE, cachedInstallId, createTelemetry, enqueueOutOfBandTelemetry, jarvisConfigDir, loadInstallId, readInstallId, telemetryEnabled, withTelemetry } from "./telemetry.js";
export type { OutOfBandTelemetryEvent, OutOfBandTelemetryOptions, Telemetry, TelemetryOptions } from "./telemetry.js";
export {
  buildLastRunRecord,
  formatDiagnostics,
  lastCrashFilePath,
  lastRunFilePath,
  loadSupportCode,
  readLastCrash,
  readLastRun,
  recordCrash,
  supportCodeFromInstallId,
  writeLastRun,
} from "./diagnostics.js";
export type { CrashReason, CrashRecord, DiagnosticsInfo, LastRunRecord, LastRunStep } from "./diagnostics.js";

export interface StepDefinition {
  id: string;
  title: string;
  /**
   * How the customer meets this step (Peter's flow ruling, 2026-08-14):
   *  - "auto": mechanical work — runs by itself, narrating; no skip exists.
   *  - "required": needs the customer's INPUT, never their permission to ask —
   *    it opens straight on its own question and cannot be skipped. Quitting
   *    the wizard (safe, resumable) is the only way out.
   *  - "optional": a genuine product choice, asked INSIDE the step in plain
   *    words ("connect Slack?"), with a decline that simply moves on.
   * There is no per-step gate and no skip machinery: the classification is
   * documentation plus the contract the tests pin.
   */
  kind: "auto" | "required" | "optional";
  run: (ctx: WizardContext) => Promise<StepOutcome>;
}

export const STEPS: StepDefinition[] = [
  { id: "preflight", title: "Preflight", kind: "auto", run: preflight },
  // Present ONLY when the probe finds (or cannot rule out) an earlier install
  // — see plannedSteps in runSetup. When present it runs by itself: cleaning
  // what WE left behind needs narration, not permission (Peter, 2026-08-14).
  { id: "migrate", title: "Earlier Install", kind: "auto", run: migrate },
  { id: "hermes", title: "Hermes", kind: "auto", run: hermes },
  { id: "profile", title: "Profile", kind: "auto", run: profile },
  // Auto: the copying is mechanical; each SCHEDULE is its own plain-language
  // yes/no inside the step — that choice is the product, not step machinery.
  { id: "compose-roles", title: "Compose Roles", kind: "auto", run: composeRoles },
  { id: "model", title: "Brain", kind: "required", run: model },
  { id: "slack", title: "Slack", kind: "optional", run: slack },
  // Start Engine comes BEFORE Voice on purpose (integration review r1, finding
  // 3): typed chat is the product working, and it must not wait behind a
  // minute of speech self-tests. Voice is the enhancement, and a machine that
  // cannot speak still has a working Jarvis — which is exactly what the Voice
  // step's copy promises.
  { id: "gateway", title: "Start Engine", kind: "auto", run: gateway },
  // Optional in the mic sense: the OS permission question inside it is real.
  { id: "voice", title: "Voice", kind: "optional", run: voice },
  { id: "install-app", title: "Install App", kind: "auto", run: installApp },
  { id: "hello", title: "Hello", kind: "auto", run: hello },
  { id: "summary", title: "Summary", kind: "auto", run: summary },
];

export interface StepProgressEvent {
  id: string;
  title: string;
  index: number;
  total: number;
  previousStatus?: StepStatus;
  /** True only on stepFinished when an already-done step was gate-retained. */
  retained?: boolean;
}

export interface SetupResult {
  summary: StepSummary[];
  failed: boolean;
  /** True when the run stopped early because it was cancelled. */
  cancelled?: boolean;
}

export interface SetupHooks {
  /**
   * The sequence THIS run will actually walk, resolved after detection —
   * because a step that does not apply to this machine (Earlier Install on a
   * clean one) is not rendered-then-yanked, and not shown greyed: it is
   * absent. Frontends render their step list from this, never from STEPS.
   */
  planned?: (steps: { id: string; title: string }[]) => void;
  stepStarted?: (event: StepProgressEvent) => void;
  stepFinished?: (event: StepProgressEvent & { outcome: StepOutcome }) => void;
  finished?: (result: SetupResult) => void;
}

export async function runSetup(
  ctx: WizardContext,
  hooks: SetupHooks = {},
  steps: StepDefinition[] = STEPS,
  platform: NodeJS.Platform = process.platform,
  seams: {
    probeLegacyFootprint?: typeof probeLegacyFootprint;
    /** Test seam for the safety lifecycle; production always uses the real one. */
    ensureProviderSafety?: typeof ensureProviderSafety;
    /** Test seam; production re-verifies a retained Brain once, without prompting. */
    verifyRetainedBrain?: typeof verifyRetainedBrain;
    /**
     * An explicit "run setup again" from the customer: no step is retained —
     * every step runs, and each proves its own work is still true (a revoked
     * key re-verifies, a failed schedule retries). Resume-after-interrupt
     * keeps retention; this is only ever set by a human asking.
     */
    rerunAll?: boolean;
    slackOnly?: boolean;
  } = {},
): Promise<SetupResult> {
  const progress = await readSetupProgress(ctx);
  progress.completed = false;
  ctx.summary.length = 0;

  // Unsafe legacy provider routes are cleared BEFORE any step runs and before
  // any gate can retain one — a completed install is exactly the case that
  // never reaches a step again, and the route it carries is the one that can
  // reach the Codex-import or Claude-Code replay resolver. The SAME shared
  // lifecycle runs at HUD launch, so both entry points update state identically.
  // While this is true the profile is NOT PROVEN SAFE and no gateway may be
  // started. A pass that could not read the config (1.3.x machine, broken engine
  // python) is not a softer kind of pass — it is the state the unsafe routes
  // live in. It is re-evaluated after the Hermes step, which is the moment a
  // machine that could not be checked usually becomes checkable.
  /**
   * Read through a call, never a cached boolean: the signal aborts WHILE the
   * run is in flight, and TypeScript's own narrowing would otherwise convince
   * itself the flag cannot have changed since the loop began.
   */
  const cancelledNow = (): boolean => ctx.signal?.aborted === true;
  let safetyBlocked = false;
  // Only ONE blocked reason is repaired by installing the engine: a check that
  // could not run because there was no engine python to run it with. Re-running
  // the whole lifecycle for any other reason means a second config scan and a
  // second gateway restart attempt for nothing (review r2, finding 6).
  let blockedByMissingEngine = false;
  const safetyLifecycle = seams.ensureProviderSafety ?? ensureProviderSafety;
  const runSafetyPass = async (phase: "pre-gate" | "post-engine" | "post-model"): Promise<void> => {
    const safety = await safetyLifecycle(ctx).catch((error: unknown) => {
      ctx.io.warn(`The provider-safety check could not run: ${error instanceof Error ? error.message : String(error)}`);
      // A check that could not run proves nothing, so the block stays on.
      return undefined;
    });
    if (safety === undefined) {
      safetyBlocked = true;
      blockedByMissingEngine = false;
      ctx.summary.push({
        id: `provider-safety-${phase}`,
        title: "Provider Safety",
        status: "failed",
        notes: ["I could not check this profile's model provider for unsafe settings, so I have not started the engine."],
        props: { provider_safety: "check-failed", provider_safety_phase: phase },
      });
      return;
    }
    // REPLACE, never merge — see adoptProgress: a merge cannot delete a key the
    // lifecycle just cleared, which resurrected settled restart-pending state.
    adoptProgress(progress, safety.progress);
    safetyBlocked = safety.blocked;
    blockedByMissingEngine = safety.blocked && safety.inconclusiveReason === "engine-missing";
    if (!safety.changed && !safety.blocked && safety.notes.length === 0) return;
    for (const note of safety.notes) ctx.io.note(note);
    ctx.summary.push({
      id: `provider-safety-${phase}`,
      title: "Provider Safety",
      // Anything short of PROVEN safe is a failed step, not a green one: a
      // gateway still serving the removed route, and an inconclusive check
      // alike (review r1, finding 1 — inconclusive used to record as "done").
      status: safety.blocked ? "failed" : "done",
      notes: safety.notes,
      props: {
        provider_safety: safety.actions.join(",") || "none",
        provider_safety_phase: phase,
        provider_safety_blocked: safety.blocked,
        ...(safety.gateway === undefined ? {} : { provider_safety_gateway: safety.gateway }),
      },
    });
  };

  // Unsafe legacy provider routes are cleared BEFORE any step runs and before
  // any gate can retain one — a completed install is exactly the case that
  // never reaches a step again, and the route it carries is the one that can
  // reach the Codex-import or Claude-Code replay resolver. The SAME shared
  // lifecycle runs at HUD launch, so both entry points update state identically.
  await runSafetyPass("pre-gate");

  // Pre-v1.3.2 profiles need their voice config repaired even when every
  // step is retained or skipped — an updated bundle is often rerun with all
  // steps already marked done, so this cannot live behind a step gate. The
  // failure must not block setup, but it must show up in the ledger rather
  // than vanish.
  try {
    await migrateProfileVoiceConfig(ctx);
  } catch (error) {
    const outcome = renderError(error);
    ctx.summary.push({
      id: "profile-repair",
      title: "Profile Repair",
      status: "skipped",
      notes: ["The automatic voice-config repair could not run.", ...outcome.notes],
    });
  } finally {
    // Attempt-once: a repair that fails deterministically (an unparseable
    // config.yaml) must not re-open the wizard on every launch; the ledger
    // note carries the failure.
    progress.profileMigrateRev = PROFILE_MIGRATE_REVISION;
  }

  // F4: existing profiles never receive the new seed (the Profile step
  // returns early when the profile exists), so the working-folder pin is a
  // live-config migration, not only a seed — and it corrects the seed's
  // POSIX-literal default on Windows and custom-HERMES_HOME machines. Same
  // posture as the voice repair: a failure shows up in the ledger, never
  // blocks setup.
  try {
    await migrateProfileTerminalCwd(ctx);
  } catch (error) {
    const outcome = renderError(error);
    ctx.summary.push({
      id: "profile-repair-cwd",
      title: "Profile Repair",
      status: "skipped",
      notes: ["The automatic working-folder repair could not run.", ...outcome.notes],
    });
  }

  // THE SEQUENCE THIS MACHINE GETS. The Earlier Install step exists only when
  // the purge's own read-only inventory finds — or cannot rule out — something
  // actionable (Peter, 2026-08-14: on a clean machine the step "should not
  // exist — not in the SEQUENCE list, no prompt, no narration"). The probe and
  // the purge share one detector and one class list, so they cannot disagree.
  // Fail-safe direction: an INCONCLUSIVE probe keeps the step — the machine
  // that cannot be scanned is the machine most likely to need cleaning, the
  // purge itself cannot fail, and showing it costs seconds while hiding it
  // could strand leftovers. Dry runs keep it too, so they narrate the whole
  // shape. When the step is omitted, the migration marker is written here:
  // "nothing to migrate" IS the migration, and without the marker the app
  // would send the customer back to the wizard on every launch.
  let plannedSteps = steps.filter((step) => seams.slackOnly
    ? ["slack", "gateway", "summary"].includes(step.id)
    : step.id !== "slack");
  if (plannedSteps.some((step) => step.id === "migrate")) {
    const probeFootprint = seams.probeLegacyFootprint ?? probeLegacyFootprint;
    const probe = ctx.dryRun
      ? { actionable: true, conclusive: true }
      : await probeFootprint(ctx).catch(() => ({ actionable: false, conclusive: false }));
    if (!probe.actionable && probe.conclusive) {
      plannedSteps = plannedSteps.filter((step) => step.id !== "migrate");
      await writeMigrationMarker(ctx, { cleaned: false }).catch(() => {});
    }
  }
  hooks.planned?.(plannedSteps.map((step) => ({ id: step.id, title: step.title })));

  for (let index = 0; index < plannedSteps.length; index += 1) {
    // Cancellation ends the RUN, not just the child process it interrupted:
    // continuing into later steps after the customer closed the window (or hit
    // Ctrl+C) would keep changing their machine with nobody watching.
    if (cancelledNow()) {
      ctx.summary.push({
        id: "cancelled",
        title: "Cancelled",
        status: "skipped",
        notes: ["Setup was stopped, so the remaining steps did not run. Nothing already finished was undone."],
      });
      break;
    }
    const step = plannedSteps[index];
    const event: StepProgressEvent = {
      id: step.id,
      title: step.title,
      index,
      total: plannedSteps.length,
      previousStatus: progress.steps[step.id],
    };
    hooks.stepStarted?.(event);

    // NO GATE. Steps run by themselves; the only question the machinery still
    // answers is "did an earlier run already finish this?" — a done step is
    // retained without re-asking (resume-after-interrupt must not interrogate
    // the customer about work that already happened), and five steps force a
    // re-run because "done once" is not the property they need:
    //
    // The engine is started by THIS run, every run: a previous run's "done" says
    // nothing about whether a gateway is live right now, and the step is cheap.
    const forceGatewayStart = step.id === "gateway";
    // Skills are the update channel and must reach existing installs on every
    // setup pass. Knowledge is copy-if-missing; demo, operations, and cron work
    // are each guarded or marker-merged, so the rest is idempotent too.
    const forceComposeRoles = step.id === "compose-roles";
    // The setup-written product facts must follow upgrades and live settings,
    // including the prompt-free pass where every ordinary step is retained.
    const forceSelfKnowledge = step.id === "summary";
    // The bundled engine is INSTALLED whenever what is on disk is not the
    // engine this build ships — a 1.3.x machine has a healthy network-installed
    // engine, so an already-done Hermes step would leave it there forever
    // (review r1, finding 10; same forced-rerun shape as gatewayIntegrityRev).
    const forceEngineInstall = step.id === "hermes" && await engineInstallRequired(ctx);
    // Earlier Install is in this run's sequence ONLY because the probe found
    // (or could not rule out) something to clean — so being "done" in a
    // previous run proves nothing about what is on the machine NOW.
    const forceMigration = step.id === "migrate";
    // A profile that could not be PROVEN safe must not keep an already-done
    // Model step either: the route it holds is the one nobody could read, and
    // the Model step is what replaces it wholesale.
    const forceModelReview = step.id === "model" && safetyBlocked;
    const shouldRun = seams.rerunAll === true
      || seams.slackOnly === true
      || forceGatewayStart
      || forceComposeRoles
      || forceSelfKnowledge
      || forceEngineInstall
      || forceMigration
      || forceModelReview
      || event.previousStatus !== "done";
    let retained = false;
    let rawOutcome: StepOutcome;
    if (step.id === "gateway" && safetyBlocked) {
      rawOutcome = blockedGatewayStep();
    } else if (shouldRun) {
      rawOutcome = await runStep(ctx, step);
    } else if (step.id === "model") {
      rawOutcome = await runStep(ctx, {
        ...step,
        run: async (context) => await (seams.verifyRetainedBrain ?? verifyRetainedBrain)(context),
      });
      retained = rawOutcome.status === "done";
    } else {
      rawOutcome = retainStep();
      retained = true;
    }
    const outcome = withPathHelp(rawOutcome);

    ctx.summary.push({ id: step.id, title: step.title, ...outcome });
    progress.steps[step.id] = outcome.status;
    if (step.id === "gateway" && ctx.state.gatewayIntegrityVerified === true) {
      progress.gatewayIntegrityRev = GATEWAY_INTEGRITY_REVISION;
    }
    await persistProgress(ctx, progress);
    hooks.stepFinished?.({ ...event, ...(retained ? { retained: true } : {}), outcome });

    if (outcome.status === "failed" && outcome.fatal === true) break;

    // The engine this build ships has just been installed, so a machine whose
    // config could not be READ a moment ago (its 1.3.x python was missing or
    // half-installed) can finally be checked. This runs BEFORE the Model step's
    // gate and long before Start Engine, so a route found unsafe here still
    // invalidates a completed Model step and still blocks the gateway.
    if (
      step.id === "hermes"
      && outcome.status !== "failed"
      && safetyBlocked
      && blockedByMissingEngine
      // Cancellation is checked HERE, not only at the top of the loop: this
      // pass rewrites configuration and can restart a gateway, and doing that
      // after the customer closed the window is exactly what cancellation is
      // supposed to prevent (review r2, finding 6).
      && !cancelledNow()
    ) {
      await runSafetyPass("post-engine");
    }

    // A Model step that just FINISHED is the one remedy every safety block
    // points the customer at — "fix it in the Model step". Without a re-check
    // here, the fixed profile stayed blocked until the NEXT launch: the
    // customer followed the instruction, and the same wall came back up on the
    // very screen that told them to do it (Codex review 2026-08-14, finding
    // 2). The pass re-reads everything from disk, so it either clears the
    // block for the Start Engine step below or honestly renews it.
    if (
      step.id === "model"
      && outcome.status === "done"
      && safetyBlocked
      && !cancelledNow()
    ) {
      await runSafetyPass("post-model");
    }
  }

  const cancelled = cancelledNow();
  const result: SetupResult = {
    summary: [...ctx.summary],
    // A degraded step (voice that cannot run on this machine) is reported red
    // but does not make the run failed — see StepOutcome.degraded.
    failed: ctx.summary.some((step) => step.status === "failed" && step.degraded !== true),
    ...(cancelled ? { cancelled: true } : {}),
  };
  progress.completed = !result.failed && !cancelled;
  await persistProgress(ctx, progress);
  if (progress.completed && !ctx.dryRun) {
    try {
      if (isPlatformDefaultHermesRoot(ctx.hermesRoot, process.env, { homeDir: ctx.homeDir })) {
        removeHermesRootPointer(process.env, { homeDir: ctx.homeDir });
      } else {
        writeHermesRootPointer(ctx.hermesRoot, process.env, { homeDir: ctx.homeDir });
      }
    } catch {
      ctx.io.warn(
        "Setup completed, but the Hermes root pointer could not be updated; GUI launches without HERMES_HOME may use the platform default.",
      );
    }
  }
  hooks.finished?.(result);
  return result;
}

/**
 * True when a packaged build's installed engine is not the one this build
 * ships — INCLUDING when the payload is missing or unreadable.
 *
 * The earlier version returned false there, so a broken payload plus an
 * already-done Hermes step let setup finish with the old 1.3 network engine
 * still in place (review r2, finding 7). Forcing the step makes it run and
 * return its explicit packaged-payload failure instead.
 */
async function engineInstallRequired(ctx: WizardContext): Promise<boolean> {
  if (ctx.dryRun) return false;
  if (ctx.vendorRoot === undefined) return false;
  try {
    const manifest = await readVendorManifest(ctx.vendorRoot, { requireTrust: ctx.packaged });
    if (manifest === undefined) return ctx.packaged;
    return await vendoredEngineNeedsInstall(ctx, manifest);
  } catch {
    // An invalid manifest in a packaged app must be REPORTED, not retained.
    return ctx.packaged;
  }
}

function withPathHelp(outcome: StepOutcome): StepOutcome {
  const candidateLines = [...outcome.notes, ...(outcome.nextActions ?? [])];
  const helped = withFileExplorerAddressBarHint(candidateLines);
  if (helped.length === candidateLines.length) return outcome;
  return { ...outcome, notes: [...outcome.notes, helped.at(-1) ?? ""] };
}

async function persistProgress(ctx: WizardContext, progress: SetupProgress): Promise<void> {
  try {
    await writeSetupProgress(ctx, progress);
  } catch {
    // Progress is a convenience; a failed write must never fail setup.
  }
}

async function runStep(ctx: WizardContext, step: StepDefinition): Promise<StepOutcome> {
  try {
    return await step.run(ctx);
  } catch (error) {
    // Ctrl+C at a raw-mode prompt arrives here as a thrown cancellation, not as
    // an OS signal. Re-throwing keeps it out of the failed-step rendering that
    // let setup carry on to the next step after the customer had stopped it.
    if (isSetupCancelled(error)) throw error;
    return renderError(error);
  }
}

function retainStep(): StepOutcome {
  return { status: "done", notes: ["Completed in a previous run; left untouched."] };
}

/**
 * Start Engine, refused.
 *
 * Starting the engine is the single action that makes a provider route LIVE, so
 * it is the one action a profile that could not be proven safe may not have.
 * Reported as a failed step rather than a skipped one: setup is not finished,
 * and the customer is told what to do about it.
 */
function blockedGatewayStep(): StepOutcome {
  return {
    status: "failed",
    notes: [
      "I did not start the engine: I could not confirm this profile's model provider is safe to use, "
      + "and starting it would put that provider back to work.",
    ],
    nextActions: [
      "Run the Model step above and paste an API key — Anthropic, OpenAI or OpenRouter all work.",
      "If the Model step could not finish either, restart this computer and open JARVIS again.",
      "Still stuck? Use COPY DIAGNOSTICS and email jarvis.agent.tech@pm.me.",
    ],
    props: { gateway_supervised: false, gateway_state: "blocked-unsafe-provider" },
  };
}

/** Exported for the tests that pin what a THROWN step failure still carries. */
export function renderError(error: unknown): StepOutcome {
  if (error instanceof WizardError) {
    return {
      status: "failed",
      notes: [`${error.message} Fix: ${error.hint}`],
      fatal: error.fatal,
      // Whatever the step had already learned before it threw.
      ...(error.props === undefined ? {} : { props: error.props }),
    };
  }

  const message = error instanceof Error ? error.message : String(error);
  // A step can attach what it had learned to ANY error it throws, not only a
  // WizardError (steps/model.ts) — an ordinary EACCES kills the step just as
  // dead, and support needs the same evidence.
  const carried = (error as { props?: Record<string, string | number | boolean> } | null)?.props;
  return {
    status: "failed",
    notes: [`${message} Fix: rerun jarvis-setup after correcting the issue above.`],
    ...(carried === undefined ? {} : { props: carried }),
  };
}
