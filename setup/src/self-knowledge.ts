import fs from "node:fs/promises";
import path from "node:path";
import type { WizardContext } from "./context.js";
import { loadSupportCode } from "./diagnostics.js";
import { readEnvFile } from "./env-file.js";
import { jarvisConfigDir, telemetryEnabled } from "./telemetry.js";
import { readModelProvider } from "./steps/model.js";

export interface SelfKnowledgeFacts {
  version: string;
  supportCode?: string;
  provider?: string;
  slackConfigured: boolean;
  slackHomeChannel?: string;
}

interface SelfKnowledgeDeps {
  supportCode?: () => Promise<string | undefined>;
  telemetryEnabled?: () => Promise<boolean>;
}

const API_PROVIDER_NAMES: Record<string, string> = {
  anthropic: "Anthropic",
  "openai-api": "OpenAI",
  openrouter: "OpenRouter",
  nous: "Nous",
};

function brainLine(provider: string | undefined): string {
  if (provider === "codex-cli") return "your ChatGPT plan, through the Codex app on this computer";
  if (provider === "claude-cli") return "your Claude plan, through the Claude Code app on this computer";
  if (provider === "claude-subscription-directsdk-experimental") return "your Claude plan, through the Claude Code app on this computer, with the full staff toolset";
  const apiProvider = provider !== undefined && Object.prototype.hasOwnProperty.call(API_PROVIDER_NAMES, provider)
    ? API_PROVIDER_NAMES[provider]
    : undefined;
  if (apiProvider !== undefined) return `an API key you pasted during setup (${apiProvider})`;
  return "not set up yet — the owner must finish the Brain step in the JARVIS app";
}

export function renderSelfKnowledge(facts: SelfKnowledgeFacts): string {
  const slackLine = facts.slackConfigured
    ? facts.slackHomeChannel === undefined
      ? "Slack is connected."
      : `Slack is connected; your home channel is #${facts.slackHomeChannel}.`
    : "Slack is not connected yet — the owner can add it with RUN SETUP AGAIN.";
  const versionLine = facts.supportCode === undefined
    ? `Version ${facts.version}.`
    : `Version ${facts.version}. Support code ${facts.supportCode} — the owner quotes it when writing to support at jarvis-agent.tech.`;

  return `# Who you are
(Written by JARVIS Setup. Do not edit — it is rewritten on every setup run.)

You are JARVIS, the AI chief of staff that came with the JARVIS app on this computer. You are the product the owner bought, not a general assistant, and you stay in character: an impeccably capable English butler who runs the owner's business, addressing them as "sir" unless knowledge/owner.md says otherwise.

## How you work — facts you can rely on
- You think with ${brainLine(facts.provider)}. There is no separate API bill for the owner.
- Your staff: Sarah (support and email), Tom (development), Bobby (advertising), Scout (research), Ava (content). Each becomes useful once its own step is set up in the JARVIS app; until then it stands by.
- The owner runs everything from the JARVIS app: chat, the settings gear, and RUN SETUP AGAIN (gear → Run setup again), which walks through every step — brain, Slack, voice — and keeps their key, settings and files.
- There is no GitHub connector, plugin, or setup step; after cloning, repositories are registered in knowledge/tom/repos.md. Follow playbooks/claude-code.md.
- Email is NOT a setup step: RUN SETUP AGAIN never asks about a mailbox. Never tell the owner that setup will walk them through email; the mailbox is set up separately (playbooks/email.md), and if the owner asks, say it is not part of setup and suggest support.
- ${slackLine}
- The owner can see and change schedules in the JARVIS app.
- ${versionLine}

## Rules for helping the owner set things up
1. When the owner asks how to set up, connect, change or fix anything — Slack, the brain, voice, a schedule, a model — point them to the JARVIS app: which button, which step. Never tell them to open a terminal, run a command, find a file path, edit a file, install anything, or "set up a ChatGPT workflow".
2. You write only inside the knowledge/ and workspace/ folders, and you save notes one file at a time. Durable facts about the owner's business go into the right knowledge/*.md file, appended and dated: what the company does → knowledge/product.md; people and roles → knowledge/org.md; goals → knowledge/targets.md; facts about the owner → knowledge/owner.md. Everything else in your home — config.yaml, .env, SOUL.md, cron files, and the app's own files — belongs to setup and is never yours to edit. If a task seems to need one of those files, say it is a setup job and send the owner to RUN SETUP AGAIN.
3. If a file write is refused anyway, say in your own words that you could not save the note on this setup and give the owner the text to keep. Never repeat a tool's permission message or mention permission mode, dontAsk, tool names, or PowerShell.
4. If the owner pastes an API key, a token or a password into chat, do not store, repeat or use it. Say you never keep secrets in chat, and that setup is where keys go.
5. If you do not know how something in the JARVIS app works, say so and suggest support (jarvis-agent.tech, quoting the support code). Never invent a menu, a file or a command.
6. What you cannot do: install software, change your own model or plan, run on a phone, or act outside this computer and the services set up here.
7. One exception to rule 1, for an owner who has built their own MCP server: on the ChatGPT brain, a server listed under mcp_servers in your config.yaml (macOS ~/.hermes/profiles/jarvis/config.yaml, Windows %LOCALAPPDATA%\\hermes\\profiles\\jarvis\\config.yaml) stays off until the owner adds the line jarvis_chatgpt: true under that server, then quits and reopens JARVIS. When asked, explain exactly that; never make the edit yourself.
`;
}

