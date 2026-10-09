/**
 * Voice wiring smoke test, inside a real Electron main process.
 *
 *   pnpm --dir hud exec electron scripts/voice-electron-smoke.cjs
 *
 * The unit tests cover the logic and voice-smoke.mjs covers the engines, but
 * neither runs under Electron. This does, because the things that break there
 * are Electron-specific: whether the permission handlers can actually be
 * registered on the default session, whether the voice payload resolves from
 * the app's idea of its own resources, and whether spawning the bundled
 * (signed, hardened-runtime) child processes works from inside the app.
 *
 * Headless: no window, no microphone, no speaker.
 *
 * CommonJS on purpose. An ESM main entry that calls app.whenReady() at top
 * level deadlocks: Electron does not emit `ready` until the entry module has
 * finished evaluating, and the top-level await never returns. The dist modules
 * are ESM and are pulled in with dynamic import() below.
 */

const { app, session } = require("electron");
const fs = require("node:fs/promises");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const HUD_ROOT = path.resolve(__dirname, "..");
const checks = [];

function check(name, ok, detail = "") {
  checks.push({ name, ok });
  console.log(`${ok ? "ok  " : "FAIL"}  ${name}${detail === "" ? "" : `  — ${detail}`}`);
}

async function exists(target) {
  try {
    await fs.access(target);
    return true;
  } catch {
    return false;
  }
}

const load = (relative) => import(pathToFileURL(path.join(HUD_ROOT, relative)).href);

