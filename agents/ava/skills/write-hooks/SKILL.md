---
name: write-hooks
description: "Turn Scout's latest angles or an owner brief into short-form hooks, then report them to #content as Ava."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [content, hooks, scout, short-form, slack]
    related_skills: [post-as]
---

# Write Hooks

Use this skill when Ava needs fresh short-form hooks for the content queue. The output is a hook batch: five first-three-seconds lines per angle, each tagged with format and a one-clause reason grounded in evidence.

## Inputs

1. Prefer an owner brief in the current request.
2. If no brief is supplied, find Scout's newest angle report:

```bash
ls -t workspace/reports/scout-*.md 2>/dev/null | head -1
```

3. Read `knowledge/ava/style.md` for voice rules and banned phrases.
4. Read `knowledge/org.md` for Ava's #content channel ID. In standalone mode, ask the owner for the channel if the setup wizard has not filled it.
5. If there is no owner brief and no Scout report, ask for a brief. Do not invent angles.

## Procedure

1. Extract each angle, its source file, and the evidence that makes it worth testing.
2. Write exactly five hooks per angle. Each hook must be a first-three-seconds line, not a caption, thesis, or polite preamble.
3. Tag each hook with one format: `talking-head`, `b-roll`, `meme`, or `demo`.
4. Add `why it should work` as one clause, citing the angle's evidence and source file.
5. Cut weak lines. If two hooks make the same promise, keep the sharper one and replace the other.

## Report Format

Write `workspace/reports/ava-hooks-YYYY-MM-DD.md` with this structure:

```markdown
# Ava Hooks — YYYY-MM-DD

Source: <owner brief or workspace/reports/scout-YYYY-MM-DD.md>

## <Angle>

Evidence: <short cited evidence from the source file>

| # | Format | Hook | Why it should work |
|---|---|---|---|
| 1 | talking-head | <first-three-seconds line> | <one clause citing evidence> |
```

## Delivery

Post the batch to #content via the shared `post-as` skill, using username `Ava` and the channel ID from `knowledge/org.md`. If the batch is long, post a concise summary plus the report path. Check Slack returned `"ok": true`; if Slack is not configured, leave the report in `workspace/reports/` and say so plainly.
