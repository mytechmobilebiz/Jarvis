import type { StepOutcome, WizardContext } from "../context.js";
import { classifySignInFailure, parseDeviceCode, stripAnsi, type SignInResult } from "../device-code.js";
import { hermesInvocation, openUrl as defaultOpenUrl } from "../system.js";
import { confirm, deviceCode, note, select, warn } from "../ui.js";
import {
  applyProviderConfig,
  markDirty,
  verifyAndFinish,
  type ModelRunners,
  type ModelSession,
} from "./model-config.js";
import { offlineNote, SIGN_IN_TIMEOUT_MS } from "./model-nous.js";

/**
 * ChatGPT / Codex sign-in — the advanced subscription path.
 *
 * Shape (fixed by the 2026-08-11 OpenAI findings and DECISION.md §1):
 *   - Turns run through the engine's existing `codex_app_server` transport
 *     (`model.openai_runtime = codex_app_server`, gated in
 *     hermes_cli/runtime_provider.py:374-388), which hands the whole turn to a
 *     `codex app-server` subprocess — OpenAI's documented "integrate Codex in
 *     your own product" surface — not raw `codex exec` shell-outs.
 *   - BRIGHT LINE: JARVIS never reads, copies or transmits ~/.codex/auth.json.
 *     The Codex CLI owns its OAuth lifecycle. Sign-in here runs the ENGINE's
 *     own device-code login (hermes_cli/auth.py:7038 `_codex_device_code_login`,
 *     against auth.openai.com), which mints its own credential; nothing in this
 *     file touches Codex's file.
 *   - The engine's runtime gate only accepts providers {openai, openai-codex}
 *     (runtime_provider.py:384), so the provider is `openai-codex`.
 */

/** The engine's minimum supported Codex CLI (agent/transports/codex_app_server.py:32). */
export const MIN_CODEX_VERSION: readonly [number, number, number] = [0, 125, 0];

/**
 * Model tiers offered for the Codex path.
 *
 * Slugs come from the pinned engine's curated Codex catalogue
 * (hermes_cli/codex_models.py:14-18). The quota behind them is the customer's
 * OWN ChatGPT plan, shared with their own ChatGPT and Codex use — so the light
 * tier is the default: a background triage loop that eats a buyer's ChatGPT
 * allowance before they have typed anything is a support ticket, not a feature.
 */
export const CODEX_MODELS = {
  light: "gpt-5.4-mini",
  balanced: "gpt-5.5",
} as const;

export type CodexTier = keyof typeof CODEX_MODELS;

const INSTALL_COMMAND = "npm install -g @openai/codex";

export async function signInCodex(
  ctx: WizardContext,
  runners: ModelRunners,
  session: ModelSession,
): Promise<StepOutcome> {
  const { notes } = session;
  const platform = runners.platform ?? process.platform;

  const present = await ensureCodexCli(ctx, runners, notes);
  if (!present) {
    notes.push(manualInstallNote(platform));
    notes.push(offlineNote());
    return { status: "skipped", notes, props: { signin_method: "codex", signin_result: "cli-missing" } };
  }

  // Two sign-ins are genuinely required here, and saying so up front beats a
  // confusing failure at the verification round-trip:
  //   - the Codex app's own login, which authenticates the turns it runs
  //     (the transport spawns `codex app-server` with its own CODEX_HOME —
  //     agent/transports/codex_app_server.py:90-94), and
  //   - the engine's own Codex credential, without which provider resolution
  //     refuses before the turn is ever handed over
  //     (hermes_cli/runtime_provider.py:1766-1772).
  // JARVIS reads neither store; it asks each owner about its own state.
  if (!(await ensureCodexAppSignedIn(ctx, runners, notes))) {
    notes.push(offlineNote());
    return { status: "skipped", notes, props: { signin_method: "codex", signin_result: "cli-signed-out" } };
  }

  const signedIn = await ensureCodexLogin(ctx, runners, session);
  if (signedIn !== "verified") {
    notes.push(offlineNote());
    return { status: "skipped", notes, props: { signin_method: "codex", signin_result: signedIn } };
  }

  const tier = await select<CodexTier>(
    ctx,
    "Which ChatGPT model should Jarvis use? Both draw on your own ChatGPT allowance.",
    [
      {
        key: "light",
        hotkey: "1",
        label: "Light",
        description: "recommended — leaves most of your ChatGPT allowance for you",
      },
      {
        key: "balanced",
        hotkey: "2",
        label: "Balanced",
        description: "stronger answers, uses your allowance faster",
      },
    ],
    "light",
  );
  const model = CODEX_MODELS[tier];

  ctx.state.providerChoice = "openai-codex";
  await applyProviderConfig(ctx, runners.run, session, {
    provider: "openai-codex",
    model,
    openaiRuntime: "codex_app_server",
  });
  notes.push(
    "Jarvis shares your ChatGPT allowance with your own use of ChatGPT and Codex. Every kind of work — "
      + "the morning brief, background triage, and your own chats — runs on the model above: the engine "
      + "takes one model per profile, so Jarvis cannot route background work to a cheaper tier for you. "
      + "If it uses more of your allowance than you want, turn schedules off or pick a lighter model by "
      + "running setup again from JARVIS settings.",
  );

  return verifyAndFinish(ctx, runners.run, {
    session,
    method: "codex",
    platform: runners.platform,
    props: { codex_tier: tier },
    remedy: "Check that the Codex app is still signed in (run codex login status in a terminal), then rerun this step.",
  });
}

