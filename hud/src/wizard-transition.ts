/**
 * The Birth — the setup → HUD transition timeline (spec/v150/TRANSITION-DESIGN.md).
 *
 * The customer who just paid, pasted a key, and heard JARVIS narrate his own
 * installation must not be asked to click a button. When setup finishes clean,
 * the wizard recedes, the plasma orb births itself out of nothing while the
 * `setup.complete` line plays, the bezel spins up around the core, and the
 * whole thing detonates into the HUD — no click. This module owns that
 * choreography: the beat timers, the overlay classes, the video and SFX
 * lifecycle, the launchHud call and the transition telemetry.
 *
 * What it deliberately does NOT own:
 *  - the contract door — every exit still goes through the `setup:launch-hud`
 *    IPC and `mayLeaveSetup()` in the main process; a refusal arrives back as
 *    an error broadcast and aborts the overlay (notifyErrorEvent);
 *  - the voice — `setup.complete` keeps its exact wizard-audio path (queued,
 *    never-drop, per-playback settle). The one coupling is Td: the controller
 *    learns of settlement through notifyCueSettled and clamps it to
 *    [3400 ms, 15000 ms]. Muted/silent/no-cue runs get the full visual at
 *    3400 ms — zero dead air.
 *
 * RENDERER-REACHABLE MODULE: loaded by setup.html, a `file://` page — no bare
 * import specifiers, ever (the renderer-graph guard enforces it).
 *
 * Every timer runs through injectable now/schedule/cancel seams (the same seam
 * style as wizard-audio.ts) so the whole timeline is unit-testable with a fake
 * clock, and `timeScale` lets the packaged smoke run the full choreography in
 * under a second.
 */

/** Beat offsets in ms from T0 = overlay mount (the canonical spec, §4). */
export const TRANSITION_BEATS = {
  /** A point of light ignites at center. */
  SPARK: 250,
  /** orb.mp4 births in: fade + scale .5→1. */
  ORB: 600,
  /** The six-ring bezel whips in from -560° and decelerates into cruise. */
  BEZEL: 900,
  /** Brand fades in; the spoken line types itself below the orb. */
  CAPTION: 1400,
  /** Brackets, rules and ticks assemble the frame around the core. */
  ASSEMBLY: 2600,
  /** The composition breathes until the spoken line ends. */
  HOLD: 3600,
  /** Td floor: muted/silent runs detonate here — no dead air. */
  TD_MIN: 3400,
  /** Td ceiling: matches the audio player's own playback watchdog. */
  TD_MAX: 15_000,
  /** Orb surge + bezel whip + flash, then the frame is pure void. */
  DETONATE: 950,
  /** Reduced motion swaps the detonation for a plain fade to void. */
  REDUCED_FADE: 400,
  /** The skip escape: fast fade while the fast launch is already in flight. */
  SKIP_FADE: 300,
  /** Dark-frame patience before the honest wait line appears. */
  WAIT_LINE: 2500,
  /** Caption typing pace — the landing's welcome pace. */
  TYPE_MS_PER_CHAR: 28,
  /** No `playing` event by here → the CSS ring fallback orb takes over. */
  VIDEO_FALLBACK: 900,
} as const;

export type TransitionMode = "auto" | "manual";
export type TransitionState = "idle" | "running" | "dark" | "done" | "aborted" | "skipped";
export type TransitionSound = "played" | "muted" | "silent";
export type TransitionTelemetryName = "hud_transition_started" | "hud_transition_finished";

/** The slice of classList the controller touches (real elements satisfy it). */
export interface TransitionClassList {
  add(...names: string[]): void;
  remove(...names: string[]): void;
}

export interface TransitionSurface {
  classList: TransitionClassList;
  hidden: boolean;
}

/** The slice of HTMLVideoElement the orb lifecycle touches. */
export interface TransitionVideo {
  src: string;
  preload: string;
  play(): Promise<void>;
  addEventListener?(type: "playing", listener: () => void, options?: { once?: boolean }): void;
}

export interface TransitionElements {
  /** #transition — beat classes land here; `hidden` mounts/unmounts it. */
  overlay: TransitionSurface;
  /** .tr-orb — src assigned only when the overlay actually runs (§5). */
  video: TransitionVideo;
  /** .tr-ring — the CSS fallback orb, un-hidden when the video cannot play. */
  ring: { hidden: boolean };
  /** .tr-bezel — receives the ported hudMarkup() SVG stack. */
  bezel: { innerHTML: string };
  /** .tr-caption — the spoken line types itself here. */
  caption: { textContent: string | null };
  /** .tr-wait — the honest slow-engine line on the dark frame. */
  wait: TransitionSurface;
  /** The wizard chrome (.topbar, .setup-grid, .corner) that recedes at T0. */
  receded: Array<{ classList: TransitionClassList }>;
}

