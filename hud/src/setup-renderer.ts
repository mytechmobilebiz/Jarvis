import type { StepOutcome, StepStatus, StepSummary } from "@jarvis/setup/engine";
import type { JarvisBridge } from "./preload.cjs";
import { SIGN_IN_CARD, setupStepIntros, stepVerdict } from "./setup-copy.js";
import type { DeviceCodeStatus, SecretPromptGuide, SetupEvent, SetupPrompt, SetupPromptChoice, SetupStepInfo } from "./setup-protocol.js";
import { sanitizeSecret, secretHint } from "./secret-input.js";
import { createTypingDots } from "./typing-dots.js";
import { WIZARD_AUDIO_CUES, createWizardAudioPlayer, greetingMomentFor } from "./wizard-audio.js";
import {
  createTransition,
  type TransitionController,
  type TransitionElements,
  type TransitionMode,
  type TransitionStartOptions,
} from "./wizard-transition.js";

declare global {
  interface Window {
    jarvis?: JarvisBridge;
  }
}

const bridge = window.jarvis;
const STEP_INTROS = setupStepIntros(bridge?.platform ?? "");

const STATUS_LABELS: Record<StepStatus | "pending" | "running", string> = {
  pending: "PENDING",
  running: "RUNNING",
  done: "DONE",
  skipped: "SKIPPED",
  failed: "FAILED",
};