async function main() {
  const { createMicGate, createVoiceService, decideVoicePermission } = await load("dist/voice-main.js");
  const { resolveVoiceRoot } = await load("dist/voice-resources.js");
  const { assertPlayableWav } = await import("@jarvis/setup/voice-engine");

  const voiceRoot = resolveVoiceRoot({ packaged: app.isPackaged, resourcesPath: process.resourcesPath });
  check("voice payload resolves", await exists(voiceRoot), voiceRoot);

  // Both handlers must attach; Electron throws if the signature is wrong.
  const gate = createMicGate();
  session.defaultSession.setPermissionRequestHandler((_contents, permission, callback) => {
    callback(decideVoicePermission({ permission, isMainFrame: true, requestingUrl: "file:///x" }, gate));
  });
  session.defaultSession.setPermissionCheckHandler((_contents, permission) =>
    decideVoicePermission({ permission, isMainFrame: true, requestingUrl: "file:///x" }, gate));
  check("permission handlers register on the default session", true);
  check("the mic gate is closed until a press arms it",
    decideVoicePermission({ permission: "media", isMainFrame: true, requestingUrl: "file:///x" }, gate) === false);

  // The real Chromium sequence around one getUserMedia: several capability
  // CHECKS, then one permission REQUEST. Only the request may consume.
  const seqGate = createMicGate();
  const owner = { contentsId: 42, frameId: 1 };
  const asCheck = { permission: "media", isMainFrame: true, requestingUrl: "file:///x", mediaType: "audio", contentsId: 42, frameId: 1, consuming: false };
  const asRequest = { permission: "media", isMainFrame: true, requestingUrl: "file:///x", mediaTypes: ["audio"], contentsId: 42, frameId: 1, consuming: true };
  seqGate.arm(owner);
  const checkA = decideVoicePermission(asCheck, seqGate);
  const checkB = decideVoicePermission(asCheck, seqGate);
  const granted = decideVoicePermission(asRequest, seqGate);
  const replay = decideVoicePermission(asRequest, seqGate);
  check("checks pass, the request is granted once, and a replay is denied",
    checkA && checkB && granted && !replay, `${checkA}/${checkB}/${granted}/${replay}`);

  const videoGate = createMicGate();
  videoGate.arm(owner);
  check("a camera check during an armed mic press is denied",
    decideVoicePermission({ ...asCheck, mediaType: "video" }, videoGate) === false);

  const service = createVoiceService({
    voiceRoot,
    resourcesPath: process.resourcesPath,
    userDataDir: app.getPath("userData"),
    appVersion: app.getVersion(),
    packaged: app.isPackaged,
  });

  const started = Date.now();
  const status = await service.init();
  check("asset verification completes", true, `${Date.now() - started} ms, available=${status.available}`);
  check("voice reports available", status.available, status.detail);
  check("status carries no filesystem path", !status.detail.includes(voiceRoot));

  if (status.available) {
    const briefText = "Revenue is twelve thousand four hundred. Three invoices are overdue.";
    const brief = await service.prepareBrief("2026-08-13", briefText);
    check("brief pre-generation returns a clip", typeof brief === "string" && brief.endsWith(".wav"));
    if (brief !== null) {
      await assertPlayableWav(brief);
      check("brief pre-generation produces a playable WAV", true);
      check("audio is written under userData, not into the read-only bundle",
        brief.startsWith(app.getPath("userData")));
    }

    const cachedStart = Date.now();
    await service.prepareBrief("2026-08-13", briefText);
    const cachedMs = Date.now() - cachedStart;
    check("cached brief replay is instant", cachedMs < 100, `${cachedMs} ms`);

    const selfTest = await service.selfTest();
    check("self-test passes both engines", selfTest.stt && selfTest.tts, JSON.stringify(selfTest));
  }

  // A PHYSICALLY mutated payload, not a stubbed status object. This is the
  // real antivirus-quarantine / swapped-model case: the file is byte-edited on
  // disk and the service must refuse to run the engines against it.
  if (status.available) {
    const victim = path.join(voiceRoot, "models", "kokoro", "tokens.txt");
    const original = await fs.readFile(victim);
    // Same LENGTH, different bytes: defeats any size-only check and is exactly
    // what a tampered model looks like.
    const mutated = Buffer.from(original);
    mutated[mutated.length - 1] = mutated[mutated.length - 1] ^ 0xff;
    await fs.writeFile(victim, mutated);
    try {
      const tampered = createVoiceService({
        voiceRoot,
        resourcesPath: process.resourcesPath,
        userDataDir: app.getPath("userData"),
        appVersion: app.getVersion(),
        packaged: app.isPackaged,
      });
      const tamperedStatus = await tampered.init();
      check("a byte-mutated payload file is detected and voice refuses to run",
        !tamperedStatus.available && tamperedStatus.reason === "assets-corrupt", tamperedStatus.reason);
    } finally {
      await fs.writeFile(victim, original);
    }

    // An extra file nobody listed — the engine hands whole directories to the
    // child, so a dropped-in DLL is as dangerous as an edited one.
    const planted = path.join(voiceRoot, "models", "kokoro", "planted.bin");
    await fs.writeFile(planted, "not ours");
    try {
      const extra = createVoiceService({
        voiceRoot,
        resourcesPath: process.resourcesPath,
        userDataDir: app.getPath("userData"),
        appVersion: app.getVersion(),
        packaged: app.isPackaged,
      });
      const extraStatus = await extra.init();
      check("an unlisted file planted in the payload is detected", !extraStatus.available, extraStatus.reason);
    } finally {
      await fs.rm(planted, { force: true });
    }
  }

  // The failure path matters as much as the happy one: voice must degrade, not
  // throw, when the payload is not there.
  const broken = createVoiceService({
    voiceRoot: path.join(app.getPath("userData"), "no-voice-here"),
    userDataDir: app.getPath("userData"),
    appVersion: app.getVersion(),
    packaged: true,
  });
  const brokenStatus = await broken.init();
  check("a missing payload degrades instead of throwing",
    !brokenStatus.available && brokenStatus.detail.length > 0, brokenStatus.reason);
  await broken.transcribe(Buffer.alloc(32_000)).then(
    () => check("transcribe rejects when voice is unavailable", false),
    () => check("transcribe rejects when voice is unavailable", true),
  );
}

app.whenReady()
  .then(main)
  .catch((error) => {
    check("smoke ran without throwing", false, (error && error.stack) || String(error));
  })
  .finally(() => {
    const failed = checks.filter((entry) => !entry.ok).length;
    console.log(`\n${checks.length - failed}/${checks.length} checks passed`);
    app.exit(failed === 0 ? 0 : 1);
  });
