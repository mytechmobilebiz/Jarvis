import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export const VOICE_RESOURCES_DIRECTORY = "voice";
/** Where fetch-voice-assets.mjs stages a checkout's payload. Gitignored. */
export const VOICE_STAGING_DIRECTORY = "voice-assets";

/**
 * Packaged builds read the read-only tree electron-builder placed in
 * Resources/voice. A checkout reads hud/voice-assets, which the developer
 * stages with `node hud/scripts/fetch-voice-assets.mjs`.
 *
 * Kept deliberately shallow (spec: `resources/voice/models/...`). Deep
 * Hugging-Face-style cache paths are a known source of Windows MAX_PATH
 * failures and of antivirus heuristics firing on unfamiliar directory shapes.
 */
export function resolveVoiceRoot(options: { packaged: boolean; resourcesPath?: string }): string {
  if (options.packaged) {
    return path.join(options.resourcesPath ?? process.cwd(), VOICE_RESOURCES_DIRECTORY);
  }
  return path.resolve(__dirname, "..", VOICE_STAGING_DIRECTORY);
}