/** The slice of Audio the wake SFX touches. */
export interface TransitionSfx {
  volume: number;
  play(): Promise<void>;
}

/** The finished-event payload main records when the handoff SUCCEEDS. */
export interface TransitionFinishedReport {
  name: "hud_transition_finished";
  props: Record<string, unknown>;
}

export interface TransitionOptions {
  elements: TransitionElements;
  /** The one door out: the same setup:launch-hud IPC every exit uses.
   * `finished` (r3 P2) asks MAIN to record the completed outcome only after
   * the mayLeaveSetup/gateway checks pass — a refusal must never read as a
   * completed handoff, and the renderer may not survive a successful one. */
  launchHud(opts: { entrance: "full" | "fast"; finished?: TransitionFinishedReport }): Promise<void>;
  /** Fire-and-forget transition telemetry (setup:transition-event). */
  sendTelemetry?(name: TransitionTelemetryName, props: Record<string, unknown>): void;
  /** Live audio verdict for the finished event and the SFX gate. */
  sound?(): TransitionSound;
  /** Called after an abort finishes unwinding, to restore the classic card. */
  onAborted?(): void;
  /** Test seams; production uses Audio, Date.now and window timers. */
  createSfx?(url: string): TransitionSfx;
  now?(): number;
  schedule?(fn: () => void, ms: number): unknown;
  cancel?(handle: unknown): void;
  /** Compresses every delay (packaged smoke); 1 = real time. */
  timeScale?: number;
}

export interface TransitionStartOptions {
  /** The exact words below the orb — the transcript, never a paraphrase. */
  caption: string;
  /** OS reduced-motion at T0 → the dignified static variant (§10). */
  reducedMotion?: boolean;
  /**
   * True only when a real `setup.complete` playback is in flight (the play()
   * call was accepted). False — muted, silent, manual mode — pins Td to the
   * 3400 ms floor immediately: same visuals, tighter pace, no dead air.
   */
  cueLive?: boolean;
}

export interface TransitionController {
  start(mode: TransitionMode, opts: TransitionStartOptions): void;
  /** Wired to the audio player's onCueEnded; only setup.complete matters. */
  notifyCueSettled(momentId: string): void;
  /** Any setup:event error while the overlay is active → abort (§10). */
  notifyErrorEvent(): void;
  /** The sacred escape: any pointer/key at any beat → fast fade → fast HUD. */
  skip(): void;
  state(): TransitionState;
}

/**
 * A verbatim TypeScript port of the landing page's `hudMarkup()` — the
 * six-layer spinning instrument bezel (rings, tick dashes, targeting arcs,
 * pointer brackets, cardinal ticks, circular instrument text). Decorative
 * instrument copy, same organism as the site; not narration, so the
 * plain-language rule does not apply to it.
 */