const URL_PATTERN = /https?:\/\/[^\s<>()"']+/g;

const els = {
  steps: must<HTMLElement>("steps"),
  log: must<HTMLElement>("log"),
  status: must<HTMLElement>("status"),
  dryRun: must<HTMLElement>("dry-run"),
};

const chips = new Map<string, HTMLElement>();
const tiles = new Map<string, HTMLElement>();
const outputs = new Map<string, HTMLDetailsElement>();
// One sign-in card per device-code attempt, so a retry replaces the dead code
// instead of stacking a second live-looking one under it.
const signInCards = new Map<number, HTMLElement>();
let currentStepId = "";

// The wizard's voice (baked cues, pre-TTS). Cues QUEUE and play strictly in
// order (Peter, 2026-08-14) — work never waits, only the presentation
// serializes. Each clip's transcript prints the moment it STARTS speaking
// (never voice-only), and narration printing (only printing — never work,
// never prompts) then defers until that clip ends, so lines at the ads'
// speaking rate (4-6s, hard cap ~8s) do not straddle unrelated text
// scrolling underneath them. Dropped-stale cues go to the console, not the
// customer's log.
const wizardAudio = createWizardAudioPlayer({
  onTranscript: (text) => {
    appendLine("J.A.R.V.I.S.", text);
    holdNarration();
  },
  onCueEnded: (momentId) => {
    releaseHeldNarration();
    // The birth's one audio coupling: Td rides the player's own settle
    // semantics, through this existing wiring (TRANSITION-DESIGN.md §9).
    // ALSO recorded (integration review r2 P2): setup.complete can end — or
    // fail instantly — while startAutoBirth is still awaiting the
    // lifecycleDebt IPC; a settlement delivered to a null controller was
    // lost and the birth then sat on its 15-second cue ceiling. The birth
    // consumes this record when it starts.
    settledCues.add(momentId);
    transition?.notifyCueSettled(momentId);
  },
  onCueDropped: (momentId, why) => console.warn(`wizard-audio: dropped ${momentId} (${why})`),
});
/** Cue endings seen so far — consulted when a birth starts AFTER its cue ended (r2 P2). */
const settledCues = new Set<string>();
declare global {
  interface Window {
    /** Read by the packaged wizard smoke: proves the audio graph is alive. */
    __wizardAudio?: {
      status(): string;
      reason(): string | undefined;
      probe(): boolean;
      probeUrl(): string;
      probeQueue(): boolean;
      order(): string[];
      busy(): boolean;
    };
  }
}
const PROBE_MOMENT = "model.verified";
window.__wizardAudio = {
  status: () => wizardAudio.status(),
  // Lets the smoke tell an autoplay REGRESSION (NotAllowedError) from a
  // machine with no usable audio output (Codex review 2026-08-14, finding 5).
  reason: () => wizardAudio.silentReason(),
  // The smoke calls this to prove a clip actually PLAYS in the packaged app
  // without a user gesture — the autoplay property cannot be proven
  // statically. Unmuted first: the probe tests the pipeline, not a persisted
  // preference (finding 5).
  probe: () => {
    wizardAudio.setMuted(false);
    return wizardAudio.play(PROBE_MOMENT);
  },
  // The URL the probe actually played, derived from the SAME catalog entry —
  // the smoke's no-device decode fallback must exercise the real cue path,
  // not a separately hard-coded filename that could stay green while the
  // catalog rots (Codex re-review 2026-08-14, F1).
  probeUrl: () => `wizard-audio/${WIZARD_AUDIO_CUES[PROBE_MOMENT]?.file ?? ""}`,
  // Serialization is load-bearing (Peter's queue rule, 2026-08-14): the smoke
  // fires two cues and proves they START strictly in order — which also
  // proves real 'ended' events reach the player in this app shape, because
  // the second cue can only ever start when the first one's playback settles.
  probeQueue: () => {
    wizardAudio.setMuted(false);
    const first = wizardAudio.play("voice.handoff");
    const second = wizardAudio.play("engine.install");
    return first && second;
  },
  order: () => [...wizardAudio.startedOrder()],
  busy: () => wizardAudio.busy(),
};
let greeted = false;
// Dry runs make no changes; a voice announcing an open office over one would
// lie (Codex review 2026-08-14, finding 8). Set by the opening event.
let voiceSuppressed = false;

/**
 * Fire a cue. The player queues it behind whatever is speaking; the
 * narration hold engages when the clip actually STARTS (onTranscript above).
 * Returns whether a real playback is now in flight or queued — false for
 * suppressed/muted runs, which the birth reads as "no cue to wait for".
 */
function playCue(momentId: string): boolean {
  if (voiceSuppressed) return false;
  return wizardAudio.play(momentId);
}

function playGreeting(firstRun: boolean): void {
  // RETRY SETUP re-runs on the same open window; one open, one greeting.
  if (greeted || voiceSuppressed) return;
  greeted = true;
  playCue(greetingMomentFor(new Date().getHours(), firstRun));
}

// ---------------------------------------------------------------------------
// Deferred narration: while a clip is speaking, narration LINES queue and
// flush when the clip ends (or after a hard 8.5s cap — no clip is allowed to
// be longer, and a wedged 'ended' event must not dam the log forever).
// Prompts, sign-in cards and the finish panel are never deferred: a question
// sitting on screen while the voice finishes is the sanctioned overlap.
// ---------------------------------------------------------------------------
let heldNarration: (() => void)[] | null = null;
let heldNarrationTimer: number | undefined;

function holdNarration(): void {
  if (heldNarration !== null) return;
  heldNarration = [];
  heldNarrationTimer = window.setTimeout(releaseHeldNarration, 8500);
}

function releaseHeldNarration(): void {
  if (heldNarrationTimer !== undefined) window.clearTimeout(heldNarrationTimer);
  heldNarrationTimer = undefined;
  const queue = heldNarration;
  heldNarration = null;
  for (const flush of queue ?? []) flush();
}

/** Append a narration node to the log now, or after the current clip ends. */
function appendNarration(node: HTMLElement): void {
  if (heldNarration !== null) {
    heldNarration.push(() => {
      els.log.append(node);
      keepWorkingDotsLast();
      scrollLog();
    });
    return;
  }
  els.log.append(node);
  keepWorkingDotsLast();
  scrollLog();
}

// ---------------------------------------------------------------------------
// The working indicator: balanced working(true/false) pairs arrive from the
// engine around every child process (SetupIO.working). The dots appear only
// when the busy stretch outlives 600ms — a fast command must not flicker —
// and always sit at the bottom of the log, like a messenger's typing bubble.
// ---------------------------------------------------------------------------
let workingCount = 0;
let workingLine: HTMLElement | null = null;
let workingTimer: number | undefined;

function setWorking(active: boolean): void {
  workingCount = Math.max(0, workingCount + (active ? 1 : -1));
  if (workingCount > 0) {
    if (workingLine !== null || workingTimer !== undefined) return;
    workingTimer = window.setTimeout(() => {
      workingTimer = undefined;
      if (workingCount === 0 || workingLine !== null) return;
      const line = document.createElement("div");
      line.className = "line";
      const label = document.createElement("div");
      label.className = "speaker";
      label.textContent = "J.A.R.V.I.S.";
      const body = document.createElement("p");
      body.append(createTypingDots(document));
      line.append(label, body);
      workingLine = line;
      els.log.append(line);
      scrollLog();
    }, 600);
    return;
  }
  if (workingTimer !== undefined) {
    window.clearTimeout(workingTimer);
    workingTimer = undefined;
  }
  workingLine?.remove();
  workingLine = null;
}

/** New log content goes UNDER everything except the live working dots. */
function keepWorkingDotsLast(): void {
  if (workingLine !== null && workingLine.isConnected) els.log.append(workingLine);
}

// ---------------------------------------------------------------------------
// The escape offer: a small muted way out shown while long AUTOMATIC work
// runs (the saved-key auto-verify's "Use a different key instead"). Not a
// prompt — nothing waits on it; clicking answers its id like any prompt and
// the engine changes course. The engine retracts it when the work settles,
// and a new run or step clears any stragglers.
// ---------------------------------------------------------------------------
const escapeOffers = new Map<string, HTMLElement>();

function renderEscapeOffer(escapeId: string, label: string): void {
  removeEscapeOffer(escapeId);
  const line = document.createElement("div");
  line.className = "line escape-offer";
  const body = document.createElement("p");
  const control = document.createElement("button");
  control.type = "button";
  control.textContent = label;
  control.style.cssText = "background:none;border:1px solid var(--line);color:var(--dim);"
    + "font:inherit;font-size:12px;padding:2px 10px;border-radius:4px;cursor:pointer;";
  control.addEventListener("click", () => {
    removeEscapeOffer(escapeId);
    bridge?.answerPrompt(escapeId, "true");
  });
  body.append(control);
  line.append(body);
  escapeOffers.set(escapeId, line);
  els.log.append(line);
  keepWorkingDotsLast();
  scrollLog();
}

function removeEscapeOffer(escapeId: string): void {
  escapeOffers.get(escapeId)?.remove();
  escapeOffers.delete(escapeId);
}

function clearEscapeOffers(): void {
  for (const escapeId of [...escapeOffers.keys()]) removeEscapeOffer(escapeId);
}

// ---------------------------------------------------------------------------
// The idle nudge (M5): sixty silent seconds at the Model step's own prompts,
// once per run — typing resets the clock, answering cancels it.
// ---------------------------------------------------------------------------
let modelIdleTimer: number | undefined;
let modelIdleNudged = false;

function armModelIdleNudge(): void {
  if (modelIdleNudged || currentStepId !== "model") return;
  clearModelIdleNudge();
  modelIdleTimer = window.setTimeout(() => {
    modelIdleTimer = undefined;
    if (modelIdleNudged) return;
    // The nudge only breaks a real silence: if JARVIS is speaking (or has
    // cues waiting), or any clip played within the last minute, this is not
    // idleness — re-arm and keep the once-per-run allowance (an idle nudge
    // mid-monologue would be absurd; Peter's queue rules, 2026-08-14).
    if (wizardAudio.busy() || Date.now() - wizardAudio.lastActivityAt() < 60_000) {
      armModelIdleNudge();
      return;
    }
    modelIdleNudged = true;
    playCue("model.idle");
  }, 60_000);
}

function clearModelIdleNudge(): void {
  if (modelIdleTimer !== undefined) window.clearTimeout(modelIdleTimer);
  modelIdleTimer = undefined;
}

/** The persisted SOUND toggle, in the topbar — defaulting to sound ON. */
function mountMuteToggle(): void {
  const bar = document.querySelector(".topbar");
  if (bar === null || document.getElementById("audio-toggle") !== null) return;
  const toggle = document.createElement("button");
  toggle.id = "audio-toggle";
  toggle.type = "button";
  toggle.style.cssText = "background:none;border:1px solid var(--line);color:var(--dim);"
    + "font:inherit;font-size:12px;padding:2px 8px;border-radius:4px;cursor:pointer;margin-left:12px;";
  const paint = (): void => {
    toggle.textContent = wizardAudio.muted() ? "SOUND OFF" : "SOUND ON";
    toggle.setAttribute("aria-pressed", wizardAudio.muted() ? "true" : "false");
    toggle.title = wizardAudio.muted() ? "JARVIS will stay quiet" : "JARVIS speaks during setup";
  };
  toggle.addEventListener("click", () => {
    wizardAudio.setMuted(!wizardAudio.muted());
    paint();
  });
  paint();
  bar.append(toggle);
}

// ---------------------------------------------------------------------------
// The Birth (spec/v150/TRANSITION-DESIGN.md): when setup finishes clean, the
// wizard recedes and the orb births itself while `setup.complete` plays — no
// click. One controller per attempt; the auto path still goes through the same
// setup:launch-hud IPC, so mayLeaveSetup stays the only door out.
// ---------------------------------------------------------------------------
let transition: TransitionController | null = null;

const MANUAL_BIRTH_CAPTION = "The office is open, sir.";

function prefersReducedMotion(): boolean {
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

/** The live audio verdict for the finished telemetry and the wake-SFX gate. */
function transitionSound(): "played" | "muted" | "silent" {
  if (wizardAudio.muted()) return "muted";
  return wizardAudio.status() === "silent" ? "silent" : "played";
}

/** The real overlay DOM, gathered fresh per attempt; null if the page rotted. */
function transitionElements(): TransitionElements | null {
  const overlay = document.getElementById("transition");
  const video = overlay?.querySelector<HTMLVideoElement>(".tr-orb") ?? null;
  const ring = overlay?.querySelector<HTMLElement>(".tr-ring") ?? null;
  const bezel = overlay?.querySelector<HTMLElement>(".tr-bezel") ?? null;
  const caption = overlay?.querySelector<HTMLElement>(".tr-caption") ?? null;
  const wait = overlay?.querySelector<HTMLElement>(".tr-wait") ?? null;
  if (overlay === null || video === null || ring === null || bezel === null || caption === null || wait === null) {
    return null;
  }
  const receded = [
    ...document.querySelectorAll<HTMLElement>(".topbar"),
    ...document.querySelectorAll<HTMLElement>(".setup-grid"),
    ...document.querySelectorAll<HTMLElement>("body > .corner"),
  ];
  return { overlay, video, ring, bezel, caption, wait, receded };
}

/**
 * Run the birth. Creates a fresh controller per attempt (an aborted overlay
 * must not wedge the next BOOT HUD click), wires the contract-refusal path
 * back to the classic card, and starts the choreography.
 */
function beginBirth(mode: TransitionMode, opts: TransitionStartOptions, restoreCard: () => void): boolean {
  if (bridge === undefined) return false;
  const launchHud = bridge.launchHud.bind(bridge);
  const sendTransitionEvent = bridge.sendTransitionEvent?.bind(bridge);
  if (transition !== null && transition.state() !== "idle" && transition.state() !== "aborted") {
    return true; // a birth is already running; never stack a second overlay
  }
  const elements = transitionElements();
  if (elements === null) return false;
  transition = createTransition({
    elements,
    launchHud,
    ...(sendTransitionEvent === undefined ? {} : {
      sendTelemetry: (name: string, props: Record<string, unknown>) => sendTransitionEvent(name, props),
    }),
    sound: transitionSound,
    onAborted: restoreCard,
  });
  transition.start(mode, opts);
  return true;
}

/** The escape is sacred: any pointer, any non-modifier key, at any beat. */
function wireBirthEscape(): void {
  window.addEventListener("pointerdown", () => transition?.skip(), { capture: true });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Shift" || event.key === "Control" || event.key === "Alt" || event.key === "Meta") return;
    transition?.skip();
  }, { capture: true });
}

