/**
 * Zero-token brain-CLI probes for the wizard and the HUD heartbeat.
 *
 * The setup/HUD (TypeScript) side of the auth probes that also live in the
 * engine (agent/brain/probes.py). Both answer the same question the same
 * cheap way, but the wizard needs it BEFORE the engine profile is even
 * configured — so this drives the CLIs directly:
 *
 *  - codex: a one-shot `codex app-server` handshake + `getAuthStatus`
 *    (M0: `{authMethod: "chatgpt"}` signed in, `{authMethod: null}` out).
 *  - claude: `claude auth status --json` (M0: `loggedIn`/`authMethod`/
 *    `subscriptionType`; exit 0 either way, so parse JSON not exit code).
 *
 * CREDENTIAL HYGIENE: codex `getAuthStatus` returns an `authToken` field on
 * signed-in accounts, and claude's status carries `email`. Neither is ever
 * read, returned, or logged — results are built from an allowlist of
 * non-secret fields only. The presence-file existence checks the wizard uses
 * elsewhere stay presence-only (the `claudeCodeCredentialsPresent`
 * precedent); this module never reads a credential file's contents.
 *
 * These are health CHECKS, never the final proof: the wizard's
 * mayLeaveSetup gate still requires a real challenge turn (verify-contract),
 * exactly as the v1.4.0 key path does.
 */

import { spawn } from "node:child_process";
import { constants as fsConstants } from "node:fs";
import fs from "node:fs/promises";
import { readEnvValue } from "./system.js";
import { ANTHROPIC_API_KEY_ENV, FORBIDDEN_ENV_KEYS } from "./adapter-guard.js";

/**
 * Provider keys stripped from a probe's environment, so the app reports its
 * OWN sign-in state — never "signed in" borrowed from an inherited API key
 * while the subscription is actually absent (mirrors agent/brain/probes.py;
 * Codex review 2026-08-15, P2-3). Names come from adapter-guard, the one
 * module allowed to spell the subscription-token env vars.
 */
export const PROBE_SCRUB_ENV: readonly string[] = [
  ANTHROPIC_API_KEY_ENV,
  "ANTHROPIC_AUTH_TOKEN",
  ...FORBIDDEN_ENV_KEYS,
  "OPENAI_API_KEY",
];

/**
 * The environment a probe spawns with: the caller's base (PATH-augmented —
 * see BrainProbeOptions.env), with the credential keys scrubbed LAST so no
 * base can smuggle one back in.
 */
export function scrubbedProbeEnv(base: NodeJS.ProcessEnv = process.env): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...base };
  // Case-insensitive on purpose: Windows env names are case-insensitive and
  // an inherited `OpenAI_Api_Key` is the same variable.
  const banned = new Set(PROBE_SCRUB_ENV.map((k) => k.toUpperCase()));
  for (const key of Object.keys(env)) if (banned.has(key.toUpperCase())) delete env[key];
  return env;
}

export type BrainCli = "codex" | "claude";

/** Closed set — mirrors agent/brain/probes.py `state`. */
export type BrainAuthState = "signed-in" | "signed-out" | "cli-missing" | "probe-failed";

/**
 * The ONE auth method per app that means "the customer's own plan". The apps
 * can also be signed in pay-per-use (codex `login --with-api-key` reports
 * `apiKey`; claude's Console login reports `console`); those are NEVER a
 * plan sign-in — a "plan" proof passing on a paid key would prove the wrong
 * thing and lie in the copy ("no new bills"). Mirrors
 * agent/brain/probes.py SUBSCRIPTION_AUTH_METHODS (Codex review r2, P1).
 */
export const SUBSCRIPTION_AUTH_METHODS: Readonly<Record<BrainCli, readonly string[]>> = {
  // codex app-server auth modes that upstream classifies as ChatGPT-ACCOUNT
  // auth (Codex review r3): plain chatgpt, the tokens variant, and a
  // personal access token. "apiKey" is the pay-per-use one.
  codex: ["chatgpt", "chatgptAuthTokens", "personalAccessToken"],
  claude: ["claude.ai"],
};

/**
 * Methods that mean "signed in, but pay-per-use" — the only ones the wizard
 * may describe as "signed in with a developer key". Anything else that is
 * not a plan (claude's "none", an unknown future mode) is plain signed-out.
 */
export const PAY_PER_USE_AUTH_METHODS: Readonly<Record<BrainCli, readonly string[]>> = {
  codex: ["apiKey"],
  claude: ["console"],
};

export interface BrainAuthStatus {
  cli: BrainCli;
  state: BrainAuthState;
  loggedIn: boolean;
  /** Non-secret: "chatgpt" | "claude.ai" | undefined. */
  authMethod?: string;
  /** Non-secret plan tier when the CLI exposes it (claude: subscriptionType). */
  plan?: string;
}

