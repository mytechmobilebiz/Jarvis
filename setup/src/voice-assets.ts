/**
 * Bundled voice assets: where they live, and proof they are the ones we shipped.
 *
 * Appliance doctrine — everything is staged into the installer at BUILD time by
 * hud/scripts/fetch-voice-assets.mjs, which writes manifest.json next to them.
 * Nothing here ever reaches the network; the only question this module answers
 * is "are the bytes on this disk the bytes we signed off on".
 *
 * Hashing ~200 MB costs about a second, which is too much for every launch and
 * too little to skip forever. So: sizes are checked every time (catches the
 * common truncated-install and antivirus-quarantine cases instantly), and the
 * full SHA-256 sweep runs once per app version and is remembered in userData.
 */

import { createHash } from "node:crypto";
import { createReadStream, promises as fs } from "node:fs";
import path from "node:path";
import { SIGNABLE_EXECUTABLES, verifyCodeSignature, type SignatureVerdict } from "./code-signature.js";
import { errorCodeOf } from "./vendor.js";

/**
 * This module lives in @jarvis/setup rather than in the HUD because BOTH need
 * it — the HUD to gate the feature at launch, the setup wizard's Voice step to
 * verify the install — and the HUD already depends on setup. Putting it the
 * other way round would be a dependency cycle, and duplicating it would let
 * the two copies of the integrity rule drift apart.
 */
/**
 * `assets-unreadable` is deliberately its own reason (v1.5.1, Leg 3 audit):
 * a file that is GONE and a file that is there but cannot be read are
 * different diseases with different remedies — a reinstall fixes the first,
 * while the second is a permission or scan-lock problem a reinstall repeats.
 * v1.5.0 reported both as `assets-missing`.
 */
export type VoiceAssetProblem = "assets-missing" | "assets-corrupt" | "assets-unreadable";

export const VOICE_MANIFEST_FILE = "manifest.json";
export const VOICE_MANIFEST_VERSION = 1;

export interface VoiceAssetEntry {
  /** Path relative to the voice resources root, POSIX separators. */
  path: string;
  bytes: number;
  sha256: string;
  license: string;
  source: string;
  /** Absent means every platform. */
  platform?: NodeJS.Platform;
  /**
   * This file is EXECUTED, and packaging signs it — which rewrites it and
   * invalidates the hash above. Flagged files may therefore fall back to the
   * operating system's signature (pinned to our identity) when, and only when,
   * their hash no longer matches in a packaged build. Absent means "hash only",
   * so an older manifest can never buy the exception.
   */
  signable?: boolean;
}

/**
 * Provenance of one upstream archive the payload was built from. Recorded so
 * a build can be reproduced, or a shipped installer audited, from the manifest
 * alone — without reading the fetch script that produced it.
 */
export interface VoiceSourceEntry {
  id: string;
  file: string;
  url: string;
  bytes?: number;
  sha256: string;
}

/** How a binary we compile ourselves (macOS whisper-cli) was produced. */
export interface VoiceBuildEntry {
  from: string;
  sourceSha256: string;
  toolchain: string;
  cmakeFlags?: string[];
  note?: string;
}

export interface VoiceManifest {
  manifestVersion: number;
  generatedFor: string;
  stt: { engine: string; version: string; model: string };
  tts: {
    engine: string;
    version: string;
    model: string;
    voice: string;
    voiceId: string;
    speakerId: number;
    bundle?: string;
    quantization?: string;
    fallbackVoice?: string;
    fallbackSpeakerId?: number;
  };
  sources?: VoiceSourceEntry[];
  build?: Record<string, VoiceBuildEntry>;
  assets: VoiceAssetEntry[];
}

export interface VoiceVerification {
  ok: boolean;
  reason?: VoiceAssetProblem;
  /** Manifest-relative paths that failed, for diagnostics. Never absolute. */
  bad: string[];
  checkedHashes: boolean;
}

export function assetsForPlatform(manifest: VoiceManifest, platform: NodeJS.Platform): VoiceAssetEntry[] {
  return manifest.assets.filter((asset) => asset.platform === undefined || asset.platform === platform);
}

export async function readVoiceManifest(voiceRoot: string): Promise<VoiceManifest | null> {
  try {
    const raw = JSON.parse(await fs.readFile(path.join(voiceRoot, VOICE_MANIFEST_FILE), "utf8")) as unknown;
    return normalizeManifest(raw);
  } catch {
    return null;
  }
}

