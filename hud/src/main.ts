import { BrowserWindow, Menu, app, utilityProcess, clipboard, dialog, ipcMain, nativeImage, session, shell } from "electron";
import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createHudStateServer, type HudStateServer } from "./state-server.js";
import { getTranscript, listSessions } from "./history.js";
import { DEFAULT_SETTINGS, normalizeSettings, persistableSettings, type HudSettings } from "./settings.js";
import { authorizeGatewayStart, createContext, ensureProviderSafety, enqueueOutOfBandTelemetry, formatDiagnostics, jarvisConfigDir, loadSupportCode, NO_BRAIN_LINE, readLastCrash, readLastRun, readMigrationMarker, readSetupProgressAt, resolveVendorRoot, telemetryEnabled, type DiagnosticsInfo, type SetupIO } from "@jarvis/setup/engine";
import { resolveHermesRoot } from "@jarvis/setup/hermes-root";
import {
  clearSessionMarkerSync,
  detectDirtyShutdown,
  markSessionStartSync,
  recordProcessGone,
} from "./crash-reporter.js";
import { maybeGenerateFirstBrief } from "./first-brief.js";
import { preparePackagedContent } from "./packaged-content.js";
import { resolveRepoRoot } from "./resources.js";
import { preparePackagedSetupContent } from "./setup-readiness.js";
import { decideLaunchMode, fileExists, mayLeaveSetup, shouldRunSetupFor } from "./setup-decision.js";
import { createSetupHost, type SetupHost } from "./setup-host.js";
import { createHudGateway, type GatewayStatus, type HudGateway } from "./gateway.js";
import type { GatewayVerification } from "@jarvis/setup/engine";
import { decideInstanceRole } from "./single-instance.js";
import { buildContextMenuTemplate } from "./context-menu.js";
import type { DiagnosticsReport, SetupAnswer, SetupEvent, SetupPrompt } from "./setup-protocol.js";
// --- Lane C (voice) ---------------------------------------------------------
import { createMicGate, createVoiceService, decideVoicePermission, type VoiceService } from "./voice-main.js";
import { resolveVoiceRoot } from "./voice-resources.js";
import type { VoiceMicState } from "./voice-types.js";
// --- end Lane C -------------------------------------------------------------

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const { repoRoot: bundledRepoRoot, packaged } = resolveRepoRoot({
  isPackaged: app.isPackaged,
  resourcesPath: process.resourcesPath,
});
// The offline engine payload this build carries (undefined in a checkout,
// which keeps the network install path for developers and Linux).
const vendorRoot = resolveVendorRoot({
  packaged,
  resourcesPath: process.resourcesPath,
  repoRoot: bundledRepoRoot,
  env: process.env,
});
const iconPath = path.resolve(__dirname, "../build/icon.png");

app.setName("JARVIS HUD");
const dryRun = process.argv.includes("--dry-run");
// The wizard speaks from the moment it opens (baked voice cues, 2026-08-14),
// and Chromium's default autoplay policy would silence the first clip until a
// click. Set before app ready; scoped to our own windows only.
app.commandLine.appendSwitch("autoplay-policy", "no-user-gesture-required");

const setupForced = process.argv.includes("--setup") || process.argv.includes("--slack") || process.env.JARVIS_SETUP === "1";

/**
 * One JARVIS per machine. Without this, two HUDs each spawn a gateway with
 * --replace and evict each other in a loop: alternating restarts, a port that
 * keeps moving, and a renderer talking to an engine that just died (review r1,
 * finding 2). The lock is taken BEFORE app.whenReady() so a second instance
 * exits before it can start anything at all.
 */
const instanceRole = decideInstanceRole({
  dryRun,
  hasLock: dryRun ? true : app.requestSingleInstanceLock(),
});
const isPrimaryInstance = instanceRole.primary;
if (instanceRole.quit) {
  app.quit();
} else {
  app.on("second-instance", () => {
    // Someone launched JARVIS again (dock, Start menu, an installer finish
    // step). Surface the window we already have instead of racing it.
    const [existing] = BrowserWindow.getAllWindows();
    if (existing === undefined) return;
    if (existing.isMinimized()) existing.restore();
    existing.focus();
  });
}

let settings: HudSettings = { ...DEFAULT_SETTINGS };
let settingsPath = "";
let stateServer: HudStateServer | null = null;
let setupHost: SetupHost | null = null;
let gateway: HudGateway | null = null;
let setupMode = false;
let setupRepoRoot = bundledRepoRoot;
let stopping = false;
/**
 * Set when this launch must neither run setup nor start an engine: provider
 * safety or the 1.3.x migration forced setup, and the content that setup needs
 * could not be staged. Carries the customer-facing reason.
 */
let launchBlocked: string | undefined;
// --- Lane C (voice) ---------------------------------------------------------
let voiceService: VoiceService | null = null;
const micGate = createMicGate();
/** 120 s of 16 kHz mono PCM16. Mirrors the renderer's own capture ceiling. */
const MAX_PCM_BYTES = 120 * 16_000 * 2;
// --- end Lane C -------------------------------------------------------------

function createWindow(): void {
  const mainWindow = new BrowserWindow({
    width: 1920,
    height: 1080,
    backgroundColor: "#07090f",
    show: false,
    icon: iconPath,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.resolve(__dirname, "preload.cjs"),
    },
  });

  applyWindowMode(mainWindow);
  // Windows does not emit before-quit on shutdown, restart, or logout, so the
  // session marker would survive and read as a crash on the next launch. This
  // Windows-only event is the clean-exit signal for those paths.
  mainWindow.on("session-end", () => {
    clearSessionMarkerSync(jarvisConfigDir());
  });
  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
  });

  void mainWindow.loadFile(pageFor(setupMode));
}

