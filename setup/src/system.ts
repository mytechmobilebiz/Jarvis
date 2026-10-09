import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { brainCliHomeDirs } from "./brain-cli-homes.js";
import { pathExists } from "./fs-utils.js";
import { redactSecrets } from "./redact.js";
import { confirm, note } from "./ui.js";
import type { WizardContext } from "./context.js";
import type { CommandStream } from "./io.js";

export interface CommandResult {
  status: number;
  stdout: string;
  stderr: string;
}

export interface WatchedCommandResult extends CommandResult {
  /** How the run ended — a cancelled login must not be reported as a failure. */
  end: WatchedRunEnd;
}

interface RunOptions {
  cwd?: string;
  env?: NodeJS.ProcessEnv;
  /**
   * Env keys REMOVED from the child's environment after merging. `env` can
   * only add or override; the brain-CLI verify turn needs the opposite — the
   * wizard's own ANTHROPIC_API_KEY / OPENAI_API_KEY (a dev shell, a carried
   * launch env) must not reach the engine, or the CLI under it would answer on
   * a paid key while the step reports the subscription as proven.
   */
  unsetEnv?: readonly string[];
  stream?: boolean;
  /** Defaults to ctx.signal; pass explicitly to scope one command's lifetime. */
  signal?: AbortSignal;
  /**
   * POSIX: run the child in its own session (setsid) even when stdio is
   * inherited, so it has NO CONTROLLING TERMINAL while its output still
   * reaches the customer's tty through the inherited fds. A child that
   * probes /dev/tty — upstream's installer gating its gateway stage — then
   * takes its own no-terminal path deterministically. Cancellation is
   * unaffected: terminateProcessTree already signals the process group.
   */
  detachTty?: boolean;
}

export interface SystemOverrides {
  platform?: NodeJS.Platform;
  baseEnv?: NodeJS.ProcessEnv;
  homeDir?: string;
  execPath?: string;
}

interface CommandExistsOverrides extends SystemOverrides {
  probe?: (command: string, args: string[], env: NodeJS.ProcessEnv) => number | null;
}

export interface PreparedCommand {
  command: string;
  args: string[];
  windowsVerbatimArguments?: boolean;
}

interface PrepareCommandOverrides {
  platform?: NodeJS.Platform;
  env?: NodeJS.ProcessEnv;
  cwd?: string;
  resolveCommand?: (command: string) => string | undefined;
}

export async function runCommand(
  ctx: WizardContext,
  command: string,
  args: string[],
  options: RunOptions = {},
): Promise<CommandResult> {
  if (ctx.dryRun) {
    note(ctx, `[dry-run] would run ${formatCommand(command, args)}${options.cwd ? ` in ${options.cwd}` : ""}.`);
    return { status: 0, stdout: "", stderr: "" };
  }

  const streaming = options.stream !== false;
  const inherit = streaming && ctx.io.inheritsStdio;
  if (streaming && !inherit) {
    ctx.io.command(formatCommand(command, args));
  }

  // Cancellation reaches ORDINARY commands too, not only the watched ones.
  // The provider-safety transaction runs through here: closing the wizard
  // while it is mid-flight must take its python with it, or the rewrite (and
  // the gateway work that follows) happens after the customer walked away
  // (integration review r3, P2).
  const signal = options.signal ?? ctx.signal;
  if (signal?.aborted === true) {
    return { status: 130, stdout: "", stderr: "Cancelled before the command started." };
  }

  // The honest busy signal: ON at spawn, OFF at settle, on every exit path —
  // including a SYNCHRONOUS throw from environment preparation or spawn()
  // itself (an invalid cwd throws before any 'error' event exists, and the
  // unbalanced ON left the working dots up forever — Codex re-review
  // 2026-08-14, F2).
  ctx.io.working?.(true);
  return new Promise<CommandResult>((resolve) => {
    let child: ChildProcess;
    try {
      const env = spawnEnvironment(ctx, options.env);
      if (options.unsetEnv !== undefined && options.unsetEnv.length > 0) {
        // Case-insensitive: Windows env names are; an inherited `OpenAI_Api_Key`
        // must not survive a scrub of OPENAI_API_KEY (Codex review 2026-08-15).
        const banned = new Set(options.unsetEnv.map((key) => key.toUpperCase()));
        for (const key of Object.keys(env)) if (banned.has(key.toUpperCase())) delete env[key];
      }
      const prepared = prepareCommand(command, args, { env, cwd: options.cwd });
      child = spawn(prepared.command, prepared.args, {
        cwd: options.cwd,
        env,
        stdio: inherit ? "inherit" : ["ignore", "pipe", "pipe"],
        windowsVerbatimArguments: prepared.windowsVerbatimArguments,
        // A POSIX process GROUP, matching runWatchedCommand. terminateProcessTree
        // signals `-pid`, which needs a group to exist — without this the signal
        // reached only the direct child, and its own children (the engine CLI's
        // python, a shell's helpers) survived the cancellation that killed their
        // parent (integration review r4, finding 2). Never on Windows, where
        // `detached` means a new console rather than a process group.
        detached: process.platform !== "win32" && (!inherit || options.detachTty === true),
      });
    } catch (error) {
      ctx.io.working?.(false);
      resolve({ status: 127, stdout: "", stderr: error instanceof Error ? error.message : String(error) });
      return;
    }

    let stdout = "";
    let stderr = "";
    let aborted = false;
    // The whole tree: the engine's CLI spawns python, which spawns its own
    // children, and killing only the direct child leaves those running.
    const onAbort = (): void => {
      aborted = true;
      terminateProcessTree(child);
    };
    signal?.addEventListener("abort", onAbort, { once: true });

    if (!inherit) {
      child.stdout?.on("data", (chunk: Buffer) => {
        const text = chunk.toString("utf8");
        if (streaming) ctx.io.commandOutput(redactSecrets(text), "stdout");
        else stdout += text;
      });
      child.stderr?.on("data", (chunk: Buffer) => {
        const text = chunk.toString("utf8");
        if (streaming) ctx.io.commandOutput(redactSecrets(text), "stderr");
        else stderr += text;
      });
    }

    // Balanced with the working(true) above even if 'error' AND 'close' both
    // fire for the same child — a double OFF would steal a concurrent spawn's
    // indicator out from under it.
    let workSettled = false;
    const settleWork = (): void => {
      if (workSettled) return;
      workSettled = true;
      ctx.io.working?.(false);
    };
    child.on("error", (error) => {
      signal?.removeEventListener("abort", onAbort);
      settleWork();
      resolve({ status: 127, stdout, stderr: error.message });
    });
    child.on("close", (code) => {
      signal?.removeEventListener("abort", onAbort);
      settleWork();
      // A cancelled command never reports success, whatever exit code the dying
      // child happened to produce.
      if (aborted) resolve({ status: 130, stdout, stderr: `${stderr}\nCancelled.` });
      else resolve({ status: code ?? 1, stdout, stderr });
    });
  });
}

