import path from "node:path";
import { type StepOutcome, type WizardContext } from "../context.js";
import { readEnvFile, upsertEnv } from "../env-file.js";
import { pathExists, readTextIfExists, writeTextIfChanged } from "../fs-utils.js";
import { restartGatewayForCredentialChange } from "../gateway-restart.js";
import type { SecretPromptGuide } from "../io.js";
import { listSlackChannels, slackApi, type SlackResponse } from "../slack-api.js";
import { openUrl, runCommand } from "../system.js";
import { FILE_EXPLORER_ADDRESS_BAR_HINT, confirm, note, promptSecret, promptText, warn } from "../ui.js";

// Manifest scopes used by channel setup, replies, and the bots.info identity check.
const REQUIRED_BOT_SCOPES = ["chat:write", "channels:manage", "channels:read", "groups:read", "users:read"];

const CHANNELS = ["jarvis-office", "support", "dev", "ads", "research", "content", "approvals"];

const ROLE_CHANNELS: Record<string, string> = {
  Jarvis: "jarvis-office",
  Sarah: "support",
  Tom: "dev",
  Bobby: "ads",
  Scout: "research",
  Ava: "content",
};

interface SlackOverrides {
  run?: typeof runCommand;
  platform?: NodeJS.Platform;
  api?: typeof slackApi;
  listChannels?: typeof listSlackChannels;
}

// Slack's app console lands on Basic Information, which shows five credentials
// — NONE of which is what these fields want (Peter reproduced the buyer's exact
// dead end, 2026-08-14). Each field carries its own where-to-find-it sentence.
// The app console URL is generic because the customer creates the app by
// pasting a manifest themselves, so the wizard never learns the app ID.
const SLACK_APPS_URL = "https://api.slack.com/apps";

export const BOT_TOKEN_GUIDE: SecretPromptGuide = {
  text: "In Slack's app settings: left sidebar → OAuth & Permissions → the green Bot User OAuth Token → Copy. If you see an Install to Workspace button there instead, click it first — the token appears after installing.",
  linkText: "api.slack.com/apps",
  url: SLACK_APPS_URL,
};

export const APP_TOKEN_GUIDE: SecretPromptGuide = {
  text: "On Basic Information, scroll down to App-Level Tokens, click the token named socket, and copy the value that starts with xapp-.",
  linkText: "api.slack.com/apps",
  url: SLACK_APPS_URL,
};

export const MEMBER_ID_GUIDE: SecretPromptGuide = {
  text: "In Slack itself (not the settings site): click your profile photo → Profile → the ⋮ More button → Copy member ID. It starts with U.",
};

