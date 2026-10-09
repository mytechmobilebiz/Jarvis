/**
 * The ONE way to obtain a runnable voice payload.
 *
 * Round 2 of review found the HUD service doing this correctly while the setup
 * wizard did its own weaker version: reading a manifest out of an
 * environment-selected directory, hashing the files against that same
 * manifest, and then executing the helpers it had just "verified". A manifest
 * that vouches for itself is not verification, and `JARVIS_VOICE_ROOT` made it
 * reachable in a shipped build.
 *
 * So there is now exactly one function that resolves, verifies and unlocks the
 * payload, and both callers use it. The rules it enforces:
 *
 *  - **Packaged builds read one directory only**: `<resourcesPath>/voice`. No
 *    environment variable, no checkout fallback, no caller-supplied path.
 *  - **Packaged builds require the ASAR trust anchor.** The anchor is written
 *    at build time and lives inside app.asar, which the code signature covers.
 *    Absent or mismatched means the payload is refused outright.
 *  - **Verification is complete before anything is executed**: manifest digest,
 *    every file's SHA-256, symlink rejection, and unlisted-file rejection.
 *  - Development checkouts may use a staged tree without an anchor, and are
 *    told so explicitly rather than being reported as fully verified.
 */

import { promises as fs } from "node:fs";
import path from "node:path";
import { resolveVoicePaths, type VoicePaths } from "./voice-engine.js";
import {
  readVoiceManifestResult,
  verifyVoiceAssets,
  type VoiceManifest,
} from "./voice-assets.js";
import type { SignatureVerdict } from "./code-signature.js";

/** Closed set of reasons the payload cannot be used. */
export type VoiceLoadProblem =
  | "not-packaged"
  | "assets-missing"
  | "assets-corrupt"
  | "assets-unreadable"
  | "binary-missing"
  | "unsupported-platform"
  | "unverified";

export interface VoiceAnchor {
  manifestSha256: string;
  generatedFor?: string;
  stagedAt?: string;
}

export interface VoiceLoadOptions {
  packaged: boolean;
  /** Electron's process.resourcesPath. The ONLY root a packaged build uses. */
  resourcesPath?: string;
  /** Where a checkout stages its payload. Ignored when packaged. */
  devRoot?: string;
  /** Reads the build-time anchor from inside the ASAR. */
  readAnchor: () => Promise<VoiceAnchor | null>;
  platform?: NodeJS.Platform;
  /**
   * Allows JARVIS_VOICE_ROOT. Callers must pass `false` (or omit) in anything
   * that can run in a shipped build; it exists for local development and for
   * the smoke harnesses.
   */
  allowEnvRoot?: boolean;
  env?: NodeJS.ProcessEnv;
  /** Test seam for the packaged signature fallback; see code-signature.ts. */
  verifySignature?: (filePath: string) => Promise<SignatureVerdict>;
}

export type VoiceLoadResult =
  | {
    ok: true;
    voiceRoot: string;
    manifest: VoiceManifest;
    paths: VoicePaths;
    /** False only in a development checkout with no build-time anchor. */
    anchored: boolean;
  }
  | { ok: false; reason: VoiceLoadProblem };

export const VOICE_RESOURCES_DIRNAME = "voice";

/**
 * Resolve the payload root under the packaging rules above. Exported so the
 * decision is testable on its own — it is the part an attacker most wants to
 * influence.
 */
export function resolveVoiceRootFor(options: VoiceLoadOptions): string | null {
  if (options.packaged) {
    // Deliberately ignores devRoot AND the environment. A shipped build reads
    // the directory next to its own signed executable, or nothing.
    if (options.resourcesPath === undefined || options.resourcesPath === "") return null;
    return path.join(options.resourcesPath, VOICE_RESOURCES_DIRNAME);
  }
  const env = options.env ?? process.env;
  if (options.allowEnvRoot === true) {
    const override = env.JARVIS_VOICE_ROOT;
    if (override !== undefined && override !== "") return override;
  }
  return options.devRoot ?? null;
}

export async function loadVerifiedVoice(options: VoiceLoadOptions): Promise<VoiceLoadResult> {
  const platform = options.platform ?? process.platform;
  if (platform !== "win32" && platform !== "darwin") {
    return { ok: false, reason: "unsupported-platform" };
  }

  const voiceRoot = resolveVoiceRootFor(options);
  if (voiceRoot === null) {
    return { ok: false, reason: options.packaged ? "assets-missing" : "not-packaged" };
  }

  // ONE read, and its actual failure kept: absent, blocked and damaged are
  // three different answers with three different remedies (Codex r3/r4, P2).
  const manifestResult = await readVoiceManifestResult(voiceRoot);
  if ("problem" in manifestResult) {
    return { ok: false, reason: options.packaged ? manifestResult.problem : "not-packaged" };
  }
  const manifest = manifestResult.manifest;

  const anchor = await options.readAnchor().catch(() => null);
  // A packaged build with no anchor has nothing vouching for the payload; the
  // manifest beside it is not evidence about itself.
  if (options.packaged && anchor === null) return { ok: false, reason: "unverified" };

  const verification = await verifyVoiceAssets({
    voiceRoot,
    manifest,
    ...(anchor === null ? {} : { expectedManifestSha256: anchor.manifestSha256 }),
    platform,
    // Packaged only: signing rewrote the executables this build ships, so their
    // OS signature (pinned to our Team ID) stands in for a hash that packaging
    // itself invalidated. Every other file, and every unsigned build, is
    // verified byte-for-byte exactly as before.
    packaged: options.packaged,
    ...(options.verifySignature === undefined ? {} : { verifySignature: options.verifySignature }),
  });
  if (!verification.ok) return { ok: false, reason: verification.reason ?? "assets-corrupt" };

  const paths = resolveVoicePaths(voiceRoot, platform);
  for (const binary of [paths.whisperCli, paths.ttsBin]) {
    try {
      const info = await fs.lstat(binary);
      if (!info.isFile()) return { ok: false, reason: "binary-missing" };
    } catch {
      return { ok: false, reason: "binary-missing" };
    }
  }

  return { ok: true, voiceRoot, manifest, paths, anchored: anchor !== null };
}
