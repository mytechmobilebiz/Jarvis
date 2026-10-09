/**
 * The voice half of the HUD chat bar: push-to-talk, the visible state line,
 * and speech recognition.
 *
 * Accessibility rule (Scott Reed, support/CLAUDE.md): everything voice does
 * must also be visible and doable as text. So a transcript is inserted into
 * the chat input and sent as an ordinary user message — the chat log after a
 * spoken turn is indistinguishable from a typed one — and every voice state
 * and failure is announced in an aria-live region rather than only as a colour
 * change on a button.
 */

import { MicError, startCapture, type MicRecording } from "./voice-capture.js";
import { createReplyChunker } from "./voice-chunker.js";
import { createSpeechQueue } from "./voice-queue.js";
import { ACKNOWLEDGEMENTS, nextAcknowledgement, type PcmAudio } from "./voice-types.js";
import type { VoiceStatus } from "./voice-types.js";
import type { JarvisVoiceBridge } from "./preload.cjs";

export interface VoiceUiElements {
  mic: HTMLButtonElement;
  speakReplies: HTMLInputElement;
  stop: HTMLButtonElement;
  state: HTMLElement;
  form: HTMLElement;
  input: HTMLInputElement;
  detail: HTMLElement;
  voiceId: HTMLElement;
  /** The chat drawer and its collapse toggle; a hold pops the drawer open. */
  chat: HTMLElement;
  chatToggle: HTMLButtonElement;
}

export interface VoiceUiDeps {
  bridge: JarvisVoiceBridge;
  elements: VoiceUiElements;
  /** Sends a message exactly as the typed path does. */
  submit: (text: string) => void;
  /** True while a reply is streaming; a second PTT must not race it. */
  isBusy: () => boolean;
  capture?: typeof startCapture;
  player?: ReturnType<typeof createSpeechPlayer>;
  speakReplies?: boolean;
}

export interface VoiceUi {
  refresh(): Promise<void>;
  interrupt(): void;
  beginReply(): number;
  onReplyToken(token: string, generation: number): void;
  onReplyEnd(generation: number): void;
  readonly status: VoiceStatus | null;
}

const IDLE = "";

