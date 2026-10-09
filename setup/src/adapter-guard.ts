/**
 * The Claude-Code token-replay guard (standing liability, v1.4.0).
 *
 * The engine ships `agent/anthropic_adapter.py`: it harvests the Claude Code
 * OAuth token from the keychain / ~/.claude/.credentials.json and calls
 * api.anthropic.com directly with spoofed Claude Code identity headers. That is
 * the pattern Anthropic enforces against, and the ban would land on the
 * CUSTOMER'S personal Claude account — a far worse failure than a bad API key.
 *
 * JARVIS therefore must never write a config that can reach it. Three doors,
 * all closed here (verified against the pinned engine, v2026.7.1):
 *
 *   1. Provider aliases that resolve to the adapter:
 *      `plugins/model-providers/anthropic/__init__.py:45`
 *        aliases=("claude", "claude-oauth", "claude-code")
 *      `hermes_cli/auth.py:1621`  "claude": "anthropic", "claude-code": "anthropic"
 *   2. Subscription-token env vars the anthropic provider accepts INSTEAD of an
 *      API key: `hermes_cli/auth.py:317`
 *        api_key_env_vars=("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
 *      The first is a real API key; the other two are subscription tokens.
 *   3. Selecting the anthropic provider with NO key at all — the engine then
 *      falls through to the adapter's keychain read.
 *
 * `assertNoTokenReplay` is called before every config/env write the Model step
 * performs, so a future edit that violates the rule fails setup loudly instead
 * of quietly signing the buyer's Claude account up for a ban.
 */

import { WizardError } from "./context.js";

/** Provider ids/aliases that resolve to the Claude-Code token-replay adapter. */
export const FORBIDDEN_PROVIDER_ALIASES: readonly string[] = [
  "claude",
  "claude-code",
  "claude_code",
  "claudecode",
  "claude-oauth",
  "claude_oauth",
];

/** Env vars that hand the engine a Claude SUBSCRIPTION token instead of a key. */
export const FORBIDDEN_ENV_KEYS: readonly string[] = [
  "CLAUDE_CODE_OAUTH_TOKEN",
  "ANTHROPIC_TOKEN",
];

/** The one Anthropic credential JARVIS is allowed to write. */
export const ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY";

export interface ConfigWriteIntent {
  /** `model.provider` about to be written, if any. */
  provider?: string;
  /** `model.default` about to be written, if any. */
  model?: string;
  /** Env keys about to be written into the profile .env. */
  envKeys?: readonly string[];
  /**
   * Env keys already present with a non-empty value. An Anthropic route is only
   * legal when ANTHROPIC_API_KEY is in this set or in `envKeys`.
   */
  existingEnvKeys?: readonly string[];
}

const HINT =
  "This is a JARVIS bug, not something you did. Jarvis will not sign in with a Claude subscription: "
  + "Anthropic bans third-party apps from using Claude.ai logins, and the ban would land on your account. "
  // Deliberately generic: this module is a leaf (the route list imports the
  // guard, not the other way round), and naming routes here is how the last
  // version ended up recommending a route the build cannot offer.
  + "Choose one of the sign-in options the Model step offers, or paste an Anthropic API key instead.";

function normalize(value: string): string {
  return value.trim().toLowerCase();
}

/** True when a model id routes at Anthropic (`anthropic/...` or a bare claude-*). */
export function targetsAnthropic(model: string | undefined): boolean {
  if (model === undefined) return false;
  const id = normalize(model);
  return id.startsWith("anthropic/") || id.startsWith("claude-") || id === "claude";
}

/**
 * Throws when a write would open a path to the token-replay adapter.
 * Called at setup time, and asserted directly in tests.
 */
export function assertNoTokenReplay(intent: ConfigWriteIntent): void {
  const provider = intent.provider === undefined ? undefined : normalize(intent.provider);
  if (provider !== undefined && FORBIDDEN_PROVIDER_ALIASES.includes(provider)) {
    throw new WizardError(
      `Refusing to set the model provider to "${intent.provider}".`,
      HINT,
      { fatal: true },
    );
  }

  const envKeys = (intent.envKeys ?? []).map((key) => key.trim().toUpperCase());
  const forbidden = envKeys.find((key) => FORBIDDEN_ENV_KEYS.includes(key));
  if (forbidden !== undefined) {
    throw new WizardError(`Refusing to write ${forbidden} into the profile.`, HINT, { fatal: true });
  }

  // An Anthropic route with no API key anywhere is the adapter's own entry
  // point: the engine falls back to the Claude Code keychain read.
  //
  // An explicit non-Anthropic provider is NOT such a route even when the model
  // slug names a Claude model: "anthropic/claude-sonnet-5" under provider=nous
  // is Nous routing its own catalogue (models.py:180-185), and under
  // provider=openrouter it is OpenRouter's. Only an explicit anthropic
  // provider — or an unset provider, where the engine auto-detects from the
  // model slug — reaches api.anthropic.com and therefore the adapter.
  const anthropicRoute = provider === "anthropic"
    || (provider === undefined && targetsAnthropic(intent.model));
  if (!anthropicRoute) return;
  const known = new Set([...envKeys, ...(intent.existingEnvKeys ?? []).map((key) => key.trim().toUpperCase())]);
  if (!known.has(ANTHROPIC_API_KEY_ENV)) {
    throw new WizardError(
      "Refusing to point Jarvis at Anthropic without an Anthropic API key.",
      HINT,
      { fatal: true },
    );
  }
}

/**
 * Does this text CONFIGURE a forbidden provider (as opposed to mentioning it)?
 *
 * Shipped assets are scanned with this, so it has to separate configuration
 * from documentation. Tom's coding lane legitimately drives the `claude` CLI
 * under the owner's own subscription — first-party Claude Code use, which the
 * playbooks explicitly allow — so backtick code spans and prose about that
 * binary must not trip the guard, while `provider: claude` in any config, or a
 * quoted `claude-code`/`claude_oauth`/`claudecode` anywhere, must.
 */
export function configuresForbiddenProvider(text: string): boolean {
  const distinctive = FORBIDDEN_PROVIDER_ALIASES
    .filter((alias) => alias !== "claude")
    .map((alias) => alias.replace(/[-_]/g, "[-_]"))
    .join("|");
  // A provider assignment, whatever the alias.
  const assignment = new RegExp(
    `provider\\s*[:=]\\s*["']?(?:${FORBIDDEN_PROVIDER_ALIASES.map((a) => a.replace(/[-_]/g, "[-_]")).join("|")})(?=["'\\s,}\\]]|$)`,
    "i",
  );
  // Or one of the unambiguous aliases in string quotes (never backticks, which
  // are markdown code spans around CLI names).
  const quoted = new RegExp(`["'](?:${distinctive})["']`, "i");
  return assignment.test(text) || quoted.test(text);
}
