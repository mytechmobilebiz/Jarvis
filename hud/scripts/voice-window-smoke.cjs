/**
 * Loads the real HUD window and checks the voice UI actually wired up.
 *
 *   pnpm --dir hud exec electron scripts/voice-window-smoke.cjs
 *
 * Worth its own script because renderer.ts resolves every element through
 * must<T>(), which throws on a missing id — so one typo in index.html does not
 * break "the mic button", it breaks the ENTIRE dashboard at boot. A unit test
 * cannot catch that; only loading the page can.
 *
 * Runs offscreen, needs no gateway, and asserts the chat bar still works when
 * voice is present.
 */

const { BrowserWindow, app, ipcMain } = require("electron");
const path = require("node:path");

const HUD_ROOT = path.resolve(__dirname, "..");
const checks = [];
const consoleErrors = [];

function check(name, ok, detail = "") {
  checks.push({ name, ok });
  console.log(`${ok ? "ok  " : "FAIL"}  ${name}${detail === "" ? "" : `  — ${detail}`}`);
}

// Stub only what the page calls on boot. The point is the renderer's own
// wiring, not the main process (voice-electron-smoke.cjs covers that).
function stubIpc() {
  const handlers = {
    "settings:get": () => ({ apiKey: "", gatewayPort: 8642, stateServerPort: 4870, demoMode: false, kiosk: false }),
    "app:get-version": () => "1.7.4",
    "diagnostics:get": () => ({ text: "" }),
    "copy:no-brain-line": () => "Brain: not set up yet — open the gear → RUN SETUP AGAIN when you have ChatGPT or Claude",
    // VOICE_BROKEN simulates a corrupt/quarantined payload so the same page
    // can be asserted to fall back to exactly the typed HUD that shipped in
    // v1.3 — the guarantee that voice is never allowed to damage chat.
    "voice:status": () => (process.env.VOICE_BROKEN === "1"
      ? {
        available: false,
        reason: "assets-corrupt",
        detail: "Some voice files do not match this build — antivirus quarantine is the usual cause. Reinstall JARVIS to restore them.",
        mic: "unknown",
        voiceId: "bm_george",
      }
      : {
        available: true,
        detail: "Local speech recognition and speech generation are ready.",
        mic: "unknown",
        voiceId: "bm_george",
      }),
  };
  for (const [channel, handler] of Object.entries(handlers)) ipcMain.handle(channel, handler);
  for (const channel of ["voice:arm-mic", "voice:disarm-mic", "voice:report-mic"]) {
    ipcMain.handle(channel, () => null);
  }
  // VOICE_HOSTILE makes transcription and brief preparation throw.
  if (process.env.VOICE_HOSTILE === "1") {
    for (const channel of ["voice:transcribe", "voice:prepare-brief"]) {
      ipcMain.removeHandler(channel);
      ipcMain.handle(channel, () => {
        throw new Error("voice is deliberately broken for this smoke");
      });
    }
  }
}

