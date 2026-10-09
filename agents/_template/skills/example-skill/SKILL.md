---
name: example-skill
description: "<what this skill reliably produces, in one sentence>"
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [template, example]
    related_skills: []
---

# Example Skill

<!--
Rename this folder and metadata to the actual skill name.
Skills are procedures: what to read, what to decide, what to write, and when to ask.
-->

Use this skill when <trigger condition>. The output is <artifact/report/action>, written in <role voice>.

## Inputs

1. Read `<knowledge-file.md>` for durable facts.
2. Read `workspace/reports/<source>-*.md` if this skill depends on another role's latest work.
3. If the required input is absent, ask the owner for the missing brief. Do not invent the premise; that is how one acquires expensive nonsense.

## Procedure

1. Restate the useful part of the request in working notes.
2. Gather only the files and data needed for this task.
3. Produce the draft, report, or decision table.
4. Apply any approval gate before touching customers, money, infrastructure, or the public.

## Output

- Write the durable result to `workspace/reports/<role>-YYYY-MM-DD.md` unless the skill names a more specific file.
- Cite source files for claims.
- If posting to Slack, use the shared `post-as` skill and the channel ID from `knowledge/org.md`.

## Approval Gate

Ask before irreversible or public actions. Include the exact proposed action, the reason, and the files used to reach it.