export async function slack(ctx: WizardContext, overrides: SlackOverrides = {}): Promise<StepOutcome> {
  const notes: string[] = [];
  const api = overrides.api ?? slackApi;

  // Re-running setup is the persona's NORM (persona evidence: Ross, Justin C.,
  // Caleb all re-ran). A buyer who already did the hardest optional step —
  // creating a Slack app — must never be greeted with a cold "Configure Slack
  // now?" that implies starting over. Reuse also keeps re-installs on their
  // existing app generation instead of a freshly-created app with Slack's
  // unverified new agent runtime (support/BUGS.md, SLACK FOR NEW CUSTOMERS).
  const saved = await readSavedSlack(ctx);
  if (saved !== undefined) {
    if (ctx.dryRun) {
      note(ctx, "[dry-run] would check the saved Slack tokens with auth.test and offer to reuse them.");
    } else {
      const verdict = await checkSavedTokens(api, saved);
      if (verdict.status === "missing-scopes") return slackScopeFailure(notes, verdict.missing);
      if (verdict.status === "dead") {
        warn(ctx, "I found your Slack setup from last time, but Slack no longer accepts it — the app may have been deleted or its tokens revoked. Let's set it up fresh.");
        notes.push(`Saved Slack tokens failed auth.test (${verdict.error}); ran the normal walkthrough.`);
      } else if (verdict.status === "mismatch") {
        warn(ctx, "I found a Slack setup from last time, but its two tokens belong to two different Slack apps — messages would never arrive. Let's set it up fresh from one app.");
        notes.push("Saved Slack tokens belong to different apps; ran the normal walkthrough.");
      } else if (verdict.status === "unproven") {
        warn(ctx, "I found a Slack setup from last time, but Slack wouldn't confirm that both halves belong to the same app, so I can't promise it works. Let's set it up fresh.");
        // Plain sentence first — a summary line has to make sense to the
        // buyer on its own; Slack's own word for it trails as the detail
        // support needs when they read the same note in diagnostics.
        notes.push(`Couldn't confirm the saved Slack app, so setup asked for a fresh one. Slack replied: ${verdict.error ?? "no app id"}.`);
      } else {
        if (verdict.status === "unverified") {
          note(ctx, "I couldn't reach Slack to double-check your saved setup just now — if reusing it doesn't work, run this step again once you're back online.");
        }
        const workspace = verdict.status === "valid" ? verdict.workspace : undefined;
        const where = workspace === undefined ? "" : ` (workspace: ${workspace})`;
        const reuse = await confirm(ctx, `Found your Slack setup from last time${where}. Use it?`, true);
        if (reuse) {
          note(ctx, `Reconnecting${workspace === undefined ? "" : ` to ${workspace}`} — no new Slack app needed.`);
          notes.push(verdict.status === "valid"
            ? "Reused the saved Slack app; both tokens re-validated with auth.test."
            : "Reused the saved Slack app; Slack was unreachable for the pre-check.");
          await migrateAllowlistKeys(ctx, saved, notes);
          return finishWithBotToken(ctx, notes, saved.botToken, overrides);
        }
        // NO means NO: the customer gets the unchanged create-an-app flow.
      }
    }
  }

  warn(ctx, "Slack is optional, but skipping it removes the phone brief and remote approvals.");
  const configure = await confirm(ctx, "Configure Slack now?", false);
  if (!configure) {
    notes.push(SLACK_SKIP_NOTE);
    return { status: "skipped", notes };
  }

  const manifestPath = path.join(ctx.repoRoot, "slack", "jarvis-manifest.json");
  if (!(await pathExists(manifestPath))) {
    const missing = `The Slack manifest is missing at ${manifestPath}, so JARVIS cannot print the Slack walkthrough yet.`;
    warn(ctx, missing);
    if (/\\AppData\\/i.test(manifestPath)) {
      warn(ctx, FILE_EXPLORER_ADDRESS_BAR_HINT);
    }
    notes.push(missing);
    return { status: "skipped", notes };
  }

  printWalkthrough(ctx, manifestPath);
  await openUrl(ctx, SLACK_APPS_URL, { platform: overrides.platform, run: overrides.run });

  const botToken = await promptChecked(ctx, {
    kind: "bot-token",
    message: "Slack bot token xoxb-",
    guide: BOT_TOKEN_GUIDE,
    secret: true,
  });
  const appToken = botToken === "" ? "" : await promptChecked(ctx, {
    kind: "app-token",
    message: "Slack app token xapp-",
    guide: APP_TOKEN_GUIDE,
    secret: true,
  });
  const memberId = appToken === "" ? "" : await promptChecked(ctx, {
    kind: "member-id",
    message: "Your Slack member ID",
    guide: MEMBER_ID_GUIDE,
    secret: false,
  });

  if (botToken === "" || appToken === "" || memberId === "") {
    notes.push(SLACK_SKIP_NOTE);
    return { status: "skipped", notes };
  }

  await upsertEnv(
    ctx,
    path.join(ctx.profileDir, ".env"),
    {
      SLACK_APP_TOKEN: appToken,
      SLACK_ALLOWED_USERS: memberId,
      SLACK_BOT_TOKEN: botToken,
      SLACK_OWNER_MEMBER_ID: memberId,
    },
    { secretKeys: ["SLACK_APP_TOKEN", "SLACK_BOT_TOKEN"] },
  );
  notes.push("Slack tokens and owner member ID stored in the profile .env.");

  if (ctx.dryRun) {
    notes.push("[dry-run] would validate Slack auth, create office channels, and write channel IDs into knowledge/org.md.");
    notes.push(await restartGatewayForCredentialChange(ctx, overrides.run ?? runCommand));
    return { status: "done", notes };
  }

  const auth = await api("auth.test", botToken);
  let authFailureClass: string | undefined;
  if (auth.ok) {
    notes.push("Slack bot token validated with auth.test.");
    const app = await api("apps.connections.open", appToken);
    const missing = missingSlackScopes(auth, app);
    if (missing.length > 0) return slackScopeFailure(notes, missing);
  } else if (isTransportFailure(auth.error)) {
    notes.push(`Slack couldn't be reached to check the token (${transportKind(auth.error)}); the token was saved either way.`);
  } else {
    authFailureClass = "auth-test failed";
    notes.push(`Slack auth.test returned ${auth.error ?? "unknown_error"}; continue after checking the bot token.`);
  }

  return finishWithBotToken(ctx, notes, botToken, overrides, authFailureClass);
}