declare global {
  interface Window {
    /** Read by the packaged wizard smoke: proves the birth machinery is alive. */
    __wizardTransition?: {
      /** The overlay scaffolding shipped with the page. */
      present(): boolean;
      /** A compressed full timeline against a stub door → final state. */
      probe(): Promise<{ state: string; launches: number; entrance: string | null }>;
      /** Start + immediate skip → exactly one fast launch. */
      probeSkip(): Promise<{ state: string; launches: number; entrance: string | null }>;
      /** The packaged orb asset, for the smoke's decode check. */
      videoUrl(): string;
      state(): string;
    };
  }
}

/**
 * The smoke's timeline probe: real (detached) elements, a stub launchHud, and
 * the whole choreography compressed through the controller's timeScale seam —
 * proving the beat machine, the skip escape and the launch call run in THIS
 * app shape without waiting 4.4 real seconds per probe.
 */
async function runTransitionProbe(kind: "full" | "skip"): Promise<{ state: string; launches: number; entrance: string | null }> {
  const surface = (): HTMLElement => document.createElement("div");
  let launches = 0;
  let entrance: string | null = null;
  const probe = createTransition({
    elements: {
      overlay: surface(),
      video: {
        src: "",
        preload: "none",
        play: () => Promise.resolve(),
        addEventListener: () => {},
      },
      ring: surface(),
      bezel: surface(),
      caption: surface(),
      wait: surface(),
      receded: [surface()],
    },
    launchHud: (opts) => {
      launches += 1;
      entrance = opts.entrance;
      return Promise.resolve();
    },
    timeScale: 0.02,
  });
  probe.start("auto", { caption: "probe", cueLive: false });
  if (kind === "skip") {
    probe.skip();
    probe.skip(); // idempotence is part of what the smoke asserts
  }
  await new Promise((resolve) => setTimeout(resolve, kind === "skip" ? 60 : 250));
  return { state: probe.state(), launches, entrance };
}