export function createVoiceUi(deps: VoiceUiDeps): VoiceUi {
  const { bridge, elements } = deps;
  const capture = deps.capture ?? startCapture;

  let status: VoiceStatus | null = null;
  let recording: MicRecording | null = null;
  let replyGeneration = 0;
  let muted = false;
  let previousAck = -1;
  const chunker = createReplyChunker();
  const player = deps.player ?? createSpeechPlayer();
  const queue = createSpeechQueue<PcmAudio | string | null>({
    async synthesize(text) {
      if (ACKNOWLEDGEMENTS.includes(text)) {
        if (status === null || status.chatTts === "warming") return null;
        return status?.chatTts === "ready" ? bridge.acknowledgement(text) : text;
      }
      return await bridge.speak(text) ?? text;
    },
    play: (audio) => audio === null ? Promise.resolve() : player.play(audio),
    stopPlayback: () => player.stop(),

    onError: (error) => console.warn("[voice] sentence skipped", error),
  });
  function interrupt(): void {
    replyGeneration++;
    muted = true;
    elements.stop.toggleAttribute("hidden", true);
    chunker.reset();
    queue.cancel();
    void bridge.cancel().catch((error) => console.warn("[voice] cancel failed", error));
  }

  /**
   * Bumped on every press AND every release. Opening the microphone is async,
   * so a release can land while getUserMedia is still pending; the resolved
   * stream then belongs to an attempt nobody is waiting for. Comparing the
   * token on resume is what lets us abort it instead of storing it with no
   * future stop.
   */
  let attempt = 0;
  let pressed = false;
  let transcribing = false;

  function setState(text: string, kind = ""): void {
    elements.state.textContent = text;
    elements.state.className = `voice-state${kind === "" ? "" : ` ${kind}`}`;
    elements.state.toggleAttribute("hidden", text === "");
  }

  function applyStatus(next: VoiceStatus): void {
    const wasEnabled = status?.speakReplies !== false;
    status = next;
    elements.speakReplies.checked = next.speakReplies !== false;
    if (wasEnabled && next.speakReplies === false) interrupt();
    elements.mic.toggleAttribute("hidden", !next.available);
    elements.form.classList.toggle("has-voice", next.available);
    elements.voiceId.textContent = next.available ? next.voiceId.toUpperCase() : "UNAVAILABLE";
    elements.detail.textContent = next.detail;
    if (!next.available) {
      setState(IDLE);
    }
  }

  /**
   * Holding the shortcut with the drawer collapsed used to record into a
   * hidden panel: the "Listening" line, the transcript and the reply all
   * landed below the fold of a 40px header strip (Peter, live testing). A hold
   * now pops the drawer open so dictation is visible from the first moment.
   *
   * Deliberately a pure class flip, exactly what the header toggle does:
   * focus does not move, so the held key keeps its original target — focusing
   * the chat box here would make every Space key-repeat start typing spaces —
   * and the capture pipeline is never touched. Cosmetics must not be able to
   * break the recording, hence the swallow-everything catch.
   */
  function revealChat(): void {
    try {
      if (!elements.chat.classList.contains("collapsed")) return;
      elements.chat.classList.remove("collapsed");
      elements.chatToggle.setAttribute("aria-expanded", "true");
    } catch {
      // The drawer is presentation; the microphone must open regardless.
    }
  }

  /** Stop and release any capture, whatever state it is in. */
  function releaseRecording(): void {
    const active = recording;
    recording = null;
    active?.abort();
  }

  async function beginListening(): Promise<void> {
    if (pressed || transcribing || status?.available !== true) return;
    interrupt();
    pressed = true;
    attempt += 1;
    const mine = attempt;

    elements.mic.classList.add("active");
    // Honest about what dictation does here: recognition is whole-utterance
    // (whisper.cpp decodes the clip once, on release), so no words can stream
    // in mid-hold. Saying "release to send" stops the silence reading as
    // broken while someone watches the open drawer and sees no text appear.
    setState("Listening — release to send", "listening");
    revealChat();

    let started: MicRecording | undefined;
    let armToken: number | null = null;
    try {
      armToken = await bridge.armMic();
      // The release may already have happened while arming. Checking here
      // avoids opening the microphone at all for an attempt that is over.
      if (mine !== attempt || !pressed) return;
      started = await capture();
      // The release already happened while the microphone was opening. Nothing
      // will ever call stop() on this, so it must be torn down here.
      if (mine !== attempt || !pressed) {
        started.abort();
        return;
      }
      recording = started;
      await bridge.reportMic("granted");
    } catch (error) {
      // Anything after getUserMedia resolved — including a rejected
      // reportMic — must still release the live microphone.
      started?.abort();
      if (mine === attempt) {
        pressed = false;
        elements.mic.classList.remove("active");
        recording = null;
        await reportMicFailure(error);
      }
    } finally {
      // Disarm only OUR grant. A rapid release-then-press means this teardown
      // runs after the next attempt has armed, and an unconditional disarm
      // would revoke the microphone the customer is currently holding down.
      if (armToken !== null) void bridge.disarmMic(armToken).catch(() => {});
    }
  }

  async function endListening(): Promise<void> {
    if (!pressed) return;
    pressed = false;
    // Invalidate any capture still starting up, so beginListening's resume
    // path aborts it rather than adopting it.
    attempt += 1;
    elements.mic.classList.remove("active");
    const active = recording;
    recording = null;
    if (active === null) return;

    transcribing = true;
    setState("Transcribing…");
    try {
      const pcm = await active.stop();
      // Shorter than this is a mis-press, not an utterance. Transcribing it
      // wastes two seconds to produce nothing.
      if (pcm.byteLength < 16_000) {
        setState(IDLE);
        return;
      }
      const { text } = await bridge.transcribe(pcm);
      if (text === "") {
        setState("I didn't catch that.", "error");
        return;
      }
      // Visible as text before it is sent, exactly like typing it: the input
      // is filled, then submitted through the ordinary chat path.
      elements.input.value = text;
      // The chat path drops a submit while a reply is still streaming, which
      // would strand the transcript in the box with no explanation.
      if (deps.isBusy()) {
        setState("Transcribed — press Enter to send once the reply finishes.", "error");
        elements.input.focus();
        return;
      }
      setState(IDLE);
      deps.submit(text);
    } catch {
      setState("Speech recognition did not run. You can still type.", "error");
    } finally {
      // Belt and braces: the microphone must be released even if stop() threw.
      active.abort();
      transcribing = false;
    }
  }

  async function reportMicFailure(error: unknown): Promise<void> {
    const kind = error instanceof MicError ? error.kind : "unavailable";
    if (kind === "denied") {
      await bridge.reportMic("denied").catch(() => {});
      setState(
        navigator.userAgent.includes("Windows")
          ? "Microphone access is off. Check Settings › Privacy & security › Microphone, including \"Let desktop apps access your microphone\"."
          : "Microphone access is off. Check System Settings › Privacy & Security › Microphone.",
        "error",
      );
      return;
    }
    if (kind === "no-device") {
      await bridge.reportMic("no-device").catch(() => {});
      setState("No microphone found. You can still type.", "error");
      return;
    }
    setState("The microphone could not be opened. You can still type.", "error");
  }

  elements.mic.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    void beginListening();
  });
  // pointerup on the window, not the button: releasing after the cursor has
  // slid off the mic must still end the recording rather than leave it open.
  window.addEventListener("pointerup", () => {
    void endListening();
  });
  window.addEventListener("pointercancel", () => {
    void endListening();
  });

  window.addEventListener("keydown", (event) => {
    if (!isPushToTalk(event)) return;
    event.preventDefault();
    void beginListening();
  });
  window.addEventListener("keyup", (event) => {
    if (event.code !== "Space") return;
    void endListening();
  });
  // Losing focus mid-hold (alt-tab, a dialog) would otherwise leave the mic
  // open with no key-up ever arriving.
  window.addEventListener("blur", () => {
    void endListening();
  });
  // A reload or a close must not leave the microphone live — including a
  // capture that is still starting up, which has no recording to abort yet.
  window.addEventListener("pagehide", () => {
    pressed = false;
    attempt += 1;
    releaseRecording();
    interrupt();
  });

  elements.stop.addEventListener("click", interrupt);
  elements.speakReplies.addEventListener("change", () => {
    if (!elements.speakReplies.checked) interrupt();
    void bridge.setSpeakReplies(elements.speakReplies.checked).then(applyStatus)
      .catch((error) => console.warn("[voice] setting failed", error));
  });
  bridge.onStatusChanged(applyStatus);

  return {
    interrupt,
    beginReply() {
      interrupt();
      muted = (status?.speakReplies ?? deps.speakReplies) === false;
      if (!muted) {
        elements.stop.toggleAttribute("hidden", false);
        player.resume();
        previousAck = nextAcknowledgement(previousAck);
        queue.enqueue(ACKNOWLEDGEMENTS[previousAck]);
      }
      return replyGeneration;
    },
    onReplyToken(token, generation) {
      if (muted || generation !== replyGeneration) return;
      for (const sentence of chunker.push(token)) queue.enqueue(sentence);
    },
    onReplyEnd(generation) {
      if (muted || generation !== replyGeneration) return;
      for (const sentence of chunker.flush()) queue.enqueue(sentence);
      void queue.drained().then(() => {
        if (generation === replyGeneration) elements.stop.toggleAttribute("hidden", true);
      });
    },
    async refresh(): Promise<void> {
      try {
        const next = await bridge.getStatus();
        if (next !== null) applyStatus(next);
      } catch {
        // A status call that fails leaves the mic hidden, which is the safe
        // default: the HUD is simply the typed HUD.
      }
    },
    get status(): VoiceStatus | null {
      return status;
    },
  };
}