/**
 * Everything after a bot token is known-good enough to act on: create the
 * office channels (tolerant of ones that already exist), map them into
 * knowledge/org.md, set the home channel, and surface the tokens to a running
 * gateway. Shared by the fresh-paste path and the reuse path, so a re-run that
 * saved tokens but died before this point completes on reuse instead of
 * forcing the customer back through app creation.
 */
async function finishWithBotToken(
  ctx: WizardContext,
  notes: string[],
  botToken: string,
  overrides: SlackOverrides,
  authFailureClass?: string,
): Promise<StepOutcome> {
  const api = overrides.api ?? slackApi;
  const listChannels = overrides.listChannels ?? listSlackChannels;

  const createdIds: Record<string, string> = {};
  let printedScopeHint = false;
  let missingScope: string | undefined;
  let offline = false;
  let offlineKind = "";
  for (const channel of CHANNELS) {
    const response = await api("conversations.create", botToken, {
      name: channel,
      is_private: "false",
    });
    if (response.ok && response.channel?.id !== undefined) {
      createdIds[channel] = response.channel.id;
      notes.push(`#${channel} created.`);
      continue;
    }
    if (response.error === "name_taken") {
      notes.push(`#${channel} already exists.`);
      continue;
    }
    if (response.error === "missing_scope") {
      missingScope ??= response.needed;
      if (!printedScopeHint) {
        notes.push(slackScopeHint(response.needed));
        printedScopeHint = true;
      }
      continue;
    }
    if (isTransportFailure(response.error)) {
      // Every remaining call would only wait out its own 10-second timeout,
      // and the step would look hung for a minute and a half.
      offline = true;
      // The socket's own words ("getaddrinfo ENOTFOUND slack.com") are not
      // customer copy, and notes are printed to the customer.
      offlineKind = transportKind(response.error);
      notes.push(`Stopped creating channels because Slack couldn't be reached (${offlineKind}).`);
      break;
    }
    notes.push(`#${channel} was not created: ${response.error ?? "unknown_error"}.`);
  }

  const listed = offline ? { ids: {} } : await listChannels(botToken);
  if (listed.error === "missing_scope") {
    missingScope ??= listed.needed;
    if (!printedScopeHint) {
      notes.push(slackScopeHint(listed.needed));
      printedScopeHint = true;
    }
  }
  const channelIds = { ...listed.ids, ...createdIds };
  ctx.state.configuredChannels = channelIds;

  const orgPath = path.join(ctx.profileDir, "knowledge", "org.md");
  const orgText = (await readTextIfExists(orgPath)) ?? (await readTextIfExists(path.join(ctx.repoRoot, "agents", "jarvis", "knowledge", "org.md"))) ?? "";
  const updatedOrg = updateOrgChannels(orgText, channelIds);
  await writeTextIfChanged(ctx, orgPath, updatedOrg, "Slack channel map");
  notes.push("knowledge/org.md channel table updated.");

  const homeChannel = channelIds["jarvis-office"];
  const props = {
    slack_outcome: printedScopeHint && homeChannel === undefined ? "missing-scope"
      : offline ? "offline"
      : Object.keys(createdIds).length > 0 ? "created"
      : homeChannel !== undefined ? "existing" : "unresolved",
    ...(missingScope === undefined ? {} : { slack_missing_scope: missingScope }),
  };
  if (homeChannel !== undefined) {
    await upsertEnv(ctx, path.join(ctx.profileDir, ".env"), { SLACK_HOME_CHANNEL: homeChannel });
    ctx.state.slackConfigured = true;
    ctx.state.slackHomeChannelId = homeChannel;
    notes.push("SLACK_HOME_CHANNEL set to the #jarvis-office channel ID.");
  } else {
    notes.push("SLACK_HOME_CHANNEL was not set because #jarvis-office could not be resolved.");
  }

  // A running gateway read its .env at startup; on a re-run where only this
  // step executes (model/service retained), nothing else would surface the
  // new Slack tokens until a reboot. Worth doing even on the unfinished path
  // below: the tokens ARE saved, and the gateway should hold the fresh ones.
  notes.push(await restartGatewayForCredentialChange(ctx, overrides.run ?? runCommand, {
    platform: overrides.platform,
  }));

  if (homeChannel === undefined) {
    // Slack was probably unreachable (or the app is missing a scope). Marking
    // this DONE would retain it forever with no home channel and no phone
    // brief; degraded keeps the run green — the product works without Slack —
    // while making this step run again next time so it can finish.
    warn(ctx, "Slack isn't finished: I couldn't find or create the #jarvis-office channel — Slack may have been unreachable. Your tokens are saved, and I'll finish this next time setup runs.");
    const failureClass = offline
      ? offlineKind
      : authFailureClass ?? "channel-resolve failed";
    return { status: "failed", degraded: true, notes, props: { ...props, slack_failure_class: failureClass } };
  }

  return { status: "done", notes, props };
}