window.__wizardTransition = {
  present: () => {
    const overlay = document.getElementById("transition");
    return overlay !== null
      && overlay.querySelector(".tr-orb") !== null
      && overlay.querySelector(".tr-bezel") !== null
      && overlay.querySelector(".tr-wait") !== null;
  },
  probe: () => runTransitionProbe("full"),
  probeSkip: () => runTransitionProbe("skip"),
  videoUrl: () => "transition/orb.mp4",
  state: () => transition?.state() ?? "idle",
};

boot();

function boot(): void {
  if (bridge === undefined) {
    appendLine("J.A.R.V.I.S.", "The setup bridge is unavailable, sir. Launch the HUD with pnpm run hud so the preload script is in place.", "error");
    return;
  }

  bridge.onSetupEvent(handleEvent);
  bridge.onSetupPrompt(renderPrompt);
  mountMuteToggle();
  wireBirthEscape();
  void showVersion();
  void startSetup();
}

/** Muted build stamp in the wizard chrome; non-critical, so failures stay silent. */
async function showVersion(): Promise<void> {
  const el = document.getElementById("app-version");
  if (el === null || bridge?.getVersion === undefined) return;
  try {
    el.textContent = `v${await bridge.getVersion()}`;
  } catch {
    // Leave it blank rather than surface an error on the setup screen.
  }
}

async function startSetup(): Promise<void> {
  try {
    await bridge?.startSetup();
  } catch (error) {
    appendLine("J.A.R.V.I.S.", `Setup could not run: ${errorMessage(error)}`, "error");
  }
}

function handleEvent(event: SetupEvent): void {
  switch (event.type) {
    case "opening":
      // Emitted the instant the run starts: the greeting covers the engine's
      // silent planning seconds, which is the whole reason it exists.
      voiceSuppressed = event.dryRun;
      playGreeting(event.firstRun);
      break;
    case "steps":
      renderSteps(event.steps, event.dryRun);
      break;
    case "working":
      setWorking(event.active);
      break;
    case "escape-offer":
      renderEscapeOffer(event.escapeId, event.label);
      break;
    case "escape-retract":
      removeEscapeOffer(event.escapeId);
      break;
    case "step-started":
      startStep(event.id, event.index, event.total, event.previousStatus);
      break;
    case "note":
      appendLine("J.A.R.V.I.S.", event.text);
      break;
    case "warn":
      appendLine("J.A.R.V.I.S.", event.text, "warn");
      break;
    case "error":
      appendLine("J.A.R.V.I.S.", event.text, "error");
      // A refusal while the birth is playing (mayLeaveSetup, a blocked
      // gateway) aborts the overlay back to the classic card (§10). The
      // error line itself is already in the log via the append above.
      transition?.notifyErrorEvent();
      break;
    case "command":
      appendCommand(event.stepId, event.text);
      break;
    case "command-output":
      appendCommandOutput(event.stepId, event.chunk);
      break;
    case "block":
      appendBlock(event.title, event.lines);
      break;
    case "device-code":
      renderDeviceCode(event.attempt, event.status, event.message, event.url, event.code);
      break;
    case "step-finished":
      finishStep(event.id, event.outcome, event.retained === true);
      break;
    case "finished":
      renderFinish(event.summary, event.failed, event.supportCode);
      break;
  }
}

function renderSteps(steps: SetupStepInfo[], dryRun: boolean): void {
  els.steps.replaceChildren();
  chips.clear();
  tiles.clear();
  outputs.clear();
  signInCards.clear();
  currentStepId = "";
  els.dryRun.hidden = !dryRun;
  els.status.className = "status";
  els.status.textContent = "● SETUP";

  // A new run resets the once-per-run nudge (RETRY SETUP is a new run) —
  // and the working counter, so a run that died with an unbalanced busy
  // signal cannot leave phantom dots over the retry (Codex re-review
  // 2026-08-14, F2).
  modelIdleNudged = false;
  clearModelIdleNudge();
  workingCount = 0;
  setWorking(false); // count already 0: clamps to 0 and clears dots + timer
  clearEscapeOffers();
  // A retry is a NEW run: stop the old run's clip and drain its queue — a
  // butler must not narrate the previous attempt over this one (final
  // review 2026-08-14, F5).
  wizardAudio.reset();
  releaseHeldNarration();

  steps.forEach((step, index) => {
    const tile = document.createElement("div");
    tile.className = "step";

    const number = document.createElement("span");
    number.className = "step-index";
    number.textContent = String(index + 1).padStart(2, "0");

    const title = document.createElement("span");
    title.className = "step-title";
    title.textContent = step.title.toUpperCase();

    const chip = document.createElement("span");
    chip.className = "chip";
    chip.append(document.createElement("i"));
    const chipText = document.createElement("span");
    chipText.textContent = STATUS_LABELS.pending;
    chip.append(chipText);

    tile.append(number, title, chip);
    els.steps.append(tile);
    chips.set(step.id, chip);
    tiles.set(step.id, tile);
  });

  if (els.log.childElementCount === 0) {
    appendLine("J.A.R.V.I.S.", "Good day, sir. The sequence on the left is everything this computer needs; I will take care of the groundwork myself, narrate as I go, and check with you when a choice is truly yours to make.");
  }
}

