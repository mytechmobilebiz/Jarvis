# JARVIS migration: design and read-only source analysis

**First deliverable, for your approval.** 2026-10-19, kit 0.1.0, branch
`jarvis-migration` (separate from the 0.5.1 release).

**What has not happened:**
- Nothing has been imported or activated.
- Nothing has run on your PC.
- The running Command Center and JARVIS are untouched.

**What this covers:**
1. What the source analysis found from the JARVIS repository: the templates,
   the installer and the desktop app.
2. A read-only kit you run on your PC. It analyses the installation itself and
   makes the encrypted backup from Phase 1.
3. The design of Phases 4 to 6: memory, importer, workflows and validation.

**What it waits on:** your decisions in section 10.

**Update for 0.6.0:** sections 5 to 10 now describe what was built (the JARVIS
Brain). Sections 1 to 4 are the original analysis.

Status labels used below:
- **confirmed**: the repository proves it.
- **inferred**: likely, not proven.
- **unknown**: the kit or you must check on the PC.

PROF means your profile, `C:\Users\mytec\AppData\Local\hermes\profiles\jarvis`.

---

## 1. Summary

- **What JARVIS is (confirmed).**
  - A private distribution of Hermes Agent, version 1.7.4, with the engine
    pinned to Hermes v2026.7.1.
  - All six staff run inside **one** Hermes profile, `jarvis`: Jarvis, Sarah,
    Tom, Bobby, Scout and Ava.
  - Setup copies the role templates into that profile.
- **Where the live state is.**
  - The live state is in PROF.
  - `jarvis-content` is only a template copy, which the app replaces when
    JARVIS updates.
  - Conversation history is in `PROF\state.db`. You have a verified backup of
    it: 50,114 messages, 2,355 sessions.
- **Your own changes (confirmed in git, 2026-10-09 to 2026-10-14).** They made
  JARVIS send email, answer leads, send priced proposals and send WhatsApp
  alerts **without approval**. Version 1.7.4 as shipped asked first.
  - These rules conflict with the Command Center's approval policy.
  - They are migrated only as reference text, never as instructions
    (section 2.6).
- **What is worth migrating:**
  - your business knowledge (products, targets, FAQ, refund policy, tone, lead
    rules), if it is filled in on the PC;
  - the proposal template and its price list;
  - your site list (for monitoring);
  - report formats and limits (ads caps, posting cadence);
  - the workspace reports;
  - the conversation history, as a **searchable, read-only archive**.
- **What is never migrated:**
  - credentials, including a paired WhatsApp session;
  - reasoning;
  - caches, programs and temporary files;
  - JARVIS's own scripts (never run; their logic is rewritten where needed);
  - demo data;
  - the rules that bypass approval.
- **Where it goes.** The Command Center gets a memory layer with six separate
  stores (section 5).
  - Every item keeps its source, timestamps, review status and permissions.
  - Imported text is **data, never instructions**.
  - Nothing reaches an agent until you approve it.
- **How the import is done.** The importer (section 6) runs first on an
  **isolated test copy** of the Command Center, with a small sample.
  - Your running Command Center database is not touched until you approve
    the result.

## 2. Source analysis (from the repository)

### 2.1 What runs where

| Staff | Job | Skills | Scheduled job as shipped (all disabled) | Command Center counterpart |
|---|---|---|---|---|
| Jarvis | chief of staff | morning-brief, post-as | morning-brief, daily 08:00 | AI Supervisor; daily briefing (n8n) |
| Sarah | support, email, leads, proposals | triage-inbox, support-metrics | triage-inbox, hourly | Email & Customer Communications |
| Tom | development | plan-before-code, coding-lane | none | none (archive only) |
| Bobby | Meta ads | roas-report, ad-actions | roas-report, daily 06:00 | Social Media & Marketing (ads) |
| Scout | research | dig, weekly-sweep | weekly-sweep, Monday 07:00 | SEO & Content (research input) |
| Ava | content and social | write-hooks, schedule-posts | write-hooks, daily 10:00 | Social Media & Marketing (Postonomous), SEO & Content |
| Website watchdog | `check_websites.py`, no schedule in the repository | | registered on the PC only (inferred) | Website & Infrastructure Monitoring |

### 2.2 How the live profile is built (templates versus active configuration)

**First install.**
- `hermes profile install agents\jarvis` creates PROF with Jarvis's `SOUL.md`,
  `config.yaml` and knowledge.