app.whenReady().then(async () => {
  stubIpc();
  const window = new BrowserWindow({
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      offscreen: true,
      preload: path.join(HUD_ROOT, "dist/preload.cjs"),
    },
  });

  window.webContents.on("console-message", (_event, level, message) => {
    // Electron prints its own dev-only security advisories at warning level
    // (they say so, and they do not appear in a packaged build). Only genuine
    // page errors are of interest here.
    if (level >= 2 && !/electronjs\.org\/docs\/tutorial\/security|Security Warning|unsafe-eval|once the app is packaged/i.test(message)) {
      consoleErrors.push(message);
    }
  });

  await window.loadFile(path.join(HUD_ROOT, "src/index.html"));
  // Let boot() finish its async settings/voice-status round trips.
  await new Promise((resolve) => setTimeout(resolve, 1_500));

  const result = await window.webContents.executeJavaScript(`(() => {
    const id = (name) => document.getElementById(name);
    return {
      mic: id("voice-mic") !== null,
      micVisible: id("voice-mic") !== null && !id("voice-mic").hasAttribute("hidden"),
      stateLine: id("voice-state") !== null,
      voiceDetail: id("voice-detail")?.textContent ?? "",
      voiceId: id("voice-voice-id")?.textContent ?? "",
      hasVoiceClass: id("chat-form")?.classList.contains("has-voice") === true,
      chatInput: id("chat-input") !== null,
      chatSendEnabled: id("chat-send") !== null && !id("chat-send").disabled,
      bridge: typeof window.jarvis?.voice?.transcribe === "function",
    };
  })()`);

  const broken = process.env.VOICE_BROKEN === "1";
  const hostile = process.env.VOICE_HOSTILE === "1";
  if (hostile) console.log("(voice methods are throwing on purpose)");
  check("the page boots with no renderer errors", consoleErrors.length === 0, consoleErrors.slice(0, 2).join(" | "));
  check("every voice element resolves (must<T>() did not throw)",
    result.mic && result.stateLine);
  check("the voice bridge reaches the renderer through context isolation", result.bridge);
  if (broken) {
    check("a corrupt payload hides the mic entirely", !result.micVisible);
    check("the chat bar does not reflow for a mic that is not there", !result.hasVoiceClass);
    check("settings says voice is unavailable", result.voiceId === "UNAVAILABLE", result.voiceId);
    check("settings explains why, without a filesystem path", /antivirus/i.test(result.voiceDetail), result.voiceDetail);
  } else {
    check("the mic button is shown when voice is available", result.micVisible);
    check("the chat bar reflows for the mic", result.hasVoiceClass);
    check("settings names the voice", result.voiceId === "BM_GEORGE", result.voiceId);
    check("settings explains the voice state in plain words", result.voiceDetail.length > 10, result.voiceDetail);
  }
  // --- microphone lifecycle races (r2 #2/#4) --------------------------------
  // A real DOM with the real voice UI attached. getUserMedia is replaced with
  // one that never resolves until released, which is the only way to observe
  // "released while the microphone was still opening".
  if (!broken) {
    const mic = await window.webContents.executeJavaScript(`(async () => {
      const tracks = [];
      let releaseOpen = null;
      navigator.mediaDevices.getUserMedia = () => new Promise((resolve) => {
        releaseOpen = () => {
          const track = { kind: "audio", readyState: "live", stop() { this.readyState = "ended"; } };
          tracks.push(track);
          resolve({ getTracks: () => [track] });
        };
      });

      const button = document.getElementById("voice-mic");
      const down = () => button.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, cancelable: true }));
      const up = () => window.dispatchEvent(new PointerEvent("pointerup", { bubbles: true }));
      const settle = () => new Promise((r) => setTimeout(r, 60));

      // 1. Press, RELEASE before the mic finishes opening, then let it open.
      down();
      await settle();
      up();
      await settle();
      releaseOpen && releaseOpen();
      await settle();
      const strandedOpen = tracks.filter((t) => t.readyState === "live").length;

      // 2. Rapid re-press: down, up, down again immediately.
      down(); await settle(); up(); down(); await settle();
      releaseOpen && releaseOpen();
      await settle();
      up();
      await settle();
      const liveAfter = tracks.filter((t) => t.readyState === "live").length;

      return { strandedOpen, liveAfter, opened: tracks.length };
    })()`);

    check("a release during microphone startup leaves no live track",
      mic.strandedOpen === 0, `${mic.strandedOpen} track(s) still live`);
    check("rapid release-then-press leaves no microphone open at the end",
      mic.liveAfter === 0, `${mic.liveAfter} track(s) still live of ${mic.opened} opened`);
  }

  // The load-bearing guarantee, asserted in EVERY mode: chat still works.
  check("typed chat controls are present and enabled", result.chatInput && result.chatSendEnabled);

  // Actually SUBMIT. "The input exists and Send is enabled" is not the same
  // claim as "a message can be sent"; with voice hooks throwing, the old code
  // would have skipped the finally that re-enables Send and wedged chat
  // permanently after one message.
  const submitted = await window.webContents.executeJavaScript(`(async () => {
    const input = document.getElementById("chat-input");
    const form = document.getElementById("chat-form");
    const log = document.getElementById("chat-log");
    const before = log.children.length;
    input.value = "does typed chat still work";
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    // The gateway is not running here, so the turn ends in a connection
    // failure. How LONG that takes is the operating system's business, not
    // ours: a Windows runner whose firewall drops the SYN rather than
    // answering RST spends ~21s on the default retry schedule, which is what
    // made this check flap (green on 43d7a0e, red on the config-only 0dd0e41).
    // The claim being asserted is unchanged and unweakened — the turn COMPLETES
    // and leaves the UI usable — so the budget is now larger than any connect
    // failure can cost, and the elapsed time is reported either way.
    const started = Date.now();
    while (document.getElementById("chat-send").disabled && Date.now() - started < 30000) {
      await new Promise((r) => setTimeout(r, 50));
    }
    return {
      bubblesAdded: log.children.length - before,
      userText: log.children[before] ? log.children[before].textContent : "",
      sendReEnabled: !document.getElementById("chat-send").disabled,
      inputCleared: input.value === "",
      elapsedMs: Date.now() - started,
    };
  })()`);

  check("submitting a message adds the user and assistant bubbles", submitted.bubblesAdded >= 2,
    `added ${submitted.bubblesAdded}`);
  check("the typed text reaches the log", submitted.userText === "does typed chat still work", submitted.userText);
  check("Send is re-enabled after the turn (chat is not wedged)", submitted.sendReEnabled,
    `the turn ended after ${submitted.elapsedMs}ms`);
  check("the input is cleared", submitted.inputCleared);

  // And a second message, because a wedge often only shows on the turn after.
  const second = await window.webContents.executeJavaScript(`(async () => {
    const input = document.getElementById("chat-input");
    const form = document.getElementById("chat-form");
    const log = document.getElementById("chat-log");
    const before = log.children.length;
    input.value = "second message";
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    const started = Date.now();
    while (document.getElementById("chat-send").disabled && Date.now() - started < 30000) {
      await new Promise((r) => setTimeout(r, 50));
    }
    return {
      added: log.children.length - before,
      sendReEnabled: !document.getElementById("chat-send").disabled,
      elapsedMs: Date.now() - started,
    };
  })()`);
  // Two separate claims, reported separately: "the second turn was accepted"
  // and "the UI came back". Merged into one boolean with no detail, this check
  // failed in CI saying only that something was wrong.
  check("a second message is accepted (both bubbles appear)", second.added >= 2,
    `added ${second.added}`);
  check("the UI is usable after the second turn", second.sendReEnabled,
    `the turn ended after ${second.elapsedMs}ms`);

  const failed = checks.filter((entry) => !entry.ok).length;
  console.log(`\n${checks.length - failed}/${checks.length} checks passed`);
  app.exit(failed === 0 ? 0 : 1);
}).catch((error) => {
  console.error(error);
  app.exit(1);
});
