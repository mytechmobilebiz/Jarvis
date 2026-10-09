import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { type StepOutcome, type WizardContext } from "../context.js";
import { createVoiceEngine } from "../voice-engine.js";
import { loadVerifiedVoice, type VoiceAnchor, type VoiceLoadProblem, type VoiceLoadResult } from "../voice-load.js";

/**
 * Voice step, v1.4.0 — a verification step, not an installer.
 *
 * Everything voice needs now ships inside the application: whisper.cpp and a
 * Kokoro TTS engine as bundled child executables, with their models. So this
 * step no longer installs anything. It confirms the bundled payload is intact
 * and actually runs, on Windows exactly as on macOS.
 *
 * What went away, deliberately:
 *  - the blanket `win32` skip. Voice was the single biggest gap between what
 *    the ads promised and what Windows buyers got, and it cost a refund.
 *  - `faster-whisper`, `edge-tts`, PortAudio and the whole Python voice extra.
 *    edge-tts in particular was never local: it streams every spoken reply to
 *    a Microsoft endpoint, which contradicts "local speech" in our own copy
 *    and carries no commercial service grant.
 *
 * The step is never fatal. A machine that fails every check here still has a
 * completely working typed JARVIS, and says so.
 */

export interface VoiceProbe {
  /** Resolves and fully verifies the payload, or explains why it cannot. */
  load: () => Promise<VoiceLoadResult>;
  /** Runs the two engines against an already-verified payload. */
  selfTest: (loaded: Extract<VoiceLoadResult, { ok: true }>) => Promise<{ stt: boolean; tts: boolean }>;
}

export const VOICE_PRIVACY_NOTE =
  "Voice recognition and speech generation run locally on your computer. "
  + "Your request still goes to the AI provider you selected.";

export async function voice(
  ctx: WizardContext,
  platform: NodeJS.Platform = process.platform,
  probe?: VoiceProbe,
): Promise<StepOutcome> {
  const notes: string[] = [];

  if (platform !== "win32" && platform !== "darwin") {
    return {
      status: "skipped",
      notes: [`Bundled voice ships for Windows and macOS; this is ${platform}. Everything else works normally.`],
      props: { voice_platform: "unsupported" },
    };
  }

  if (ctx.dryRun) {
    return {
      status: "skipped",
      notes: [
        "[dry-run] would verify the bundled voice models and run a one-sentence speech test.",
        VOICE_PRIVACY_NOTE,
      ],
      props: { voice_dry_run: true },
    };
  }

  const resolved = probe ?? defaultProbe(ctx, platform);
  const loaded = await resolved.load();

  if (!loaded.ok) {
    if (loaded.reason === "not-packaged") {
      // The wizard also runs from a source checkout and the repo zip, where the
      // voice payload is a build artifact rather than part of the tree. Calling
      // that "broken" would be false.
      return {
        status: "skipped",
        notes: [
          "Voice assets ship inside the installed JARVIS application, and this is a source run — nothing to verify here.",
          "Install JARVIS from the installer to get voice, or stage the assets in a checkout with: node hud/scripts/fetch-voice-assets.mjs",
        ],
        props: { voice_assets_ok: false, voice_source_run: true },
      };
    }
    if (loaded.reason === "unsupported-platform") {
      return {
        status: "skipped",
        notes: ["Bundled voice ships for Windows and macOS only."],
        props: { voice_platform: "unsupported" },
      };
    }
    return failure(...describeLoadFailure(loaded.reason));
  }

  notes.push(loaded.anchored
    ? `Bundled voice files verified against this build's signed manifest: ${loaded.manifest.assets.length} files.`
    : `Bundled voice files verified against the staged manifest (development build, no signed anchor): ${loaded.manifest.assets.length} files.`);

  const selfTest = await resolved.selfTest(loaded);
  if (!selfTest.tts || !selfTest.stt) {
    // Files intact but the engines will not run: almost always a blocked
    // executable or an unusual CPU. Report exactly which half failed.
    return failure(
      selfTest.tts
        ? "The speech engine speaks, but speech recognition did not run on this machine."
        : selfTest.stt
          ? "Speech recognition works, but the speech engine did not run on this machine."
          : "The bundled speech programs did not run on this machine.",
      "Allow JARVIS through your antivirus or endpoint protection, then rerun this step. Typed chat is unaffected either way.",
      { voice_assets_ok: true, tts_selftest: selfTest.tts, stt_selftest: selfTest.stt },
    );
  }

  notes.push("Spoke a test sentence and ran a recognition self-test — both engines work.");
  notes.push("In the HUD: hold the microphone button in the chat bar (or hold Space) to talk, and Jarvis answers aloud.");
  notes.push(VOICE_PRIVACY_NOTE);
  return {
    status: "done",
    notes,
    props: { voice_assets_ok: true, tts_selftest: true, stt_selftest: true, voice_anchored: loaded.anchored },
  };
}