/**
 * Hold Space to talk when the caret is not in a text field, or Ctrl+Space from
 * anywhere including mid-sentence in the chat box. Deliberately not Alt+Space
 * (the Windows window menu) or anything with Meta (Spotlight).
 */
export function isPushToTalk(event: KeyboardEvent): boolean {
  if (event.code !== "Space" || event.repeat || event.metaKey || event.altKey) return false;
  if (event.ctrlKey) return true;
  if (event.shiftKey) return false;
  const target = event.target as HTMLElement | null;
  const tag = target?.tagName ?? "";
  return tag !== "INPUT" && tag !== "TEXTAREA" && target?.isContentEditable !== true;
}

/** PCM and OS speech share one player so acknowledgements cannot overlap replies. */
export function createSpeechPlayer() {
  let context: AudioContext | null = null;
  let source: AudioBufferSourceNode | null = null;
  let finish: (() => void) | null = null;
  return {
    resume() {
      context ??= new AudioContext();
      void context.resume().catch((error) => console.warn("[voice] audio resume failed", error));
    },
    play(audio: PcmAudio | string): Promise<void> {
      return new Promise((resolve, reject) => {
        const done = () => { if (finish === done) { finish = null; source = null; } resolve(); };
        finish = done;
        if (typeof audio === "string") {
          const utterance = new SpeechSynthesisUtterance(audio);
          const voices = window.speechSynthesis.getVoices();
          utterance.voice = voices.find((voice) => /^en-GB/i.test(voice.lang))
            ?? voices.find((voice) => /^en/i.test(voice.lang)) ?? null;
          utterance.lang = utterance.voice?.lang ?? "en-GB";
          utterance.onend = done;
          utterance.onerror = (event) => { done(); console.warn("[voice] OS speech failed", event.error); };
          window.speechSynthesis.speak(utterance);
        } else {
          context ??= new AudioContext();
          const buffer = context.createBuffer(1, audio.samples.length, audio.sampleRate);
          buffer.copyToChannel(new Float32Array(audio.samples), 0);
          source = context.createBufferSource();
          source.buffer = buffer;
          source.connect(context.destination);
          const node = source;
          node.onended = () => { node.disconnect(); done(); };
          node.start();
          void context.resume().catch((error) => { node.stop(); reject(error); done(); });
        }
      });
    },
    stop() {
      if (source) { source.onended = null; source.stop(); source.disconnect(); source = null; }
      window.speechSynthesis?.cancel();
      finish?.();
    },
  };
}
