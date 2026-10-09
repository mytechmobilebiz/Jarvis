#!/usr/bin/env node
/**
 * Refuses to build (or to believe) an installer without a PROVEN offline
 * engine payload.
 *
 * Why this exists: electron-builder copies `extraResources` SILENTLY. With the
 * payload missing it emitted a perfectly normal-looking 94 MB installer with no
 * engine inside and no warning — a packaged app that would fall back to the
 * network install on a customer machine, which is the exact class v1.4.0 is
 * meant to delete. A build that cannot ship the appliance must fail here, on
 * the build machine, not on a buyer's laptop.
 *
 * What "proven" means (review finding 6 — names and counts are NOT integrity):
 *   1. The manifest pins the sha256 of digests.sha256, which pins the sha256
 *      of every payload file. That chain is checked in EVERY mode: a payload
 *      whose digest file does not match its manifest is rejected outright.
 *   2. In --release mode, every listed file is re-hashed byte-for-byte and the
 *      payload is swept for unlisted extras — expensive, and worth it exactly
 *      once per release build. One flipped bit in one wheel fails the build.
 *   3. In --release mode, provenance is checked against the CHECKED-IN root
 *      of trust, hud/vendor/engine-pin.json — not against the manifest's own
 *      claims (r2 finding 9: a manifest asserting "I am v2026.7.1" proves
 *      only self-consistency). The recorded source commit / tarball sha256
 *      must EQUAL the pinned values, and the pin file is re-read here, so a
 *      builder handed a retagged commit or substituted tarball cannot ship.
 *      "local-dirty" (someone's edited ~/.hermes checkout) and
 *      "tarball-unpinned" (hash recorded but nothing to compare it to) are
 *      dev artifacts and can NEVER ship.
 *   4. In --release mode, JARVIS_ALLOW_NO_VENDOR is IGNORED. The escape hatch
 *      exists for engine-less dev builds only; a release command must have no
 *      reachable bypass, or someday a tired human will use it at 2am.
 *
 * Universal mac awareness (review finding 7): a universal Electron app needs
 * BOTH python runtimes (PBS ships no universal2 CPython). `--arch universal`
 * requires a manifest with arch "universal" and python.variants for arm64 AND
 * x64, each present, executable-shaped, and covered by the digest file.
 *
 * Modes:
 *   node scripts/verify-vendor-payload.mjs --platform win32 --arch x64
 *       Pre-build: the assembled payload at hud/vendor/engine is complete and
 *       targets the platform being built.
 *   node scripts/verify-vendor-payload.mjs --platform win32 --packed <dir>
 *       Post-build: the payload actually landed in the packaged resources.
 *   Add --release for the full byte-level + provenance gate (dist:win /
 *   dist:mac pass it; dev packaging loops may omit it for speed).
 *
 * Escape hatch for a deliberate engine-less dev build (NON-release only):
 *   JARVIS_ALLOW_NO_VENDOR=1 (prints a loud warning; unreachable in --release).
 */
import crypto from "node:crypto";
import fs from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { auditDistlibStubs } from "./lib/distlib-stubs.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const defaultPayload = path.resolve(here, "..", "vendor", "engine");
const enginePinPath = path.resolve(here, "..", "vendor", "engine-pin.json");

/**
 * The checked-in root of trust (r2 finding 9). Read from SOURCE CONTROL, not
 * from the payload: the whole point is to compare what the builder recorded
 * against an identity the builder could not have invented. Only --release
 * needs it, so failures here are release failures.
 */
function loadEnginePin() {
  let pin;
  try {
    pin = JSON.parse(fs.readFileSync(enginePinPath, "utf8"));
  } catch (error) {
    fail(
      `cannot read the engine pin (${enginePinPath}): ${error.message}`,
      "The pin file is the checked-in root of trust — a release build without it is unverifiable. Restore it from git.",
    );
  }
  if (typeof pin.tag !== "string" || pin.tag === "") fail(`engine pin has no "tag" (${enginePinPath})`);
  if (typeof pin.commit !== "string" || (pin.commit !== "" && !/^[0-9a-f]{40}$/.test(pin.commit))) {
    fail(`engine pin "commit" must be empty or a 40-hex sha (${enginePinPath})`);
  }
  if (
    typeof pin.tarballSha256 !== "string" ||
    (pin.tarballSha256 !== "" && !/^[0-9a-f]{64}$/.test(pin.tarballSha256))
  ) {
    fail(`engine pin "tarballSha256" must be empty or a 64-hex sha256 (${enginePinPath})`);
  }
  return pin;
}

