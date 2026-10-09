import {
  buildLastRunRecord,
  createContext,
  jarvisConfigDir,
  loadSupportCode,
  markWizardGreeted,
  redactSecrets,
  runSetup,
  telemetryEnabled,
  withTelemetry,
  wizardGreeted,
  writeLastRun,
  type LastRunRecord,
  type SecretPromptGuide,
  type SecretPromptOptions,
  type SetupIO,
  type StepStatus,
  type StepSummary,
} from "@jarvis/setup/engine";
import type { GatewayControl } from "@jarvis/setup/engine";
import { deviceCodeEvent } from "./setup-protocol.js";
import type { SetupAnswerValue, SetupEvent, SetupPrompt } from "./setup-protocol.js";

interface SetupHostOptions {
  dryRun: boolean;
  packaged: boolean;
  /** Explicit RUN SETUP AGAIN: run every step, never retain a previous done. */
  rerunAll?: boolean;
  slackOnly?: boolean;
  repoRoot: string;
  appVersion: string;
  /** Root of the bundled offline engine payload, when this build carries one. */
  vendorRoot?: string;
  /** The HUD-supervised gateway; the Start Engine step drives it. */
  gateway?: GatewayControl;
  emitEvent: (event: SetupEvent) => void;
  emitPrompt: (prompt: SetupPrompt) => void;
}

export interface SetupHost {
  start(): Promise<{ failed: boolean }>;
  answer(promptId: string, value: SetupAnswerValue): void;
  cancel(): void;
  /**
   * Lane C (voice): dispose anything a step registered that owns an OS
   * resource (the voice self-test's child processes). Called on quit so a
   * helper started mid-run is not orphaned past the drain cap.
   */
  disposeResources(): void;
  /**
   * The just-finished run's step record, preferred over the on-disk copy by
   * the diagnostics IPC: it survives an unwritable config dir, and it cannot
   * race the best-effort write kicked off in the finished hook.
   */
  lastRunRecord(): LastRunRecord | undefined;
  /**
   * Resolves once no run is in flight — including its final telemetry flush.
   * Shutdown must await this: Electron does not wait for pending promises,
   * and quitting mid-flush drops the abandoned-run events.
   */
  idle(): Promise<void>;
}

interface PendingPrompt {
  resolve: (value: SetupAnswerValue) => void;
  reject: (error: Error) => void;
}

