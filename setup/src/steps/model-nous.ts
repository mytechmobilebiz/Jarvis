import type { StepOutcome, WizardContext } from "../context.js";
import { classifySignInFailure, parseDeviceCode, type SignInResult } from "../device-code.js";
import { hermesInvocation, openUrl as defaultOpenUrl } from "../system.js";
import { confirm, deviceCode, note, warn } from "../ui.js";
import {
  applyProviderConfig,
  markDirty,
  verifyAndFinish,
  type ModelRunners,
  type ModelSession,
} from "./model-config.js";

/**
 * The model Jarvis uses on a fresh Nous sign-in.
 *
 * Parity with the API-key path's Anthropic default, and present in the pinned
 * engine's curated Nous catalogue (hermes_cli/models.py:180-185). Routed BY
 * NOUS — provider `nous` sends it to inference-api.nousresearch.com, never to
 * api.anthropic.com, so no Anthropic credential is involved (see
 * adapter-guard.ts for why that distinction is load-bearing).
 *
 * Which models a given account may actually use depends on its Nous plan, and
 * the engine exposes no CLI to read that entitlement. So this is a starting
 * choice, not a promise: the verification round-trip is what decides, and a
 * refusal is reported in plain language instead of being papered over.
 */
export const NOUS_DEFAULT_MODEL = "anthropic/claude-sonnet-4.6";

/** Where a customer with no plan (or an exhausted one) has to go. */
export const NOUS_PORTAL_URL = "https://portal.nousresearch.com";

/**
 * How long one sign-in attempt may run before the child is killed.
 *
 * The engine's own device code expires well inside this, and a poller that
 * outlives the customer's patience is exactly the process that later persists
 * credentials for a setup nobody is watching any more.
 */
export const SIGN_IN_TIMEOUT_MS = 10 * 60 * 1000;

/**
 * Nous account sign-in — the default path, and the reason v1.4.0 exists.
 *
 * Everything here is driven through the engine's own device-code login
 * (`hermes auth add nous --type oauth`, hermes_cli/auth_commands.py:262-308 →
 * `_nous_device_code_login`, hermes_cli/auth.py:7669). JARVIS never sees a
 * token: the engine mints it, stores it in its own auth store, and uses the
 * access token directly as the inference bearer.
 */
export async function signInNous(
  ctx: WizardContext,
  runners: ModelRunners,
  session: ModelSession,
): Promise<StepOutcome> {
  const { notes } = session;
  const openUrl = runners.openUrl ?? defaultOpenUrl;
  let attempt = 0;

  while (true) {
    attempt += 1;
    note(ctx, "I will open the Nous sign-in page in your browser. Sign in there, and I will do the rest.");
    let announced = false;
    let transcript = "";

    // --no-browser: the engine would open the browser from its own process,
    // which a packaged GUI cannot rely on. We open it ourselves, after
    // showing the code, so the customer keeps both.
    const loginInvocation = hermesInvocation(
      ctx,
      ["-p", "jarvis", "auth", "add", "nous", "--type", "oauth", "--no-browser"],
      runners.platform,
    );
    const login = await runners.watch(
      ctx,
      loginInvocation.command,
      loginInvocation.args,
      {
        // Auth output never reaches the command log: it carries the code, the
        // URL that embeds it, and the account label the engine derives from
        // the token's email claim.
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
            message: "Sign in to your Nous account to give Jarvis a brain.",
            url: prompt.url,
            code: prompt.code,
          });
          void openUrl(ctx, prompt.url).catch(() => {
            warn(ctx, "I could not open your browser by myself — please open the address above yourself.");
          });
        },
      },
    );

    // The engine writes its auth store on success, so from here the running
    // gateway's view is stale whatever happens next.
    if (login.status === 0) markDirty(session);

    if (login.status === 0) {
      if (announced) {
        deviceCode(ctx, { attempt, status: "signed-in", message: "Signed in. That code is finished with." });
      }
      notes.push("Signed in to Nous; the engine holds the credential, not Jarvis.");
      break;
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
        message: `That code is no longer usable — ${failureLabel(failure)}.`,
      });
    }
    notes.push(`Nous sign-in did not complete (${failureLabel(failure)}).`);
    if (failure === "no-plan") {
      notes.push(`Your Nous account has no active plan for inference. Add one at ${NOUS_PORTAL_URL}/billing, then run this step again.`);
    }

    // A closed window is not a question to ask — there is nobody there.
    if (failure === "cancelled" && login.end === "aborted") {
      notes.push(offlineNote());
      return { status: "skipped", notes, props: { signin_method: "nous", signin_result: "cancelled" } };
    }

    // Truthful about what "no" does: this run ends without a provider. It does
    // not drop back to the menu, and promising otherwise would be a small lie
    // on the exact screen this release exists to make trustworthy.
    const again = await confirm(
      ctx,
      "Try the Nous sign-in again? Choose no to finish setup without a provider for now.",
      true,
    );
    if (!again) {
      notes.push(offlineNote());
      return { status: "skipped", notes, props: { signin_method: "nous", signin_result: failure } };
    }
  }

  ctx.state.providerChoice = "nous";
  await applyProviderConfig(ctx, runners.run, session, {
    provider: "nous",
    model: NOUS_DEFAULT_MODEL,
  });

  return verifyAndFinish(ctx, runners.run, {
    session,
    method: "nous",
    platform: runners.platform,
    remedy: `Check that your Nous account has an active plan at ${NOUS_PORTAL_URL}, then rerun this step.`,
  });
}

function failureLabel(failure: SignInResult): string {
  switch (failure) {
    case "cancelled":
      return "the sign-in was stopped";
    case "timeout":
      return "the sign-in page timed out";
    case "denied":
      return "the sign-in was refused";
    case "no-plan":
      return "the account has no active plan";
    case "network":
      return "your computer could not reach the sign-in service";
    default:
      return "the engine reported an error";
  }
}

export function offlineNote(): string {
  return "No model provider was set up, so Jarvis stays offline until one is added. "
    + "Nothing is lost: use RUN SETUP AGAIN in HUD settings (or rerun the wizard) and this step will offer the same "
    + "Brain choices again.";
}