const pinFillHint = (tag) =>
  `Fill hud/vendor/engine-pin.json: commit from \`git ls-remote ` +
  `https://github.com/NousResearch/hermes-agent.git refs/tags/${tag} "refs/tags/${tag}^{}"\` ` +
  `(take the ^{} sha); tarballSha256 from \`curl -L ` +
  `https://github.com/NousResearch/hermes-agent/archive/refs/tags/${tag}.tar.gz | shasum -a 256\`.`;

function parseArgs(argv) {
  const args = {
    platform: process.platform,
    arch: process.arch,
    packed: undefined,
    payload: defaultPayload,
    release: false,
    allowUnsignedPayload: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const flag = argv[index];
    if (flag === "--platform") args.platform = argv[++index];
    else if (flag === "--arch") args.arch = argv[++index];
    else if (flag === "--packed") args.packed = argv[++index];
    else if (flag === "--payload") args.payload = argv[++index];
    else if (flag === "--release") args.release = true;
    // CI has no Developer ID and never will. Everything else still runs; only
    // the signature audit is waived, and only when asked for by name.
    else if (flag === "--allow-unsigned-payload") args.allowUnsignedPayload = true;
    else {
      fail(`Unknown argument: ${flag}`);
    }
  }
  return args;
}

function fail(message, hint) {
  console.error(`[verify-vendor-payload] FAIL: ${message}`);
  if (hint !== undefined) console.error(`[verify-vendor-payload] ${hint}`);
  process.exit(1);
}

const fmtBytes = (n) => {
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(2)} GB`;
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${n} B`;
};

/** sha256 of a file's contents (follows symlinks, matching the builder). */
function sha256File(absPath) {
  return crypto.createHash("sha256").update(fs.readFileSync(absPath)).digest("hex");
}

/** A symlink's REAL target must resolve inside the payload root (r2 finding
 *  9). Hashing a symlink by referent without this check would bless a link to
 *  /etc/passwd or into the .app bundle: the digest matches whatever foreign
 *  bytes it points at today, and the installed copy is at the mercy of a path
 *  the payload does not own. */
function assertSymlinkInRoot(rootReal, abs, relative, label) {
  let real;
  try {
    real = fs.realpathSync(abs);
  } catch (error) {
    fail(`unresolvable symlink in payload: ${relative} (${label}): ${error.message}`);
  }
  const rel = path.relative(rootReal, real);
  if (rel.startsWith("..") || path.isAbsolute(rel)) {
    fail(
      `symlink escapes the payload root: ${relative} -> ${real} (${label})`,
      "A payload file's bytes must live inside the payload. This is tampering or a broken build.",
    );
  }
}

/** Every file under root as posix-relative paths. Symlinks to files count as
 *  files (the builder digests them by followed content) but must resolve
 *  INSIDE the payload; dangling/dir symlinks fail — the builder refuses to
 *  produce any of these, so their presence IS tampering. */
function walkFiles(root, label) {
  const rootReal = fs.realpathSync(root);
  const out = [];
  const walk = (dir, prefix) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const relative = prefix === "" ? entry.name : `${prefix}/${entry.name}`;
      const abs = path.join(dir, entry.name);
      if (entry.isSymbolicLink()) {
        const st = fs.statSync(abs, { throwIfNoEntry: false });
        if (!st || st.isDirectory()) fail(`unverifiable symlink in payload: ${relative} (${label})`);
        assertSymlinkInRoot(rootReal, abs, relative, label);
        out.push(relative);
      } else if (entry.isDirectory()) {
        walk(abs, relative);
      } else if (entry.isFile()) {
        out.push(relative);
      } else {
        fail(`unsupported file type in payload: ${relative} (${label})`);
      }
    }
  };
  walk(root, "");
  return out;
}

function readManifest(root) {
  const manifestPath = path.join(root, "vendor-manifest.json");
  if (!fs.existsSync(manifestPath)) return undefined;
  try {
    return JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  } catch (error) {
    fail(`${manifestPath} is not readable JSON: ${error.message}`);
  }
  return undefined;
}

/** A payload-relative path from the digest file must stay inside the payload.
 *  The digest file is hash-pinned by the manifest, but validating costs
 *  nothing and a traversal path in it can only mean tampering. */
function safeJoin(root, relative) {
  if (relative.includes("\\") || path.posix.isAbsolute(relative)) {
    fail(`digest file lists a non-portable path: ${relative}`);
  }
  const resolved = path.join(root, ...relative.split("/"));
  const rel = path.relative(root, resolved);
  if (rel.startsWith("..") || path.isAbsolute(rel)) {
    fail(`digest file lists a path escaping the payload: ${relative}`);
  }
  return resolved;
}

