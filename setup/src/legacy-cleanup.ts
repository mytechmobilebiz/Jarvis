import path from "node:path";
import type { WizardContext } from "./context.js";
import { runCommand, type CommandResult } from "./system.js";

/**
 * The afterlife of v1.3.x's gateway lifecycle.
 *
 * 1.3.x installed the gateway as an OS auto-start entry — a scheduled task on
 * some machines (Justin), a Startup-folder `.vbs` on others (Kevin), a launchd
 * agent on macOS. 1.4.0 has no such entry: the app starts at login and
 * supervises the gateway itself. Anything left behind is not merely untidy —
 * a 1.3.x gateway on the SAME profile holds the engine's runtime lock and
 * would fight our child, and it answers with a key we no longer know.
 *
 * OWNERSHIP IS PROVEN, NEVER GUESSED (review r1, finding 8). This code deletes
 * scheduled tasks and kills processes, so the bar is: the registration or the
 * process must name THIS install's canonical hermes root or profile directory.
 * A filename containing "jarvis", or a bare `-p jarvis` on a command line, is
 * not proof — a second Hermes install on the same machine can carry both, and
 * destroying someone else's setup is a far worse failure than leaving a stale
 * entry we then report honestly. Anything unreadable is "unresolved": surfaced
 * to the customer, never touched.
 *
 * A foreign install is also no longer a threat to us: the supervised gateway
 * binds a free port, so nothing can squat it. Only OUR profile's leftovers can
 * still break us, and those are exactly what this can prove.
 */
export interface LegacyRegistration {
  kind: "scheduled-task" | "startup-vbs" | "launch-agent";
  /** Task name, or absolute file path for file-backed registrations. */
  id: string;
  ownership: "owned" | "foreign" | "unresolved";
}

export interface LegacyArtifacts {
  /**
   * Whether the scan itself was AUTHORITATIVE. A failed schtasks/CIM query used
   * to become an empty result, so "we could not look" was indistinguishable
   * from "there is nothing there" — and migration then certified a machine it
   * had never actually inspected (review r2, finding 5).
   */
  scanOk: boolean;
  /** Which queries failed, for the customer-facing explanation. */
  scanFailures: string[];
  registrations: LegacyRegistration[];
  /** PIDs proven to be gateways running from OUR hermes root. */
  gatewayPids: number[];
  /** Gateway-looking processes we could not attribute: reported, never killed. */
  unresolvedPids: number[];
  /** The 1.3.x engine checkout (a .git directory) that 1.4.0 replaces. */
  gitCheckout: boolean;
  /** Superseded credential lines in the profile .env. */
  staleEnvKeys: string[];
}

export interface LegacyCleanupResult {
  removed: LegacyRegistration[];
  failed: LegacyRegistration[];
  killedPids: number[];
  survivingPids: number[];
  /** PIDs that stopped matching us between detection and the kill. */
  skippedPids: number[];
  /** True when a failure looked like a privilege problem. */
  elevationLikely: boolean;
}

export interface LegacyDeps {
  platform?: NodeJS.Platform;
  run?: (
    ctx: WizardContext,
    command: string,
    args: string[],
    options: { stream: boolean },
  ) => Promise<CommandResult>;
  powershell?: string;
  /** Injected filesystem so tests never touch a real Startup folder. */
  listDir?: (dirPath: string) => Promise<string[]>;
  readFile?: (filePath: string) => Promise<string | undefined>;
  removeFile?: (filePath: string) => Promise<void>;
  env?: NodeJS.ProcessEnv;
  homeDir?: string;
}

const TASK_NAME_PATTERN = /^Hermes_Gateway/i;

