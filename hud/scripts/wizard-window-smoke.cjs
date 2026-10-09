/**
 * wizard-window-smoke.cjs — open the page every customer opens FIRST.
 *
 * The first-run wizard shipped blank in every packaged v1.4.0 build. One bare
 * import in `setup-copy.ts` threw `Failed to resolve module specifier` in the
 * renderer, the module graph died before drawing anything, and nothing else
 * noticed: the main process logged nothing, no crash was recorded, the tests
 * passed, and a notarized DMG went out. It was found by the first human to open
 * the page — after CI, after signing, after notarization.
 *
 * The reason nothing caught it is embarrassing and simple: the only renderer
 * smoke we had loads `index.html` (the HUD). The wizard page — the very first
 * thing a customer sees — was never opened by anything automated.
 *
 * So this opens BOTH pages, in Electron, and fails on:
 *   - any uncaught exception or error-level console message in the renderer,
 *   - a page whose scaffolding never rendered,
 *   - a renderer that never executed its own module graph.
 *
 * It deliberately does NOT need a working setup host: the failure class it
 * exists for happens before any IPC, when the browser resolves imports.
 *
 *   pnpm --dir hud exec electron scripts/wizard-window-smoke.cjs
 */
const { app, BrowserWindow, ipcMain } = require("electron");

// The wizard plays baked voice cues from open (2026-08-14); the smoke proves
// they can AUTOPLAY in this exact app shape, so it needs the same policy flag
// the real main process sets.
app.commandLine.appendSwitch("autoplay-policy", "no-user-gesture-required");
const path = require("node:path");

// A build gate must not be able to hang a release. Offscreen rendering still
// touches graphics; without this a stalled GPU process blocks the cut with no
// output at all (review finding 5a).
app.disableHardwareAcceleration();

/**
 * `--packed <resourcesDir>` runs the SAME checks against what electron-builder
 * actually produced, rather than against the repository. A packaging filter
 * that drops a renderer module ships a blank window exactly like a bad import
 * does, and only this mode can see it (review finding 5b).
 */
const packedIndex = process.argv.indexOf("--packed");
// Resolved against the CALLER's directory, not this script's: the npm scripts
// pass a path relative to hud/, and a relative value here silently became
// hud/scripts/dist-app/… — reported as ERR_FILE_NOT_FOUND rather than as the
// mistake it was.
const PACKED_RESOURCES = packedIndex === -1
  ? undefined
  : path.resolve(process.cwd(), process.argv[packedIndex + 1]);
const STAGE_DEADLINE_MS = 60_000;

const withDeadline = (promise, label) => Promise.race([
  promise,
  new Promise((_resolve, reject) => setTimeout(() => reject(new Error(`${label} exceeded ${STAGE_DEADLINE_MS}ms`)), STAGE_DEADLINE_MS)),
]);

/**
 * The minimum main-process surface each page calls while booting. Stubbing it
 * is not the point of this smoke — the point is that the renderer's own module
 * graph executes — but a page that dies on a missing IPC handler wipes its own
 * DOM and hides the very failure we are looking for.
 */
const invoked = new Set();

function stubIpc() {
  const handlers = {
    "settings:get": () => ({ apiKey: "", gatewayPort: 8642, stateServerPort: 4870, demoMode: false, kiosk: false }),
    "app:get-version": () => "1.7.4",
    "diagnostics:get": () => ({ text: "" }),
    "voice:status": () => ({ available: true, detail: "ready", mic: "unknown", voiceId: "bm_george" }),
    "setup:start": () => ({ started: false }),
    "setup:lifecycle-debt": () => ({ blocked: false, reasons: [], detail: "" }),
    "gateway:status": () => ({ state: "stopped" }),
    "history:list": () => ({ sessions: [] }),
    "brief:get": () => ({ text: "", audioPath: "" }),
    "copy:no-brain-line": () => "Brain: not set up yet — open the gear → RUN SETUP AGAIN when you have ChatGPT or Claude",
  };
  for (const [channel, handler] of Object.entries(handlers)) {
    if (!ipcMain.eventNames().includes(channel)) {
      ipcMain.handle(channel, (...args) => {
        invoked.add(channel);
        return handler(...args);
      });
    }
  }
  // Anything else the pages ask for answers null rather than throwing: an
  // unstubbed channel must not be able to masquerade as a renderer defect.
  for (const channel of [
    "voice:arm-mic", "voice:disarm-mic",
    "voice:report-mic", "setup:answer", "setup:run-step",
    "setup:skip-step", "setup:cancel", "gateway:restart", "brief:play",
  ]) {
    ipcMain.handle(channel, () => {
      invoked.add(channel);
      return null;
    });
  }
}

