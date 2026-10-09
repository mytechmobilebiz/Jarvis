/**
 * Baked-in JARVIS voice cues for the setup wizard (Peter, 2026-08-14): the ads
 * show JARVIS speaking, so the wizard speaks from the moment it opens — long
 * before the local TTS payload is installed. Clips are pre-generated MP3s
 * (ElevenLabs, the ads' voice) shipped INSIDE the ASAR next to setup.html, so
 * the app's own code signature covers their integrity. They are deliberately
 * NOT part of the voice-payload manifest/anchor — that chain is for the big
 * runtime payloads, and entangling tiny UI assets with it would couple a copy
 * cue to a 270 MB digest tree.
 *
 * RENDERER-REACHABLE MODULE: loaded by setup.html, a `file://` page — no bare
 * import specifiers, ever (the renderer-graph guard enforces it; a single one
 * shipped a blank wizard once).
 *
 * The rules this module owns (team-lead's briefs, 2026-08-14):
 *  - time-of-day greeting from the LOCAL clock, three buckets;
 *  - a re-run of setup gets the short re-greeting, never the full one
 *    (first-ever open vs re-run arrives on the `opening` event);
 *  - playback NEVER blocks or delays a step — WORK never waits, only the
 *    presentation serializes;
 *  - cues QUEUE and play strictly in order, never simultaneously (Peter:
 *    the landing page once fired every interaction voice at once — a mess),
 *    with a staleness valve: one speaking + two waiting is the most a
 *    butler may fall behind; the oldest droppable waiter yields, greetings
 *    and setup.complete never do, and drops are reported for diagnostics;
 *  - no audio device, no file, autoplay refused → SILENT NO-OP;
 *  - every clip mirrors text that appears in the voice log — the transcript
 *    prints at PLAYBACK START (not enqueue), before the sound, so no
 *    information is ever voice-only;
 *  - a persisted mute toggle, DEFAULTING TO SOUND ON (that is the point);
 *    muting stops the active clip mid-word and empties the queue.
 *
 * The assets are Peter's picked ElevenLabs recordings (2026-08-14),
 * loudness-normalized to the ads' mastering. Replacing one is a file drop
 * into src/wizard-audio/ plus the matching transcript edit below — the
 * verify-wizard-audio dist gate refuses placeholder-sized files.
 */

export type GreetingBucket = "morning" | "afternoon" | "evening";

export interface WizardAudioCue {
  /** Filename under src/wizard-audio/, resolved relative to setup.html. */
  file: string;
  /** The exact words spoken — printed to the voice log when the cue plays. */
  transcript: string;
}

/**
 * The cue catalog. Moment ids are the seam the creative side fills in: adding
 * a moment is one entry here plus one `playCue` call site. Transcripts are
 * the VERBATIM spoken words of the shipped recordings (Peter's picks,
 * 2026-08-14) — change a clip and its transcript together, never one alone.
 */
export const WIZARD_AUDIO_CUES: Record<string, WizardAudioCue> = {
  "greeting.morning": {
    file: "greeting-morning.mp3",
    transcript: "Good morning, sir. Do get comfortable — settling in is rather my job.",
  },
  "greeting.afternoon": {
    file: "greeting-afternoon.mp3",
    transcript: "Good afternoon, sir. Make yourself comfortable. The moving in is mine to do.",
  },
  "greeting.evening": {
    file: "greeting-evening.mp3",
    transcript: "Good evening, sir. I'd say make yourself comfortable — but that's rather my job.",
  },
  // Re-runs get a shorter word, not the full ceremony (update installs rerun
  // setup routinely; a butler does not re-introduce himself every morning).
  "greeting.rerun": {
    file: "greeting-rerun.mp3",
    transcript: "Back again, sir. Just seeing to a few improvements — this won't take a moment.",
  },
  // M2: the Earlier Install step only EXISTS when the probe found something,
  // so its step-started moment is the honest trigger.
  "migrate.found": {
    file: "migrate-found.mp3",
    transcript: "Ah. It appears I've lived here before. Allow me to tidy up after myself.",
  },
  // M3: the longest wait in the whole sequence deserves a spoken word.
  "engine.install": {
    file: "engine-install.mp3",
    transcript: "Moving in now. I travel light, sir. Give me a minute to unpack.",
  },
  // M5: sixty seconds of silence at the key prompt, once per run — a nudge,
  // never a nag.
  "model.idle": {
    file: "model-idle.mp3",
    transcript: "No rush, sir. That key is the one thing I can't fetch for myself.",
  },
  "model.verified": {
    file: "model-verified.mp3",
    transcript: "Ah. That would be my mind arriving, sir. Thank you for that.",
  },
  // M7: the moment the local voice takes over from these baked clips.
  "voice.handoff": {
    file: "voice-handoff.mp3",
    transcript: "From here on, sir, the voice you hear will be my own. I was rather fond of this one, though.",
  },
  "setup.complete": {
    file: "setup-complete.mp3",
    transcript: "That's everything, sir. Now — what would you like me to handle first?",
  },
};

