# Playbook: Connect Slack (one app, ~5 minutes)

Slack is Jarvis's command center — the morning brief on your phone, your staff working in channels, and approve/deny buttons wherever you are. It is optional: skip it and Jarvis remains fully at your service on this machine via the HUD chat pane and voice. You will need a Slack workspace where you are an admin; a free one from [slack.com/create](https://slack.com/create) does nicely.

## 1. Create the app from our manifest

1. Open [api.slack.com/apps](https://api.slack.com/apps) → **Create an App** → **From an app manifest**.
2. Select your workspace → **Next**.
3. Switch the editor to the **JSON** tab, delete what's there, and paste the entire contents of `slack/jarvis-manifest.json` from this repository → **Next** → **Create**.

The manifest carries everything: Socket Mode, every permission scope, event subscriptions, slash commands, and the Messages tab. No checkbox spelunking required.

The manifest's `https://hermes-agent.local/slack/commands` command URL is an intentional placeholder required by Slack's schema. Socket Mode does not use it, so do not create or expose a public endpoint for that address.

## 2. Generate the connection token (`xapp-…`)

On your new app's page: **Settings → Basic Information → App-Level Tokens** → **Generate Token and Scopes**. Name it anything (`socket` will do), click **Add Scope** and choose **`connections:write`**, then **Generate**. Copy the token beginning `xapp-`.

## 3. Install to your workspace (`xoxb-…`)

**Settings → Install App** → **Install to Workspace** → **Allow**. Copy the **Bot User OAuth Token** beginning `xoxb-`.

> If you ever change scopes or update the manifest later, this page shows a banner asking you to **reinstall** — the change does not take effect until you do.

## 4. Find your Member ID

In Slack, click your own avatar → **View full profile** → **⋮** → **Copy member ID** (looks like `U01AB2CDE`). Only IDs on the allowlist may command Jarvis; everyone else who DMs him is politely asked to pair.

## 5. Hand the three values to the wizard

The setup wizard (`pnpm run wizard` from the built repository root) asks for exactly these:

| Value | Where you got it |
|---|---|
| Bot token `xoxb-…` | Step 3 |
| App token `xapp-…` | Step 2 |
| Your Member ID `U…` | Step 4 |

The wizard validates both tokens, creates the office channels (#jarvis-office, #support, #dev, #ads, #research, #content, #approvals), and invites Jarvis in. He will greet you in #jarvis-office when the household is in order.

## Verify

- DM the bot: he replies without being mentioned.
- In a channel: `@Jarvis are you there?` — he answers in a thread.
- If he is silent in a channel, he probably was not invited: `/invite @Jarvis`.

## Rotation & housekeeping

Tokens can be revoked and regenerated any time on the app's **OAuth & Permissions** (bot token — requires reinstall) and **Basic Information → App-Level Tokens** (app token) pages; paste new values into the profile's `.env` and restart the gateway. Nothing about this app is public: Socket Mode means no URLs are exposed, and the app exists only in your workspace.
