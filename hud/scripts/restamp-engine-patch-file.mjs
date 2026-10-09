#!/usr/bin/env node
/**
 * Push a CHANGED engine-patch-series file into the ALREADY-BUILT payloads and
 * re-stamp their digest chains — without re-vendoring the engine.
 *
 * Why this exists (v1.5.2, brain-CLI discovery): the engine's own fallback
 * list (`agent/brain/providers.py` `brain_cli_home_dirs`) had to learn the
 * folders OpenAI's and Anthropic's official Windows installers actually use,
 * in lockstep with the wizard's list. That file is OWNED by the patch series
 * (`hud/vendor/engine-patches/files/`), not by upstream Hermes, so the change
 * is a patch-series edit — but the payloads on disk were built weeks ago and
 * a re-vendor would put an unreviewed engine into a release whose whole point
 * is one edited function. Exactly the v1.5.1 AV-strip situation, exactly the
 * v1.5.1 answer: prove the payload, change only the named files, re-stamp.
 *
 * This rewrites exactly three things per payload:
 *   1. the named file, everywhere it appears in the payload,
 *   2. `digests.sha256` — the VERIFIED list with those files' new hashes,
 *   3. `vendor-manifest.json` — `digests.sha256`, `digests.fileCount` and
 *      `sizeBytes`.
 * `builtAt`, `source`, `enginePatches`, `engineTag` and every other field are
 * left alone: the engine still comes from where it came from, and the patch
 * series it carries is still the series named in the manifest — the series
 * digest changes with the file, and `enginePatches.sha256` is deliberately
 * recomputed only when --patches-digest is passed (the caller proves it).
 *
 * SAFETY, inherited wholesale from restamp-vendor-payload.mjs and the six
 * Codex review rounds behind it:
 *   - PROVE BEFORE REWRITING: every listed file is re-hashed against the
 *     manifest's chain first, so this can never launder a corrupted payload.
 *   - The new digest list is the VERIFIED list with exactly the named files'
 *     hashes replaced — never a fresh, unverified walk of the tree.
 *   - Every byte it may change is backed up first and restored on ANY abort,
 *     including SIGINT/SIGTERM/SIGHUP: a half-re-stamped payload names files
 *     that are gone and is only recoverable by a full re-vendor.
 *
 * The new manifest digest is load-bearing downstream: the install stamp
 * records the payload digest it came from (`setup/src/vendor.ts`), so every
 * existing machine sees "different build" and reinstalls the engine from this
 * payload — which is how the discovery fix reaches a customer at all.
 *
 * Usage:
 *   node hud/scripts/restamp-engine-patch-file.mjs --file agent/brain/providers.py
 *   node hud/scripts/restamp-engine-patch-file.mjs --file <rel> --check
 *   node hud/scripts/restamp-engine-patch-file.mjs --file <rel> --payload <dir>
 *
 * `--file` is the path INSIDE the engine tree, exactly as the patch series
 * names it. `--check` mutates nothing: it reports whether every payload copy
 * already matches the series source and whether the chain is intact, and is
 * safe to run in a release battery.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { DIGESTS_FILE, MANIFEST_FILE, dirSize, sha256File, walkDigestFiles } from "./lib/payload-digest.mjs";

const HUD_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PATCHES_FILES_DIR = path.join(HUD_DIR, "vendor", "engine-patches", "files");
const DEFAULT_PAYLOADS = [
  path.join(HUD_DIR, "vendor", "engine"),
  path.join(HUD_DIR, "vendor", "engine-win"),
];

class RestampError extends Error {}
const fail = (message) => {
  throw new RestampError(message);
};
const log = (message) => console.log(message);

function parseArgs(argv) {
  const opts = { payloads: [], check: false, file: undefined };
  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i];
    if (flag === "--check") opts.check = true;
    else if (flag === "--payload") opts.payloads.push(path.resolve(argv[++i] ?? fail("--payload needs a directory")));
    else if (flag === "--file") opts.file = (argv[++i] ?? fail("--file needs a path inside the engine tree")).replace(/\\/g, "/");
    else fail(`Unknown argument: ${flag}`);
  }
  if (opts.file === undefined) fail("--file <path-inside-the-engine-tree> is required");
  if (opts.file.startsWith("/") || opts.file.split("/").includes("..")) {
    fail("--file must be a relative path inside the engine tree, with no '..' segments");
  }
  if (opts.payloads.length === 0) opts.payloads = DEFAULT_PAYLOADS.filter((dir) => fs.existsSync(dir));
  if (opts.payloads.length === 0) fail("No payload directories found — pass --payload <dir>.");
  return opts;
}

function readManifest(payloadRoot) {
  const manifestPath = path.join(payloadRoot, MANIFEST_FILE);
  if (!fs.existsSync(manifestPath)) fail(`${payloadRoot} has no ${MANIFEST_FILE} — that is not a built payload.`);
  try {
    return JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  } catch (error) {
    return fail(`${manifestPath} is not readable JSON: ${error.message}`);
  }
}

/** The manifest's own size accounting, recomputed the way the builder computes it. */
function payloadSizeBytes(payloadRoot, manifest) {
  const sizes = [dirSize(path.join(payloadRoot, "hermes-agent"))];
  if (fs.existsSync(path.join(payloadRoot, "wheelhouse"))) {
    sizes.push(dirSize(path.join(payloadRoot, "wheelhouse")));
  }
  for (const relative of Object.values(manifest.site ?? {})) {
    sizes.push(dirSize(path.join(payloadRoot, relative)));
  }
  const runtimes = new Set(
    [manifest.python?.executable, ...Object.values(manifest.python?.variants ?? {})]
      .filter((entry) => typeof entry === "string" && entry !== "")
      .map((entry) => entry.split("/")[0]),
  );
  for (const runtime of runtimes) sizes.push(dirSize(path.join(payloadRoot, runtime)));
  return sizes.reduce((sum, size) => sum + size, 0);
}