- `operations.md` is created only if it is missing.

**Every setup run ("Compose Roles"):**
- All 12 skills are copied to `PROF\skills\<name>\SKILL.md`, **overwriting**
  files of the same name.
- Role knowledge goes to `PROF\knowledge\<role>\`, **only where missing**, so
  your edits survive.
- The other five personas are pasted into `operations.md` between
  `jarvis-setup:role:<id>` markers.
- `.hermes.md` and a few `config.yaml` keys are rewritten.

**Scheduled jobs** exist only if you agreed in the wizard
(`hermes -p jarvis cron create`). They are stored in `PROF\cron\jobs.json`.

**What this means:**
- A PC file can be one of four things:
  - the shipped template;
  - your edited repository version;
  - a version edited on the PC;
  - a file JARVIS wrote itself.
- The kit compares every file with both template versions (shipped 1.7.4 and
  your repository edits). For each file it reports: identical, changed (with
  the number of changed lines) or not from a template.

**The kit cannot see:**
- whether JARVIS's history view assigns a session to the right staff member;
- which staff member wrote a chat message.

Only sessions of the five named scheduled jobs are attributable to a staff
member (session ids `cron_<job>_<date>_<time>`). Everything else is filed under
Jarvis, as the app does.

### 2.3 Data locations

| Data | Location | Format | Handling |
|---|---|---|---|
| Conversations | `PROF\state.db` (you backed it up) | SQLite: `sessions` and `messages`, a full-text index, `gateway_routing` | Archive (section 5, store C). Read from the **backup** only. |
| Persona and rules | `PROF\SOUL.md`; the "## Roles" blocks in `operations.md`; `AGENTS.md` if present | Markdown | Reference (store B); never instructions |
| Knowledge | `PROF\knowledge\*.md`; `knowledge\<role>\*` | Markdown and HTML | Store A after review, if filled in |
| Skills | `PROF\skills\*\SKILL.md` | Markdown | Store B (reference); translated per section 7 |
| Schedules | `PROF\cron\jobs.json` | JSON | Section 7: disabled drafts |
| Hermes memory | `PROF\memories\MEMORY.md`, `USER.md` (unknown whether present) | Markdown; one entry per `§`-separated block (inferred; the kit also counts list items) | Store D after review, one item per entry |
| Workspace | `PROF\workspace\reports`, `briefs`, `proposals` (with `sent-log.jsonl`), `metrics` | Markdown, JSON, PDF | Store E (history); customer data goes to store F |
| Settings | `PROF\config.yaml` | YAML; may hold the Postiz key inside a URL | Setting names only |
| Hooks and plugins | `PROF\hooks\<name>\`, `PROF\plugins\<name>\` (none ship with the 1.7.4 templates, so any found were added on the PC); `hooks:` keys in `config.yaml` | YAML, Python | Inventoried and backed up (redacted). **Never run or imported as code.** Reference only (store B); section 7 |
| Secrets | `PROF\.env` and variants, `auth.json`, `local-api.json`, `credentials.json`, token files; `hermes\.env`, `hermes\auth.json` | | **Never copied** (section 5.6) |
| WhatsApp linked device | `PROF\platforms\whatsapp\session\` or `PROF\whatsapp\session\`, if Sarah's WhatsApp alerts were paired (`creds.json`, `pre-key-*.json`) | JSON | **Never copied.** It gives full access to the WhatsApp account. The kit only reports whether one is present (section 5.6). |
| Logs, transcripts, caches, programs | `PROF\logs`, `PROF\sessions`, caches, `.exe` and similar | | Excluded |
| Template copy | `hermes\jarvis-content` | as the repository | Only files you added there are of interest |

### 2.4 The database (what is known before the kit runs)

**From the app's own queries (confirmed):**
- `sessions` has id, title, display_name, source, started_at and ended_at
  (epoch seconds), message_count, tool_call_count, the cost columns, archived,
  parent_session_id, end_reason and model_config.
- `messages` has role, content, tool_name, tool_calls, timestamp, active and
  compacted.

**Relationships:**
- **Continuations:** a parent with `end_reason = compression`.
- **Branches:** `branched`, or `_branched_from` in model_config.
- **Delegations:** `_delegate_from`.
- **Compaction (Hermes, confirmed in its source and in the app's code):**
  - compaction archives the original turns as `active = 0, compacted = 1`
    (still searchable);
  - it then adds the summary, and the turns it carries on, as new
    `active = 1` rows;
  - rewound or edited messages, and superseded copies, are
    `active = 0, compacted = 0`, which Hermes hides.

**Unknown:**
- reasoning columns;
- the other tables;
- whether 50,114 includes archived and compacted rows.

The kit measures all of these without reading any content out.

### 2.5 Integrations and credentials (names only)

| Integration | JARVIS credential names | Command Center today |
|---|---|---|
| AI model | `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, or a subscription sign-in in `auth.json` | Anthropic key only (encrypted) |
| Slack | `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_ALLOWED_USERS`, `SLACK_OWNER_MEMBER_ID`, `SLACK_HOME_CHANNEL` | missing (planned in 0.6.0, Messaging) |
| Email (IMAP read, SMTP send) | `EMAIL_ADDRESS`, `EMAIL_PASSWORD`, `EMAIL_IMAP_*`, `EMAIL_SMTP_*`, `EMAIL_ALLOWED_USERS` | sending is a mock behind approvals; reading is missing |
| WhatsApp lead alerts | `WHATSAPP_ENABLED`, `WHATSAPP_ALLOWED_USERS`, plus the paired linked-device session folder (2.3) | missing (0.6.0); pair the Command Center separately, never reuse JARVIS's session |
| Stripe, RevenueCat | `STRIPE_RESTRICTED_KEY`, `REVENUECAT_*` | missing |
| Meta Ads | `META_ACCESS_TOKEN`, `META_AD_ACCOUNT_ID` | mock (sample data, approvals) |
| Postiz | `POSTIZ_API_KEY`, `POSTIZ_URL`, and inside `config.yaml` | Postonomous mock |
| GitHub and Claude Code (Tom) | `GH_TOKEN`, sign-ins | missing |
| YouTube | `YOUTUBE_API_KEY` | missing |

