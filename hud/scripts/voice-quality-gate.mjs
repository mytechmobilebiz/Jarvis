#!/usr/bin/env node
/**
 * Speech quality gate for the shipped Kokoro voice.
 *
 * We ship `kokoro-multi-lang-v1_0` (fp32). This checks the staged model
 * for intelligibility and signal integrity.
 *
 * "Sounds fine" is not a check anyone can run in CI, so this measures two
 * things that correlate with the failure modes that actually matter:
 *
 *  1. **Intelligibility** — every line is re-recognised with the same
 *     whisper.cpp model JARVIS ships, and scored as word error rate against
 *     the text that was synthesized. Model regressions show up here first
 *     as slurred or dropped words, especially on names and numbers.
 *  2. **Signal integrity** — clipping, silence and level, which is where synthesis
 *     artefacts (buzzing, dropouts, DC offset) show up even when the words
 *     are still recognisable.
 *
 * Run against the shipped model or an alternative to get a comparison:
 *
 *   node hud/scripts/voice-quality-gate.mjs                       # shipped fp32
 *   node hud/scripts/voice-quality-gate.mjs --model=/path/model.onnx --label=candidate
 *
 * It reports numbers and a verdict; it does not replace Peter listening to
 * spec/v140/voice-auditions/. Ears decide whether the voice is RIGHT; this
 * detects speech regressions.
 */

import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HUD_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const VOICE_ROOT = path.join(HUD_ROOT, "voice-assets");

/** The Codex listening list: the shapes small TTS models fail on. */
const LINES = [
  ["greeting", "Good morning, sir. You have three priorities today."],
  ["numbers", "The Henderson invoice is four thousand two hundred pounds, due on the fourteenth of August."],
  ["names", "Kevin Dunn from Aldridge and Coe called; I have booked him in with Sarah at eleven."],
  ["ack-short", "Right away, sir."],
  ["ack-medium", "Consider it done."],
  ["reply", "Revenue is up nine percent on last month, though two of those invoices are still unpaid."],
  ["bad-news", "I am afraid the Aldridge contract has been cancelled. They gave no reason."],
  ["urgent", "Sir, the payment gateway has been down for eleven minutes. Nothing is being collected."],
];

/** Above this, intelligibility has degraded enough to escalate. */
const WER_ESCALATE = 0.10;
/** Any clipping at all on a synthetic voice is an artefact, not headroom. */
const CLIP_ESCALATE = 0.001;

