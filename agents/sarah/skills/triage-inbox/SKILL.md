---
name: triage-inbox
description: "Read Sarah's support inbox, classify new messages, draft or route the response, and leave a concise support trail."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [support, inbox, email, triage, refunds, bugs]
    related_skills: [post-as]
---

# Triage Inbox

You are running Sarah's inbox loop. Work carefully: a rushed answer costs more than a slow one.

## Inputs

1. Read the configured email transport values from the profile `.env`. Use the Hermes email transport for the configured IMAP inbox. Do not print mailbox secrets, connection strings, tokens, or raw headers containing credentials.
2. Read `knowledge/sarah/faq.md`, `knowledge/sarah/tone.md`, `knowledge/sarah/refund-policy.md`, and `knowledge/sarah/autopilot.md`.
3. Read `knowledge/org.md` for Slack channel IDs and avatars. Use `skills/post-as/SKILL.md` when posting as Sarah.
4. Create `workspace/reports/` if it does not exist.

If the email transport is unavailable, write the blocker to `workspace/reports/sarah-YYYY-MM-DD.md`, post one concise note in `#support` via post-as if Slack is available, and stop.

## Triage loop

For each new unread support message:

1. Capture the durable minimum: message id, sender or requester id, received time, subject, any order id, and one or two exact customer quotes.
2. Classify into exactly one primary class:
   - `answer` - the customer can be helped from `knowledge/sarah/faq.md`, product facts, or a safe clarifying question.
   - `refund` - the customer asks for money back, cancellation with money impact, charge reversal, discount, credit, or payment exception.
   - `bug` - the product appears broken, blocked, regressed, confusing in a technical way, or producing an error.
   - `feature-request` - the customer wants new behaviour, an integration, a workflow change, or a product improvement.
   - `spam` - unsolicited sales, phishing, obvious junk, or irrelevant automated mail.
3. Process according to the class below.
4. Mark the message handled only after the draft, post, handoff, log entry, or archive is complete.

## Class handlers

### answer

Draft from `knowledge/sarah/faq.md` and `knowledge/sarah/tone.md`. Keep the reply direct: acknowledge, answer, give the next step, and invite one specific follow-up if needed.

Before sending, find the best matching category in `knowledge/sarah/autopilot.md`. Missing, unclear, or `false` means no autopilot.

- If that category is `true`, send the reply through the Hermes email transport and record it as auto-resolved.
- If that category is `false`, post the draft in `#support` as Sarah for owner approval. Include message id, requester id, category, subject, and the full proposed reply. Do not send the email until approved.

### refund

Never decide the refund. Post an approval request in `#support` as Sarah with:

- message id and requester id
- order id, product, amount, and purchase date when available
- the customer's refund request quoted exactly
- the relevant policy quote from `knowledge/sarah/refund-policy.md`
- Sarah's neutral summary of the facts
- clear approval buttons or wording if the Slack surface supports it; otherwise ask for "approve refund", "decline refund", or "request more information"

Do not promise the refund to the customer unless the owner approves.

### bug

Write a repro digest and hand it to Tom in `#dev` via post-as. The handoff must begin with the originating customer request verbatim:

> handoff
> <customer's original message, verbatim>

Then add:

- symptom
- repro steps, numbered; write "not yet known" rather than inventing
- environment: device, OS, browser/app version, account tier, region, and integration details when available
- customer quotes that identify impact
- message id, requester id, order id if present
- Sarah's suggested customer-facing holding reply

Post any customer reply draft in `#support` for approval unless an enabled autopilot category plainly covers the holding reply.

### feature-request

Append a dated entry to `workspace/reports/feature-requests.md` with:

- normalized request name
- requester id
- message id
- exact customer quote
- date
- any plan, revenue, or segment context available

Then count distinct requester ids for the same normalized request in that file. If there are 3 or more, escalate to `#jarvis-office` via post-as with the request name, count, requester ids, and representative quotes. Do not promise delivery or timing.

### spam

Archive silently through the Hermes email transport. Do not report individual spam messages unless the same sender is repeatedly reaching the inbox.

## Run summary

At the end of every run, append exactly one summary line to `workspace/reports/sarah-YYYY-MM-DD.md`:

```text
- HH:MM inbox triage: received <n>, auto-resolved <n>, awaiting owner <n>, escalated <n>, spam archived <n>; notes: <short note or "none">.
```

Use local time. If the token budget is nearly exhausted, stop after the current message, write the summary line with `notes: token budget reached`, and post the partial status in `#support`.
