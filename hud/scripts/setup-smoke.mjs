// Drives the first-run setup UI end to end in dry-run mode under Electron and
// asserts the renderer reaches the finish panel without console errors.
import { BrowserWindow, app, ipcMain } from "electron";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createSetupHost } from "../dist/setup-host.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, "..");
const repoRoot = path.resolve(root, "..");
const problems = [];

app.commandLine.appendSwitch("disable-gpu");

app.whenReady().then(async () => {
  const window = new BrowserWindow({
    width: 1440,
    height: 900,
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(root, "dist", "preload.cjs"),
    },
  });

  const host = createSetupHost({
    dryRun: true,
    repoRoot,
    emitEvent: (event) => send(window, "setup:event", event),
    emitPrompt: (prompt) => send(window, "setup:prompt", prompt),
  });

  ipcMain.handle("setup:start", () => host.start());
  ipcMain.on("setup:answer", (_event, answer) => host.answer(answer.promptId, answer.value));
  ipcMain.handle("setup:launch-hud", () => undefined);
  ipcMain.handle("shell:open-external", () => undefined);

  window.webContents.on("console-message", (event, level, message) => {
    const text = String(event?.message ?? message ?? "");
    const severity = event?.level ?? level;
    const isError = severity === "error" || severity === 2 || severity === 3;
    // Electron's own dev-only CSP notice is not a renderer failure.
    if (isError && !text.includes("Electron Security Warning")) {
      problems.push(`console: ${text}`);
    }
  });
  window.webContents.on("preload-error", (_event, file, error) => {
    problems.push(`preload ${file}: ${error.message}`);
  });
  window.webContents.on("render-process-gone", (_event, details) => {
    problems.push(`renderer gone: ${details.reason}`);
  });

  await window.loadFile(path.join(root, "src", "setup.html"));

  const report = await waitFor(window, 90_000);
  if (report === null) problems.push("the setup UI never reached the finish panel");
  else if (report.finish !== "SETUP COMPLETE") problems.push(`finish panel says ${report.finish}`);
  else if (report.tiles !== 12) problems.push(`expected 12 step tiles, saw ${report.tiles}`);
  else if (report.failed > 0) problems.push(`${report.failed} step tiles reported FAILED`);

  window.destroy();
  if (problems.length > 0) {
    for (const problem of problems) console.error(`setup smoke: ${problem}`);
    process.exitCode = 1;
  } else {
    console.log(`setup smoke ok: 12 steps ran in dry-run, ${report.done} done, ${report.skipped} skipped`);
  }
  app.quit();
});

function send(window, channel, payload) {
  if (!window.isDestroyed()) window.webContents.send(channel, payload);
}

async function waitFor(window, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const report = await window.webContents.executeJavaScript(`(() => {
      const finish = document.querySelector(".finish h2");
      if (finish === null) return null;
      const chips = [...document.querySelectorAll(".chip")].map((chip) => chip.textContent ?? "");
      return {
        finish: finish.textContent,
        tiles: document.querySelectorAll(".step").length,
        done: chips.filter((text) => text.includes("DONE")).length,
        skipped: chips.filter((text) => text.includes("SKIPPED")).length,
        failed: chips.filter((text) => text.includes("FAILED")).length,
      };
    })()`);
    if (report !== null) return report;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return null;
}
