/**
 * Main-process voice service: asset gate, IPC surface, microphone permission
 * policy, and morning-brief pre-synthesis.
 *
 * The governing rule for this whole file is that voice is an enhancement, not
 * a dependency. Every path through it either produces audio or produces an
 * honest status string; none of them may throw into the chat path, block the
 * window, or leave the customer looking at a control that silently does
 * nothing. If the assets are missing, the HUD is exactly the typed HUD that
 * shipped in v1.3.
 */

import { promises as fs } from "node:fs";
import path from "node:path";
import {
  assertPlayableWav,
  createVoiceEngine,
  type VoiceEngine,
  type VoicePaths,
} from "@jarvis/setup/voice-engine";
import type { VoiceManifest } from "@jarvis/setup/voice-assets";
import { loadVerifiedVoice, type VoiceAnchor } from "@jarvis/setup/voice-load";
import { readVoiceAnchor } from "./voice-anchor.js";
import {
  VOICE_SAMPLE_RATE,
  ACKNOWLEDGEMENTS,
  isSlowSynthesis,
  type PcmAudio,
  type VoiceMicState,
  type VoiceStatus,
  type VoiceUnavailableReason,
} from "./voice-types.js";

export const VOICE_CACHE_DIRNAME = "voice-cache";
/** Beyond this a "brief" is a document; synthesizing it would run for minutes. */
const MAX_BRIEF_CHARS = 4_000;

export interface TtsProcess {
  postMessage(message: unknown): void;
  on(event: "message", callback: (message: any) => void): unknown;
  on(event: "exit", callback: (code: number) => void): unknown;
  kill(): boolean;
}

export interface VoiceServiceOptions {
  speakRepliesDefault?: boolean;
  forkTts?: () => TtsProcess;
  onStatusChanged?: (status: VoiceStatus) => void;

  /** Development staging directory. IGNORED when packaged. */
  voiceRoot: string;
  /** Electron's process.resourcesPath — the only root a packaged build reads. */
  resourcesPath?: string;
  userDataDir: string;
  appVersion: string;
  packaged: boolean;
  platform?: NodeJS.Platform;
  /** Test seam: swap the engine without touching disk. */
  createEngine?: typeof createVoiceEngine;
  /** Test seam: supply the build-time trust anchor instead of reading it. */
  readAnchor?: () => Promise<VoiceAnchor | null>;
}

export interface VoiceService {
  /** Verifies assets and builds the engine. Never throws. Safe to call twice. */
  init(): Promise<VoiceStatus>;
  status(): VoiceStatus;
  setSpeakReplies(enabled: boolean): VoiceStatus;
  speak(text: string): Promise<PcmAudio | null>;
  acknowledgement(text: string): PcmAudio | null;
  cancel(): void;
  setMicState(state: VoiceMicState): VoiceStatus;
  transcribe(pcm: Buffer): Promise<{ text: string; ms: number }>;
  /** Kill every child and refuse further work (application shutdown). */
  dispose(): void;
  /** Pre-generate (or await) the brief clip. Returns null if voice is unavailable. */
  prepareBrief(date: string, text: string): Promise<string | null>;
  selfTest(): Promise<{ stt: boolean; tts: boolean }>;
  readonly cacheDir: string;
}

const DETAIL: Record<VoiceUnavailableReason, string> = {
  "not-packaged": "Voice assets are not staged in this checkout. Run: node hud/scripts/fetch-voice-assets.mjs",
  "assets-missing": "Some voice files are missing from this installation. Reinstall JARVIS to restore them.",
  "assets-corrupt": "Some voice files do not match this build — antivirus quarantine is the usual cause. Reinstall JARVIS to restore them.",
  "assets-unreadable": "Some voice files are on this computer but could not be read — antivirus or a permissions problem is the usual cause. Allow JARVIS in your antivirus, then restart it.",
  "binary-missing": "The local speech programs are missing from this installation. Reinstall JARVIS to restore them.",
  "unsupported-platform": "Local voice ships for Windows and macOS. Everything else in JARVIS works here.",
  "engine-failed": "The local speech engine did not start on this machine. Typed chat is unaffected.",
  unverified: "The voice files could not be proven to be the ones this build shipped, so they will not be run. Reinstall JARVIS to restore them.",
};

import { speakable } from "./voice-chunker.js";
export { speakable } from "./voice-chunker.js";

