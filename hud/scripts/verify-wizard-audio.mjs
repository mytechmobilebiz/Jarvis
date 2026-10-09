#!/usr/bin/env node
/**
 * Release gate for the wizard's baked voice cues (Codex re-review 2026-08-14,
 * F4 — release blocker): the mechanism shipped with PLACEHOLDER tones (0.35 s
 * sine waves, ~1.9 KB each) standing in for the real ElevenLabs recordings.
 * A paying customer must never hear a beep where the log prints a sentence —
 * so a dist build REFUSES to run while any catalog asset is still a
 * placeholder.
 *
 * The check is catalog-driven (WIZARD_AUDIO_CUES from the built renderer
 * module — the same source of truth the wizard plays from), against a CLOSED
 * moment set: file present, no symlink, unique by identity, a validated MPEG
 * Layer-III frame chain, and at least MIN_REAL_CLIP_BYTES. The shipped
 * recordings are MP3 mono 44.1 kHz 128 kbps (~16 KB/s, 4.4-6.3 s ≈ 70-102 KB);
 * the floor stays at 4 KB — comfortably above the ~1.9 KB placeholder tones
 * while agnostic to a future re-encode at a leaner bitrate.
 *
 * Dev builds and tests are unaffected — this runs only in the dist:* chains.
 * `--placeholders-ok` exists for deliberately unshippable CI/dev exercises.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HUD_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const ASSET_DIR = path.join(HUD_DIR, "src", "wizard-audio");
const MIN_REAL_CLIP_BYTES = 4096;

const placeholdersOk = process.argv.includes("--placeholders-ok");

/**
 * The CLOSED moment set (Codex re-review 2026-08-14, F3): an empty or
 * partial catalog must not pass just because every entry it still HAS is
 * fine — a moment silently vanishing from the catalog is exactly the rot
 * this gate exists to catch. Adding a moment is a conscious act: update
 * this list in the same commit as the catalog entry and its call site.
 */
const EXPECTED_MOMENTS = [
  "greeting.morning",
  "greeting.afternoon",
  "greeting.evening",
  "greeting.rerun",
  "migrate.found",
  "engine.install",
  "model.idle",
  "model.verified",
  "voice.handoff",
  "setup.complete",
];

const { WIZARD_AUDIO_CUES } = await import(
  new URL("../dist/wizard-audio.js", import.meta.url).href
);

const problems = [];

const actualMoments = Object.keys(WIZARD_AUDIO_CUES);
for (const expected of EXPECTED_MOMENTS) {
  if (!actualMoments.includes(expected)) {
    problems.push(`${expected}: missing from the catalog — a wizard moment has silently lost its voice`);
  }
}
for (const actual of actualMoments) {
  if (!EXPECTED_MOMENTS.includes(actual)) {
    problems.push(
      `${actual}: in the catalog but not in this gate's expected set — `
      + "add it to EXPECTED_MOMENTS in the same commit as its call site",
    );
  }
}

/**
 * A real MP3 must contain a CHAIN of valid MPEG audio frames — not merely a
 * two-byte sync pattern somewhere (second re-review 2026-08-14, F1: any
 * FF Ex pair in a 2048-byte window passed, which random garbage contains
 * with ~63% probability). Each frame header is validated field by field
 * (version, layer, bitrate, sample rate) and its exact length computed.
 * Every counted frame must be FULLY PRESENT in the file — round-3 review,
 * F1 held: "the file may end inside the frame" accepted a bare 4-byte
 * header at EOF with zero audio payload, which ffprobe rejects — and at
 * least MIN_FRAME_CHAIN consecutive agreeing frames must be proven (the
 * 4 KB size floor guarantees far more in any real recording). Planting
 * that by accident is not a thing.
 */
const MIN_FRAME_CHAIN = 3;
const MP3_BITRATES_V1L3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320];
const MP3_BITRATES_V2L3 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160];
const MP3_RATES = { 3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000] };

