#!/usr/bin/env node
/**
 * voice-tts-probe.mjs — why is this machine's speech silent?
 *
 * The macOS CI runner produces a well-formed WAV of pure zeros while the same
 * verified payload speaks correctly on Windows CI and on the build Mac. sherpa
 * reports success ("Saved ... successfully!") and even a plausible duration, so
 * nothing in the normal path can say WHICH input is being mis-handled.
 *
 * This runs the shipped TTS binary directly, several ways, and prints the level
 * of what each produced. It is a DIAGNOSTIC: it never fails the build, it just
 * makes the next CI log answer the question.
 *
 *   node hud/scripts/voice-tts-probe.mjs
 */
import { execFileSync, spawnSync } from "node:child_process";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HUD_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const VOICE_ROOT = process.env.JARVIS_VOICE_ROOT ?? path.join(HUD_ROOT, "voice-assets");
const TEXT = "Good morning, sir. You have three priorities today.";

const binDir = path.join(VOICE_ROOT, "bin", process.platform === "win32" ? "win32" : "darwin");
const ttsBin = path.join(binDir, process.platform === "win32" ? "sherpa-onnx-offline-tts.exe" : "sherpa-onnx-offline-tts");
const kokoro = path.join(VOICE_ROOT, "models", "kokoro");

const base = {
  model: path.join(kokoro, "model.onnx"),
  voices: path.join(kokoro, "voices.bin"),
  tokens: path.join(kokoro, "tokens.txt"),
  lexicon: path.join(kokoro, "lexicon-gb-en.txt"),
  dataDir: path.join(kokoro, "espeak-ng-data"),
};

/** Peak/RMS of a 16-bit WAV's payload, ignoring the 44-byte header. */
async function levelOf(file) {
  const buffer = await fs.readFile(file).catch(() => null);
  if (buffer === null || buffer.length <= 44) return { bytes: 0, peak: 0, rms: 0 };
  let peak = 0;
  let sum = 0;
  const samples = Math.floor((buffer.length - 44) / 2);
  for (let index = 0; index < samples; index += 1) {
    const value = buffer.readInt16LE(44 + index * 2) / 32_768;
    peak = Math.max(peak, Math.abs(value));
    sum += value * value;
  }
  return { bytes: buffer.length, peak, rms: samples === 0 ? 0 : Math.sqrt(sum / samples) };
}

async function run(label, extraArgs) {
  const out = path.join(await fs.mkdtemp(path.join(os.tmpdir(), "jarvis-tts-probe-")), "probe.wav");
  const args = [
    `--kokoro-model=${base.model}`,
    `--kokoro-voices=${base.voices}`,
    `--kokoro-tokens=${base.tokens}`,
    ...extraArgs,
    "--sid=26",
    `--output-filename=${out}`,
    TEXT,
  ];
  const started = Date.now();
  const result = spawnSync(ttsBin, args, { encoding: "utf8" });
  const level = await levelOf(out);
  const said = `${result.stdout ?? ""}${result.stderr ?? ""}`
    .split("\n")
    .filter((line) => /duration|Elapsed|RTF|error|Error|warn|Warn|lexicon|espeak|token/i.test(line))
    .slice(0, 4)
    .join(" | ");
  console.log(
    `${label.padEnd(28)} exit=${result.status} bytes=${String(level.bytes).padStart(8)} `
    + `peak=${level.peak.toFixed(3)} rms=${level.rms.toFixed(5)} ${Date.now() - started}ms`,
  );
  if (said !== "") console.log(`${" ".repeat(30)}${said}`);
  await fs.rm(path.dirname(out), { recursive: true, force: true });
}

async function main() {
  console.log(`probe: ${process.platform}/${process.arch}, ${os.cpus().length} cpus, node ${process.version}`);
  if (process.platform === "darwin") {
    for (const tool of [["lipo", ["-archs", ttsBin]], ["file", [ttsBin]]]) {
      try {
        console.log(`  ${tool[0]}: ${execFileSync(tool[0], tool[1], { encoding: "utf8" }).trim()}`);
      } catch {
        // Diagnostics only.
      }
    }
  }
  for (const [name, size] of Object.entries(base)) {
    const stat = await fs.stat(size).catch(() => null);
    console.log(`  ${name.padEnd(8)} ${stat === null ? "MISSING" : `${stat.isDirectory() ? "dir" : `${stat.size} bytes`}`} ${size}`);
  }

  // The combination the product ships, then each input removed in turn: the one
  // whose absence changes the answer is the one being mis-handled.
  await run("as shipped", [`--kokoro-lexicon=${base.lexicon}`, `--kokoro-data-dir=${base.dataDir}`, "--kokoro-lang=en", "--num-threads=1"]);
  await run("no lexicon", [`--kokoro-data-dir=${base.dataDir}`, "--kokoro-lang=en", "--num-threads=1"]);
  await run("no data-dir", [`--kokoro-lexicon=${base.lexicon}`, "--kokoro-lang=en", "--num-threads=1"]);
  await run("no lang", [`--kokoro-lexicon=${base.lexicon}`, `--kokoro-data-dir=${base.dataDir}`, "--num-threads=1"]);
  await run("threads=2", [`--kokoro-lexicon=${base.lexicon}`, `--kokoro-data-dir=${base.dataDir}`, "--kokoro-lang=en", "--num-threads=2"]);
  await run("sid=25 (bm_fable)", [`--kokoro-lexicon=${base.lexicon}`, `--kokoro-data-dir=${base.dataDir}`, "--kokoro-lang=en", "--num-threads=1", "--sid=25"].filter((arg) => !arg.startsWith("--sid=26")));
}

main().catch((error) => {
  console.error(`probe failed: ${error.message}`);
});
