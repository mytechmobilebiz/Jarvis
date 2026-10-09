import path from "node:path";
import { type StepOutcome, type WizardContext } from "../context.js";
import { readEnvFile } from "../env-file.js";
import { hermesRootPointerPath } from "../hermes-root-pointer.js";
import { isPlatformDefaultHermesRoot } from "../hermes-root.js";
import { writeSelfKnowledge } from "../self-knowledge.js";
import { withFileExplorerAddressBarHint } from "../ui.js";
import { NO_BRAIN_LINE } from "./model.js";

export async function summary(ctx: WizardContext): Promise<StepOutcome> {
  const rows = ctx.summary.map((step) => {
    const notes = step.notes.join(" ").replace(/\|/g, "/");
    return `| ${step.title} | ${step.status} | ${notes} |`;
  });
  ctx.io.block("Setup Summary", ["| Step | Result | Notes |", "|---|---|---|", ...rows]);
  // Paths are for the terminal (a technical, source-install audience) — the
  // GUI narration stays plain per the standing copy rule; the same detail is
  // recoverable from diagnostics when support needs it.
  if (ctx.io.inheritsStdio) {
    const hermesPaths = [`Hermes root: ${ctx.hermesRoot}`];
    if (!isPlatformDefaultHermesRoot(ctx.hermesRoot, process.env, { homeDir: ctx.homeDir })) {
      hermesPaths.push(`Hermes root pointer: ${hermesRootPointerPath(process.env, { homeDir: ctx.homeDir })}`);
    }
    ctx.io.block("Hermes Paths", withFileExplorerAddressBarHint(hermesPaths));
  }
  const env = await readEnvFile(path.join(ctx.profileDir, ".env"));
  const options = {
    apiServerConfigured: await apiServerConfigured(ctx),
    slackConfigured: Object.prototype.hasOwnProperty.call(env, "SLACK_BOT_TOKEN"),
  };
  ctx.io.block(
    "Next Actions",
    withFileExplorerAddressBarHint(nextActions(ctx, options).map((action) => `- ${action}`)),
  );

  if (!ctx.dryRun) await writeSelfKnowledge(ctx);

  return {
    status: "done",
    notes: ["Printed setup summary and next actions."],
  };
}

/**
 * "Chat will work" is only promised when this run actually verified the engine
 * through its port — the Start Engine step sets that flag, and nothing else
 * can, because the key and port exist only in the supervisor's memory.
 */
async function apiServerConfigured(ctx: WizardContext): Promise<boolean> {
  if (ctx.dryRun) return false;
  return ctx.state.gatewayIntegrityVerified === true;
}

export function nextActions(ctx: WizardContext, options: { apiServerConfigured: boolean; slackConfigured?: boolean }): string[] {
  const actions: string[] = [];

  if (ctx.io.inheritsStdio) {
    // Terminal runs are a technical, source-install audience: commands are the
    // honest instructions here, and there is no supervisor — the engine is
    // started by hand. The GUI narration below stays command-free.
    actions.push("Open the HUD from the repository root: pnpm run hud");
    actions.push('Use terminal chat any time: hermes -p jarvis chat -q "hello"');
    actions.push("Start the engine for a terminal session: hermes -p jarvis gateway run");
  } else if (options.apiServerConfigured) {
    actions.push("Close this window and the office opens by itself — nothing to connect, nothing to paste.");
  } else {
    actions.push("Chat needs the engine running: reopen JARVIS and it starts the engine itself.");
  }

  // Lane C (voice): the feature is invisible unless someone says how to use
  // it, and the honest scope has to travel with the instruction.
  const voiceStep = ctx.summary?.find((step) => step.id === "voice");
  if (voiceStep?.status === "done") {
    actions.push(
      "Talk to Jarvis in the HUD: hold the microphone in the chat bar (or hold Space) and speak — he answers aloud. "
      + "Speech recognition and speech generation run on this computer; your request still goes to the AI provider you selected.",
    );
  } else if (voiceStep?.status === "failed") {
    actions.push("Voice did not verify on this machine — typed chat is unaffected. Run setup again after allowing JARVIS through your antivirus.");
  }

  if (options.slackConfigured || ctx.state.slackConfigured) {
    actions.push("DM the Slack bot, then watch #jarvis-office for the office greeting and morning brief.");
  } else {
    actions.push(
      ctx.io.inheritsStdio
        ? "Connect Slack when ready: run `pnpm run wizard -- --slack` or see playbooks/slack.md"
        : "Connect Slack when you're ready: Settings → CONNECT SLACK.",
    );
  }

  if (!ctx.state.modelConfigured) {
    actions.push(NO_BRAIN_LINE);
  }

  if (ctx.io.inheritsStdio) {
    actions.push("For revenue, ads, content, and support keys, use playbooks/stripe.md, playbooks/revenuecat.md, playbooks/meta-ads.md, playbooks/postiz.md, and playbooks/email.md.");
  } else {
    actions.push("Want revenue, ads, or content numbers on the dashboard? Ask me in chat and I will walk you through connecting each one.");
  }

  return actions;
}
