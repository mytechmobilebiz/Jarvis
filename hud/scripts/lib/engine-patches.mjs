/**
 * Applies the JARVIS engine patch series (hud/vendor/engine-patches/) to a
 * STAGING copy of the pinned Hermes source at vendoring time.
 *
 * Contract (see engine-patches/README.md):
 *  - never applied in place — the caller hands us its own staging tree;
 *  - `files/` additions are copied first and must be NEW paths (an addition
 *    that would overwrite an upstream file is a series bug, not a merge);
 *  - diffs then apply in `series` order via `git apply`, checked first, so a
 *    drifted engine bump fails the BUILD loudly instead of shipping a
 *    half-patched engine;
 *  - the returned digest goes into the vendor manifest so a payload records
 *    exactly which patch series produced it.
 */

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

/** Read the ordered patch list. Blank lines and #-comments are ignored. */
export function readSeries(patchesDir) {
  const seriesPath = path.join(patchesDir, "series");
  if (!fs.existsSync(seriesPath)) {
    throw new Error(`Engine patch series not found: ${seriesPath}`);
  }
  return fs
    .readFileSync(seriesPath, "utf8")
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "" && !line.startsWith("#"));
}

/** sha256 over the series file, every patch, and every files/ addition —
 *  stable across platforms (posix-relative sorted paths). */
export function seriesDigest(patchesDir) {
  const hash = createHash("sha256");
  const entries = [["series", fs.readFileSync(path.join(patchesDir, "series"))]];
  for (const name of readSeries(patchesDir)) {
    entries.push([name, fs.readFileSync(path.join(patchesDir, name))]);
  }
  const filesRoot = path.join(patchesDir, "files");
  if (fs.existsSync(filesRoot)) {
    for (const rel of listFiles(filesRoot).sort()) {
      entries.push([`files/${rel}`, fs.readFileSync(path.join(filesRoot, rel))]);
    }
  }
  for (const [name, data] of entries) {
    hash.update(name);
    hash.update("\0");
    hash.update(data);
    hash.update("\0");
  }
  return hash.digest("hex");
}

/**
 * Copy files/ additions + apply the diff series onto engineTree.
 * Returns { patches, filesAdded, digest }.
 */
export function applyEnginePatches(patchesDir, engineTree, { log = () => {} } = {}) {
  const series = readSeries(patchesDir);

  // 1. New-file additions.
  const filesRoot = path.join(patchesDir, "files");
  let added = [];
  if (fs.existsSync(filesRoot)) {
    for (const rel of listFiles(filesRoot).sort()) {
      const target = path.join(engineTree, rel);
      if (fs.existsSync(target)) {
        throw new Error(
          `Engine patch addition would overwrite an upstream file: ${rel}. ` +
          `Additions must be new paths; changes to existing files belong in a .patch.`,
        );
      }
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.copyFileSync(path.join(filesRoot, rel), target);
      added.push(rel);
    }
  }
  log(`  engine patches: added ${added.length} new files`);

  // 2. Diffs, checked before applied so nothing lands half-way.
  for (const name of series) {
    const patchPath = path.join(patchesDir, name);
    if (!fs.existsSync(patchPath)) {
      throw new Error(`Series names a missing patch: ${name}`);
    }
    const args = ["apply", "--whitespace=nowarn", patchPath];
    try {
      execFileSync("git", ["apply", "--check", "--whitespace=nowarn", patchPath], {
        cwd: engineTree,
        stdio: ["ignore", "pipe", "pipe"],
      });
    } catch (err) {
      throw new Error(
        `Engine patch ${name} does not apply cleanly to this engine source. ` +
        `The pinned engine likely moved under the patch series — regenerate the ` +
        `patch before shipping (never ship a half-patched engine).\n` +
        `${err.stderr ? err.stderr.toString() : err.message}`,
      );
    }
    execFileSync("git", args, { cwd: engineTree, stdio: ["ignore", "pipe", "pipe"] });
    log(`  engine patches: applied ${name}`);
  }

  return { patches: series.length, filesAdded: added.length, digest: seriesDigest(patchesDir) };
}

function listFiles(root, prefix = "") {
  const out = [];
  for (const entry of fs.readdirSync(path.join(root, prefix), { withFileTypes: true })) {
    const rel = prefix === "" ? entry.name : `${prefix}/${entry.name}`;
    if (entry.isDirectory()) out.push(...listFiles(root, rel));
    else if (entry.isFile()) out.push(rel);
  }
  return out;
}