export interface WatchedRunOptions extends RunOptions {
  /**
   * Called once per complete output line (stdout and stderr interleaved, in
   * arrival order) while the child is still running. This is what makes a
   * blocking device-code login watchable: the engine prints the URL and code,
   * then blocks polling for minutes.
   */
  onLine?: (line: string) => void;
  /**
   * Keep the child's raw output OFF the frontend's command log.
   *
   * Auth output is not ordinary command output: the engine prints the device
   * code, the verification URL that embeds it, and — after saving — a
   * credential label it derives from the account's JWT email claim. The
   * redactor only knows a handful of API-token prefixes, so forwarding these
   * chunks would put a customer's account identifiers into the setup log and
   * from there into diagnostics they paste to support. Callers that pass this
   * get the lines in memory, and publish only parsed, closed-set events.
   */
  quiet?: boolean;
  /** Abort the child (process tree) when this fires. */
  signal?: AbortSignal;
  /** Kill the child after this long. Device flows must not poll forever. */
  timeoutMs?: number;
}

/** Why a watched command stopped, when it was not the child's own decision. */
export type WatchedRunEnd = "exited" | "aborted" | "timeout";

/**
 * Run a long-lived child, stream its output to the frontend AND hand every
 * completed line to a watcher, returning the full capture when it exits.
 *
 * `runCommand` cannot do this: with `stream: true` the output goes to the
 * frontend and is never returned, and with `stream: false` nothing is visible
 * until the process exits — either way a device-code prompt would stay hidden
 * for the whole login.
 *
 * stdio is always piped, never inherited, so terminal and GUI runs take the
 * exact same code path (and the same tests). The child therefore has no stdin:
 * the engine's optional "[Y/n]" prompts see EOF and take their documented
 * default (hermes_cli/auth_commands.py:270-273 imports shared credentials on
 * EOFError), which is the same answer a customer pressing Enter would give.
 */
