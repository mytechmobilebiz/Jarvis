import type { StepStatus } from "@jarvis/setup/engine";

/**
 * Since the per-step gate is gone (Peter, 2026-08-14) a "skipped" outcome can
 * only mean the step looked and found nothing to do — nobody instructed
 * anything, so the old "Skipped, as instructed." would be a small lie now.
 */
export function stepVerdict(status: StepStatus): string {
  if (status === "done") return "Done, sir.";
  if (status === "failed") return "That did not go to plan, sir.";
  return "Nothing to do there, sir.";
}

/**
 * The sign-in card shown while a device-code login polls. Kept here with the
 * rest of the customer-facing copy rather than inline in the renderer: this is
 * the one screen a non-technical buyer must act on outside the app, so the
 * words matter more than the markup.
 */
export const SIGN_IN_CARD = {
  heading: "SIGN IN",
  /** Under the code: says who is waiting for what, and what not to do. */
  waiting: "Waiting for you to finish in the browser. Leave this window open.",
  /** Shown when the browser could not be opened for them. */
  openManually: "If no browser opened, copy the address above into one yourself.",
} as const;

export function setupStepIntros(platform: string): Record<string, string> {
  const windows = platform.toLowerCase().startsWith("win");
  return {
    // Plain language throughout (Peter, 2026-08-14): no tool names, no paths,
    // no environment variables in anything the wizard says out loud. The
    // technical detail lives in the setup log, not the narration.
    preflight: "First, a quick look around to make sure this computer has everything JARVIS needs. Nothing is changed — I only check.",
    // Only present when an earlier install was actually found; it runs by
    // itself and narrates one plain line (the mechanics go to the log).
    migrate: "I found traces of an earlier JARVIS on this computer, so I will tidy them away before we start. Your keys, settings, and history stay exactly where they are.",
    hermes: "Next I install the engine your staff runs on. It is carried inside the application, so there is nothing to download — give it a moment.",
    profile: "Now I set up your JARVIS office: the workspace, briefs, skills, and schedules.",
    "compose-roles": "Time to bring in the staff. Sarah, Tom, Bobby, Scout, and Ava join the household and their personas are merged into your operations manual.",
    model: "Now the Brain, sir. JARVIS thinks with the ChatGPT or Claude plan you already have — no separate bill.",
    slack: "Slack gives you the phone brief and remote approvals. Optional, sir, though I do recommend it.",
    // v1.4.0 (Lane C): voice is a shipped feature on Windows as well as macOS,
    // and it lives in this HUD rather than in the terminal. The copy no longer
    // apologises, and no longer promises anything the install cannot do.
    voice: "Now the voice. It travels inside the application, so there is nothing to download: I check the speech files, say a test sentence, and confirm I can hear you. Afterwards, hold the microphone in the chat bar and simply talk to me.",
    gateway: "Now I start the engine myself and ask it a question through its own door to be sure it answers. Nothing is installed to run behind your back: the engine lives while JARVIS is open, and I hand it its credentials directly.",
    "install-app": windows
      ? "Now I build the JARVIS HUD installer, so you can open me from the taskbar or Start menu rather than a terminal."
      : "Now I build JARVIS HUD as a proper application on this machine and install it, so you open me from the Dock or applications menu rather than a terminal.",
    hello: "A short hello, to confirm the office is open.",
    summary: "And finally the ledger: what was done, and what remains for you.",
  };
}
