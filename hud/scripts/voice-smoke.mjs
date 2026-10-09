#!/usr/bin/env node
/**
 * End-to-end voice smoke test against the STAGED payload.
 *
 * This is the only check that runs the shipped speech binaries for real, so it
 * is what stands in for "we tested it on Windows": CI runs it on
 * windows-latest, which is the only place the win32 executables ever execute
 * before a customer's machine does.
 *
 * It is also how the latency numbers in the Lane C report were measured, so it
 * prints timings rather than only a pass/fail.
 *
 *   node hud/scripts/voice-smoke.mjs            # verify + measure
 *   node hud/scripts/voice-smoke.mjs --repeat=5
 *   node hud/scripts/voice-smoke.mjs --auditions=<dir>   # voice-lock set
 *
 * It never asks for a microphone or a speaker: speech is synthesized to WAV,
 * then fed back through recognition. That runs headless in CI and on a machine
 * with no audio hardware at all.
 */

import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HUD_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const VOICE_ROOT = path.join(HUD_ROOT, "voice-assets");

/**
 * The Codex listening list from spec/v140/voice-codex-report.md: the utterance
 * shapes small TTS models fail on, rather than a sentence that happens to
 * sound fine.
 */
const AUDITION_LINES = [
  ["greeting", "Good morning, sir. You have three priorities today."],
  ["numbers", "The Henderson invoice is four thousand two hundred pounds, due on the fourteenth of August."],
  ["names", "Kevin Dunn from Aldridge & Coe called; I have booked him in with Sarah at eleven."],
  ["ack-short", "Right away, sir."],
  ["ack-medium", "Consider it done."],
  ["reply", "Revenue is up nine percent on last month, though two of those invoices are still unpaid. I would chase Aldridge first — they are the larger of the two and they have paid late twice this quarter."],
  ["bad-news", "I am afraid the Aldridge contract has been cancelled. They gave no reason, and the deposit is non-refundable."],
  ["urgent", "Sir, the payment gateway has been down for eleven minutes. Nothing is being collected."],
  ["brief", "Good morning. Revenue stands at twelve thousand four hundred, against a target of thirty thousand. Three invoices are overdue. Sarah has two calls booked, and the quarterly filing is due on Friday. Nothing else needs you today."],
];

/**
 * Long enough to recognise, and made of words a small English model handles
 * well. The first CI macOS run failed on a one-line utterance whose keywords a
 * cold int8 model did not recover; the assertion is unchanged in KIND — every
 * keyword must still survive the round trip — but the sample it is asked about
 * is a fair one.
 */
const OPENER = "Good morning, sir. You have three priorities today. "
  + "The first is the release, the second is the customer email, and the third is lunch.";

/** Content words, not filler: each must survive recognition. */
const KEYWORDS = ["morning", "priorities", "release", "customer", "lunch"];

/** Peak and RMS of a 16-bit PCM buffer, normalised to 0..1. */
function levelOf(pcm) {
  let peak = 0;
  let sum = 0;
  const samples = pcm.length / 2;
  for (let index = 0; index < samples; index += 1) {
    const value = pcm.readInt16LE(index * 2) / 32_768;
    peak = Math.max(peak, Math.abs(value));
    sum += value * value;
  }
  return { peak, rms: samples === 0 ? 0 : Math.sqrt(sum / samples) };
}

