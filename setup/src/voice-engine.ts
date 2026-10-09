/**
 * Owns the two voice child processes. Main-process only.
 *
 * Both engines are upstream prebuilt executables invoked as separate children
 * (hud/scripts/fetch-voice-assets.mjs stages them). That is deliberate and
 * load-bearing:
 *  - the repo forbids native Node modules (see history.ts:1-11), and
 *  - sherpa-onnx statically links GPL-3.0 eSpeak NG, which is fine across a
 *    process boundary and never fine inside a proprietary addon.
 *
 * Every spawn is argv-array, no shell, absolute argv[0], with a bare
 * environment. Nothing here reads a customer-supplied path and nothing here
 * opens a socket — the no-network test asserts the second part.
 *
 * Two invariants this file exists to guarantee, both learned from review:
 *
 *  1. **Every child is reachable and killable.** A cancelled job must actually
 *     terminate its process, not merely drop the promise. Otherwise rapid
 *     push-to-talk leaves a pile of Kokoro processes competing for the CPU,
 *     and quitting orphans them.
 *  2. **Only one engine runs at a time.** STT and TTS each want most of the
 *     machine, and half-duplex means we never legitimately need both. Jobs are
 *     serialized through a single lane so a barge-in cannot overlap them.
 */

import { spawn, type ChildProcess } from "node:child_process";
import { promises as fs } from "node:fs";
import type { FileHandle } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import type { VoiceManifest } from "./voice-assets.js";

export interface SpawnedChild {
  /** Resolves once the process exits or is killed. */
  done: Promise<{ code: number; stdout: string; stderr: string }>;
  kill: () => void;
}

/** Injectable so tests can drive the protocol without real executables. */
export type SpawnChild = (
  file: string,
  args: string[],
  options: { cwd: string; env: NodeJS.ProcessEnv },
) => SpawnedChild;

export interface VoicePaths {
  root: string;
  whisperCli: string;
  whisperModel: string;
  ttsBin: string;
  kokoroModel: string;
  kokoroVoices: string;
  kokoroTokens: string;
  kokoroLexicon: string;
  espeakData: string;
}

export interface VoiceEngineOptions {
  paths: VoicePaths;
  manifest: VoiceManifest;
  cacheDir: string;
  spawnChild?: SpawnChild;
  cpuCount?: number;
  now?: () => number;
  /** Test seam for the watchdog timers. */
  setTimer?: (fn: () => void, ms: number) => { clear: () => void };
}

export interface TranscribeResult {
  text: string;
  ms: number;
}

export interface SynthesizeResult {
  audioPath: string;
  ms: number;
}

/**
 * Carries the generation a job was admitted under. Re-checked after every
 * await so a cancel during pre-spawn filesystem work cannot be outrun.
 */
export interface JobToken {
  readonly generation: number;
  readonly cancelled: boolean;
  assertLive(): void;
}

/** Thrown when a job is cancelled by barge-in or shutdown. */
export class VoiceCancelled extends Error {
  constructor() {
    super("Voice job was cancelled.");
    this.name = "VoiceCancelled";
  }
}

/**
 * Speech speed. The Codex report puts Kokoro's natural register at roughly
 * 0.92-0.96 of nominal for this voice; sherpa expresses it as a length scale
 * (larger is slower), so 1.05 is that same small slowdown.
 */
export const KOKORO_LENGTH_SCALE = 1.05;

const STT_TIMEOUT_MS = 30_000;
const TTS_TIMEOUT_MS = 120_000;
/** Utterances are push-to-talk, so anything past this is a stuck button. */
export const MAX_UTTERANCE_SECONDS = 120;
/** Grace between SIGTERM and SIGKILL before we stop being polite. */
const KILL_GRACE_MS = 2_000;

export function resolveVoicePaths(root: string, platform: NodeJS.Platform = process.platform): VoicePaths {
  const binDir = path.join(root, "bin", platform === "win32" ? "win32" : "darwin");
  const exe = platform === "win32" ? ".exe" : "";
  const kokoro = path.join(root, "models", "kokoro");
  return {
    root,
    whisperCli: path.join(binDir, `whisper-cli${exe}`),
    whisperModel: path.join(root, "models", "whisper", "ggml-base.en-q5_1.bin"),
    ttsBin: path.join(binDir, `sherpa-onnx-offline-tts${exe}`),
    kokoroModel: path.join(kokoro, "model.onnx"),
    kokoroVoices: path.join(kokoro, "voices.bin"),
    kokoroTokens: path.join(kokoro, "tokens.txt"),
    kokoroLexicon: path.join(kokoro, "lexicon-gb-en.txt"),
    espeakData: path.join(kokoro, "espeak-ng-data"),
  };
}