/**
 * Does the payload on disk still match, BYTE FOR BYTE, the chain it claims?
 * Verbatim in intent from restamp-vendor-payload.mjs — nothing may be
 * rewritten that was not first proven.
 */
function auditDigestChain(payloadRoot, manifest) {
  const problems = [];
  const listed = new Map();
  const digestsPath = path.join(payloadRoot, DIGESTS_FILE);
  if (!fs.existsSync(digestsPath)) return { problems: [`payload has no ${DIGESTS_FILE}`], listed };
  const actual = sha256File(digestsPath);
  if (actual !== manifest.digests?.sha256) {
    problems.push(`${DIGESTS_FILE} sha256 ${actual} does not match the manifest pin ${manifest.digests?.sha256}`);
    return { problems, listed };
  }
  for (const line of fs.readFileSync(digestsPath, "utf8").split("\n")) {
    if (line.trim() === "") continue;
    const match = /^([0-9a-f]{64})\s+(.+)$/.exec(line.trim());
    if (match === null) {
      problems.push(`malformed line in ${DIGESTS_FILE}: "${line}"`);
      return { problems, listed };
    }
    listed.set(match[2], match[1]);
  }
  if (listed.size !== manifest.digests?.fileCount) {
    problems.push(`${DIGESTS_FILE} lists ${listed.size} files but the manifest claims ${manifest.digests?.fileCount}`);
  }
  const onDisk = walkDigestFiles(payloadRoot, fail)
    .filter((relative) => relative !== DIGESTS_FILE && relative !== MANIFEST_FILE);
  for (const relative of onDisk) {
    if (!listed.has(relative)) problems.push(`file on disk that nothing vouches for: ${relative}`);
  }
  let mismatched = 0;
  for (const [relative, expected] of listed) {
    const abs = path.join(payloadRoot, ...relative.split("/"));
    if (!fs.existsSync(abs)) {
      problems.push(`digested file is missing: ${relative}`);
      continue;
    }
    if (sha256File(abs) !== expected) {
      mismatched += 1;
      if (mismatched <= 5) problems.push(`digested file does not match its checksum: ${relative}`);
    }
  }
  if (mismatched > 5) problems.push(`...and ${mismatched - 5} more file(s) that do not match their checksums`);
  return { problems, listed };
}

