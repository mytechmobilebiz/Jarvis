// Boots the real setup renderer and preload to exercise secret-key input,
// clipboard paste, visibility toggling and sanitized submission under Electron.
import assert from "node:assert/strict";
import { BrowserWindow, Menu, app, clipboard, ipcMain } from "electron";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, "..");
const clipboardKey = `sk-ant-api03-${"A".repeat(90)}\n`;
const sanitizedKey = clipboardKey.trim();
let receivedAnswer = null;

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

  ipcMain.handle("setup:start", () => ({ failed: false }));
  ipcMain.on("setup:answer", (_event, answer) => {
    receivedAnswer = answer;
  });
  ipcMain.handle("setup:launch-hud", () => undefined);
  ipcMain.handle("shell:open-external", () => undefined);
  ipcMain.handle("clipboard:read-text", () => clipboard.readText());

  try {
    clipboard.writeText("");
    await window.loadFile(path.join(root, "src", "setup.html"));
    window.webContents.send("setup:prompt", {
      promptId: "p1",
      kind: "secret",
      message: "Anthropic API key",
      secret: true,
    });

    await waitFor(window, `document.querySelector(".prompt.wide .prompt-row.stacked input[type=password]") !== null`);
    await window.webContents.executeJavaScript(`(() => {
      const input = document.querySelector(".prompt.wide .prompt-row.stacked input[type=password]");
      const buttons = [...document.querySelectorAll(".prompt.wide button")].map((button) => button.textContent);
      if (input === null) throw new Error("secret password input was not rendered");
      if (buttons.join(",") !== "PASTE,SHOW,SUBMIT") throw new Error(\`unexpected buttons: \${buttons.join(",")}\`);
      return true;
    })()`);

    await window.webContents.executeJavaScript(`document.querySelector(".prompt.wide button")?.click()`);
    await waitFor(window, `document.querySelector(".prompt.wide .field-hint")?.textContent === "clipboard is empty - copy the key first"`);
    clipboard.writeText(clipboardKey);
    await window.webContents.executeJavaScript(`document.querySelector(".prompt.wide button")?.click()`);
    await waitFor(window, `document.querySelector(".prompt.wide input")?.value.length === 103`);
    await window.webContents.executeJavaScript(`(() => {
      const input = document.querySelector(".prompt.wide input");
      const hint = document.querySelector(".prompt.wide .field-hint");
      if (input?.value.length !== 103) throw new Error(\`expected 103 characters, got \${input?.value.length}\`);
      if (!hint?.textContent?.startsWith("103 characters captured")) throw new Error(\`unexpected hint: \${hint?.textContent}\`);
      return true;
    })()`);

    await window.webContents.executeJavaScript(`document.querySelectorAll(".prompt.wide button")[1]?.click()`);
    await window.webContents.executeJavaScript(`(() => {
      const input = document.querySelector(".prompt.wide input");
      const reveal = document.querySelectorAll(".prompt.wide button")[1];
      if (input?.type !== "text") throw new Error(\`expected text input, got \${input?.type}\`);
      if (reveal?.textContent !== "HIDE") throw new Error(\`expected HIDE, got \${reveal?.textContent}\`);
      return true;
    })()`);

    await window.webContents.executeJavaScript(`document.querySelectorAll(".prompt.wide button")[2]?.click()`);
    await waitForAnswer();
    assert.deepEqual(receivedAnswer, { promptId: "p1", value: sanitizedKey });
    await window.webContents.executeJavaScript(`(() => {
      const input = document.querySelector(".prompt.wide input");
      if (input?.value !== "") throw new Error("submitted secret input was not cleared");
      if (input?.type !== "password") throw new Error(\`submitted secret input was not masked: \${input?.type}\`);
      return true;
    })()`);

    assert.notEqual(Menu.getApplicationMenu(), null);
    console.log("input smoke ok: paste sanitized 103 characters, reveal toggled, submit cleared and masked");
  } finally {
    window.destroy();
    app.quit();
  }
});

async function waitFor(window, expression, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await window.webContents.executeJavaScript(expression)) return;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(`timed out waiting for: ${expression}`);
}

async function waitForAnswer(timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (receivedAnswer !== null) return;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error("timed out waiting for setup:answer");
}