/**
 * A mac payload's binaries are signed BEFORE its digests are computed, by the
 * payload builder, and electron-builder is told not to re-sign them
 * (mac.signIgnore). That ordering is what lets the digest chain cover the exact
 * bytes we ship — no "these files are excused" map, no contents left unpinned.
 *
 * This gate proves the first half of that arrangement actually happened: in a
 * RELEASE mac payload, every Mach-O must carry OUR Developer ID signature with
 * a secure timestamp, which is precisely what Apple's notary service checks and
 * what the wheels could never satisfy.
 */
const { isMachO } = await import("@jarvis/setup/vendor");
const { developerIdRequirement, JARVIS_MAC_TEAM_ID } = await import("@jarvis/setup/code-signature");

function isMachOFile(absPath) {
  const handle = fs.openSync(absPath, "r");
  try {
    const head = Buffer.alloc(4);
    if (fs.readSync(handle, head, 0, 4, 0) < 4) return false;
    return isMachO(head);
  } finally {
    fs.closeSync(handle);
  }
}

function assertPayloadIsSigned(root, label, args) {
  if (process.platform !== "darwin") {
    if (args.allowUnsignedPayload) {
      console.log(`[verify-vendor-payload] not on macOS: skipping the signature audit (${label}).`);
      return;
    }
    // Failing closed matters more than convenience here: a Linux job that
    // printed OK for a RELEASE mac payload would be certifying something it
    // never looked at. Structural verification without --release is available
    // to anyone who wants it.
    fail(
      `a RELEASE darwin payload cannot be verified from ${process.platform} (${label})`,
      "Its binaries' signatures can only be checked on macOS. Run this on the signing Mac, "
      + "pass --allow-unsigned-payload if this is the unsigned CI path, or drop --release.",
    );
  }
  const requirement = developerIdRequirement(JARVIS_MAC_TEAM_ID);
  const binaries = walkFiles(root, label)
    .map((relative) => path.join(root, ...relative.split("/")))
    .filter((abs) => {
      const lst = fs.lstatSync(abs, { throwIfNoEntry: false });
      // A symlink is signed through its target, which the walk visits itself.
      return lst?.isFile() && isMachOFile(abs);
    });
  if (binaries.length === 0) fail(`a mac payload with no Mach-O binaries at all (${label}) — it cannot run`);

  const problems = [];
  for (const abs of binaries) {
    const verified = spawnSync("codesign", ["--verify", "--strict", `-R=${requirement}`, abs], { encoding: "utf8" });
    if (verified.error !== undefined || verified.status !== 0) {
      problems.push([abs, "not signed by us, or modified since signing"]);
      continue;
    }
    // A signature without a SECURE TIMESTAMP satisfies the requirement above
    // and is still refused by Apple's notary service — it was half of why the
    // wheels failed. Checking it here turns a 15-minute notarization round
    // trip into an immediate, named failure (review finding 4).
    const described = spawnSync("codesign", ["-d", "--verbose=4", abs], { encoding: "utf8" });
    const text = `${described.stdout ?? ""}${described.stderr ?? ""}`;
    if (!/^Timestamp=.+$/m.test(text)) problems.push([abs, "signed WITHOUT a secure timestamp"]);
    else if (!/flags=\S*runtime/.test(text)) problems.push([abs, "signed without the hardened runtime"]);
  }
  if (problems.length > 0) {
    fail(
      `${problems.length} of ${binaries.length} payload binaries are not release-signed (${label}): `
      + problems.slice(0, 5).map(([abs, why]) => `${path.relative(root, abs)} (${why})`).join("; ")
      + (problems.length > 5 ? ", ..." : ""),
      "Apple's notary service refuses every one of them. Rebuild the payload on a machine with the "
      + "Developer ID identity in its keychain — build-vendor-payload.mjs signs the payload before hashing it.",
    );
  }
  console.log(
    `[verify-vendor-payload] all ${binaries.length} payload binaries carry our Developer ID signature, `
    + `hardened runtime and a secure timestamp (${label}).`,
  );
}

/**
 * No Mach-O may hide inside an archive.
 *
 * This is the exact defect that cost this release its first notarization: the
 * notary opens .whl and .tar alike and refuses the unsigned binaries inside,
 * and nothing in a hash chain notices, because the archive's own hash is
 * perfectly correct. We ship no such archive today (checked: the two bundled
 * pip wheels and dateutil's zoneinfo tarball hold none), and this makes sure a
 * future dependency cannot reintroduce one silently.
 */