export function buildBezelMarkup(uid: string): string {
  const C = "#35c6ff";
  const ICE = "#bfe9ff";
  const TAU = Math.PI * 2;
  const ring = (r: number, w: number, op: number, col: string, dash?: string, cap?: string): string =>
    `<circle cx="100" cy="100" r="${r}" fill="none" stroke="${col}" stroke-width="${w}"`
    + (dash !== undefined ? ` stroke-dasharray="${dash}"` : "")
    + (cap !== undefined ? ` stroke-linecap="${cap}"` : "")
    + ` opacity="${op}"/>`;
  // Bezel ticks: thick stroke + short dashes = radial tick marks.
  const ticks = (r: number, n: number, tick: number, len: number, op: number, col: string): string =>
    ring(r, len, op, col, `${tick} ${(TAU * r / n - tick).toFixed(3)}`);
  // Targeting arcs: dash segments as fractions of the circumference.
  const arcs = (r: number, w: number, op: number, col: string, fr: number[]): string =>
    ring(r, w, op, col, fr.map((f) => (f * TAU * r).toFixed(2)).join(" "), "round");
  const svg = (cls: string, inner: string): string =>
    `<svg class="hud-l ${cls}" viewBox="0 0 200 200" aria-hidden="true">${inner}</svg>`;
  // Pointer brackets around the rim — alternating major (T-bracket + block) / minor (pip).
  let pointers = "";
  for (let i = 0; i < 8; i += 1) {
    pointers += `<g transform="rotate(${i * 45} 100 100)">` + (i % 2 === 0
      ? `<path d="M95.8 6.4h8.4M100 6.4v4.8" fill="none" stroke="${ICE}" stroke-width="1.1" opacity=".9"/>`
        + `<rect x="98.4" y="2.4" width="3.2" height="2.6" fill="${C}" opacity=".85"/>`
      : `<circle cx="100" cy="5.4" r="1.15" fill="${C}" opacity=".8"/>`
        + `<path d="M100 7.2v3.4" stroke="${C}" stroke-width=".9" opacity=".6"/>`) + "</g>";
  }
  let cardinals = "";
  for (let i = 0; i < 4; i += 1) {
    cardinals += `<g transform="rotate(${i * 90} 100 100)"><path d="M100 11.2v5.2" stroke="${ICE}" stroke-width="1.2" opacity=".75"/></g>`;
  }
  const TXT = "J.A.R.V.I.S — ARC CORE MK·IV — PWR 100% — ALL SYSTEMS NOMINAL — ";
  return (
    svg("hud-static", ring(88, 0.7, 0.45, C) + ring(56.5, 0.5, 0.3, C) + cardinals)
    + svg("hud-r1", ticks(90.5, 76, 1.1, 4.4, 0.8, ICE))
    + svg("hud-r2", pointers)
    + svg("hud-r3",
      ring(75.5, 1.7, 0.95, C, `0.1 ${(TAU * 75.5 / 56 - 0.1).toFixed(3)}`, "round")
      + ticks(85.5, 120, 0.5, 2, 0.5, C))
    + svg("hud-r4", arcs(70, 1.4, 0.75, C, [0.24, 0.06, 0.1, 0.05, 0.31, 0.24]))
    + svg("hud-r5", arcs(63, 3, 0.9, ICE, [0.07, 0.22, 0.02, 0.16, 0.11, 0.42]))
    + svg("hud-r6",
      `<defs><path id="hudtp-${uid}" d="M100 20a80 80 0 1 1 -.01 0" fill="none"/></defs>`
      + `<text font-size="4.1" letter-spacing="1.9" fill="${C}" opacity=".65"><textPath href="#hudtp-${uid}">${TXT}${TXT}</textPath></text>`)
  );
}

/** Every class the controller may have left on the overlay, for teardown. */
const OVERLAY_CLASSES = [
  "on", "tr-reduced", "t-spark", "t-orb", "orb-live", "orb-fallback", "t-bezel",
  "t-caption", "t-assembly", "t-hold", "t-detonate", "t-dark", "tr-fade",
  "tr-skip", "tr-abort",
];

