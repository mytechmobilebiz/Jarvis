// NOTE: no `node:url` (or any Node builtin) may be imported here. This preload
// runs SANDBOXED (hud/src/main.ts never sets `sandbox: false`; Electron's
// default is on), and the sandbox's `require("url")` returns a browser shim
// with no `pathToFileURL`. In v1.4.0 that made brief
// playback die silently at the path→URL conversion. Path/URL work now lives in
// the main process ("path:to-file-url"); see VOICE-DIAG-260815.
import { contextBridge, ipcRenderer } from "electron";
import type { HudSettings } from "./settings.js";
import type { SessionListResult, TranscriptResult } from "./history.js";
import type { ApprovalDecision, ApprovalRequest } from "./state-server.js";
import type { DiagnosticsReport, SetupAnswerValue, SetupEvent, SetupPrompt } from "./setup-protocol.js";
import type { PcmAudio, VoiceMicState, VoiceStatus } from "./voice-types.js";

// --- Lane C (voice) ---------------------------------------------------------
/** 120 s of 16 kHz mono PCM16 — the same ceiling capture and main enforce. */
const MAX_PCM_BYTES = 120 * 16_000 * 2;

// The bridge stays a narrow, typed surface: the renderer can ask for a
// transcript or a cached brief clip, and can never name a file path, a
// binary, or a model. Audio crosses as a plain Uint8Array of 16 kHz PCM16.
export interface JarvisVoiceBridge {
  getStatus(): Promise<VoiceStatus>;
  setSpeakReplies(enabled: boolean): Promise<VoiceStatus>;
  speak(text: string): Promise<PcmAudio | null>;
  acknowledgement(text: string): Promise<PcmAudio | null>;
  cancel(): Promise<void>;
  reportMic(state: VoiceMicState): Promise<VoiceStatus>;
  /** Opens the gate for exactly one getUserMedia call; returns its nonce. */
  armMic(): Promise<number>;
  /** Closes the gate only if `token` still owns it. */
  disarmMic(token: number): Promise<void>;
  transcribe(pcm: Uint8Array): Promise<{ text: string; ms: number }>;
  /** Resolves to a cached/synthesized brief clip (path + playable URL), or null if voice is unavailable. */
  prepareBrief(date: string, text: string): Promise<{ audioPath: string; audioUrl: string } | null>;
  onStatusChanged(callback: (status: VoiceStatus) => void): () => void;
}
// --- end Lane C -------------------------------------------------------------

export interface JarvisBridge {
  platform: NodeJS.Platform;
  getVersion(): Promise<string>;
  getNoBrainLine(): Promise<string>;
  getSettings(): Promise<HudSettings>;
  saveSettings(patch: Partial<HudSettings>): Promise<HudSettings>;
  onSettingsChanged(callback: (settings: HudSettings) => void): () => void;
  /** Async: the conversion happens in the main process (sandboxed preloads have no real `node:url`). */
  pathToFileUrl(filePath: string): Promise<string>;
  openExternal(url: string): Promise<void>;
  readClipboardText(): Promise<string>;
  writeClipboardText(text: string): Promise<void>;
  getDiagnostics(): Promise<DiagnosticsReport>;
  rerunSetup(): Promise<void>;
  connectSlack(): Promise<void>;
  listSessions(role: string, offset?: number): Promise<SessionListResult>;
  getTranscript(role: string, sessionId: string): Promise<TranscriptResult>;
  onApprovalRequest(callback: (request: ApprovalRequest | null) => void): () => void;
  answerApproval(id: string, decision: ApprovalDecision): void;
  startSetup(): Promise<{ failed: boolean }>;
  /** The one door out of setup. `entrance` shapes the HUD's arrival only —
   * "full" (the birth's choreographed arrival, the default) or "fast" (the
   * user skipped; instant office). The contract checks are unchanged. */
  launchHud(opts?: {
    entrance?: "full" | "fast";
    /** Recorded by MAIN only after the handoff checks pass (r3 P2). */
    finished?: { name: "hud_transition_finished"; props: Record<string, unknown> };
  }): Promise<void>;
  /** Fire-and-forget transition telemetry (persist-first in the main process). */
  sendTransitionEvent(name: string, props: Record<string, unknown>): void;
  /** Outstanding provider-safety / migration debt: BOOT HUD ANYWAY is refused while it stands. */
  lifecycleDebt(): Promise<{ blocked: boolean; detail: string }>;
  answerPrompt(promptId: string, value: SetupAnswerValue): void;
  onSetupEvent(callback: (event: SetupEvent) => void): () => void;
  onSetupPrompt(callback: (prompt: SetupPrompt) => void): () => void;
  voice: JarvisVoiceBridge;
}