function assertNoBinariesInArchives(root, label) {
  const archives = walkFiles(root, label).filter((relative) => /\.(whl|zip|tar|tar\.gz|tgz|jar)$/i.test(relative));
  const offenders = [];
  for (const relative of archives) {
    const abs = path.join(root, ...relative.split("/"));
    // `tar tf`/`unzip -l` cannot read member CONTENT, so ask python — the same
    // interpreter the payload ships — to check each member's magic bytes.
    const probe = spawnSync(process.execPath, ["-e", ARCHIVE_PROBE, abs], { encoding: "utf8" });
    if (probe.status !== 0) {
      fail(`could not inspect the archive ${relative} (${label})`, probe.stderr?.trim() ?? "");
    }
    const members = (probe.stdout ?? "").trim();
    if (members !== "") offenders.push(`${relative} -> ${members}`);
  }
  if (offenders.length > 0) {
    fail(
      `${offenders.length} archive(s) in the payload contain Mach-O binaries (${label}): ${offenders.slice(0, 3).join("; ")}`,
      "Apple's notary service opens archives and refuses unsigned binaries inside them — this is what "
      + "forced the wheelhouse off macOS in v1.4.0. Install the package into the site tree instead of "
      + "shipping it as an archive.",
    );
  }
}

/** Reads a zip's members without a dependency: local file headers only. */
const ARCHIVE_PROBE = `
const fs = require("node:fs");
const zlib = require("node:zlib");
const file = process.argv[1];
const MAGICS = new Set([0xfeedface, 0xcefaedfe, 0xfeedfacf, 0xcffaedfe, 0xcafebabe, 0xbebafeca, 0xcafebabf, 0xbfbafeca]);
const hits = [];
const isMachO = (buf) => buf.length >= 4 && MAGICS.has(buf.readUInt32BE(0));
const data = fs.readFileSync(file);
if (/\\.(whl|zip|jar)$/i.test(file)) {
  // Walk local file headers; only the first 4 bytes of each member are needed,
  // and stored/deflated both decompress cheaply at that size.
  let at = 0;
  while (at + 30 <= data.length && data.readUInt32LE(at) === 0x04034b50) {
    const method = data.readUInt16LE(at + 8);
    const compressed = data.readUInt32LE(at + 18);
    const nameLength = data.readUInt16LE(at + 26);
    const extraLength = data.readUInt16LE(at + 28);
    const name = data.toString("utf8", at + 30, at + 30 + nameLength);
    const start = at + 30 + nameLength + extraLength;
    const body = data.subarray(start, start + Math.min(compressed, 4096));
    let head = body;
    if (method === 8) { try { head = zlib.inflateRawSync(body, { finishFlush: zlib.constants.Z_SYNC_FLUSH }); } catch { head = Buffer.alloc(0); } }
    if (isMachO(head)) hits.push(name);
    at = start + compressed;
    if (compressed === 0 && method !== 0) break;
  }
} else {
  const raw = /\\.(gz|tgz)$/i.test(file) ? zlib.gunzipSync(data) : data;
  for (let at = 0; at + 512 <= raw.length; ) {
    const name = raw.toString("utf8", at, at + 100).replace(/\\0.*$/, "");
    if (name === "") break;
    const size = parseInt(raw.toString("ascii", at + 124, at + 136).replace(/\\0| /g, ""), 8) || 0;
    const body = raw.subarray(at + 512, at + 512 + Math.min(size, 4));
    if (isMachO(body)) hits.push(name);
    at += 512 + Math.ceil(size / 512) * 512;
  }
}
process.stdout.write(hits.join(", "));
`;

/**
 * The digest chain: manifest -> digests.sha256 -> every payload file.
 * Always: hash the digest file and compare to the manifest pin, parse it,
 * check the file count. --release only: re-hash every listed file and sweep
 * for unlisted extras (the expensive, byte-level proof).
 * Returns { entries, verifiedFiles, verifiedBytes } for reporting.
 */
