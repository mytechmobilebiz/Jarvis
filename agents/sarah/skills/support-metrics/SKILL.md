---
name: support-metrics
description: "Compile Sarah's daily support counts for Jarvis's morning brief."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [support, metrics, daily, report]
    related_skills: [triage-inbox]
---

# Support Metrics

Prepare Sarah's support numbers for the morning brief. Keep it short enough for Jarvis to read in one pass.

## Procedure

1. Read today's `workspace/reports/sarah-YYYY-MM-DD.md`. If it does not exist, use zeroes and say the inbox has not yet run today.
2. If yesterday's late support work matters to the morning brief, read the previous day's Sarah report as well and label it clearly.
3. Sum the daily counts:
   - received
   - auto-resolved
   - awaiting owner
   - escalated
   - spam archived
4. Note the open items that need the owner: refund approvals, bug handoffs, and feature requests that crossed the 3-user threshold.
5. Write `workspace/reports/sarah-daily.md`.

## Output format

```markdown
# Sarah daily support

- **Date:** YYYY-MM-DD
- **Received:** 0
- **Auto-resolved:** 0
- **Awaiting owner:** 0
- **Escalated:** 0
- **Spam archived:** 0

## Owner attention

- None.

## Notes for Jarvis

One sentence suitable for the morning brief.
```

Do not invent counts. If the underlying report is incomplete, say exactly what is missing and cite the file you used.
