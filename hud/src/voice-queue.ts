/** Synthesis runs ahead of serial playback; generations discard cancelled work. */
export interface SpeechQueueOptions<Audio = string> {
  /** Resolves to something the player understands (a file path or URL). */
  synthesize: (text: string) => Promise<Audio>;
  /** Must resolve when playback of that clip finishes or is stopped. */
  play: (audio: Audio) => Promise<void>;
  /** Called to interrupt whatever `play` is currently doing. */
  stopPlayback: () => void;
  /** Fired once a clip has finished playing, so it can be deleted. */
  onPlayed?: (audio: Audio) => void;
  onStateChange?: (speaking: boolean) => void;
  onError?: (error: unknown) => void;
}

export interface SpeechQueue {
  /** Queue a speakable chunk. Safe to call while speaking. */
  enqueue(text: string): void;
  /** Stop everything and discard pending work (push-to-talk pressed). */
  cancel(): void;
  /** Resolves once everything queued so far has finished playing. */
  drained(): Promise<void>;
  readonly speaking: boolean;
}

export function createSpeechQueue<Audio = string>(options: SpeechQueueOptions<Audio>): SpeechQueue {
  const pending: string[] = [];
  const ready: Audio[] = [];

  let generation = 0;
  let synthesizing = false;
  let playing = false;
  let speaking = false;
  let waiters: Array<() => void> = [];

  function setSpeaking(next: boolean): void {
    if (speaking === next) return;
    speaking = next;
    options.onStateChange?.(next);
  }

  function settleIfIdle(): void {
    if (synthesizing || playing || pending.length > 0 || ready.length > 0) return;
    setSpeaking(false);
    const settle = waiters;
    waiters = [];
    for (const resolve of settle) resolve();
  }

  function pumpSynthesis(): void {
    if (synthesizing || pending.length === 0) return;
    const mine = generation;
    const batch = pending.shift() ?? "";
    if (batch === "") {
      settleIfIdle();
      return;
    }
    synthesizing = true;
    void options
      .synthesize(batch)
      .then((audio) => {
        // Cancelled while this was in flight: the customer is already talking
        // over it, so the clip is discarded rather than played late.
        if (mine !== generation) return;
        ready.push(audio);
        pumpPlayback();
      })
      .catch((error) => {
        if (mine === generation) options.onError?.(error);
      })
      .finally(() => {
        if (mine !== generation) return;
        synthesizing = false;
        pumpSynthesis();
        settleIfIdle();
      });
  }

  function pumpPlayback(): void {
    if (playing || ready.length === 0) return;
    const mine = generation;
    const audio = ready.shift() as Audio;
    playing = true;
    setSpeaking(true);
    void options
      .play(audio)
      .catch((error) => {
        if (mine === generation) options.onError?.(error);
      })
      .finally(() => {
        if (mine !== generation) return;
        playing = false;
        // Release any playback resources.
        try {
          options.onPlayed?.(audio);
        } catch {
          // Housekeeping must never break playback.
        }
        pumpPlayback();
        pumpSynthesis();
        settleIfIdle();
      });
  }

  return {
    enqueue(text: string): void {
      const trimmed = text.trim();
      if (trimmed === "") return;
      pending.push(trimmed);
      pumpSynthesis();
    },
    cancel(): void {
      generation += 1;
      pending.length = 0;
      ready.length = 0;
      synthesizing = false;
      playing = false;
      options.stopPlayback();
      setSpeaking(false);
      const settle = waiters;
      waiters = [];
      for (const resolve of settle) resolve();
    },
    drained(): Promise<void> {
      if (!synthesizing && !playing && pending.length === 0 && ready.length === 0) {
        return Promise.resolve();
      }
      return new Promise<void>((resolve) => {
        waiters.push(resolve);
      });
    },
    get speaking(): boolean {
      return speaking;
    },
  };
}
