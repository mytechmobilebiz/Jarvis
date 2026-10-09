# JARVIS

## Good evening, sir. Welcome to your new staff.

I am Jarvis, your self-hosted AI chief of staff. This package installs me — and five colleagues — on your own machine, running on an AI account you own. No subscription, no third-party server, nothing leaves the house without your say-so. Under the hood I am a private distribution of [Hermes Agent](https://github.com/NousResearch/hermes-agent) (MIT), pinned to release `v2026.7.1` so an upstream change can never rearrange the furniture overnight.

## What you've acquired

| Agent | Office | What they do |
| --- | --- | --- |
| **Jarvis** | Chief of staff | Runs the office, composes the morning brief, delegates, guards your money |
| **Sarah** | Support | Triages the inbox, drafts replies, tracks support metrics |
| **Tom** | Developer | Plans before coding, fixes bugs in your repos, runs the tests |
| **Bobby** | Media buyer | Reads ad performance, reports ROAS, proposes budget moves (never spends unasked) |
| **Scout** | Research | Sweeps your market weekly, watches sources, answers "what changed?" |
| **Ava** | Content (bonus) | Writes hooks, drafts posts, keeps the cadence |

Plus the **JARVIS HUD** — your desktop command center: the arc reactor, live agent tiles, an activity feed, and a chat pane wired straight to Jarvis. Click any member of staff to review their full session history and read back every transcript, word for word. On first launch it handles your onboarding itself.

## What you need

- A machine that can stay on: a Mac, a native Windows machine, a Linux box, or a small VPS. Two-way voice ships for Mac and Windows; on Linux everything else runs natively.
- Nothing preinstalled, if you use the installer from your download page or the launcher in this folder. Only the developer path below needs **Node.js 22 or newer** and **pnpm**.
- **A way for me to think.** Easiest: sign in with a ChatGPT or Claude subscription you already pay for, and there is nothing to find or paste. Otherwise an API key of your own — Anthropic, OpenRouter, or OpenAI — at single-digit dollars per day of normal use. One or the other is the only hard requirement.
- Optional: a free Slack workspace (command your staff from your phone, approve actions with buttons).
- Nothing extra for voice: the speech engines and their models ship inside the application.

Everything else — Stripe, Meta ads, email, RevenueCat, Postiz — is optional and added later via the playbooks.

## Accounts and keys

Create these before running the wizard (or skip the optional ones and add them any time — each has a step-by-step playbook in `playbooks/`).

New to API keys? Follow the 2-minute guides: https://jarvis-agent.tech/guides/api-keys

| Service | Powers | Needed? | Get your key | Recipe |
| --- | --- | --- | --- | --- |
| [Anthropic](https://platform.claude.com/settings/keys) | The staff's brain (recommended) | **One of these three** | **Settings → API keys** → Create key (`sk-ant-…`); add a billing method first — [2-minute guide](https://jarvis-agent.tech/guides/anthropic-api-key) | `playbooks/models.md` |
| [OpenRouter](https://openrouter.ai/keys) | Alternative brain, many models via one key | **One of these three** | **Keys** → Create API key — [2-minute guide](https://jarvis-agent.tech/guides/openrouter-api-key) | `playbooks/models.md` |
| [OpenAI](https://platform.openai.com/api-keys) | Alternative brain | **One of these three** | **API keys** → Create new secret key — [2-minute guide](https://jarvis-agent.tech/guides/openai-api-key) | `playbooks/models.md` |
| [Slack](https://api.slack.com/apps) | Command the staff from your phone, approval buttons | Optional | **Create an App → From an app manifest** — use `slack/jarvis-manifest.json` | `playbooks/slack.md` |
| [Gmail app password](https://myaccount.google.com/apppasswords) (or iCloud/Fastmail) | Sarah reads and drafts in your support mailbox | Optional | Requires 2-Step Verification first; 16-character app password | `playbooks/email.md` |
| [Stripe](https://dashboard.stripe.com/) | Real revenue in the morning brief | Optional | Developers → API keys → **Create restricted key** (read-only) | `playbooks/stripe.md` |
| [Meta for Developers](https://developers.facebook.com/apps) | Bobby reads your ad performance | Optional | Create App → Marketing API token; Ad Account ID from [Business settings](https://business.facebook.com/settings) | `playbooks/meta-ads.md` |
| [RevenueCat](https://app.revenuecat.com/) | Subscription metrics for app businesses | Optional | Project → API keys (secret, v2) + Project ID | `playbooks/revenuecat.md` |
| [Postiz](https://postiz.com/) | Ava schedules your posts | Optional | Free self-hosted (docker compose) or hosted from $23/mo; key: **Settings → Developers → Public API** | `playbooks/postiz.md` |
| [Claude Code](https://claude.ai/code) | Tom's coding hands — and one of the two subscription sign-in routes for the staff's brain | Optional | Your own Claude Code install and account | `playbooks/claude-code.md` |

A brain is the only thing that gates setup, whether that is a subscription sign-in or a provider key. Every other row can wait until the household is running.

Wondering whether signing in with your own plan is safe, or an assistant told you it is a "red flag"? Read `playbooks/trust.md` first (or paste it into ChatGPT or Claude): what runs where, what JARVIS never does with your login, and the API-key alternative.

## Quick start

**Mac or Windows:** use the installer from your download page. Nothing else to install, and no terminal.

**Windows, from this folder instead:** double-click `1-START-JARVIS-WINDOWS.bat`.

**Linux, servers, developers:** open a terminal in this folder and run `bash start.sh`. It provisions its own pinned Node and takes it from there.

If you would rather drive it yourself and already have Node 22 and pnpm:

```sh
# If pnpm is missing:
npm install --global pnpm

pnpm install --frozen-lockfile
pnpm run hud
```

On a machine with no JARVIS profile yet, the HUD opens in **first-run mode**: I walk you through ten steps in the HUD itself — machine check, Hermes (pinned), the `jarvis` profile, the staff and their schedules, how I think, starting the engine, voice, then building and installing JARVIS HUD as a real desktop app, a first hello, and a summary — showing every command and its output as it runs. (On a machine with an earlier JARVIS on it, an eleventh step tidies that up first.) When the last step finishes, the same window becomes the HUD. Plan on 10–30 minutes; the short path (model key) is nearer 10. Quit halfway and the next launch resumes where you left off. Slack is optional and lives in Settings → CONNECT SLACK.

Setup ends by building JARVIS HUD on your own machine and installing it — `/Applications` on macOS (or `~/Applications` if that is read-only), the taskbar and Start menu on Windows, your applications menu on Linux. From then on you double-click JARVIS HUD like any other app; the terminal is not needed again. Because the app is built here rather than downloaded, macOS opens it without a Gatekeeper warning.

On a headless box (a VPS over SSH), or if you simply prefer the terminal, the same steps run as a text wizard — press **Enter** to run each, **S** to skip:

```sh
pnpm -r build
pnpm run wizard
pnpm run hud   # later, on a machine with a display
```

Prefer to see every command before it runs? Both setups print each command live, and `SETUP.md` is the complete manual mirror.

## After the wizard: personalize the household

The staff arrive knowing how to work, not who they work for. Before enabling schedules, spend ten minutes editing `~/.hermes/profiles/jarvis/knowledge/` (Windows: `%LOCALAPPDATA%\hermes\profiles\jarvis\knowledge\`; if you set `HERMES_HOME` yourself, use `<your HERMES_HOME>/profiles/jarvis/knowledge/`):

| File | Tell it |
| --- | --- |
| `product.md` | What you sell, pricing, links |
| `targets.md` | Your real revenue target and current MRR |
| `sarah/faq.md`, `sarah/refund-policy.md` | Your actual support answers and policy |
| `bobby/account.md`, `bobby/limits.md` | Your ad account and hard spend limits |
| `scout/sources.md` | The competitors and channels worth watching |
| `tom/repos.md` | The repositories Tom may touch |
| `ava/style.md`, `ava/cadence.md` | Your voice and posting rhythm |

Scheduled jobs (morning brief, weekly sweeps) ship **disabled**, and setup asks you about each one in plain language as it composes the staff — so if you said no then, or skipped ahead, that is why nothing is running. To change your mind later: `hermes -p jarvis cron list`, then enable the ones you want. The morning brief arrives at 08:00.

## The playbooks

Each optional integration is a 10–20 minute recipe in `playbooks/`:

| Playbook | Gives you |
| --- | --- |
| `slack.md` | The one Slack app — command your staff from anywhere, approval buttons included |
| `models.md` | Provider keys, model choice, and the important warning about consumer-subscription ToS |
| `email.md` | Sarah reads and drafts in a real mailbox (IMAP app password, no OAuth review) |
| `stripe.md` | Real revenue numbers in the brief (restricted read-only key) |
| `meta-ads.md` | Bobby reads your actual ad performance |
| `revenuecat.md` | Subscription metrics for app businesses |
| `postiz.md` | Ava schedules posts — free, self-hosted, native MCP |
| `claude-code.md` | Tom drives Claude Code on your repos |
| `voice-fast.md` | Sub-second local voice with NVIDIA Parakeet |

## Demo mode

Want to preview the HUD with sample data? Flip **DEMO MODE** in the HUD settings (gear icon). It seeds realistic business data (`demo/`) so you can see the dashboard, the brief format, and the office layout — clearly badged as sample data, and cleared the moment you switch it off. Setup itself never enables demo mode: Jarvis needs a real brain to run, and the wizard walks you through it.

## Setup telemetry

The setup wizard reports anonymous usage telemetry so we can fix the steps
people actually get stuck on: a random installation ID minted on your machine,
your OS and app version, which wizard steps finished or failed, and which AI
provider you picked. Never your keys, your name, or any content — and never
linked to your purchase. The wizard says so on screen; opt out anytime by
setting `JARVIS_NO_TELEMETRY=1` in your environment or in the profile `.env`.
Everything Jarvis does *after* setup stays on your machine, as promised.

## When something goes wrong

- **Setup hit a snag** (HUD or wizard) → every step is mirrored manually in `SETUP.md` with a Verify line after each command.
- **Want setup again?** → relaunching resumes an unfinished run automatically; to revisit a finished one, `pnpm run hud -- --setup` reopens first-run mode with completed steps offered as already done.
- **No JARVIS HUD app icon** → `pnpm run install-app` builds and installs the desktop app on its own, any time.
- **Slack ignores you** → confirm `SLACK_ALLOWED_USERS` in `~/.hermes/profiles/jarvis/.env` (Windows: `%LOCALAPPDATA%\hermes\profiles\jarvis\.env`; if you set `HERMES_HOME` yourself, use `<your HERMES_HOME>/profiles/jarvis/.env`) contains your Member ID (Slack profile → ⋯ → Copy member ID).
- **HUD chat says 401** → the app supplies the engine's key and port itself, at launch. If an earlier JARVIS left `API_SERVER_KEY`, `API_SERVER_PORT`, `API_SERVER_HOST` or `API_SERVER_ENABLED` in the profile `.env`, those override what the app supplies and chat will refuse. Delete those lines (your provider keys and everything else stay), then restart JARVIS. Do not add them back by hand.
- **Nothing at 08:00** → most often JARVIS was closed, and the engine runs only while the app does. Otherwise cron ships disabled by design; see "After the wizard" above.
- Anything else: `SETUP.md` troubleshooting section, or write in — **jarvis.agent.tech@pm.me**. Include the wizard's summary output.

## Updates

Lifetime updates are part of the purchase. Installing an update never touches your `.env`, memories, or edited knowledge files — that is the point of profile distributions.

## Repo map

| Path | Purpose |
| --- | --- |
| `setup/` | The TypeScript setup wizard |
| `hud/` | The Electron HUD |
| `agents/` | Profile distributions for the staff |
| `slack/` | The Jarvis Slack app manifest |
| `playbooks/` | Integration recipes |
| `demo/` | Zero-key demo data |
| `examples/sample-app/` | A small Express app with a seeded bug, for Tom's first ticket |

## License

Proprietary — licensed to you, the purchaser, personally: use it in any business you own and operate, across as many instances as you like. What the license does not cover is redistribution — reselling the code, sharing copies, or deploying it for companies you don't own requires a separate agreement. Hermes Agent itself remains MIT-licensed by Nous Research.

I remain, as ever, ready to put the household in order.