/** Re-checks the tree against an already-verified snapshot: same names, same bytes. */
function auditAgainstSnapshot(payloadRoot, snapshot) {
  const problems = [];
  const onDisk = walkDigestFiles(payloadRoot, fail)
    .filter((relative) => relative !== DIGESTS_FILE && relative !== MANIFEST_FILE);
  const seen = new Set(onDisk);
  for (const relative of onDisk) {
    if (!snapshot.has(relative)) problems.push(`file appeared during the re-stamp: ${relative}`);
  }
  for (const [relative, expected] of snapshot) {
    if (!seen.has(relative)) {
      problems.push(`file disappeared during the re-stamp: ${relative}`);
      continue;
    }
    if (sha256File(path.join(payloadRoot, ...relative.split("/"))) !== expected) {
      problems.push(`file changed during the re-stamp: ${relative}`);
    }
  }
  return problems;
}

function writeDigestFileFromSnapshot(payloadRoot, snapshot) {
  const relatives = [...snapshot.keys()].sort();
  const lines = relatives.map((relative) => `${snapshot.get(relative)}  ${relative}`);
  const digestsPath = path.join(payloadRoot, DIGESTS_FILE);
  fs.writeFileSync(digestsPath, lines.join("\n") + "\n", "utf8");
  return { sha256: sha256File(digestsPath), fileCount: relatives.length };
}

/**
 * Every place the named engine file appears in this payload.
 *
 * Found by walking the DIGEST LIST rather than guessing the payload's shape:
 * the mac payload carries the engine source plus two per-architecture site
 * trees, Windows carries one — and a copy this script did not know about
 * would ship the OLD code while every test looked at the new one, which is
 * the precise failure mode "both sides must agree" exists to prevent.
 */