function startStep(id: string, index: number, total: number, previousStatus?: StepStatus): void {
  currentStepId = id;
  // A step change means whoever offered an escape is gone; the engine's own
  // retract is the normal path, this is the belt for a step that threw.
  clearEscapeOffers();
  for (const [stepId, tile] of tiles) {
    tile.classList.toggle("current", stepId === id);
  }
  setChip(id, "running");

  const title = tiles.get(id)?.querySelector(".step-title")?.textContent ?? id.toUpperCase();
  const divider = document.createElement("div");
  divider.className = "divider";
  const number = document.createElement("b");
  number.textContent = `${String(index + 1).padStart(2, "0")}/${total}`;
  divider.append(number, document.createTextNode(title));
  appendNarration(divider);

  const intro = STEP_INTROS[id];
  if (intro !== undefined) appendLine("J.A.R.V.I.S.", intro);

  // Spoken moments tied to steps STARTING. The Earlier Install step only
  // exists when the probe found something, so its start IS the honest
  // "found an old install" moment; the engine install is the longest wait.
  if (id === "migrate") playCue("migrate.found");
  if (id === "hermes") playCue("engine.install");
  scrollLog();
}

function finishStep(id: string, outcome: StepOutcome, retained = false): void {
  setChip(id, outcome.status);
  // Spoken reactions at the moments that matter — never for retained repeats.
  if (id === "model" && outcome.status === "done" && !retained) {
    playCue("model.verified");
  }
  // The local voice exists from here on; these baked clips retire after the
  // setup-complete line.
  if (id === "voice" && outcome.status === "done" && !retained) {
    playCue("voice.handoff");
  }
  clearModelIdleNudge();
  const details = outputs.get(id);
  if (details !== undefined) details.open = false;

  // Said only when it actually happened: forced steps re-run even when a
  // previous run finished them, so this line rides the engine's retained flag
  // rather than guessing from previousStatus (Codex review 2026-08-14).
  if (retained) {
    appendLine("J.A.R.V.I.S.", "Already done on an earlier run — left exactly as it was.");
  }
  const verdict = stepVerdict(outcome.status);
  appendLine("J.A.R.V.I.S.", verdict, outcome.status === "failed" ? "error" : undefined);

  // A skipped step used to explain itself only in the (failed-only) summary, so
  // "Nothing to do there" arrived with no reason attached. Failures repeat here
  // too: the finish panel can be several screens away by then.
  if (outcome.status !== "done") {
    for (const note of outcome.notes) {
      appendLine("J.A.R.V.I.S.", note, outcome.status === "failed" ? "error" : "warn");
    }
    for (const action of outcome.nextActions ?? []) {
      appendLine("J.A.R.V.I.S.", action, "warn");
    }
  }
  scrollLog();
}

function setChip(id: string, status: StepStatus | "running" | "pending"): void {
  const chip = chips.get(id);
  if (chip === undefined) return;
  chip.className = `chip ${status}`;
  const label = chip.querySelector("span");
  if (label !== null) label.textContent = STATUS_LABELS[status];
}

function appendLine(speaker: string, text: string, variant?: "warn" | "error"): HTMLElement {
  const line = document.createElement("div");
  line.className = variant === undefined ? "line" : `line ${variant}`;

  const label = document.createElement("div");
  label.className = speaker === "YOU" ? "speaker you" : "speaker";
  label.textContent = speaker;

  const body = document.createElement("p");
  renderText(body, text);

  line.append(label, body);
  appendNarration(line);
  return line;
}

function appendCommand(stepId: string, text: string): void {
  const details = ensureOutput(stepId);
  const command = document.createElement("div");
  command.className = "cmd";
  command.textContent = `$ ${text}`;
  details.append(command);
  scrollLog();
}

function appendCommandOutput(stepId: string, chunk: string): void {
  const details = ensureOutput(stepId);
  let pre = details.querySelector("pre");
  if (pre === null) {
    pre = document.createElement("pre");
    details.append(pre);
  }
  pre.append(document.createTextNode(chunk));
  pre.scrollTop = pre.scrollHeight;
  scrollLog();
}

function ensureOutput(stepId: string): HTMLDetailsElement {
  const existing = outputs.get(stepId);
  if (existing !== undefined) return existing;

  const details = document.createElement("details");
  details.className = "output";
  details.open = true;
  const summary = document.createElement("summary");
  summary.textContent = "COMMAND OUTPUT";
  details.append(summary);
  els.log.append(details);
  outputs.set(stepId, details);
  return details;
}

function appendBlock(title: string, lines: string[]): void {
  const block = document.createElement("div");
  block.className = "block";
  const heading = document.createElement("h2");
  heading.textContent = title.toUpperCase();
  const pre = document.createElement("pre");
  pre.textContent = lines.join("\n");
  block.append(heading, pre);
  appendNarration(block);
}

/**
 * The sign-in card: one sentence, one clickable address, one code big enough to
 * read while typing it into a phone or a second window.
 *
 * One card per ATTEMPT, updated in place. Appending a fresh card per event
 * would leave an expired code sitting on screen looking every bit as actionable
 * as the live one, and would leave "Waiting for you to finish in the browser"
 * under a sign-in that had already succeeded, failed, or been cancelled.
 */
function renderDeviceCode(
  attempt: number,
  status: DeviceCodeStatus,
  message: string,
  url?: string,
  code?: string,
): void {
  // Like prompts, a sign-in card is never deferred behind a clip.
  releaseHeldNarration();
  const existing = signInCards.get(attempt);
  const card = existing ?? document.createElement("div");
  card.className = `signin ${status}`;
  card.replaceChildren();

  const heading = document.createElement("h2");
  heading.textContent = SIGN_IN_CARD.heading;

  const line = document.createElement("p");
  line.className = "signin-message";
  line.textContent = message;
  card.append(heading, line);

  if (status === "waiting" && url !== undefined && code !== undefined) {
    const address = document.createElement("a");
    address.className = "signin-url";
    address.textContent = url;
    address.addEventListener("click", (event) => {
      event.preventDefault();
      void bridge?.openExternal(url);
    });

    const codeRow = document.createElement("p");
    codeRow.className = "signin-code";
    const label = document.createElement("span");
    label.textContent = "CODE";
    const value = document.createElement("b");
    value.textContent = code;
    codeRow.append(label, value);

    const waiting = document.createElement("p");
    waiting.className = "signin-waiting";
    waiting.textContent = `${SIGN_IN_CARD.waiting} ${SIGN_IN_CARD.openManually}`;
    card.append(address, codeRow, waiting);
  }

  if (existing === undefined) {
    signInCards.set(attempt, card);
    els.log.append(card);
  }
  scrollLog();
}