export async function detectLegacyInstall(
  ctx: WizardContext,
  deps: LegacyDeps = {},
): Promise<LegacyArtifacts> {
  const platform = deps.platform ?? process.platform;
  const run = deps.run ?? runCommand;
  const registrations: LegacyRegistration[] = [];
  const failures: string[] = [];

  if (platform === "win32") {
    const tasks = await detectScheduledTasks(ctx, run, deps);
    registrations.push(...tasks.registrations);
    if (!tasks.ok) failures.push("scheduled-tasks");
    const startup = await detectStartupEntries(ctx, deps);
    registrations.push(...startup.registrations);
    if (!startup.ok) failures.push("startup-folder");
  } else if (platform === "darwin") {
    const agents = await detectLaunchAgents(ctx, deps);
    registrations.push(...agents.registrations);
    if (!agents.ok) failures.push("launch-agents");
  }

  const processes = await detectGatewayProcesses(ctx, platform, run, deps);
  if (!processes.ok) failures.push("process-scan");
  return {
    scanOk: failures.length === 0,
    scanFailures: failures,
    registrations,
    gatewayPids: processes.owned,
    unresolvedPids: processes.unresolved,
    gitCheckout: await fileExists(deps, path.join(ctx.hermesAgentDir, ".git")),
    staleEnvKeys: [],
  };
}

async function detectScheduledTasks(
  ctx: WizardContext,
  run: NonNullable<LegacyDeps["run"]>,
  deps: LegacyDeps,
): Promise<{ registrations: LegacyRegistration[]; ok: boolean }> {
  const query = await run(ctx, "schtasks.exe", ["/Query", "/FO", "CSV", "/NH"], { stream: false });
  // Exit 1 with "no tasks" is a normal clean machine; a query that could not
  // RUN is not, and must never be read as "there is nothing there".
  if (query.status !== 0 && !/no tasks/i.test(`${query.stdout}${query.stderr}`)) {
    return { registrations: [], ok: false };
  }
  const names = new Set<string>();
  for (const line of query.stdout.split(/\r?\n/)) {
    const first = /^"([^"]*)"/.exec(line.trim())?.[1];
    if (first === undefined) continue;
    const name = first.replace(/^\\+/, "");
    if (TASK_NAME_PATTERN.test(name)) names.add(name);
  }

  const found: LegacyRegistration[] = [];
  for (const name of names) {
    const detail = await run(ctx, "schtasks.exe", ["/Query", "/TN", name, "/V", "/FO", "LIST"], {
      stream: false,
    });
    // An unreadable task is UNRESOLVED, never "probably ours": deleting a
    // scheduled task on a guess can break another product's install.
    found.push({
      kind: "scheduled-task",
      id: name,
      ownership: detail.status === 0 ? classify(ctx, detail.stdout, deps) : "unresolved",
    });
  }
  return { registrations: found, ok: true };
}

async function detectStartupEntries(
  ctx: WizardContext,
  deps: LegacyDeps,
): Promise<{ registrations: LegacyRegistration[]; ok: boolean }> {
  const dir = startupDirectory(deps);
  // No APPDATA at all means we could not even build the path to look in.
  if (dir === undefined) return { registrations: [], ok: false };
  const listing = await listDirectory(deps, dir);
  const found: LegacyRegistration[] = [];
  for (const entry of listing.entries) {
    if (!TASK_NAME_PATTERN.test(entry) || !/\.vbs$/i.test(entry)) continue;
    const filePath = path.win32.join(dir, entry);
    const contents = await readFileText(deps, filePath);
    found.push({
      kind: "startup-vbs",
      id: filePath,
      ownership: contents === undefined ? "unresolved" : classify(ctx, contents, deps),
    });
  }
  return { registrations: found, ok: listing.ok };
}