interface SavedSlack {
  botToken: string;
  appToken: string;
  allowedUsers: string;
  ownerMemberId: string;
}

/**
 * A prior run's Slack credentials, if the profile has a workable set: both
 * tokens plus at least one identity key. A hand-edited profile may carry only
 * one of SLACK_ALLOWED_USERS / SLACK_OWNER_MEMBER_ID — still reusable, and
 * migrateAllowlistKeys fills in the missing one, because the gateway
 * default-denies everyone when SLACK_ALLOWED_USERS is absent.
 */
async function readSavedSlack(ctx: WizardContext): Promise<SavedSlack | undefined> {
  const env = await readEnvFile(path.join(ctx.profileDir, ".env"));
  const botToken = env.SLACK_BOT_TOKEN ?? "";
  const appToken = env.SLACK_APP_TOKEN ?? "";
  const allowedUsers = env.SLACK_ALLOWED_USERS ?? "";
  const ownerMemberId = env.SLACK_OWNER_MEMBER_ID ?? "";
  if (botToken === "" || appToken === "") return undefined;
  // Judged on what the values CONTAIN, not on whether the keys are set: a
  // hand-edited `SLACK_ALLOWED_USERS=" , "` is a set with nobody in it, and
  // reusing it would hand back an office where the gateway denies everyone.
  if (canonicalMemberIds(allowedUsers) === "" && canonicalMemberIds(ownerMemberId) === "") return undefined;
  return { botToken, appToken, allowedUsers, ownerMemberId };
}

/**
 * Slack's answers that mean THE TOKEN ITSELF is no good. Anything else
 * (timeouts, DNS failures, http_5xx, rate limits) says nothing about the
 * credential, and a customer must never be sent back through app creation
 * because their wifi blinked.
 */
const DEAD_TOKEN_ERRORS = new Set([
  "invalid_auth",
  "account_inactive",
  "token_revoked",
  "token_expired",
  "not_authed",
]);

type SavedVerdict =
  | { status: "missing-scopes"; missing: string[] }
  | { status: "valid"; workspace?: string }
  | { status: "dead"; error: string }
  /** Proven to be two different Slack apps. */
  | { status: "mismatch" }
  /** Slack answered, but not with enough to vouch for the pair. */
  | { status: "unproven"; error?: string }
  /** Slack could not be reached; the tokens are probably fine. */
  | { status: "unverified" };

/**
 * True when the failure says nothing about the credential: a DNS error, a
 * socket timeout, a proxy's HTML error page. Slack's OWN errors are lowercase
 * snake_case codes; our transport failures carry the socket's message text,
 * and `http_<status>` means a reply that was not even JSON.
 *
 * The distinction is load-bearing twice: a customer must never be sent back
 * through Slack app creation because their wifi blinked, and once the network
 * is gone every further call just burns its own 10-second timeout.
 */
export function isTransportFailure(error?: string): boolean {
  if (error === undefined) return false;
  if (error.startsWith("http_")) return true;
  return !/^[a-z0-9_]+$/.test(error);
}