/**
 * WHY the manifest could not be used, when it could not — from the ACTUAL
 * failure rather than from a second guess afterwards.
 *
 * Three different diseases hide behind "no manifest": it is not there
 * (reinstall), it is there and blocked (unblock it — reinstalling repeats the
 * problem), or it is there and damaged/foreign (reinstall). An earlier
 * version asked `access()` after the fact, which called a malformed manifest
 * "missing" and could not see a read that failed for any reason `access`
 * tolerates (Codex review r4, P2). This reads once and keeps the answer.
 */
export async function readVoiceManifestResult(
  voiceRoot: string,
): Promise<{ manifest: VoiceManifest } | { problem: VoiceAssetProblem }> {
  let text: string;
  try {
    text = await fs.readFile(path.join(voiceRoot, VOICE_MANIFEST_FILE), "utf8");
  } catch (error) {
    const code = errorCodeOf(error);
    // EISDIR (a directory where the file belongs) is damage, not absence.
    if (code === "ENOENT" || code === "ENOTDIR") return { problem: "assets-missing" };
    if (code === "EISDIR") return { problem: "assets-corrupt" };
    return { problem: "assets-unreadable" };
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { problem: "assets-corrupt" };
  }
  const manifest = normalizeManifest(parsed);
  // Present and readable but not a manifest this build understands: damaged
  // or foreign, and a reinstall is the honest remedy.
  return manifest === null ? { problem: "assets-corrupt" } : { manifest };
}

/** The problem alone, for callers that already know there is no manifest. */
export async function voiceManifestProblem(voiceRoot: string): Promise<VoiceAssetProblem> {
  const result = await readVoiceManifestResult(voiceRoot);
  return "problem" in result ? result.problem : "assets-corrupt";
}

export function normalizeManifest(raw: unknown): VoiceManifest | null {
  if (typeof raw !== "object" || raw === null) return null;
  const source = raw as Record<string, unknown>;
  if (source.manifestVersion !== VOICE_MANIFEST_VERSION) return null;
  if (!Array.isArray(source.assets)) return null;

  const assets: VoiceAssetEntry[] = [];
  for (const entry of source.assets) {
    if (typeof entry !== "object" || entry === null) return null;
    const asset = entry as Record<string, unknown>;
    if (typeof asset.path !== "string" || typeof asset.sha256 !== "string" || typeof asset.bytes !== "number") {
      return null;
    }
    // A manifest path is a key into our own read-only tree. Refuse anything
    // that could climb out of it even though we wrote the file ourselves —
    // this is the one place a tampered resources dir gets to name a path.
    if (asset.path.includes("..") || path.posix.isAbsolute(asset.path) || /^[a-zA-Z]:/.test(asset.path)) {
      return null;
    }
    assets.push({
      path: asset.path,
      bytes: asset.bytes,
      sha256: asset.sha256,
      license: typeof asset.license === "string" ? asset.license : "unknown",
      source: typeof asset.source === "string" ? asset.source : "",
      ...(typeof asset.platform === "string" ? { platform: asset.platform as NodeJS.Platform } : {}),
      // Carried through EXPLICITLY, and only when it is literally `true`: this
      // flag is what lets a packaged build accept a signed executable whose
      // hash packaging itself changed. Normalization dropping it was invisible
      // — the payload simply failed as before, on the one path that cannot be
      // tested without a real signed build.
      ...(asset.signable === true ? { signable: true } : {}),
    });
  }

  const stt = source.stt as VoiceManifest["stt"] | undefined;
  const tts = source.tts as VoiceManifest["tts"] | undefined;
  if (stt === undefined || tts === undefined) return null;

  return {
    manifestVersion: VOICE_MANIFEST_VERSION,
    generatedFor: String(source.generatedFor ?? ""),
    stt,
    tts,
    // Provenance is documentation, not a runtime gate: it is carried through
    // when present and never a reason to reject an otherwise valid manifest.
    ...(Array.isArray(source.sources) ? { sources: source.sources as VoiceSourceEntry[] } : {}),
    ...(typeof source.build === "object" && source.build !== null
      ? { build: source.build as Record<string, VoiceBuildEntry> }
      : {}),
    assets,
  };
}

/**
 * Size-only sweep. Cheap enough to run on every launch, and it is what actually
 * catches the field failures: a half-copied install, an antivirus that deleted
 * an .exe, a user who "cleaned up" a big file.
 */