async function detectLaunchAgents(
  ctx: WizardContext,
  deps: LegacyDeps,
): Promise<{ registrations: LegacyRegistration[]; ok: boolean }> {
  const home = deps.homeDir ?? ctx.homeDir;
  const dir = path.posix.join(home, "Library", "LaunchAgents");
  const listing = await listDirectory(deps, dir);
  const found: LegacyRegistration[] = [];
  for (const entry of listing.entries) {
    if (!/hermes/i.test(entry) || !/\.plist$/i.test(entry)) continue;
    const filePath = path.posix.join(dir, entry);
    const contents = await readFileText(deps, filePath);
    if (contents !== undefined && !/gateway/i.test(`${entry} ${contents}`)) continue;
    found.push({
      kind: "launch-agent",
      id: filePath,
      ownership: contents === undefined ? "unresolved" : classify(ctx, contents, deps),
    });
  }
  return { registrations: found, ok: listing.ok };
}

/**
 * Ownership from canonical paths only. The text must contain this install's
 * profile directory or hermes root; everything else is someone else's.
 */
function classify(ctx: WizardContext, text: string, deps: LegacyDeps): "owned" | "foreign" {
  return referencesThisInstall(ctx, text, deps) ? "owned" : "foreign";
}

/**
 * Ownership by PATH BOUNDARY, never by substring (review r2, finding 8).
 *
 * `includes()` classified `C:\Users\Jo\.hermes-backup` as this install because
 * it contains `C:\Users\Jo\.hermes` — and this code deletes scheduled tasks and
 * kills processes on that answer. So the text is PARSED into candidate paths,
 * each is normalized with the target platform's rules, and each is compared as
 * "equal to, or a descendant of" our root/profile via path.relative.
 */
export function referencesThisInstall(ctx: WizardContext, text: string, deps: LegacyDeps = {}): boolean {
  const platform = deps.platform ?? process.platform;
  const paths = extractPaths(text, platform);
  const profile = profileName(ctx);

  // (a) Anything naming OUR PROFILE DIRECTORY is ours, full stop: that path
  // exists for exactly one profile.
  if (paths.some((candidate) => isSameOrInside(ctx.profileDir, candidate, platform))) return true;

  // (b) Otherwise the engine root alone is NOT enough. Several profiles share
  // one Hermes root, and the reviewer reproduced JARVIS claiming
  // `...\.hermes\hermes-agent\...\hermes.exe -p workbot gateway run` purely
  // because the executable lived under our root (review r3, finding 4). The
  // profile identity must agree too.
  const underOurRoot = paths.some((candidate) => isSameOrInside(ctx.hermesRoot, candidate, platform));
  return underOurRoot && profileTokenMatches(text, profile);
}

/** This install's profile name, from its profile directory. */
function profileName(ctx: WizardContext): string {
  const posix = ctx.profileDir.replace(/\\/g, "/").replace(/\/+$/, "");
  return posix.slice(posix.lastIndexOf("/") + 1);
}

/**
 * An explicit `-p <name>` / `--profile <name>` (or HERMES_HOME pointing at that
 * profile) naming OUR profile. A command with no profile at all does not match:
 * the engine would use its own sticky active-profile, which we cannot read from
 * here, and guessing would put us back in the business of killing strangers.
 */
export function profileTokenMatches(text: string, profile: string): boolean {
  if (profile === "") return false;
  const escaped = profile.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const flag = new RegExp(`(?:^|\\s)(?:-p|--profile)[\\s=]+"?${escaped}"?(?![\\w-])`, "i");
  const home = new RegExp(`HERMES_HOME\\s*=\\s*"?[^"\\r\\n]*[\\\\/]${escaped}(?![\\w-])`, "i");
  return flag.test(text) || home.test(text);
}

