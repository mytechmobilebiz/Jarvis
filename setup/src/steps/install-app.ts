import { constants as fsConstants } from "node:fs";
import fs from "node:fs/promises";
import path from "node:path";
import { WizardError, type StepOutcome, type WizardContext } from "../context.js";
import { ensureDir, pathExists } from "../fs-utils.js";
import { runCommand } from "../system.js";
import { note } from "../ui.js";

const APP_NAME = "JARVIS HUD";
const INSTALL_HINT = "Run pnpm run install-app after fixing the error above.";
/** Matches executableName in hud/package.json build.linux. */
const LINUX_EXECUTABLE = "jarvis-hud";
/** electron-builder names the output linux-unpacked on x64 and linux-<arch>-unpacked elsewhere. */
const isLinuxOutput = (name: string): boolean => name.startsWith("linux") && name.endsWith("unpacked");
const WINDOWS_EXECUTABLE = `${APP_NAME}.exe`;
const isWindowsOutput = (name: string): boolean =>
  name.startsWith("win") || /\.(?:exe|blockmap)$/i.test(name) || name.toLowerCase() === "latest.yml";
type PlatformInstaller = (ctx: WizardContext) => Promise<StepOutcome>;

export interface WindowsProcessRecord {
  ProcessId: number;
  ParentProcessId: number;
  Name: string;
}

export async function installApp(
  ctx: WizardContext,
  platform: NodeJS.Platform = process.platform,
  installerOverride?: PlatformInstaller,
): Promise<StepOutcome> {
  if (ctx.packaged) {
    return {
      status: "skipped",
      notes: [`${APP_NAME} is already installed; packaged setup does not rebuild or replace the running app.`],
    };
  }

  const installer = installerOverride
    ?? (platform === "win32" ? installWindows : platform === "darwin" ? installMac : installLinux);
  try {
    return await installer(ctx);
  } catch (error) {
    const failure = error instanceof WizardError
      ? `${error.message} ${error.hint}`
      : error instanceof Error
        ? error.message
        : String(error);
    return {
      status: "skipped",
      notes: [
        `Desktop app installation did not finish: ${failure}`,
        `The ${APP_NAME} desktop app shortcut was not created.`,
        "JARVIS still runs from the launcher or directly from this checkout.",
        "Run pnpm run install-app any time to retry.",
      ],
    };
  }
}

async function installMac(ctx: WizardContext): Promise<StepOutcome> {
  const notes: string[] = [];
  await clearPreviousOutput(ctx, (name) => name.startsWith("mac"));
  await buildBundle(ctx, ["--mac", "--dir"]);

  if (ctx.dryRun) {
    notes.push(`[dry-run] would close a running ${APP_NAME} before replacing the installed app.`);
    notes.push(`[dry-run] would copy hud/dist-app/mac*/${APP_NAME}.app into /Applications, or ~/Applications when /Applications is read-only.`);
    notes.push(`[dry-run] would then hand you ${APP_NAME} as a normal double-click app.`);
    return { status: "done", notes };
  }

  const built = await findBuiltApp(ctx);
  if (built === undefined) {
    throw new WizardError(`The built ${APP_NAME}.app was not found under hud/dist-app.`, INSTALL_HINT);
  }
  notes.push(`Built ${APP_NAME}.app on this machine with electron-builder.`);

  const target = await chooseApplicationsDir(ctx);
  const destination = path.join(target.dir, `${APP_NAME}.app`);
  // Immediately before the swap, not earlier: the build takes minutes, plenty
  // of time for the app to be (re)launched after an earlier check passed.
  await quitRunningApp(ctx, notes, destination);
  if (await pathExists(destination)) {
    await fs.rm(destination, { force: true, recursive: true });
    notes.push(`Removed the previous ${destination}.`);
  }

  const copy = await runCommand(ctx, "ditto", [built, destination], { stream: false });
  if (copy.status !== 0) {
    throw new WizardError(`Copying ${APP_NAME}.app to ${target.dir} did not complete.`, INSTALL_HINT);
  }
  notes.push(`Installed ${destination}.`);
  if (target.fallback) {
    notes.push("/Applications was not writable, so the app went to your personal Applications folder instead.");
  }
  notes.push(`Launch ${APP_NAME} from Spotlight or the Dock from now on; the terminal is no longer needed.`);
  notes.push("Because the app was built here rather than downloaded, macOS opens it without a Gatekeeper warning.");

  return { status: "done", notes };
}