/** Local-clock buckets: 05–11 morning, 12–17 afternoon, everything else evening. */
export function bucketForHour(hour: number): GreetingBucket {
  if (hour >= 5 && hour < 12) return "morning";
  if (hour >= 12 && hour < 18) return "afternoon";
  return "evening";
}

/** Which greeting this open gets: the time-of-day one first-ever, the short one after. */
export function greetingMomentFor(hour: number, firstRun: boolean): string {
  return firstRun ? `greeting.${bucketForHour(hour)}` : "greeting.rerun";
}

const MUTE_STORAGE_KEY = "jarvis-wizard-audio-muted";

/**
 * How many cues may WAIT behind the one speaking (Peter's queue rule,
 * 2026-08-14): cues queue and play strictly in order — never simultaneously
 * (the landing page once fired every interaction voice at once; "a mess"),
 * never silently swallowed — but a butler three sentences behind reality
 * reads as broken, so past one speaking + two waiting the OLDEST waiting
 * reaction is dropped (never a greeting, never setup.complete).
 */
const MAX_WAITING_CUES = 2;

/**
 * If neither 'ended' nor an error settles a real clip (media error after
 * play() resolved, device loss, an indefinite stall), this watchdog does —
 * otherwise the queue starves forever behind a zombie playback (final review
 * 2026-08-14, F3). Longest legal clip is ~8s; the margin is generous.
 */
const PLAYBACK_WATCHDOG_MS = 15_000;

/** Cues that may never be sacrificed to the staleness valve. */
function undroppable(momentId: string): boolean {
  return momentId.startsWith("greeting.") || momentId === "setup.complete";
}

export interface WizardAudioPlayer {
  /** Fire-and-forget: starts the cue, or queues it behind the one speaking. */
  play(momentId: string): boolean;
  muted(): boolean;
  setMuted(muted: boolean): void;
  /** "played" | "muted" | "silent" | "idle" — read by the packaged wizard smoke. */
  status(): string;
  /**
   * Why the last attempt went silent (the rejection's error name, e.g.
   * "NotAllowedError"), or undefined. The packaged smoke uses this to tell an
   * autoplay REGRESSION from a machine that simply has no audio device
   * (Codex mechanism review 2026-08-14, finding 5).
   */
  silentReason(): string | undefined;
  /** True while a clip is speaking or any cue is waiting its turn. */
  busy(): boolean;
  /** Epoch ms of the last playback start/end — the idle nudge's clock. */
  lastActivityAt(): number;
  /**
   * The wizard reached an input-wait: everything older than the NEWEST
   * waiting cue is stale narration for steps the customer has already been
   * carried past — drop it (droppables only, logged via onCueDropped).
   */
  pruneStale(): void;
  /** Moment ids in the order their clips actually STARTED (smoke assertion). */
  startedOrder(): readonly string[];
  /**
   * A NEW RUN starts (RETRY SETUP): stop the active clip and drain the queue
   * — a butler must not narrate the previous run over this one (final review
   * 2026-08-14, F5).
   */
  reset(): void;
}

/** The seam mirrors the parts of HTMLAudioElement the player touches. */
export interface WizardAudioElement {
  play(): Promise<void>;
  volume: number;
  pause?(): void;
  addEventListener?(type: "ended" | "error", listener: () => void): void;
}