export async function runWatchedCommand(
  ctx: WizardContext,
  command: string,
  args: string[],
  options: WatchedRunOptions = {},
): Promise<WatchedCommandResult> {
  if (ctx.dryRun) {
    note(ctx, `[dry-run] would run ${formatCommand(command, args)} and watch its output.`);
    return { status: 0, stdout: "", stderr: "", end: "exited" };
  }

  const signal = options.signal ?? ctx.signal;
  if (signal?.aborted === true) {
    return { status: 130, stdout: "", stderr: "", end: "aborted" };
  }

  ctx.io.command(formatCommand(command, args));

  // Same honest busy signal as runCommand; finish() below runs exactly once
  // on every exit path, and the synchronous-throw guard mirrors runCommand's
  // (Codex re-review 2026-08-14, F2).
  ctx.io.working?.(true);
  return new Promise<WatchedCommandResult>((resolve) => {
    let child: ChildProcess;
    try {
      const env = spawnEnvironment(ctx, {
        // CPython block-buffers stdout when it is a pipe, so an unflushed device
        // code would sit in the child's buffer until the login timed out.
        PYTHONUNBUFFERED: "1",
        ...options.env,
      });
      if (options.unsetEnv !== undefined && options.unsetEnv.length > 0) {
        const banned = new Set(options.unsetEnv.map((key) => key.toUpperCase()));
        for (const key of Object.keys(env)) if (banned.has(key.toUpperCase())) delete env[key];
      }
      const prepared = prepareCommand(command, args, { env, cwd: options.cwd });
      child = spawn(prepared.command, prepared.args, {
        cwd: options.cwd,
        env,
        stdio: ["ignore", "pipe", "pipe"],
        windowsVerbatimArguments: prepared.windowsVerbatimArguments,
        // Its own process group on POSIX, so an abort kills the engine AND the
        // python/browser helpers it spawned — killing the parent alone would
        // leave an OAuth poller running after the customer closed setup.
        detached: process.platform !== "win32",
      });
    } catch (error) {
      ctx.io.working?.(false);
      resolve({
        status: 127,
        stdout: "",
        stderr: error instanceof Error ? error.message : String(error),
        end: "exited",
      });
      return;
    }

    let stdout = "";
    let stderr = "";
    let pending = "";
    let end: WatchedRunEnd = "exited";
    let settled = false;

    const consume = (text: string): void => {
      pending += text;
      const lines = pending.split(/\r?\n/);
      pending = lines.pop() ?? "";
      for (const line of lines) options.onLine?.(line);
    };

    const attach = (stream: NodeJS.ReadableStream | null, sink: CommandStream): void => {
      stream?.on("data", (chunk: Buffer) => {
        const text = chunk.toString("utf8");
        if (sink === "stdout") stdout += text;
        else stderr += text;
        if (options.quiet !== true) ctx.io.commandOutput(redactSecrets(text), sink);
        consume(text);
      });
    };

    attach(child.stdout, "stdout");
    attach(child.stderr, "stderr");

    const stop = (reason: WatchedRunEnd): void => {
      if (settled) return;
      end = reason;
      terminateProcessTree(child);
    };

    const onAbort = (): void => stop("aborted");
    signal?.addEventListener("abort", onAbort, { once: true });
    const deadline = options.timeoutMs === undefined
      ? undefined
      : setTimeout(() => stop("timeout"), options.timeoutMs);

    const finish = (result: WatchedCommandResult): void => {
      if (settled) return;
      settled = true;
      if (deadline !== undefined) clearTimeout(deadline);
      signal?.removeEventListener("abort", onAbort);
      ctx.io.working?.(false);
      resolve(result);
    };

    child.on("error", (error) => {
      finish({ status: 127, stdout, stderr: `${stderr}${error.message}`, end });
    });
    child.on("close", (code) => {
      // A device code printed without a trailing newline still has to reach the
      // watcher before the result is handed back.
      if (pending !== "") options.onLine?.(pending);
      finish({ status: end === "exited" ? (code ?? 1) : 130, stdout, stderr, end });
    });
  });
}

/**
 * Kill a child AND everything it spawned.
 *
 * A bare `child.kill()` leaves grandchildren — on Windows the engine runs
 * behind a cmd.exe shim, and on POSIX the login spawns helpers — so an
 * abandoned OAuth poller could still complete a sign-in minutes after the
 * customer closed the window. SIGKILL follows if the tree ignores the polite
 * signal.
 */
export interface TerminateOverrides {
  platform?: NodeJS.Platform;
  /** The tree-killer binary; injectable so the Windows path is testable. */
  killer?: string;
}

export function terminateProcessTree(child: ChildProcess, overrides: TerminateOverrides = {}): void {
  const pid = child.pid;
  if (pid === undefined) return;

  let closed = false;
  child.once("close", () => { closed = true; });

  const posixSignal = (signal: NodeJS.Signals): void => {
    try {
      // Negative pid = the whole process group created by `detached: true`.
      process.kill(-pid, signal);
    } catch {
      try {
        child.kill(signal);
      } catch {
        // Already gone; nothing to clean up.
      }
    }
  };

  const fallback = (): void => {
    if (closed) return;
    try {
      child.kill("SIGTERM");
    } catch {
      // Nothing left to signal.
    }
    const hard = setTimeout(() => {
      if (closed) return;
      try {
        child.kill("SIGKILL");
      } catch {
        // Nothing left to signal.
      }
    }, 2000);
    hard.unref?.();
    child.once("close", () => clearTimeout(hard));
  };

  if ((overrides.platform ?? process.platform) === "win32") {
    // taskkill is the only way to reach grandchildren on Windows (the engine
    // runs behind a cmd.exe shim), but it can exit nonzero — unavailable on a
    // stripped PATH, or denied — and ignoring that left the OAuth poller alive
    // after the HUD's five-second shutdown cap. Its result is checked, and
    // Node's own kill is the fallback.
    const killer = spawn(overrides.killer ?? "taskkill", ["/pid", String(pid), "/T", "/F"], { stdio: "ignore" });
    killer.on("error", fallback);
    killer.on("close", (code) => {
      if (code !== 0) fallback();
      // Verify: taskkill can report success while the tree lingers a moment.
      const check = setTimeout(fallback, 1500);
      check.unref?.();
      child.once("close", () => clearTimeout(check));
    });
    return;
  }

  posixSignal("SIGTERM");
  const hard = setTimeout(() => { if (!closed) posixSignal("SIGKILL"); }, 2000);
  hard.unref?.();
  child.once("close", () => clearTimeout(hard));
}