function verifyDigestChain(root, manifest, label, releaseMode) {
  const d = manifest.digests;
  if (typeof d !== "object" || d === null) {
    fail(
      `manifest has no "digests" record (${label}) — this payload predates the ` +
      "integrity chain. Rebuild it with the current build-vendor-payload.mjs.",
    );
  }
  if (d.algorithm !== "sha256") fail(`unsupported digest algorithm "${d.algorithm}" (${label})`);
  // The file name is a fixed contract shared with the runtime installer.
  if (d.file !== "digests.sha256") fail(`manifest points digests at "${d.file}", expected "digests.sha256" (${label})`);
  if (!/^[0-9a-f]{64}$/.test(d.sha256 ?? "")) fail(`manifest digest pin is not a sha256 hex string (${label})`);
  if (!Number.isInteger(d.fileCount) || d.fileCount <= 0) fail(`manifest digest fileCount is not a positive integer (${label})`);

  const digestsPath = path.join(root, d.file);
  if (!fs.existsSync(digestsPath)) fail(`payload has no ${d.file} (${label})`);
  const actual = sha256File(digestsPath);
  if (actual !== d.sha256) {
    fail(
      `${d.file} does not match the manifest pin (${label}).`,
      `expected ${d.sha256}, got ${actual} — the payload was modified after the manifest was written.`,
    );
  }

  const entries = [];
  const lines = fs.readFileSync(digestsPath, "utf8").split("\n");
  for (const line of lines) {
    if (line === "") continue;
    const match = /^([0-9a-f]{64})  (.+)$/.exec(line);
    if (!match) fail(`malformed line in ${d.file}: "${line}" (${label})`);
    entries.push({ hash: match[1], relative: match[2] });
  }
  if (entries.length !== d.fileCount) {
    fail(`${d.file} lists ${entries.length} files but the manifest claims ${d.fileCount} (${label})`);
  }

  // Case-fold uniqueness (r2 finding 9): `A.whl` and `a.whl` are two digest
  // entries but ONE file on the customer's Windows/macOS disk — after
  // extraction one body would have to satisfy two hashes, or worse, satisfy
  // one and silently shadow the other. Cheap to check, so checked in every
  // mode, not just --release.
  const folded = new Map();
  for (const { relative } of entries) {
    const key = relative.toLowerCase();
    const prev = folded.get(key);
    if (prev !== undefined) {
      fail(
        `case-fold collision in ${d.file}: "${prev}" vs "${relative}" (${label})`,
        "These are one file on a case-insensitive filesystem — the payload cannot verify after install. Rebuild it.",
      );
    }
    folded.set(key, relative);
  }

  let verifiedFiles = 0;
  let verifiedBytes = 0;
  if (releaseMode) {
    const rootReal = fs.realpathSync(root);
    for (const { hash, relative } of entries) {
      const abs = safeJoin(root, relative);
      const lst = fs.lstatSync(abs, { throwIfNoEntry: false });
      if (!lst) fail(`digested file is missing: ${relative} (${label})`);
      if (lst.isSymbolicLink()) {
        // Hashing by referent is only meaningful when the referent is OURS.
        assertSymlinkInRoot(rootReal, abs, relative, label);
      } else if (!lst.isFile()) {
        fail(`digested entry is not a regular file: ${relative} (${label})`);
      }
      const st = fs.statSync(abs, { throwIfNoEntry: false });
      if (!st || !st.isFile()) fail(`digested file is missing: ${relative} (${label})`);
      const got = sha256File(abs);
      if (got !== hash) {
        fail(
          `CONTENT MISMATCH: ${relative} (${label})`,
          `expected sha256 ${hash}, got ${got} — the payload is corrupt or tampered. Rebuild it. `
          + "(If this is a packed mac app, electron-builder re-signed a payload binary it was told to "
          + "leave alone — check mac.signIgnore in hud/package.json.)",
        );
      }
      verifiedFiles += 1;
      verifiedBytes += st.size;
    }
    // Sweep for files the digest file does not know about: a payload with
    // extras is not the payload that was built and hashed.
    const listed = new Set(entries.map((e) => e.relative));
    const extras = walkFiles(root, label).filter(
      (f) => f !== "vendor-manifest.json" && f !== d.file && !listed.has(f),
    );
    if (extras.length > 0) {
      fail(
        `payload contains ${extras.length} file(s) absent from ${d.file} (${label}): ` +
        `${extras.slice(0, 10).join(", ")}${extras.length > 10 ? ", ..." : ""}`,
      );
    }
  }
  return { entries, verifiedFiles, verifiedBytes };
}

