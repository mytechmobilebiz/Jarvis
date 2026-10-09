import { constants as fsConstants } from "node:fs";
import { spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import { WizardError, type StepOutcome, type WizardContext } from "../context.js";
import { ANTHROPIC_API_KEY_ENV, FORBIDDEN_ENV_KEYS } from "../adapter-guard.js";
import { installBrainCli, type BrainVendor } from "../brain-install.js";
import { brainCliHomeDirs } from "../brain-cli-homes.js";
import {
  BRAIN_CLI_PATH_ENV,
  firstJsonObject,
  probeClaudeAuth,
  probeCodexAuth,
  type BrainAuthStatus,
  type BrainProbeOptions,
} from "../brain-probe.js";
import { parseDeviceCode, stripAnsi, type DeviceCodeInfo } from "../device-code.js";
import { readEnvFile, upsertEnv } from "../env-file.js";
import { migrateProfileTerminalCwd } from "../profile-migrate.js";
import { commandExists, openUrl as defaultOpenUrl, readEnvValue, spawnEnvironment, type SystemOverrides } from "../system.js";
import { confirm, deviceCode, note, select, warn } from "../ui.js";
import {
  applyProviderConfig,
  makeRouteInert,
  finishRouteChange,
  verifyAndFinish,
  type ModelRunners,
  type ModelSession,
  type SignInMethod,
  type VerifyOptions,
} from "./model-config.js";

export type BrainRouteKey = Extract<SignInMethod, "codex-cli" | "claude-cli">;

export const BRAIN_ROUTES: Readonly<Record<BrainVendor, {
  key: BrainRouteKey;
  provider: string;
  model: string;
  app: "ChatGPT" | "Claude";
  plan: "your ChatGPT plan" | "your Claude plan";
}>> = {
  codex: {
    key: "codex-cli",
    provider: "codex-cli",
    model: "gpt-5.6-sol",
    app: "ChatGPT",
    plan: "your ChatGPT plan",
  },
  claude: {
    key: "claude-cli",
    provider: "claude-subscription-directsdk-experimental",
    model: "claude-sonnet-5[1m]",
    app: "Claude",
    plan: "your Claude plan",
  },
};

/** A plan proof must never borrow a pay-per-use credential. */
export const BRAIN_VERIFY_SCRUB_ENV: readonly string[] = [
  ANTHROPIC_API_KEY_ENV,
  "ANTHROPIC_AUTH_TOKEN",
  ...FORBIDDEN_ENV_KEYS,
  "OPENAI_API_KEY",
  "CODEX_API_KEY",
  "CODEX_ACCESS_TOKEN",
];

/** The Codex sign-in itself gets the same bright-line environment. */
export const CODEX_LOGIN_SCRUB_ENV: readonly string[] = [
  "OPENAI_API_KEY",
  "CODEX_API_KEY",
  "CODEX_ACCESS_TOKEN",
];

export const BRAIN_SIGN_IN_TIMEOUT_MS = 5 * 60 * 1_000;
export const BRAIN_SIGN_IN_POLL_MS = 2_000;

/** Profile-recorded vendor binary locations are part of the engine's environment too. */
export async function brainProbeEnvironment(
  ctx: WizardContext,
  overrides: Pick<SystemOverrides, "baseEnv" | "homeDir" | "platform" | "execPath"> = {},
): Promise<NodeJS.ProcessEnv> {
  const env = spawnEnvironment(ctx, undefined, overrides);
  const profile = await readEnvFile(path.join(ctx.profileDir, ".env")).catch(() => ({}));
  const platform = overrides.platform ?? process.platform;
  for (const name of Object.values(BRAIN_CLI_PATH_ENV)) {
    const saved = (readEnvValue(profile, name, platform) ?? "").trim();
    if (saved === "") continue;
    if (platform === "win32") {
      for (const key of Object.keys(env)) if (key.toLowerCase() === name.toLowerCase()) delete env[key];
    }
    env[name] = saved;
  }
  return env;
}

export interface BrainFlowDeps {
  gitBashPresent?: () => Promise<boolean>;
  install?: typeof installBrainCli;
  find?: (ctx: WizardContext, vendor: BrainVendor, platform?: NodeJS.Platform) => Promise<string | undefined>;
  probe?: (ctx: WizardContext, vendor: BrainVendor, binaryPath: string, platform?: NodeJS.Platform) => Promise<BrainAuthStatus>;
  delay?: (milliseconds: number) => Promise<void>;
  pollIntervalMs?: number;
  signInTimeoutMs?: number;
  migrateProfile?: typeof migrateProfileTerminalCwd;
}

const sleep = async (milliseconds: number): Promise<void> => await new Promise((resolve) => setTimeout(resolve, milliseconds));

/** One recovery door for every Brain route transaction. Undefined returns to Screen 1. */
export async function writeBrainConfigWithRecovery<T>(
  ctx: WizardContext,
  write: () => Promise<T>,
  deps: Pick<BrainFlowDeps, "migrateProfile"> = {},
): Promise<T | undefined> {
  try {
    return await write();
  } catch (error) {
    if (!(error instanceof WizardError)) throw error;
  }

  const choice = await select<"reset" | "back">(
    ctx,
    "JARVIS couldn't update his settings file.",
    [
      {
        key: "reset",
        hotkey: "1",
        label: "Reset the settings file and try again",
        description: "keeps your knowledge, keys and files",
      },
      { key: "back", hotkey: "2", label: "Back", description: "return to the Brain choices" },
    ],
    "reset",
  );
  if (choice === "back") return undefined;

  try {
    await fs.copyFile(
      path.join(ctx.repoRoot, "agents", "jarvis", "config.yaml"),
      path.join(ctx.profileDir, "config.yaml"),
    );
    await (deps.migrateProfile ?? migrateProfileTerminalCwd)(ctx);
    return await write();
  } catch {
    warn(ctx, "JARVIS still couldn't update his settings file. I returned to the Brain choices, sir.");
    return undefined;
  }
}

/** Recorded location first, known vendor homes second, PATH last. */
export async function findBrainCli(
  ctx: WizardContext,
  vendor: BrainVendor,
  platform: NodeJS.Platform = process.platform,
): Promise<string | undefined> {
  const env = await brainProbeEnvironment(ctx, { platform });
  const recorded = (readEnvValue(env, BRAIN_CLI_PATH_ENV[vendor], platform) ?? "").trim();
  if (recorded !== "" && await executableFile(recorded)) return recorded;

  const names = platform === "win32" ? [vendor === "codex" ? "codex.exe" : "claude.exe"] : [vendor];
  for (const home of brainCliHomeDirs(platform, ctx.homeDir, env)) {
    for (const name of names) {
      const candidate = platform === "win32" ? path.win32.join(home, name) : path.join(home, name);
      if (await executableFile(candidate)) return candidate;
    }
  }
  if (vendor === "claude") {
    const located = platform === "win32"
      ? spawnSync("where.exe", ["claude"], { env, encoding: "utf8" })
      : spawnSync("sh", ["-c", "command -v claude"], { env, encoding: "utf8" });
    return located.status === 0 ? located.stdout.trim().split(/\r?\n/)[0] || undefined : undefined;
  }
  return commandExists(ctx, vendor, { platform }) ? vendor : undefined;
}

async function executableFile(candidate: string): Promise<boolean> {
  try {
    if (!(await fs.stat(candidate)).isFile()) return false;
    await fs.access(candidate, fsConstants.X_OK);
    return true;
  } catch {
    return false;
  }
}

/** Repair only the Codex binary JARVIS installed and recorded as its own. */
export async function repairManagedCodex(
  ctx: WizardContext,
  deps: Pick<BrainFlowDeps, "install"> = {},
  platform: NodeJS.Platform = ctx.platform ?? process.platform,
): Promise<void> {
  const env = await readEnvFile(path.join(ctx.profileDir, ".env"));
  const recorded = (readEnvValue(env, BRAIN_CLI_PATH_ENV.codex, platform) ?? "").trim();
  const managed = path.join(ctx.hermesRoot, "brain", "codex", platform === "win32" ? "codex.exe" : "codex");
  const ours = platform === "win32"
    ? recorded.toLowerCase() === managed.toLowerCase()
    : recorded !== "" && path.resolve(recorded) === path.resolve(managed);
  // Homebrew/winget and explicit external paths own their sibling helper; do not modify them.
  if (!ours) return;
  await (deps.install ?? installBrainCli)(ctx, "codex", (line) => note(ctx, line));
}

async function probeBrain(
  ctx: WizardContext,
  vendor: BrainVendor,
  binaryPath: string,
  platform?: NodeJS.Platform,
): Promise<BrainAuthStatus> {
  const env = await brainProbeEnvironment(ctx, platform === undefined ? {} : { platform });
  const options: BrainProbeOptions = { command: binaryPath, env };
  if (platform !== undefined) options.platform = platform;
  return vendor === "codex" ? await probeCodexAuth(options) : await probeClaudeAuth(options);
}

/** Set up one explicitly chosen vendor. Undefined means return to Screen 1. */
export async function runBrainVendor(
  ctx: WizardContext,
  runners: ModelRunners,
  session: ModelSession,
  vendor: BrainVendor,
  deps: BrainFlowDeps = {},
): Promise<StepOutcome | undefined> {
  const route = BRAIN_ROUTES[vendor];
  const find = deps.find ?? findBrainCli;
  const probe = deps.probe ?? probeBrain;
  const install = deps.install ?? installBrainCli;
  let binaryPath = await find(ctx, vendor, runners.platform);

  if (binaryPath === undefined) {
    note(
      ctx,
      `I'll fetch the official ${route.app} app for you — about ${vendor === "codex" ? "90" : "300"} MB, one time. Nothing to click.`,
    );
    try {
      binaryPath = (await install(ctx, vendor, (line) => note(ctx, line))).binaryPath;
    } catch (error) {
      warn(ctx, `${error instanceof Error ? error.message : String(error)} You can try again, or set this up later.`);
      return undefined;
    }
  }

  let status = await probe(ctx, vendor, binaryPath, runners.platform);
  if (status.state !== "signed-in") {
    const signedIn = vendor === "codex"
      ? await signInCodexApp(ctx, runners, binaryPath, deps)
      : await signInClaudeApp(ctx, runners, binaryPath, deps);
    if (!signedIn.complete) return undefined;
    status = await probe(ctx, vendor, binaryPath, runners.platform);
    if (status.state !== "signed-in") {
      warn(ctx, `The ${route.app} app signed in, but I couldn't confirm that it is using your plan. You can try again, or set this up later.`);
      return undefined;
    }
  }

  let outcome: Awaited<ReturnType<typeof verifyAndFinish>> | undefined;
  try {
    const routeWritten = await writeBrainConfigWithRecovery(ctx, async () => {
      await makeRouteInert(ctx, runners.run, session, runners.platform);
      if (vendor === "claude") {
        await upsertEnv(ctx, path.join(ctx.profileDir, ".env"), {
          CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND: binaryPath,
        });
      }
      const env = await readEnvFile(path.join(ctx.profileDir, ".env"));
      await applyProviderConfig(ctx, runners.run, session, {
        provider: route.provider,
        model: route.model,
        platform: runners.platform,
        existingEnvKeys: Object.entries(env).filter(([, value]) => value.trim() !== "").map(([name]) => name),
      });
      return true;
    }, deps);
    if (routeWritten !== true) return undefined;
    const verifyOptions: VerifyOptions = {
      session,
      method: route.key,
      platform: runners.platform,
      maxAttempts: 1,
      remedy: "So I returned to the Brain choices.",
      scrubEnv: BRAIN_VERIFY_SCRUB_ENV,
      successNote: `${route.app} answered — JARVIS is thinking with ${route.plan}, sir.`,
      props: { brain_route: route.key },
    };
    outcome = await verifyAndFinish(ctx, runners.run, verifyOptions);
    if (vendor === "claude" && outcome.status === "failed" && outcome.authExpired) {
      note(ctx, "Signing out the expired Claude sign-in before trying again.");
      await runners.run(ctx, binaryPath, ["auth", "logout"], { stream: false, unsetEnv: BRAIN_VERIFY_SCRUB_ENV });
      const signedIn = await signInClaudeApp(ctx, runners, binaryPath, deps);
      if (signedIn.complete) outcome = await verifyAndFinish(ctx, runners.run, verifyOptions);
    }
    if (vendor === "codex" && outcome.status === "failed" && await confirm(ctx, CODEX_ASK_AGAIN, true)) {
      outcome = await verifyAndFinish(ctx, runners.run, verifyOptions);
    }
    if (outcome.status === "done") return outcome;

    warn(ctx, `The ${route.app} app is signed in, but its answer did not reach JARVIS. Choose what you'd like me to try next, sir.`);
    return undefined;
  } finally {
    await finishRouteChange(ctx, session, outcome?.status === "done");
  }
}

// ChatGPT can lose its first answer after a sign-in and pass on the next ask
// (Windows, 1.7.2, BUGS.md 2026-09-26), so its proof gets one more try.
export const CODEX_ASK_AGAIN = "ChatGPT's first answer sometimes gets lost, sir. Shall I ask again?";

interface SignInAttempt {
  complete: boolean;
}

async function signInCodexApp(
  ctx: WizardContext,
  runners: ModelRunners,
  binaryPath: string,
  deps: BrainFlowDeps,
): Promise<SignInAttempt> {
  const abort = new AbortController();
  const abortFromContext = (): void => abort.abort();
  ctx.signal?.addEventListener("abort", abortFromContext, { once: true });
  let captured = "";
  let reveal: ((info: DeviceCodeInfo) => void) | undefined;
  const promptReady = new Promise<DeviceCodeInfo>((resolve) => { reveal = resolve; });
  note(ctx, "Opening the ChatGPT sign-in…");
  let openingBack = ctx.io.offerEscape?.("Back");
  const watched = runners.watch(ctx, binaryPath, ["login", "--device-auth"], {
    quiet: true,
    signal: abort.signal,
    timeoutMs: deps.signInTimeoutMs ?? BRAIN_SIGN_IN_TIMEOUT_MS,
    unsetEnv: CODEX_LOGIN_SCRUB_ENV,
    onLine: (line) => {
      captured += `${line}\n`;
      const parsed = parseDeviceCode(captured);
      if (parsed !== undefined && reveal !== undefined) {
        const ready = reveal;
        reveal = undefined;
        ready(parsed);
      }
    },
  });

  try {
    const first = await Promise.race([
      promptReady.then((info) => ({ kind: "prompt" as const, info })),
      watched.then((result) => ({ kind: "ended" as const, result })),
      ...(openingBack === undefined ? [] : [openingBack.taken.then(() => ({ kind: "back" as const }))]),
    ]);
    if (first.kind === "back") return { complete: false };
    if (first.kind === "ended") {
      warn(ctx, "The ChatGPT sign-in did not start. You can try again, or set this up later.");
      return { complete: false };
    }

    openingBack?.retract();
    openingBack = undefined;

    deviceCode(ctx, {
      attempt: 1,
      status: "waiting",
      url: first.info.url,
      code: first.info.code,
      message: `I've opened a ChatGPT sign-in page. Enter this code there: ${first.info.code}. JARVIS never sees your password — I'll notice when you're done.`,
    });
    await openBrowser(ctx, runners, first.info.url);
    const complete = await waitForSignIn(ctx, runners, "codex", binaryPath, first.info.url, watched, abort, deps);
    deviceCode(ctx, {
      attempt: 1,
      status: complete ? "signed-in" : "cancelled",
      message: complete ? "ChatGPT sign-in complete." : "ChatGPT sign-in stopped.",
    });
    return { complete };
  } finally {
    abort.abort();
    openingBack?.retract();
    ctx.signal?.removeEventListener("abort", abortFromContext);
  }
}

export interface ClaudeLoginLaunch {
  command: string;
  args: string[];
  commandFile?: string;
  commandFileContent?: string;
}

/** Pure platform command builder, pinned in tests for both shipped platforms. */
export function claudeLoginLaunch(
  profileDir: string,
  binaryPath: string,
  platform: NodeJS.Platform,
): ClaudeLoginLaunch {
  if (platform === "win32") {
    return {
      command: "cmd.exe",
      args: ["/c", "start", "JARVIS sign-in", binaryPath, "auth", "login"],
    };
  }
  const commandFile = path.join(profileDir, "sign-in-claude.command");
  return {
    command: "open",
    args: ["-a", "Terminal", commandFile],
    commandFile,
    commandFileContent: `#!/bin/bash\n\"${binaryPath.replaceAll("\\", "\\\\").replaceAll("\"", "\\\"")}\" auth login\n`,
  };
}

async function signInClaudeApp(
  ctx: WizardContext,
  runners: ModelRunners,
  binaryPath: string,
  deps: BrainFlowDeps,
): Promise<SignInAttempt> {
  const platform = runners.platform ?? process.platform;
  const launch = claudeLoginLaunch(ctx.profileDir, binaryPath, platform);
  try {
    if (launch.commandFile !== undefined && launch.commandFileContent !== undefined) {
      await fs.writeFile(launch.commandFile, launch.commandFileContent, { encoding: "utf8", mode: 0o755 });
      await fs.chmod(launch.commandFile, 0o755);
    }
    const opened = await runners.run(ctx, launch.command, launch.args, { stream: false });
    if (opened.status !== 0) {
      warn(ctx, "The Claude sign-in window did not open. You can try again, or set this up later.");
      return { complete: false };
    }

    note(ctx, "A small window just opened and your browser will follow. Sign in to Claude there — JARVIS never sees your password. I'll notice when you're done.");
    const complete = await waitForSignIn(ctx, runners, "claude", binaryPath, "https://claude.ai/", undefined, undefined, deps);
    return { complete };
  } finally {
    if (launch.commandFile !== undefined) await fs.rm(launch.commandFile, { force: true }).catch(() => {});
  }
}

async function waitForSignIn(
  ctx: WizardContext,
  runners: ModelRunners,
  vendor: BrainVendor,
  binaryPath: string,
  url: string,
  watched: ReturnType<ModelRunners["watch"]> | undefined,
  abort: AbortController | undefined,
  deps: BrainFlowDeps,
): Promise<boolean> {
  const interval = deps.pollIntervalMs ?? BRAIN_SIGN_IN_POLL_MS;
  const timeout = deps.signInTimeoutMs ?? BRAIN_SIGN_IN_TIMEOUT_MS;
  const delay = deps.delay ?? sleep;
  const back = ctx.io.offerEscape?.("Back");
  let reopen = ctx.io.offerEscape?.("Open the page again");
  let elapsed = 0;

  try {
    while (elapsed < timeout) {
      if (ctx.signal?.aborted === true) {
        abort?.abort();
        return false;
      }
      const signedIn = await verifySignedIn(ctx, runners, vendor, binaryPath);
      if (signedIn) {
        abort?.abort();
        return true;
      }
      if (vendor === "claude" && elapsed > 0 && Math.floor(elapsed / 30_000) > Math.floor((elapsed - interval) / 30_000)) {
        const minutes = Math.floor(elapsed / 60_000);
        const seconds = Math.floor((elapsed % 60_000) / 1_000);
        const duration = [minutes ? `${minutes} min` : "", seconds ? `${seconds} s` : ""].filter(Boolean).join(" ");
        note(ctx, `Still waiting for your sign-in in the browser (${duration}). Nothing opened? Use Open the page again, sir.`);
      }

      const tick = delay(interval).then(() => "tick" as const);
      const choices: Promise<"tick" | "back" | "reopen" | "ended">[] = [tick];
      if (back !== undefined) choices.push(back.taken.then(() => "back" as const));
      if (reopen !== undefined) choices.push(reopen.taken.then(() => "reopen" as const));
      if (watched !== undefined) choices.push(watched.then(() => "ended" as const));
      const event = await Promise.race(choices);
      if (event === "back") {
        abort?.abort();
        return false;
      }
      if (event === "reopen") {
        reopen?.retract();
        await openBrowser(ctx, runners, url);
        reopen = ctx.io.offerEscape?.("Open the page again");
        continue;
      }
      if (event === "ended" && vendor === "codex") {
        const signedIn = await verifySignedIn(ctx, runners, vendor, binaryPath);
        if (!signedIn) {
          warn(ctx, "The sign-in didn't complete. We can try again, or you can set this up later from the JARVIS app.");
        }
        return signedIn;
      }
      elapsed += interval;
    }
  } finally {
    back?.retract();
    reopen?.retract();
  }

  abort?.abort();
  warn(ctx, "The sign-in didn't complete. We can try again, or you can set this up later from the JARVIS app.");
  return false;
}

export async function verifySignedIn(
  ctx: WizardContext,
  runners: ModelRunners,
  vendor: BrainVendor,
  binaryPath: string,
): Promise<boolean> {
  const args = vendor === "codex" ? ["login", "status"] : ["auth", "status", "--json"];
  const result = await runners.run(ctx, binaryPath, args, { stream: false, unsetEnv: BRAIN_VERIFY_SCRUB_ENV });
  if (vendor === "codex") {
    const lines = stripAnsi(`${result.stdout}\n${result.stderr}`).split(/\r?\n/);
    return result.status === 0 && lines.some((line) => /^Logged in\b/i.test(line.trim()));
  }
  const payload = firstJsonObject(result.stdout);
  return payload?.loggedIn === true;
}

async function openBrowser(ctx: WizardContext, runners: ModelRunners, url: string): Promise<void> {
  const open = runners.openUrl ?? (async (context: WizardContext, target: string) => {
    const options: { platform?: NodeJS.Platform; run: ModelRunners["run"] } = { run: runners.run };
    if (runners.platform !== undefined) options.platform = runners.platform;
    await defaultOpenUrl(context, target, options);
  });
  await open(ctx, url).catch(() => warn(ctx, "I couldn't open the sign-in page. Use Open the page again, sir."));
}