function failure(
  message: string,
  hint: string,
  props: Record<string, string | number | boolean>,
): StepOutcome {
  return {
    status: "failed",
    notes: [`${message} Fix: ${hint}`, "Typed chat, Slack and the terminal are unaffected."],
    // Never fatal: voice is the one step whose failure must not stop setup.
    fatal: false,
    // ...and never a reason to call the whole RUN failed, which would reopen
    // the wizard on every launch and contradict the sentence above it
    // (integration review r1, finding 3). The step still shows red and still
    // reruns next time; setup itself is finished.
    degraded: true,
    props,
  };
}

/** Customer-facing wording for each way the payload can fail to load. */
export function describeLoadFailure(
  reason: VoiceLoadProblem,
): [string, string, Record<string, string | number | boolean>] {
  if (reason === "unverified") {
    return [
      "The bundled voice files could not be proven to be the ones this build shipped, so they were not run.",
      "Reinstall JARVIS from the official installer.",
      { voice_assets_ok: false, voice_reason: "unverified" },
    ];
  }
  if (reason === "assets-missing") {
    return [
      "The bundled voice files were not found in this installation.",
      "Reinstall JARVIS from the installer; the voice payload ships inside it.",
      { voice_assets_ok: false, voice_reason: "missing" },
    ];
  }
  // Present but unreadable is NOT a mismatch (Codex review r2, P2): the files
  // are the right files, this computer simply would not let us read them, and
  // "reinstall" repeats a permission or scan-lock problem rather than fixing
  // it. Without this branch the new reason fell through to the corruption
  // copy and recorded voice_reason=mismatch — the wrong diagnosis AND the
  // wrong remedy, which is exactly what the split was added to stop.
  if (reason === "assets-unreadable") {
    return [
      "Some bundled voice files are on this computer but could not be read.",
      "Allow JARVIS in your antivirus or security software, then run setup again. Reinstalling will not help if the files are being blocked.",
      { voice_assets_ok: false, voice_reason: "unreadable" },
    ];
  }
  if (reason === "binary-missing") {
    return [
      "The local speech programs are missing from this installation.",
      "Reinstall JARVIS from the installer.",
      { voice_assets_ok: false, voice_reason: "binary-missing" },
    ];
  }
  return [
    "Some bundled voice files do not match this build.",
    "Antivirus quarantine is the usual cause. Reinstall JARVIS, and allow-list the JARVIS folder if the check fails again.",
    { voice_assets_ok: false, voice_reason: "mismatch" },
  ];
}

/**
 * The real probe, built on the SAME verified-load function the HUD service
 * uses. Round 2 found this step running its own weaker version: it read a
 * manifest out of an environment-selected directory, hashed the files against
 * that same manifest, and executed the helpers it had just "verified".
 *
 * Now there is one code path. In a packaged build it reads only
 * `<resourcesPath>/voice`, requires the anchor from inside the signed ASAR,
 * and refuses to execute anything that does not verify completely.
 */
