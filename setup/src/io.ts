import type { StepStatus } from "./context.js";

export interface SetupChoice<T extends string = string> {
  key: T;
  label: string;
  description: string;
  hotkey: string;
}

export type CommandStream = "stdout" | "stderr";

/**
 * An optional help line shown with an input prompt: one sentence, plus one
 * link when a page exists to point at. The link is kept separate from the
 * sentence so a graphical frontend can make it clickable while a terminal
 * simply prints it. Some guides have no link at all (the Slack member ID
 * lives inside the Slack app, not on any web page) — both link fields are
 * absent together.
 */
export interface SecretPromptGuide {
  /** The sentence before the link. */
  text: string;
  /** The link exactly as a customer should read it, without a scheme. */
  linkText?: string;
  /** The absolute https URL `linkText` resolves to. */
  url?: string;
}

/**
 * A sign-in page the customer must open, with the short code they type into it.
 * Shown while the engine's device-code flow blocks on polling, so it is a
 * NOTIFICATION, not a prompt: nothing here waits for an answer, because the
 * answer arrives out of band (in the browser) and the engine detects it.
 */
export interface DeviceCodePrompt {
  /** One plain sentence naming the account being signed in to. */
  message: string;
  /**
   * Which attempt this belongs to, from 1. A retry issues a NEW code, and the
   * expired one must stop looking actionable, so a frontend keys its card on
   * this rather than appending a second live-looking card.
   */
  attempt: number;
  /** Where this attempt got to. A card that only ever says "waiting" lies. */
  status: DeviceCodeStatus;
  /** The verification URL, exactly as the engine printed it. Waiting only. */
  url?: string;
  /** The short code to type on that page. Waiting only. */
  code?: string;
}

/** Lifecycle of one device-code attempt. A CLOSED SET. */
export type DeviceCodeStatus = "waiting" | "signed-in" | "failed" | "cancelled";

/**
 * How a secret prompt treats an EMPTY submission.
 *
 * When `emptySubmitLabel` is set, submitting nothing is a legal, meaningful
 * answer, and the label says in plain words what it does ("KEEP SAVED KEY") —
 * a graphical frontend swaps its submit button to that label while the field
 * is empty. When absent, an empty submission would do nothing at all, so a
 * graphical frontend disables submit until something is typed (Peter's smoke
 * feedback, 2026-08-14: a live button that does nothing is a mystery).
 */
export interface SecretPromptOptions {
  emptySubmitLabel?: string;
}

/**
 * A small non-blocking way out, offered while long AUTOMATIC work runs.
 *
 * The saved-key auto-verify runs with no key UI at all ("JARVIS never asks
 * for a key he already has" — Peter, 2026-08-14); this is the deliberate-
 * switch affordance that keeps that from becoming a trap: without it, a
 * customer with a WORKING saved key could never reach the paste screen to
 * change it. Not a prompt — nothing waits on it, work continues, and it
 * usually retracts unused.
 */
export interface EscapeOffer {
  /** Resolves if the customer takes the escape. NEVER rejects. */
  taken: Promise<void>;
  /** Withdraw the offer (idempotent); frontends remove the control. */
  retract(): void;
}

/** Every prompt and every line of output a step produces flows through this interface. */
export interface SetupIO {
  /** True when child processes may inherit this process's stdio, as in a real terminal. */
  readonly inheritsStdio: boolean;
  // There is deliberately NO stepGate here. The wizard used to ask "Shall I
  // run X, sir?" before every step; Peter ruled that meta-prompt out
  // (2026-08-14): mechanical steps just run, steps that need input open
  // straight on their own question, and a real product choice is a confirm
  // inside the step — never permission to ask.
  confirm(message: string, defaultValue: boolean): Promise<boolean>;
  select(message: string, choices: SetupChoice[], defaultKey: string): Promise<string>;
  promptText(message: string, defaultValue: string, guide?: SecretPromptGuide): Promise<string>;
  promptSecret(message: string, guide?: SecretPromptGuide, options?: SecretPromptOptions): Promise<string>;
  /**
   * An honest busy signal: emitted with `true` the moment a child process is
   * spawned and `false` when it settles, so a frontend can show that JARVIS is
   * WORKING during the silent seconds of a verification chat or an engine
   * operation (Peter's smoke feedback, 2026-08-14: a fresh key "did nothing"
   * for ten seconds — it was working the whole time and never said so).
   * Optional because a terminal streams the child's own output instead.
   */
  working?(active: boolean): void;
  /**
   * Offer an EscapeOffer (see above) labelled in plain words ("Use a
   * different key instead"). Optional: a terminal frontend streams output and
   * has Ctrl+C; only the GUI needs a rendered control.
   */
  offerEscape?(label: string): EscapeOffer;
  /**
   * Show a device-code sign-in: URL plus code, while the engine polls. It
   * returns nothing — the customer answers in their browser, not here.
   */
  deviceCode(prompt: DeviceCodePrompt): void;
  note(text: string): void;
  warn(text: string): void;
  error(text: string): void;
  /** The exact command about to run; frontends without inherited stdio show it verbatim. */
  command(text: string): void;
  commandOutput(chunk: string, stream: CommandStream): void;
  /** A titled block of preformatted lines, used by the summary step. */
  block(title: string, lines: string[]): void;
}