/**
 * A CLOSED SET describing how the network failed, for notes the customer
 * reads and for the diagnostics they may paste into an email. The raw string
 * is a socket/proxy message — never customer copy, and never something to
 * forward verbatim.
 */
export function transportKind(error?: string): "timeout" | "no connection" | "server error" | "network error" {
  if (error === undefined) return "network error";
  if (error.startsWith("http_")) return "server error";
  if (/timed out|ETIMEDOUT/i.test(error)) return "timeout";
  if (/ENOTFOUND|EAI_AGAIN|ECONNREFUSED|ENETUNREACH|ECONNRESET/i.test(error)) return "no connection";
  return "network error";
}

/**
 * Both tokens matter: the bot token drives the API, the app-level token is
 * what Socket Mode connects with, and either can be revoked or rotated on its
 * own (playbooks/slack.md documents exactly that). The app-level token is
 * checked with the method Socket Mode actually uses — apps.connections.open
 * proves both the token and its connections:write scope. (auth.test also
 * accepts xapp tokens — verified live 2026-08-14 — but connections.open is
 * the exact capability the gateway needs; the unused WSS URL it mints
 * expires on its own.) A token without the scope can never serve Socket
 * Mode, so its missing scope is reported alongside the bot scopes.
 */
async function checkSavedTokens(api: typeof slackApi, saved: SavedSlack): Promise<SavedVerdict> {
  const bot = await api("auth.test", saved.botToken);
  if (!bot.ok && DEAD_TOKEN_ERRORS.has(bot.error ?? "")) {
    return { status: "dead", error: bot.error ?? "unknown_error" };
  }
  // Once the network is gone, every later call only burns its own timeout.
  if (isTransportFailure(bot.error)) return { status: "unverified" };
  // Slack refused, in its own words, for a reason that is not a dead token
  // (not_allowed_token_type, ratelimited, an unrecognised code). That is not
  // "Slack was unreachable" — the pair cannot be vouched for, and saying the
  // network was down would be a lie the customer can catch. Settled BEFORE
  // the next probe, so a later timeout cannot overwrite a verdict Slack has
  // already given (Codex review r6).
  if (!bot.ok) return { status: "unproven", error: bot.error ?? "unknown_error" };

  const app = await api("apps.connections.open", saved.appToken);
  if (!app.ok && DEAD_TOKEN_ERRORS.has(app.error ?? "")) {
    return { status: "dead", error: app.error ?? "unknown_error" };
  }
  const missing = missingSlackScopes(bot, app);
  if (missing.length > 0) return { status: "missing-scopes", missing };
  if (isTransportFailure(app.error)) return { status: "unverified" };
  if (!app.ok) return { status: "unproven", error: app.error ?? "unknown_error" };

  // Two valid tokens can belong to two DIFFERENT Slack apps (a half-finished
  // earlier attempt, a hand-edited .env). Everything downstream uses the bot
  // token, while Socket Mode connects with the app one — so the office would
  // look configured and no message would ever arrive. Both sides are asked
  // who they belong to.
  //
  // When an identity call cannot answer, the direction of the doubt matters:
  // a transport failure is fail-open-with-a-note (the tokens are probably
  // fine, the network is not), while anything Slack itself says — a missing
  // scope, a bot it cannot find — leaves a set we cannot vouch for, and the
  // buyer is better served re-creating one app than reusing a pair that may
  // never deliver a message.
  const appIdentity = await api("auth.test", saved.appToken);
  if (isTransportFailure(appIdentity.error)) return { status: "unverified" };
  if (!appIdentity.ok || appIdentity.app_id === undefined || bot.bot_id === undefined) {
    return { status: "unproven" };
  }

  const botIdentity = await api("bots.info", saved.botToken, { bot: bot.bot_id });
  if (isTransportFailure(botIdentity.error)) return { status: "unverified" };
  if (!botIdentity.ok || botIdentity.bot?.app_id === undefined) {
    return { status: "unproven", error: botIdentity.error ?? "no_app_id" };
  }

  if (appIdentity.app_id !== botIdentity.bot.app_id) return { status: "mismatch" };

  return { status: "valid", workspace: bot.team };
}