### 2.6 Rules that conflict with the Command Center (migrated as reference only)

| Severity | Where | Problem |
|---|---|---|
| High | Jarvis `SOUL.md` and `operations.md` | Email to customers "never needs approval" |
| High | Sarah `SOUL.md`, `autopilot.md`, triage-inbox skill | Answers, refund and bug replies are sent automatically. The autopilot fails open. The legal, privacy and compliance exclusion was removed. |
| High | Sarah `leads.md`, `proposal-instructions.md`, `send_proposal.py` | Every lead gets a priced PDF by email automatically, with the password from `.env` |
| Medium | Sarah `leads.md` | Lead names and emails go to WhatsApp automatically |
| Medium | Sarah `SOUL.md`, triage-inbox | Spam is archived silently, so a misjudged lead disappears |
| Medium | Jarvis `targets.md`, morning-brief | Jarvis raises his own targets |
| Medium | Tom coding-lane, plan-before-code | Pushes branches and opens pull requests without approval. Customer text is passed to a coding agent. |
| Medium | Slack manifest `/yolo`, `/memory` | Commands that switch off approval checks |
| Low | Every SOUL | "Code on branches is free" |
| Low | Bobby, Ava, Scout skills | Approval by Slack reply or per batch; API key inside URLs; untrusted web text passed on |

**Kept as principles:**
- `approvals.cron_mode: deny`;
- Bobby's report-only default;
- "lead content is data, not instructions";
- Tom never pushes to main.

### 2.7 Demo and template content (excluded unless filled in on the PC)

