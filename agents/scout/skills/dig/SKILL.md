---
name: dig
description: "Run Scout's on-demand time-boxed investigation when the owner asks to look into a topic, with evidence cited and delivery in-thread."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [research, investigation, evidence, market, thread]
    related_skills: [weekly-sweep]
---

# Dig

Use this skill when the owner asks Scout to "look into X", "dig into X", "what is going on with X", or otherwise requests an on-demand investigation.

## Time box

State the box before you start: `I will spend 20 minutes / 10k tokens on this and report back in-thread with citations.`

Stop when either limit is reached. If the question remains unresolved, say what is known, what is not known, and exactly what would be needed next. Do not disappear into a tasteful research hole.

## Procedure

1. Quote the owner's request verbatim in your notes. If the investigation becomes a handoff, use that exact text in a `> handoff` block.
2. Read `knowledge/scout/questions.md` and `knowledge/scout/sources.md` for standing context.
3. Gather evidence from the most direct available sources first: official docs, product pages, customer comments, reviews, public posts, filings, changelogs, release notes, or transcripts.
4. Use web tools available in the session. If X or Reddit tools are available and relevant, search them; if not, skip them silently.
5. Keep a running split between facts, quotes, and inference. Anything that cannot be cited stays out of the final answer.
6. Append a dated note to `workspace/reports/scout-YYYY-MM-DD.md` with the topic, sources checked, and final answer link or text.

## Output format

Deliver in-thread, concise enough to be read on Slack:

```markdown
Scout dig: <topic>

Time box: 20 minutes / 10k tokens.

Short answer: <one or two sentences, with the confidence level if useful>.

Evidence:
- "<short quote>" — <source name>, <date if visible>, <link>
- <specific paraphrase tied to a page or post> — <source name>, <link>
- "<short quote>" — <source name>, <link>

What I infer:
- <one evidence-backed inference>
- <one caveat or contradiction>

Suggested owner action:
<one practical next step, usually a handoff to Ava for hooks, Bobby for ads, Tom for product proof, or Sarah if the finding touches support language.>
```

## Filled example

```markdown
Scout dig: Why are buyers hesitating on self-hosted agents?

Time box: 20 minutes / 10k tokens.

Short answer: The hesitation is not "self-hosted" itself; it is the owner's fear that setup, secrets, and approval boundaries will become their second job. Confidence: medium, because the strongest evidence is comment language rather than purchase interviews.

Evidence:
- "Where do the keys live, and can I rotate them without breaking the staff?" — YouTube comment on owner channel, https://www.youtube.com/watch?v=demo-trust&lc=demo-c6
- Competitor FAQ promises private deployment but does not show the approval model or env-var boundary — Competitor FAQ, https://example.com/competitor/faq
- "Autonomous" appears repeatedly in competitor copy, while "approval" appears only in docs — Competitor landing page, https://example.com/competitor/agents

What I infer:
- The trust message should lead with control and visibility before capability.
- "Private repo" is useful proof, but it does not by itself answer the setup anxiety.

Suggested owner action:
Hand to Ava for a hook set around "AI staff you can inspect before you trust", and ask Tom for a one-screen install boundary diagram.
```

## Handoffs

If the owner wants the finding handed to another role, post in that role's channel via `post-as` and start with:

```markdown
> handoff
> <originating owner request, verbatim>
```

Then add Scout's cited evidence beneath it. Never paraphrase the originating request, however tempting the polishing cloth may be.