export function createVoiceService(options: VoiceServiceOptions): VoiceService {
  const platform = options.platform ?? process.platform;
  const cacheDir = path.join(options.userDataDir, VOICE_CACHE_DIRNAME);
  const createEngine = options.createEngine ?? createVoiceEngine;

  let engine: VoiceEngine | null = null;
  let manifest: VoiceManifest | null = null;
  let initialized: Promise<VoiceStatus> | null = null;
  let reason: VoiceUnavailableReason | undefined = platform === "win32" || platform === "darwin"
    ? undefined
    : "unsupported-platform";
  let mic: VoiceMicState = "unknown";
  /** False in a development checkout with no build-time anchor. */
  let anchored = false;
  let speakReplies = options.speakRepliesDefault ?? true;
  let chatTts: NonNullable<VoiceStatus["chatTts"]> = "unavailable";
  let verifiedPaths: VoicePaths | null = null;
  let tts: TtsProcess | null = null;
  let nextId = 0;
  let chatFailures = 0;
  let disposed = false;
  let warmTimer: ReturnType<typeof setTimeout> | undefined;
  const acknowledgements = new Map<string, PcmAudio>();
  const requests = new Map<number, { resolve: (audio: PcmAudio | null) => void; reject: (error: Error) => void }>();
  function cancel(): void {
    tts?.postMessage({ type: "cancel" });
    for (const request of requests.values()) request.reject(new Error("Speech cancelled"));
    requests.clear();
  }
  function startTts(): void {
    if (disposed || !speakReplies || tts || !verifiedPaths || !options.forkTts) return;
    chatTts = "warming";
    function failed(error: unknown): void {
      if (disposed) return;
      console.warn("[voice] warm TTS unavailable; using OS voice", error);
      clearTimeout(warmTimer);
      chatTts = "unavailable";
      for (const request of requests.values()) request.resolve(null);
      requests.clear();
      options.onStatusChanged?.(snapshot());
    }
    try {
      const child = options.forkTts();
      tts = child;
      warmTimer = setTimeout(() => { failed("warm-up timed out"); child.kill(); }, 60_000);
      child.on("message", (message) => {
        if (message.type === "warm") {
          clearTimeout(warmTimer);
          chatTts = isSlowSynthesis(message.warmMs, message.audioMs) ? "slow" : "ready";
          console.info("[voice] Kokoro warm-up", { loadMs: message.loadMs, warmMs: message.warmMs, audioMs: message.audioMs, chatTts });
          if (chatTts === "slow") child.kill();
          options.onStatusChanged?.(snapshot());
        } else if (message.type === "ack") acknowledgements.set(message.text, message.audio);
        else if (message.type === "failed") { failed(message.error); child.kill(); }
        else {
          const request = requests.get(message.id);
          requests.delete(message.id);
          if (!request) return;
          if (message.type === "error") {
            chatFailures++;
            console.warn("[voice] sentence synthesis failed", message.error);
            request?.reject(new Error("Sentence synthesis failed"));
          } else request?.resolve(message.audio);
        }
      });
      child.on("exit", (code) => {
        if (chatTts !== "slow") failed(`utility process exited (${code})`);
      });
      child.postMessage({ type: "init", paths: verifiedPaths, speakerId: manifest!.tts.speakerId, phrases: ACKNOWLEDGEMENTS });
    } catch (error) { failed(error); }
  }
  const briefJobs = new Map<string, Promise<string | null>>();

  function snapshot(): VoiceStatus {
    const available = engine !== null && reason === undefined;
    return {
      available,
      speakReplies,
      chatTts,
      chatFailures,
      ...(available ? {} : { reason: reason ?? "engine-failed" }),
      detail: available
        ? (anchored
          ? "Local speech recognition and speech generation are ready."
          // Say which guarantee is actually in force. In a checkout the files
          // were verified against a manifest with nothing signed vouching for
          // it, which is weaker than what a customer's install gets.
          : "Ready (development build — voice files verified, but not against a signed manifest).")
        : DETAIL[reason ?? "engine-failed"],
      mic,
      voiceId: manifest?.tts.voiceId ?? "bm_george",
    };
  }

  async function boot(): Promise<VoiceStatus> {
    if (reason === "unsupported-platform") return snapshot();

    // The SAME verified-load function the setup wizard uses. Packaged builds
    // read only <resourcesPath>/voice, require the ASAR anchor, and verify
    // every file (hashes, symlinks, unlisted extras) before anything runs.
    const loaded = await loadVerifiedVoice({
      packaged: options.packaged,
      ...(options.resourcesPath === undefined ? {} : { resourcesPath: options.resourcesPath }),
      devRoot: options.voiceRoot,
      readAnchor: options.readAnchor ?? readVoiceAnchor,
      platform,
      allowEnvRoot: !options.packaged,
    });

    if (!loaded.ok) {
      reason = loaded.reason;
      return snapshot();
    }

    manifest = loaded.manifest;
    anchored = loaded.anchored;
    engine = createEngine({ paths: loaded.paths, manifest: loaded.manifest, cacheDir });
    reason = undefined;
    verifiedPaths = loaded.paths;
    startTts();
    // Sweeping here rather than on exit: a crash or a power cut is exactly when
    // clips get orphaned, and this is the first moment we know the cache path.
    void pruneCache(cacheDir).catch(() => {});
    return snapshot();
  }

  function requireEngine(): VoiceEngine {
    if (engine === null) throw new Error(DETAIL[reason ?? "engine-failed"]);
    return engine;
  }

  return {
    cacheDir,
    init(): Promise<VoiceStatus> {
      initialized ??= boot().catch(() => {
        reason = "engine-failed";
        return snapshot();
      });
      return initialized;
    },
    status: snapshot,
    setSpeakReplies(enabled) {
      speakReplies = enabled;
      if (!enabled) cancel();
      else startTts();
      return snapshot();
    },
    speak(text) {
      if (!speakReplies || chatTts !== "ready" || !tts) return Promise.resolve(null);
      const clean = speakable(text).slice(0, 300);
      if (!clean) return Promise.resolve(null);
      const id = ++nextId;
      return new Promise((resolve, reject) => {
        requests.set(id, { resolve, reject });
        tts!.postMessage({ type: "speak", id, text: clean });
      });
    },
    acknowledgement: (text) => speakReplies && chatTts === "ready" ? acknowledgements.get(text) ?? null : null,
    cancel,
    setMicState(state: VoiceMicState): VoiceStatus {
      mic = state;
      return snapshot();
    },
    async transcribe(pcm: Buffer): Promise<{ text: string; ms: number }> {
      const seconds = pcm.length / (VOICE_SAMPLE_RATE * 2);
      if (seconds > 240) throw new Error("That recording is too long to transcribe.");
      return requireEngine().transcribe(pcm);
    },
    dispose(): void {
      disposed = true;
      cancel();
      clearTimeout(warmTimer);
      tts?.kill();
      engine?.dispose();
    },
    async prepareBrief(date: string, text: string): Promise<string | null> {
      if (engine === null) return null;
      const clean = speakable(text).slice(0, MAX_BRIEF_CHARS);
      if (clean === "") return null;
      const key = briefKey(date, clean);
      const existing = briefJobs.get(key);
      if (existing !== undefined) return existing;

      const job = (async () => {
        const target = path.join(cacheDir, `${key}.wav`);
        // A cached clip is the whole point of pre-generation: replay has to be
        // instant, and re-synthesizing a five-paragraph brief is half a minute.
        // "Non-zero size" is NOT proof of a clip, though — validate the
        // container, or one crashed synthesis poisons this brief forever.
        const cached = await assertPlayableWav(target).then(() => true).catch(() => false);
        if (cached) return target;
        await fs.rm(target, { force: true }).catch(() => {});
        const result = await requireEngine().synthesize(clean, { key });
        return result.audioPath;
      })().catch(() => {
        // Drop the memo so a later press retries rather than being told "no"
        // forever by a cached rejection.
        briefJobs.delete(key);
        return null;
      });
      briefJobs.set(key, job);
      return job;
    },
    selfTest(): Promise<{ stt: boolean; tts: boolean }> {
      if (engine === null) return Promise.resolve({ stt: false, tts: false });
      return engine.selfTest();
    },
  };
}

