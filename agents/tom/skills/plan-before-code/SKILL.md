---
name: plan-before-code
description: "Tom's intake gate: write and post a short engineering plan in #dev before any code is touched."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [dev, planning, handoff, slack, tom]
    related_skills: [post-as, coding-lane]
---

# Plan Before Code

Every development task starts here. Usually the task arrives as a Sarah handoff in `#dev`; sometimes it arrives from the owner directly. Either way, do not code first. Write the plan, post it, then decide whether to proceed.

## Inputs

1. Read the originating request. If it is a handoff, preserve the request exactly.
2. Read `knowledge/tom/repos.md` to identify the target repo, test command, and main branch.
3. Read `knowledge/tom/definition-of-done.md` so the plan is measured against the owner's bar.
4. Read the Tom row in `knowledge/org.md` from the active JARVIS profile for the `#dev` channel ID.

## Plan format

Post this in `#dev` as Tom via the `post-as` skill:

```markdown
> handoff
> <originating request verbatim, if this came from another role>

**What is broken:** <one or two plain-English sentences>
**Root-cause hypothesis:** <your best current theory, clearly labelled as a hypothesis>
**Files to touch:** <likely files or directories; say "to confirm" if unknown>
**Test to prove it:** <the command or regression test that must go green>
**Scope:** S/M/L - <why>
```

If the task came directly from the owner, omit the `> handoff` block unless you are passing it onward later.

## Scope

- **S:** one localised defect or doc change, expected under 45 minutes, low blast radius.
- **M:** multiple files, unclear root cause, data migration, dependency change, or visible product behaviour.
- **L:** architectural change, production/infrastructure involvement, broad refactor, customer data, or any risk the owner should explicitly accept.

## Gate

- For **S** tasks, proceed immediately after posting the plan.
- For **M** or **L** tasks, wait for owner go-ahead in the thread.
- If a task touches customers, money, infrastructure, the public, sends, deletions, or production, ask before acting even if the code change itself looks small.

Write the plan and any owner decision to `workspace/reports/tom-YYYY-MM-DD.md`. Never skip the plan. A skipped plan is not speed; it is debt arriving early.
