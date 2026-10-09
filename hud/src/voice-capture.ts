/**
 * Renderer-side microphone capture and playback.
 *
 * Capture is Web Audio, never MediaRecorder: MediaRecorder's container and
 * codec support varies machine to machine, and whisper.cpp wants raw 16 kHz
 * mono PCM anyway. Asking for an AudioContext at 16 kHz lets Chromium's own
 * (well-tested, Windows-hardened) resampler do the rate conversion instead of
 * hand-rolling one here.
 *
 * The capture graph is an AudioWorklet where available, falling back to a
 * ScriptProcessorNode. The fallback is deprecated but universally present, and
 * a deprecated node that records is worth more than a modern one that throws.
 */

import { VOICE_SAMPLE_RATE } from "./voice-types.js";

/** Hard stop, so a stuck key or a forgotten button cannot record forever. */
export const MAX_CAPTURE_SECONDS = 120;

export type MicFailure = "denied" | "no-device" | "unavailable";

export class MicError extends Error {
  readonly kind: MicFailure;

  constructor(kind: MicFailure, message: string) {
    super(message);
    this.kind = kind;
    this.name = "MicError";
  }
}

export interface MicRecording {
  /** Stops capture and returns 16 kHz mono PCM16, ready for the engine. */
  stop(): Promise<Uint8Array>;
  /** Tears everything down and discards audio (barge-in, error paths). */
  abort(): void;
  /** False once the microphone has actually been released. */
  readonly active: boolean;
}

const WORKLET_SOURCE = `
class JarvisCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length > 0) this.port.postMessage(new Float32Array(channel));
    return true;
  }
}
registerProcessor('jarvis-capture', JarvisCaptureProcessor);
`;

export interface StartCaptureDeps {
  getUserMedia?: (constraints: MediaStreamConstraints) => Promise<MediaStream>;
  createContext?: (sampleRate: number) => AudioContext;
}

export async function startCapture(deps: StartCaptureDeps = {}): Promise<MicRecording> {
  const getUserMedia = deps.getUserMedia
    ?? ((constraints) => navigator.mediaDevices.getUserMedia(constraints));

  let stream: MediaStream;
  try {
    stream = await getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });
  } catch (error) {
    throw asMicError(error);
  }

  // From here on the microphone is LIVE. Every failure path below must stop
  // the tracks before rethrowing, or the recording indicator stays on with
  // nothing holding a reference to turn it off.
  const stopTracks = (): void => {
    for (const track of stream.getTracks()) {
      try {
        track.stop();
      } catch {
        // Already ended.
      }
    }
  };

  let context: AudioContext;
  let source: MediaStreamAudioSourceNode;
  try {
    context = (deps.createContext ?? defaultContext)(VOICE_SAMPLE_RATE);
    source = context.createMediaStreamSource(stream);
  } catch (error) {
    stopTracks();
    throw new MicError("unavailable", "The microphone could not be opened.");
  }

  const chunks: Float32Array[] = [];
  let frames = 0;
  let stopped = false;

  const limit = VOICE_SAMPLE_RATE * MAX_CAPTURE_SECONDS;
  const collect = (samples: Float32Array): void => {
    if (stopped || frames >= limit) return;
    // Trim the final block to the ceiling instead of appending it whole. A
    // block that straddles the limit would push a legitimate hard-stop
    // recording past MAX_PCM_BYTES, and the IPC guard would then reject the
    // very recording the hard stop just produced.
    const room = limit - frames;
    const slice = samples.length > room ? samples.subarray(0, room) : samples;
    chunks.push(slice);
    frames += slice.length;
  };

  let disconnect: () => void;
  try {
    disconnect = await attachWorklet(context, source, collect);
  } catch {
    try {
      disconnect = attachScriptProcessor(context, source, collect);
    } catch (error) {
      stopTracks();
      void context.close().catch(() => {});
      throw new MicError("unavailable", "The microphone could not be opened.");
    }
  }

  const teardown = (): void => {
    if (stopped) return;
    stopped = true;
    if (guard !== undefined) clearTimeout(guard);
    try {
      disconnect();
      source.disconnect();
    } catch {
      // Already torn down; nothing here is worth surfacing to the customer.
    }
    // Tracks first and unconditionally: this is what actually turns the
    // microphone indicator off.
    stopTracks();
    void context.close().catch(() => {});
  };

  // A real hard stop. Capping the BUFFER (as this used to) leaves the
  // microphone open forever behind a stuck key — it just stops remembering
  // what it hears, which is the worst of both worlds.
  const guard = setTimeout(teardown, MAX_CAPTURE_SECONDS * 1_000);

  return {
    async stop(): Promise<Uint8Array> {
      if (stopped) return encodePcm16(chunks, frames);
      teardown();
      return encodePcm16(chunks, frames);
    },
    abort(): void {
      teardown();
      chunks.length = 0;
      frames = 0;
    },
    get active(): boolean {
      return !stopped;
    },
  };
}

