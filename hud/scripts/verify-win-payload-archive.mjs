#!/usr/bin/env node
/**
 * Proves the Windows installer's PACKED payload can actually be UNPACKED.
 *
 * Why this exists (2026-08-18, v1.5.2): the shipped v1.5.2 installer contained
 * one member whose compressed stream was damaged —
 * `python/Lib/site-packages/pip/_internal/utils/__pycache__/setuptools_build.cpython-312.pyc`.
 * NSIS could not write that file on any customer machine, so every Windows
 * install failed at the engine step with `payload-corrupt` /
 * `file-missing` / count 1. It reached production because EVERY gate we had
 * looked somewhere else:
 *
 *   - `verify-vendor-payload --packed` re-hashes `dist-app/win-unpacked`, the
 *     STAGING tree, before electron-builder compresses it into `app-64.7z`.
 *   - `verify-win-artifact` opens `app-64.7z` but extracts exactly ONE member
 *     from it (`JARVIS HUD.exe`) to read its PE header. That member was fine.
 *   - The ASAR string-probes read a different container entirely.
 *
 * Every one of those was green on a broken installer. The lesson, and the rule
 * this script enforces: **a gate that reads one member of a container has not
 * verified the container.**
 *
 * What it does, on the FINAL artifact — the same file a customer downloads:
 *   1. Pulls `app-64.7z` out of the NSIS installer.
 *   2. Runs a FULL `7z t` over it, so every member is decompressed and
 *      CRC-checked. One bad block anywhere fails the build.
 *   3. Extracts the engine payload back out of the archive and hands it to
 *      `verify-vendor-payload --packed`, which re-checks the digest chain the
 *      customer's installer will check — from the bytes that actually shipped,
 *      not from the directory they were built out of.
 *
 * Usage:
 *   node scripts/verify-win-payload-archive.mjs
 *   node scripts/verify-win-payload-archive.mjs --installer <path-to-exe>
 */