/**
 * Confirm a usable Codex CLI, offering one guided install.
 *
 * npm is preferred over the PowerShell one-liner installer: JARVIS already
 * requires Node (Preflight), whereas `irm https://chatgpt.com/codex/install.ps1
 * | iex` is blocked on many managed Windows machines and reads as a scam to a
 * non-technical buyer. Both are named in the honest-failure note.
 */
async function ensureCodexCli(
  ctx: WizardContext,
  runners: ModelRunners,
  notes: string[],
): Promise<boolean> {
  const first = await detectCodex(ctx, runners);
  if (first.ok) {
    notes.push(`Codex app found (version ${first.version}).`);
    return true;
  }
  notes.push(first.reason);

  const install = await confirm(
    ctx,
    "Install the Codex app now? It is OpenAI's own app and it installs on this computer.",
    true,
  );
  if (!install) {
    notes.push("Codex install declined.");
    return false;
  }

  const result = await runners.run(ctx, "npm", ["install", "-g", "@openai/codex"], { stream: true });
  if (result.status !== 0) {
    notes.push("The Codex install did not finish.");
    return false;
  }

  const second = await detectCodex(ctx, runners);
  if (!second.ok) {
    // Freshly-installed global npm binaries often are not on the PATH of an
    // already-running process; say so instead of implying the install failed.
    notes.push(`${second.reason} A restart of Jarvis usually makes a fresh install visible.`);
    return false;
  }
  notes.push(`Codex app installed (version ${second.version}).`);
  return true;
}

/**
 * Ask the Codex app whether IT is signed in — `codex login status`, the
 * vendor's own read-only report. We never inspect ~/.codex/auth.json: the CLI
 * owns that file and its OAuth lifecycle, which is the single rule that keeps
 * this integration on the right side of OpenAI's terms.
 *
 * `codex login` needs a real terminal and a browser, so it cannot be driven
 * from a packaged GUI: the customer is shown the exact command and asked to
 * confirm, then the state is re-read rather than assumed.
 */
async function ensureCodexAppSignedIn(
  ctx: WizardContext,
  runners: ModelRunners,
  notes: string[],
): Promise<boolean> {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const status = await runners.run(ctx, "codex", ["login", "status"], { stream: false });
    if (status.status === 0 && /logged in/i.test(`${status.stdout}${status.stderr}`)) {
      notes.push("The Codex app is signed in to ChatGPT.");
      return true;
    }
    if (attempt > 0) break;

    notes.push("The Codex app is not signed in to ChatGPT yet.");
    // The command is repeated inside the warning on purpose: a terminal run
    // inherits stdio, so TerminalIO.command() prints nothing (terminal-io.ts:148)
    // and a customer following the GUI-shaped instruction would see no command.
    ctx.io.command("codex login");
    warn(ctx, "The Codex app has its own sign-in, and it needs a terminal window. Open one, run: codex login — finish in your browser, then come back here.");
    const done = await confirm(ctx, "I signed the Codex app in. Check again?", true);
    if (!done) {
      notes.push("Codex app sign-in was declined. Open a terminal, run: codex login — then rerun this step.");
      return false;
    }
  }

  notes.push(
    "The Codex app still reports itself signed out, so Jarvis will not pretend otherwise. "
      + "Open a terminal, run: codex login — then rerun this step.",
  );
  return false;
}

interface CodexDetection {
  ok: boolean;
  version?: string;
  reason: string;
}

async function detectCodex(ctx: WizardContext, runners: ModelRunners): Promise<CodexDetection> {
  const probe = await runners.run(ctx, "codex", ["--version"], { stream: false });
  if (probe.status !== 0) {
    return { ok: false, reason: "The Codex app is not installed on this computer." };
  }
  const version = parseCodexVersion(`${probe.stdout} ${probe.stderr}`);
  if (version === undefined) {
    return { ok: false, reason: "The Codex app answered, but not with a version I recognise." };
  }
  if (compareVersions(version, MIN_CODEX_VERSION) < 0) {
    return {
      ok: false,
      version: version.join("."),
      reason: `The Codex app on this computer is version ${version.join(".")}; Jarvis needs ${MIN_CODEX_VERSION.join(".")} or newer.`,
    };
  }
  return { ok: true, version: version.join("."), reason: "" };
}