/**
 * Electron has no built-in context menu, so right-clicking the wizard's API-key
 * field did nothing. Registered app-wide rather than per-window so the HUD's own
 * settings fields (index.html #api-key) get it too. Keyboard paste already works
 * on every platform via Electron's default application menu — do not replace or
 * null out that menu, or Cmd+V on macOS stops working.
 */
function registerContextMenu(): void {
  app.on("web-contents-created", (_event, contents) => {
    contents.on("context-menu", (_contextEvent, params) => {
      const template = buildContextMenuTemplate(params);
      if (template.length === 0) return;
      Menu.buildFromTemplate(template).popup({
        window: BrowserWindow.fromWebContents(contents) ?? undefined,
        frame: params.frame ?? undefined,
        x: params.x,
        y: params.y,
      });
    });
  });
}

function pageFor(setup: boolean): string {
  return path.resolve(__dirname, setup ? "../src/setup.html" : "../src/index.html");
}

/**
 * Setup mode is forced by flag or env. Otherwise an unfinished wizard run wins,
 * so quitting mid-setup returns here instead of a half-configured dashboard.
 * With no state file at all we require positive proof of a finished install —
 * a Hermes-installed profile plus its .env — because the wizard writes .env
 * long before it finishes.
 */
async function shouldRunSetup(): Promise<boolean> {
  return shouldRunSetupFor({
    forced: setupForced,
    packagedContentMissing: packaged && !(await repoContentExists()),
    profileDir: jarvisProfileDir(),
    readProgress: readSetupProgressAt,
    exists: fileExists,
    migrated: hasMigrated,
  });
}

/**
 * Has this machine been through the 1.4 migration?
 *
 * Read on its own as well as inside the setup decision: an unmigrated machine
 * may not open a dashboard even when something else fails, so the answer has to
 * be available separately from "should the wizard run".
 */
async function hasMigrated(profileDir: string = jarvisProfileDir()): Promise<boolean> {
  try {
    return (await readMigrationMarker(profileDir)) !== undefined;
  } catch {
    // Unreadable proves nothing; treat it as unmigrated and let setup look.
    return false;
  }
}

function jarvisProfileDir(): string {
  return path.join(resolveHermesRoot(), "profiles", "jarvis");
}

async function repoContentExists(): Promise<boolean> {
  try {
    // Deliberately does NOT assert the Slack manifest: a false here silently
    // opens the dashboard instead of the wizard (shouldRunSetupFor), whereas
    // preparePackagedContent rejects the same bundle loudly, with a dialog.
    const [agentTemplate, playbooks, demo, sampleApp, marker] = await Promise.all([
      fs.stat(path.join(bundledRepoRoot, "agents", "jarvis")),
      fs.stat(path.join(bundledRepoRoot, "playbooks")),
      fs.stat(path.join(bundledRepoRoot, "demo")),
      fs.stat(path.join(bundledRepoRoot, "examples", "sample-app")),
      fs.stat(path.join(bundledRepoRoot, "jarvis-content.json")),
    ]);
    return agentTemplate.isDirectory()
      && playbooks.isDirectory()
      && demo.isDirectory()
      && sampleApp.isDirectory()
      && marker.isFile();
  } catch {
    return false;
  }
}

/**
 * The live view the renderer gets. The gateway credential and port come from
 * the supervisor in memory, never from disk — settings.json cannot hold them
 * (see settings.ts), so the HUD and its engine cannot drift apart.
 */
function effectiveSettings(): HudSettings {
  // Only a VERIFIED connection is published. Handing the renderer a port/key
  // before the authenticated round-trip let the first chat fire at an engine
  // that was not listening yet and lose the message (review r1, finding 3).
  const connection = gateway?.status().verified === true ? gateway.connection() : undefined;
  return {
    ...settings,
    apiKey: connection?.key ?? "",
    gatewayPort: connection?.port ?? 0,
    demoMode: settings.demoMode || process.env.JARVIS_DEMO === "1",
    kiosk: settings.kiosk || process.argv.includes("--kiosk"),
  };
}

function applyWindowMode(window: BrowserWindow): void {
  const kiosk = !setupMode && effectiveSettings().kiosk;
  window.setFullScreen(kiosk);
  window.setMenuBarVisibility(!kiosk);
}

function broadcastSettings(): void {
  const next = effectiveSettings();
  for (const window of BrowserWindow.getAllWindows()) {
    applyWindowMode(window);
    window.webContents.send("settings:changed", next);
  }
}

function broadcast(channel: string, payload: unknown): void {
  for (const window of BrowserWindow.getAllWindows()) {
    if (!window.isDestroyed()) window.webContents.send(channel, payload);
  }
}

async function loadSettings(): Promise<void> {
  try {
    settings = normalizeSettings(JSON.parse(await fs.readFile(settingsPath, "utf8")));
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (code !== "ENOENT") {
      console.warn("Unable to load HUD settings, using defaults.", error);
    }
    settings = { ...DEFAULT_SETTINGS };
  }
}

async function saveSettings(next: HudSettings): Promise<void> {
  await fs.mkdir(path.dirname(settingsPath), { recursive: true });
  const tmpPath = `${settingsPath}.tmp`;
  // persistableSettings drops apiKey/gatewayPort: a 1.3.x settings.json loses
  // its stale key the first time this runs, and none is ever written again.
  await fs.writeFile(tmpPath, `${JSON.stringify(persistableSettings(next), null, 2)}\n`, "utf8");
  await fs.rename(tmpPath, settingsPath);
}

/**
 * The gateway lifecycle: created once, owned by this process, stopped on quit.
 * There is no login task and no Startup entry — the APP auto-starts (see
 * applyLoginItem) and the engine lives and dies with it.
 */
