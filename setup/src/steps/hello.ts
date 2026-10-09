import path from "node:path";
import { type StepOutcome, type WizardContext } from "../context.js";
import { readEnvFile } from "../env-file.js";
import { probeGateway } from "../gateway-probe.js";
import { slackApi } from "../slack-api.js";
import { officeGreeting } from "../ui.js";

export async function hello(ctx: WizardContext): Promise<StepOutcome> {
  const notes: string[] = [];
  const env = await readEnvFile(path.join(ctx.profileDir, ".env"));
  const botToken = env.SLACK_BOT_TOKEN;
  const homeChannel = env.SLACK_HOME_CHANNEL ?? ctx.state.slackHomeChannelId;
  const greeting = officeGreeting();

  if (ctx.dryRun) {
    notes.push(`[dry-run] would ${botToken !== undefined && homeChannel !== undefined ? "post the greeting to #jarvis-office" : "print the greeting in the terminal"}.`);
    notes.push(greeting);
    return { status: "done", notes };
  }

  if (botToken !== undefined && homeChannel !== undefined) {
    const response = await slackApi("chat.postMessage", botToken, {
      channel: homeChannel,
      text: greeting,
    });
    if (response.ok) {
      notes.push("Greeting posted to #jarvis-office.");
    } else {
      notes.push(`Slack greeting was not posted: ${response.error ?? "unknown_error"}.`);
      notes.push(greeting);
    }
  } else {
    notes.push(greeting);
  }

  // The greeting alone used to declare "the office is open" even when the
  // gateway was answering the port but rejecting the current key — the exact
  // shape that leaves a customer with a silently broken HUD chat. A health-only
  // check keeps the step non-blocking but stops it from greenwashing that state.
  const reach = await probeGateway(ctx.gateway?.connection());
  // Emitted through io.warn, not as a done-note: the GUI renderer only shows
  // notes for non-"done" steps, so a note here would be invisible on a normal
  // (successful greeting) run — exactly when the customer needs the heads-up.
  if (reach === "auth-rejected") {
    ctx.io.warn("The JARVIS engine is answering on its port but rejecting this session's key, so HUD chat will show an authentication error. Run the Start Engine step again to fix it.");
  } else if (reach === "unreachable") {
    ctx.io.warn("The JARVIS engine did not answer on its port; if chat does not respond after setup, run the Start Engine step again.");
  }

  return { status: "done", notes, props: { gateway_reachable: reach } };
}