export async function verifySizes(
  voiceRoot: string,
  manifest: VoiceManifest,
  platform: NodeJS.Platform = process.platform,
): Promise<VoiceVerification> {
  const bad: string[] = [];
  let missing = false;
  let unreadable = false;
  for (const asset of assetsForPlatform(manifest, platform)) {
    try {
      const stat = await fs.stat(path.join(voiceRoot, asset.path));
      if (stat.size !== asset.bytes) bad.push(asset.path);
    } catch (error) {
      // ENOENT is an absence; EACCES/EPERM/EBUSY is a file we were not allowed
      // to read (a scan lock, a filter driver, a permissions accident). Same
      // split as the engine payload's digest check — telling a customer to
      // reinstall over a permission problem just repeats it.
      if (errorCodeOf(error) === "ENOENT") missing = true;
      else unreadable = true;
      bad.push(asset.path);
    }
  }
  if (bad.length === 0) return { ok: true, bad, checkedHashes: false };
  // Absence outranks unreadability when both happened: a missing file is the
  // bigger and more actionable finding.
  const reason: VoiceAssetProblem = missing ? "assets-missing" : unreadable ? "assets-unreadable" : "assets-corrupt";
  return { ok: false, reason, bad, checkedHashes: false };
}


/** Full SHA-256 sweep. Streamed, so peak memory stays flat on the 320 MB model. */
export interface HashVerifyOptions {
  /**
   * Packaging signs the executables it ships, which rewrites them. ONLY a
   * packaged build may fall back to a signature; a checkout, CI, and every
   * unsigned build verify every byte by hash exactly as before.
   */
  packaged?: boolean;
  /** Test seam / platform strategy; see code-signature.ts. */
  verifySignature?: (filePath: string, identifier?: string) => Promise<SignatureVerdict>;
}

/**
 * Every file is hashed. Nothing is skipped.
 *
 * A file whose hash matches is done — that is still the common case, including
 * every unsigned build. A file whose hash does NOT match fails, unless all four
 * of these hold: the build is packaged, the manifest flags that file as
 * signable, the OS says its signature is intact, and the signing identity is
 * OURS. Anything else — no signature, a broken one, a valid signature by
 * someone else, or no way to ask — is a corrupt payload.
 */
export async function verifyHashes(
  voiceRoot: string,
  manifest: VoiceManifest,
  platform: NodeJS.Platform = process.platform,
  options: HashVerifyOptions = {},
): Promise<VoiceVerification> {
  const bad: string[] = [];
  let missing = false;
  let unreadable = false;
  const checkSignature = options.verifySignature
    ?? ((filePath: string, identifier?: string) => verifyCodeSignature(filePath, {
      platform,
      ...(identifier === undefined ? {} : { identifier }),
    }));
  for (const asset of assetsForPlatform(manifest, platform)) {
    const full = path.join(voiceRoot, asset.path);
    try {
      // lstat, not stat: a symlink pointing at an attacker-chosen file would
      // otherwise be hashed through to its target and pass.
      const info = await fs.lstat(full);
      if (!info.isFile()) {
        bad.push(asset.path);
        continue;
      }
      if (info.size === asset.bytes && await sha256File(full) === asset.sha256) continue;
      // The hash failed. Only a signed executable in a packaged build gets a
      // second question asked about it — and only one of the executables this
      // product actually runs. The closed set means a different binary, even
      // one legitimately signed by us, cannot be dropped in under a helper's
      // name and inherit its permission to run (review r2, finding 3).
      const basename = path.posix.basename(asset.path);
      if (options.packaged !== true || asset.signable !== true || !SIGNABLE_EXECUTABLES.has(basename)) {
        bad.push(asset.path);
        continue;
      }
      const verdict = await checkSignature(full, SIGNABLE_EXECUTABLES.get(basename))
        .catch(() => ({ state: "unverified" as const, detail: "check threw" }));
      if (verdict.state !== "ours") bad.push(asset.path);
    } catch (error) {
      // THE LIVE PATH (Codex review r1, P2): this is the function
      // loadVerifiedVoice actually runs — verifySizes has no production
      // caller — so the missing-vs-unreadable split has to be here, or a
      // customer whose voice files are merely unreadable is told to reinstall.
      if (errorCodeOf(error) === "ENOENT") missing = true;
      else unreadable = true;
      bad.push(asset.path);
    }
  }
  if (bad.length === 0) return { ok: true, bad, checkedHashes: true };
  const reason: VoiceAssetProblem = missing ? "assets-missing" : unreadable ? "assets-unreadable" : "assets-corrupt";
  return { ok: false, reason, bad, checkedHashes: true };
}