function ensureGateway(): HudGateway {
  if (gateway !== null) return gateway;
  gateway = createHudGateway({
    hermesRoot: resolveHermesRoot(),
    // Publishes <profile>/local-api.json for integrations, only ever for an
    // engine that has answered an authenticated request (see local-api.ts).
    profileDir: jarvisProfileDir(),
    appVersion: app.getVersion(),
    log: (message: string) => console.log(message),
    // Re-read on EVERY start and restart, inside the supervisor, so no caller
    // — including a future one — can start an engine past outstanding
    // provider-safety or migration debt (integration review r2, finding 1).
    authorize: () => authorizeGatewayStart({ profileDir: jarvisProfileDir() }),
    // The engine runs under the parent-death watchdog, launched with this very
    // Electron binary in Node mode, so a force-killed HUD cannot orphan it.
    launcher: { execPath: process.execPath, script: path.resolve(__dirname, "gateway-watchdog.js") },
  });
  gateway.onChange((next: GatewayStatus) => {
    broadcastSettings();
    broadcast("gateway:status", next);
  });
  return gateway;
}

async function startGateway(): Promise<GatewayVerification | undefined> {
  if (dryRun) return undefined;
  const result = await ensureGateway().ensureRunning();
  if (result.state !== "ok") {
    console.warn(`JARVIS engine not ready: ${result.state}`, result.detail ?? "");
  }
  if (result.state === "blocked" && result.detail !== undefined) {
    // The authoritative refusal, published where every other engine state is
    // published — the renderer must not have to guess (review r3, P2).
    launchBlocked = result.detail;
  }
  broadcastSettings();
  broadcast("gateway:status", ensureGateway().status());
  return result;
}

/**
 * One auto-start entry, owned by us and visible in HUD settings. Electron owns
 * the registration, so there is no .vbs to leave behind and nothing to evict.
 */
function applyLoginItem(): void {
  if (dryRun || !app.isPackaged) return;
  try {
    app.setLoginItemSettings({ openAtLogin: settings.startAtLogin, openAsHidden: false });
  } catch (error) {
    console.warn("Unable to update the start-at-sign-in setting.", error);
  }
}

async function startStateServer(): Promise<void> {
  if (stateServer) return;
  stateServer = createHudStateServer({
    storageDir: app.getPath("userData"),
    profileDir: jarvisProfileDir(),
    port: effectiveSettings().stateServerPort,
    demoMode: effectiveSettings().demoMode,
    onApprovalRequest: (request) => broadcast("approval:request", request),
    onBriefStored, // Lane C: pre-generate the spoken brief.
  });
  await stateServer.start();
}

async function launchHud(entrance: "full" | "fast" = "full"): Promise<void> {
  if (!setupMode) return;
  // THE CONTRACT DOOR (Peter, 2026-08-14: a completed setup means a working
  // JARVIS). Every path out of setup goes through this IPC, so the required-
  // step rule is enforced HERE, not only on the finish panel's buttons — a
  // stale renderer, a replayed IPC, any future button all hit the same wall.
  const leave = mayLeaveSetup(setupHost?.lastRunRecord());
  if (!leave.allowed) {
    broadcast("setup:event", {
      type: "error",
      stepId: "model",
      text: leave.reason ?? "Setup is not finished yet.",
    });
    return;
  }
  setupHost?.cancel();
  await startStateServer();
  // The engine is started BEFORE leaving the setup surface, and its answer is
  // awaited: a refusal here is the authoritative one (it re-reads the debt from
  // disk), and navigating away from setup on a refusal stranded the customer in
  // a dashboard that could never work (review r3, P2).
  const started = await startGateway();
  if (started?.state === "blocked") {
    broadcast("setup:event", {
      type: "error",
      stepId: "gateway",
      text: started.detail ?? "JARVIS did not start its engine.",
    });
    return;
  }
  setupMode = false;
  // First boot straight out of setup: ask the agent for its opening brief so
  // day one isn't dark. Fire-and-forget — it must never delay the HUD window,
  // and it self-limits so a down gateway can't turn it into a retry loop.
  if (!dryRun) {
    const briefSettings = effectiveSettings();
    // Only ever against the live supervised child: with no verified connection
    // there is no port to call and the helper no-ops on the empty key.
    void maybeGenerateFirstBrief({
      configDir: jarvisConfigDir(),
      gatewayPort: briefSettings.gatewayPort,
      apiKey: briefSettings.apiKey,
    }).catch(() => {});
  }
  const [window] = BrowserWindow.getAllWindows();
  if (window === undefined) {
    createWindow();
    return;
  }
  applyWindowMode(window);
  // The born query drives the HUD's ARRIVAL choreography only (a normal,
  // non-setup launch has no query and boots exactly as today). Navigation
  // happens on the transition's solid void frame, so the cut is invisible.
  await window.loadFile(pageFor(false), { query: { born: entrance } });
}

function relaunchIntoSetup(extraArg?: string): void {
  const args = process.argv.slice(1).filter((arg) => arg !== "--setup" && arg !== "--slack");
  args.push("--setup");
  if (extraArg !== undefined) args.push(extraArg);
  app.relaunch({ args });
  app.quit();
}

async function applySettings(patch: Partial<HudSettings>): Promise<HudSettings> {
  // Lane B: the engine key and port are supervisor-owned and exist only in
  // memory, so no renderer patch may set them — whatever a window sends, the
  // live connection below is what gets published.
  const { apiKey: _apiKey, gatewayPort: _gatewayPort, ...safe } = patch;
  settings = normalizeSettings({ ...settings, ...safe });
  await saveSettings(settings).catch((error: unknown) => {
    console.warn("Unable to persist HUD settings.", error);
  });
  // The one auto-start entry the product owns follows the saved setting.
  applyLoginItem();
  const next = effectiveSettings();
  stateServer?.setDemoMode(next.demoMode);
  const voiceStatus = voiceService?.setSpeakReplies(next.speakReplies);
  broadcastSettings();
  if (voiceStatus) broadcast("voice:status-changed", voiceStatus);
  return next;
}