function copiesOf(listed, relativeFile) {
  const suffix = `/${relativeFile}`;
  return [...listed.keys()].filter((entry) => entry === relativeFile || entry.endsWith(suffix)).sort();
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const sourcePath = path.join(PATCHES_FILES_DIR, ...opts.file.split("/"));
  if (!fs.existsSync(sourcePath)) {
    fail(`the patch series has no ${opts.file} under ${path.relative(HUD_DIR, PATCHES_FILES_DIR)} — this script only pushes files the series OWNS`);
  }
  const source = fs.readFileSync(sourcePath);
  log(`series source: ${path.relative(HUD_DIR, sourcePath)} (${source.length} bytes, sha256 ${sha256File(sourcePath).slice(0, 12)}…)`);

  let failures = 0;
  for (const payloadRoot of opts.payloads) {
    const manifest = readManifest(payloadRoot);
    log(`\n=== ${payloadRoot}  (${manifest.platform}/${manifest.arch}, engine ${manifest.engineTag})`);

    const verified = auditDigestChain(payloadRoot, manifest);
    const copies = copiesOf(verified.listed, opts.file);

    if (opts.check) {
      const problems = [...verified.problems];
      if (copies.length === 0) problems.push(`payload carries no copy of ${opts.file}`);
      for (const relative of copies) {
        const abs = path.join(payloadRoot, ...relative.split("/"));
        if (!fs.existsSync(abs) || !fs.readFileSync(abs).equals(source)) {
          problems.push(`payload copy differs from the patch series: ${relative}`);
        }
      }
      if (problems.length === 0) {
        log(`  OK — ${copies.length} copy/copies match the series source, digest chain matches the manifest`);
      } else {
        failures += problems.length;
        for (const problem of problems) console.error(`  FAIL: ${problem}`);
      }
      continue;
    }

    if (verified.problems.length > 0) {
      for (const problem of verified.problems) console.error(`  FAIL: ${problem}`);
      fail(
        "refusing to re-stamp a payload that does not match its own checksums — "
        + "re-stamping it would bless whatever changed it. Rebuild the payload instead.",
      );
    }
    log(`  verified ${verified.listed.size} files against the existing chain before touching anything`);
    if (copies.length === 0) fail(`payload carries no copy of ${opts.file} — nothing to push, and that is not a payload shape this script understands`);

    // Every byte this run may change is held first — the copies AND the two
    // sidecars — and every mutation happens inside the rollback scope below.
    const backup = new Map();
    for (const relative of [...copies, DIGESTS_FILE, MANIFEST_FILE]) {
      backup.set(relative, fs.readFileSync(path.join(payloadRoot, ...relative.split("/"))));
    }
    const restore = () => {
      for (const [relative, bytes] of backup) {
        fs.writeFileSync(path.join(payloadRoot, ...relative.split("/")), bytes);
      }
      const stray = path.join(payloadRoot, `${MANIFEST_FILE}.tmp`);
      if (fs.existsSync(stray)) fs.rmSync(stray, { force: true });
    };
    const onSignal = (signal) => {
      restore();
      console.error(`\n  ${signal} during the re-stamp — rolled back, the payload is unchanged`);
      process.exit(1);
    };
    const signals = ["SIGINT", "SIGTERM", "SIGHUP"];
    for (const signal of signals) process.on(signal, onSignal);

    try {
      const approved = new Map(verified.listed);
      let changed = 0;
      for (const relative of copies) {
        const abs = path.join(payloadRoot, ...relative.split("/"));
        if (fs.readFileSync(abs).equals(source)) {
          log(`  unchanged ${relative}`);
          continue;
        }
        fs.writeFileSync(abs, source);
        changed += 1;
        log(`  updated   ${relative}`);
      }
      // The new list is the VERIFIED list with exactly these entries' hashes
      // replaced — never a fresh walk. auditAgainstSnapshot then re-checks the
      // whole tree against it, so anything else that moved under us is caught
      // rather than stamped.
      for (const relative of copies) {
        approved.set(relative, sha256File(path.join(payloadRoot, ...relative.split("/"))));
      }
      const drift = auditAgainstSnapshot(payloadRoot, approved);
      if (drift.length > 0) {
        for (const problem of drift) console.error(`  FAIL: ${problem}`);
        fail("the payload changed while it was being re-stamped — refusing to stamp bytes nobody verified");
      }

      const digests = writeDigestFileFromSnapshot(payloadRoot, approved);
      const previous = manifest.digests?.sha256;
      manifest.digests = { ...manifest.digests, sha256: digests.sha256, fileCount: digests.fileCount };
      manifest.sizeBytes = payloadSizeBytes(payloadRoot, manifest);
      const manifestPath = path.join(payloadRoot, MANIFEST_FILE);
      const tmp = `${manifestPath}.tmp`;
      fs.writeFileSync(tmp, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
      fs.renameSync(tmp, manifestPath);
      log(`  ${changed} file(s) changed; digest chain ${String(previous).slice(0, 12)}… -> ${digests.sha256.slice(0, 12)}… (${digests.fileCount} files)`);
    } catch (error) {
      restore();
      console.error(`  rolled back — the payload is unchanged`);
      throw error;
    } finally {
      for (const signal of signals) process.off(signal, onSignal);
    }
  }

  if (failures > 0) {
    console.error(`\n${failures} problem(s).`);
    process.exit(1);
  }
  log("\nDone.");
}

try {
  main();
} catch (error) {
  if (error instanceof RestampError) {
    console.error(`\nFAIL: ${error.message}`);
    process.exit(1);
  }
  throw error;
}