export async function runShell(
  ctx: WizardContext,
  script: string,
  options: RunOptions = {},
): Promise<CommandResult> {
  const shell = process.platform === "win32" ? "powershell.exe" : "bash";
  const args = process.platform === "win32" ? ["-NoProfile", "-Command", script] : ["-lc", script];
  return runCommand(ctx, shell, args, options);
}

/**
 * Frontends without inherited stdio cannot answer a child process that wants a
 * terminal (sudo, prompts). Offer the exact command for the buyer to run instead.
 */
export async function confirmManualRun(ctx: WizardContext, display: string): Promise<boolean> {
  if (ctx.dryRun || ctx.io.inheritsStdio) return false;
  ctx.io.command(display);
  ctx.io.warn("That command may need a real terminal. Run it yourself in Terminal, then confirm below.");
  return confirm(ctx, "I ran it in a terminal and it succeeded. Continue?", false);
}

export function commandExists(
  ctx: WizardContext,
  command: string,
  overrides: CommandExistsOverrides = {},
): boolean {
  const platform = overrides.platform ?? process.platform;
  const env = spawnEnvironment(ctx, undefined, overrides);
  const probe = overrides.probe
    ?? ((probeCommand: string, args: string[], probeEnv: NodeJS.ProcessEnv) =>
      spawnSync(probeCommand, args, { env: probeEnv, stdio: "ignore" }).status);
  if (platform === "win32") {
    return probe("where.exe", [command], env) === 0;
  }
  return probe("sh", ["-c", `command -v ${shellQuote(command)} >/dev/null 2>&1`], env) === 0;
}

/**
 * A GUI-launched Electron app, and a shell opened right after the Hermes
 * installer runs, can both miss the directories hermes and brew live in.
 * Explicit option env entries still win.
 */
export function spawnEnvironment(
  ctx: PathRoots,
  env?: NodeJS.ProcessEnv,
  overrides: SystemOverrides = {},
): NodeJS.ProcessEnv {
  const platform = overrides.platform ?? process.platform;
  const merged = { ...(overrides.baseEnv ?? process.env), ...env };
  const result = {
    ...merged,
    HERMES_HOME: env?.HERMES_HOME ?? ctx.hermesRoot,
    // A PATH supplied for this command is intentional and wins over augmentation.
    PATH: env?.PATH ?? augmentedPath(ctx, merged, overrides),
  };
  if (platform === "win32") removeDuplicateWindowsEnvKeys(result, "PATH");
  return result;
}

/** The two roots PATH augmentation reads — a full WizardContext, or just those. */
export type PathRoots = Pick<WizardContext, "hermesRoot" | "hermesAgentDir">;

/**
 * The environment the HUD hands the engine on EVERY gateway spawn: the same
 * PATH augmentation the wizard's spawns and its post-install hand-off use,
 * rebuilt from the process environment at spawn time. A cold Finder/Dock
 * launch never runs the wizard's hand-off, so raw process.env still carries
 * launchd's bare PATH (/usr/bin:/bin:/usr/sbin:/sbin) — and the engine's
 * ChatGPT (codex) brain route spawns `codex` by bare name through that PATH
 * (codex_app_server.py), which would fail on every boot after the one that
 * ran setup. Rebuilt per spawn (not snapshotted) so a hand-off that already
 * happened in this process is folded in, not fought.
 */
export function engineHostEnvironment(hermesRoot: string, overrides: SystemOverrides = {}): NodeJS.ProcessEnv {
  const platform = overrides.platform ?? process.platform;
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  return spawnEnvironment({ hermesRoot, hermesAgentDir: pathApi.join(hermesRoot, "hermes-agent") }, undefined, overrides);
}