function registerIpc(): void {
  ipcMain.on("approval:ready", (event) => {
    event.sender.send("approval:request", stateServer?.getApprovalRequest() ?? null);
  });
  ipcMain.on("approval:answer", (_event, id: unknown, decision: unknown) => {
    if (typeof id === "string" && (decision === "approve" || decision === "deny")) {
      stateServer?.answerApproval(id, decision);
    }
  });
  // Surfaced persistently in the wizard chrome and the HUD settings pane so a
  // customer (and support) can read the running build at a glance.
  ipcMain.handle("app:get-version", () => app.getVersion());
  // Path→file:// conversion lives HERE because the sandboxed preload has no
  // real `node:url` (its shim lacks pathToFileURL — the silent-voice P0 in
  // v1.4.0). Pure string work, no filesystem access, absolute paths only.
  ipcMain.handle("path:to-file-url", (_event, filePath: unknown) => {
    const value = String(filePath ?? "");
    if (value === "" || !path.isAbsolute(value)) {
      throw new Error("Only absolute paths can be converted to a file URL.");
    }
    return pathToFileURL(value).toString();
  });
  ipcMain.handle("settings:get", () => effectiveSettings());
  ipcMain.handle("gateway:status", (): GatewayStatus => (
    launchBlocked === undefined
      ? ensureGateway().status()
      // No engine was started, and the renderer must say so in the same place
      // it reports every other engine state rather than looking merely idle.
      : { state: "unavailable", port: 0, verified: false, detail: launchBlocked }
  ));
  ipcMain.handle("gateway:restart", async () => {
    // Restarting is not a way around the block: the reason it exists is that
    // starting an engine here is the unsafe act.
    if (launchBlocked !== undefined) return "unavailable";
    const result = await ensureGateway().restart();
    broadcastSettings();
    return result.state;
  });
  // One authority for every settings change (Lane C), which also strips the
  // supervisor-owned fields and reapplies the login item (Lane B).
  ipcMain.handle("settings:save", async (_event, patch: Partial<HudSettings>) => applySettings(patch));
  // Hermes archives are small and read-only, so the synchronous reads finish in
  // a millisecond or two; keeping them on the main process avoids handing the
  // renderer a filesystem path it could wander away from.
  ipcMain.handle("history:list-sessions", (_event, role: string, offset: unknown) =>
    listSessions(String(role ?? ""), { offset: typeof offset === "number" ? offset : 0 }),
  );
  ipcMain.handle("history:get-transcript", (_event, role: string, sessionId: string) =>
    getTranscript(String(role ?? ""), String(sessionId ?? "")),
  );
  ipcMain.handle("shell:open-external", async (_event, url: string) => {
    const protocol = safeProtocol(url);
    if (protocol !== "http:" && protocol !== "https:") return;
    await shell.openExternal(url);
  });
  // Backs the wizard's PASTE button. Reading the clipboard in the renderer
  // would need a permission dance on file:// pages; a main-process read is one
  // line and behaves identically on macOS, Windows and Linux.
  ipcMain.handle("clipboard:read-text", () => clipboard.readText());
  // Writing goes through the main process for the same file:// reasons as the
  // read above; backs COPY DIAGNOSTICS on the wizard failure panel and in the
  // settings dialog.
  ipcMain.handle("clipboard:write-text", (_event, text: unknown) => {
    clipboard.writeText(typeof text === "string" ? text : "");
  });
  ipcMain.handle("copy:no-brain-line", () => NO_BRAIN_LINE);
  ipcMain.handle("diagnostics:get", async (): Promise<DiagnosticsReport> => {
    const configDir = jarvisConfigDir();
    const profileDir = jarvisProfileDir();
    // Same opt-out gate as the CLI and setup host: when telemetry is off, don't
    // mint telemetry.json or show a code that correlates to no server event.
    const supportCode = (await telemetryEnabled({ dryRun, profileDir }))
      ? await loadSupportCode(configDir, { mint: false }).catch(() => undefined)
      : undefined;
    const lastRun = setupHost?.lastRunRecord() ?? await readLastRun(configDir);
    const lastCrash = await readLastCrash(configDir).catch(() => undefined);
    let brainLedger: DiagnosticsInfo["brainLedger"];
    try {
      const stored = JSON.parse(await fs.readFile(path.join(profileDir, "brain-ledger.json"), "utf8")) as {
        schema: 1;
        providers: Record<string, { days?: Record<string, { turns: number; input_tokens: number; output_tokens: number; by_class: Record<string, number>; models?: Record<string, number> }> }>;
      };
      brainLedger = Object.entries(stored.providers).flatMap(([provider, entry]) => {
        const days = Object.entries(entry.days ?? {})
          .map(([date, day]) => ({ date, turns: day.turns, inputTokens: day.input_tokens, outputTokens: day.output_tokens, byClass: day.by_class, models: day.models ?? {} }));
        return days.length === 0 ? [] : [{ provider, days }];
      });
    } catch {
      // Missing before v1.6 and on API-key installs; diagnostics stay unchanged.
    }
    // Lane C (voice): the live engine state, not a stale setup result.
    const voiceStatus = voiceService?.status();
    return {
      ...(supportCode === undefined ? {} : { supportCode }),
      text: formatDiagnostics({
        supportCode,
        appVersion: app.getVersion(),
        platform: process.platform,
        arch: process.arch,
        packaged,
        lastRun,
        ...(lastCrash === undefined ? {} : { lastCrash }),
        ...(brainLedger === undefined ? {} : { brainLedger }),
        ...(voiceStatus === undefined ? {} : {
          voice: {
            available: voiceStatus.available,
            ...(voiceStatus.reason === undefined ? {} : { reason: voiceStatus.reason }),
            voiceId: voiceStatus.voiceId,
            speakReplies: voiceStatus.speakReplies,
            chatTts: voiceStatus.chatTts,
            chatFailures: voiceStatus.chatFailures,
            mic: voiceStatus.mic,
          },
        }),
      }),
    };
  });
  ipcMain.handle("setup:rerun", async () => {
    const options = {
      type: "question" as const,
      message: "Run setup again?",
      detail: "JARVIS will close and reopen in the setup screen, and the current dashboard session will end.",
      buttons: ["Run Setup", "Cancel"],
      defaultId: 1,
      cancelId: 1,
    };
    const focusedWindow = BrowserWindow.getFocusedWindow();
    const result = focusedWindow === null
      ? await dialog.showMessageBox(options)
      : await dialog.showMessageBox(focusedWindow, options);
    if (result.response === 0) relaunchIntoSetup();
  });
  ipcMain.handle("setup:connect-slack", async () => {
    const options = {
      type: "question" as const,
      message: "Connect Slack?",
      detail: "JARVIS will close and reopen in the setup screen for the Slack step, and the current dashboard session will end.",
      buttons: ["Connect Slack", "Cancel"],
      defaultId: 1,
      cancelId: 1,
    };
    const focusedWindow = BrowserWindow.getFocusedWindow();
    const result = focusedWindow === null
      ? await dialog.showMessageBox(options)
      : await dialog.showMessageBox(focusedWindow, options);
    if (result.response === 0) relaunchIntoSetup("--slack");
  });
}

