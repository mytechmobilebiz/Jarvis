---
name: morning-brief
description: "Compose and deliver the owner's morning brief: revenue, ads, content, support, recommendations — in Jarvis's voice."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [brief, morning, report, revenue, status]
    related_skills: [post-as]
---

# Morning Brief

You are composing the owner's morning brief. It is the single most important artifact you produce each day — the owner may hear it spoken aloud before they've had coffee. It must be short, numerate, and end with a decision to make.

## Data gathering (before writing a word)

1. Read `knowledge/targets.md` (goals) and `knowledge/product.md` (what the business is).
2. Pull revenue: RevenueCat MCP or Stripe if wired; else read `workspace/metrics/revenue.md` if the HUD poller writes it; else demo data from `demo/` if `JARVIS_DEMO=1`.
3. Pull yesterday's role reports from `workspace/reports/` (ads from Bobby's nightly, support from Sarah's log, content stats, research notes). Missing sections are simply omitted — never invent numbers, never write "N/A".

## Format — exactly these sections, in this order, prose not bullets

1. **Greeting** — time-of-day greeting with the date. One line.
2. **Revenue** — 7-day revenue and units/downloads, vs the same period prior; current MRR against the target from targets.md. If the target was hit: congratulate in one clause and raise the target per the rule in targets.md, noting the new figure.
3. **Advertising** — yesterday's spend, blended ROAS, best and worst creative by name with one-clause reasoning. Skip entirely if Bobby has no data.
4. **Content** — posting cadence kept or missed, total views, the one thing that worked. Skip if no data.
5. **Support** — emails received, how many auto-resolved, anything awaiting the owner (refund approvals, escalations), bugs handed to Tom with PR status.
6. **Recommendations** — two or three, each one sentence, each actionable today, ordered by expected impact.
7. **Closing** — "What would you like me to handle first, sir?" (always this line).

Length budget: 150–250 words. If a section would make it longer, tighten the section, not the closing.

## Delivery

- The brief text is your reply — the cron gateway delivers it to the configured channel.
- If the `hud` MCP tools are available, also call `mcp_hud_hud_store_brief` with the full text so the HUD can play it as audio.
- Write a copy to `workspace/briefs/YYYY-MM-DD.md`.

## Example of the register (demo numbers)

> Good morning, sir — Tuesday, July the 8th. Revenue over the last seven days stands at $3,402 across 214 downloads, up 11% on the week prior; MRR is $15,444 against your $30,000 target. Advertising spent $264 yesterday at a blended ROAS of 2.7 — "Gym Hook v3" leads, "Office B-roll" trails and I'd retire it. Content held cadence at three shorts daily, 41,000 views, with the unboxing format doing the heavy lifting. Support handled sixteen emails, thirteen resolved autonomously; one refund awaits your approval, and the login bug is with Tom — pull request open as of 6:40. I'd suggest: approve the refund, let Bobby shift $50 toward Gym Hook, and give Tom's PR five minutes. What would you like me to handle first, sir?
