import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * The trust anchor for the voice payload.
 *
 * `manifest.json` lists the expected hash of every voice file, but it lives in
 * the same mutable `resources/voice` tree as the files it vouches for — so on
 * its own it proves nothing. Anyone able to swap a model can swap the manifest
 * to match it.
 *
 * hud/scripts/fetch-voice-assets.mjs therefore also writes the manifest's own
 * SHA-256 to `build/voice-manifest-digest.json`, which electron-builder packs
 * INSIDE the ASAR. The ASAR is covered by the application's code signature, so
 * the chain of custody runs:
 *
 *     code signature -> app.asar -> manifest digest -> per-file hashes
 *
 * A missing anchor is honestly reported as "unanchored" rather than silently
 * treated as valid: development checkouts legitimately have none, and callers
 * decide what that means. Packaged builds always have one.
 */
export const VOICE_ANCHOR_FILE = "voice-manifest-digest.json";

export interface VoiceAnchor {
  manifestSha256: string;
  generatedFor: string;
  stagedAt: string;
}

export function voiceAnchorPath(): string {
  return path.resolve(__dirname, "..", "build", VOICE_ANCHOR_FILE);
}

export async function readVoiceAnchor(anchorPath = voiceAnchorPath()): Promise<VoiceAnchor | null> {
  try {
    const raw = JSON.parse(await fs.readFile(anchorPath, "utf8")) as Partial<VoiceAnchor>;
    if (typeof raw.manifestSha256 !== "string" || !/^[0-9a-f]{64}$/.test(raw.manifestSha256)) return null;
    return {
      manifestSha256: raw.manifestSha256,
      generatedFor: typeof raw.generatedFor === "string" ? raw.generatedFor : "",
      stagedAt: typeof raw.stagedAt === "string" ? raw.stagedAt : "",
    };
  } catch {
    return null;
  }
}
