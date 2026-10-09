---
name: ad-actions
description: "Execute Bobby's approved Meta Ads actions: pause_ad and shift_budget only, gated by limits, approval, and hard caps."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [ads, meta, approval, budget, safety]
    related_skills: [post-as, roas-report]
---

# Ad Actions

This skill is for narrow, gated execution after Bobby has a recommendation. It never creates campaigns, ad sets, ads, or audiences. It never edits audiences. It only supports `pause_ad` and `shift_budget`.

## First gate: limits

1. Read `knowledge/bobby/limits.md` before doing anything else.
2. Treat missing `bobby_actions` as `false`.
3. If `bobby_actions: false`, refuse politely, make no Meta call, and log the recommendation instead in `workspace/reports/bobby-actions.log.md`.
4. If `daily_spend_cap` is blank or unclear, refuse execution and log the recommendation. Paid media without a cap is theatre with invoices.
5. Use `max_shift_pct` from `knowledge/bobby/limits.md`, capped at 20 even if the file says more.

## Tool gate

If no MCP tools named `mcp__meta-ads__*` are available, do not act. Log `Meta Ads MCP not wired` with the requested action, object name, expected impact, and timestamp.

## Allowed actions

### pause_ad

Required inputs: ad ID, ad name, campaign name, current status, spend, ROAS, CPA, frequency, reason, expected impact.

Procedure:

1. Fetch current ad state from Meta Ads MCP and record it as `before`.
2. Post an approval request in #ads via `post-as` as Bobby.
3. Wait for explicit owner approval in the thread. Do not treat emoji, silence, or vague assent as approval.
4. If approved, pause only that existing ad.
5. Fetch current ad state again and record it as `after`.
6. Append the executed action to `workspace/reports/bobby-actions.log.md`.

### shift_budget

Required inputs: campaign ID, campaign name, current daily budget, proposed daily budget, direction, reason, expected impact.

Hard caps, regardless of approval:

- The total shift for a campaign on one calendar day must be within `max_shift_pct`, and never above 20%.
- The resulting daily spend across Bobby-managed campaigns must not exceed `daily_spend_cap`.
- Check today's entries in `workspace/reports/bobby-actions.log.md` before calculating remaining shift capacity.

Procedure:

1. Fetch current campaign budget from Meta Ads MCP and record it as `before`.
2. Calculate the maximum allowed shift for today. If the requested shift exceeds it, reduce the request to the cap or refuse if no capacity remains.
3. Confirm the proposed account daily spend remains at or below `daily_spend_cap`.
4. Post an approval request in #ads via `post-as` as Bobby.
5. Wait for explicit owner approval in the thread.
6. If approved, update only that existing campaign's daily budget.
7. Fetch campaign budget again and record it as `after`.
8. Append the executed action to `workspace/reports/bobby-actions.log.md`.

## Approval request format

```markdown
*Bobby approval request - <pause_ad|shift_budget>*
Object: <ad or campaign name> (`<id>`)
Reason: <one clause, with figures>
Before: <status or budget>
After proposed: <status or budget>
Caps checked: max shift <x>%, daily spend cap <amount>
Expected impact: <figure per day or per week>

Reply in thread with `APPROVE <action-id>` to execute.
```

## Log format

Append to `workspace/reports/bobby-actions.log.md` for every refusal, recommendation-only entry, approval request, and executed action.

```markdown
## YYYY-MM-DD HH:MM - <action-id>

- Action: pause_ad
- Status: recommendation_only | requested_approval | executed | refused | not_wired
- Object: <name> (`<id>`)
- Reason: <figures>
- Before: <status/budget>
- After: <status/budget or proposed state>
- Expected impact: <figure>
- Approval: <thread link or "not requested">
- Source: <report file or request>
```

If handing this to another role, include the originating request verbatim:

```markdown
> handoff
> <originating request exactly as received>
```
