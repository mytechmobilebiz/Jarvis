---
name: weekly-sweep
description: "Run Scout's weekly research sweep: YouTube comments, competitor pages, optional social search, and the evidence-cited '5 angles worth testing' report."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [research, sweep, youtube, competitors, angles]
    related_skills: [post-as, dig]
---

# Weekly Sweep

You are producing Scout's weekly market sweep: a short, cited report of angles worth testing. The job is not to sound clever. The job is to find what the market is already telling us and make it usable.

## Budget and outputs

- Cron budget: 25k tokens. If collection or synthesis would exceed it, stop with a partial report and say where the cap was hit.
- Report path: `workspace/reports/scout-YYYY-WW.md` using ISO week format.
- Slack delivery: post the finished report to Scout's `#research` channel via the shared `post-as` skill, reading the channel ID and avatar from `knowledge/org.md`.
- Hermes ships cron disabled-on-install; this skill may be present before any scheduler is active.

## Read first

1. `knowledge/scout/sources.md` for YouTube channels, competitor videos, competitor sites, and keywords.
2. `knowledge/scout/questions.md` for standing questions the owner wants watched.
3. `knowledge/org.md` for Scout's Slack channel and avatar.

Treat example rows, blank rows, and IDs or URLs containing `EXAMPLE` as unconfigured.

If zero real sources are configured, read `demo/comments.json`, produce the same report from that JARVIS_DEMO data, and start the source note with: `Demo data used because no Scout sources are configured.`

## Collect evidence

### 1. YouTube comments

Use YouTube Data API v3 only when `$YOUTUBE_API_KEY` is set. Never print the key. The `commentThreads.list` request is a `GET` to `https://www.googleapis.com/youtube/v3/commentThreads`; use `part=snippet,replies`, `maxResults=100`, `order=time`, and `textFormat=plainText`.

For the owner's channel comments:

```bash
curl -sG "https://www.googleapis.com/youtube/v3/commentThreads" \
  --data-urlencode "part=snippet,replies" \
  --data-urlencode "allThreadsRelatedToChannelId=${OWN_CHANNEL_ID}" \
  --data-urlencode "maxResults=100" \
  --data-urlencode "order=time" \
  --data-urlencode "textFormat=plainText" \
  --data-urlencode "key=${YOUTUBE_API_KEY}"
```

For configured competitor videos:

```bash
curl -sG "https://www.googleapis.com/youtube/v3/commentThreads" \
  --data-urlencode "part=snippet,replies" \
  --data-urlencode "videoId=${VIDEO_ID}" \
  --data-urlencode "maxResults=100" \
  --data-urlencode "order=time" \
  --data-urlencode "textFormat=plainText" \
  --data-urlencode "key=${YOUTUBE_API_KEY}"
```

Use exactly one filter per call: `allThreadsRelatedToChannelId` for the owner's configured channel, or `videoId` for a competitor video. If a response includes `nextPageToken`, fetch at most two additional pages per source unless the owner explicitly asked for a deeper scrape. Each API call costs quota; no unattended retry loops.

For every useful comment, keep:

- exact quote
- author display name if present
- published timestamp
- source label from `knowledge/scout/sources.md`
- URL in the shape `https://www.youtube.com/watch?v=<VIDEO_ID>&lc=<COMMENT_ID>` when a video ID and top-level comment ID are present

### 2. Competitor pages and blogs

Fetch every configured competitor site and blog URL in `knowledge/scout/sources.md` using the web tools available in the session. Capture launch language, pricing changes, feature claims, comparison pages, comment sections, changelog notes, and anything repeated across multiple competitors.

Each retained observation needs a link and either a short quote or a specific paraphrase tied to a page section. Do not cite a homepage as proof of a detailed claim unless the detail is visibly on that page.

### 3. Optional X and Reddit keyword search

If X or Reddit tools are available in the session, search the configured keywords from `knowledge/scout/sources.md` and collect public posts with stable links. If those tools are not available, skip this step silently. Do not mention missing tools in the owner-facing report unless the owner asked for that surface specifically.

## Synthesis rules

Produce the report titled `Scout weekly sweep — YYYY-WW`.

The main section is **5 angles worth testing**. Each angle must include:

1. `Hypothesis` — one sentence.
2. `Evidence` — two or three bullets with quotes or page-specific observations and links.
3. `Suggested owner action` — usually "Hand to Ava for hooks" or "Hand to Bobby as an ad angle"; include a concrete next step.

Five is the target. If the evidence only supports three or four, publish fewer and say why. Padding the fifth angle is how one gets invited to fewer meetings.

Keep evidence and inference separate. Do not write "people want X" when the evidence is "three commenters complained about Y"; write the latter, then the testable inference.

## Report format

Use this structure exactly:

```markdown
# Scout weekly sweep — 2026-27

Source note: Demo data used because no Scout sources are configured.

## Executive read

Three customer phrases cluster around setup anxiety, proof of control, and wanting the product to feel staffed rather than automated. The strongest test is to sell the first hour after purchase, not the abstract promise of "AI productivity".

## 5 angles worth testing

### 1. Sell the first evening, not the whole transformation

Hypothesis: Prospects may respond better to "installed tonight, useful tomorrow morning" than broad chief-of-staff language.

Evidence:
- "I bought it because I wanted Monday morning to feel less feral" — Demo YouTube comment, `demo/comments.json`, https://www.youtube.com/watch?v=demo-brief&lc=demo-c1
- "The setup wizard matters more than another agent demo" — Demo YouTube comment, `demo/comments.json`, https://www.youtube.com/watch?v=demo-brief&lc=demo-c2

Suggested owner action: Hand to Ava for three hooks about the first 12 hours after install.

### 2. Make control visible before capability

Hypothesis: Buyers with budget are less worried about whether agents can act and more worried about whether they can be stopped.

Evidence:
- "Can it ask before refunding or posting?" — Demo YouTube comment, `demo/comments.json`, https://www.youtube.com/watch?v=demo-safety&lc=demo-c3
- Competitor copy leads with automation volume but buries approvals below the fold — Example competitor page, https://example.com/competitor/agents

Suggested owner action: Hand to Bobby as an ad angle contrasting "acts fast" with "asks before it costs you".

### 3. Use role names as memory handles

Hypothesis: Sarah, Tom, Bobby, Scout, and Ava make the product easier to explain than a generic multi-agent diagram.

Evidence:
- "I remember Bobby because ads are the bit I dread" — Demo YouTube comment, `demo/comments.json`, https://www.youtube.com/watch?v=demo-roles&lc=demo-c4
- "The office metaphor is doing real work here" — Demo YouTube comment, `demo/comments.json`, https://www.youtube.com/watch?v=demo-roles&lc=demo-c5

Suggested owner action: Hand to Ava for hooks that introduce one staff role per post.

### 4. Answer the self-hosting objection with receipts

Hypothesis: The phrase "private repo" helps only when paired with practical proof of where data and secrets live.

Evidence:
- "Self-hosted is good, but where do my keys sit?" — Demo YouTube comment, `demo/comments.json`, https://www.youtube.com/watch?v=demo-trust&lc=demo-c6
- Competitor FAQ mentions privacy without showing an install boundary — Example competitor FAQ, https://example.com/competitor/faq

Suggested owner action: Ask Tom for a concise architecture note Ava can turn into a carousel.

### 5. Turn dry limits into premium trust

Hypothesis: The hard rules are not disclaimers; they are a luxury positioning asset.

Evidence:
- "The ask-before-act bit is the part I would pay for" — Demo YouTube comment, `demo/comments.json`, https://www.youtube.com/watch?v=demo-safety&lc=demo-c7
- "Most agents sound like interns with root access" — Demo Reddit-style note, `demo/comments.json`, https://reddit.com/r/example/comments/demo

Suggested owner action: Hand to Bobby for an ad concept around "AI staff with manners and a stop button".

## Standing questions watched

- Which objection keeps stopping serious buyers? Current evidence points to approval gates and key handling.
- Which competitor promise is overused? "Autonomous growth" appears louder than the evidence beneath it.

## Next Scout move

Add two real competitor blogs and the owner's YouTube channel ID to `knowledge/scout/sources.md`; the demo bench has done what a demo bench can do.
```

## Delivery

1. Write the report to `workspace/reports/scout-YYYY-WW.md`.
2. Post the same report to `#research` using `post-as` with username `Scout`.
3. If Slack is not configured, leave the report file and reply with its path. Do not fail the sweep because the front desk has not yet been fitted with a bell.
