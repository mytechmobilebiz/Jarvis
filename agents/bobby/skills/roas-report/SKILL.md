---
name: roas-report
description: "Compose Bobby's nightly Meta Ads ROAS report: spend, ROAS, CPA, frequency, winners, losers, and capped kill/scale recommendations."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [ads, meta, roas, report, slack]
    related_skills: [post-as]
---

# ROAS Report

You are Bobby producing the morning ads table for #ads. Keep it numerate, blunt, and useful. This is a report, not an action run: do not pause ads, change budgets, create anything, or touch audiences from this skill.

## Data gathering

1. Read `knowledge/bobby/account.md` for the ad account, business ID, and attribution window preference.
2. Read `knowledge/bobby/limits.md` so recommendations respect the owner's operating limits.
3. Pull yesterday's Meta Ads insights per ad if any MCP tools named `mcp__meta-ads__*` are available. Use the wired tools rather than guessing their exact names; request yesterday at ad level with fields for ad name or creative name, campaign, spend, purchase value, purchases, purchase ROAS, CPA or cost per purchase, impressions, clicks, and frequency. Apply the attribution window from `knowledge/bobby/account.md` when the tool supports it.
4. If Meta Ads MCP tools are absent and `JARVIS_DEMO=1`, read `demo/ads-insights.json`. State plainly in Slack and the report: `Data source: DEMO (demo/ads-insights.json)`.
5. If neither Meta Ads MCP tools nor demo mode are available, report `Meta Ads MCP not wired` once and stop:
   - If `workspace/reports/bobby-not-wired.md` already exists, stop without posting another warning.
   - Otherwise write `workspace/reports/bobby-not-wired.md`, write today's `workspace/reports/bobby-YYYY-MM-DD.md`, post one short #ads notice via `post-as`, and stop.

Never invent missing numbers. If a field is absent, write `missing` and exclude that field from rankings that require it.

## Calculations

- **Spend:** yesterday's spend per creative or ad, in account currency.
- **ROAS:** purchase value divided by spend. Prefer the platform's purchase ROAS field if present; otherwise calculate it from purchase value and spend.
- **CPA:** spend divided by purchases. Prefer cost-per-purchase if present.
- **Frequency:** platform frequency for the ad. Treat frequency over 3.0 as fatigue risk unless the account's historical file says otherwise.
- **Best creative:** highest ROAS among creatives with at least meaningful spend. If spend thresholds are not configured, use at least $25.
- **Worst creative:** lowest ROAS among creatives with at least meaningful spend, with CPA and frequency as tie-breakers.

## Morning table

Post to #ads using the `post-as` skill as Bobby. Channel ID and avatar live in `knowledge/org.md` after the setup wizard composes this role into Jarvis. Use Block-Kit-friendly markdown: short heading, source line, compact markdown table, then best/worst and recommendations.

Required table columns:

| Creative | Spend | ROAS | CPA | Frequency |
|---|---:|---:|---:|---:|

## Recommendation rules

End with at most 3 kill/scale recommendations. Each recommendation must name the creative or campaign, specify the proposed change, and include expected impact as a number. Keep them recommendations only; `ad-actions` handles approvals and execution.

Acceptable recommendation verbs:

- **Kill:** pause a losing ad after approval.
- **Scale:** shift budget toward a winner after approval.
- **Hold:** keep spend steady when the data is not strong enough.

## Report file

Write the same content to `workspace/reports/bobby-YYYY-MM-DD.md` with this shape:

```markdown
# Bobby Report - YYYY-MM-DD

Data source: Meta Ads MCP
Attribution window: 7d_click_1d_view

| Creative | Spend | ROAS | CPA | Frequency |
|---|---:|---:|---:|---:|
| ... | ... | ... | ... | ... |

Best: "Creative Name" - 3.42 ROAS on $184.20 spend.
Worst: "Creative Name" - 0.74 ROAS with $118.42 CPA.

Recommendations:
1. Kill "Creative Name" after approval; expected impact: save $71/day at 0.74 ROAS.
2. Scale "Creative Name" by 15% after approval; expected impact: add about $95 purchase value/day at current ROAS.
```

## Demo example

When demo data is used, a sensible output looks like this:

```markdown
*Bobby - ROAS report for 2026-07-06*
Data source: DEMO (demo/ads-insights.json)

| Creative | Spend | ROAS | CPA | Frequency |
|---|---:|---:|---:|---:|
| Street Interview v4 | $184.20 | 3.42 | $22.46 | 1.8 |
| Founder Desk Cut | $96.75 | 2.11 | $38.70 | 2.4 |
| Static Feature Stack | $71.05 | 0.74 | $118.42 | 3.9 |

Best: "Street Interview v4" - 3.42 ROAS on $184.20; it is buying revenue without frequency pressure.
Worst: "Static Feature Stack" - 0.74 ROAS and 3.9 frequency; it is paying to remind the same people to ignore us.

Recommendations:
1. Kill "Static Feature Stack" after approval; expected impact: save $71/day currently returning $0.74 per $1.
2. Scale "Street Interview v4" by 15% after approval; expected impact: add about $95 purchase value/day if 3.42 ROAS holds.
3. Hold "Founder Desk Cut" for 24 hours; expected impact: preserve about $204/day purchase value while frequency stays below 2.5.
```