export function augmentedPath(
  ctx: PathRoots,
  env: NodeJS.ProcessEnv = process.env,
  overrides: SystemOverrides = {},
): string {
  const platform = overrides.platform ?? process.platform;
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  const delimiter = platform === "win32" ? path.win32.delimiter : path.posix.delimiter;
  const homeDir = overrides.homeDir ?? os.homedir();
  const execPath = overrides.execPath ?? process.execPath;
  const current = readEnvValue(env, "PATH", platform) ?? "";
  const entries = current.split(delimiter).filter((entry) => entry !== "");
  // The engine spawns codex by bare name; JARVIS installs it here
  // (brain-install.ts), so the managed and authenticated binary wins. Its head
  // position is deliberate: the wizard-installed and signed-in binary must beat
  // every customer-PATH codex—a different version that may not speak app-server.
  const candidates = platform === "win32"
    ? [
        pathApi.join(ctx.hermesRoot, "brain", "codex"),
        pathApi.join(ctx.hermesAgentDir, "venv", "Scripts"),
        pathApi.join(ctx.hermesRoot, "bin"),
        pathApi.join(ctx.hermesRoot, "git", "cmd"),
        pathApi.join(ctx.hermesRoot, "git", "bin"),
        pathApi.join(ctx.hermesRoot, "git", "usr", "bin"),
        pathApi.join(ctx.hermesRoot, "node"),
        pathApi.dirname(execPath),
        env.PNPM_HOME,
      ]
    : [
        pathApi.join(ctx.hermesRoot, "brain", "codex"),
        pathApi.join(ctx.hermesAgentDir, "venv", "bin"),
        pathApi.join(ctx.hermesRoot, "bin"),
        // Where a version manager or corepack keeps pnpm next to this runtime.
        pathApi.dirname(execPath),
        env.PNPM_HOME,
        pathApi.join(homeDir, ".local", "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
      ];
  // Windows machines with a stripped or corrupted PATH can't find the OS
  // baseline tools JARVIS shells out to — netstat.exe, where.exe, and bare
  // powershell.exe all live under System32. A blank netstat/where read looks
  // exactly like "port is free" / "command missing" and mis-drives setup
  // (Kevin Dunn, Caleb 2026-08-11). These go at the TAIL, after the buyer's own
  // entries, so a healthy PATH keeps its ordering and this only heals a broken
  // one — dedup drops the tail copy when System32 is already present.
  // The vendor install homes for the ChatGPT/Claude tools ride the SAME tail,
  // on both platforms, from the one list the engine's own fallback is pinned
  // against (brain-cli-homes.ts). Before v1.5.2 the Windows branch had none of
  // them: a customer who ran OpenAI's official `install.ps1` had codex.exe in
  // %USERPROFILE%\.codex\bin and JARVIS reported nothing installed, because the
  // user PATH update does not reach an already-running Explorer session.
  const brainHomes = brainCliHomeDirs(platform, homeDir, env);
  const systemTail = platform === "win32"
    ? [...brainHomes, ...windowsSystemDirs(env)]
    : [...versionManagerDirs(homeDir, pathApi), ...brainHomes];
  const seen = new Set<string>();
  return [...candidates, ...entries, ...systemTail]
    .filter((entry): entry is string => entry !== undefined && entry !== "")
    .filter((entry) => {
      // On Windows, treat quoted and trailing-separator variants of a directory
      // as the same entry, so the System32 tail can't double an existing
      // "C:\Windows\System32" or C:\Windows\System32\ already on PATH. A bare
      // drive root ("C:\") keeps its separator — "C:\" and "C:" are different
      // paths (root vs. drive-relative) and must not be conflated.
      const key = platform === "win32" ? normalizeWindowsPathKey(entry) : entry;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .join(delimiter);
}

/**
 * Where version managers keep the programs they manage, on macOS.
 *
 * The darwin candidates above cover a plain install (~/.local/bin, Homebrew,
 * /usr/local/bin) but miss every node managed by volta, bun, asdf or mise —
 * so a customer whose ChatGPT/Claude companion tool came in that way had it
 * read as "not installed" (Justin Lipes D6A74078, 2026-08-17). These also
 * cover the two vendor directories the engine's own `find_brain_cli` checks.
 *
 * TAIL, not head — deliberately, like the Windows System32 tail. A working
 * PATH keeps its own ordering, so a customer who has chosen which `node` (or
 * which anything) comes first keeps that choice; this only heals a PATH that
 * never had these at all, which is exactly the launchd/Finder case. Dedup
 * drops a tail copy of a directory already present.
 *
 * nvm is deliberately NOT here: its binaries live under a version-specific
 * directory (~/.nvm/versions/node/<version>/bin) with no stable path to
 * guess, and guessing wrong would pin a customer to an arbitrary old node.
 * HERMES_CODEX_CLI_PATH / HERMES_CLAUDE_CLI_PATH are the answer there, and
 * the probes now honour them (brain-probe.ts).
 */
function versionManagerDirs(homeDir: string, pathApi: path.PlatformPath): string[] {
  return [
    pathApi.join(homeDir, ".volta", "bin"),
    pathApi.join(homeDir, ".bun", "bin"),
    pathApi.join(homeDir, ".asdf", "shims"),
    pathApi.join(homeDir, ".local", "share", "mise", "shims"),
    // The vendor install locations agent/brain/providers.py looks in itself
    // come from brain-cli-homes.ts, appended to this tail by the caller — one
    // list, pinned against the engine's own (brain-cli-homes.test.ts).
  ];
}

/**
 * The Windows OS baseline directories. netstat.exe / where.exe live in
 * System32; the stock powershell.exe lives one level deeper. Wbem carries
 * wmic and is part of the canonical machine PATH. %SystemRoot% (falling back
 * to %windir%, then the near-universal C:\Windows) locates them without
 * assuming a drive letter.
 */
function windowsSystemDirs(env: NodeJS.ProcessEnv): string[] {
  // `??` alone would accept a blank SystemRoot="" and build relative "System32"
  // entries; on a broken environment we want the next real value, or C:\Windows.
  const systemRoot =
    [readEnvValue(env, "SystemRoot", "win32"), readEnvValue(env, "windir", "win32")]
      .find((value): value is string => value !== undefined && value !== "")
    ?? "C:\\Windows";
  const system32 = path.win32.join(systemRoot, "System32");
  return [
    system32,
    path.win32.join(system32, "WindowsPowerShell", "v1.0"),
    path.win32.join(system32, "Wbem"),
  ];
}

/**
 * A case-insensitive dedup key for a Windows PATH entry: drop surrounding quotes
 * and a trailing separator, but keep the separator on a bare drive root so "C:\"
 * (the root) and "C:" (drive-relative current dir) stay distinct.
 */
function normalizeWindowsPathKey(entry: string): string {
  const unquoted = entry.replace(/^"|"$/g, "");
  // A bare drive root ("C:\" / "C:/") is canonicalized to "C:\" but stays
  // distinct from the drive-relative "C:"; every other entry just loses any
  // trailing separator so quoted/slash variants of a real dir collapse.
  const driveRoot = /^([A-Za-z]:)[\\/]+$/.exec(unquoted);
  const normalized = driveRoot === null
    ? unquoted.replace(/[\\/]+$/, "")
    : `${driveRoot[1]}\\`;
  return normalized.toLowerCase();
}

export function handoffHermesEnvironment(
  ctx: WizardContext,
  overrides: SystemOverrides & { targetEnv?: NodeJS.ProcessEnv } = {},
): void {
  const targetEnv = overrides.targetEnv ?? process.env;
  targetEnv.HERMES_HOME = ctx.hermesRoot;
  targetEnv.PATH = augmentedPath(ctx, targetEnv, { ...overrides, baseEnv: targetEnv });
  if ((overrides.platform ?? process.platform) === "win32") {
    removeDuplicateWindowsEnvKeys(targetEnv, "PATH");
  }
}

/**
 * Open a URL in the customer's browser.
 *
 * THROWS when the opener is missing or fails. It used to discard
 * `runCommand`'s status, which resolves (never rejects) on a missing
 * `xdg-open`/`open`/`cmd` — so every `openUrl(...).catch(...)` fallback in the
 * codebase was dead code, and a customer whose browser never opened was told
 * nothing at all. On the device-code screens that is the difference between a
 * sign-in and a dead end.
 */
export async function openUrl(
  ctx: WizardContext,
  url: string,
  overrides: { platform?: NodeJS.Platform; run?: typeof runCommand } = {},
): Promise<void> {
  if (ctx.dryRun) {
    note(ctx, `[dry-run] would open ${url}.`);
    return;
  }

  const platform = overrides.platform ?? process.platform;
  const run = overrides.run ?? runCommand;
  const opener =
    platform === "darwin"
      ? { command: "open", args: [url] }
      : platform === "win32"
        ? { command: "cmd", args: ["/c", "start", "", url] }
        : { command: "xdg-open", args: [url] };
  const result = await run(ctx, opener.command, opener.args, { stream: false });
  if (result.status !== 0) {
    throw new Error(`${opener.command} could not open the address (exit ${result.status}).`);
  }
}

export function formatCommand(command: string, args: string[]): string {
  return [command, ...args].map(shellQuote).join(" ");
}

export function shellQuote(value: string): string {
  if (/^[A-Za-z0-9_./:@%+=,-]+$/.test(value)) return value;
  return `'${value.replaceAll("'", "'\\''")}'`;
}

export function platformLabel(): string {
  return `${os.type()} ${os.release()} ${os.arch()}`;
}

export function bundledUvPath(
  ctx: WizardContext,
  platform: NodeJS.Platform = process.platform,
): string {
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  return pathApi.join(ctx.hermesRoot, "bin", platform === "win32" ? "uv.exe" : "uv");
}

export function venvPythonPath(
  ctx: WizardContext,
  platform: NodeJS.Platform = process.platform,
): string {
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  return platform === "win32"
    ? pathApi.join(ctx.hermesAgentDir, "venv", "Scripts", "python.exe")
    : pathApi.join(ctx.hermesAgentDir, "venv", "bin", "python");
}

/**
 * HOW to start the engine on this machine: an executable plus the arguments
 * that must come before the caller's own.
 *
 * Two shapes, one seam. A wheelhouse install (Windows) has a real console
 * script, and the command is just that path — byte for byte what shipped in
 * v1.3 and run 7. A site install (macOS, v1.4.0) has no console script:
 * `pip install --target` generates none, and pip's own would carry the BUILD
 * machine's interpreter in its shebang. So the command is the vendored
 * interpreter running the same entry point the console script would have
 * called. Callers append their own arguments to either and never need to know
 * which one they got.
 */
export interface HermesCommand {
  executable: string;
  args: string[];
}

/** The engine's entry point, exactly as its console script invokes it. */
const HERMES_ENTRY = "from hermes_cli.main import main; main()";

export function hermesCommand(
  ctx: WizardContext,
  platform: NodeJS.Platform = process.platform,
  exists: (target: string) => boolean = fs.existsSync,
): HermesCommand {
  const venv = hermesVenvExecutablePath(ctx, platform);
  if (exists(venv)) return { executable: venv, args: [] };
  const python = sitePythonPath(ctx, platform);
  if (exists(python)) return { executable: python, args: ["-c", HERMES_ENTRY] };
  // A 1.3.x machine whose engine came from the network still has `hermes` on
  // PATH; nothing here may make that machine worse.
  return { executable: "hermes", args: [] };
}

/**
 * The interpreter a SITE install runs the engine with. Its libraries live in
 * that interpreter's OWN site-packages, so no PYTHONPATH, no wrapper script and
 * no environment variable stands between the supervisor and the engine — the
 * command behaves identically from a shell, from a test, and from a spawned
 * child, which is exactly what the gateway seam needs.
 */
export function sitePythonPath(
  ctx: WizardContext,
  platform: NodeJS.Platform = process.platform,
): string {
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  // <hermesRoot>/python — the same place ensureLocalPython() puts it, keyed to
  // the engine ROOT rather than one engine folder, so replacing the engine does
  // not mean recopying an 80 MB interpreter that has not changed.
  const root = pathApi.join(ctx.hermesRoot, "python");
  return platform === "win32"
    ? pathApi.join(root, "python.exe")
    : pathApi.join(root, "bin", "python3");
}

/**
 * The ONE way to compose an engine-CLI invocation. Every spawn of the engine
 * CLI must go through this (hermes-spawn-guard.test.ts enforces it).
 *
 * It exists because the alternative shipped a P1: hermesExecutablePath() alone
 * drops the site-install's `-c "<entry point>"` arguments, turning
 * `hermes -p jarvis cron create …` into `python3 -p jarvis cron create …` —
 * Python rejects `-p`, the schedule silently never exists, and the customer is
 * shown a Python usage error (Peter's walkthrough, 2026-08-14). The venv shape
 * (Windows) and the PATH fallback (1.3.x machines) have empty prefix args,
 * which is exactly why the bug was invisible everywhere but a fresh mac
 * install.
 */
export function hermesInvocation(
  ctx: WizardContext,
  args: readonly string[],
  platform: NodeJS.Platform = process.platform,
): { command: string; args: string[] } {
  const shape = hermesCommand(ctx, platform);
  return { command: shape.executable, args: [...shape.args, ...args] };
}

/**
 * The engine's OWN Python interpreter, whichever payload shape this machine
 * has: the wheelhouse venv (Windows, 1.3.x) or the vendored site interpreter
 * (macOS, v1.4.0). Both carry the engine's libraries (PyYAML included), so
 * either can run the atomic config transaction. Undefined means no engine
 * python is installed yet — callers fall back to the CLI path.
 */
export function enginePythonPath(
  ctx: WizardContext,
  platform: NodeJS.Platform = process.platform,
  exists: (target: string) => boolean = fs.existsSync,
): string | undefined {
  const venv = venvPythonPath(ctx, platform);
  if (exists(venv)) return venv;
  const site = sitePythonPath(ctx, platform);
  if (exists(site)) return site;
  return undefined;
}

/**
 * For callers that only need something to SHOW, or to test for existence.
 * NEVER a spawn target: on a site install this is a bare Python interpreter
 * that does not understand hermes arguments. Anything that SPAWNS must use
 * hermesInvocation() — hermes-spawn-guard.test.ts fails the build on any
 * spawn-shaped use of this function outside system.ts.
 */
export function hermesExecutablePath(
  ctx: WizardContext,
  platform: NodeJS.Platform = process.platform,
): string {
  return hermesCommand(ctx, platform).executable;
}

export function hermesVenvExecutablePath(
  ctx: WizardContext,
  platform: NodeJS.Platform = process.platform,
): string {
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  return platform === "win32"
    ? pathApi.join(ctx.hermesAgentDir, "venv", "Scripts", "hermes.exe")
    : pathApi.join(ctx.hermesAgentDir, "venv", "bin", "hermes");
}

export function managedGitPath(
  ctx: WizardContext,
  platform: NodeJS.Platform = process.platform,
): string {
  return platform === "win32"
    ? path.win32.join(ctx.hermesRoot, "git", "cmd", "git.exe")
    : "git";
}

export async function gitBashPresent(
  ctx: PathRoots,
  env: NodeJS.ProcessEnv = process.env,
  overrides: SystemOverrides = {},
  exists: (filePath: string) => Promise<boolean> = pathExists,
): Promise<boolean> {
  const platform = overrides.platform ?? process.platform;
  const localAppData = readEnvValue(env, "LOCALAPPDATA", platform);
  const candidates = [
    readEnvValue(env, "HERMES_GIT_BASH_PATH", platform),
    ...(localAppData ? [
      path.win32.join(localAppData, "hermes", "git", "bin", "bash.exe"),
      path.win32.join(localAppData, "hermes", "git", "usr", "bin", "bash.exe"),
    ] : []),
    path.win32.join(readEnvValue(env, "ProgramFiles", platform) ?? "C:\\Program Files", "Git", "bin", "bash.exe"),
    path.win32.join(readEnvValue(env, "ProgramFiles(x86)", platform) ?? "C:\\Program Files (x86)", "Git", "bin", "bash.exe"),
    ...(localAppData ? [path.win32.join(localAppData, "Programs", "Git", "bin", "bash.exe")] : []),
    ...augmentedPath(ctx, env, overrides).split(path.win32.delimiter)
      .map((entry) => path.win32.join(entry.replace(/^"|"$/g, ""), "bash.exe")),
  ];
  for (const candidate of candidates) {
    if (candidate && await exists(candidate)) return true;
  }
  return false;
}

/**
 * Node cannot execute .cmd/.bat shims directly on Windows. Resolve the shim,
 * quote it using cmd.exe's rules, and hand spawn an already-escaped command
 * line instead of enabling a general-purpose shell.
 */
export function prepareCommand(
  command: string,
  args: string[],
  overrides: PrepareCommandOverrides = {},
): PreparedCommand {
  const platform = overrides.platform ?? process.platform;
  if (platform !== "win32") return { command, args: [...args] };

  const env = overrides.env ?? process.env;
  const resolved = overrides.resolveCommand?.(command)
    ?? resolveWindowsCommand(command, env, overrides.cwd);
  if (resolved === undefined) return { command, args: [...args] };
  if (/\.(?:com|exe)$/i.test(resolved)) return { command: resolved, args: [...args] };

  const normalized = path.win32.normalize(resolved);
  const doubleEscape = /node_modules[\\/]\.bin[\\/][^\\/]+\.cmd$/i.test(normalized);
  const shellCommand = [escapeCmdCommand(normalized), ...args.map((arg) => escapeCmdArgument(arg, doubleEscape))].join(" ");
  return {
    command: readEnvValue(env, "COMSPEC", "win32") ?? "cmd.exe",
    args: ["/d", "/s", "/c", `"${shellCommand}"`],
    windowsVerbatimArguments: true,
  };
}

function resolveWindowsCommand(
  command: string,
  env: NodeJS.ProcessEnv,
  cwd: string = process.cwd(),
): string | undefined {
  const hasSeparator = /[\\/]/.test(command);
  const pathEntries = hasSeparator
    ? [""]
    : [cwd, ...(readEnvValue(env, "PATH", "win32") ?? "").split(path.win32.delimiter)];
  const extensions = path.win32.extname(command) !== ""
    ? [""]
    : (readEnvValue(env, "PATHEXT", "win32") ?? ".COM;.EXE;.BAT;.CMD").split(path.win32.delimiter);

  for (const rawEntry of pathEntries) {
    const entry = rawEntry.replace(/^"|"$/g, "");
    const base = hasSeparator
      ? (path.win32.isAbsolute(command) ? command : path.win32.resolve(cwd, command))
      : path.win32.join(entry, command);
    for (const extension of extensions) {
      const candidate = `${base}${extension}`;
      if (fs.existsSync(candidate)) return path.win32.resolve(candidate);
    }
  }
  return undefined;
}

const CMD_META_CHARS = /([()\][%!^"`<>&|;, *?])/g;

function escapeCmdCommand(value: string): string {
  return value.replace(CMD_META_CHARS, "^$1");
}

function escapeCmdArgument(value: string, doubleEscapeMetaChars: boolean): string {
  let escaped = value.replace(/(?=(\\+?)?)\1"/g, "$1$1\\\"");
  escaped = escaped.replace(/(?=(\\+?)?)\1$/g, "$1$1");
  escaped = `"${escaped}"`.replace(CMD_META_CHARS, "^$1");
  return doubleEscapeMetaChars ? escaped.replace(CMD_META_CHARS, "^$1") : escaped;
}

/**
 * An environment variable read the way the PLATFORM reads it: Windows env
 * names are case-insensitive, so `Path`, `PATH` and `path` are one variable
 * there and exact-key lookup silently misses two of them.
 */
export function readEnvValue(
  env: NodeJS.ProcessEnv,
  name: string,
  platform: NodeJS.Platform = process.platform,
): string | undefined {
  if (platform !== "win32") return env[name];
  const key = Object.keys(env).find((candidate) => candidate.toLowerCase() === name.toLowerCase());
  return key === undefined ? undefined : env[key];
}

function removeDuplicateWindowsEnvKeys(env: NodeJS.ProcessEnv, keep: string): void {
  for (const key of Object.keys(env)) {
    if (key !== keep && key.toLowerCase() === keep.toLowerCase()) delete env[key];
  }
}