// --- Lane C (voice) ---------------------------------------------------------

/**
 * Electron needs both handlers to cover media completely: the request handler
 * answers getUserMedia, the check handler answers Chromium's synchronous
 * capability queries. They are given DIFFERENT detail shapes — `mediaTypes`
 * (array) for a request, `mediaType` (string) for a check — so both are
 * normalised into one policy that denies anything it cannot positively
 * identify as our own main frame asking for audio while a press is live.
 */
function registerVoicePermissions(): void {
  const describe = (
    permission: string,
    details: unknown,
    contents: Electron.WebContents | null,
    fallbackUrl: string,
    consuming: boolean,
  ): Parameters<typeof decideVoicePermission>[0] => {
    const info = (details ?? {}) as {
      isMainFrame?: boolean;
      requestingUrl?: string;
      mediaTypes?: string[];
      mediaType?: string;
      frame?: { routingId?: number };
    };
    return {
      permission,
      consuming,
      isMainFrame: info.isMainFrame === true,
      requestingUrl: info.requestingUrl ?? fallbackUrl,
      ...(Array.isArray(info.mediaTypes) ? { mediaTypes: info.mediaTypes } : {}),
      ...(typeof info.mediaType === "string" ? { mediaType: info.mediaType } : {}),
      ...(contents === null ? {} : { contentsId: contents.id }),
      ...(info.frame?.routingId === undefined ? {} : { frameId: info.frame.routingId }),
    };
  };

  const current = session.defaultSession;
  // The REQUEST consumes the grant (one press, one getUserMedia). The CHECK
  // does not: Chromium fires several capability checks around a single
  // request, and consuming on the first would deny the real one behind it.
  current.setPermissionRequestHandler((contents, permission, callback, details) => {
    callback(decideVoicePermission(describe(permission, details, contents, contents?.getURL() ?? "", true), micGate));
  });
  current.setPermissionCheckHandler((contents, permission, requestingOrigin, details) =>
    decideVoicePermission(describe(permission, details, contents, requestingOrigin ?? "", false), micGate),
  );
}

/**
 * Voice IPC. Every handler resolves — a rejection here would surface in the
 * renderer as an unhandled promise on the chat path, and voice is never
 * allowed to damage typed chat.
 */
function registerVoiceIpc(): void {
  const service = voiceService;
  ipcMain.handle("voice:status", async () => (service === null ? null : await service.init()));
  ipcMain.handle("voice:set-speak-replies", async (_event, enabled: unknown) => {
    await applySettings({ speakReplies: enabled === true });
    return service?.status() ?? null;
  });
  ipcMain.handle("voice:speak", (_event, text: unknown) => service?.speak(String(text ?? "")) ?? null);
  ipcMain.handle("voice:acknowledgement", (_event, text: unknown) => service?.acknowledgement(String(text ?? "")) ?? null);
  ipcMain.handle("voice:cancel", () => service?.cancel());
  ipcMain.handle("voice:report-mic", (_event, state: unknown) => {
    const next = service?.setMicState(state as VoiceMicState) ?? null;
    if (next !== null) broadcast("voice:status-changed", next);
    return next;
  });
  // The gate is bound to the frame that asked and expires on its own, so a
  // renderer that crashes mid-press cannot leave the microphone open to the
  // next thing that asks.
  ipcMain.handle("voice:arm-mic", (event) =>
    micGate.arm({ contentsId: event.sender.id, frameId: event.senderFrame?.routingId ?? undefined }));
  ipcMain.handle("voice:disarm-mic", (_event, token: unknown) => {
    // Conditional: only the attempt that armed the gate may close it.
    micGate.disarm(typeof token === "number" ? token : undefined);
  });
  ipcMain.handle("voice:transcribe", async (_event, pcm: unknown) => {
    if (service === null) throw new Error("Voice is not available on this installation.");
    if (!(pcm instanceof Uint8Array)) throw new Error("Voice received an unusable recording.");
    // Enforced here as well as in preload: a compromised renderer can call IPC
    // directly, and this check must not depend on the sender's good behaviour.
    if (pcm.byteLength > MAX_PCM_BYTES) throw new Error("That recording is too long to transcribe.");
    return service.transcribe(Buffer.from(pcm.buffer, pcm.byteOffset, pcm.byteLength));
  });
  ipcMain.handle("voice:prepare-brief", async (_event, date: unknown, text: unknown) => {
    if (service === null) return null;
    const briefDate = String(date ?? "");
    const briefText = String(text ?? "");
    const audioPath = await service.prepareBrief(briefDate, briefText).catch(() => null);
    // Commit it, so the next PLAY is a cache hit through ordinary state rather
    // than another round trip. Conditional: a brief superseded while this was
    // synthesizing must not be resurrected.
    if (audioPath !== null) stateServer?.setBriefAudioPathIfCurrent(briefDate, briefText, audioPath);
    // State keeps storing the PATH; the renderer gets the playable URL too,
    // converted here because the sandboxed preload cannot do it.
    return audioPath === null ? null : { audioPath, audioUrl: pathToFileURL(audioPath).toString() };
  });
}