export interface VoiceEngine {
  /** 16 kHz mono PCM16 -> text. Returns "" when the clip held no speech. */
  transcribe(pcm: Buffer): Promise<TranscribeResult>;
  /** Text -> a WAV under the cache dir. `key` makes the filename stable. */
  synthesize(text: string, options?: { speakerId?: number; key?: string }): Promise<SynthesizeResult>;
  /** Kill every running and queued job. Safe to call at any time. */
  cancel(): void;
  /** Cancel and refuse all future work (app shutdown). */
  dispose(): void;
  selfTest(): Promise<{ stt: boolean; tts: boolean }>;
  /** Test/diagnostic view of how many children are alive right now. */
  readonly liveChildren: number;
}

export function createVoiceEngine(options: VoiceEngineOptions): VoiceEngine {
  const { paths, manifest, cacheDir } = options;
  const spawnChild = options.spawnChild ?? defaultSpawn;
  const now = options.now ?? (() => Date.now());
  const setTimer = options.setTimer ?? defaultTimer;
  // Leave the machine two cores. On a four-core laptop that is two threads for
  // inference and enough headroom that the HUD itself keeps painting.
  //
  // The FLOOR IS TWO, and that is not a performance choice. With
  // `--num-threads=1` the previously bundled Kokoro int8 model produces a well-formed WAV
  // of PURE SILENCE on macOS/arm64 — exit 0, plausible duration, "Saved
  // successfully!", every sample zero. Reproduced on the 3-core macOS CI runner
  // and again on an 10-core Mac by forcing one thread; two threads produce
  // correct audio on the same machine, same payload, same text (CI run
  // 31688337494). Any Mac with two or three cores would otherwise ship a JARVIS
  // that "speaks" inaudibly — and, before the silence check, would have been
  // told its voice was working.
  const threads = Math.max(2, Math.min(8, (options.cpuCount ?? os.cpus().length) - 2));

  /** Every child currently alive, so cancel() and dispose() can reach them. */
  const live = new Set<{ kill: () => void }>();
  /** Bumped by cancel(); a job whose generation is stale aborts on resume. */
  let generation = 0;
  let disposed = false;
  /** The serialization lane: every job awaits the previous one. */
  let lane: Promise<unknown> = Promise.resolve();

  /**
   * Runs `job` with only one engine active at a time. The lane is advanced
   * even when a job throws, so one failure cannot wedge voice for the session.
   */
  function enqueue<T>(job: (token: JobToken) => Promise<T>): Promise<T> {
    const mine = generation;
    const result = lane.then(() => {
      // The token carries the generation this job was ADMITTED under, and is
      // re-checked after every await. Reading `generation` afresh later would
      // silently adopt a newer one — which is exactly how a job that was
      // cancelled during its pre-spawn filesystem work went on to spawn a
      // child after killAll() had already run and found nothing to kill.
      const token: JobToken = {
        generation: mine,
        get cancelled(): boolean {
          return disposed || mine !== generation;
        },
        assertLive(): void {
          if (disposed || mine !== generation) throw new VoiceCancelled();
        },
      };
      token.assertLive();
      return job(token);
    });
    lane = result.catch(() => undefined);
    return result;
  }

  /**
   * Spawns one child and guarantees it is dead by the time this resolves,
   * whether it exited, timed out, or was cancelled.
   */
  async function runChild(
    token: JobToken,
    file: string,
    args: string[],
    timeoutMs: number,
  ): Promise<{ code: number; stdout: string; stderr: string }> {
    // The last possible moment before a process exists. Everything above this
    // line is cancellable without leaving anything behind; everything below
    // has to be cleaned up.
    token.assertLive();
    const child = spawnChild(file, args, { cwd: path.dirname(file), env: childEnv() });
    const handle = { kill: child.kill };
    live.add(handle);

    let timedOut = false;
    const timer = setTimer(() => {
      timedOut = true;
      child.kill();
    }, timeoutMs);

    try {
      const result = await child.done;
      if (timedOut) throw new Error(`${path.basename(file)} timed out after ${timeoutMs} ms`);
      token.assertLive();
      return result;
    } finally {
      timer.clear();
      live.delete(handle);
      // Belt and braces: if the promise resolved by any path other than a
      // clean exit, make sure nothing is still running.
      child.kill();
    }
  }

  async function transcribe(pcm: Buffer): Promise<TranscribeResult> {
    return enqueue(async (token) => {
      const started = now();
      await fs.mkdir(cacheDir, { recursive: true });
      token.assertLive();
      const wavPath = path.join(cacheDir, `utterance-${process.pid}-${started}-${token.generation}.wav`);
      await fs.writeFile(wavPath, wavFromPcm16(pcm));
      try {
        // Cancelled while the recording was being written: stop here, and let
        // the finally below delete it.
        token.assertLive();
        const result = await runChild(token, paths.whisperCli, [
          "-m", paths.whisperModel,
          "-f", wavPath,
          "-l", "en",
          "--no-timestamps",
          "--no-prints",
          "-t", String(threads),
          // Push-to-talk clips are short and self-contained; greedy decoding is
          // both faster and less prone to the repetition loops beam search can
          // fall into on a clip that is mostly silence.
          "-bs", "1",
        ], STT_TIMEOUT_MS);
        if (result.code !== 0) throw new Error(`whisper-cli exited ${result.code}`);
        return { text: cleanTranscript(result.stdout), ms: now() - started };
      } finally {
        // The recording is the most private byte sequence in the product. It
        // goes as soon as the transcript exists, on every path.
        await fs.rm(wavPath, { force: true }).catch(() => {});
      }
    });
  }

  async function synthesize(text: string, opts: { speakerId?: number; key?: string } = {}): Promise<SynthesizeResult> {
    return enqueue(async (token) => {
      const started = now();
      const speakerId = opts.speakerId ?? manifest.tts.speakerId;
      await fs.mkdir(cacheDir, { recursive: true });
      token.assertLive();
      const audioPath = path.join(cacheDir, `${opts.key ?? `say-${process.pid}-${started}`}.wav`);
      // Synthesize to a unique temp file and rename only after the WAV is
      // proven well-formed. Writing straight to audioPath means a crash or a
      // timeout leaves a truncated clip that the brief cache would happily
      // serve forever as a valid hit.
      const tmpPath = `${audioPath}.${process.pid}.${started}.part`;
      try {
        const result = await runChild(token, paths.ttsBin, [
          `--kokoro-model=${paths.kokoroModel}`,
          `--kokoro-voices=${paths.kokoroVoices}`,
          `--kokoro-tokens=${paths.kokoroTokens}`,
          `--kokoro-lexicon=${paths.kokoroLexicon}`,
          `--kokoro-data-dir=${paths.espeakData}`,
          "--kokoro-lang=en",
          `--kokoro-length-scale=${KOKORO_LENGTH_SCALE}`,
          `--sid=${speakerId}`,
          `--num-threads=${threads}`,
          `--output-filename=${tmpPath}`,
          text,
        ], TTS_TIMEOUT_MS);
        if (result.code !== 0) throw new Error(`sherpa-onnx-offline-tts exited ${result.code}`);
        try {
          await assertPlayableWav(tmpPath);
        } catch (error) {
          // The engine's own explanation is worth more than ours: it names a
          // lexicon it could not read or a data directory it could not find.
          const said = result.stderr.trim().split(/\r?\n/).slice(-4).join(" ").slice(0, 400);
          throw new Error(said === ""
            ? `${(error as Error).message}.`
            : `${(error as Error).message}. It said: ${said}`);
        }
        // Cancelled while validating: do not publish a clip for an abandoned
        // turn — the catch below removes the temp file.
        token.assertLive();
        await fs.rename(tmpPath, audioPath);
        return { audioPath, ms: now() - started };
      } catch (error) {
        await fs.rm(tmpPath, { force: true }).catch(() => {});
        throw error;
      }
    });
  }

  /**
   * The setup step's honesty check: one real synthesis and one real
   * transcription of assets already on disk. No network, no microphone, and
   * safe on a machine with no audio hardware at all.
   */
  async function selfTest(): Promise<{ stt: boolean; tts: boolean }> {
    let tts = false;
    let stt = false;
    let probe: string | undefined;
    try {
      probe = (await synthesize("Good morning.", { key: `selftest-${process.pid}` })).audioPath;
      tts = true;
    } catch {
      tts = false;
    }
    try {
      // Silence, not the synthesized clip: the STT check must prove the model
      // loads and the binary runs, not that Kokoro is intelligible enough for
      // Whisper — coupling the two would make one failure look like both.
      await transcribe(Buffer.alloc(16_000 * 2, 0));
      stt = true;
    } catch {
      stt = false;
    }
    if (probe !== undefined) await fs.rm(probe, { force: true }).catch(() => {});
    return { stt, tts };
  }

  function childEnv(): NodeJS.ProcessEnv {
    // A deliberately bare environment: the children need none of the parent's
    // variables, and passing them along is how a stray PYTHONPATH or DYLD_*
    // from a customer's shell ends up changing which library gets loaded.
    const env: NodeJS.ProcessEnv = { PATH: process.env.PATH ?? "" };
    if (process.platform === "win32") {
      env.SystemRoot = process.env.SystemRoot ?? "C:\\Windows";
      env.TEMP = process.env.TEMP ?? "";
    }
    return env;
  }

  function killAll(): void {
    generation += 1;
    for (const handle of [...live]) {
      try {
        handle.kill();
      } catch {
        // Already gone; nothing to do.
      }
    }
    live.clear();
  }

  return {
    transcribe,
    synthesize,
    selfTest,
    cancel: killAll,
    dispose(): void {
      disposed = true;
      killAll();
    },
    get liveChildren(): number {
      return live.size;
    },
  };
}

