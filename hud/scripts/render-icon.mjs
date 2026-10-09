// Renders assets/icon.svg to build/icon.png (1024px, alpha preserved) using
// Electron offscreen rendering, so the icon needs no external tooling.
import { BrowserWindow, app } from "electron";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const svgPath = path.join(root, "assets", "icon.svg");
const outPath = path.join(root, "build", "icon.png");

app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("force-device-scale-factor", "1");

app.whenReady().then(async () => {
  const svg = fs.readFileSync(svgPath, "utf8");
  const html = `<!doctype html><html><body style="margin:0;background:transparent">${svg}</body></html>`;
  const window = new BrowserWindow({
    width: 1024,
    height: 1024,
    show: false,
    transparent: true,
    frame: false,
    webPreferences: { offscreen: true, backgroundThrottling: false },
  });

  await window.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
  await new Promise((resolve) => setTimeout(resolve, 800));

  const image = await window.webContents.capturePage();
  if (image.isEmpty()) {
    console.error("captured image is empty");
    app.exit(1);
    return;
  }
  const sized = image.getSize().width === 1024 ? image : image.resize({ width: 1024, height: 1024 });
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, sized.toPNG());
  console.log(`${outPath} (${sized.getSize().width}x${sized.getSize().height})`);
  app.exit(0);
});

setTimeout(() => {
  console.error("icon render timed out");
  app.exit(1);
}, 15_000);