export interface WizardAudioOptions {
  /**
   * Receives the transcript when a cue actually STARTS playing (mirror rule).
   * With queueing this is the moment the words are audible — not enqueue time.
   */
  onTranscript?: (text: string) => void;
  /** Fired when a clip finishes (or fails) — releases any deferred narration. */
  onCueEnded?: (momentId: string) => void;
  /** A queued cue sacrificed to the staleness valve — for diagnostics only. */
  onCueDropped?: (momentId: string, why: string) => void;
  /** Test seams; production uses the real Audio, localStorage and clock. */
  createAudio?: (url: string) => WizardAudioElement;
  storage?: { getItem(key: string): string | null; setItem(key: string, value: string): void };
  now?: () => number;
}

export function createWizardAudioPlayer(options: WizardAudioOptions = {}): WizardAudioPlayer {
  const storage = options.storage ?? safeLocalStorage();
  const createAudio = options.createAudio ?? ((url: string) => new Audio(url));
  // Default is SOUND ON: only an explicit "1" mutes. The read itself is
  // guarded — a denied localStorage must not take the renderer down with it
  // (Codex mechanism review 2026-08-14, finding 4).
  let muted = false;
  try {
    muted = storage?.getItem(MUTE_STORAGE_KEY) === "1";
  } catch {
    // Unknown preference = the default: sound on.
  }
  const now = options.now ?? (() => Date.now());
  let status = "idle";
  let reason: string | undefined;
  // Cues QUEUE and play strictly in order (Peter's rule, 2026-08-14): the
  // semi-automatic wizard flies through steps, and firing every moment's
  // voice at once was the landing page's proven mess. WORK never waits —
  // only the presentation serializes.
  //
  // Settlement is PER PLAYBACK and idempotent: a muted clip's still-pending
  // play() promise settles later, and without the token that late settlement
  // fired onCueEnded a second time — releasing the NEXT clip's deferred
  // narration early (Codex re-review 2026-08-14, F3). Settling the active
  // playback is also what starts the next queued cue.
  interface Playback {
    audio: WizardAudioElement;
    momentId: string;
    settled: boolean;
    guard?: ReturnType<typeof setTimeout>;
  }
  let active: Playback | null = null;
  const waiting: string[] = [];
  const started: string[] = [];
  let lastActivity = 0;

  const settle = (playback: Playback): void => {
    if (playback.settled) return;
    playback.settled = true;
    if (playback.guard !== undefined) clearTimeout(playback.guard);
    lastActivity = now();
    if (active === playback) active = null;
    options.onCueEnded?.(playback.momentId);
    startNext();
  };

  const drop = (momentId: string, why: string): void => {
    options.onCueDropped?.(momentId, why);
  };

  function startNext(): void {
    while (active === null && waiting.length > 0) {
      const nextId = waiting.shift();
      if (nextId === undefined) return;
      if (muted) {
        // Mute arriving while cues waited: they are silenced, not saved up.
        drop(nextId, "muted");
        continue;
      }
      start(nextId);
    }
  }

  /** Begin actual playback. Failures settle (advancing the queue) silently. */
  function start(momentId: string): void {
    const cue = WIZARD_AUDIO_CUES[momentId];
    if (cue === undefined) return;
    let playback: Playback | null = null;
    try {
      // Transcript at PLAYBACK START, not enqueue: the mirror rule is that
      // the printed words accompany the audible ones — and it must print
      // FIRST: if the log cannot take the words, the voice does not get to
      // say them (finding 7).
      options.onTranscript?.(cue.transcript);
      const audio = createAudio(`wizard-audio/${cue.file}`);
      audio.volume = 1;
      playback = { audio, momentId, settled: false };
      active = playback;
      started.push(momentId);
      lastActivity = now();
      const token = playback;
      const hasEnded = typeof audio.addEventListener === "function";
      if (hasEnded) {
        audio.addEventListener?.("ended", () => settle(token));
        // A media error AFTER play() resolved never fires 'ended' — without
        // this the queue starved behind the dead clip (final review, F3).
        audio.addEventListener?.("error", () => settle(token));
      }
      // Belt and braces for the stalls no event covers (device loss, a
      // wedged pipeline): the longest legal clip plus margin, then move on.
      token.guard = setTimeout(() => settle(token), PLAYBACK_WATCHDOG_MS);
      // NEVER block a step on sound: the promise is observed only to record
      // the outcome. Real elements settle on 'ended'; seams without events
      // settle when play() resolves, so tests cannot wedge the player. Both
      // settle THIS playback's token — a late resolution after a mute (or
      // after the next clip started) is a no-op, never a second callback.
      void audio.play().then(
        () => {
          if (!token.settled) {
            status = "played";
            reason = undefined;
          }
          if (!hasEnded) settle(token);
        },
        (error: unknown) => {
          if (!token.settled) {
            status = "silent";
            reason = error instanceof Error && error.name !== "" ? error.name : "unknown";
          }
          settle(token);
        },
      );
    } catch {
      status = "silent";
      reason = "unknown";
      if (playback !== null) {
        settle(playback);
      } else {
        // The transcript may already have printed (and engaged the caller's
        // narration hold) before createAudio threw — the hold pairs with the
        // transcript, so a failed start must still announce the cue's end
        // (final review 2026-08-14, F4).
        options.onCueEnded?.(momentId);
        startNext();
      }
    }
  }

  return {
    play(momentId: string): boolean {
      const cue = WIZARD_AUDIO_CUES[momentId];
      if (cue === undefined) return false;
      if (muted) {
        status = "muted";
        return false;
      }
      if (active === null && waiting.length === 0) {
        start(momentId);
        return true;
      }
      // The staleness valve: one speaking + MAX_WAITING_CUES waiting is the
      // most a butler may fall behind. The OLDEST droppable waiter goes;
      // greetings and setup.complete are never sacrificed — if nothing can
      // give way, the incoming reaction is the one that yields.
      if (waiting.length >= MAX_WAITING_CUES) {
        const oldest = waiting.findIndex((id) => !undroppable(id));
        if (oldest === -1) {
          if (!undroppable(momentId)) {
            drop(momentId, "queue-full");
            return false;
          }
        } else {
          const [sacrificed] = waiting.splice(oldest, 1);
          if (sacrificed !== undefined) drop(sacrificed, "queue-full");
        }
      }
      waiting.push(momentId);
      return true;
    },
    muted(): boolean {
      return muted;
    },
    setMuted(next: boolean): void {
      muted = next;
      if (next) {
        // Stop the voice mid-word AND empty the queue: SOUND OFF means quiet
        // now, not a backlog of speeches for later. 'ended' does not fire on
        // pause, so the active clip settles explicitly.
        for (const queued of waiting.splice(0)) drop(queued, "muted");
        if (active !== null) {
          try {
            active.audio.pause?.();
          } catch {
            // A dying device is still a successful mute.
          }
          settle(active);
        }
      }
      try {
        storage?.setItem(MUTE_STORAGE_KEY, next ? "1" : "0");
      } catch {
        // A full or blocked storage must not take the toggle down with it.
      }
    },
    status(): string {
      return status;
    },
    silentReason(): string | undefined {
      return reason;
    },
    busy(): boolean {
      return active !== null || waiting.length > 0;
    },
    lastActivityAt(): number {
      return lastActivity;
    },
    pruneStale(): void {
      // Keep only the NEWEST waiting cue: at an input-wait the older waiters
      // narrate steps the customer has already been carried past.
      while (waiting.length > 1) {
        const oldest = waiting.findIndex((id) => !undroppable(id));
        if (oldest === -1 || oldest === waiting.length - 1) return;
        const [sacrificed] = waiting.splice(oldest, 1);
        if (sacrificed !== undefined) drop(sacrificed, "input-wait");
      }
    },
    startedOrder(): readonly string[] {
      return started;
    },
    reset(): void {
      for (const queued of waiting.splice(0)) drop(queued, "reset");
      if (active !== null) {
        try {
          active.audio.pause?.();
        } catch {
          // Stopping a dying clip is still a successful reset.
        }
        settle(active);
      }
    },
  };
}

function safeLocalStorage(): WizardAudioOptions["storage"] | undefined {
  try {
    return typeof localStorage === "undefined" ? undefined : localStorage;
  } catch {
    return undefined;
  }
}