/**
 * A zero exit is not proof of audio. Validate the RIFF/WAVE container and a
 * non-empty data chunk before anything downstream treats the file as a clip —
 * a truncated WAV cached as a brief would replay as silence every morning.
 */
export async function assertPlayableWav(filePath: string): Promise<void> {
  const handle = await fs.open(filePath, "r").catch(() => null);
  if (handle === null) throw new Error("the speech engine produced no audio file");
  try {
    const stat = await handle.stat();
    if (stat.size < 44) throw new Error("the speech engine produced a truncated audio file");

    const header = Buffer.alloc(12);
    await handle.read(header, 0, 12, 0);
    if (header.subarray(0, 4).toString("ascii") !== "RIFF" || header.subarray(8, 12).toString("ascii") !== "WAVE") {
      throw new Error("the speech engine produced a file that is not a WAV");
    }
    // RIFF size counts everything after the first 8 bytes. A short file with a
    // large declared size is the signature of a killed writer.
    if (header.readUInt32LE(4) + 8 > stat.size) {
      throw new Error("the speech engine produced an incomplete WAV");
    }

    // Actually walk the chunk list and find `data` with content in it. The
    // previous version only checked that the file was longer than a header,
    // which a WAV carrying an empty data chunk (or a stray trailing chunk)
    // passes while playing as pure silence.
    let offset = 12;
    const chunk = Buffer.alloc(8);
    while (offset + 8 <= stat.size) {
      await handle.read(chunk, 0, 8, offset);
      const id = chunk.subarray(0, 4).toString("ascii");
      const size = chunk.readUInt32LE(4);
      if (id === "data") {
        if (size === 0) throw new Error("the speech engine produced a WAV with no audio");
        if (offset + 8 + size > stat.size) throw new Error("the speech engine produced an incomplete WAV");
        // A well-formed WAV of pure zeros is not speech. The macOS CI runner
        // produced exactly that — 11 seconds of silence, exit code 0, a valid
        // header — and both this check and the setup step's self-test called it
        // success, which is how a customer ends up with a JARVIS that "speaks"
        // and cannot be heard (CI run 31687161739).
        if (!(await hasSound(handle, offset + 8, Math.min(size, stat.size - offset - 8)))) {
          throw new Error("the speech engine produced silence");
        }
        return;
      }
      // Chunks are word-aligned.
      offset += 8 + size + (size % 2);
    }
    throw new Error("the speech engine produced a WAV with no audio");
  } finally {
    await handle.close().catch(() => {});
  }
}