export function createSetupHost(options: SetupHostOptions): SetupHost {
  const pending = new Map<string, PendingPrompt>();
  // Closing the window must stop work, not just stop asking questions: a
  // device-code login is a child process, not a pending prompt, so rejecting
  // prompts alone left it polling after the customer had gone.
  let aborter = new AbortController();
  let stepId = "";
  let promptSeq = 0;
  let cancelled = false;
  let running = false;
  let activeRun: Promise<unknown> | null = null;
  let lastRun: LastRunRecord | undefined;
  // The finished-hook persistence is tracked here so idle()/the before-quit
  // drain awaits it — a fire-and-forget write could lose the record if the
  // window closed the instant setup finished (Codex review 2026-08-11, P2).
  let lastRunWrite: Promise<void> = Promise.resolve();
  // Lane C (voice): OS resources a step owns while it runs.
  const disposables = new Set<{ dispose(): void }>();

  const ask = (prompt: Omit<SetupPrompt, "promptId">): Promise<SetupAnswerValue> => {
    if (cancelled) {
      return Promise.reject(new Error("Setup was closed before this answer arrived."));
    }
    promptSeq += 1;
    const promptId = `prompt-${promptSeq}`;
    return new Promise<SetupAnswerValue>((resolve, reject) => {
      pending.set(promptId, { resolve, reject });
      options.emitPrompt({ promptId, ...prompt });
    });
  };

  const truthy = (value: SetupAnswerValue): boolean => value === true || value === "true";

  const io: SetupIO = {
    inheritsStdio: false,
    async confirm(message: string, defaultValue: boolean): Promise<boolean> {
      return truthy(await ask({ kind: "confirm", message, defaultValue }));
    },
    async select(message, choices, defaultKey): Promise<string> {
      const answer = await ask({
        kind: "select",
        message,
        choices: choices.map((choice) => ({
          key: choice.key,
          label: choice.label,
          description: choice.description,
          hotkey: choice.hotkey,
        })),
        defaultValue: defaultKey,
      });
      return typeof answer === "string" ? answer : defaultKey;
    },
    async promptText(message: string, defaultValue: string, guide?: SecretPromptGuide): Promise<string> {
      const answer = await ask({ kind: "text", message, defaultValue, guide });
      return typeof answer === "string" ? answer : defaultValue;
    },
    async promptSecret(message: string, guide?: SecretPromptGuide, options?: SecretPromptOptions): Promise<string> {
      const answer = await ask({
        kind: "secret",
        message,
        secret: true,
        guide,
        ...(options?.emptySubmitLabel === undefined ? {} : { emptySubmitLabel: options.emptySubmitLabel }),
      });
      return typeof answer === "string" ? answer : "";
    },
    working(active: boolean): void {
      options.emitEvent({ type: "working", stepId, active });
    },
    offerEscape(label: string) {
      // Rides the same pending-answer channel as prompts: the renderer
      // answers `escapeId` on click. Unlike a prompt, nothing awaits it —
      // `taken` NEVER rejects (cancel() rejects pending prompts; this one's
      // reject is a no-op so a closed window cannot turn an unused offer
      // into an unhandled rejection).
      promptSeq += 1;
      const escapeId = `prompt-${promptSeq}`;
      let takenResolve: () => void = () => {};
      const taken = new Promise<void>((resolve) => {
        takenResolve = resolve;
      });
      pending.set(escapeId, { resolve: () => takenResolve(), reject: () => {} });
      options.emitEvent({ type: "escape-offer", stepId, escapeId, label });
      return {
        taken,
        retract: () => {
          pending.delete(escapeId);
          options.emitEvent({ type: "escape-retract", stepId, escapeId });
        },
      };
    },
    deviceCode(prompt) {
      // The code is a one-time device code, not a credential, and it is
      // useless without the customer's own browser session — it still goes
      // through redactSecrets so a future format change cannot leak a token.
      options.emitEvent(deviceCodeEvent(stepId, prompt, redactSecrets));
    },
    note(text) {
      options.emitEvent({ type: "note", stepId, text: redactSecrets(text) });
    },
    warn(text) {
      options.emitEvent({ type: "warn", stepId, text: redactSecrets(text) });
    },
    error(text) {
      options.emitEvent({ type: "error", stepId, text: redactSecrets(text) });
    },
    command(text) {
      options.emitEvent({ type: "command", stepId, text: redactSecrets(text) });
    },
    commandOutput(chunk, stream) {
      options.emitEvent({ type: "command-output", stepId, stream, chunk: redactSecrets(chunk) });
    },
    block(title, lines) {
      options.emitEvent({ type: "block", stepId, title, lines: lines.map(redactSecrets) });
    },
  };

  return {
    async start(): Promise<{ failed: boolean }> {
      if (running) {
        throw new Error("Setup is already running.");
      }
      // A just-finished run may still be draining telemetry (activeRun covers
      // the flush). Two live telemetry instances would interleave writes to
      // the shared queue file and clobber each other's batches, so a retry
      // waits for the drain instead of racing it.
      if (activeRun !== null) {
        await activeRun;
        if (running) {
          throw new Error("Setup is already running.");
        }
      }
      running = true;
      cancelled = false;
      // The steps event is NOT emitted here: the engine resolves the actual
      // sequence (the Earlier Install step exists only when its probe finds
      // something) and reports it through the `planned` hook below — the
      // renderer must never briefly show a step this machine will not get.
      aborter = new AbortController();
      const ctx = createContext({
        registerDisposable: (disposable) => {
          disposables.add(disposable);
          return () => disposables.delete(disposable);
        },
        dryRun: options.dryRun,
        packaged: options.packaged,
        version: options.appVersion,
        io,
        repoRoot: options.repoRoot,
        signal: aborter.signal,
        ...(options.vendorRoot === undefined ? {} : { vendorRoot: options.vendorRoot }),
        ...(options.gateway === undefined ? {} : { gateway: options.gateway }),
      });

      // The support code is resolved at FINISH (below), not here — so its
      // opt-out gate re-reads the profile .env at the same point telemetry
      // decides whether to send, and a mid-run opt-out suppresses both together.
      let finishedResult: { summary: StepSummary[]; failed: boolean } | undefined;

      const telemetry = withTelemetry(ctx, {
        stepStarted: (event) => {
          stepId = event.id;
          options.emitEvent({
            type: "step-started",
            id: event.id,
            index: event.index,
            total: event.total,
            previousStatus: event.previousStatus,
          });
        },
        stepFinished: (event) => {
          options.emitEvent({
            type: "step-finished",
            id: event.id,
            outcome: event.outcome,
            ...(event.retained === true ? { retained: true } : {}),
          });
        },
        finished: (result) => {
          finishedResult = { summary: result.summary, failed: result.failed };
          lastRun = buildLastRunRecord({
            summary: result.summary,
            failed: result.failed,
            appVersion: options.appVersion,
            packaged: options.packaged,
          });
          if (!options.dryRun) lastRunWrite = writeLastRun(jarvisConfigDir(), lastRun);
        },
      }, { appVersion: options.appVersion });

      // "Has the wizard EVER opened here" rides a durable marker in the app
      // config dir, not the profile's setup state: setup state reads
      // `undefined` for missing, corrupt, unreadable and unsupported alike, so
      // every failure mode replayed the grand first-meeting greeting — and so
      // did closing the wizard before any step persisted state (Codex
      // mechanism review 2026-08-14, finding 3). Uncertainty counts as seen.
      const firstRun = await wizardGreeted(jarvisConfigDir()).then(
        (seen) => !seen,
        () => false,
      );
      // The greeting plays at WIZARD-OPEN (finding 1): this event goes out
      // before the engine's planning work — the safety pass, repair and the
      // legacy probe can take several silent seconds, and the voice exists
      // precisely to cover that wait.
      options.emitEvent({ type: "opening", firstRun, dryRun: options.dryRun });
      // Marked immediately, not at finish: an open IS a greeting, and a
      // customer who closes mid-run must not be re-introduced to next time.
      // Dry runs are a developer tool and leave no trace.
      if (!options.dryRun) await markWizardGreeted(jarvisConfigDir());

      const run = (async () => {
        try {
          const result = await runSetup(
            ctx,
            {
              ...telemetry.hooks,
              planned: (steps) => {
                options.emitEvent({ type: "steps", steps, dryRun: options.dryRun });
              },
            },
            undefined,
            undefined,
            { rerunAll: options.rerunAll === true, slackOnly: options.slackOnly === true },
          );
          if (finishedResult !== undefined) {
            // Recheck the opt-out here, at the same moment telemetry decides
            // whether to flush — so a mid-run opt-out hides the code AND the
            // events together, never a code with no server record behind it.
            const supportCode = (await telemetryEnabled(ctx))
              ? await loadSupportCode(jarvisConfigDir(), { mint: !options.dryRun }).catch(() => undefined)
              : undefined;
            options.emitEvent({
              type: "finished",
              summary: finishedResult.summary,
              failed: finishedResult.failed,
              ...(supportCode === undefined ? {} : { supportCode }),
            });
          }
          return { failed: result.failed };
        } catch (error) {
          // A cancelled run is a closed window, not a crash worth reporting back.
          if (cancelled) return { failed: true };
          throw error;
        } finally {
          running = false;
          // Also on the cancelled/interrupted path — abandoned runs are exactly
          // what this telemetry measures. Persist-first flushing means even a
          // quit racing this await leaves the events queued for the next run.
          await telemetry.flush();
          // The last-run record backs Copy Diagnostics after a relaunch; await
          // it here so a quit racing the finish screen can't drop it.
          await lastRunWrite.catch(() => {});
        }
      })();
      activeRun = run.catch(() => {}).finally(() => {
        activeRun = null;
      });
      return run;
    },
    idle(): Promise<void> {
      return activeRun === null ? Promise.resolve() : activeRun.then(() => {});
    },
    lastRunRecord(): LastRunRecord | undefined {
      return lastRun;
    },
    answer(promptId, value) {
      const waiting = pending.get(promptId);
      if (waiting === undefined) return;
      pending.delete(promptId);
      waiting.resolve(value);
    },
    cancel() {
      cancelled = true;
      aborter.abort();
      for (const [promptId, waiting] of pending) {
        pending.delete(promptId);
        waiting.reject(new Error("Setup was closed."));
      }
      // A cancelled run must also stop anything it started. Rejecting the
      // prompts alone leaves a running self-test helper with nobody to kill it.
      this.disposeResources();
    },
    disposeResources() {
      for (const disposable of [...disposables]) {
        disposables.delete(disposable);
        try {
          disposable.dispose();
        } catch {
          // Best effort: one bad disposer must not block the rest of shutdown.
        }
      }
    },
  };
}