function defaultContext(sampleRate: number): AudioContext {
  return new AudioContext({ sampleRate });
}

async function attachWorklet(
  context: AudioContext,
  source: MediaStreamAudioSourceNode,
  collect: (samples: Float32Array) => void,
): Promise<() => void> {
  // A blob URL keeps the worklet in this file rather than as a loose script
  // that electron-builder would have to remember to package.
  const url = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: "text/javascript" }));
  try {
    await context.audioWorklet.addModule(url);
  } finally {
    URL.revokeObjectURL(url);
  }
  const node = new AudioWorkletNode(context, "jarvis-capture");
  node.port.onmessage = (event: MessageEvent<Float32Array>) => collect(event.data);
  source.connect(node);
  // Worklets are not pulled unless the graph reaches a destination. A
  // zero-gain sink keeps it running without putting the microphone on the
  // speakers, which would be a feedback loop as well as a nasty surprise.
  const sink = context.createGain();
  sink.gain.value = 0;
  node.connect(sink);
  sink.connect(context.destination);
  return () => {
    node.port.onmessage = null;
    node.disconnect();
    sink.disconnect();
  };
}

function attachScriptProcessor(
  context: AudioContext,
  source: MediaStreamAudioSourceNode,
  collect: (samples: Float32Array) => void,
): () => void {
  const node = context.createScriptProcessor(4_096, 1, 1);
  node.onaudioprocess = (event) => collect(new Float32Array(event.inputBuffer.getChannelData(0)));
  source.connect(node);
  const sink = context.createGain();
  sink.gain.value = 0;
  node.connect(sink);
  sink.connect(context.destination);
  return () => {
    node.onaudioprocess = null;
    node.disconnect();
    sink.disconnect();
  };
}

export function encodePcm16(chunks: Float32Array[], frames: number): Uint8Array {
  const out = new Uint8Array(frames * 2);
  const view = new DataView(out.buffer);
  let offset = 0;
  for (const chunk of chunks) {
    for (let i = 0; i < chunk.length; i += 1) {
      // Clamp before scaling: Web Audio can hand back values slightly outside
      // [-1, 1], which would wrap around into loud noise as signed 16-bit.
      const sample = Math.max(-1, Math.min(1, chunk[i]));
      view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
      offset += 2;
    }
  }
  return out;
}

function asMicError(error: unknown): MicError {
  const name = typeof error === "object" && error !== null && "name" in error ? String(error.name) : "";
  if (name === "NotAllowedError" || name === "SecurityError") {
    return new MicError("denied", "Microphone access was declined.");
  }
  if (name === "NotFoundError" || name === "OverconstrainedError") {
    return new MicError("no-device", "No microphone was found on this computer.");
  }
  return new MicError("unavailable", "The microphone could not be opened.");
}