/**
 * Is there any actual signal in this PCM?
 *
 * Deliberately a low bar: any sample above ~0.2% of full scale, anywhere in the
 * clip, counts. This is not a quality gate (the audition gate is), it is the
 * difference between audio and nothing at all — and it reads a bounded window
 * rather than the whole file so a long clip costs nothing.
 */
async function hasSound(handle: FileHandle, dataOffset: number, dataSize: number): Promise<boolean> {
  const window = Math.min(dataSize, 1_048_576);
  if (window < 2) return false;
  const buffer = Buffer.alloc(window);
  await handle.read(buffer, 0, window, dataOffset);
  const threshold = 64; // int16 full scale is 32768.
  for (let index = 0; index + 1 < buffer.length; index += 2) {
    if (Math.abs(buffer.readInt16LE(index)) > threshold) return true;
  }
  // A long clip whose FIRST megabyte is silence may still speak later (a long
  // leading pause), so the tail gets a look too before we call it silent.
  if (dataSize > window) {
    const tailAt = dataOffset + dataSize - window;
    await handle.read(buffer, 0, window, tailAt);
    for (let index = 0; index + 1 < buffer.length; index += 2) {
      if (Math.abs(buffer.readInt16LE(index)) > threshold) return true;
    }
  }
  return false;
}