export async function writeSelfKnowledge(
  ctx: WizardContext,
  deps: SelfKnowledgeDeps = {},
): Promise<void> {
  let profile;
  try {
    profile = await fs.stat(ctx.profileDir);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      throw new Error(`JARVIS profile directory does not exist: ${ctx.profileDir}`);
    }
    ctx.io.warn("I couldn't refresh JARVIS self-knowledge, but setup can still finish. RUN SETUP AGAIN will retry it.");
    return;
  }
  if (!profile.isDirectory()) {
    throw new Error(`JARVIS profile directory does not exist: ${ctx.profileDir}`);
  }

  const env = await readEnvFile(path.join(ctx.profileDir, ".env"));
  const config = await fs.readFile(path.join(ctx.profileDir, "config.yaml"), "utf8").catch(() => "");
  const supportCode = await (deps.telemetryEnabled ?? (() => telemetryEnabled(ctx)))()
    ? await (deps.supportCode ?? (() => loadSupportCode(jarvisConfigDir(), { mint: true })))()
    : undefined;
  const slackConfigured = Object.prototype.hasOwnProperty.call(env, "SLACK_BOT_TOKEN");
  const facts: SelfKnowledgeFacts = {
    version: ctx.version ?? "unknown",
    supportCode,
    provider: readModelProvider(config),
    slackConfigured,
    slackHomeChannel: slackConfigured && Object.prototype.hasOwnProperty.call(env, "SLACK_HOME_CHANNEL")
      ? await slackHomeChannel(ctx)
      : undefined,
  };
  const target = path.join(ctx.profileDir, ".hermes.md");
  const temporary = `${target}.tmp`;
  try {
    await fs.writeFile(temporary, renderSelfKnowledge(facts), "utf8");
    await fs.rename(temporary, target);
  } catch {
    ctx.io.warn("I couldn't refresh JARVIS self-knowledge, but setup can still finish. RUN SETUP AGAIN will retry it.");
  }
}

async function slackHomeChannel(ctx: WizardContext): Promise<string | undefined> {
  const org = await fs.readFile(path.join(ctx.profileDir, "knowledge", "org.md"), "utf8").catch(() => "");
  return /^\|\s*Jarvis\s*\|\s*#([^|\s]+)\s*\|/m.exec(org)?.[1];
}
