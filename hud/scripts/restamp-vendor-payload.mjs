#!/usr/bin/env node
/**
 * Strip the antivirus-bait launcher stubs from an ALREADY-BUILT engine payload
 * and re-stamp its digest chain — without re-vendoring the engine.
 *
 * Why this exists (v1.5.1, AV-BAIT-STRIP): three paying customers were blocked
 * by antivirus deleting `pip/_vendor/distlib/t64-arm.exe` out of the shipped
 * payload, after which the digest check correctly refused to install anything.
 * The fix is to stop shipping the stubs no platform of ours can use. The engine
 * itself is NOT being changed for that: the vendored source, the wheels, the
 * site trees and the 8-patch series (`enginePatches`) must come out of this
 * byte-for-byte identical, because a re-vendor would put an unreviewed engine
 * into a release whose whole point is removing four Windows files.
 *
 * So this rewrites exactly three things:
 *   1. the stub files — deleted,
 *   2. `digests.sha256` — rebuilt from what is on disk afterwards,
 *   3. `vendor-manifest.json` — `digests.sha256`, `digests.fileCount`,
 *      `sizeBytes` and `strippedStubs` updated to match.
 * `builtAt`, `source`, `enginePatches`, `engineTag` and every other field are
 * left alone: nothing about WHERE the engine came from changed.
 *
 * The new manifest digest is deliberately load-bearing downstream. The install
 * stamp records the payload digest it came from (`setup/src/vendor.ts`), so
 * every 1.5.0 machine sees "different build" and reinstalls the engine and its
 * python from the stripped payload — which is precisely how the strip reaches
 * a customer whose stub is already quarantined.
 *
 * Usage:
 *   node hud/scripts/restamp-vendor-payload.mjs                # both payloads
 *   node hud/scripts/restamp-vendor-payload.mjs --payload <dir>
 *   node hud/scripts/restamp-vendor-payload.mjs --check        # verify only
 *
 * `--check` mutates nothing: it reports whether the payload still carries a
 * stub, whether it lost one it needs, and whether the digest chain on disk
 * matches the manifest. It is safe to run in a release battery.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { auditDistlibStubs, findDistlibStubs, stripDistlibStubs } from "./lib/distlib-stubs.mjs";
import {
  DIGESTS_FILE,
  MANIFEST_FILE,
  dirSize,
  sha256File,
  walkDigestFiles,
} from "./lib/payload-digest.mjs";

const HUD_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
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
  const opts = { payloads: [], check: false };
  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i];
    if (flag === "--check") opts.check = true;
    else if (flag === "--payload") opts.payloads.push(path.resolve(argv[++i] ?? fail("--payload needs a directory")));
    else fail(`Unknown argument: ${flag}`);
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
  // Runtime dirs are the first segment of each interpreter path: "python",
  // "python-arm64", "python-x64" — read from the manifest rather than guessed.
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
 *
 * Every listed file is re-hashed — not just the sidecar and the file counts
 * (Codex review r1, P1). A weaker check would let this script take a payload
 * whose bytes had been modified or corrupted since it was built, recompute a
 * fresh digest list over the damage, and hand it back with its trusted
 * provenance intact: the re-stamper would have LAUNDERED the corruption it
 * exists to protect against, and the release verifier downstream would then
 * happily bless it. Nothing may be rewritten that was not first proven.
 */