async function installWindows(ctx: WizardContext): Promise<StepOutcome> {
  const notes: string[] = [];
  await clearPreviousWindowsOutput(ctx);
  await buildBundle(ctx, ["--win", "nsis"]);

  if (ctx.dryRun) {
    notes.push(`[dry-run] would close other running ${WINDOWS_EXECUTABLE} process trees by PID before installation.`);
    notes.push(`[dry-run] would launch the freshly built NSIS installer from hud/dist-app.`);
    notes.push(`[dry-run] would then hand you ${APP_NAME} from the taskbar or Start menu.`);
    return { status: "done", notes };
  }

  const installer = await findWindowsInstaller(ctx);
  if (installer === undefined) {
    throw new WizardError(`The built ${APP_NAME} NSIS installer was not found under hud/dist-app.`, INSTALL_HINT);
  }
  notes.push(`Built the ${APP_NAME} NSIS installer on this machine with electron-builder.`);
  await quitRunningWindowsApp(ctx, notes);

  const launched = await runCommand(ctx, installer, [], { stream: true });
  if (launched.status !== 0) {
    throw new WizardError(`The ${APP_NAME} installer did not complete.`, INSTALL_HINT);
  }
  notes.push(`Launched and completed ${path.basename(installer)}.`);
  notes.push(`Launch ${APP_NAME} from the taskbar or Start menu from now on; the terminal is no longer needed.`);
  return { status: "done", notes };
}

async function quitRunningWindowsApp(ctx: WizardContext, notes: string[]): Promise<void> {
  const initial = selectWindowsAppPids(await listWindowsProcesses(ctx), process.pid);
  if (initial.pids.length === 0) {
    if (initial.preservedCurrentTree) {
      notes.push(`Kept this ${APP_NAME} setup window running; no other JARVIS windows needed closing.`);
    }
    return;
  }

  for (const pid of initial.pids) {
    // PID targeting is deliberate: /IM would terminate the packaged wizard
    // that is currently executing this setup step. /T closes only this other
    // app root and its renderer/utility descendants.
    await runCommand(ctx, "taskkill.exe", ["/PID", String(pid), "/T", "/F"], { stream: false });
  }

  const remaining = selectWindowsAppPids(await listWindowsProcesses(ctx), process.pid);
  if (remaining.pids.length !== 0) {
    throw new WizardError(
      `Another ${APP_NAME} window is still running and could not be closed, so the installer was not launched.`,
      `Close other ${APP_NAME} windows, then run pnpm run install-app again.`,
    );
  }
  notes.push(`Closed other running ${APP_NAME} windows before launching the update; this setup process was left running.`);
}

async function listWindowsProcesses(ctx: WizardContext): Promise<WindowsProcessRecord[]> {
  const script = [
    "$processes = @(Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId, Name);",
    "ConvertTo-Json -InputObject $processes -Compress",
  ].join(" ");
  const listing = await runCommand(
    ctx,
    "powershell.exe",
    ["-NoProfile", "-Command", script],
    { stream: false },
  );
  if (listing.status !== 0) throw UNKNOWABLE("the Windows process scan failed");

  try {
    const parsed = JSON.parse(listing.stdout.replace(/^\uFEFF/, "")) as unknown;
    const records = Array.isArray(parsed) ? parsed : [parsed];
    return records.flatMap((record): WindowsProcessRecord[] => {
      if (typeof record !== "object" || record === null) return [];
      const raw = record as Record<string, unknown>;
      const ProcessId = Number(raw.ProcessId);
      const ParentProcessId = Number(raw.ParentProcessId);
      const Name = raw.Name;
      return Number.isInteger(ProcessId) && Number.isInteger(ParentProcessId) && typeof Name === "string"
        ? [{ ProcessId, ParentProcessId, Name }]
        : [];
    });
  } catch {
    throw UNKNOWABLE("the Windows process scan returned unreadable data");
  }
}

/** Select other app roots, excluding the current process and every descendant. */
export function selectWindowsAppPids(
  processes: WindowsProcessRecord[],
  currentPid: number,
): { pids: number[]; preservedCurrentTree: boolean } {
  const currentTree = new Set<number>([currentPid]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const record of processes) {
      if (!currentTree.has(record.ProcessId) && currentTree.has(record.ParentProcessId)) {
        currentTree.add(record.ProcessId);
        changed = true;
      }
    }
  }

  const matching = processes.filter((record) => record.Name.toLowerCase() === WINDOWS_EXECUTABLE.toLowerCase());
  const targets = new Map(
    matching
      .filter((record) => !currentTree.has(record.ProcessId))
      .map((record) => [record.ProcessId, record]),
  );
  const byPid = new Map(processes.map((record) => [record.ProcessId, record]));
  const hasTargetAncestor = (record: WindowsProcessRecord): boolean => {
    const visited = new Set<number>([record.ProcessId]);
    let parentPid = record.ParentProcessId;
    while (!visited.has(parentPid)) {
      if (targets.has(parentPid)) return true;
      visited.add(parentPid);
      const parent = byPid.get(parentPid);
      if (parent === undefined) return false;
      parentPid = parent.ParentProcessId;
    }
    return false;
  };

  return {
    pids: [...targets.values()]
      .filter((record) => !hasTargetAncestor(record))
      .map((record) => record.ProcessId)
      .sort((left, right) => left - right),
    preservedCurrentTree: matching.some((record) => currentTree.has(record.ProcessId)),
  };
}