/**
 * whisper-cli prints the transcript on stdout and its own chatter on stderr.
 * Empty clips still yield bracketed non-speech markers like "[BLANK_AUDIO]" or
 * "(silence)", which must never be spoken back or sent to the model as if the
 * customer had said them.
 */
export function cleanTranscript(stdout: string): string {
  const text = stdout
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "")
    .join(" ")
    .replace(/\[[^\]]*\]/g, " ")
    .replace(/\((?:silence|blank_audio|inaudible|music|applause)\)/gi, " ")
    .replace(/\s+/g, " ")
    .trim();
  // A transcript of nothing but punctuation is the other silence tell.
  return /[a-z0-9]/i.test(text) ? text : "";
}

/** Minimal 16 kHz mono PCM16 RIFF header. whisper.cpp accepts nothing else. */
export function wavFromPcm16(pcm: Buffer, sampleRate = 16_000): Buffer {
  const header = Buffer.alloc(44);
  header.write("RIFF", 0, "ascii");
  header.writeUInt32LE(36 + pcm.length, 4);
  header.write("WAVE", 8, "ascii");
  header.write("fmt ", 12, "ascii");
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20); // PCM
  header.writeUInt16LE(1, 22); // mono
  header.writeUInt32LE(sampleRate, 24);
  header.writeUInt32LE(sampleRate * 2, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36, "ascii");
  header.writeUInt32LE(pcm.length, 40);
  return Buffer.concat([header, pcm]);
}

const defaultTimer = (fn: () => void, ms: number): { clear: () => void } => {
  const handle = setTimeout(fn, ms);
  // A pending voice watchdog must never be the reason the process stays alive.
  handle.unref?.();
  return { clear: () => clearTimeout(handle) };
};

const defaultSpawn: SpawnChild = (file, args, options) => {
  const child: ChildProcess = spawn(file, args, {
    cwd: options.cwd,
    env: options.env,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });

  let stdout = "";
  let stderr = "";
  // Bounded: a runaway child must not be able to grow the main process's heap.
  const cap = 8 * 1024 * 1024;
  child.stdout?.on("data", (chunk: Buffer) => {
    if (stdout.length < cap) stdout += chunk.toString("utf8");
  });
  child.stderr?.on("data", (chunk: Buffer) => {
    if (stderr.length < cap) stderr += chunk.toString("utf8");
  });

  let killTimer: NodeJS.Timeout | undefined;
  const done = new Promise<{ code: number; stdout: string; stderr: string }>((resolve) => {
    child.on("error", () => resolve({ code: -1, stdout, stderr }));
    child.on("close", (code, signal) => {
      if (killTimer !== undefined) clearTimeout(killTimer);
      // A killed child has no exit code; report it as a failure rather than
      // letting `null` coerce into a success further up.
      resolve({ code: code ?? (signal === null ? -1 : 143), stdout, stderr });
    });
  });

  return {
    done,
    kill: () => {
      if (child.exitCode !== null || child.signalCode !== null || child.killed) return;
      try {
        child.kill("SIGTERM");
      } catch {
        return;
      }
      // Escalate if it ignores the polite request. Unreffed so a shutdown is
      // never delayed waiting for this.
      killTimer = setTimeout(() => {
        try {
          child.kill("SIGKILL");
        } catch {
          // Already reaped.
        }
      }, KILL_GRACE_MS);
      killTimer.unref?.();
    },
  };
};