/** Path-shaped tokens in a command line, task description or launcher script. */
export function extractPaths(text: string, _platform: NodeJS.Platform): string[] {
  const found = new Set<string>();
  // Both shapes are always extracted: a Windows .vbs can carry forward-slash
  // paths, and the comparison below is strict, so extra candidates cost
  // nothing while a missed one would silently mean "not ours".
  const patterns = [
    /[A-Za-z]:[\\/][^"'\r\n]*/g, // C:\... or C:/...
    /\\\\[^"'\r\n]+/g, // \\server\share
    /\/[^\s"':]+/g, // /Users/...
  ];
  for (const pattern of patterns) {
    for (const match of text.matchAll(pattern)) {
      const value = match[0].trim().replace(/["']+$/, "");
      if (value !== "") for (const candidate of spaceVariants(value)) found.add(candidate);
    }
  }
  return [...found];
}

/**
 * A Windows path may contain spaces (`C:\Program Files\...`), so the pattern
 * above cannot stop at whitespace — it runs to the end of the line. That makes
 * the FIRST candidate for
 *
 *   Task To Run: cmd /c set HERMES_HOME=C:\Users\Jo\...\jarvis && hermes gateway run
 *
 * the whole tail, junk included, which is equal to no profile directory and
 * inside no root: a 1.3.x scheduled task went unrecognized, and its cleanup
 * silently did nothing (CI run 31685775063, first Windows execution of these
 * paths — the field shape behind bug #28).
 *
 * So every whitespace-delimited PREFIX is a candidate too. `C:\Program
 * Files\JARVIS\x` still matches as itself, and the junk-suffixed variant no
 * longer hides the real path. The comparison downstream stays strict, so extra
 * candidates cannot create a false positive: each is still either equal to, or
 * a descendant of, our own directory.
 */
function spaceVariants(value: string): string[] {
  const parts = value.split(/\s+/);
  if (parts.length === 1) return [value];
  const variants: string[] = [];
  for (let count = 1; count <= parts.length; count += 1) {
    const prefix = parts.slice(0, count).join(" ").replace(/["']+$/, "");
    if (prefix !== "") variants.push(prefix);
  }
  return variants;
}

export function isSameOrInside(root: string, candidate: string, platform: NodeJS.Platform): boolean {
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  const normalize = (value: string): string => {
    const unified = platform === "win32" ? value.replace(/\//g, "\\") : value;
    const resolved = pathApi.normalize(unified).replace(/[\\/]+$/, "");
    // Windows paths are case-insensitive; POSIX paths are not, and folding
    // them would make /home/jo/.hermes match /home/JO/.hermes — a DIFFERENT
    // directory that we must never touch.
    return platform === "win32" ? resolved.toLowerCase() : resolved;
  };
  const normalizedRoot = normalize(root);
  const normalizedCandidate = normalize(candidate);
  if (normalizedRoot === "" || normalizedCandidate === "") return false;
  if (normalizedRoot === normalizedCandidate) return true;
  const relative = pathApi.relative(normalizedRoot, normalizedCandidate);
  return relative !== "" && !relative.startsWith("..") && !pathApi.isAbsolute(relative);
}

interface ProcessScan {
  owned: number[];
  unresolved: number[];
  /** False when the enumeration itself could not run. */
  ok: boolean;
}

async function detectGatewayProcesses(
  ctx: WizardContext,
  platform: NodeJS.Platform,
  run: NonNullable<LegacyDeps["run"]>,
  deps: LegacyDeps,
): Promise<ProcessScan> {
  const owned: number[] = [];
  const unresolved: number[] = [];
  const enumeration = await enumerateGatewayPids(ctx, platform, run, deps);
  for (const pid of enumeration.pids) {
    const detail = await describeProcess(ctx, pid, platform, run, deps);
    if (detail === undefined) unresolved.push(pid);
    else if (referencesThisInstall(ctx, detail, deps)) owned.push(pid);
    // A gateway from another root is not ours to kill, and cannot hurt us:
    // different root, different profile, different lock, and our port is free.
  }
  return { owned, unresolved, ok: enumeration.ok };
}

/**
 * Exported for the Hermes step's installer-gateway attribution (F1 review
 * round 3): a gateway process that APPEARS during the official installer's
 * run was started by it — temporal attribution, needed because the
 * installer's default-profile gateway names no profile dir and so can never
 * pass referencesThisInstall.
 */
export async function enumerateGatewayPids(
  ctx: WizardContext,
  platform: NodeJS.Platform,
  run: NonNullable<LegacyDeps["run"]>,
  deps: LegacyDeps,
): Promise<{ pids: number[]; ok: boolean }> {
  if (platform === "win32") {
    const powershell = deps.powershell ?? "powershell.exe";
    const script = [
      "$found = Get-CimInstance Win32_Process |",
      "Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match '\\bgateway\\b' -and $_.CommandLine -match 'hermes' };",
      "foreach ($p in $found) { Write-Output $p.ProcessId }",
    ].join(" ");
    const result = await run(ctx, powershell, ["-NoProfile", "-Command", script], { stream: false });
    // PowerShell that cannot run is a failed scan; an empty result is not.
    return { pids: parsePids(result.stdout), ok: result.status === 0 };
  }
  // pgrep exits 1 for "no matches" — that IS an authoritative empty answer;
  // anything else means the scan did not happen.
  const result = await run(ctx, "pgrep", ["-f", "hermes.*gateway"], { stream: false });
  return { pids: parsePids(result.stdout), ok: result.status === 0 || result.status === 1 };
}

/**
 * The identity of a single process, re-read on demand. Used both when
 * detecting and — critically — immediately before killing, because a PID can
 * be recycled between consent and action (review r1, finding 8).
 */
async function describeProcess(
  ctx: WizardContext,
  pid: number,
  platform: NodeJS.Platform,
  run: NonNullable<LegacyDeps["run"]>,
  deps: LegacyDeps,
): Promise<string | undefined> {
  if (platform === "win32") {
    const powershell = deps.powershell ?? "powershell.exe";
    // ExecutablePath is the load-bearing field: a 1.3.x gateway runs from
    // <hermesRoot>\hermes-agent\venv\Scripts\hermes.exe, which names the root.
    const script = `$p = Get-CimInstance Win32_Process -Filter "ProcessId = ${pid}"; `
      + "if ($null -ne $p) { Write-Output ($p.ExecutablePath + ' ' + $p.CommandLine); exit 0 } else { exit 1 }";
    const result = await run(ctx, powershell, ["-NoProfile", "-Command", script], { stream: false });
    if (result.status !== 0 || result.stdout.trim() === "") return undefined;
    return result.stdout;
  }
  const result = await run(ctx, "ps", ["-o", "command=", "-p", String(pid)], { stream: false });
  if (result.status !== 0 || result.stdout.trim() === "") return undefined;
  return result.stdout;
}

function parsePids(text: string): number[] {
  return text
    .split(/\r?\n/)
    .map((line) => Number(line.trim()))
    .filter((pid) => Number.isInteger(pid) && pid > 0);
}

export function ownedRegistrations(artifacts: LegacyArtifacts): LegacyRegistration[] {
  return artifacts.registrations.filter((entry) => entry.ownership === "owned");
}

/**
 * The bar for "this machine is provably clean": an AUTHORITATIVE scan that
 * found nothing owned, nothing running, and nothing it could not attribute.
 * Only this may write a migration marker.
 */
export function machineIsProvablyClean(artifacts: LegacyArtifacts): boolean {
  return artifacts.scanOk
    && ownedRegistrations(artifacts).length === 0
    && artifacts.gatewayPids.length === 0
    && artifacts.unresolvedPids.length === 0
    && artifacts.registrations.every((entry) => entry.ownership !== "unresolved")
    && artifacts.staleEnvKeys.length === 0;
}

export function hasLegacyArtifacts(artifacts: LegacyArtifacts): boolean {
  return ownedRegistrations(artifacts).length > 0
    || artifacts.gatewayPids.length > 0
    || artifacts.gitCheckout
    || artifacts.staleEnvKeys.length > 0;
}

export async function cleanupLegacyInstall(
  ctx: WizardContext,
  artifacts: LegacyArtifacts,
  deps: LegacyDeps = {},
): Promise<LegacyCleanupResult> {
  const platform = deps.platform ?? process.platform;
  const run = deps.run ?? runCommand;
  const removed: LegacyRegistration[] = [];
  const failed: LegacyRegistration[] = [];
  let elevationLikely = false;

  for (const entry of artifacts.registrations) {
    if (entry.ownership !== "owned") continue;
    if (entry.kind === "scheduled-task") {
      const deleted = await run(ctx, "schtasks.exe", ["/Delete", "/F", "/TN", entry.id], { stream: false });
      if (deleted.status === 0) {
        removed.push(entry);
      } else {
        failed.push(entry);
        if (/access is denied|elevat/i.test(deleted.stderr)) elevationLikely = true;
      }
      continue;
    }
    if (entry.kind === "launch-agent") {
      await run(ctx, "launchctl", ["unload", "-w", entry.id], { stream: false });
    }
    try {
      await removeFilePath(deps, entry.id);
      removed.push(entry);
    } catch {
      failed.push(entry);
    }
  }

  const killedPids: number[] = [];
  const survivingPids: number[] = [];
  const skippedPids: number[] = [];
  for (const pid of artifacts.gatewayPids) {
    // Re-validate at the moment of the kill. Between detection, the consent
    // prompt and now, that PID may have exited and been recycled by an
    // unrelated program — killing it would be indefensible.
    const detail = await describeProcess(ctx, pid, platform, run, deps);
    if (detail === undefined || !referencesThisInstall(ctx, detail, deps)) {
      skippedPids.push(pid);
      continue;
    }
    const killed = platform === "win32"
      ? await run(ctx, "taskkill.exe", ["/PID", String(pid), "/T", "/F"], { stream: false })
      : await run(ctx, "kill", ["-TERM", String(pid)], { stream: false });
    if (killed.status === 0) killedPids.push(pid);
    else {
      survivingPids.push(pid);
      if (/access is denied|not permitted/i.test(killed.stderr)) elevationLikely = true;
    }
  }

  return { removed, failed, killedPids, survivingPids, skippedPids, elevationLikely };
}

function startupDirectory(deps: LegacyDeps): string | undefined {
  const env = deps.env ?? process.env;
  const appData = env.APPDATA ?? env.appdata;
  if (appData === undefined || appData === "") return undefined;
  return path.win32.join(appData, "Microsoft", "Windows", "Start Menu", "Programs", "Startup");
}

/**
 * A directory listing WITH completeness. Only ENOENT is authoritative absence:
 * a permission or I/O error means we could not look, and an empty array there
 * used to certify a machine that still had an old .vbs or plist on it (review
 * r3, finding 5). Fail closed.
 */
async function listDirectory(
  deps: LegacyDeps,
  dirPath: string,
): Promise<{ entries: string[]; ok: boolean }> {
  const list = deps.listDir
    ?? (async (target: string) => {
      const fs = await import("node:fs/promises");
      return fs.readdir(target);
    });
  try {
    return { entries: await list(dirPath), ok: true };
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    return { entries: [], ok: code === "ENOENT" || code === "ENOTDIR" };
  }
}

async function readFileText(deps: LegacyDeps, filePath: string): Promise<string | undefined> {
  const read = deps.readFile
    ?? (async (target: string) => {
      const fs = await import("node:fs/promises");
      try {
        return await fs.readFile(target, "utf8");
      } catch {
        return undefined;
      }
    });
  return read(filePath);
}

async function removeFilePath(deps: LegacyDeps, filePath: string): Promise<void> {
  const remove = deps.removeFile
    ?? (async (target: string) => {
      const fs = await import("node:fs/promises");
      await fs.rm(target, { force: true });
    });
  await remove(filePath);
}

async function fileExists(deps: LegacyDeps, filePath: string): Promise<boolean> {
  const contents = await readFileText(deps, filePath);
  if (contents !== undefined) return true;
  // A .git DIRECTORY is not readable as a file; fall back to a listing.
  const listing = await listDirectory(deps, filePath);
  return listing.entries.length > 0;
}