async function findWindowsInstaller(ctx: WizardContext): Promise<string | undefined> {
  const installers = (await outputEntries(ctx))
    .filter((entry) => entry.isFile() && isWindowsInstallerName(entry.name))
    .map((entry) => entry.name)
    .sort((left, right) => Number(/setup/i.test(right)) - Number(/setup/i.test(left)));
  const name = installers[0];
  return name === undefined ? undefined : path.join(outputDir(ctx), name);
}

export function isWindowsInstallerName(name: string): boolean {
  return name.toLowerCase().endsWith(".exe");
}

/**
 * An unpacked directory rather than an AppImage: AppImage needs libfuse.so.2,
 * which Ubuntu 22.04 and newer no longer ship, so it would install cleanly and
 * then refuse to launch.
 */
async function installLinux(ctx: WizardContext): Promise<StepOutcome> {
  const notes: string[] = [];
  await clearPreviousOutput(ctx, isLinuxOutput);
  await buildBundle(ctx, ["--linux", "dir"]);

  const installDir = path.join(ctx.homeDir, ".local", "opt", "jarvis-hud");
  const iconDir = path.join(ctx.homeDir, ".local", "share", "icons");
  const desktopDir = path.join(ctx.homeDir, ".local", "share", "applications");
  const iconPath = path.join(iconDir, "jarvis-hud.png");
  const desktopPath = path.join(desktopDir, "jarvis-hud.desktop");

  if (ctx.dryRun) {
    notes.push(`[dry-run] would close a running ${LINUX_EXECUTABLE} before replacing the installed app.`);
    notes.push(`[dry-run] would replace ${installDir} with the built hud/dist-app/linux*unpacked directory.`);
    notes.push(`[dry-run] would copy hud/build/icon.png to ${iconPath} and write the ${APP_NAME} launcher to ${desktopPath}, pointing at ${path.join(installDir, LINUX_EXECUTABLE)}.`);
    return { status: "done", notes };
  }

  const built = await findLinuxBuild(ctx);
  if (built === undefined) {
    throw new WizardError("No built app directory was found under hud/dist-app.", INSTALL_HINT);
  }
  notes.push(`Built ${APP_NAME} on this machine with electron-builder.`);

  // Immediately before the swap, not earlier: the build takes minutes, plenty
  // of time for the app to be (re)launched after an earlier check passed.
  await quitRunningApp(ctx, notes, installDir);

  await fs.rm(installDir, { force: true, recursive: true });
  await ensureDir(ctx, path.dirname(installDir));
  await fs.cp(built, installDir, { recursive: true });
  const executable = await findLinuxExecutable(installDir);
  if (executable === undefined) {
    throw new WizardError(`No ${APP_NAME} executable was found in ${installDir}.`, INSTALL_HINT);
  }
  await fs.chmod(executable, 0o755);
  notes.push(`Installed ${installDir}.`);

  await ensureDir(ctx, iconDir);
  await fs.copyFile(path.join(ctx.repoRoot, "hud", "build", "icon.png"), iconPath);
  await ensureDir(ctx, desktopDir);
  await fs.writeFile(desktopPath, desktopEntry(executable, iconPath), "utf8");
  notes.push(`Wrote ${desktopPath}; launch ${APP_NAME} from your applications menu from now on, the terminal is no longer needed.`);
  // Ubuntu 23.10+ restricts unprivileged user namespaces via AppArmor, which
  // Electron's sandbox needs; the SUID helper is the documented way out.
  notes.push(
    `If ${APP_NAME} shows a sandbox error at launch (some distros restrict user namespaces), run: sudo chown root:root ${path.join(installDir, "chrome-sandbox")} && sudo chmod 4755 ${path.join(installDir, "chrome-sandbox")}`,
  );

  return { status: "done", notes };
}

async function findLinuxBuild(ctx: WizardContext): Promise<string | undefined> {
  for (const entry of await outputEntries(ctx)) {
    if (entry.isDirectory() && isLinuxOutput(entry.name)) return path.join(outputDir(ctx), entry.name);
  }
  return undefined;
}