function defaultProbe(ctx: WizardContext, platform: NodeJS.Platform): VoiceProbe {
  const packaged = ctx.packaged;
  return {
    load: () => loadVerifiedVoice({
      packaged,
      ...(electronResourcesPath() === undefined ? {} : { resourcesPath: electronResourcesPath() as string }),
      devRoot: path.join(ctx.repoRoot, "hud", "voice-assets"),
      readAnchor: () => readVoiceAnchorFrom(voiceAnchorPathFor(ctx, packaged)),
      platform,
      // JARVIS_VOICE_ROOT is a development convenience and must never be
      // reachable in a shipped build.
      allowEnvRoot: !packaged,
    }),
    selfTest: async (loaded) => {
      const engine = createVoiceEngine({
        paths: loaded.paths,
        manifest: loaded.manifest,
        cacheDir: path.join(ctx.hermesRoot, "voice-selftest"),
      });
      // Registered with the host so quitting mid-self-test kills the helper
      // instead of orphaning it: this engine is created here, not by the HUD
      // service, so before-quit cannot otherwise reach it.
      const release = ctx.registerDisposable?.(engine);
      try {
        return await engine.selfTest();
      } finally {
        engine.dispose();
        release?.();
      }
    },
  };
}

/**
 * Where the build-time trust anchor lives, packaged and unpackaged.
 *
 * Packaged, it is at `<resources>/app.asar/build/voice-manifest-digest.json`.
 * The previous version walked four parents up from this module's own location
 * — `app.asar/node_modules/@jarvis/setup/dist/steps/voice.js` — which lands on
 * `app.asar/node_modules/@jarvis/build/…`, a path that does not exist. The
 * packaged wizard therefore reported voice as unverified and skipped its
 * self-test, while the HUD's own reader (which resolves from resourcesPath)
 * found it (integration review r2, finding 5).
 *
 * So the packaged path is anchored to `process.resourcesPath`, the same root
 * the HUD uses, with the module-relative walk kept only as a fallback for a
 * host that exposes no resourcesPath.
 */
export function voiceAnchorPathFor(ctx: WizardContext, packaged: boolean): string {
  if (!packaged) return path.join(ctx.repoRoot, "hud", "build", VOICE_ANCHOR_FILE);
  const resources = electronResourcesPath();
  if (resources !== undefined && resources !== "") {
    return path.join(resources, "app.asar", "build", VOICE_ANCHOR_FILE);
  }
  // No guessed fallback. A packaged run without resourcesPath is not a shape we
  // ship, and a wrong path here does not fail loudly — it silently reports the
  // payload as unverified, which is exactly the bug this replaced (review r3,
  // P3). An empty path cannot be read, so the caller reports "unverified" for a
  // reason that is true.
  return "";
}

export const VOICE_ANCHOR_FILE = "voice-manifest-digest.json";

/** Present only under Electron; the CLI wizard has none. */
function electronResourcesPath(): string | undefined {
  return (process as NodeJS.Process & { resourcesPath?: string }).resourcesPath;
}

export async function readVoiceAnchorFrom(anchorPath: string): Promise<VoiceAnchor | null> {
  try {
    const raw = JSON.parse(await fs.readFile(anchorPath, "utf8")) as Partial<VoiceAnchor>;
    if (typeof raw.manifestSha256 !== "string" || !/^[0-9a-f]{64}$/.test(raw.manifestSha256)) return null;
    return { manifestSha256: raw.manifestSha256 };
  } catch {
    return null;
  }
}

// The online Microsoft speech client, the Python transcription package and the
// system audio library this step used to install are intentionally gone (their
// names are spelled out in the header comment and asserted absent by
// voice-offline.test.ts). Do not reintroduce them: the product promise is local
// speech, and each of those pulled it back online or into a Python dependency
// graph resolved on the customer's machine.