export interface BrainProbeOptions {
  /** Resolved binary path (JARVIS records it when it installs the CLI). */
  command?: string;
  timeoutMs?: number;
  /**
   * The base environment for the spawn — the caller passes a PATH-augmented
   * one (system.ts spawnEnvironment). A Finder-launched app inherits
   * launchd's bare PATH (/usr/bin:/bin:/usr/sbin:/sbin), which does not hold
   * ~/.local/bin or /opt/homebrew/bin where the apps actually live: a
   * bare-name spawn there is ENOENT, "cli-missing", and the whole sign-in
   * journey silently falls through to the key path (Peter's live walkthrough
   * of the 2026-08-15 cut). Credential keys are scrubbed from this base
   * AFTER it is taken, so the scrub always wins. Defaults to process.env.
   */
  env?: NodeJS.ProcessEnv;
  /** Injection seam for tests — a fake spawn. */
  spawnImpl?: typeof spawn;
  /** Decides how env names are matched (Windows is case-insensitive). */
  platform?: NodeJS.Platform;
}

const DEFAULT_TIMEOUT_MS = 15_000;

/**
 * The explicit "the tool is HERE" override, per app. These are the engine's
 * own variable names (agent/brain/providers.py `_BRAIN_BINARIES`), read here
 * so the wizard and the engine agree about where the tool is: without this,
 * a customer who set the override watched the wizard report nothing installed
 * while the engine would have found it perfectly well.
 */
export const BRAIN_CLI_PATH_ENV: Record<BrainCli, string> = {
  codex: "HERMES_CODEX_CLI_PATH",
  claude: "HERMES_CLAUDE_CLI_PATH",
};

/**
 * What to spawn: an explicit test command, else the override if it names a
 * program that exists and can run, else the bare name through PATH.
 *
 * The fallback matters as much as the override — `find_brain_cli` falls back
 * to PATH when the override is stale, and a wizard that reported "not
 * installed" where the engine happily runs the tool would be worse than
 * having no override at all.
 */
async function binaryFor(cli: BrainCli, options: BrainProbeOptions): Promise<string> {
  if (options.command !== undefined) return options.command;
  // Case-INSENSITIVELY on Windows, where env names are (Codex review r3, P2):
  // Python's os.getenv would find `Hermes_Codex_Cli_Path` and the wizard must
  // not disagree with the engine about the same machine.
  const override = (readEnvValue(options.env ?? process.env, BRAIN_CLI_PATH_ENV[cli], options.platform) ?? "").trim();
  if (override === "") return cli;
  try {
    // isFile AND executable, exactly as the engine's find_brain_cli requires
    // (os.path.isfile + os.access X_OK). A DIRECTORY passes X_OK on POSIX —
    // it means "searchable" — so accepting it would spawn a directory, report
    // probe-failed, and recreate the wizard/engine disagreement this override
    // exists to remove: the engine would have fallen back to PATH and found a
    // working tool (Codex review r2, P2).
    const info = await fs.stat(override);
    if (!info.isFile()) return cli;
    await fs.access(override, fsConstants.X_OK);
    return override;
  } catch {
    return cli;
  }
}

/**
 * codex: drive one app-server handshake and read ONLY `authMethod` from the
 * getAuthStatus reply. `authToken` and every other field are dropped unread.
 */