/** Prefers the configured name, then any other executable file, in case that name drifts. */
async function findLinuxExecutable(installDir: string): Promise<string | undefined> {
  const configured = path.join(installDir, LINUX_EXECUTABLE);
  if (await pathExists(configured)) return configured;

  const skipped = new Set([".so", ".so.1", ".so.2", ".pak", ".dat", ".bin", ".json", ".html", ".png", ".dll"]);
  for (const entry of await fs.readdir(installDir, { withFileTypes: true })) {
    if (!entry.isFile()) continue;
    if ([...skipped].some((suffix) => entry.name.endsWith(suffix))) continue;
    return path.join(installDir, entry.name);
  }
  return undefined;
}

/**
 * Replacing an app bundle while it is running leaves the live process reading
 * mismatched offsets from the new archive — its window ends up rendering raw
 * file contents. Close it gracefully first (SIGKILL only as a last resort).
 *
 * Exact-name matching means the dev-mode HUD (whose process is Electron, not
 * the installed app name) can run this step without terminating itself.
 */
/**
 * Only processes launched from the install being replaced count — matching by
 * executable path (never by name) means another user's copy, a JARVIS_APP_DIR
 * test copy, or a same-name dev build elsewhere is neither killed nor allowed
 * to block the install. SIGTERM asks Electron to quit; SIGKILL is the fallback;
 * a survivor (e.g. another user's process we cannot signal from here, which the
 * swap genuinely would corrupt) aborts the install with the bundle untouched.
 */
async function quitRunningApp(ctx: WizardContext, notes: string[], installPath: string): Promise<void> {
  const find = () => findAppPids(ctx, installPath);
  const pids = await find();
  if (pids.length === 0) return;

  signalAll(pids, "SIGTERM");
  if (!(await waitFor(async () => (await find()).length === 0, 5_000))) {
    signalAll(await find(), "SIGKILL");
    if (!(await waitFor(async () => (await find()).length === 0, 2_000))) {
      throw new WizardError(
        `${APP_NAME} is still running and could not be closed, so the installed app was left untouched.`,
        `Quit ${APP_NAME} (it may belong to another logged-in user), then run pnpm run install-app again.`,
      );
    }
  }
  notes.push(`Closed the running ${APP_NAME} so the update could be installed; relaunch it once setup finishes.`);
}

async function findAppPids(ctx: WizardContext, installPath: string): Promise<number[]> {
  // Canonicalize before comparing: process executable paths come back resolved,
  // so a symlinked $HOME or ~/.local would otherwise hide a genuinely running
  // app. A missing install path means nothing can be running out of it. (A
  // process still holding deleted inodes from an older install keeps them
  // regardless of what we write; the swap cannot corrupt it.)
  let realInstallPath: string;
  try {
    realInstallPath = await fs.realpath(installPath);
  } catch {
    return [];
  }

  // macOS ps may report the launch-time (logical) path rather than the resolved
  // one, so the darwin match accepts either spelling of the install location.
  return process.platform === "darwin"
    ? macAppPids(ctx, [realInstallPath, installPath])
    : linuxAppPids(realInstallPath);
}

const UNKNOWABLE = (detail: string) =>
  new WizardError(
    `Whether ${APP_NAME} is currently running could not be determined (${detail}), so the installed app was left untouched.`,
    `Quit ${APP_NAME} if it is open, then run pnpm run install-app again.`,
  );

/** ps -o comm= prints full executable paths on macOS, and ps is base-system. */
async function macAppPids(ctx: WizardContext, installPaths: string[]): Promise<number[]> {
  const listing = await runCommand(ctx, "ps", ["-axo", "pid=,comm="], { stream: false });
  if (listing.status !== 0) throw UNKNOWABLE("ps failed");

  const prefixes = [...new Set(installPaths)].map((p) => p + path.sep);
  const pids: number[] = [];
  for (const line of listing.stdout.split("\n")) {
    const match = /^\s*(\d+)\s+(.+)$/.exec(line);
    if (match?.[1] === undefined || match[2] === undefined) continue;
    if (prefixes.some((prefix) => match[2].startsWith(prefix))) pids.push(Number(match[1]));
  }
  return pids;
}

/**
 * No pgrep/pkill dependency on Linux: minimal installs may lack procps, and a
 * missing pkill would make a "graceful quit" a silent no-op right before the
 * bundle swap. /proc and process.kill are always there and always verifiable.
 * The executable's location is the source of truth — no name filter, so a
 * previous install whose executable used findLinuxExecutable's fallback name is
 * still caught. exe readlink fails with EACCES for other users' processes,
 * which by definition are not running our per-user install.
 */
