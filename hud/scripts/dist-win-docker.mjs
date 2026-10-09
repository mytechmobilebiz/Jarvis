#!/usr/bin/env node
/**
 * dist-win-docker.mjs — build the Windows installer on this Mac, in a container.
 *
 * v1.4.0 moved Windows packaging off GitHub Actions (the free tier was
 * exhausted in a day; a full matrix run cost $1.21-1.65). Everything the
 * Windows build needs is now producible here:
 *
 *   - the win-x64 engine payload: built natively on macOS, because
 *     build-vendor-payload.mjs evaluates PEP 508 markers itself instead of
 *     letting pip evaluate them against the host;
 *   - the voice payload: pinned upstream downloads plus the frozen,
 *     hash-pinned whisper-cli.exe in hud/vendor/voice/win32;
 *   - the arch verifier: it parses PE bytes, so it runs anywhere.
 *
 * Only the NSIS installer itself needs Windows tooling, and electron-builder
 * builds it under wine — hence this container. The image is amd64 and runs
 * under emulation on Apple Silicon, so expect it to be several times slower
 * than a native runner. That is the trade Peter chose, with the cost stated.
 *
 * WHAT THIS DOES NOT GIVE US: no win-x64 binary is EXECUTED anywhere. The
 * offline install, the watchdog's kill behaviour and the voice binaries are
 * proven by a manual smoke of the shipped installer on real Windows — see
 * spec/v140/RELEASE-CHECKLIST.md, where that manual run is a release gate.
 *
 *   node hud/scripts/dist-win-docker.mjs [--image <ref>] [--keep-container]
 */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HUD_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const REPO_ROOT = path.resolve(HUD_DIR, "..");
const DEFAULT_IMAGE = "electronuserland/builder:wine";

function fail(message, hint) {
  console.error(`[dist-win-docker] FAIL: ${message}`);
  if (hint !== undefined) console.error(`[dist-win-docker] ${hint}`);
  process.exit(1);
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return undefined;
  }
}

/**
 * THE SERIALIZATION GUARD.
 *
 * `hud/vendor/engine` and `hud/voice-assets` are single, platform-specific
 * directories, and so is the ASAR trust anchor beside them. Building Windows
 * while a macOS payload is staged would silently package the wrong engine and
 * the wrong anchor — an app that rejects its own payload on a customer's
 * machine, with a message about a damaged download. The checklist rule is
 * "serialize the builds"; this is that rule, enforced rather than remembered.
 */
function assertWindowsPayloadStaged() {
  const manifest = readJson(path.join(HUD_DIR, "vendor", "engine", "vendor-manifest.json"));
  if (manifest === undefined) {
    fail(
      "no engine payload is staged at hud/vendor/engine",
      "Build one: node hud/scripts/build-vendor-payload.mjs --platform win32 --arch x64 --clean",
    );
  }
  if (manifest.platform !== "win32" || manifest.arch !== "x64") {
    fail(
      `the staged engine payload is ${manifest.platform}/${manifest.arch}, not win32/x64`,
      "Windows and macOS share one staging directory. Stage the win-x64 payload first — and never "
      + "while a macOS build is packaging.",
    );
  }

  const voice = readJson(path.join(HUD_DIR, "voice-assets", "manifest.json"));
  if (voice === undefined) fail("no voice payload is staged at hud/voice-assets");
  if (voice.generatedFor !== "win32") {
    fail(
      `the staged voice payload was generated for ${voice.generatedFor}, not win32`,
      "Stage it: node hud/scripts/fetch-voice-assets.mjs --platform=win32",
    );
  }

  const anchor = readJson(path.join(HUD_DIR, "build", "voice-manifest-digest.json"));
  if (anchor?.generatedFor !== "win32") {
    fail(
      `the ASAR trust anchor is for ${anchor?.generatedFor ?? "nothing"}, not win32`,
      "It is one shared filename for both platforms (v1.4.1 splits it). Restage the win32 voice "
      + "payload so the anchor matches what will be packed.",
    );
  }
  console.log("[dist-win-docker] staged for win32: engine payload, voice payload, trust anchor.");
}

function run(command, args, options = {}) {
  console.log(`[dist-win-docker] $ ${command} ${args.join(" ")}`);
  const result = spawnSync(command, args, { stdio: "inherit", ...options });
  if (result.error !== undefined) fail(`${command} could not be started: ${result.error.message}`);
  if (result.status !== 0) fail(`${command} exited with status ${result.status}`);
}

