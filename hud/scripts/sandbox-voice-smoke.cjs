/**
 * sandbox-voice-smoke.cjs — play one real clip through the REAL preload,
 * in a REALLY SANDBOXED renderer.
 *
 * Why this exists: every v1.4.0 install shipped with silent spoken replies and
 * a dead brief PLAY button. The preload's `pathToFileUrl` called
 * `pathToFileURL` from `"url"` — which does not exist in the sandboxed
 * preload's browser-side `url` shim — so the path→URL conversion threw on
 * every reply, between successful synthesis and playback, and the speech
 * queue's deliberately quiet onError turned it into pure silence
 * (spec/v150/VOICE-DIAG-260815.md). Nothing automated ever ran the real
 * preload under a sandboxed renderer AND exercised that call: unit tests ran
 * under plain Node (where pathToFileURL exists), and the window smokes never
 * touched the audio path. This gate closes exactly that gap.
 *
 * What it proves, end to end:
 *   1. the shipped preload boots under `sandbox: true` (not the workspace
 *      copy when `--packed` is given — the one inside app.asar),
 *   2. `window.jarvis.pathToFileUrl()` resolves to a playable file:// URL
 *      (the v1.4.0 regression, asserted directly),
 *   3. a clip ACTUALLY PLAYS to its `ended` event in that renderer —
 *      with `--clip real`, a clip synthesized by the real voice engine via
 *      the real `voice:prepare-brief` contract (audioUrl minted in main).
 *
 * Modes:
 *   --packed <resourcesDir>   use <resources>/app.asar/dist/preload.cjs
 *   --clip real|generated     real = synthesize via the staged voice payload
 *                             (default; requires host-runnable binaries, i.e.
 *                             darwin on the build mac). generated = a WAV this
 *                             script writes itself — used for the WINDOWS
 *                             packed artifact, whose sherpa binaries cannot
 *                             execute on the mac build host; the preload,
 *                             sandbox, conversion and playback checks are
 *                             identical and still run against the win asar.
 *
 *   pnpm --dir hud exec electron scripts/sandbox-voice-smoke.cjs
 */

const { app, BrowserWindow, ipcMain } = require("electron");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
const { pathToFileURL } = require("node:url");

app.commandLine.appendSwitch("autoplay-policy", "no-user-gesture-required");
// A build gate must not hang a release on a stalled GPU process.
app.disableHardwareAcceleration();

const HUD_ROOT = path.resolve(__dirname, "..");
const packedIndex = process.argv.indexOf("--packed");
const PACKED_RESOURCES = packedIndex === -1 ? undefined : path.resolve(process.cwd(), process.argv[packedIndex + 1]);
const clipIndex = process.argv.indexOf("--clip");
const CLIP_MODE = clipIndex === -1 ? "real" : process.argv[clipIndex + 1];
const DEADLINE_MS = 120_000;

const checks = [];
function check(label, ok, detail) {
  checks.push({ label, ok });
  console.log(`${ok ? "ok  " : "FAIL"}  ${label}${ok || detail === undefined ? "" : `  — ${detail}`}`);
}

/** A 1-second 24 kHz mono PCM16 sine WAV, written with no dependencies. */
function writeGeneratedWav(target) {
  const rate = 24_000;
  const samples = rate;
  const data = Buffer.alloc(samples * 2);
  for (let i = 0; i < samples; i += 1) {
    data.writeInt16LE(Math.round(Math.sin((2 * Math.PI * 440 * i) / rate) * 12_000), i * 2);
  }
  const header = Buffer.alloc(44);
  header.write("RIFF", 0);
  header.writeUInt32LE(36 + data.length, 4);
  header.write("WAVEfmt ", 8);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(rate, 24);
  header.writeUInt32LE(rate * 2, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36);
  header.writeUInt32LE(data.length, 40);
  fs.writeFileSync(target, Buffer.concat([header, data]));
  return target;
}