/** Slack sends uppercase member IDs and the gateway allowlist compares exactly. */
function canonicalMemberIds(value: string): string {
  return value
    .split(",")
    .map((entry) => entry.trim().toUpperCase())
    .filter((entry) => entry !== "")
    .join(",");
}

/**
 * A legacy or hand-edited profile can hold one identity key without the
 * other, or hold lowercase IDs. The gateway reads SLACK_ALLOWED_USERS,
 * compares case-sensitively, and default-denies the owner when the key is
 * missing — so reuse fills whichever side is empty from the one that exists
 * (owner = the first allowlisted user) and canonicalises both.
 */
async function migrateAllowlistKeys(ctx: WizardContext, saved: SavedSlack, notes: string[]): Promise<void> {
  const allowed = canonicalMemberIds(saved.allowedUsers);
  // The whole owner value seeds a missing allowlist — an owner key holding
  // several IDs must not lose everyone after the first — while the singular
  // owner key keeps only the first.
  const owners = canonicalMemberIds(saved.ownerMemberId);
  const nextAllowed = allowed === "" ? owners : allowed;
  const nextOwner = (owners === "" ? allowed : owners).split(",")[0] ?? "";

  const updates: Record<string, string> = {};
  if (nextAllowed !== saved.allowedUsers && nextAllowed !== "") updates.SLACK_ALLOWED_USERS = nextAllowed;
  if (nextOwner !== saved.ownerMemberId && nextOwner !== "") updates.SLACK_OWNER_MEMBER_ID = nextOwner;
  if (Object.keys(updates).length === 0) return;

  await upsertEnv(ctx, path.join(ctx.profileDir, ".env"), updates);
  notes.push("Repaired the saved Slack owner/allowlist keys so the gateway recognises you.");
}

export type SlackPasteKind = "bot-token" | "app-token" | "member-id";

/**
 * Plain-words feedback for the classic wrong-pastes, in place of a raw error
 * later. Never quotes the pasted value: a mistake here is usually some OTHER
 * credential, and it must not end up echoed to the screen or a log.
 */
export function slackPasteProblem(kind: SlackPasteKind, value: string): string | undefined {
  if (kind === "bot-token") {
    if (value.startsWith("xoxb-")) return undefined;
    if (value.startsWith("xapp-")) {
      return "That's the app-level token (it starts with xapp-). This field needs the BOT token — the green one on the OAuth & Permissions page, starting with xoxb-.";
    }
    if (value.startsWith("xoxp-")) {
      return "That's a user token (it starts with xoxp-). This field needs the BOT token just above it on OAuth & Permissions — it starts with xoxb-.";
    }
    return "That doesn't look like a bot token — it should start with xoxb-. It's on the OAuth & Permissions page; nothing on Basic Information is this token.";
  }
  if (kind === "app-token") {
    if (value.startsWith("xapp-")) return undefined;
    if (value.startsWith("xoxb-")) {
      return "That's the bot token again (it starts with xoxb-). This field needs the app-level token from Basic Information → App-Level Tokens — it starts with xapp-.";
    }
    return "That doesn't look like an app-level token — it should start with xapp-. On Basic Information, scroll down to App-Level Tokens and copy the socket token.";
  }
  if (/^[UW][A-Z0-9]{4,}$/i.test(value)) return undefined;
  if (value.startsWith("xoxb-") || value.startsWith("xapp-")) {
    return "That's one of the tokens from the previous fields. This one wants YOUR member ID from the Slack app: profile photo → Profile → ⋮ More → Copy member ID. It starts with U.";
  }
  if (value.startsWith("@")) {
    return "That looks like a username. The member ID is different — in Slack: profile photo → Profile → ⋮ More → Copy member ID. It starts with U.";
  }
  return "That doesn't look like a member ID — it starts with U (for example U0123ABCD). In Slack: profile photo → Profile → ⋮ More → Copy member ID.";
}

interface CheckedPromptSpec {
  kind: SlackPasteKind;
  message: string;
  guide: SecretPromptGuide;
  secret: boolean;
}

/**
 * Prompt with a where-to-find-it guide and a friendly format check. A wrong
 * paste gets a plain-words correction and another go; "Paste again? NO" accepts
 * the value as typed, because token formats drift and a valid future credential
 * must survive this gate (same doctrine as the model step's key handling).
 * An empty submission still means "skip Slack" — the GUI's secret prompts
 * surface that as an explicit SKIP button instead of a dead submit.
 */