export function parseCodexVersion(text: string): [number, number, number] | undefined {
  const match = /(\d+)\.(\d+)\.(\d+)/.exec(stripAnsi(text));
  if (match === null) return undefined;
  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

function compareVersions(
  left: readonly [number, number, number],
  right: readonly [number, number, number],
): number {
  for (let index = 0; index < 3; index += 1) {
    if (left[index] !== right[index]) return left[index] - right[index];
  }
  return 0;
}

async function ensureCodexLogin(
  ctx: WizardContext,
  runners: ModelRunners,
  session: ModelSession,
): Promise<SignInResult> {
  const { notes } = session;
  const statusInvocation = hermesInvocation(ctx, ["-p", "jarvis", "auth", "status", "openai-codex"], runners.platform);
  const status = await runners.run(ctx, statusInvocation.command, statusInvocation.args, {
    stream: false,
  });
  // hermes_cli/auth_commands.py:487-505 prints "<provider>: logged in" or
  // "<provider>: logged out (<reason>)".
  if (status.status === 0 && /:\s*logged in\b/i.test(`${status.stdout}${status.stderr}`)) {
    notes.push("ChatGPT sign-in was already in place.");
    return "verified";
  }

  const openUrl = runners.openUrl ?? defaultOpenUrl;
  let attempt = 0;

  // The same plain-language retry the Nous path offers: a device flow fails for
  // ordinary reasons (a mistyped code, a closed tab, a dropped connection), and
  // a single failure should not cost the customer the whole step.
  while (true) {
    attempt += 1;
    note(ctx, "I will show you a ChatGPT sign-in page and a short code. Sign in there, and I will do the rest.");
    let announced = false;
    let transcript = "";

    const loginInvocation = hermesInvocation(
      ctx,
      ["-p", "jarvis", "auth", "add", "openai-codex", "--type", "oauth"],
      runners.platform,
    );
    const login = await runners.watch(
      ctx,
      loginInvocation.command,
      loginInvocation.args,
      {
        quiet: true,
        timeoutMs: SIGN_IN_TIMEOUT_MS,
        onLine: (line) => {
          transcript += `${line}\n`;
          if (announced) return;
          const prompt = parseDeviceCode(transcript);
          if (prompt === undefined) return;
          announced = true;
          deviceCode(ctx, {
            attempt,
            status: "waiting",
            message: "Sign in with your ChatGPT account so Jarvis can use the Codex app.",
            url: prompt.url,
            code: prompt.code,
          });
          void openUrl(ctx, prompt.url).catch(() => {
            warn(ctx, "I could not open your browser by myself — please open the address above yourself.");
          });
        },
      },
    );

    if (login.status === 0) {
      markDirty(session);
      if (announced) {
        deviceCode(ctx, { attempt, status: "signed-in", message: "Signed in. That code is finished with." });
      }
      notes.push("Signed in with ChatGPT; the Codex app keeps its own sign-in, and Jarvis never reads it.");
      return "verified";
    }

    const failure: SignInResult = login.end === "aborted"
      ? "cancelled"
      : login.end === "timeout"
        ? "timeout"
        : classifySignInFailure(login.status, `${transcript}\n${login.stderr}`);
    if (announced) {
      deviceCode(ctx, {
        attempt,
        status: failure === "cancelled" ? "cancelled" : "failed",
        message: "That code is no longer usable.",
      });
    }
    notes.push(`ChatGPT sign-in did not complete (${codexFailureLabel(failure)}).`);

    if (failure === "cancelled" && login.end === "aborted") return "cancelled";
    const again = await confirm(
      ctx,
      "Try the ChatGPT sign-in again? Choose no to finish setup without a provider for now.",
      true,
    );
    if (!again) return failure;
  }
}

function codexFailureLabel(failure: SignInResult): string {
  switch (failure) {
    case "cancelled":
      return "the sign-in was stopped";
    case "timeout":
      return "the sign-in page timed out";
    case "denied":
      return "the sign-in was refused";
    case "network":
      return "your computer could not reach the sign-in service";
    case "permission":
      return "this computer refused the engine permission";
    case "engine-missing":
      return "the Hermes engine could not be run";
    default:
      return "the engine reported an error";
  }
}

export function manualInstallNote(platform: NodeJS.Platform): string {
  return platform === "win32"
    ? `To use ChatGPT sign-in, install the Codex app first: open PowerShell and run ${INSTALL_COMMAND}, `
      + "or follow OpenAI's installer at chatgpt.com/codex/install. Then rerun this step."
    : `To use ChatGPT sign-in, install the Codex app first: run ${INSTALL_COMMAND} in a terminal, `
      + "then rerun this step.";
}