function main() {
  const argv = process.argv.slice(2);
  const KNOWN = new Set(["--image", "--keep-container"]);
  for (const argument of argv) {
    if (argument.startsWith("--") && !KNOWN.has(argument)) fail(`unknown argument: ${argument}`);
  }
  const image = argv.includes("--image") ? argv[argv.indexOf("--image") + 1] : DEFAULT_IMAGE;

  assertWindowsPayloadStaged();

  // Verify the payload BEFORE a slow emulated build, exactly as dist:win does.
  run(process.execPath, [
    path.join(HUD_DIR, "scripts", "verify-vendor-payload.mjs"),
    "--release", "--platform", "win32", "--arch", "x64",
  ]);
  run(process.execPath, [
    path.join(HUD_DIR, "scripts", "fetch-voice-assets.mjs"),
    "--check", "--platform=win32",
  ]);

  // The wizard-audio gate, same as dist:win: this direct-driving script must
  // reproduce EVERY gate, and the first omission would have shipped 1.9 KB
  // placeholder tones as JARVIS's voice (Codex re-review 2026-08-14, F5).
  run(process.execPath, [path.join(HUD_DIR, "scripts", "verify-wizard-audio.mjs")]);

  // THE P0 GATE. This script drives electron-builder directly, so it does not
  // inherit `dist:win`'s steps — and the first time that mattered, the wizard
  // smoke (added because a bare import shipped a blank first-run window) would
  // have been skipped on every Windows build. Run it here, before the slow
  // emulated packaging, and again against the packed app below.
  run(process.execPath, [
    path.join(REPO_ROOT, "node_modules", ".bin", "electron"),
    path.join(HUD_DIR, "scripts", "wizard-window-smoke.cjs"),
  ], { cwd: HUD_DIR });

  // The repo is mounted at the SAME absolute path it has on the host, so pnpm's
  // node_modules symlinks resolve inside the container without reinstalling.
  const containerArgs = [
    "run", "--rm",
    "--platform", "linux/amd64",
    "-v", `${REPO_ROOT}:${REPO_ROOT}`,
    "-w", path.join(REPO_ROOT, "hud"),
    // electron-builder caches the Electron zip and its tools here; keeping it
    // in a named volume means the next build does not re-download 200 MB.
    "-v", "jarvis-electron-builder-cache:/root/.cache/electron-builder",
    "-v", "jarvis-electron-cache:/root/.cache/electron",
    "-e", "CSC_IDENTITY_AUTO_DISCOVERY=false",
    image,
    "npx", "--no-install", "electron-builder", "--win", "--x64",
  ];
  run("docker", containerArgs);

  // And verify what came out, on the host, with the same gates the mac cut uses.
  run(process.execPath, [path.join(HUD_DIR, "scripts", "verify-win-artifact.mjs")]);
  run(process.execPath, [
    path.join(HUD_DIR, "scripts", "verify-vendor-payload.mjs"),
    "--release", "--platform", "win32", "--arch", "x64",
    "--packed", path.join(HUD_DIR, "dist-app", "win-unpacked", "resources"),
  ]);
  // The voice tree inside the packed app. This is the check that caught
  // electron-builder expanding `${platform}` to the HOST platform rather than
  // the target: cross-building Windows from Linux looked for
  // voice-assets/bin/linux, logged one "file source doesn't exist" line, and
  // shipped an installer with NO voice binaries at all.
  run(process.execPath, [
    path.join(HUD_DIR, "scripts", "fetch-voice-assets.mjs"),
    "--check",
    `--out=${path.join(HUD_DIR, "dist-app", "win-unpacked", "resources", "voice")}`,
  ]);

  // And the pages, in what electron-builder actually produced — a packaging
  // filter that drops a renderer module ships a blank window exactly like a bad
  // import does, and only the packed run can see that.
  run(process.execPath, [
    path.join(REPO_ROOT, "node_modules", ".bin", "electron"),
    path.join(HUD_DIR, "scripts", "wizard-window-smoke.cjs"),
    "--packed", path.join(HUD_DIR, "dist-app", "win-unpacked", "resources"),
  ], { cwd: HUD_DIR });

  console.log("[dist-win-docker] OK — installer built and verified. It is UNSIGNED; the manual "
    + "Windows smoke in spec/v140/RELEASE-CHECKLIST.md is the gate that replaces CI execution.");
}

main();