async function main() {
  const args = process.argv.slice(2);
  const label = readFlag(args, "--label") ?? "fp32 (shipped)";
  const modelOverride = readFlag(args, "--model");
  const voicesOverride = readFlag(args, "--voices");
  const keep = readFlag(args, "--out");

  const { readVoiceManifest } = await import("@jarvis/setup/voice-assets");
  const { createVoiceEngine, resolveVoicePaths } = await import("@jarvis/setup/voice-engine");

  const manifest = await readVoiceManifest(VOICE_ROOT);
  if (manifest === null) throw new Error(`No voice manifest at ${VOICE_ROOT}.`);

  const paths = resolveVoicePaths(VOICE_ROOT, process.platform);
  if (modelOverride !== undefined) paths.kokoroModel = modelOverride;
  if (voicesOverride !== undefined) paths.kokoroVoices = voicesOverride;

  const cacheDir = await fs.mkdtemp(path.join(os.tmpdir(), "jarvis-voice-quality-"));
  const engine = createVoiceEngine({ paths, manifest, cacheDir });
  if (keep !== undefined) await fs.mkdir(keep, { recursive: true });

  console.log(`\nKokoro quality gate — ${label}`);
  console.log(`model  ${path.basename(paths.kokoroModel)}`);
  console.log(`voice  ${manifest.tts.voice} (sid ${manifest.tts.speakerId})\n`);
  console.log("line          WER    clip%   peak   rms    dur     synth");
  console.log("-".repeat(66));

  let totalErrors = 0;
  let totalWords = 0;
  let worstClip = 0;
  const rows = [];

  for (const [name, text] of LINES) {
    const spoken = await engine.synthesize(text, { key: `q-${name}` });
    const wav = await readWav(spoken.audioPath);
    const heard = await engine.transcribe(toPcm16(wav));

    const { errors, words } = wordErrors(text, heard.text);
    totalErrors += errors;
    totalWords += words;
    worstClip = Math.max(worstClip, wav.clipRatio);
    rows.push({ name, wer: errors / words, heard: heard.text, text });

    console.log(
      `${name.padEnd(12)} ${pct(errors / words).padStart(5)}  ${(wav.clipRatio * 100).toFixed(3).padStart(6)}`
      + `  ${wav.peak.toFixed(2).padStart(5)}  ${wav.rms.toFixed(3).padStart(5)}`
      + `  ${wav.seconds.toFixed(2).padStart(5)}s  ${String(spoken.ms).padStart(5)}ms`,
    );
    if (keep !== undefined) await fs.copyFile(spoken.audioPath, path.join(keep, `${name}.wav`));
  }

  const overallWer = totalErrors / totalWords;
  console.log("-".repeat(66));
  console.log(`overall WER ${pct(overallWer)} (${totalErrors}/${totalWords} words), worst clipping ${(worstClip * 100).toFixed(3)}%`);

  const misheard = rows.filter((row) => row.wer > 0);
  if (misheard.length > 0) {
    console.log("\nlines that did not survive recognition:");
    for (const row of misheard) {
      console.log(`  ${row.name}\n    said:  ${row.text}\n    heard: ${row.heard}`);
    }
  }

  const problems = [];
  if (overallWer > WER_ESCALATE) problems.push(`WER ${pct(overallWer)} exceeds the ${pct(WER_ESCALATE)} escalation threshold`);
  if (worstClip > CLIP_ESCALATE) problems.push(`clipping ${(worstClip * 100).toFixed(3)}% exceeds ${(CLIP_ESCALATE * 100).toFixed(3)}%`);

  console.log("");
  if (problems.length > 0) {
    console.log(`VERDICT [${label}]: ESCALATE — ${problems.join("; ")}`);
    process.exitCode = 1;
  } else {
    console.log(`VERDICT [${label}]: intelligible and clean by these measures.`);
    console.log("This does not judge whether the voice is RIGHT — that needs ears on spec/v140/voice-auditions/.");
  }
  await fs.rm(cacheDir, { recursive: true, force: true });
}

const NUMBER_WORDS = {
  zero: 0, one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9,
  ten: 10, eleven: 11, twelve: 12, thirteen: 13, fourteen: 14, fifteen: 15, sixteen: 16,
  seventeen: 17, eighteen: 18, nineteen: 19, twenty: 20, thirty: 30, forty: 40, fifty: 50,
  sixty: 60, seventy: 70, eighty: 80, ninety: 90,
};
const MULTIPLIERS = { hundred: 100, thousand: 1_000, million: 1_000_000 };
/** Ordinals the brief and diary lines actually use. */
const ORDINALS = {
  first: "1", second: "2", third: "3", fourth: "4", fifth: "5", sixth: "6", seventh: "7",
  eighth: "8", ninth: "9", tenth: "10", eleventh: "11", twelfth: "12", thirteenth: "13",
  fourteenth: "14", fifteenth: "15", twentieth: "20", thirtieth: "30",
};

/**
 * Whisper writes "£4,200" where Kokoro was given "four thousand two hundred
 * pounds", and "9%" for "nine percent". Scoring those as errors would measure
 * the recogniser's orthography, not whether the SPEECH was intelligible —
 * which is the only thing this gate exists to judge. Both sides are therefore
 * reduced to the same canonical form first: symbols become words, and number
 * words become digits.
 */
function canonicalWords(text) {
  const flat = text
    .toLowerCase()
    .replace(/[£]/g, " pounds ")
    .replace(/[$]/g, " dollars ")
    .replace(/[€]/g, " euros ")
    .replace(/%/g, " percent ")
    .replace(/&/g, " and ")
    .replace(/(\d),(\d)/g, "$1$2")
    .replace(/(\d+)(st|nd|rd|th)\b/g, "$1")
    .replace(/[^a-z0-9 ]/g, " ")
    .split(/\s+/)
    .filter(Boolean);

  const out = [];
  let pending = 0;
  let total = 0;
  let sawNumber = false;
  const flush = () => {
    if (!sawNumber) return;
    out.push(String(total + pending));
    pending = 0;
    total = 0;
    sawNumber = false;
  };

  for (const word of flat) {
    if (word === "and" && sawNumber) continue; // "four thousand AND two hundred"
    if (word in ORDINALS) {
      flush();
      out.push(ORDINALS[word]);
      continue;
    }
    if (word in NUMBER_WORDS) {
      pending += NUMBER_WORDS[word];
      sawNumber = true;
      continue;
    }
    if (word in MULTIPLIERS) {
      const factor = MULTIPLIERS[word];
      if (factor === 100) pending = (pending || 1) * 100;
      else {
        total += (pending || 1) * factor;
        pending = 0;
      }
      sawNumber = true;
      continue;
    }
    flush();
    out.push(word);
  }
  flush();
  return out;
}