/** Remove cached audio older than a week. */
export async function pruneCache(cacheDir: string, maxAgeMs = 7 * 24 * 60 * 60 * 1_000, now = Date.now()): Promise<void> {
  const entries = await fs.readdir(cacheDir).catch(() => [] as string[]);
  for (const entry of entries) {
    if (!entry.endsWith(".wav")) continue;
    const full = path.join(cacheDir, entry);
    const stat = await fs.stat(full).catch(() => null);
    if (stat !== null && now - stat.mtimeMs > maxAgeMs) {
      await fs.rm(full, { force: true }).catch(() => {});
    }
  }
}

/**
 * Keyed by content, not just date, so a brief that gets restated during the
 * day (a corrected figure, a re-run) is re-synthesized instead of replaying
 * yesterday's words under today's headline.
 */
export function briefKey(date: string, text: string): string {
  let hash = 5_381;
  for (let i = 0; i < text.length; i += 1) {
    hash = ((hash << 5) + hash + text.charCodeAt(i)) >>> 0;
  }
  const safeDate = date.replace(/[^0-9a-z-]/gi, "").slice(0, 12) || "undated";
  return `brief-${safeDate}-${hash.toString(16)}`;
}

/**
 * Microphone policy for the HUD's session.
 *
 * Electron needs BOTH handlers: the request handler covers the getUserMedia
 * prompt, the check handler covers synchronous capability queries that
 * Chromium makes without prompting. Media is granted only to our own top-level
 * page and only while a microphone press is actually in flight, so a rogue
 * iframe or a background page cannot open the mic behind the customer's back.
 */