const bridge: JarvisBridge = {
  platform: process.platform,
  getVersion: () => ipcRenderer.invoke("app:get-version") as Promise<string>,
  getNoBrainLine: () => ipcRenderer.invoke("copy:no-brain-line") as Promise<string>,
  getSettings: () => ipcRenderer.invoke("settings:get") as Promise<HudSettings>,
  saveSettings: (patch) => ipcRenderer.invoke("settings:save", patch) as Promise<HudSettings>,
  onSettingsChanged: (callback) => {
    const handler = (_event: Electron.IpcRendererEvent, settings: HudSettings): void => {
      callback(settings);
    };
    ipcRenderer.on("settings:changed", handler);
    return () => {
      ipcRenderer.off("settings:changed", handler);
    };
  },
  pathToFileUrl: (filePath) => ipcRenderer.invoke("path:to-file-url", filePath) as Promise<string>,
  openExternal: (url) => ipcRenderer.invoke("shell:open-external", url) as Promise<void>,
  readClipboardText: () => ipcRenderer.invoke("clipboard:read-text") as Promise<string>,
  writeClipboardText: (text) => ipcRenderer.invoke("clipboard:write-text", text) as Promise<void>,
  getDiagnostics: () => ipcRenderer.invoke("diagnostics:get") as Promise<DiagnosticsReport>,
  rerunSetup: () => ipcRenderer.invoke("setup:rerun") as Promise<void>,
  connectSlack: () => ipcRenderer.invoke("setup:connect-slack") as Promise<void>,
  listSessions: (role, offset) => ipcRenderer.invoke("history:list-sessions", role, offset) as Promise<SessionListResult>,
  getTranscript: (role, sessionId) => ipcRenderer.invoke("history:get-transcript", role, sessionId) as Promise<TranscriptResult>,
  onApprovalRequest: (callback) => {
    const handler = (_event: Electron.IpcRendererEvent, request: ApprovalRequest | null): void => callback(request);
    ipcRenderer.on("approval:request", handler);
    ipcRenderer.send("approval:ready");
    return () => { ipcRenderer.off("approval:request", handler); };
  },
  answerApproval: (id, decision) => { ipcRenderer.send("approval:answer", id, decision); },
  startSetup: () => ipcRenderer.invoke("setup:start") as Promise<{ failed: boolean }>,
  launchHud: (opts) => ipcRenderer.invoke("setup:launch-hud", opts) as Promise<void>,
  sendTransitionEvent: (name, props) => {
    ipcRenderer.send("setup:transition-event", { name, props });
  },
  lifecycleDebt: () => ipcRenderer.invoke("setup:lifecycle-debt") as Promise<{ blocked: boolean; detail: string }>,
  answerPrompt: (promptId, value) => {
    ipcRenderer.send("setup:answer", { promptId, value });
  },
  onSetupEvent: (callback) => {
    const handler = (_event: Electron.IpcRendererEvent, setupEvent: SetupEvent): void => {
      callback(setupEvent);
    };
    ipcRenderer.on("setup:event", handler);
    return () => {
      ipcRenderer.off("setup:event", handler);
    };
  },
  onSetupPrompt: (callback) => {
    const handler = (_event: Electron.IpcRendererEvent, prompt: SetupPrompt): void => {
      callback(prompt);
    };
    ipcRenderer.on("setup:prompt", handler);
    return () => {
      ipcRenderer.off("setup:prompt", handler);
    };
  },
  // --- Lane C (voice) -------------------------------------------------------
  voice: {
    getStatus: () => ipcRenderer.invoke("voice:status") as Promise<VoiceStatus>,
    setSpeakReplies: (enabled) => ipcRenderer.invoke("voice:set-speak-replies", enabled),
    speak: (text) => ipcRenderer.invoke("voice:speak", text),
    acknowledgement: (text) => ipcRenderer.invoke("voice:acknowledgement", text),
    cancel: () => ipcRenderer.invoke("voice:cancel"),
    reportMic: (state) => ipcRenderer.invoke("voice:report-mic", state) as Promise<VoiceStatus>,
    armMic: () => ipcRenderer.invoke("voice:arm-mic") as Promise<number>,
    disarmMic: (token) => ipcRenderer.invoke("voice:disarm-mic", token) as Promise<void>,
    transcribe: (pcm) => {
      // Bound the payload at the boundary as well as in main. A structured
      // clone of an arbitrarily large array would be copied twice before any
      // duration check downstream could reject it.
      if (!(pcm instanceof Uint8Array)) return Promise.reject(new Error("Voice received an unusable recording."));
      if (pcm.byteLength > MAX_PCM_BYTES) return Promise.reject(new Error("That recording is too long to transcribe."));
      return ipcRenderer.invoke("voice:transcribe", pcm) as Promise<{ text: string; ms: number }>;
    },
    prepareBrief: (date, text) =>
      ipcRenderer.invoke("voice:prepare-brief", date, text) as Promise<{ audioPath: string; audioUrl: string } | null>,
    onStatusChanged: (callback) => {
      const handler = (_event: Electron.IpcRendererEvent, status: VoiceStatus): void => {
        callback(status);
      };
      ipcRenderer.on("voice:status-changed", handler);
      return () => {
        ipcRenderer.off("voice:status-changed", handler);
      };
    },
  },
  // --- end Lane C -----------------------------------------------------------
};

contextBridge.exposeInMainWorld("jarvis", bridge);