function renderPrompt(prompt: SetupPrompt): void {
  // A question is never deferred behind a clip — flush any held narration so
  // the log reads in order, then let the voice finish over the waiting form
  // (that overlap is the sanctioned one). Cues still WAITING for steps the
  // wizard has already carried the customer past are stale — pruned, keeping
  // only the newest (Peter's queue rules, 2026-08-14).
  releaseHeldNarration();
  wizardAudio.pruneStale();
  // The idle line is ABOUT the key ("that key is the one thing I can't
  // fetch"), so only the Model step's SECRET prompt arms it — saying it over
  // the provider menu or a retry confirmation would be nonsense (final
  // review 2026-08-14, F6).
  if (prompt.kind === "secret") armModelIdleNudge();

  const block = document.createElement("div");
  block.className = "prompt";

  const message = document.createElement("p");
  message.className = "prompt-message";
  renderText(message, prompt.message);
  block.append(message);

  const answer = (value: string | boolean, echo: string): void => {
    clearModelIdleNudge();
    block.classList.add("answered");
    for (const control of block.querySelectorAll("button, input")) {
      (control as HTMLButtonElement | HTMLInputElement).disabled = true;
    }
    bridge?.answerPrompt(prompt.promptId, value);
    appendLine("YOU", echo);
  };

  if (prompt.kind === "confirm") {
    const yesDefault = prompt.defaultValue !== false;
    const row = document.createElement("div");
    row.className = "prompt-row";
    row.append(
      button("YES", yesDefault, () => answer(true, "Yes.")),
      button("NO", !yesDefault, () => answer(false, "No.")),
    );
    block.append(row);
  } else if (prompt.kind === "select") {
    const options = document.createElement("div");
    options.className = "options";
    for (const choice of prompt.choices ?? []) {
      options.append(optionButton(choice, choice.key === prompt.defaultValue, () => {
        answer(choice.key, choice.label);
      }));
    }
    block.append(options);
  } else {
    const secret = prompt.kind === "secret";
    if (secret) block.classList.add("wide");

    const row = document.createElement("div");
    row.className = secret ? "prompt-row stacked" : "prompt-row";

    const field = document.createElement("div");
    field.className = "field";

    const input = document.createElement("input");
    input.type = secret ? "password" : "text";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.setAttribute("aria-label", prompt.message);
    const fallback = typeof prompt.defaultValue === "string" ? prompt.defaultValue : "";
    input.placeholder = secret
      ? "paste or type your key"
      : fallback === ""
        ? "press Enter to continue"
        : fallback;

    // A 108-character API key cannot fit in one line at a readable size, so the
    // hint — not the field width — is what tells the buyer the whole key landed.
    const hint = secret ? document.createElement("p") : null;
    if (hint !== null) {
      hint.className = "field-hint";
      hint.setAttribute("aria-live", "polite");
    }
    const refreshHint = (): void => {
      if (hint !== null) hint.textContent = secretHint(input.value);
    };

    // SUBMIT must never be a live control that does nothing (Peter's smoke
    // feedback, 2026-08-14): with nothing typed and nothing saved it is
    // disabled; with a saved key it wears the plain-language label for what
    // an empty submission actually does ("KEEP SAVED KEY").
    const submitBtn = button("SUBMIT", true, () => submit());
    const emptyLabel = secret ? prompt.emptySubmitLabel : undefined;
    const currentlyEmpty = (): boolean => (secret ? sanitizeSecret(input.value) : input.value.trim()) === "";
    const refreshSubmit = (): void => {
      if (!secret) return;
      const empty = currentlyEmpty();
      submitBtn.disabled = empty && emptyLabel === undefined;
      submitBtn.textContent = empty && emptyLabel !== undefined ? emptyLabel : "SUBMIT";
    };

    const submit = (): void => {
      const typed = secret ? sanitizeSecret(input.value) : input.value.trim();
      // Enter must not bypass a disabled submit.
      if (secret && typed === "" && emptyLabel === undefined) return;
      const value = typed === "" ? fallback : typed;
      if (secret) {
        // Do not leave a revealed key sitting in the answered block.
        input.type = "password";
        input.value = "";
        if (hint !== null) hint.textContent = "";
      }
      answer(
        value,
        secret
          ? typed === "" ? "Keep what's already saved." : "••••••••"
          : value === "" ? "(left blank)" : value,
      );
    };

    input.addEventListener("keydown", (event) => {
      // Typing is activity: the customer is here, not idle (secret/key
      // prompts only — the nudge is about the key).
      if (secret) armModelIdleNudge();
      if (event.key === "Enter") {
        event.preventDefault();
        submit();
      }
    });
    input.addEventListener("input", () => {
      refreshHint();
      refreshSubmit();
    });

    field.append(input);
    if (hint !== null) field.append(hint);
    if (prompt.guide !== undefined) field.append(guideLine(prompt.guide));
    row.append(field);

    if (secret && hint !== null) {
      // One left-click to paste: right-click menus and Ctrl+V chords are hard
      // for buyers driving the app with an eye-tracker or on-screen keyboard.
      const paste = button("PASTE", false, () => {
        void (async () => {
          const text = sanitizeSecret((await bridge?.readClipboardText()) ?? "");
          if (text === "") {
            hint.textContent = "clipboard is empty - copy the key first";
            return;
          }
          input.value = text;
          refreshHint();
          refreshSubmit();
          input.focus();
        })().catch(() => {
          hint.textContent = "could not read the clipboard - try Ctrl+V instead";
        });
      });
      paste.setAttribute("aria-label", "Paste the key from the clipboard");

      const reveal = button("SHOW", false, () => {
        const masked = input.type === "password";
        input.type = masked ? "text" : "password";
        reveal.textContent = masked ? "HIDE" : "SHOW";
        input.focus();
      });
      reveal.setAttribute("aria-label", "Show or hide the key");

      row.append(paste, reveal);
    }

    refreshSubmit();
    row.append(submitBtn);
    block.append(row);
    els.log.append(block);
    scrollLog();
    input.focus();
    return;
  }

  els.log.append(block);
  scrollLog();
}