export interface MicGate {
  /**
   * Open the gate for ONE getUserMedia request from one specific frame.
   * Returns a nonce; only the holder of that nonce can disarm the grant, so a
   * stale attempt's teardown cannot clear the grant a newer press just made.
   */
  arm(owner: MicGateOwner): number;
  /** Disarm only if `nonce` still owns the grant. Omit to force-disarm. */
  disarm(nonce?: number): void;
  /**
   * Non-consuming look: answers Chromium's capability checks. Chromium makes
   * several of these per getUserMedia, so consuming here would deny the real
   * request that follows.
   */
  peek(owner: MicGateOwner): boolean;
  /** Consuming claim for the ONE real permission request. Single use. */
  consume(owner: MicGateOwner): boolean;
  readonly armed: boolean;
}

export interface MicGateOwner {
  /** id of the WebContents that asked. */
  contentsId: number;
  /** Frame routing id, so a subframe cannot spend the main frame's grant. */
  frameId?: number;
}

/** A press-to-talk grant is worth seconds, not minutes. */
export const MIC_GATE_TTL_MS = 15_000;

export function createMicGate(options: { now?: () => number; ttlMs?: number } = {}): MicGate {
  const now = options.now ?? (() => Date.now());
  const ttlMs = options.ttlMs ?? MIC_GATE_TTL_MS;
  let owner: MicGateOwner | null = null;
  let armedAt = 0;
  let nonce = 0;

  const live = (): boolean => owner !== null && now() - armedAt <= ttlMs;

  const matches = (candidate: MicGateOwner): boolean => {
    if (!live() || owner === null) return false;
    if (owner.contentsId !== candidate.contentsId) return false;
    // A grant armed for the main frame must not be spendable by a subframe.
    if (owner.frameId !== undefined && candidate.frameId !== undefined && owner.frameId !== candidate.frameId) {
      return false;
    }
    return true;
  };

  return {
    arm(next: MicGateOwner): number {
      owner = next;
      armedAt = now();
      nonce += 1;
      return nonce;
    },
    disarm(token?: number): void {
      // Conditional by default. Rapid release-then-press means the FIRST
      // attempt's teardown runs after the SECOND has armed; an unconditional
      // disarm there would revoke the new press's grant and deny a microphone
      // the customer is actively holding down.
      if (token !== undefined && token !== nonce) return;
      owner = null;
    },
    peek(candidate: MicGateOwner): boolean {
      return matches(candidate);
    },
    consume(candidate: MicGateOwner): boolean {
      if (!matches(candidate)) return false;
      // Single use: the grant is spent by the request it was armed for.
      owner = null;
      return true;
    },
    get armed(): boolean {
      return live();
    },
  };
}

export interface PermissionRequest {
  permission: string;
  isMainFrame: boolean;
  requestingUrl: string;
  /**
   * True for the one real permission REQUEST (which consumes the grant),
   * false for Chromium's capability checks (which must not).
   */
  consuming?: boolean;
  /** setPermissionRequestHandler shape. */
  mediaTypes?: string[];
  /** setPermissionCheckHandler shape in Electron 37 — SINGULAR. */
  mediaType?: string;
  contentsId?: number;
  frameId?: number;
}

/**
 * Shared decision for both Electron permission handlers, extracted so the
 * policy itself is unit-testable without an Electron session.
 *
 * Deny-by-default at every step. In particular the two handlers are given
 * DIFFERENT shapes by Electron — `mediaTypes` (array) for a request,
 * `mediaType` (string) for a check — and reading only one of them meant a
 * video check sailed through as "no media type specified" while the gate was
 * armed for the microphone.
 */
export function decideVoicePermission(request: PermissionRequest, gate: MicGate): boolean {
  if (request.permission !== "media" && request.permission !== "audioCapture") return false;
  if (!request.isMainFrame) return false;
  if (!request.requestingUrl.startsWith("file://")) return false;

  // Normalise both shapes, then require an explicit, audio-only answer. An
  // absent media type is treated as unknown and refused rather than assumed
  // to be audio.
  const types = request.mediaTypes ?? (request.mediaType === undefined ? [] : [request.mediaType]);
  if (request.permission === "media") {
    if (types.length === 0) return false;
    if (types.some((type) => type !== "audio")) return false;
  } else if (types.some((type) => type !== "audio")) {
    return false;
  }

  const owner = {
    contentsId: request.contentsId ?? -1,
    ...(request.frameId === undefined ? {} : { frameId: request.frameId }),
  };
  return request.consuming === true ? gate.consume(owner) : gate.peek(owner);
}