// Electron quits when the last window closes, and this smoke deliberately
// closes one page before opening the next — without this the process ended
// after the FIRST page and the second one silently never ran.
app.on("window-all-closed", () => {});

const checks = [];
function check(label, ok, detail) {
  checks.push({ label, ok });
  // Detail only when it FAILED: an explanation printed beside "ok" reads as a
  // problem and buries the one line that is actually a problem.
  console.log(`${ok ? "ok  " : "FAIL"}  ${label}${ok || detail === undefined ? "" : `  — ${detail}`}`);
}

// Renderer noise that is real but not ours, and would make this flap.
const IGNORABLE = [
  /Autofill\.(enable|setAddresses)/i,
  /Request Autofill\.enable failed/i,
  /ExperimentalWarning/i,
  /devtools/i,
  // Electron's own dev-only advisories, which say themselves that they do not
  // appear in a packaged build (the voice smoke filters the same set).
  /electronjs\.org\/docs\/tutorial\/security|Security Warning|unsafe-eval|once the app is packaged/i,
];

async function openPage(page, { expectIds, boot }) {
  const problems = [];
  const source = PACKED_RESOURCES === undefined
    ? path.join(__dirname, "..", "src", page)
    : path.join(PACKED_RESOURCES, "app.asar", "src", page);
  console.log(`\n-- ${page} (${PACKED_RESOURCES === undefined ? "workspace" : "packaged app"})`);

  const window = new BrowserWindow({
    show: false,
    width: 1200,
    height: 900,
    webPreferences: {
      preload: PACKED_RESOURCES === undefined
        ? path.join(__dirname, "..", "dist", "preload.cjs")
        : path.join(PACKED_RESOURCES, "app.asar", "dist", "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      offscreen: true,
    },
  });

  window.webContents.on("console-message", (_event, level, message, line, sourceId) => {
    // level 3 = error. A renderer that throws at import time reports here and
    // nowhere else — which is exactly how this class of defect stays invisible.
    if (level >= 2 && !IGNORABLE.some((pattern) => pattern.test(message))) {
      problems.push(`console(${level}): ${message} @ ${sourceId}:${line}`);
    }
  });
  window.webContents.on("render-process-gone", (_event, details) => {
    problems.push(`render process gone: ${details.reason}`);
  });
  window.webContents.on("preload-error", (_event, preloadPath, error) => {
    problems.push(`preload failed (${preloadPath}): ${error.message}`);
  });
  window.webContents.on("did-fail-load", (_event, code, description) => {
    problems.push(`did-fail-load ${code}: ${description}`);
  });

  try {
    await withDeadline(window.loadFile(source), `${page}: load`);

    // THE LOAD-BEARING ASSERTION. Every id below exists in the static HTML, so
    // finding them proves only that a file was parsed — a renderer that
    // silently did nothing would pass. What cannot be faked is a BOOT EFFECT:
    // the renderer asked the main process for something and wrote the answer
    // into the page. Poll for it, rather than sleeping and hoping (findings 4a,
    // 4b).
    const started = Date.now();
    let marker = { ok: false, value: "" };
    while (Date.now() - started < 15_000) {
      marker = await withDeadline(window.webContents.executeJavaScript(
        `(() => { const el = document.getElementById(${JSON.stringify(boot.elementId)});
           const value = el === null ? "" : el.textContent.trim();
           return { ok: value !== "" && value !== ${JSON.stringify(boot.emptyValue ?? "")}, value }; })()`,
      ), `${page}: boot probe`);
      if (marker.ok) break;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }

    const found = await withDeadline(window.webContents.executeJavaScript(`(() => ({
      ids: ${JSON.stringify(expectIds)}.filter((id) => document.getElementById(id) !== null),
      bodyText: document.body.innerText.trim().length,
    }))()`), `${page}: dom probe`);

    check(`${page}: the renderer reported no errors`, problems.length === 0, problems.join(" | "));
    check(
      `${page}: the renderer BOOTED (#${boot.elementId} filled in by the page itself)`,
      marker.ok,
      `#${boot.elementId} = "${marker.value}" — the module graph never ran, or never reached the main process`,
    );
    check(
      `${page}: it called the main process`,
      boot.channels.every((channel) => invoked.has(channel)),
      `missing ${boot.channels.filter((channel) => !invoked.has(channel)).join(", ")}`,
    );
    check(
      `${page}: its scaffolding rendered`,
      found.ids.length === expectIds.length,
      `found ${found.ids.join(",") || "nothing"} of ${expectIds.join(",")}`,
    );
    check(`${page}: the page is not empty`, found.bodyText > 0, `${found.bodyText} characters of text`);

    // The wizard's baked voice cues (2026-08-14): prove a clip actually PLAYS
    // in THIS app shape without a user gesture. Static analysis cannot prove
    // autoplay. The probe force-unmutes first (a persisted preference must not
    // fail a release), and a "silent" outcome is split by its reason (Codex
    // mechanism review 2026-08-14, finding 5): NotAllowedError is an autoplay
    // REGRESSION and fails; a machine with no usable audio output passes only
    // if the asset still fetches AND decodes out of the ASAR — the two halves
    // (permission, asset) are what this check exists to protect.
    if (page === "setup.html") {
      const probed = await withDeadline(window.webContents.executeJavaScript(
        "window.__wizardAudio ? window.__wizardAudio.probe() : false",
      ), `${page}: audio probe`);
      let audioStatus = "missing";
      const audioStarted = Date.now();
      while (Date.now() - audioStarted < 10_000) {
        audioStatus = await withDeadline(window.webContents.executeJavaScript(
          "window.__wizardAudio ? window.__wizardAudio.status() : 'missing'",
        ), `${page}: audio status`);
        if (audioStatus === "played" || audioStatus === "silent") break;
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
      let verdict = probed === true && audioStatus === "played";
      let detailSuffix = "";
      if (probed === true && audioStatus === "silent") {
        const reason = await withDeadline(window.webContents.executeJavaScript(
          "window.__wizardAudio && window.__wizardAudio.reason ? (window.__wizardAudio.reason() || 'unknown') : 'unknown'",
        ), `${page}: audio silent reason`);
        detailSuffix = ` reason=${reason}`;
        // ONLY NotSupportedError may take the no-device fallback — and only
        // with INDEPENDENT evidence that this machine actually has no audio
        // output. NotSupportedError is ambiguous (the HTML spec also raises
        // it for unsupported sources/formats, i.e. real build regressions),
        // so a machine that HAS output devices fails outright even with this
        // reason (Codex second re-review 2026-08-14, finding 1). Everything
        // else — NotAllowedError (autoplay policy), SecurityError (CSP),
        // AbortError, unknown — is a build regression and fails outright.
        let audioOutputs = -1;
        if (reason === "NotSupportedError") {
          audioOutputs = await withDeadline(window.webContents.executeJavaScript(`
            (async () => {
              try {
                const devices = await navigator.mediaDevices.enumerateDevices();
                return devices.filter((d) => d.kind === "audiooutput").length;
              } catch { return -1; }
            })()
          `), `${page}: audio output device count`);
          detailSuffix += ` audioOutputs=${audioOutputs}`;
        }
        if (reason === "NotSupportedError" && audioOutputs === 0) {
          // The asset must still fetch AND decode out of the ASAR — via the
          // SAME catalog-derived URL the probe played, so a rotten catalog
          // path cannot pass on an unrelated hard-coded file.
          const decoded = await withDeadline(window.webContents.executeJavaScript(`
            (async () => {
              try {
                const url = window.__wizardAudio && window.__wizardAudio.probeUrl
                  ? window.__wizardAudio.probeUrl() : "";
                if (!url || url.endsWith("/")) return "no-url";
                const res = await fetch(url);
                if (!res.ok) return "fetch:" + res.status;
                const bytes = await res.arrayBuffer();
                const ctx = new AudioContext();
                await ctx.decodeAudioData(bytes.slice(0));
                await ctx.close();
                return "decoded";
              } catch (error) { return "error:" + (error && error.name ? error.name : "unknown"); }
            })()
          `), `${page}: audio decode fallback`);
          detailSuffix += ` decode=${decoded}`;
          verdict = decoded === "decoded";
        }
      }
      check(
        `${page}: a baked voice cue autoplays (no user gesture)`,
        verdict,
        `probe=${probed} status=${audioStatus}${detailSuffix}`,
      );

      // Serialization is load-bearing (Peter's queue rule, 2026-08-14): two
      // queued cues must START strictly in order — which also proves real
      // 'ended' (or rejection) settlement reaches the player in this app
      // shape, because the second cue can only start when the first settles.
      // Real clips run 4-6s each; with the probe's clip possibly still
      // playing ahead of them, allow a generous deadline.
      const queued = await withDeadline(window.webContents.executeJavaScript(
        "window.__wizardAudio && window.__wizardAudio.probeQueue ? window.__wizardAudio.probeQueue() : false",
      ), `${page}: audio queue probe`);
      let order = [];
      const queueStarted = Date.now();
      while (Date.now() - queueStarted < 25_000) {
        order = await withDeadline(window.webContents.executeJavaScript(
          "window.__wizardAudio && window.__wizardAudio.order ? window.__wizardAudio.order() : []",
        ), `${page}: audio queue order`);
        if (order.includes("voice.handoff") && order.includes("engine.install")) break;
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
      const handoffAt = order.indexOf("voice.handoff");
      const installAt = order.indexOf("engine.install");
      check(
        `${page}: queued voice cues play strictly in sequence`,
        queued === true && handoffAt !== -1 && installAt !== -1 && handoffAt < installAt,
        `queued=${queued} order=${JSON.stringify(order)}`,
      );

      // The setup→HUD birth (TRANSITION-DESIGN.md §16). Three halves to
      // protect: the overlay scaffolding shipped with the page, the timeline
      // machinery actually runs in THIS app shape (compressed via the
      // controller's timeScale seam, against a stub launchHud), and the orb
      // asset decodes out of the ASAR.
      const trPresent = await withDeadline(window.webContents.executeJavaScript(
        "window.__wizardTransition ? window.__wizardTransition.present() : false",
      ), `${page}: transition surface`);
      check(`${page}: the birth overlay and probe surface shipped`, trPresent === true,
        "window.__wizardTransition/present() missing — overlay DOM or probe surface rotted");

      const trProbe = await withDeadline(window.webContents.executeJavaScript(
        "window.__wizardTransition ? window.__wizardTransition.probe() : null",
      ), `${page}: transition timeline probe`);
      check(
        `${page}: the birth timeline runs to done with one full launch`,
        trProbe !== null && trProbe.state === "done" && trProbe.launches === 1 && trProbe.entrance === "full",
        `probe=${JSON.stringify(trProbe)}`,
      );

      const trSkip = await withDeadline(window.webContents.executeJavaScript(
        "window.__wizardTransition ? window.__wizardTransition.probeSkip() : null",
      ), `${page}: transition skip probe`);
      check(
        `${page}: the skip escape lands exactly one fast launch`,
        trSkip !== null && trSkip.state === "skipped" && trSkip.launches === 1 && trSkip.entrance === "fast",
        `probeSkip=${JSON.stringify(trSkip)}`,
      );

      // The orb must reach loadedmetadata from the packaged path. A machine
      // (CI, offscreen) that cannot DECODE falls back to fetch + size — the
      // same posture as the audio probe's no-device fallback: the asset must
      // at least ship intact in the ASAR.
      const orbVerdict = await withDeadline(window.webContents.executeJavaScript(`
        (async () => {
          const url = window.__wizardTransition ? window.__wizardTransition.videoUrl() : "";
          if (!url) return "no-url";
          const decoded = await new Promise((resolve) => {
            const video = document.createElement("video");
            video.muted = true;
            video.preload = "metadata";
            const timer = setTimeout(() => resolve("timeout"), 10000);
            video.addEventListener("loadedmetadata", () => { clearTimeout(timer); resolve("loadedmetadata"); }, { once: true });
            video.addEventListener("error", () => { clearTimeout(timer); resolve("decode-error"); }, { once: true });
            video.src = url;
          });
          if (decoded === "loadedmetadata") return decoded;
          try {
            const res = await fetch(url);
            if (!res.ok) return decoded + " fetch:" + res.status;
            const bytes = await res.arrayBuffer();
            return bytes.byteLength > 400 * 1024 ? decoded + " fetched:" + bytes.byteLength : decoded + " undersized:" + bytes.byteLength;
          } catch (error) { return decoded + " fetch-error:" + (error && error.name ? error.name : "unknown"); }
        })()
      `), `${page}: orb asset probe`);
      check(
        `${page}: the orb video ships and loads from the app package`,
        orbVerdict === "loadedmetadata" || /fetched:\d+$/.test(String(orbVerdict)),
        `orb=${orbVerdict}`,
      );
    }
  } finally {
    // Always, so a failed probe cannot leave a window holding the app open.
    window.destroy();
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return problems;
}

app.whenReady().then(async () => {
  stubIpc();
  // setup.html is the first-run wizard AND the "blocked" first-run screen —
  // main.ts serves both from this one page, so covering it covers both.
  await openPage("setup.html", {
    expectIds: ["steps", "log", "status", "app-version"],
    // showVersion() asks the main process and writes "v1.4.0" into the chrome;
    // startSetup() is the renderer's first real act. Both are boot effects that
    // a dead module graph cannot produce.
    boot: { elementId: "app-version", channels: ["app:get-version", "setup:start"] },
  });
  // index.html is the HUD. The voice smoke covers its behaviour; this covers
  // the same import-time failure class on the page that has one.
  await openPage("index.html", {
    expectIds: ["chat-log", "chat-input"],
    boot: { elementId: "voice-voice-id", channels: ["settings:get", "voice:status"] },
  });

  const failed = checks.filter((entry) => !entry.ok).length;
  console.log(`\n${checks.length - failed}/${checks.length} checks passed`);
  app.exit(failed === 0 ? 0 : 1);
}).catch((error) => {
  console.error(error);
  app.exit(1);
});