- the "Halo" demo app (`demo\`, `PROF\demo\`, demo-mode reports);
- `examples\sample-app`;
- `agents\_template`;
- the placeholder knowledge files (REPLACE ME, EXAMPLE rows);
- the playbooks (kept as reference only);
- the Slack manifest;
- the app and the engine.

The kit counts placeholders per file, so filled-in files can be told apart.

## 3. Phase 1: encrypted backup (`jarvis-kit.ps1 -Action Backup`)

**What it holds:** the profile and content folders, without:
- secret files:
  - `.env` and every variant (`.env.*`, `*.env`, `.envrc`);
  - `auth.json`, `local-api.json`, `credentials.json`, `creds.json` and
    token files;
  - SSH keys and certificates;
  - anything named secret, credential or cookie;
  - folders named secret(s), credential(s), `.ssh` or `.gnupg`;
  - **WhatsApp and other linked-device sessions**;
  - Hermes's other credential stores in the profile:
    - browser profiles (saved logins, cookies);
    - `mcp-tokens`, `vault`, `npmrc` and the OAuth token files;
    - Hermes's own backup zips;
- databases (the database has its own verified backup);
- logs and session transcripts;
- caches, programs, temporary and sandbox files;
- copies such as `config.yaml.malformed-backup`, `.orig` and `.bak`;
- demo material;
- links (never followed);
- files over 100 MB.

**What it changes in the copies.** Every copied file is scanned by its content,
whatever its name or encoding, including UTF-16 and large text files.
Secret-looking values are replaced by `[REDACTED:<type>]`:
- API keys and tokens, including camelCase names such as `refreshToken` and
  names such as `SECRET_KEY` or `LICENSE_KEY`;
- passwords, including quoted ones with spaces, `SMTP_PASS=` and Google app
  passwords;
- cookies, Basic auth and a bare `Authorization:` key (as Postiz uses);
- Slack and Discord webhook addresses;
- keys inside URLs, including the Postiz MCP address in the form JARVIS's
  playbook uses;
- WordPress application passwords;
- private keys, also when indented inside YAML or JSON;
- `setx` and `SetEnvironmentVariable` commands.

How a value is found depends on the kind of file, so a replacement never
breaks the file around it:
- **Code** (Python, PowerShell, JavaScript and similar): code that only reads a
  setting is left alone, so the scripts stay valid; quoted secrets in them are
  still replaced.
- **Shell and batch files:** `NAME=value` is a literal value and is replaced;
  `NAME=$OTHER` and `NAME=%OTHER%` are not.
- **Configuration files** (YAML, INI, TOML, `.env`): a setting's value is
  replaced up to the end of the line, without its comment.
- **Text and Markdown:** only a setting at the start of a line, or inside
  JSON-like or code-span markup, is replaced, and only if the value looks like
  a secret. Ordinary sentences such as "the password: is set in the vault"
  stay as they are.

Long base64 data (images, certificates) is not taken for a key.

**Limits.** Redaction recognises known formats only, and it cannot look inside
PDFs and images. `backup-files.json`, which stays on your PC, names every
redacted file and every file it could not scan. Treat the backup as
confidential anyway.

**How it is protected:**
- **Encryption:** AES-256-GCM in 1 MB chunks. The key comes from your
  passphrase (at least 12 characters) through scrypt (2^17, 8, 1).
- **Tamper detection:** every chunk is bound to its position and to the end of
  the file, so a changed, reordered or cut-off archive does not open.
- **Manifest:** a list inside the archive with the SHA-256 of every file.

**Checks:**
- The kit verifies the archive right after writing it.
- `-Action VerifyBackup` checks it again later and can restore it into an
  empty folder.
- `backup-report.json` lists what was included, left out and not scannable,
  by category. It names no file.
- **JARVIS can stay open.** A file that changes while it is being read is
  read again, up to three times. A file still changing then, or one that
  disappeared, is left out and counted. A file JARVIS changed before the kit
  reached it is backed up as it then was.
- **The archive is `.partial` until it verifies.** It gets its final name only
  after verification. If Docker stops the kit mid-run, the wrapper removes the
  unfinished file.
- **A restore goes into a staging folder first.** A damaged archive or a wrong
  passphrase restores nothing.

## 4. Phases 2 and 3: inventory (`jarvis-kit.ps1 -Action Discover`)

**What it reads:**
- the profile and content folders;
- your verified database backup.

**Files: what it records for each:**
- category, size and SHA-256;
- template status, and the number of lines changed against the template;
- the number of placeholders;
- counts of secret-like strings, personal data (emails, phone numbers, SSNs,
  card numbers, Credit Repair terms) and instruction-like text (override
  attempts, approval bypasses, shell commands).

It also reports:
- the setting names in secret files (never values);
- the `config.yaml` keys and a few harmless settings (model, provider,
  approval mode), and the MCP servers: JARVIS's own by name, any others
  numbered;
- every scheduled job: schedule, enabled, delivery channel, skills, prompt
  length, and whether the prompt differs from the template;
- Hermes memory entry counts;
- hooks and plugins (as `plugin_or_hook` files, with the same template
  comparison and counts);
- whether a WhatsApp or other linked-device session is present (yes or no).

**Database: what it records:**
- every table, column, index and trigger;
- row counts;
- per column: the non-empty count, the total length and the longest value;
- values of fields such as role, source, model, tool_name and end_reason;
- date ranges and counts per month;
- archived, continuation, branch and delegation counts;
- scheduled-job sessions per job;
- live messages, messages archived by compaction, and rewound or superseded
  messages;
- tool-call messages versus conversation;
- messages per session;
- orphans;
- `<think>` blocks.

Random identifiers, such as Hermes's `message_uid`, are not scanned as text.

**Progress and time.** The kit prints a progress line per step (counts only).
- On a synthetic database the size of yours, measured in the sandbox, the
  default run took 29 seconds (with `-Check`) and `-Full` took about 25 minutes (one CPU core).
- Through Docker Desktop's file sharing on Windows, expect longer.

**Reasoning columns are only counted.** The kit never reads them out.

**Content scan.** A **sample** (500 by default, spread across the database) is
scanned. It counts:
- secret-like strings;
- personal data;
- instruction-like text, per role;
- business topics (leads and proposals, support, email, advertising, social,
  SEO, websites, development, finance, credit repair);
- duplicates.

`-Full` scans every row, once you have seen the sample.

**What leaves the container:** counts and structure. No message text, title,
system prompt, file content or secret value.

The summary also shows a name only when it cannot be yours:
- a path, only for a file at a JARVIS template's own place;
- a skill name, only for a template skill;
- a credential file's name, only for a standard name at the top of the
  profile;
- setting and key names, only when made of the words JARVIS's templates and
  Hermes use;
- MCP servers and their tools, only for JARVIS's own servers.

Everything else is numbered. `discovery-detail.json` has the real names for
you and stays on your PC.

**Test results (cloud sandbox, synthetic installation):** the kit's tests
plant secrets, personal data, reasoning and message text in a JARVIS-shaped
profile and `state.db`. They also add a live database, links pointing outside,
caches and programs.

| Check | Result |
|---|---|
| Unit and integration tests (`migration/jarvis/tests`) | **92 passed** |
| End-to-end through `jarvis-kit.ps1` in PowerShell 7 with the Command Center image (`testing/jarvis-kit-check.sh`) | **13 of 13 passed** |

**Three independent review rounds.**
- **Round 1** confirmed 21 problems:
  - 6 in the privacy of the summary;
  - 8 in the backup's coverage and redaction;
  - 3 in the wrapper's folder checks and its documentation;
  - 4 in this document.
- **Round 2**, on the fixed kit, confirmed 17 more:
  - config and MCP names in the summary;
  - secret formats the redaction missed;
  - patterns that could break scripts;
  - Hermes's other credential stores;
  - files changing during a run;
  - unfinished archives and restores;
  - identifier columns counted as card numbers;
  - too many passes over the database;
  - inaccurate statements here.
- **Round 3**, on the twice-fixed kit, confirmed 14 more:
  - indented private keys no longer redacted (a round-2 regression);
  - replacements that could break JSON, YAML or shell files, or change
    ordinary sentences;
  - a slow pattern on very long lines;
  - base64 data taken for a key;
  - `*_KEY` setting names missed;
  - plugin tool names in the summary;
  - a stopped restore that could not be run again;
  - the wrapper's clean-up after a stopped run;
  - a database whose time column mixes numbers and text;
  - a table that cannot be opened;
  - PowerShell 5.1 treating Docker's progress output as an error.

All are fixed. Every fix to the kit, the wrapper and the database report has a
test. The corrections to this document and the README (wording, missing items,
commands) were checked by hand against the code and the JARVIS source.

They prove these points:
- **Reports:**
  - no planted value appears in any report;
  - the summary names no file, skill, job, client, domain or number of yours,
    even for a customer-named copy of a template or a customer-named
    credential file;
  - a multi-line key in `.env`, or a value pasted on its own line, is not
    taken for a setting name;
  - an MCP server, quick command or chat named after a client, or keyed by
    their number, is not named;
  - damaged text in the database neither stops the run nor gets printed;
  - a table that cannot be opened, or a time column that mixes numbers and
    text, is reported, not fatal;
  - a plugin's tools are grouped, not named.
- **Read-only:**
  - every source file is opened for reading only;
  - the live `state.db`, programs, caches and link targets are never opened;
  - the backup is opened only read-only and immutable, and writes, `ATTACH` and
    pragma changes are refused;
  - the installation is byte-for-byte unchanged afterwards.
- **Accuracy:**
  - counts, template status (including Windows line endings), lineage and
    reasoning columns are reported correctly;
  - a sample scans only part of the database;
  - identifier columns are not counted as card numbers;
  - repeated content counts as duplicate whatever its identifiers.
- **Backup:**
  - it restores without secret files or values. That holds for the WhatsApp
    session, `.env` variants, SSH keys, the Postiz key in both URL forms,
    cookies, passwords that look like placeholders, UTF-16 text and text over
    4 MB.
  - It also holds for Google app passwords, `SMTP_PASS`, `refreshToken`,
    webhooks, a bare `Authorization:` key, secrets in `.cmd`, `.sh` and `.ps1`
    files, and Hermes's credential stores.
  - Files with nothing to redact restore byte-for-byte (binaries, line
    endings). JARVIS's scripts and the owner's still parse: only a quoted
    secret is replaced. Redacted JSON and YAML still load, a redacted shell
    script passes `bash -n`, and ordinary sentences are unchanged.
  - Private keys are redacted in any indentation; a 1 MB line is scanned in
    well under a second.
  - A restore works where file times cannot be set (Docker Desktop on Windows).
  - A file vanishing or still changing during the backup is counted, not fatal.
  - An unverified archive leaves nothing behind.
  - A damaged archive restores nothing, and the same folder can be used again.
  - A restore stopped half way (Docker closed) leaves the folder as it was, or
    is cleaned up by the next run.
  - a wrong passphrase, a flipped byte, a cut-off or an extended archive all
    fail;
  - a short passphrase is refused.
- **Refusals:** the wrapper refuses, before anything runs:
  - an output or restore folder inside the JARVIS folders;
  - the live database;
  - a missing image.

  A relative output folder works, and so does a copy of the profile outside
  Hermes's folders.

## 5. Phase 4: memory, as built in 0.6.0 (the JARVIS Brain)

Sections 5 to 10 were the plan; they now describe what 0.6.0 contains. The
full specification, with every rule and test, is `JARVIS_BRAIN_SPEC.md`.

### 5.1 Where things are kept

All in the Command Center's own PostgreSQL database (migration
`0006_brain.sql`), with its roles, audit log and approvals:

| Table | What it holds |
|---|---|
| `memory_items`, `memory_item_versions` | Approved memory: business facts, knowledge, agent notes, operating instructions and skills (as reference), projects, decisions, accomplishments, problems. Every change is a version. |
| `archive_sessions`, `archive_messages` | JARVIS's conversations, cleaned. A conversation holding restricted data keeps no text at all (withheld). |
| `workflow_drafts` | JARVIS's scheduled jobs, as drafts that never run. |
| `memory_conflicts` | What an import found that differs from the Command Center's own records; a person decides. |
| `import_runs`, `import_records`, `brain_tombstones` | One row per run and per source record (action, reason, target), for verification, resume and rollback; purged keys never come back. |

The plan's store F (an encrypted store for customer personal data) was not
built: restricted data (SSNs, card and bank details, dates of birth, credit
files) is never stored, and Credit Repair material stays in the Credit Repair
module.

### 5.2 Every item carries

Its source (system, path or session and message, SHA-256, import run), times
(in JARVIS, imported, reviewed, edited), review status (`pending_review`,
`quarantined`, `approved`, `rejected`, `archived`), sensitivity (`general` or
`customer`), the agent it belongs to (or shared), its flags (secrets removed,
instruction-like, approval conflict) and its full version history.

### 5.3 Who sees what

- **Agents** read approved items only. The Supervisor also reads the other
  agents' items (never Credit Repair's). Customer items reach only the
  Supervisor and Email & Customer Communications.
- **AI prompts** get memory only if an admin switches it on in Settings (off
  by default): approved, general items of the kinds that describe the
  business, never customer items, never quarantined or flagged ones, fenced
  as reference material.
- **People:** viewers see approved general memory and no conversations;
  operators see general conversations and items, never customer or
  quarantined ones; admins see everything except Credit Repair, which needs
  the sensitive-data permission.
- **History questions** ("what did JARVIS do about SSL?") are answered by the
  Supervisor from general conversations and approved items, locally: no AI
  model sees the answer.

### 5.4 Imported text is data, never instructions

JARVIS's rules that bypass approval ("Email is pre-approved", "Always send
proposals without asking") are imported quarantined, flagged as an approval
conflict, need an admin, and never reach a prompt even when approved. The
Command Center's approvals are code; no import changes a setting, an agent's
state, a schedule or an approval (tested: `test_brain_authority.py`).

### 5.5 Edit, delete, export, back up, audit

The JARVIS Brain page: search, review, edit (a new version), delete and
restore, purge (admins, with a tombstone), redact an old version, export,
resolve conflicts, promote an archived message to memory, withhold a message.
`backup.ps1` covers the new tables. Every change is in the audit log, without
text.

### 5.6 Credentials: never imported

Secrets are redacted before anything is stored; the import report lists their
kinds and counts (never values) for you to rotate. A paired WhatsApp session
is never copied.

## 6. Phase 5: the importer, as built

`python -m app.cli brain-import plan|run|resume|verify|rollback|status|rescan|decisions-export|decisions-apply`,
run by `scripts/jarvis-import.ps1` in a one-off container from the running
api's image, on the private network only, with the backup and the restored
profile mounted read-only.

| Feature | How |
|---|---|
| Plan | Everything but the writes: what each record would become, counts and reasons. |
| Staged import | 5 sessions, then about 10 %, then all; a re-run adds nothing (stable source keys). |
| Cleaning | Secrets redacted (the kit's detectors plus conversation, command-line, JSON-key and webhook rules); restricted data withholds the whole conversation; e-mail addresses and phone numbers masked; private reasoning never read. |
| Compaction | Live rows are the transcript; compacted rows are kept as history; rewound rows are excluded with the reason. |
| Conflicts | Existing Command Center records are never overwritten; a difference becomes a conflict for a person. |
| Checkpoints | Batches with savepoints; an interrupted run resumes where it stopped. |
| Verify | Counts in against out, no planted value or secret anywhere (rollback copies included), search works. |
| Rollback | Removes what one run created and puts back what it changed; refused when a later run depends on it or a decision since would be lost; withheld text never comes back. |
| No network, no AI | The importer is rule-based and has no route out. |

## 7. Workflows and skills (drafts, never run)

JARVIS's jobs become workflow drafts with their schedule, inputs, approvals
and history; skills become reference items. A draft never runs. An admin can
convert a reviewed draft into a Command Center schedule, which is created
**disabled**; switch the job off in JARVIS before switching the schedule on.
Websites JARVIS watched become conflicts that add a site **paused**.

## 8. Phase 6: validation

The test copy (`brain-test.ps1`: Docker project `cc-brain-test`, its own
database, secrets and port 8190, a worker without internet, no n8n), the
staged import into it, review there, then the live import after your
approval: `DEPLOY_WINDOWS.md`, "JARVIS Brain: test copy and import". Test
results and performance: `RELEASE_NOTES_0.6.0.md`.

## 9. Outstanding security risks

1. **Detection is pattern-based.** Names and street addresses are not
   detected; an unusual secret format can be missed. Review in the test copy
   is the second line; `rescan` re-cleans everything when detectors improve.
2. **Credentials found in history** must be rotated where they are used: the
   rotation list says which kinds.
3. **The backups** (the JARVIS database copy, the restored folder, the
   pre-import dump) are plain data on your PC: delete them as the guide says.
4. **JARVIS keeps running.** Its schedules and sends continue until you switch
   them off in JARVIS; running both could double-answer leads.
5. **Unverified on your PC.** Everything here was tested in the cloud sandbox
   only.

## 10. What I need from you

See `RELEASE_NOTES_0.6.0.md`, section "Your approvals".

## Sources

- **Repository** (read-only, this branch):
  - `agents/`, `setup/src/steps/profile.ts`, `setup/src/steps/compose-roles.ts`
  - `hud/src/history.ts`, `hud/src/resources.ts`, `hud/src/packaged-content.ts`
  - `README.md`, `SETUP.md`, `playbooks/`
  - git history 288cb6c to 3d83ecc
- **Hermes Agent documentation:**
  - https://hermes-agent.nousresearch.com/docs/user-guide/profiles
  - https://hermes-agent.nousresearch.com/docs/user-guide/sessions
  - https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/user-guide/profiles.md
  - https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/user-guide/sessions.md
  - https://github.com/NousResearch/hermes-agent/issues/8650
- **Other write-ups of the Hermes layout:**
  - https://mer.vin/2026/05/hermes-agent-dot-hermes-folder-layout-soul-secrets-memory-skills/
  - https://blog.dailydoseofds.com/p/the-anatomy-of-hermes-folder