async function promptChecked(ctx: WizardContext, spec: CheckedPromptSpec): Promise<string> {
  for (;;) {
    const raw = spec.secret
      ? await promptSecret(ctx, spec.message, spec.guide, { emptySubmitLabel: "SKIP SLACK FOR NOW" })
      : await promptText(ctx, spec.message, "", spec.guide);
    const value = raw.trim();
    if (value === "") return "";
    const problem = slackPasteProblem(spec.kind, value);
    // Slack member IDs are uppercase and the gateway allowlist compares them
    // exactly, so a lowercased paste is accepted but stored canonically.
    if (problem === undefined) return spec.kind === "member-id" ? value.toUpperCase() : value;
    warn(ctx, problem);
    const retry = await confirm(ctx, "Paste again?", true);
    if (!retry) return value;
  }
}

export function updateOrgChannels(orgText: string, channelIds: Record<string, string>): string {
  const lines = orgText.split(/\r?\n/).map((line) => {
    if (!line.startsWith("|")) return line;
    const cells = line.split("|").map((cell) => cell.trim());
    const role = cells[1];
    if (role === undefined || ROLE_CHANNELS[role] === undefined) return line;
    const channelId = channelIds[ROLE_CHANNELS[role]];
    if (channelId === undefined) return line;
    cells[3] = channelId;
    return `| ${cells[1]} | ${cells[2]} | ${cells[3]} | ${cells[4]} | ${cells[5]} |`;
  });

  const approvalsId = channelIds.approvals;
  let text = lines.join("\n").trimEnd();
  if (approvalsId !== undefined) {
    text = text.replace(
      /Approvals mirror: #approvals(?: \([^)]+\))?\./,
      `Approvals mirror: #approvals (${approvalsId}).`,
    );
  }
  return `${text}\n`;
}

const SLACK_SKIP_NOTE = "Slack skipped; no phone brief or remote approvals until playbooks/slack.md is completed.";

function printWalkthrough(ctx: WizardContext, manifestPath: string): void {
  note(ctx, "Slack walkthrough from playbooks/slack.md:");
  note(ctx, "1. Open https://api.slack.com/apps and create an app from a manifest.");
  note(ctx, `2. Paste the contents of ${manifestPath}.`);
  if (/\\AppData\\/i.test(manifestPath)) {
    note(ctx, FILE_EXPLORER_ADDRESS_BAR_HINT);
  }
  note(ctx, "3. Generate an app-level token with connections:write.");
  note(ctx, "4. Install the app to the workspace and copy the xoxb bot token.");
  note(ctx, "5. Copy your Slack member ID from your profile menu.");
  note(ctx, "6. Paste xoxb, xapp, and member ID here; setup will create the office channels.");
}

function missingSlackScopes(bot: SlackResponse, app: SlackResponse): string[] {
  const granted = bot.scopes;
  const missing = granted === undefined ? [] : REQUIRED_BOT_SCOPES.filter((scope) => !granted.includes(scope));
  // connections:write belongs to the xapp token, never to the bot's scope header.
  if (app.error === "missing_scope") missing.push("connections:write");
  return missing;
}

function slackScopeFailure(notes: string[], missing: string[]): StepOutcome {
  const needed = missing.join(",");
  notes.push(slackScopeHint(needed));
  return { status: "failed", degraded: true, notes, props: { slack_outcome: "missing-scope", slack_missing_scope: needed } };
}

function slackScopeHint(needed?: string): string {
  return `Slack is missing ${needed === undefined ? "a scope" : `scope ${needed}`}; add the scopes from playbooks/slack.md, reinstall the app, and rerun this step.`;
}

export async function loadSlackStateFromEnv(ctx: WizardContext): Promise<void> {
  const env = await readEnvFile(path.join(ctx.profileDir, ".env"));
  if (env.SLACK_BOT_TOKEN !== undefined && env.SLACK_HOME_CHANNEL !== undefined) {
    ctx.state.slackConfigured = true;
    ctx.state.slackHomeChannelId = env.SLACK_HOME_CHANNEL;
  }
}