/** Parse a Layer-III frame header at `at`; returns {length, version, rate} or undefined. */
function parseFrame(bytes, at) {
  if (at + 4 > bytes.length) return undefined;
  if (bytes[at] !== 0xff || (bytes[at + 1] & 0xe0) !== 0xe0) return undefined;
  const version = (bytes[at + 1] >> 3) & 0x03; // 3=MPEG1, 2=MPEG2, 0=MPEG2.5, 1=reserved
  const layer = (bytes[at + 1] >> 1) & 0x03; // 1 = Layer III
  if (version === 1 || layer !== 1) return undefined;
  const bitrateIndex = (bytes[at + 2] >> 4) & 0x0f;
  const rateIndex = (bytes[at + 2] >> 2) & 0x03;
  if (bitrateIndex === 0 || bitrateIndex === 0x0f || rateIndex === 3) return undefined;
  const bitrate = (version === 3 ? MP3_BITRATES_V1L3 : MP3_BITRATES_V2L3)[bitrateIndex] * 1000;
  const rate = MP3_RATES[version][rateIndex];
  const padding = (bytes[at + 2] >> 1) & 0x01;
  const perFrame = version === 3 ? 144 : 72;
  const length = Math.floor((perFrame * bitrate) / rate) + padding;
  if (length < 24) return undefined;
  return { length, version, rate };
}

function mp3Problem(file) {
  const bytes = fs.readFileSync(file);
  if (bytes.length < 128) return "too small to hold any audio frame";
  let at = 0;
  if (bytes.toString("latin1", 0, 3) === "ID3") {
    if (bytes.length < 10) return "truncated ID3 header";
    // ID3v2 syncsafe size: 4 × 7-bit bytes, tag body follows the 10-byte header.
    const tagSize = ((bytes[6] & 0x7f) << 21) | ((bytes[7] & 0x7f) << 14) | ((bytes[8] & 0x7f) << 7) | (bytes[9] & 0x7f);
    at = 10 + tagSize;
  }
  // Tolerate a little encoder slack after the tag, but the frame found there
  // must parse strictly — a bare sync pattern no longer counts.
  const limit = Math.min(bytes.length - 4, at + 2048);
  let first;
  while (at <= limit) {
    first = parseFrame(bytes, at);
    if (first !== undefined) break;
    at += 1;
  }
  if (first === undefined) return "no valid MPEG Layer-III frame where the audio must start";
  let frames = 0;
  let pos = at;
  while (frames < MIN_FRAME_CHAIN) {
    const frame = parseFrame(bytes, pos);
    if (frame === undefined) {
      return `frame ${frames + 1} does not parse where frame ${frames} ends — not a real MP3 stream`;
    }
    if (frame.version !== first.version || frame.rate !== first.rate) {
      return `frame ${frames + 1} disagrees with the first on version/sample-rate — not one stream`;
    }
    if (pos + frame.length > bytes.length) {
      return `the file ends inside frame ${frames + 1} — a truncated header is not audio`;
    }
    frames += 1;
    pos += frame.length;
    if (pos >= bytes.length) {
      // The stream ends exactly on a frame boundary before the chain quota —
      // only possible under the size floor with huge frames; still refuse,
      // a real recording at any sane bitrate has hundreds of frames.
      if (frames < MIN_FRAME_CHAIN) return `only ${frames} complete audio frame(s) — not a real recording`;
    }
  }
  return undefined;
}

// The audio dir itself must be a real directory: if it (or any parent a
// catalog path passes through) is a symlink, every per-file check below is
// examining files that live somewhere else than the tree the build packs
// (round-3 review, F1 held: only the FINAL path component was
// symlink-checked — a symlinked parent directory escaped containment).
if (fs.lstatSync(ASSET_DIR).isSymbolicLink()) {
  console.error(`!! ${ASSET_DIR} is a symlink — the recordings must live in the repo itself`);
  process.exit(1);
}
const REAL_ASSET_DIR = fs.realpathSync(ASSET_DIR);