export async function sha256File(filePath: string): Promise<string> {
  const hash = createHash("sha256");
  await new Promise<void>((resolve, reject) => {
    const stream = createReadStream(filePath);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("error", reject);
    stream.on("end", () => resolve());
  });
  return hash.digest("hex");
}

export async function sha256Of(filePath: string): Promise<string | null> {
  return sha256File(filePath).catch(() => null);
}

/**
 * Files present in the payload tree that the manifest does not list.
 *
 * Hashing only what the manifest names proves nothing about what else is in
 * the directory. An attacker who cannot alter a listed file can still DROP one
 * in — and the engine resolves several paths (the espeak data directory in
 * particular) as directories, not as individually named files.
 */
export async function findExtras(
  voiceRoot: string,
  manifest: VoiceManifest,
  platform: NodeJS.Platform = process.platform,
): Promise<string[]> {
  const expected = new Set(assetsForPlatform(manifest, platform).map((asset) => asset.path));
  expected.add(VOICE_MANIFEST_FILE);
  // The other platform's binaries are absent from a real install, but a dev
  // tree staged for both is not tampering — only bin/<other> is tolerated.
  const otherBin = platform === "win32" ? "bin/darwin/" : "bin/win32/";

  const extras: string[] = [];
  const walk = async (relative: string): Promise<void> => {
    const absolute = relative === "" ? voiceRoot : path.join(voiceRoot, relative);
    const entries = await fs.readdir(absolute, { withFileTypes: true }).catch(() => []);
    for (const entry of entries) {
      const child = relative === "" ? entry.name : `${relative}/${entry.name}`;
      if (entry.isSymbolicLink()) {
        extras.push(child);
        continue;
      }
      if (entry.isDirectory()) {
        await walk(child);
        continue;
      }
      if (!expected.has(child) && !child.startsWith(otherBin)) extras.push(child);
    }
  };
  await walk("");
  return extras;
}

/**
 * Trusted verification of the whole payload.
 *
 * The trust anchor matters as much as the hashing. manifest.json lives in the
 * same mutable resources tree as the files it vouches for, so on its own it
 * proves nothing: anyone who can swap a model can swap the manifest to match.
 * `expectedManifestSha256` is the digest recorded at BUILD time inside the
 * ASAR, which is covered by the application's code signature — so the chain
 * runs signature -> ASAR -> manifest digest -> per-file hashes.
 *
 * There is deliberately no cached "already verified" fast path. The previous
 * design remembered a successful sweep and then accepted a same-sized
 * replacement forever afterwards, which is precisely the case a quarantine-
 * and-restore or a swapped model produces. Measured cost of the full sweep is
 * a few hundred milliseconds, paid once per process, off the boot path.
 */
export async function verifyVoiceAssets(options: {
  voiceRoot: string;
  manifest: VoiceManifest;
  /** Build-time digest of manifest.json, read from inside the signed ASAR. */
  expectedManifestSha256?: string;
  platform?: NodeJS.Platform;
  /** See HashVerifyOptions: signature fallback is packaged-only. */
  packaged?: boolean;
  verifySignature?: (filePath: string, identifier?: string) => Promise<SignatureVerdict>;
}): Promise<VoiceVerification> {
  const platform = options.platform ?? process.platform;

  if (options.expectedManifestSha256 !== undefined) {
    const actual = await sha256Of(path.join(options.voiceRoot, VOICE_MANIFEST_FILE));
    if (actual !== options.expectedManifestSha256) {
      return {
        ok: false,
        // A manifest we could not hash is either gone or blocked; the same
        // question, asked the same way, as everywhere else in this module.
        reason: actual === null ? await voiceManifestProblem(options.voiceRoot) : "assets-corrupt",
        bad: [VOICE_MANIFEST_FILE],
        checkedHashes: true,
      };
    }
  }

  const hashes = await verifyHashes(options.voiceRoot, options.manifest, platform, {
    ...(options.packaged === undefined ? {} : { packaged: options.packaged }),
    ...(options.verifySignature === undefined ? {} : { verifySignature: options.verifySignature }),
  });
  if (!hashes.ok) return hashes;

  const extras = await findExtras(options.voiceRoot, options.manifest, platform);
  if (extras.length > 0) {
    return { ok: false, reason: "assets-corrupt", bad: extras.slice(0, 10), checkedHashes: true };
  }
  return hashes;
}