export function createTransition(options: TransitionOptions): TransitionController {
  const els = options.elements;
  const now = options.now ?? (() => Date.now());
  const scheduleFn = options.schedule
    ?? ((fn: () => void, ms: number) => setTimeout(fn, ms) as unknown);
  const cancelFn = options.cancel
    ?? ((handle: unknown) => clearTimeout(handle as ReturnType<typeof setTimeout>));
  const createSfx = options.createSfx ?? ((url: string) => new Audio(url));
  const scale = options.timeScale ?? 1;

  let state: TransitionState = "idle";
  let mode: TransitionMode = "auto";
  let reduced = false;
  let startedAt = 0;
  // Td gate: the floor timer AND the cue settling must both have happened.
  let floorReached = false;
  let cueSettled = false;
  let videoLive = false;
  // The launchHud call is made exactly once, whichever path gets there first.
  let launched = false;
  // One finished event per run, first outcome wins (a skip that then hits a
  // refusing contract restores the card but does not double-report).
  let finishedSent = false;
  const timers = new Set<unknown>();

  const at = (ms: number, fn: () => void): unknown => {
    const handle = scheduleFn(() => {
      timers.delete(handle);
      fn();
    }, ms * scale);
    timers.add(handle);
    return handle;
  };

  const cancelAll = (): void => {
    for (const handle of timers) cancelFn(handle);
    timers.clear();
  };

  const telemetry = (name: TransitionTelemetryName, props: Record<string, unknown>): void => {
    try {
      options.sendTelemetry?.(name, props);
    } catch {
      // Telemetry must never affect the show.
    }
  };

  const sendFinished = (outcome: "completed" | "skipped" | "aborted"): void => {
    if (finishedSent) return;
    finishedSent = true;
    telemetry("hud_transition_finished", {
      mode,
      outcome,
      duration_ms: Math.max(0, Math.round(now() - startedAt)),
      sound: options.sound?.() ?? "silent",
      reduced_motion: reduced,
    });
  };

  const launch = (entrance: "full" | "fast"): void => {
    if (launched) return;
    launched = true;
    // Only an un-reported run hands main a completed event to record; a skip
    // already reported "skipped" from the renderer (which stays alive there).
    const finished: TransitionFinishedReport | undefined = finishedSent ? undefined : {
      name: "hud_transition_finished",
      props: {
        mode,
        outcome: "completed",
        duration_ms: Math.max(0, Math.round(now() - startedAt)),
        sound: options.sound?.() ?? "silent",
        reduced_motion: reduced,
      },
    };
    options.launchHud({ entrance, ...(finished === undefined ? {} : { finished }) }).then(
      () => {
        // With a stubbed door (tests, the packaged smoke) resolution is the
        // whole story; in the real app navigation tears this renderer down
        // before anyone reads the state again. A skip keeps reading "skipped"
        // — that is its outcome, not a second completion.
        if (state === "dark") state = "done";
      },
      () => {
        // A rejected IPC is a refusal with no navigation coming: unwind to the
        // classic card rather than stranding the customer on a black frame.
        abort();
      },
    );
  };

  /** The caption types on the clock, never on audio events (mirror rule). */
  const typeCaption = (text: string): void => {
    els.caption.textContent = "";
    let index = 0;
    const step = (): void => {
      index += 1;
      els.caption.textContent = text.slice(0, index);
      if (index < text.length) at(TRANSITION_BEATS.TYPE_MS_PER_CHAR, step);
    };
    if (text.length > 0) at(TRANSITION_BEATS.TYPE_MS_PER_CHAR, step);
  };

  const startVideo = (): void => {
    els.overlay.classList.add("t-orb");
    els.video.preload = "auto";
    els.video.src = "transition/orb.mp4";
    const fallback = (): void => {
      if (videoLive || state !== "running") return;
      // The spark stays and the landing's CSS ring orb takes over; the poster
      // remains behind everything as ground truth. Timeline unchanged.
      els.ring.hidden = false;
      els.overlay.classList.add("orb-fallback");
    };
    const fallbackTimer = at(TRANSITION_BEATS.VIDEO_FALLBACK, fallback);
    els.video.addEventListener?.("playing", () => {
      if (state !== "running" && state !== "dark") return;
      videoLive = true;
      timers.delete(fallbackTimer);
      cancelFn(fallbackTimer);
      // The spark dissolves into the living core.
      els.overlay.classList.add("orb-live");
    }, { once: true });
    els.video.play().then(
      () => {
        // `playing` (or the fallback timer) decides; nothing to do here.
      },
      () => {
        fallback();
      },
    );
  };

  const maybeDetonate = (): void => {
    if (state !== "running" || !floorReached || !cueSettled) return;
    detonate();
  };

  const detonate = (): void => {
    if (state !== "running") return;
    // Kill every pending beat: with Td at the 3400 floor the HOLD timer has
    // not fired yet, and breathing must not start over the detonation.
    cancelAll();
    if (reduced) {
      // No surge, no flash — a plain 400 ms fade to void (§10).
      els.overlay.classList.add("tr-fade");
      at(TRANSITION_BEATS.REDUCED_FADE, goDark);
      return;
    }
    // The wake SFX is not a voice cue: no words, so it never enters the
    // wizard-audio queue — one fire-and-forget play, gated so it neither
    // plays for muted customers nor pretends a silent device can hear it.
    // It starts only after the spoken line settled (Td), honoring the
    // never-simultaneous rule in spirit for the one non-verbal sound.
    if ((options.sound?.() ?? "silent") === "played") {
      try {
        const sfx = createSfx("transition/wake.mp3");
        sfx.volume = 0.9;
        void sfx.play().catch(() => {});
      } catch {
        // The surge is garnish; it must never affect the timeline.
      }
    }
    els.overlay.classList.remove("t-hold");
    els.overlay.classList.add("t-detonate");
    at(TRANSITION_BEATS.DETONATE, goDark);
  };

  const goDark = (): void => {
    if (state !== "running") return;
    state = "dark";
    els.overlay.classList.add("t-dark");
    // NOT sendFinished("completed") here (review r3 P2): the handoff can
    // still be REFUSED (mayLeaveSetup, gateway) — reporting completed before
    // launchHud answers recorded a completed handoff for a customer who was
    // bounced back to the card. The completed outcome now rides the
    // launch-hud call itself and is recorded by MAIN only after the checks
    // pass — the renderer cannot be the reporter on the success path, since
    // navigation may tear it down before the IPC resolution arrives.
    launch("full");
    // Honest, plain, rare: only a genuinely slow engine start earns the line.
    at(TRANSITION_BEATS.WAIT_LINE, () => {
      if (state !== "dark") return;
      els.wait.hidden = false;
      els.wait.classList.add("on");
    });
  };

  const abort = (): void => {
    if (state !== "running" && state !== "dark" && state !== "skipped") return;
    cancelAll();
    state = "aborted";
    sendFinished("aborted");
    // The wizard surface restores over its own transition while the overlay
    // fades out; the classic card owns the next attempt — no retry loop.
    for (const surface of els.receded) surface.classList.remove("receded", "receded-quiet");
    els.overlay.classList.add("tr-abort");
    at(TRANSITION_BEATS.SKIP_FADE, () => {
      els.overlay.hidden = true;
      els.overlay.classList.remove(...OVERLAY_CLASSES);
      els.wait.hidden = true;
      els.wait.classList.remove("on");
      try {
        options.onAborted?.();
      } catch {
        // The card renderer failing must not take the log down with it.
      }
    });
  };

  return {
    start(startMode: TransitionMode, opts: TransitionStartOptions): void {
      if (state !== "idle") return;
      state = "running";
      mode = startMode;
      reduced = opts.reducedMotion === true;
      startedAt = now();
      telemetry("hud_transition_started", { mode });
      els.bezel.innerHTML = buildBezelMarkup("t");
      els.overlay.hidden = false;
      els.overlay.classList.add("on");
      // Td = clamp(settle(setup.complete), 3400, 15000). With no live cue —
      // muted, silent device, manual mode — the settle half is already true.
      cueSettled = opts.cueLive !== true;
      at(TRANSITION_BEATS.TD_MIN, () => {
        floorReached = true;
        maybeDetonate();
      });
      if (!cueSettled) {
        // The ceiling matches the audio player's own 15 s watchdog, so a
        // pathological stall can never trap the customer in the HOLD beat.
        at(TRANSITION_BEATS.TD_MAX, () => {
          cueSettled = true;
          maybeDetonate();
        });
      }
      if (reduced) {
        // The dignified static build: opacity-only recede, poster orb inside
        // the same mask, static bezel, the full caption at once — no typing,
        // no rotation, no breathing, no detonation, no flash.
        els.overlay.classList.add("tr-reduced", "t-caption");
        for (const surface of els.receded) surface.classList.add("receded-quiet");
        els.caption.textContent = opts.caption;
        return;
      }
      for (const surface of els.receded) surface.classList.add("receded");
      at(TRANSITION_BEATS.SPARK, () => els.overlay.classList.add("t-spark"));
      at(TRANSITION_BEATS.ORB, startVideo);
      at(TRANSITION_BEATS.BEZEL, () => els.overlay.classList.add("t-bezel"));
      at(TRANSITION_BEATS.CAPTION, () => {
        els.overlay.classList.add("t-caption");
        typeCaption(opts.caption);
      });
      at(TRANSITION_BEATS.ASSEMBLY, () => els.overlay.classList.add("t-assembly"));
      at(TRANSITION_BEATS.HOLD, () => els.overlay.classList.add("t-hold"));
    },

    notifyCueSettled(momentId: string): void {
      if (momentId !== "setup.complete" || state !== "running") return;
      cueSettled = true;
      maybeDetonate();
    },

    notifyErrorEvent(): void {
      abort();
    },

    skip(): void {
      // Nothing to escape before the show, and nothing left to escape after
      // it — including the dark frame, where the call is already in flight
      // and skipping is a no-op beyond reassurance.
      if (state !== "running") return;
      cancelAll();
      state = "skipped";
      sendFinished("skipped");
      // One class fades everything to void while the fast launch is already
      // on its way: the user's hand always outranks the show. The spoken clip
      // is NOT stopped — navigation tears the renderer (and the audio) down.
      els.overlay.classList.add("tr-skip");
      launch("fast");
    },

    state(): TransitionState {
      return state;
    },
  };
}