async function main() {
  const args = process.argv.slice(2);
  const repeat = Number(readFlag(args, "--repeat") ?? 1);
  const auditions = readFlag(args, "--auditions");

  const { readVoiceManifest } = await import("@jarvis/setup/voice-assets");
  const { createVoiceEngine, resolveVoicePaths } = await import("@jarvis/setup/voice-engine");

  const manifest = await readVoiceManifest(VOICE_ROOT);
  if (manifest === null) {
    throw new Error(`No voice manifest at ${VOICE_ROOT}. Run: node hud/scripts/fetch-voice-assets.mjs`);
  }

  const cacheDir = await fs.mkdtemp(path.join(os.tmpdir(), "jarvis-voice-smoke-"));
  const engine = createVoiceEngine({
    paths: resolveVoicePaths(VOICE_ROOT, process.platform),
    manifest,
    cacheDir,
  });

  console.log(`platform ${process.platform}/${process.arch}, ${os.cpus().length} cpus`);
  console.log(`stt ${manifest.stt.engine} ${manifest.stt.version} / ${manifest.stt.model}`);
  console.log(`tts ${manifest.tts.engine} ${manifest.tts.version} / ${manifest.tts.voice} (sid ${manifest.tts.speakerId})\n`);

  if (auditions !== undefined) {
    await renderAuditions(engine, manifest, auditions);
    return;
  }

  let failures = 0;
  const ttsTimes = [];
  const sttTimes = [];


  for (let round = 1; round <= repeat; round += 1) {
    // Time to first word is what a listener perceives, so the opener is timed
    // separately from the long reply.
    const opener = await engine.synthesize(OPENER, { key: `smoke-open-${round}` });
    ttsTimes.push(opener.ms);
    const audio = await fs.stat(opener.audioPath);
    if (audio.size < 1_000) {
      console.error("FAIL: synthesis produced a suspiciously small file");
      failures += 1;
    }

    // Round-trip: recognise our own speech. Resampled 24 kHz -> 16 kHz here
    // because whisper.cpp takes nothing else.
    const pcm = await pcm16From(opener.audioPath);
    // What the recognizer was actually GIVEN. A silent or clipped buffer and a
    // wrong transcript look identical in the failure message otherwise, and on
    // a CI runner nobody can listen to the file (first macOS CI run heard only
    // "you", the classic whisper output for silence).
    const level = levelOf(pcm);
    console.log(
      `  round ${round}: ${(pcm.length / 2 / 16_000).toFixed(2)}s of audio, `
      + `peak ${level.peak.toFixed(3)}, rms ${level.rms.toFixed(4)}`,
    );
    if (level.rms < 0.005) {
      console.error(`FAIL: synthesis produced (near) silence — rms ${level.rms.toFixed(5)}`);
      failures += 1;
    }
    const heard = await engine.transcribe(pcm);
    sttTimes.push(heard.ms);
    const normalised = heard.text.toLowerCase().replace(/[^a-z ]/g, "");
    // Every KEYWORD must survive, and they are the content words of the
    // sentence rather than its filler — the assertion is the same shape as
    // before, on an utterance long enough for a recognizer to work with.
    const missing = KEYWORDS.filter((word) => !normalised.includes(word));
    if (missing.length > 0) {
      console.error(`FAIL: round trip lost ${missing.join(", ")}: "${heard.text}"`);
      failures += 1;
    }
    console.log(`round ${round}: tts ${opener.ms} ms, stt ${heard.ms} ms — "${heard.text}"`);
  }

  console.log(`\ntts median ${median(ttsTimes)} ms  (min ${Math.min(...ttsTimes)}, max ${Math.max(...ttsTimes)})`);
  console.log(`stt median ${median(sttTimes)} ms  (min ${Math.min(...sttTimes)}, max ${Math.max(...sttTimes)})`);

  const selfTest = await engine.selfTest();
  console.log(`selfTest stt=${selfTest.stt} tts=${selfTest.tts}`);
  if (!selfTest.stt || !selfTest.tts) failures += 1;

  await fs.rm(cacheDir, { recursive: true, force: true });
  if (failures > 0) {
    console.error(`\n${failures} voice smoke check(s) failed`);
    process.exitCode = 1;
    return;
  }
  console.log("\nvoice smoke: OK");
}

/**
 * Renders the voice-lock audition set for both candidate voices, so the choice
 * between bm_george and bm_fable is made by listening rather than by reading a
 * grade in a table.
 */
async function renderAuditions(engine, manifest, outDir) {
  const voices = [["bm_george", manifest.tts.speakerId], ["bm_fable", 25]];
  await fs.mkdir(outDir, { recursive: true });
  for (const [name, speakerId] of voices) {
    for (const [label, line] of AUDITION_LINES) {
      const key = `${name}-${label}`;
      const result = await engine.synthesize(line, { speakerId, key });
      await fs.copyFile(result.audioPath, path.join(outDir, `${key}.wav`));
      console.log(`  ${key}.wav  (${result.ms} ms)`);
    }
  }
  console.log(`\nAuditions written to ${outDir}`);
}

/** Minimal WAV reader + linear resampler to 16 kHz mono PCM16. */
async function pcm16From(wavPath) {
  const buffer = await fs.readFile(wavPath);
  let offset = 12;
  let sampleRate = 16_000;
  let channels = 1;
  let bits = 16;
  let data = null;
  while (offset + 8 <= buffer.length) {
    const id = buffer.subarray(offset, offset + 4).toString("ascii");
    const size = buffer.readUInt32LE(offset + 4);
    if (id === "fmt ") {
      channels = buffer.readUInt16LE(offset + 10);
      sampleRate = buffer.readUInt32LE(offset + 12);
      bits = buffer.readUInt16LE(offset + 22);
    } else if (id === "data") {
      data = buffer.subarray(offset + 8, offset + 8 + size);
      break;
    }
    offset += 8 + size + (size % 2);
  }
  if (data === null || bits !== 16) throw new Error(`Unsupported WAV at ${wavPath}`);

  const frames = data.length / 2 / channels;
  const mono = new Int16Array(frames);
  for (let i = 0; i < frames; i += 1) {
    let sum = 0;
    for (let c = 0; c < channels; c += 1) sum += data.readInt16LE((i * channels + c) * 2);
    mono[i] = Math.round(sum / channels);
  }
  if (sampleRate === 16_000) return Buffer.from(mono.buffer, mono.byteOffset, mono.byteLength);

  const ratio = sampleRate / 16_000;
  const out = new Int16Array(Math.floor(frames / ratio));
  for (let i = 0; i < out.length; i += 1) {
    const source = i * ratio;
    const low = Math.floor(source);
    const frac = source - low;
    const next = Math.min(low + 1, frames - 1);
    out[i] = Math.round(mono[low] * (1 - frac) + mono[next] * frac);
  }
  return Buffer.from(out.buffer, out.byteOffset, out.byteLength);
}

function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.floor(sorted.length / 2)];
}

function readFlag(args, name) {
  const hit = args.find((arg) => arg.startsWith(`${name}=`));
  return hit === undefined ? undefined : hit.slice(name.length + 1);
}

main().catch((error) => {
  console.error(`voice smoke: ${error.message}`);
  process.exitCode = 1;
});