/**
 * Pre-generate the spoken brief the moment the agent stores one, so PLAY is a
 * cached file rather than a thirty-second wait. Fire-and-forget by design: a
 * brief that fails to synthesize must still appear as text.
 */
function onBriefStored(brief: { date: string; text: string; audioPath: string | null }): void {
  if (voiceService === null || brief.audioPath !== null) return;
  void voiceService
    .prepareBrief(brief.date, brief.text)
    .then((audioPath) => {
      // Conditional commit, NOT storeBrief(): synthesis of a long brief takes
      // tens of seconds, and brief A finishing after brief B was stored must
      // not restore A's text or recompute nextBriefAt.
      if (audioPath !== null) stateServer?.setBriefAudioPathIfCurrent(brief.date, brief.text, audioPath);
    })
    .catch(() => {});
}

// --- end Lane C -------------------------------------------------------------

function registerSetupIpc(): void {
  setupHost = createSetupHost({
    dryRun,
    packaged,
    // --setup / RUN SETUP AGAIN is an explicit human request, not a resume:
    // every step runs again (each is idempotent and fast when nothing
    // changed), so a revoked key is re-verified instead of retained (Codex
    // review 2026-08-14, finding 4). Lifecycle-forced setup (unsafe route,
    // unmigrated machine) opens WITHOUT the flag and keeps resume semantics.
    rerunAll: setupForced,
    slackOnly: process.argv.includes("--slack"),
    repoRoot: setupRepoRoot,
    appVersion: app.getVersion(),
    ...(vendorRoot === undefined ? {} : { vendorRoot }),
    gateway: ensureGateway(),
    emitEvent: (event: SetupEvent) => {
      // No key adoption any more: the wizard drives the SAME supervisor this
      // process owns, so there is nothing to copy between components.
      broadcast("setup:event", event);
    },
    emitPrompt: (prompt: SetupPrompt) => broadcast("setup:prompt", prompt),
  });

  ipcMain.handle("setup:start", async () => {
    if (setupHost === null) return { failed: true };
    return setupHost.start();
  });
  ipcMain.on("setup:answer", (_event, answer: SetupAnswer) => {
    setupHost?.answer(answer.promptId, answer.value);
  });
  ipcMain.handle("setup:lifecycle-debt", async () => {
    const decision = await authorizeGatewayStart({ profileDir: jarvisProfileDir() });
    return { blocked: !decision.authorized, detail: decision.detail };
  });
  ipcMain.handle("setup:launch-hud", async (_event, opts?: {
    entrance?: string;
    finished?: { name?: unknown; props?: unknown };
  }) => {
    // The payload shapes the arrival, never the checks: mayLeaveSetup and the
    // gateway gate inside launchHud answer exactly as they always have.
    await launchHud(opts?.entrance === "fast" ? "fast" : "full");
    // Only AFTER the checks passed (review r3 P2): the completed outcome is
    // recorded here because a refusal above must never read as a completed
    // handoff, and the renderer may not survive a successful one long enough
    // to report it. Same closed-set gate as setup:transition-event.
    if (opts?.finished?.name === "hud_transition_finished") {
      const props = typeof opts.finished.props === "object" && opts.finished.props !== null
        ? opts.finished.props as Record<string, unknown>
        : {};
      void enqueueOutOfBandTelemetry(jarvisConfigDir(), app.getVersion(), [{ name: "hud_transition_finished", props }], {
        profileDir: jarvisProfileDir(),
        dryRun,
        packaged,
      }).catch(() => {});
    }
  });
  // Transition telemetry (fire-and-forget from the renderer): persist-first
  // into the same queue file, endpoint and opt-out surface the setup batches
  // use — a quit mid-transition loses nothing.
  ipcMain.on("setup:transition-event", (_event, payload: { name?: unknown; props?: unknown }) => {
    const name = payload?.name;
    if (name !== "hud_transition_started" && name !== "hud_transition_finished") return;
    const props = typeof payload.props === "object" && payload.props !== null
      ? payload.props as Record<string, unknown>
      : {};
    void enqueueOutOfBandTelemetry(jarvisConfigDir(), app.getVersion(), [{ name, props }], {
      profileDir: jarvisProfileDir(),
      dryRun,
      packaged,
    }).catch(() => {});
  });
}

function crashInfo(): { appVersion: string; platform: string } {
  return { appVersion: app.getVersion(), platform: process.platform };
}

/**
 * Renderer and child-process deaths the main process can observe directly. A
 * "clean-exit" is ordinary teardown (navigation, a closed window), never a
 * crash. details.reason is one of Electron's closed-set codes, which is
 * anonymous, so it is safe to record verbatim.
 */
function registerCrashListeners(): void {
  app.on("render-process-gone", (_event, _contents, details) => {
    if (details.reason === "clean-exit") return;
    void recordProcessGone(jarvisConfigDir(), "render-process-gone", details.reason, crashInfo());
  });
  app.on("child-process-gone", (_event, details) => {
    if (details.reason === "clean-exit") return;
    const reason = details.type === "GPU" ? "gpu-process-gone" : "child-process-gone";
    void recordProcessGone(jarvisConfigDir(), reason, details.reason, crashInfo());
  });
}

function safeProtocol(url: string): string {
  try {
    return new URL(url).protocol;
  } catch {
    return "";
  }
}