function button(label: string, primary: boolean, onClick: () => void): HTMLButtonElement {
  const element = document.createElement("button");
  element.type = "button";
  element.className = primary ? "text-btn primary" : "text-btn";
  element.textContent = label;
  element.addEventListener("click", onClick);
  return element;
}

function optionButton(choice: SetupPromptChoice, isDefault: boolean, onClick: () => void): HTMLButtonElement {
  const element = document.createElement("button");
  element.type = "button";
  element.className = "option";

  const hotkey = document.createElement("span");
  hotkey.className = "hotkey";
  hotkey.textContent = choice.hotkey.toUpperCase();

  const label = document.createElement("span");
  label.className = "option-label";
  label.textContent = choice.label;

  const description = document.createElement("span");
  description.className = "option-desc";
  description.textContent = isDefault ? `${choice.description} · default` : choice.description;

  element.append(hotkey, label, description);
  element.addEventListener("click", onClick);
  return element;
}

function renderFinish(summary: StepSummary[], failed: boolean, supportCode?: string): void {
  releaseHeldNarration();
  // The finish is the terminal input-wait: stale step narration must not
  // delay the birth line (setup.complete is undroppable and unaffected).
  if (!failed) wizardAudio.pruneStale();
  // The ad-callback line plays the moment the run succeeds — over the birth
  // on the auto path, over the waiting card otherwise. Never over a dry run,
  // which changed nothing (playCue suppresses those and reports no cue).
  settledCues.delete("setup.complete"); // a fresh clip instance starts un-settled
  const cueLive = failed ? false : playCue("setup.complete");
  els.status.className = failed ? "status failed" : "status done";
  els.status.textContent = failed ? "● ATTENTION" : "● READY";

  // The auto path: a clean, real run tries the birth INSTEAD of the card.
  // Everything else — failures, dry runs, a page missing its overlay, debt —
  // falls through to today's panel, which stays the fallback surface.
  if (!failed && !voiceSuppressed) {
    void startAutoBirth(summary, supportCode, cueLive);
    return;
  }
  appendFinishPanel(summary, failed, supportCode);
}

/**
 * The auto birth (§3): eligibility is pre-checked against lifecycle debt
 * exactly like bootAnywayButton does — unknown or rejected is not permission —
 * so the contract's refusal stays a rare race, never the designed path. Any
 * ineligibility renders exactly today's SETUP COMPLETE card instead.
 */
async function startAutoBirth(summary: StepSummary[], supportCode: string | undefined, cueLive: boolean): Promise<void> {
  let blocked = true;
  try {
    const debt = await bridge?.lifecycleDebt();
    blocked = debt === undefined || debt.blocked;
  } catch {
    blocked = true;
  }
  const restoreCard = (): void => {
    if (els.log.querySelector(".finish") === null) appendFinishPanel(summary, false, supportCode);
  };
  const started = !blocked && beginBirth("auto", {
    caption: WIZARD_AUDIO_CUES["setup.complete"]?.transcript ?? "",
    reducedMotion: prefersReducedMotion(),
    // Liveness is decided NOW, not when the cue started (r2 P2): if the clip
    // ended — or failed on the spot — during the lifecycleDebt await above,
    // the controller must not wait for a settlement that already happened.
    cueLive: cueLive && !settledCues.has("setup.complete"),
  }, restoreCard);
  if (!started) restoreCard();
}

/** Today's finish panel, byte for byte — now the fallback and manual surface. */
function appendFinishPanel(summary: StepSummary[], failed: boolean, supportCode?: string): void {
  const panel = document.createElement("div");
  panel.className = failed ? "finish failed" : "finish";

  const heading = document.createElement("h2");
  heading.textContent = failed ? "SETUP INCOMPLETE" : "SETUP COMPLETE";

  const message = document.createElement("p");
  message.textContent = failed
    ? "A few matters need your attention before the household is fully in order, sir. Setup will resume the next time you open JARVIS, and you can always reopen it from the settings gear."
    : "The household is in order, sir. Your staff is assembled and standing by.";

  panel.append(heading, message);

  if (failed) {
    const list = document.createElement("ul");
    for (const step of summary.filter((entry) => entry.status === "failed")) {
      for (const note of step.notes) {
        const item = document.createElement("li");
        renderText(item, `${step.title}: ${note}`);
        list.append(item);
      }
    }

    const support = document.createElement("p");
    support.className = "support";
    support.append(document.createTextNode("Stuck? Email jarvis.agent.tech@pm.me — a real person reads and answers."));
    if (supportCode !== undefined) {
      const code = document.createElement("b");
      code.textContent = supportCode;
      support.append(
        document.createTextNode(" Mention support code "),
        code,
        document.createTextNode(" so we can see exactly where setup stopped."),
      );
    }

    panel.append(list, support);
  }

  const row = document.createElement("div");
  row.className = "prompt-row";
  if (failed) {
    // One left-click puts a shareable plain-text summary on the clipboard —
    // no reading codes over the phone, same reasoning as the PASTE button.
    const copy = button("COPY DIAGNOSTICS", false, () => {
      void copyDiagnostics(copy);
    });
    copy.setAttribute("aria-live", "polite");
    // A failed MODEL step withholds the boot escape entirely: the Model step
    // is required (Peter's flow ruling, 2026-08-14 — no "continue without a
    // brain" path), and BOOT HUD ANYWAY only ever checked lifecycle debt, so
    // it was the skip that officially did not exist (Codex review 2026-08-14,
    // finding 3). Optional failures (voice on a machine that cannot speak)
    // keep the escape — that is what "anyway" is for.
    const modelFailed = summary.some((entry) => entry.id === "model" && entry.status === "failed");
    if (modelFailed) {
      appendLine("J.A.R.V.I.S.", "There is no office to open yet, sir — I still need my thinking set up. RETRY SETUP will take you back to it.");
    }
    row.append(
      button("RETRY SETUP", true, () => {
        panel.remove();
        appendLine("J.A.R.V.I.S.", "Very good, sir. Running the sequence again.");
        void startSetup();
      }),
      copy,
      ...(modelFailed ? [] : [bootAnywayButton(() => startManualBirth(summary, supportCode))]),
    );
  } else {
    row.append(
      button("BOOT HUD", true, () => {
        startManualBirth(summary, supportCode);
      }),
    );
  }
  panel.append(row);

  els.log.append(panel);
  scrollLog();
}