function auditDigestChain(payloadRoot, manifest) {
  const problems = [];
  const listed = new Map();
  const digestsPath = path.join(payloadRoot, DIGESTS_FILE);
  if (!fs.existsSync(digestsPath)) return { problems: [`payload has no ${DIGESTS_FILE}`], listed };
  const actual = sha256File(digestsPath);
  if (actual !== manifest.digests?.sha256) {
    problems.push(`${DIGESTS_FILE} sha256 ${actual} does not match the manifest pin ${manifest.digests?.sha256}`);
    // The list itself is not the one the manifest vouches for, so hashing
    // files against it would prove nothing at all. Stop here.
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

/**
 * Re-checks the tree against an ALREADY-VERIFIED snapshot: same file set, same
 * bytes. Run immediately before stamping, so what gets stamped is what was
 * proved rather than whatever the disk happens to hold now.
 */
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

/**
 * Writes `digests.sha256` from the verified snapshot — the builder's exact
 * format (`<sha256>  <posix path>`, sorted by path, LF-terminated), with no
 * hash computed from bytes that were not proved first.
 */
function writeDigestFileFromSnapshot(payloadRoot, snapshot) {
  const relatives = [...snapshot.keys()].sort();
  const lines = relatives.map((relative) => `${snapshot.get(relative)}  ${relative}`);
  const digestsPath = path.join(payloadRoot, DIGESTS_FILE);
  fs.writeFileSync(digestsPath, lines.join("\n") + "\n", "utf8");
  return { sha256: sha256File(digestsPath), fileCount: relatives.length };
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  let failures = 0;
  for (const payloadRoot of opts.payloads) {
    const manifest = readManifest(payloadRoot);
    const platform = manifest.platform;
    log(`\n=== ${payloadRoot}  (${platform}/${manifest.arch}, engine ${manifest.engineTag})`);

    if (opts.check) {
      const problems = [...auditDistlibStubs(payloadRoot, platform), ...auditDigestChain(payloadRoot, manifest).problems];
      if (problems.length === 0) {
        log(`  OK — no unusable launcher stubs, digest chain matches the manifest`);
      } else {
        failures += problems.length;
        for (const problem of problems) console.error(`  FAIL: ${problem}`);
      }
      continue;
    }

    // PROVE BEFORE REWRITING. The strip is the only change this script is
    // allowed to make, so the payload must be exactly what it says it is
    // before a single file is removed.
    const verified = auditDigestChain(payloadRoot, manifest);
    if (verified.problems.length > 0) {
      for (const problem of verified.problems) console.error(`  FAIL: ${problem}`);
      fail(
        "refusing to re-stamp a payload that does not match its own checksums — "
        + "re-stamping it would bless whatever changed it. Rebuild the payload instead.",
      );
    }
    log(`  verified ${verified.listed.size} files against the existing chain before touching anything`);

    // The stubs' BYTES are held before anything is deleted, so every abort
    // path below can put the payload back exactly as it was (Codex review r4,
    // P2). Without that, a guard firing mid-strip would leave a payload whose
    // manifest and digest list still name files that are gone — unusable, and
    // unrecoverable without a full re-vendor, which is a worse outcome than
    // the race the guard exists to catch.
    // EVERY byte this script may change is held first — the stubs it might
    // delete AND the two sidecars it rewrites — and every mutation happens
    // inside the rollback scope below. A failure anywhere (a locked file, a
    // full disk, the drift guard firing) must leave the payload exactly as it
    // arrived: a half-re-stamped payload names files that are gone, which is
    // unusable and only recoverable by a full re-vendor (Codex review r5).
    const before = findDistlibStubs(payloadRoot);
    const backup = new Map();
    for (const relative of [...before, DIGESTS_FILE, MANIFEST_FILE]) {
      backup.set(relative, fs.readFileSync(path.join(payloadRoot, ...relative.split("/"))));
    }
    const restore = () => {
      for (const [relative, bytes] of backup) {
        fs.writeFileSync(path.join(payloadRoot, ...relative.split("/")), bytes);
      }
      const stray = path.join(payloadRoot, `${MANIFEST_FILE}.tmp`);
      if (fs.existsSync(stray)) fs.rmSync(stray, { force: true });
    };
    // A Ctrl-C between the first deletion and the last write would otherwise
    // end the process without unwinding, leaving a payload whose manifest
    // names files that are gone — and the next run refuses to repair it, so
    // the only way back is a full re-vendor (Codex review r6). The handlers
    // are removed again as soon as the mutation window closes.
    const onSignal = (signal) => {
      restore();
      console.error(`\n  ${signal} during the re-stamp — rolled back, the payload is unchanged`);
      process.exit(1);
    };
    const signals = ["SIGINT", "SIGTERM", "SIGHUP"];
    for (const signal of signals) process.on(signal, onSignal);

    try {
    const removed = stripDistlibStubs(payloadRoot, platform);
    for (const relative of removed) log(`  removed ${relative}`);
    const kept = findDistlibStubs(payloadRoot);
    log(`  launcher stubs: ${before.length} before, ${removed.length} removed, ${kept.length} kept`);
    for (const relative of kept) log(`  kept    ${relative}`);

    const problems = auditDistlibStubs(payloadRoot, platform);
    if (problems.length > 0) {
      for (const problem of problems) console.error(`  FAIL: ${problem}`);
      fail("refusing to re-stamp a payload whose stub set is wrong");
    }

    // NO SECOND, UNVERIFIED WALK (Codex review r2, P1). Re-hashing the tree
    // here would open a window between "proved" and "stamped": a file changed
    // or deleted by antivirus in between would be hashed as it now is — or
    // silently dropped — and handed the payload's trusted provenance. So the
    // new list is the VERIFIED list minus exactly the stubs we removed, and
    // the tree is then re-checked against it: same bytes, same names, or this
    // script refuses. (Sorted order is inherited from the builder's own list,
    // so the format contract survives untouched.)
    const approved = new Map(verified.listed);
    for (const relative of removed) approved.delete(relative);
    const drift = auditAgainstSnapshot(payloadRoot, approved);
    if (drift.length > 0) {
      for (const problem of drift) console.error(`  FAIL: ${problem}`);
      fail("the payload changed while it was being re-stamped — refusing to stamp bytes nobody verified");
    }
    const digests = writeDigestFileFromSnapshot(payloadRoot, approved);
    const previous = manifest.digests?.sha256;
    manifest.digests = { ...manifest.digests, sha256: digests.sha256, fileCount: digests.fileCount };
    manifest.sizeBytes = payloadSizeBytes(payloadRoot, manifest);
    manifest.strippedStubs = [...(manifest.strippedStubs ?? []), ...removed].sort();
    // Written the way the builder writes it: to a temp name, then renamed, so
    // the presence of the manifest never means a half-written one.
    const manifestPath = path.join(payloadRoot, MANIFEST_FILE);
    fs.writeFileSync(`${manifestPath}.tmp`, JSON.stringify(manifest, null, 2) + "\n", "utf8");
    fs.renameSync(`${manifestPath}.tmp`, manifestPath);
    log(`  ${DIGESTS_FILE}: ${digests.fileCount} files`);
    log(`  payload digest: ${previous} -> ${digests.sha256}`);
    log(`  sizeBytes: ${manifest.sizeBytes}`);
    } catch (error) {
      // Put EVERYTHING back before rethrowing — the stubs and both sidecars —
      // so the payload leaves this script either fully re-stamped or exactly
      // as it arrived, never in between.
      restore();
      console.error("  rolled back — the payload is exactly as it arrived");
      throw error;
    } finally {
      for (const signal of signals) process.off(signal, onSignal);
    }
  }
  if (failures > 0) {
    console.error(`\n${failures} problem(s) found.`);
    process.exit(1);
  }
  log("");
}

try {
  main();
} catch (error) {
  console.error(`\nRESTAMP FAILED: ${error instanceof RestampError ? error.message : error?.stack ?? error}`);
  process.exit(1);
}