app
  .whenReady()
  .then(async () => {
    // A secondary instance already called app.quit() above; it must not create
    // a window, a state server, or — above all — a second engine.
    if (!isPrimaryInstance) return;
    // Dev runs use the bare electron binary, whose dock icon is Electron's;
    // packaged builds get the bundle icon from electron-builder instead.
    if (process.platform === "darwin" && !app.isPackaged) {
      const icon = nativeImage.createFromPath(iconPath);
      if (!icon.isEmpty()) app.dock?.setIcon(icon);
    }

    const userData = app.getPath("userData");
    settingsPath = path.join(userData, "settings.json");
    await loadSettings();

    // Crash visibility: a pid-keyed marker surviving from a previous run means
    // that instance died without a clean quit (main-process kill, OS crash,
    // power loss). Detect now; the fresh marker for THIS run is written only
    // after the window exists (below), so its Windows session-end handler is in
    // place before the marker can go stale on an OS shutdown during startup.
    const crashDir = jarvisConfigDir();
    await detectDirtyShutdown(crashDir, crashInfo());
    registerCrashListeners();

    // --- Lane A (v1.4.0): provider safety before anything can start. ------
    // A completed install never runs setup again, so an unsafe legacy route
    // (Codex, or the old unkeyed Anthropic seed) would keep being served by the
    // gateway forever. This runs first, on every launch, and is a no-op once
    // the profile is clean.
    const safety = await runProviderSafetyOnLaunch();
    // --- end Lane A -------------------------------------------------------
    // Two independent reasons this machine may not simply open: a provider
    // route that could not be proven safe, and a 1.3.x install that has not
    // been migrated. They are tracked SEPARATELY from the ordinary setup
    // decision because nothing later — least of all a failure to stage content
    // — may downgrade them (integration review r1, finding 2).
    const migrationRequired = !(await hasMigrated());
    const lifecycleForcesSetup = safety.forceSetup || safety.blocked || migrationRequired;
    // A cleared route (or a gateway still serving the old one) must land the
    // customer in setup, whatever the saved progress says.
    const wantsSetup = lifecycleForcesSetup || (await shouldRunSetup());
    let contentStaged = true;
    if (packaged) {
      // Runs on every packaged launch, not only before a wizard run: a staged
      // copy missing an entry this build ships (the Slack manifest the setup
      // walkthrough sends buyers to, for one) would otherwise never be
      // repaired on an install that has already finished setup.
      const prepared = await preparePackagedSetupContent({
        bundledRoot: bundledRepoRoot,
        hermesRoot: resolveHermesRoot(),
        prepare: preparePackagedContent,
        showErrorBox: (title, content) => dialog.showErrorBox(title, content),
        required: wantsSetup,
        mustNotOpenDashboard: lifecycleForcesSetup,
        reportError: (error) => console.error("Unable to stage packaged JARVIS content.", error),
      });
      contentStaged = prepared.staged;
      if (prepared.staged) setupRepoRoot = prepared.repoRoot;
    }
    // ONE composition, in a pure function that is tested on its own: a staging
    // failure can lose the wizard, but it can never turn a forced launch into
    // an ordinary one (integration review r1, finding 2).
    const launchMode = decideLaunchMode({ lifecycleForcesSetup, wantsSetup, contentStaged });
    setupMode = launchMode === "setup";
    if (launchMode === "blocked") {
      // The wizard cannot run (no content) and the dashboard must not open (an
      // unsafe route or an unmigrated 1.3.x install is exactly what the forced
      // setup was there to handle). So: a window, an honest reason, NO engine.
      launchBlocked = migrationRequired && !safety.forceSetup && !safety.blocked
        ? "JARVIS could not prepare the files its setup needs, so it has not started its engine — an earlier JARVIS is still installed on this computer and must be cleaned up first. Free up disk space or reinstall JARVIS, then open it again."
        : "JARVIS could not prepare the files its setup needs, so it has not started its engine — this profile's AI provider could not be confirmed safe. Free up disk space or reinstall JARVIS, then open it again.";
    }
    registerIpc();
    registerContextMenu();
    applyLoginItem();
    // --- Lane C (voice) ---------------------------------------------------
    // Independent of the engine lifecycle in both directions: voice must not
    // wait for a gateway, and a gateway that never starts must still leave a
    // HUD that can hear and speak.
    voiceService = createVoiceService({
      speakRepliesDefault: settings.speakReplies,
      forkTts: () => utilityProcess.fork(path.join(__dirname, "voice-tts.cjs"), [], { serviceName: "JARVIS speech" }),
      onStatusChanged: (status) => broadcast("voice:status-changed", status),
      voiceRoot: resolveVoiceRoot({ packaged: false }),
      ...(process.resourcesPath === undefined ? {} : { resourcesPath: process.resourcesPath }),
      userDataDir: userData,
      appVersion: app.getVersion(),
      packaged,
    });
    registerVoicePermissions();
    registerVoiceIpc();
    // Warm the asset check after the window is on its way rather than before
    // it: hashing 200 MB on first launch must never sit in front of the HUD
    // appearing. Failures resolve into an honest status, never a throw.
    void voiceService.init().catch(() => {});
    // --- end Lane C -------------------------------------------------------
    if (setupMode) {
      // The wizard's Start Engine step spawns the supervised gateway, and the
      // engine discovers MCP servers ONCE at boot — 4870 must already be listening
      // or the hud entry is dead for that process's whole life. Non-fatal: a port
      // conflict must not cost the customer the wizard.
      await startStateServer().catch((error) => console.warn("Unable to start the HUD state server.", error));
      registerSetupIpc();
    } else if (launchBlocked !== undefined) {
      // The blocked surface: the app opens and says why, and the one thing it
      // does NOT do is start an engine.
      console.error(`JARVIS start blocked: ${launchBlocked}`);
      await startStateServer();
    } else {
      await startStateServer();
      // Fire-and-forget: the window must not wait on an engine that is slow to
      // boot, and the renderer learns the live port from the status broadcast.
      void startGateway().catch((error) => {
        console.warn("Unable to start the JARVIS engine.", error);
      });
    }

    createWindow();
    // The window (with its session-end handler) now exists, so a fresh marker
    // written here is covered on every clean-exit path. A shutdown BEFORE this
    // point simply leaves no marker — a missed startup crash is far better than
    // a phantom one on every OS restart.
    markSessionStartSync(crashDir, crashInfo());

    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) {
        createWindow();
      }
    });
  })
  .catch((error) => {
    console.error("Unable to start JARVIS HUD.", error);
    process.exitCode = 1;
  });

