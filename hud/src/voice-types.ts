/**
 * Shared voice vocabulary for main, preload and renderer.
 *
 * Kept dependency-free so the preload bridge (CommonJS) and the renderer
 * (ESM, no Node) can both import it without dragging Electron or node: modules
 * across the context-isolation boundary.
 */

/** What the HUD is doing right now. Drives the chat-bar indicator. */
export type VoiceActivity = "idle" | "listening" | "transcribing";

/**
 * Why voice is not available. Closed set: these strings are shown to
 * customers, written into diagnostics, and sent as telemetry props, so they
 * must never carry a path, a hostname or an error message.
 */
export type VoiceUnavailableReason =
  | "not-packaged"
  | "assets-missing"
  | "assets-corrupt"
  /** Present on disk, but this computer would not let us read them. */
  | "assets-unreadable"
  | "binary-missing"
  | "unsupported-platform"
  | "engine-failed"
  /** Assets present but not provably the ones this signed build shipped. */
  | "unverified";

/** Microphone availability, as far as the main process can tell. */
export type VoiceMicState = "unknown" | "granted" | "denied" | "no-device";

export interface VoiceStatus {
  /** True only when every asset hashed clean and both binaries are present. */
  available: boolean;
  reason?: VoiceUnavailableReason;
  /** User-facing one-liner. Honest, never blames the customer, never a path. */
  detail: string;
  mic: VoiceMicState;
  voiceId: string;
  speakReplies?: boolean;
  chatFailures?: number;
  chatTts?: "warming" | "ready" | "slow" | "unavailable";
}

export interface VoiceTranscript {
  text: string;
  /** Wall-clock ms from receiving the PCM to having the transcript. */
  ms: number;
}

export interface VoiceFailure {
  failed: true;
  reason: VoiceUnavailableReason | "cancelled" | "no-speech";
  detail: string;
}

export type VoiceTranscriptResult = VoiceTranscript | VoiceFailure;

export function isVoiceFailure(result: unknown): result is VoiceFailure {
  return typeof result === "object" && result !== null && (result as VoiceFailure).failed === true;
}

/** Capture format. Fixed everywhere: whisper.cpp accepts nothing else. */
export const VOICE_SAMPLE_RATE = 16_000;

/**
 * Marketing-honesty line. Voice is local; the model call is not. Surfaced in
 * settings and in the setup copy so no customer can read "local" as "offline".
 */
export const VOICE_PRIVACY_LINE =
  "Voice recognition and speech generation run locally on your computer. "
  + "Your request still goes to the AI provider you selected.";

export interface PcmAudio {
  samples: Float32Array;
  sampleRate: number;
}

export function isSlowSynthesis(ms: number, audioMs: number): boolean {
  return !Number.isFinite(ms) || !Number.isFinite(audioMs) || audioMs <= 0 || ms > audioMs;
}

export const ACKNOWLEDGEMENTS = ["On it.", "One moment.", "Let me check.", "Right away.", "Sure."];
export function nextAcknowledgement(previous: number, random = Math.random()): number {
  const count = ACKNOWLEDGEMENTS.length;
  if (previous < 0) return Math.floor(random * count);
  return (previous + 1 + Math.floor(random * (count - 1))) % count;
}
