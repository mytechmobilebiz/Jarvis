import type { DeviceCodeStatus, SecretPromptGuide, StepOutcome, StepStatus, StepSummary } from "@jarvis/setup/engine";

export type { DeviceCodeStatus, SecretPromptGuide };

export interface SetupOpening {
  /**
   * True when the wizard has NEVER opened on this machine (durable marker in
   * the app config dir — see wizard-greeted.ts; conservative: uncertainty is
   * "seen"). Decides the full time-of-day greeting vs the short re-greeting.
   */
  firstRun: boolean;
  /** Dry runs make no changes and get NO voice cues at all. */
  dryRun: boolean;
}

// "gate" is deliberately GONE (Peter, 2026-08-14): the wizard never asks
// permission to run a step. Steps run by themselves; the prompts that remain
// are real product questions asked inside them.
export type SetupPromptKind = "confirm" | "select" | "text" | "secret";

export interface SetupPromptChoice {
  key: string;
  label: string;
  description: string;
  hotkey: string;
}

export interface SetupPrompt {
  promptId: string;
  kind: SetupPromptKind;
  message: string;
  choices?: SetupPromptChoice[];
  defaultValue?: string | boolean;
  secret?: boolean;
  /** Optional muted help line rendered under the field, with one clickable link. */
  guide?: SecretPromptGuide;
  /**
   * Secret prompts only. When set, an empty submission is legal and the submit
   * button wears this label while the field is empty ("KEEP SAVED KEY"); when
   * absent, submit is DISABLED until something is typed — a live button that
   * does nothing is a mystery (Peter's smoke feedback, 2026-08-14).
   */
  emptySubmitLabel?: string;
}

export type SetupAnswerValue = string | boolean;

export interface SetupAnswer {
  promptId: string;
  value: SetupAnswerValue;
}

export interface SetupStepInfo {
  id: string;
  title: string;
}

export type SetupEvent =
  /**
   * Emitted the moment a run starts, BEFORE the engine's planning work (the
   * safety pass, repair, the legacy probe) — the voice greeting must play at
   * wizard-open, not after several seconds of silent groundwork (Codex
   * mechanism review 2026-08-14, finding 1).
   */
  | { type: "opening"; firstRun: boolean; dryRun: boolean }
  | {
      type: "steps";
      steps: SetupStepInfo[];
      dryRun: boolean;
      /** @deprecated The greeting rides the `opening` event now. */
      firstRun?: boolean;
    }
  /**
   * The honest busy signal: a child process is running (`active: true`) or has
   * settled (`false`). Balanced pairs; the renderer counts them and shows the
   * animated working indicator while any are outstanding.
   */
  | { type: "working"; stepId: string; active: boolean }
  /**
   * A small non-blocking way out while long AUTOMATIC work runs (the
   * saved-key auto-verify's "Use a different key instead"). Not a prompt:
   * nothing waits on it, the work continues, and it usually retracts unused.
   * The renderer shows a muted control; clicking it answers `escapeId` like
   * any prompt. `escape-retract` (or a step change) removes the control.
   */
  | { type: "escape-offer"; stepId: string; escapeId: string; label: string }
  | { type: "escape-retract"; stepId: string; escapeId: string }
  | { type: "step-started"; id: string; index: number; total: number; previousStatus?: StepStatus }
  | { type: "note"; stepId: string; text: string }
  | { type: "warn"; stepId: string; text: string }
  | { type: "error"; stepId: string; text: string }
  | { type: "command"; stepId: string; text: string }
  | { type: "command-output"; stepId: string; stream: "stdout" | "stderr"; chunk: string }
  | { type: "block"; stepId: string; title: string; lines: string[] }
  /**
   * A device-code sign-in in progress. Not a prompt: the customer answers in
   * their browser and the engine detects it, so the wizard shows this and keeps
   * waiting rather than blocking on an answer from the renderer.
   */
  | {
      type: "device-code";
      stepId: string;
      attempt: number;
      status: DeviceCodeStatus;
      message: string;
      url?: string;
      code?: string;
    }
  | {
      type: "step-finished";
      id: string;
      outcome: StepOutcome;
      /** True when an already-done step was retained rather than re-run. */
      retained?: boolean;
    }
  | { type: "finished"; summary: StepSummary[]; failed: boolean; supportCode?: string };

/**
 * What COPY DIAGNOSTICS puts on the clipboard, built entirely in the main
 * process so the wizard failure panel and the settings dialog share one
 * source of truth. The text is human-readable on purpose: the customer sees
 * exactly what they would be sharing before they paste it into an email.
 */
export interface DiagnosticsReport {
  supportCode?: string;
  text: string;
}

/**
 * Build the renderer event for one device-code lifecycle step.
 *
 * Extracted from the host so the mapping is testable without driving a whole
 * wizard run: which fields survive redaction, and — the property that matters —
 * that a finished attempt carries NO url or code, so the renderer cannot leave
 * an expired one on screen looking live.
 */
export function deviceCodeEvent(
  stepId: string,
  prompt: { attempt: number; status: DeviceCodeStatus; message: string; url?: string; code?: string },
  redact: (value: string) => string,
): Extract<SetupEvent, { type: "device-code" }> {
  const live = prompt.status === "waiting";
  return {
    type: "device-code",
    stepId,
    attempt: prompt.attempt,
    status: prompt.status,
    message: redact(prompt.message),
    ...(live && prompt.url !== undefined ? { url: prompt.url } : {}),
    ...(live && prompt.code !== undefined ? { code: redact(prompt.code) } : {}),
  };
}