const seenFiles = new Map();
for (const [moment, cue] of Object.entries(WIZARD_AUDIO_CUES)) {
  const file = path.resolve(ASSET_DIR, cue.file);
  // Containment: a catalog entry must not reach outside the audio dir
  // ("../something.mp3" passing this gate would verify a file the ASAR
  // does not even ship).
  if (!file.startsWith(ASSET_DIR + path.sep)) {
    problems.push(`${moment}: ${cue.file} escapes src/wizard-audio/ — refused`);
    continue;
  }
  let lst;
  try {
    lst = fs.lstatSync(file);
  } catch {
    problems.push(`${moment}: ${cue.file} is missing from src/wizard-audio/`);
    continue;
  }
  // No symlinks: a lexical containment check is meaningless for a link, and
  // the ASAR would pack the link target anyway (second re-review, F1).
  if (lst.isSymbolicLink()) {
    problems.push(`${moment}: ${cue.file} is a symlink — the recording itself must live here`);
    continue;
  }
  // Realpath containment: the file's TRUE identity must resolve inside the
  // audio dir, so an intermediate symlinked directory in the catalog path
  // cannot smuggle a file from elsewhere past the lexical check (round-3 F1).
  if (!fs.realpathSync(file).startsWith(REAL_ASSET_DIR + path.sep)) {
    problems.push(`${moment}: ${cue.file} resolves outside src/wizard-audio/ (symlinked parent) — refused`);
    continue;
  }
  // Uniqueness by IDENTITY (dev:ino), so hardlinks to one recording cannot
  // masquerade as distinct clips: two moments sharing one recording means
  // one of them plays the wrong words over its transcript.
  const identity = `${lst.dev}:${lst.ino}`;
  const priorMoment = seenFiles.get(identity);
  if (priorMoment !== undefined) {
    problems.push(`${moment}: shares its recording with ${priorMoment} — every moment gets its own`);
  }
  seenFiles.set(identity, moment);
  const bytes = fs.statSync(file).size;
  if (bytes < MIN_REAL_CLIP_BYTES) {
    problems.push(
      `${moment}: ${cue.file} is ${bytes} bytes — a placeholder tone, not a recording `
      + `(real clips at the documented format are ≥ ${MIN_REAL_CLIP_BYTES} bytes)`,
    );
  } else {
    const badMp3 = mp3Problem(file);
    if (badMp3 !== undefined) problems.push(`${moment}: ${cue.file} is not playable MP3 audio (${badMp3})`);
  }
  if (cue.transcript.trim() === "") {
    problems.push(`${moment}: has no transcript — voice-only information is not allowed`);
  }
}

/**
 * The setup→HUD birth's visual assets (TRANSITION-DESIGN.md §14.10) ride the
 * same refuse-to-ship posture: a missing or placeholder-sized orb means the
 * customer's one birth moment plays against a black hole. Floors are set
 * comfortably under the real assets (718 KB / 41 KB / 17 KB) while far above
 * any plausible placeholder.
 */
const TRANSITION_DIR = path.join(HUD_DIR, "src", "transition");
const TRANSITION_ASSETS = [
  { file: "orb.mp4", minBytes: 400 * 1024, role: "the living plasma core" },
  { file: "poster-orb.jpg", minBytes: 20 * 1024, role: "the poster / reduced-motion orb" },
  { file: "wake.mp3", minBytes: 8 * 1024, role: "the detonation surge" },
];
for (const asset of TRANSITION_ASSETS) {
  const file = path.join(TRANSITION_DIR, asset.file);
  let lst;
  try {
    lst = fs.lstatSync(file);
  } catch {
    problems.push(`transition/${asset.file}: missing from src/transition/ — ${asset.role} would be a hole in the birth`);
    continue;
  }
  if (lst.isSymbolicLink()) {
    problems.push(`transition/${asset.file}: is a symlink — the asset itself must live here`);
    continue;
  }
  if (lst.size < asset.minBytes) {
    problems.push(
      `transition/${asset.file}: ${lst.size} bytes — a placeholder, not ${asset.role} `
      + `(the real asset is ≥ ${asset.minBytes} bytes)`,
    );
  }
}

if (problems.length === 0) {
  console.log(`wizard-audio: ${Object.keys(WIZARD_AUDIO_CUES).length} cues present, all real recordings`);
  console.log(`transition: ${TRANSITION_ASSETS.length} birth assets present at real sizes`);
  process.exit(0);
}

for (const problem of problems) console.error(`!! ${problem}`);
if (placeholdersOk) {
  console.error("!! --placeholders-ok: continuing anyway — THIS BUILD IS NOT SHIPPABLE");
  process.exit(0);
}
console.error(
  "\nBUILD REFUSED: the wizard would ship placeholder assets to a paying customer.\n"
  + "Recordings live in hud/src/wizard-audio/ (same filenames, transcripts updated in\n"
  + "hud/src/wizard-audio.ts); the birth's orb/poster/surge live in hud/src/transition/\n"
  + "(copied from landing/assets per TRANSITION-DESIGN.md §6). Restore them and rebuild.",
);
process.exit(1);