/**
 * The manual birth (§3, mode matrix): a BOOT HUD / BOOT HUD ANYWAY click gets
 * the full choreography at the fixed 3400 ms pacing, with the manual caption
 * and no clip (the spoken line belongs to the success path). The card stays
 * behind the overlay, so an aborted manual birth needs no re-render — the
 * restore only appends when no card is on screen at all.
 */
function startManualBirth(summary: StepSummary[], supportCode?: string): void {
  const started = beginBirth("manual", {
    caption: MANUAL_BIRTH_CAPTION,
    reducedMotion: prefersReducedMotion(),
    cueLive: false,
  }, () => {
    if (els.log.querySelector(".finish") === null) appendFinishPanel(summary, false, supportCode);
  });
  // A page whose overlay scaffolding is missing must still honor the click:
  // straight through the same door, instant office.
  if (!started) void bridge?.launchHud({ entrance: "fast" });
}

/**
 * "BOOT HUD ANYWAY" — but not past lifecycle debt.
 *
 * This button used to cancel setup and start the engine unconditionally, which
 * walked straight past a provider-safety block or an unfinished 1.3.x
 * migration (integration review r2, finding 1). The supervisor now refuses such
 * a start on its own; this makes the refusal HONEST instead of mysterious, by
 * disabling the button and saying why before it is pressed.
 */
function bootAnywayButton(bootHud: () => void): HTMLButtonElement {
  const boot = button("BOOT HUD ANYWAY", false, bootHud);
  if (bridge?.lifecycleDebt === undefined) return boot;
  // Starts disabled: the answer is one IPC away, and offering it first and
  // withdrawing it later would be worse than a moment of "checking".
  boot.disabled = true;
  boot.textContent = "CHECKING…";
  void bridge.lifecycleDebt().then((debt) => {
    if (!debt.blocked) {
      boot.disabled = false;
      boot.textContent = "BOOT HUD ANYWAY";
      return;
    }
    boot.textContent = "CANNOT BOOT YET";
    boot.title = debt.detail;
    appendLine("J.A.R.V.I.S.", debt.detail);
  }).catch(() => {
    // Unknown is not permission: the supervisor would refuse anyway.
    boot.textContent = "CANNOT BOOT YET";
  });
  return boot;
}

async function copyDiagnostics(trigger: HTMLButtonElement): Promise<void> {
  if (bridge === undefined) return;
  const original = trigger.textContent ?? "COPY DIAGNOSTICS";
  try {
    const report = await bridge.getDiagnostics();
    await bridge.writeClipboardText(report.text);
    trigger.textContent = "COPIED — PASTE INTO YOUR EMAIL";
  } catch {
    trigger.textContent = "COPY FAILED — TRY AGAIN";
  }
  window.setTimeout(() => {
    trigger.textContent = original;
  }, 4000);
}

/**
 * The link is an anchor with no href on purpose: this page is loaded over
 * file://, so a real href would let a click (or a middle-click) navigate the
 * wizard window itself to the site. The main process opens it in the customer's
 * default browser instead, exactly as the log's own links do.
 */
function guideLine(guide: SecretPromptGuide): HTMLParagraphElement {
  const line = document.createElement("p");
  line.className = "field-guide";
  // A guide without a link is just the sentence (the Slack member ID lives
  // inside the Slack app — there is no page to open).
  if (guide.linkText === undefined || guide.url === undefined) {
    line.append(document.createTextNode(guide.text));
    return line;
  }
  const url = guide.url;
  line.append(document.createTextNode(`${guide.text} `));

  const open = (): void => {
    void bridge?.openExternal(url);
  };

  const link = document.createElement("a");
  link.textContent = guide.linkText;
  link.setAttribute("role", "link");
  link.tabIndex = 0;
  link.addEventListener("click", (event) => {
    event.preventDefault();
    open();
  });
  link.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    open();
  });

  line.append(link);
  return line;
}

function renderText(target: HTMLElement, text: string): void {
  let lastIndex = 0;
  for (const match of text.matchAll(URL_PATTERN)) {
    const url = match[0];
    const index = match.index ?? 0;
    if (index > lastIndex) target.append(document.createTextNode(text.slice(lastIndex, index)));
    const link = document.createElement("a");
    link.textContent = url;
    link.addEventListener("click", (event) => {
      event.preventDefault();
      void bridge?.openExternal(url);
    });
    target.append(link);
    lastIndex = index + url.length;
  }
  if (lastIndex < text.length) target.append(document.createTextNode(text.slice(lastIndex)));
}

function scrollLog(): void {
  els.log.scrollTop = els.log.scrollHeight;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function must<T extends HTMLElement>(id: string): T {
  const element = document.getElementById(id);
  if (element === null) throw new Error(`Missing element #${id}`);
  return element as T;
}