import { access, mkdtemp, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { findSevenZip, runSevenZip } from "./lib/seven-zip.mjs";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const distDir = resolve(scriptDir, "..", "dist-app");
const payloadName = "app-64.7z";

function isTargetInstaller(name) {
  return (
    /win-x64\.exe$/i.test(name) ||
    /^JARVIS HUD Setup .+\.exe$/i.test(name)
  );
}

async function findFilesNamed(root, expectedName) {
  let entries;

  try {
    entries = await readdir(root, { withFileTypes: true });
  } catch (error) {
    if (error?.code === "ENOENT") {
      return [];
    }
    throw error;
  }

  const found = [];

  for (const entry of entries) {
    const full = join(root, entry.name);
    if (entry.isDirectory()) {
      found.push(...await findFilesNamed(full, expectedName));
    } else if (entry.isFile() && entry.name === expectedName) {
      found.push(full);
    }
  }

  return found;
}

/**
 * A full decompression test. `runSevenZip` throws on a non-zero exit, which is
 * what a data error produces — but 7-Zip also prints its verdict, and a run
 * that somehow exits 0 without saying "Everything is Ok" is not a pass we are
 * willing to infer.
 */
function assertArchiveFullyDecompresses(sevenZip, archivePath, label) {
  const output = runSevenZip(
    sevenZip,
    ["t", archivePath],
    `${label} failed its decompression test`,
  );

  if (!/Everything is Ok/i.test(output)) {
    const tail = output.trim().split(/\r?\n/).slice(-8).join("\n");
    throw new Error(
      `${label} did not report a clean decompression test.\n${tail}`,
    );
  }

  const members = /Files:\s+(\d+)/i.exec(output)?.[1];
  return members ? Number(members) : undefined;
}

function verifyExtractedPayload(resourcesDir) {
  const verifier = join(scriptDir, "verify-vendor-payload.mjs");
  const result = spawnSync(
    process.execPath,
    [
      verifier,
      "--release",
      "--platform",
      "win32",
      "--arch",
      "x64",
      "--packed",
      resourcesDir,
    ],
    { encoding: "utf8", maxBuffer: 10 * 1024 * 1024 },
  );

  const output = [result.stdout, result.stderr].filter(Boolean).join("\n").trim();

  if (result.error || result.status !== 0) {
    throw new Error(
      `the engine payload EXTRACTED FROM THE INSTALLER failed verification.\n${output}`,
    );
  }

  return output;
}

async function verifyInstaller(sevenZip, installerPath) {
  const workDir = await mkdtemp(join(tmpdir(), "jarvis-win-payload-"));
  const archiveDir = join(workDir, "archive");
  const payloadDir = join(workDir, "payload");

  try {
    runSevenZip(
      sevenZip,
      ["x", "-y", `-o${archiveDir}`, installerPath, `*${payloadName}`, "-r"],
      `could not open ${basename(installerPath)}`,
    );

    const archives = await findFilesNamed(archiveDir, payloadName);
    // electron-builder picks the layout: usually an inner app-64.7z, but a
    // NSIS that embeds the app directly is equally valid. Either way the
    // assertion is the same — every byte the installer carries must come back
    // out — so the container we test is whichever one holds the app.
    const container = archives.length === 1 ? archives[0] : installerPath;
    const label = archives.length === 1
      ? `${basename(installerPath)} → ${payloadName}`
      : basename(installerPath);

    if (archives.length > 1) {
      throw new Error(
        `${basename(installerPath)} contains ${archives.length} ${payloadName} files; `
          + "expected exactly one.",
      );
    }

    const members = assertArchiveFullyDecompresses(sevenZip, container, label);
    console.log(
      `[verify-win-payload-archive] ${label}: full decompression test OK`
        + `${members === undefined ? "" : ` (${members} members)`}`,
    );

    runSevenZip(
      sevenZip,
      [
        "x",
        "-y",
        `-o${payloadDir}`,
        container,
        "resources/engine-vendor/*",
        "resources/app.asar.unpacked/node_modules/sherpa-onnx-win-x64/*",
        "-r",
      ],
      `could not extract the engine payload from ${label}`,
    );

    for (const file of ["sherpa-onnx.node", "onnxruntime.dll", "onnxruntime_providers_shared.dll", "sherpa-onnx-c-api.dll", "sherpa-onnx-cxx-api.dll"]) {
      await access(join(payloadDir, "resources", "app.asar.unpacked", "node_modules", "sherpa-onnx-win-x64", file));
    }
    const output = verifyExtractedPayload(join(payloadDir, "resources"));
    console.log(`[verify-win-payload-archive] ${output}`);
    console.log(
      `[verify-win-payload-archive] OK — ${basename(installerPath)} unpacks completely `
        + "and its engine payload matches the digest chain the customer's installer checks.",
    );
  } finally {
    await rm(workDir, { recursive: true, force: true });
  }
}

/**
 * Did the build that produced these artifacts FINISH?
 *
 * The v1.5.2 win build was interrupted — its host-side `limactl shell` client
 * was killed and the run never completed normally. That is not the data path
 * the corruption travelled (the damage is a lost partial-page writeback on the
 * QEMU 9p shared mount, independent of the SSH channel), but the abnormal
 * finish was VISIBLE and nobody looked: that build left no `.exe.blockmap`, no
 * `builder-debug.yml`, and an orphaned intermediate `@jarvishud-*.nsis.7z`
 * behind. v1.5.1's and v1.5.3's have both files and no leftover.
 *
 * So: three free assertions that would have flagged the bad cut minutes after
 * it finished. They say nothing about payload integrity — that is the full
 * decompression test above — only that we are looking at the output of a
 * completed build rather than the wreckage of a stopped one.
 */
async function assertBuildLooksComplete(root, installers) {
  const entries = await readdir(root, { withFileTypes: true });
  const names = new Set(entries.filter((entry) => entry.isFile()).map((entry) => entry.name));
  const problems = [];

  for (const installer of installers) {
    const blockmap = `${basename(installer)}.blockmap`;
    if (!names.has(blockmap)) {
      problems.push(`${blockmap} is missing — electron-builder writes it LAST, so the build did not finish`);
    }
  }

  if (!names.has("builder-debug.yml")) {
    problems.push("builder-debug.yml is missing — the build did not reach its final step");
  }

  const leftovers = [...names].filter((name) => /^@jarvishud-.*\.nsis\.7z$/i.test(name));
  if (leftovers.length > 0) {
    problems.push(
      `an intermediate archive was left behind (${leftovers.join(", ")}) — a completed build removes it`,
    );
  }

  if (problems.length > 0) {
    throw new Error(
      `the Windows build in ${root} did not complete normally:\n  - ${problems.join("\n  - ")}\n`
        + "Re-run the build. Do NOT ship an artifact from an interrupted run.",
    );
  }

  console.log("[verify-win-payload-archive] build completeness OK (blockmap + builder-debug.yml, no leftover intermediate)");
}

async function main() {
  const argv = process.argv.slice(2);
  const explicit = argv.includes("--installer")
    ? argv[argv.indexOf("--installer") + 1]
    : undefined;

  let installers;

  if (explicit) {
    installers = [resolve(explicit)];
  } else {
    let entries;
    try {
      entries = await readdir(distDir, { withFileTypes: true });
    } catch (error) {
      if (error?.code === "ENOENT") {
        throw new Error(
          `${distDir} does not exist — build the Windows installer first.`,
        );
      }
      throw error;
    }

    installers = entries
      .filter((entry) => entry.isFile() && isTargetInstaller(entry.name))
      .map((entry) => join(distDir, entry.name));
  }

  if (installers.length === 0) {
    throw new Error(
      `no Windows installer found in ${distDir}; build one first.`,
    );
  }

  // Only when checking a fresh build tree — with an explicit --installer the
  // artifact is typically a staged copy, whose directory says nothing about
  // the build that made it.
  if (!explicit) {
    await assertBuildLooksComplete(distDir, installers);
  }

  const sevenZip = await findSevenZip();

  for (const installer of installers) {
    await verifyInstaller(sevenZip, installer);
  }
}

main().catch((error) => {
  console.error(`[verify-win-payload-archive] ${error.message}`);
  process.exit(1);
});