// Setup is a one-shot flow: closing its window ends the session on every
// platform rather than stranding a half-unwound run behind an empty dock icon.
app.on("window-all-closed", () => {
  setupHost?.cancel();
  if (setupMode || process.platform !== "darwin") {
    app.quit();
  }
});

let setupDrained = false;
app.on("before-quit", (event) => {
  setupHost?.cancel();
  // Lane C (voice): kill any speech child before the process goes. Electron
  // does not reap our grandchildren for us, and a Kokoro run mid-sentence at
  // quit would otherwise survive the app that spawned it. The setup host owns
  // a SECOND engine during its self-test, which this service cannot reach —
  // hence both calls.
  voiceService?.dispose();
  setupHost?.disposeResources();
  // Reaching before-quit IS intent to exit cleanly, so clear the marker now,
  // synchronously (an async unlink could lose the race on the fast quit path
  // below). A hard kill during the brief drain that follows is treated as part
  // of that intended quit, not a crash — only a kill with no before-quit at all
  // leaves the marker behind to be detected next boot.
  clearSessionMarkerSync(jarvisConfigDir());
  // A cancelled setup run still has to persist its telemetry (abandoned runs
  // are the signal). Electron won't wait for pending promises, so quitting is
  // deferred until the host settles — capped so a wedged flush can't trap the
  // process. The cancel above rejects all prompts, so settling is quick.
  if (stopping) return;
  stopping = true;
  event.preventDefault();
  const cap = new Promise<void>((resolve) => setTimeout(resolve, 8000));
  // Killing the engine is not best-effort: an orphaned gateway holding the
  // profile lock is exactly the state 1.4.0 exists to make impossible.
  const drain = (async () => {
    // Concurrently, under ONE cap: each of these is slow in its own way (a
    // telemetry flush, a child that will not die, a server draining), and run
    // in sequence a slow first task spends the whole budget — which would leave
    // exactly the orphaned engine v1.4.0 exists to make impossible. The voice
    // children are already gone: dispose() above is synchronous.
    const tasks: Promise<unknown>[] = [];
    if (setupHost !== null && !setupDrained) {
      setupDrained = true;
      tasks.push(setupHost.idle());
    }
    tasks.push((gateway?.stop() ?? Promise.resolve(true)).catch((error: unknown) => {
      console.error("Unable to stop the JARVIS engine cleanly.", error);
    }));
    tasks.push((stateServer?.stop() ?? Promise.resolve()).catch((error: unknown) => {
      console.error("Unable to stop HUD state server cleanly.", error);
    }));
    await Promise.all(tasks);
  })();
  void Promise.race([drain, cap]).finally(() => {
    app.quit();
  });
});

/**
 * Clear unsafe legacy provider routes before the HUD does anything else.
 *
 * Deliberately quiet and best-effort: it must never stop the app opening. What
 * it changes is written into the profile and reported by the Model step the
 * next time setup runs; what matters here is that the gateway cannot be started
 * against a route that can reach the Codex-import or Claude-Code replay path.
 */
async function runProviderSafetyOnLaunch(): Promise<{ forceSetup: boolean; blocked: boolean }> {
  try {
    // A launch has no wizard UI to narrate into, so the IO sink is silent: the
    // notes go to the log here and are re-reported by the Model step when the
    // customer next opens setup.
    const silent: SetupIO = {
      inheritsStdio: false,
      async confirm(_message: string, defaultValue: boolean) { return defaultValue; },
      async select(_message, _choices, defaultKey) { return defaultKey; },
      async promptText(_message, defaultValue) { return defaultValue; },
      async promptSecret() { return ""; },
      deviceCode() {},
      note() {},
      warn() {},
      error() {},
      command() {},
      commandOutput() {},
      block() {},
    };
    const ctx = createContext({ dryRun: false, packaged, io: silent, repoRoot: bundledRepoRoot });
    const result = await ensureProviderSafety(ctx);
    for (const note of result.notes) console.log(`[provider-safety] ${note}`);
    // `blocked` is the stronger claim: not "something changed" but "this profile
    // is NOT PROVEN SAFE". It survives a content-staging failure, which
    // forceSetup alone did not (integration review r1, finding 2).
    return { forceSetup: result.forceSetup, blocked: result.blocked };
  } catch (error) {
    console.error("Provider-safety check could not run.", error);
    // A check that failed proves nothing — but if a restart was already owed,
    // the debt is still owed, so the customer must land in setup rather than an
    // HUD quietly talking to a gateway with the forbidden route loaded.
    // A check that could not run proves nothing at all, so this launch is
    // blocked whether or not a restart was already owed — the debt is only
    // logged, because it no longer changes the decision.
    if (await restartDebtOutstanding()) {
      console.error("[provider-safety] a gateway restart was already owed before this failure.");
    }
    return { forceSetup: true, blocked: true };
  }
}

/** Reads the durable "a gateway may still be serving the removed route" flag. */
async function restartDebtOutstanding(): Promise<boolean> {
  try {
    const profileDir = path.join(resolveHermesRoot(), "profiles", "jarvis");
    return (await readSetupProgressAt(profileDir))?.providerSafetyRestartPending === true;
  } catch {
    return false;
  }
}