/** Every architecture a Mach-O actually contains, read from its own header. */
function machOArchs(absPath) {
  const handle = fs.openSync(absPath, "r");
  try {
    const head = Buffer.alloc(4096);
    const read = fs.readSync(handle, head, 0, 4096, 0);
    if (read < 8) return [];
    const magic = head.readUInt32BE(0);
    const CPU_ARM64 = 0x0100000c;
    const CPU_X86_64 = 0x01000007;
    const name = (cpu) => (cpu === CPU_ARM64 ? "arm64" : cpu === CPU_X86_64 ? "x64" : `cpu-0x${cpu.toString(16)}`);
    // Fat: read the arch TABLE. "It is fat, therefore it has everything" was
    // the old assumption, and a fat file holding two architectures neither of
    // which is ours would have passed on it (review finding 8).
    const fat = magic === 0xcafebabe || magic === 0xcafebabf
      || magic === 0xbebafeca || magic === 0xbfbafeca;
    if (fat) {
      const swapped = magic === 0xbebafeca || magic === 0xbfbafeca;
      const wide = magic === 0xcafebabf || magic === 0xbfbafeca;
      const count = swapped ? head.readUInt32LE(4) : head.readUInt32BE(4);
      const stride = wide ? 32 : 20;
      const archs = [];
      for (let index = 0; index < count && 8 + index * stride + 4 <= read; index += 1) {
        const at = 8 + index * stride;
        archs.push(name(swapped ? head.readUInt32LE(at) : head.readUInt32BE(at)));
      }
      return archs;
    }
    const littleEndian = magic === 0xcffaedfe || magic === 0xcefaedfe;
    return [name(littleEndian ? head.readUInt32LE(4) : head.readUInt32BE(4))];
  } finally {
    fs.closeSync(handle);
  }
}

/**
 * Is EVERY binary in this tree built for the architecture the tree claims?
 *
 * The cross-install that produces site-x64 on an arm64 builder is one `pip`
 * flag away from silently producing arm64 wheels, and the result works
 * perfectly on the build machine and fails at the first import on the
 * customer's. Checking one file was not enough: a correct first extension
 * would hide a wrong Pillow dylib three directories later.
 */
function assertTreeArch(treeRoot, arch, relative, label) {
  const wrong = [];
  let checked = 0;
  for (const memberRelative of walkFiles(treeRoot, label)) {
    const abs = path.join(treeRoot, ...memberRelative.split("/"));
    const lst = fs.lstatSync(abs, { throwIfNoEntry: false });
    if (!lst?.isFile() || !isMachOFile(abs)) continue;
    checked += 1;
    const archs = machOArchs(abs);
    if (!archs.includes(arch)) wrong.push(`${memberRelative} (${archs.join("+") || "unreadable"})`);
  }
  if (checked === 0) fail(`site tree ${relative} holds no compiled extensions (${label})`);
  if (wrong.length > 0) {
    fail(
      `${wrong.length} of ${checked} binaries in ${relative} are not ${arch} (${label}): `
      + wrong.slice(0, 5).join(", ") + (wrong.length > 5 ? ", ..." : ""),
      "The cross-architecture pip install picked the wrong wheels. Rebuild the payload.",
    );
  }
  console.log(`[verify-vendor-payload] site tree ${relative}: all ${checked} binaries are ${arch} (${label}).`);
}