/** Levenshtein word error rate over the canonical forms. */
function wordErrors(reference, hypothesis) {
  const ref = canonicalWords(reference);
  const hyp = canonicalWords(hypothesis);
  const distance = Array.from({ length: ref.length + 1 }, (_, i) => [i, ...Array(hyp.length).fill(0)]);
  for (let j = 0; j <= hyp.length; j += 1) distance[0][j] = j;
  for (let i = 1; i <= ref.length; i += 1) {
    for (let j = 1; j <= hyp.length; j += 1) {
      distance[i][j] = ref[i - 1] === hyp[j - 1]
        ? distance[i - 1][j - 1]
        : 1 + Math.min(distance[i - 1][j - 1], distance[i - 1][j], distance[i][j - 1]);
    }
  }
  return { errors: distance[ref.length][hyp.length], words: ref.length };
}

async function readWav(wavPath) {
  const buffer = await fs.readFile(wavPath);
  let offset = 12;
  let sampleRate = 24_000;
  let channels = 1;
  let data = null;
  while (offset + 8 <= buffer.length) {
    const id = buffer.subarray(offset, offset + 4).toString("ascii");
    const size = buffer.readUInt32LE(offset + 4);
    if (id === "fmt ") {
      channels = buffer.readUInt16LE(offset + 10);
      sampleRate = buffer.readUInt32LE(offset + 12);
    } else if (id === "data") {
      data = buffer.subarray(offset + 8, offset + 8 + size);
      break;
    }
    offset += 8 + size + (size % 2);
  }
  if (data === null) throw new Error(`Unreadable WAV: ${wavPath}`);

  const frames = Math.floor(data.length / 2 / channels);
  const samples = new Int16Array(frames);
  let clipped = 0;
  let sumSquares = 0;
  let peak = 0;
  for (let i = 0; i < frames; i += 1) {
    let sum = 0;
    for (let c = 0; c < channels; c += 1) sum += data.readInt16LE((i * channels + c) * 2);
    const sample = Math.round(sum / channels);
    samples[i] = sample;
    const magnitude = Math.abs(sample) / 32_768;
    if (magnitude >= 0.999) clipped += 1;
    if (magnitude > peak) peak = magnitude;
    sumSquares += magnitude * magnitude;
  }
  return {
    samples,
    sampleRate,
    seconds: frames / sampleRate,
    clipRatio: frames === 0 ? 0 : clipped / frames,
    peak,
    rms: Math.sqrt(sumSquares / Math.max(frames, 1)),
  };
}

function toPcm16(wav) {
  if (wav.sampleRate === 16_000) return Buffer.from(wav.samples.buffer, wav.samples.byteOffset, wav.samples.byteLength);
  const ratio = wav.sampleRate / 16_000;
  const out = new Int16Array(Math.floor(wav.samples.length / ratio));
  for (let i = 0; i < out.length; i += 1) {
    const source = i * ratio;
    const low = Math.floor(source);
    const frac = source - low;
    const next = Math.min(low + 1, wav.samples.length - 1);
    out[i] = Math.round(wav.samples[low] * (1 - frac) + wav.samples[next] * frac);
  }
  return Buffer.from(out.buffer, out.byteOffset, out.byteLength);
}

const pct = (value) => `${(value * 100).toFixed(1)}%`;

function readFlag(args, name) {
  const hit = args.find((arg) => arg.startsWith(`${name}=`));
  return hit === undefined ? undefined : hit.slice(name.length + 1);
}

main().catch((error) => {
  console.error(`voice quality gate: ${error.message}`);
  process.exitCode = 1;
});