app.whenReady().then(async () => {
  const deadline = setTimeout(() => {
    console.error(`sandbox-voice-smoke exceeded ${DEADLINE_MS}ms`);
    app.exit(1);
  }, DEADLINE_MS);
  deadline.unref?.();

  const preloadPath = PACKED_RESOURCES === undefined
    ? path.join(HUD_ROOT, "dist", "preload.cjs")
    : path.join(PACKED_RESOURCES, "app.asar", "dist", "preload.cjs");
  console.log(`preload: ${preloadPath}`);
  console.log(`clip mode: ${CLIP_MODE}`);
  if (PACKED_RESOURCES !== undefined) {
    const target = JSON.parse(fs.readFileSync(path.join(PACKED_RESOURCES, "voice", "manifest.json"), "utf8")).generatedFor;
    const packages = target === "darwin" ? ["darwin-arm64", "darwin-x64"] : ["win-x64"];
    const libraries = target === "darwin"
      ? ["sherpa-onnx.node", "libonnxruntime.dylib", "libsherpa-onnx-c-api.dylib", "libsherpa-onnx-cxx-api.dylib"]
      : ["sherpa-onnx.node", "onnxruntime.dll", "onnxruntime_providers_shared.dll", "sherpa-onnx-c-api.dll", "sherpa-onnx-cxx-api.dll"];
    for (const platform of packages) for (const file of libraries) {
      const nativeFile = path.join(PACKED_RESOURCES, "app.asar.unpacked", "node_modules", `sherpa-onnx-${platform}`, file);
      check(`packed warm TTS ${platform}/${file}`, fs.existsSync(nativeFile), nativeFile);
    }
  }


  // --- main-process surface: the REAL contract shape -------------------------
  // path:to-file-url mirrors hud/src/main.ts exactly: absolute paths only,
  // conversion by real node:url IN MAIN. voice:prepare-brief serves a clip with its
  // audioUrl minted here, same as the product handler.
  ipcMain.handle("path:to-file-url", (_event, filePath) => {
    const value = String(filePath ?? "");
    if (value === "" || !path.isAbsolute(value)) throw new Error("Only absolute paths can be converted to a file URL.");
    return pathToFileURL(value).toString();
  });

  let service = null;
  let generatedClip = null;
  if (CLIP_MODE === "real") {
    // The real service, built from the same compiled module the app runs.
    const voiceMain = await import(pathToFileURL(path.join(HUD_ROOT, "dist", "voice-main.js")).toString());
    const voiceResources = await import(pathToFileURL(path.join(HUD_ROOT, "dist", "voice-resources.js")).toString());
    // Packed mode runs the service EXACTLY as the packaged app does —
    // packaged:true + resourcesPath — because the packed tree's executables
    // are re-signed during packaging and are verified BY SIGNATURE there;
    // dev-mode hash verification would (correctly) refuse them. The anchor is
    // read from the workspace build/ file, which the release checklist's ASAR
    // anchor diff proves byte-equal to the packed one. Workspace mode runs
    // dev-shape against the staged hud/voice-assets tree.
    service = PACKED_RESOURCES === undefined
      ? voiceMain.createVoiceService({
        voiceRoot: voiceResources.resolveVoiceRoot({ packaged: false }),
        userDataDir: fs.mkdtempSync(path.join(os.tmpdir(), "sandbox-voice-smoke-")),
        appVersion: "smoke",
        packaged: false,
        platform: process.platform,
      })
      : voiceMain.createVoiceService({
        voiceRoot: path.join(PACKED_RESOURCES, "voice"),
        resourcesPath: PACKED_RESOURCES,
        userDataDir: fs.mkdtempSync(path.join(os.tmpdir(), "sandbox-voice-smoke-")),
        appVersion: "smoke",
        packaged: true,
        platform: process.platform,
      });
    const status = await service.init();
    check("the real voice engine initialises from the staged payload", status.available, status.detail);
    if (!status.available) {
      console.log(`\n${checks.filter((c) => c.ok).length}/${checks.length} checks passed`);
      app.exit(1);
      return;
    }
    ipcMain.handle("voice:prepare-brief", async (_event, date, text) => {
      const audioPath = await service.prepareBrief(String(date ?? ""), String(text ?? "")).catch(() => null);
      return audioPath === null ? null : { audioPath, audioUrl: pathToFileURL(audioPath).toString() };
    });
  } else if (CLIP_MODE === "generated") {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "sandbox-voice-smoke-"));
    generatedClip = writeGeneratedWav(path.join(dir, "say-smoke.wav"));
  } else {
    console.error(`unknown --clip mode: ${CLIP_MODE}`);
    app.exit(1);
    return;
  }

  // --- the sandboxed window ---------------------------------------------------
  // `sandbox: true` is EXPLICIT so this gate keeps meaning "the preload works
  // sandboxed" even if Electron's default ever changes. The product window
  // relies on the default (main.ts sets no sandbox key), which today is on —
  // the exact configuration that shipped the silent-voice bug.
  const window = new BrowserWindow({
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      offscreen: true,
      preload: preloadPath,
    },
  });
  const pageErrors = [];
  window.webContents.on("console-message", (_event, level, message) => {
    if (level >= 2 && !/electronjs\.org\/docs\/tutorial\/security|Security Warning|unsafe-eval|once the app is packaged|Autofill/i.test(message)) {
      pageErrors.push(message);
    }
  });
  window.webContents.on("preload-error", (_event, _path, error) => {
    pageErrors.push(`preload-error: ${error?.message ?? error}`);
  });

  await window.loadFile(path.join(__dirname, "lib", "sandbox-voice-page.html"));
  check("the shipped preload boots in a sandboxed renderer (no preload/page errors)",
    pageErrors.length === 0, pageErrors.slice(0, 2).join(" | "));

  // --- 1. the regression check: pathToFileUrl through the sandboxed preload --
  const probePath = CLIP_MODE === "real" ? path.join(os.tmpdir(), "probe.wav") : generatedClip;
  const conversion = await window.webContents.executeJavaScript(`(async () => {
    try {
      const url = await window.jarvis.pathToFileUrl(${JSON.stringify(probePath)});
      return { ok: true, url };
    } catch (error) {
      return { ok: false, url: String(error && error.message || error) };
    }
  })()`);
  check("pathToFileUrl works through the sandboxed preload (the v1.4.0 silent-voice bug)",
    conversion.ok && /^file:\/\//.test(conversion.url), conversion.url);

  // --- 2. a clip actually plays to `ended` ------------------------------------
  const playScript = (urlExpression) => `(async () => {
    try {
      ${urlExpression}
      const audio = new Audio(url);
      const ended = new Promise((resolve, reject) => {
        audio.addEventListener("ended", () => resolve());
        audio.addEventListener("error", () => reject(new Error("media error: " + (audio.error && audio.error.code))));
        setTimeout(() => reject(new Error("playback did not end within 60s")), 60000);
      });
      await audio.play();
      await ended;
      return { ok: true, detail: "duration " + audio.duration.toFixed(2) + "s", url };
    } catch (error) {
      return { ok: false, detail: String(error && error.message || error), url: typeof url === "string" ? url : "" };
    }
  })()`;

  if (CLIP_MODE === "real") {
    const played = await window.webContents.executeJavaScript(playScript(`
      const brief = await window.jarvis.voice.prepareBrief("2026-08-13", "Good morning, sir. All systems are running.");
      if (!brief || typeof brief.audioUrl !== "string" || !/^file:/.test(brief.audioUrl)) {
        throw new Error("voice:prepare-brief returned no playable audioUrl: " + JSON.stringify(brief));
      }
      const url = brief.audioUrl;`));
    check("a REAL pre-generated brief plays to its ended event in the sandboxed renderer", played.ok, played.detail);
  } else {
    const played = await window.webContents.executeJavaScript(playScript(`
      const url = await window.jarvis.pathToFileUrl(${JSON.stringify(generatedClip)});`));
    check("a clip converted by the sandboxed preload plays to its ended event", played.ok, played.detail);
  }

  service?.dispose?.();
  const failed = checks.filter((entry) => !entry.ok).length;
  console.log(`\n${checks.length - failed}/${checks.length} checks passed`);
  app.exit(failed === 0 ? 0 : 1);
}).catch((error) => {
  console.error(error);
  app.exit(1);
});