async function linuxAppPids(realInstallPath: string): Promise<number[]> {
  let entries: string[];
  try {
    entries = await fs.readdir("/proc");
  } catch {
    throw UNKNOWABLE("/proc is unreadable");
  }

  const pids: number[] = [];
  for (const entry of entries) {
    if (!/^\d+$/.test(entry)) continue;
    try {
      const exe = (await fs.readlink(path.join("/proc", entry, "exe"))).replace(/ \(deleted\)$/, "");
      if (exe.startsWith(realInstallPath + path.sep)) pids.push(Number(entry));
    } catch {
      // The process exited between readdir and read, or belongs to another user.
    }
  }
  return pids;
}

function signalAll(pids: number[], signal: NodeJS.Signals): void {
  for (const pid of pids) {
    try {
      process.kill(pid, signal);
    } catch {
      // ESRCH: already gone. EPERM: another user's process — the follow-up
      // check still sees it running and the install aborts instead of guessing.
    }
  }
}

async function waitFor(condition: () => Promise<boolean>, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await condition()) return true;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return condition();
}

/** Reruns must never install a leftover bundle from an earlier attempt. */
async function clearPreviousOutput(ctx: WizardContext, matches: (name: string) => boolean): Promise<void> {
  if (ctx.dryRun) {
    note(ctx, "[dry-run] would remove the previous electron-builder output under hud/dist-app.");
    return;
  }

  for (const entry of await outputEntries(ctx)) {
    if (entry.isDirectory() && matches(entry.name)) {
      await fs.rm(path.join(outputDir(ctx), entry.name), { force: true, recursive: true });
    }
  }
}

async function clearPreviousWindowsOutput(ctx: WizardContext): Promise<void> {
  if (ctx.dryRun) {
    note(ctx, "[dry-run] would remove the previous electron-builder output under hud/dist-app.");
    return;
  }

  for (const entry of await outputEntries(ctx)) {
    if (isWindowsOutput(entry.name)) {
      await fs.rm(path.join(outputDir(ctx), entry.name), { force: true, recursive: true });
    }
  }
}

async function buildBundle(ctx: WizardContext, args: string[]): Promise<void> {
  // First run downloads the Electron distribution, so this streams live.
  const build = await runCommand(ctx, "pnpm", ["exec", "electron-builder", ...args], {
    cwd: path.join(ctx.repoRoot, "hud"),
    stream: true,
  });
  if (build.status !== 0) {
    throw new WizardError(`electron-builder did not finish building ${APP_NAME}.`, INSTALL_HINT);
  }
}

/** JARVIS_APP_DIR keeps this testable and covers unusual install locations. */
async function chooseApplicationsDir(ctx: WizardContext): Promise<{ dir: string; fallback: boolean }> {
  const override = process.env.JARVIS_APP_DIR;
  if (override !== undefined && override.trim() !== "") {
    await fs.mkdir(override, { recursive: true });
    return { dir: override, fallback: false };
  }

  if (await isWritable("/Applications")) {
    return { dir: "/Applications", fallback: false };
  }

  const personal = path.join(ctx.homeDir, "Applications");
  await fs.mkdir(personal, { recursive: true });
  return { dir: personal, fallback: true };
}

async function isWritable(dir: string): Promise<boolean> {
  try {
    await fs.access(dir, fsConstants.W_OK);
    return true;
  } catch {
    return false;
  }
}

async function findBuiltApp(ctx: WizardContext): Promise<string | undefined> {
  for (const entry of await outputEntries(ctx)) {
    if (!entry.isDirectory() || !entry.name.startsWith("mac")) continue;
    const candidate = path.join(outputDir(ctx), entry.name, `${APP_NAME}.app`);
    if (await pathExists(candidate)) return candidate;
  }
  return undefined;
}

function outputDir(ctx: WizardContext): string {
  return path.join(ctx.repoRoot, "hud", "dist-app");
}

async function outputEntries(ctx: WizardContext) {
  try {
    return await fs.readdir(outputDir(ctx), { withFileTypes: true });
  } catch {
    return [];
  }
}

function desktopEntry(execPath: string, iconPath: string): string {
  return [
    "[Desktop Entry]",
    "Type=Application",
    `Name=${APP_NAME}`,
    "Comment=Your self-hosted AI chief of staff",
    `Exec=${execPath}`,
    `Icon=${iconPath}`,
    "Terminal=false",
    "Categories=Office;Utility;",
    "",
  ].join("\n");
}
