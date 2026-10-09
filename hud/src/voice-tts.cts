/** Warm Kokoro host. Only main's verified asset paths cross this boundary. */
import { cpus } from "node:os";
import type { VoicePaths } from "@jarvis/setup/voice-engine";
import type { PcmAudio } from "./voice-types.js";

// Upstream publishes JSDoc, but no TypeScript declarations.
const { OfflineTts } = require("sherpa-onnx-node") as {
  OfflineTts: new (config: unknown) => {
    generateAsync(request: { text: string; sid: number; speed: number; enableExternalBuffer: boolean; onProgress?: () => number }): Promise<PcmAudio>;
  };
};
let engine: InstanceType<typeof OfflineTts>;
let sid = 26;
let generation = 0;
let running = false;
const jobs: Array<() => Promise<void>> = [];
let phrases: string[] = [];
const port = process.parentPort!;

function generate(text: string, mine?: number): Promise<PcmAudio> {
  return engine.generateAsync({ text, sid, speed: 1, enableExternalBuffer: false,
    onProgress: () => mine === undefined || mine === generation ? 1 : 0 });
}

/** Live jobs take priority over acknowledgement renders that have not started. */
async function pump(): Promise<void> {
  if (running) return;
  const job = jobs.shift();
  const phrase = job ? undefined : phrases.shift();
  if (!job && phrase === undefined) return;
  running = true;
  try {
    if (job) await job();
    else port.postMessage({ type: "ack", text: phrase, audio: await generate(phrase!) });
  } catch (error) {
    if (job) port.postMessage({ type: "failed", error: String(error) });
    else console.warn("[voice] acknowledgement synthesis failed", error);
  } finally {
    running = false;
    setImmediate(() => void pump());
  }
}

port.on("message", ({ data }: { data: { type: string; paths: VoicePaths; speakerId: number; id: number; text: string; phrases: string[] } }) => {
  if (data.type === "cancel") { generation++; return; }
  const mine = generation;
  jobs.push(async () => {
    if (data.type === "init") {
      const p = data.paths;
      sid = data.speakerId;
      const started = performance.now();
      engine = new OfflineTts({ model: { kokoro: {
        model: p.kokoroModel, voices: p.kokoroVoices, tokens: p.kokoroTokens,
        lexicon: p.kokoroLexicon, dataDir: p.espeakData, lang: "en", lengthScale: 1.05,
      }, numThreads: Math.min(4, Math.max(2, cpus().length - 1)), provider: "cpu", debug: 0 }, maxNumSentences: 1 });
      const loadMs = performance.now() - started;
      const warmStart = performance.now();
      const audio = await generate("Good morning, I am ready to help.");
      const warmMs = performance.now() - warmStart;
      const audioMs = audio.samples.length / audio.sampleRate * 1000;
      port.postMessage({ type: "warm", loadMs, warmMs, audioMs });
      if (audioMs <= 0 || warmMs > audioMs) return;
      phrases = [...data.phrases];
      return;
    }
    if (data.type === "speak" && mine === generation) {
      try {
        const audio = await generate(data.text, mine);
        if (mine === generation) port.postMessage({ type: "audio", id: data.id, audio });
      } catch (error) {
        port.postMessage({ type: "error", id: data.id, error: String(error) });
      }
    }
  });
  void pump();
});