export async function probeCodexAuth(options: BrainProbeOptions = {}): Promise<BrainAuthStatus> {
  const command = await binaryFor("codex", options);
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const spawnImpl = options.spawnImpl ?? spawn;

  const handshake =
    JSON.stringify({
      id: 1,
      method: "initialize",
      params: { clientInfo: { name: "jarvis-auth-probe", title: "JARVIS auth probe", version: "1" }, capabilities: {} },
    }) +
    "\n" +
    JSON.stringify({ method: "initialized", params: {} }) +
    "\n" +
    JSON.stringify({ id: 2, method: "getAuthStatus", params: {} }) +
    "\n";

  return await new Promise<BrainAuthStatus>((resolve) => {
    let child;
    try {
      child = spawnImpl(command, ["app-server"], { stdio: ["pipe", "pipe", "ignore"], env: scrubbedProbeEnv(options.env) });
    } catch {
      resolve(missing("codex"));
      return;
    }
    let settled = false;
    let buffer = "";
    const done = (status: BrainAuthStatus) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try {
        child.kill();
      } catch {
        /* already gone */
      }
      resolve(status);
    };
    const timer = setTimeout(() => done(probeFailed("codex", "timed out")), timeoutMs);

    child.on("error", (err: NodeJS.ErrnoException) => {
      done(err.code === "ENOENT" ? missing("codex") : probeFailed("codex", err.code ?? "spawn error"));
    });
    child.stdout?.on("data", (chunk: Buffer) => {
      buffer += chunk.toString("utf8");
      let index = buffer.indexOf("\n");
      while (index !== -1) {
        const line = buffer.slice(0, index).trim();
        buffer = buffer.slice(index + 1);
        index = buffer.indexOf("\n");
        if (line === "") continue;
        let message: Record<string, unknown>;
        try {
          message = JSON.parse(line) as Record<string, unknown>;
        } catch {
          continue;
        }
        if (message.id === 2 && message.result && typeof message.result === "object") {
          // ALLOWLIST READ: authMethod only. authToken is never touched.
          const authMethod = (message.result as Record<string, unknown>).authMethod;
          const method = typeof authMethod === "string" && authMethod !== "" ? authMethod : undefined;
          // Plan sign-in ONLY: "apiKey" is pay-per-use and reads as signed-out
          // (the method is still reported so the wizard can say why).
          const plan = method !== undefined && SUBSCRIPTION_AUTH_METHODS.codex.includes(method);
          done({
            cli: "codex",
            state: plan ? "signed-in" : "signed-out",
            loggedIn: plan,
            authMethod: method,
          });
          return;
        }
      }
    });
    child.on("close", () => done(probeFailed("codex", "no getAuthStatus reply")));
    try {
      // Write, then HOLD stdin open: codex app-server treats stdin EOF as
      // "client gone" and exits before answering id 2 (LIVE-FOUND on the
      // 2026-08-15 acceptance run — a signed-in ChatGPT app read as
      // probe-failed). The reply handler above kills the child instead; the
      // timeout is the other way out. Mirrors agent/brain/probes.py.
      child.stdin?.write(handshake);
    } catch {
      /* the close/error handlers will settle */
    }
  });
}

/**
 * claude: `claude auth status --json`. Parse JSON, not exit code (M0: exit 0
 * both signed in and out). Reads only `loggedIn`/`authMethod`/
 * `subscriptionType`; `email`/`orgId`/`orgName` are never read.
 */
export async function probeClaudeAuth(options: BrainProbeOptions = {}): Promise<BrainAuthStatus> {
  const command = await binaryFor("claude", options);
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const spawnImpl = options.spawnImpl ?? spawn;

  return await new Promise<BrainAuthStatus>((resolve) => {
    let child;
    try {
      child = spawnImpl(command, ["auth", "status", "--json"], { stdio: ["ignore", "pipe", "ignore"], env: scrubbedProbeEnv(options.env) });
    } catch {
      resolve(missing("claude"));
      return;
    }
    let settled = false;
    let stdout = "";
    const done = (status: BrainAuthStatus) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(status);
    };
    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch {
        /* already gone */
      }
      done(probeFailed("claude", "timed out"));
    }, timeoutMs);

    child.on("error", (err: NodeJS.ErrnoException) => {
      done(err.code === "ENOENT" ? missing("claude") : probeFailed("claude", err.code ?? "spawn error"));
    });
    child.stdout?.on("data", (chunk: Buffer) => {
      stdout += chunk.toString("utf8");
    });
    child.on("close", () => {
      const payload = firstJsonObject(stdout);
      if (payload === undefined) {
        done(probeFailed("claude", "no JSON"));
        return;
      }
      const authMethod = typeof payload.authMethod === "string" && payload.authMethod !== "" ? payload.authMethod : undefined;
      // Plan sign-in ONLY: a Console (API-billing) login is not the plan.
      const loggedIn = payload.loggedIn === true && authMethod !== undefined && SUBSCRIPTION_AUTH_METHODS.claude.includes(authMethod);
      const plan =
        typeof payload.subscriptionType === "string" && payload.subscriptionType !== "" ? payload.subscriptionType : undefined;
      done({
        cli: "claude",
        state: loggedIn ? "signed-in" : "signed-out",
        loggedIn,
        authMethod,
        plan,
      });
    });
  });
}

/** Tolerates the ``` fences `-p` output sometimes wears + stray lines. */
export function firstJsonObject(text: string): Record<string, unknown> | undefined {
  const trimmed = (text ?? "").trim();
  if (trimmed === "") return undefined;
  for (const candidate of [trimmed, trimmed.replace(/^```(?:json)?/i, "").replace(/```$/, "").trim()]) {
    try {
      const parsed = JSON.parse(candidate) as unknown;
      if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch {
      /* try the next candidate */
    }
  }
  const start = trimmed.indexOf("{");
  const end = trimmed.lastIndexOf("}");
  if (start !== -1 && end > start) {
    try {
      const parsed = JSON.parse(trimmed.slice(start, end + 1)) as unknown;
      if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch {
      return undefined;
    }
  }
  return undefined;
}

function missing(cli: BrainCli): BrainAuthStatus {
  return { cli, state: "cli-missing", loggedIn: false };
}

function probeFailed(cli: BrainCli, _detail: string): BrainAuthStatus {
  return { cli, state: "probe-failed", loggedIn: false };
}
