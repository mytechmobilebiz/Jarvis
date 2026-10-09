---
name: post-as
description: "Post to a staff role's Slack channel as the JARVIS bot via hud_slack_post, with a role prefix and owner approval."
version: 1.1.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [slack, persona, role, staff, post]
    related_skills: [morning-brief]
---

# Post as a staff role

When work belongs to a staff role (support report → Sarah, PR summary → Tom, ads table → Bobby, research → Scout, content → Ava), post it in that role's channel **with that role's prefix**, so the office reads like an office.

## How

Use `hud_slack_post` with `channel` (a channel ID or `#name`) and `text`. The owner approves each post in the HUD. A refusal means they declined — do not retry; say so.

The Slack token is never in the terminal environment. Do not curl Slack.

The bot posts under its own name: no username/avatar override (the app lacks `chat:write.customize`). Prefix staff-role messages with `*<Role>:*`. The role → channel map lives in `knowledge/org.md` (the setup wizard fills the channel IDs).

Check the response `"ok": true`. On `not_in_channel`, tell the owner to invite the bot there (`/invite @<your app name>` in that channel). On `channel_not_found`, ask for the channel ID.

## Rules

- Post-as is presentation, not identity theater toward the owner: if the owner asks who did something, the answer is honest — the staff are roles you play.
- Handoffs between roles quote the originating request **verbatim**.
- Approval-gated actions are still approval-gated regardless of which role posts.