function verify(root, args, label) {
  const manifest = readManifest(root);
  if (manifest === undefined) {
    if (!args.release && process.env.JARVIS_ALLOW_NO_VENDOR === "1") {
      console.warn(
        `[verify-vendor-payload] WARNING: no engine payload in ${root}. `
        + "JARVIS_ALLOW_NO_VENDOR=1 is set, so this build continues — the installed app "
        + "will fall back to the NETWORK engine install. Never ship this to customers.",
      );
      return;
    }
    fail(
      `no engine payload at ${root} (${label}).`,
      "Build it first: node hud/scripts/build-vendor-payload.mjs --platform "
      + `${args.platform} --arch ${args.arch} --clean`
      + (args.release ? "  (JARVIS_ALLOW_NO_VENDOR is ignored in --release mode.)" : ""),
    );
  }

  if (manifest.schema !== 1) fail(`unsupported manifest schema ${manifest.schema}`);
  if (manifest.platform !== args.platform) {
    fail(`payload targets ${manifest.platform}, but this build is ${args.platform}`);
  }
  if (args.platform === "win32" && manifest.arch !== args.arch) {
    // BUILDLOG rule 1/3: an arch mismatch on Windows is the 2026-08-08 incident.
    fail(`payload targets ${manifest.arch}, but this build is ${args.arch}`);
  }
  if (args.platform === "darwin" && args.arch === "universal" && manifest.arch !== "universal") {
    // Review finding 7: a universal .app with a single-arch python means the
    // OTHER architecture ships a binary it cannot exec.
    fail(
      `payload targets darwin/${manifest.arch}, but a universal build needs the dual-runtime payload.`,
      "Build it: node hud/scripts/build-vendor-payload.mjs --platform darwin --arch universal --clean",
    );
  }

  // A payload declares exactly ONE install shape: pre-installed site trees
  // (macOS — Apple's notary refuses the wheels a wheelhouse would carry) or a
  // wheelhouse to pip-install on first run (Windows).
  const site = manifest.site;
  const isSite = site !== undefined && site !== null;
  if (isSite && (manifest.requirements !== undefined || manifest.engineWheel !== undefined)) {
    fail(`manifest declares BOTH a site tree and a wheelhouse (${label})`);
  }
  const required = [
    manifest.python?.executable,
    ...(isSite ? [] : [manifest.requirements, manifest.engineWheel]),
    "hermes-agent",
  ];
  for (const relative of required) {
    if (typeof relative !== "string" || relative === "") fail(`manifest is missing a required path (${label})`);
    const resolved = path.join(root, ...relative.split("/"));
    if (!fs.existsSync(resolved)) fail(`payload is incomplete: ${resolved} is missing (${label})`);
  }

  if (isSite) {
    if (fs.existsSync(path.join(root, "wheelhouse"))) {
      fail(
        `a site payload still carries a wheelhouse (${label})`,
        "Its wheels would be opened by Apple's notary service and refused as unsigned binaries. Rebuild the payload.",
      );
    }
    const archs = manifest.arch === "universal" ? ["arm64", "x64"] : [manifest.arch];
    for (const arch of archs) {
      const relative = site[arch];
      if (typeof relative !== "string" || relative === "") fail(`site payload lacks site.${arch} (${label})`);
      const treeRoot = path.join(root, ...relative.split("/"));
      if (!fs.existsSync(treeRoot)) fail(`site tree ${relative} is missing (${label})`);
      // The engine's own package must be IN the tree — a tree of dependencies
      // with no engine installs cleanly and then cannot start.
      if (!fs.existsSync(path.join(treeRoot, "hermes_cli"))) {
        fail(`site tree ${relative} has no hermes_cli package (${label})`);
      }
      // And EVERY binary in it must be built for the architecture the tree
      // claims — .so, .dylib, anything with Mach-O magic — or the tree
      // installs fine and dies at the first import on the customer's Mac.
      assertTreeArch(treeRoot, arch, relative, label);
    }
  } else {
    const wheels = fs.readdirSync(path.join(root, "wheelhouse")).filter((entry) => entry.endsWith(".whl"));
    if (wheels.length === 0) fail(`the wheelhouse holds no wheels (${label})`);
    if (wheels.length !== manifest.wheelCount) {
      fail(`the wheelhouse holds ${wheels.length} wheels but the manifest claims ${manifest.wheelCount} (${label})`);
    }
  }

  // Digest chain first, provenance second: a tampered payload should be
  // reported as tampered even when it would also fail the provenance gate.
  const chain = verifyDigestChain(root, manifest, label, args.release);

  // The antivirus-bait gate (v1.5.1). Three paying customers were blocked by
  // antivirus deleting one unusable Windows launcher stub out of the payload,
  // after which the digest chain above correctly refused the whole install. A
  // payload that still carries a stub this platform can never use goes no
  // further — and neither does one that lost `t64.exe`/`w64.exe`, which pip
  // appends to CREATE hermes.exe: an over-eager strip would trade a rare AV
  // false positive for a venv with no console scripts at all.
  for (const problem of auditDistlibStubs(root, manifest.platform)) {
    fail(`${problem} (${label})`);
  }

  // Universal payloads: both runtimes must exist, look executable, and be
  // covered by the digest chain.
  if (manifest.arch === "universal") {
    const variants = manifest.python?.variants;
    const digested = new Set(chain.entries.map((e) => e.relative));
    for (const arch of ["arm64", "x64"]) {
      const relative = variants?.[arch];
      if (typeof relative !== "string" || relative === "") {
        fail(`universal payload manifest lacks python.variants.${arch} (${label})`);
      }
      const abs = path.join(root, ...relative.split("/"));
      const st = fs.statSync(abs, { throwIfNoEntry: false }); // follows bin/python3 symlinks
      if (!st || !st.isFile() || st.size === 0) {
        fail(`python variant ${arch} is missing or not a regular file: ${relative} (${label})`);
      }
      if (process.platform !== "win32" && (st.mode & 0o111) === 0) {
        fail(`python variant ${arch} is not executable: ${relative} (${label})`);
      }
      if (!digested.has(relative)) {
        fail(`python variant ${arch} (${relative}) is not covered by the digest file (${label})`);
      }
    }
  }

  if (args.release) {
    // Provenance gate (r2 finding 9): the manifest's own claims prove only
    // self-consistency, so re-read the CHECKED-IN pin and require the
    // recorded identity to EQUAL it. Only a clean checkout of the pinned
    // commit or the pin-matched tarball may ship; "local-dirty" and
    // "tarball-unpinned" are dev artifacts by definition.
    const kind = manifest.source?.kind;
    if (kind !== "git-tag-clean" && kind !== "tarball-sha256") {
      fail(
        `source.kind is "${kind ?? "missing"}" — refusing to release (${label}).`,
        "A release payload must be built from a clean checkout of the pinned commit "
        + "or the pin-matched tag tarball. Rebuild without --allow-dirty-source.",
      );
    }
    const pin = loadEnginePin();
    if (manifest.engineTag !== pin.tag) {
      fail(
        `payload is tagged "${manifest.engineTag}" but engine-pin.json pins "${pin.tag}" (${label}).`,
        "The checked-in pin is the release identity. Rebuild against the pinned tag, or bump the pin deliberately.",
      );
    }
    if (kind === "git-tag-clean") {
      if (pin.commit === "") {
        fail(
          `engine-pin.json has an empty "commit" — nothing to verify a git-built payload against (${label}).`,
          pinFillHint(pin.tag),
        );
      }
      const recorded = manifest.source?.commit ?? "";
      if (!/^[0-9a-f]{40}$/.test(recorded)) {
        fail(`source.commit ("${recorded}") is not a 40-hex sha — refusing to release (${label}).`);
      }
      if (recorded !== pin.commit) {
        fail(
          `ENGINE COMMIT MISMATCH (${label}):`,
          `payload was built from ${recorded}, but engine-pin.json pins ${pin.commit} for ${pin.tag}. `
          + "A retargeted local tag or stale pin — either way this is not the engine we ship. Investigate before touching the pin.",
        );
      }
    } else {
      // kind === "tarball-sha256"
      if (pin.tarballSha256 === "") {
        fail(
          `engine-pin.json has an empty "tarballSha256" — nothing to verify a tarball-built payload against (${label}).`,
          pinFillHint(pin.tag),
        );
      }
      const recorded = manifest.source?.tarballSha256 ?? "";
      if (recorded !== pin.tarballSha256) {
        fail(
          `ENGINE TARBALL MISMATCH (${label}):`,
          `payload records tarball sha256 ${recorded || "<missing>"}, but engine-pin.json pins ${pin.tarballSha256}. `
          + "Substituted tarball or stale pin — not the engine we ship. Investigate before touching the pin.",
        );
      }
      const recordedCommit = manifest.source?.commit ?? "";
      if (recordedCommit !== "" && pin.commit !== "" && recordedCommit !== pin.commit) {
        fail(
          `ENGINE COMMIT MISMATCH (${label}):`,
          `tarball payload records commit ${recordedCommit}, but engine-pin.json pins ${pin.commit}.`,
        );
      }
    }
  }

  // Release mac payloads must arrive signed; anything else cannot notarize.
  if (args.release && manifest.platform === "darwin") {
    assertNoBinariesInArchives(root, label);
    if (args.allowUnsignedPayload) {
      console.log(`[verify-vendor-payload] --allow-unsigned-payload: NOT checking payload signatures (${label}). Never ship this build.`);
    } else {
      assertPayloadIsSigned(root, label, args);
    }
  }

  const chainNote = args.release
    ? `digest chain OK: ${chain.verifiedFiles} files, ${fmtBytes(chain.verifiedBytes)} re-hashed byte-for-byte`
    : `digest chain OK: ${chain.entries.length} files pinned (structural mode; --release re-hashes all bytes)`;
  console.log(
    `[verify-vendor-payload] OK (${label}${args.release ? ", RELEASE" : ""}): `
    + `${manifest.engineTag} for ${manifest.platform}/${manifest.arch}, `
    + `${isSite ? `${Object.keys(site).join("+")} library trees` : `${manifest.wheelCount} wheels`}, `
    + `python ${manifest.python.version}, `
    + `source ${manifest.source?.kind ?? "unrecorded"}; ${chainNote}`,
  );
}

const args = parseArgs(process.argv.slice(2));
if (args.release && process.env.JARVIS_ALLOW_NO_VENDOR === "1") {
  console.warn(
    "[verify-vendor-payload] NOTE: JARVIS_ALLOW_NO_VENDOR is set but IGNORED in --release mode.",
  );
}
if (args.packed === undefined) {
  verify(args.payload, args, "pre-build");
} else {
  const packedRoot = path.resolve(args.packed, "engine-vendor");
  if (!fs.existsSync(packedRoot) && !args.release && process.env.JARVIS_ALLOW_NO_VENDOR === "1") {
    console.warn("[verify-vendor-payload] WARNING: packaged app carries no engine payload (allowed by env).");
    process.exit(0);
  }
  // Both modes demand exact hashes: the payload is signed before it is hashed,
  // so there is nothing to forgive on either side. This one additionally proves
  // that what electron-builder produced still matches what we handed it —
  // which is where a signIgnore that stopped matching would show up.
  verify(packedRoot, args, "packaged resources");
}
