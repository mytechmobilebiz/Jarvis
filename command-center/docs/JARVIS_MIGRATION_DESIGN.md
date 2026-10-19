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

## 5. Phase 4: memory architecture

### 5.1 Six stores

They are built on the Command Center's existing PostgreSQL, roles, audit log
and approvals. This is a future release on this branch; 0.5.1 is not changed.

| Store | What it holds | Where | Who reads it | How an agent sees it |
|---|---|---|---|---|
| A. Shared business knowledge | Verified facts: services, prices, policies, tone, targets | `memory_items` (layer `business`) | every agent; viewers and above in the dashboard | As reference notes, once approved |
| B. Agent-specific instructions and skills | JARVIS's SOULs, role rules and skills, as **reference documents**, linked to the agent they map to | `memory_items` (layer `agent_reference`) | admins; the linked agent, once approved | As reference notes only. They never change the agent's own instructions or permissions. Any change to how an agent behaves is a code or settings change you approve separately. |
| C. Conversation archive | JARVIS sessions and messages, with secrets redacted and no reasoning | `archive_sessions`, `archive_messages` | admins and operators, through search | Not in prompts by default. An admin can promote an excerpt to store A or D, which creates a reviewed item that points back to the source. |
| D. Approved long-term memories | Hermes `MEMORY.md` and `USER.md` entries; promoted excerpts | `memory_items` (layer `memory`) | per item | As reference notes, once approved |
| E. Workflow and execution history | Past runs: reports, briefs, the proposal log without contact details, ad action logs | `memory_items` (layer `history`) and `artifacts` (status `archived`) | operators and above | On request |
| F. Restricted confidential | Credit Repair material; customer personal data (names, emails, phone numbers, addresses, SSNs, card or bank details) | Credit Repair material: the existing isolated `credit_repair` database, encrypted with its own key. Other customer personal data: `restricted_items`, encrypted with a key derived from `CC_SECRETS_KEY`, admins only. | admins only, each access written to the audit log; Credit Repair only with that module's permission | Never in prompts unless an admin attaches one item to one task |

### 5.2 Every item carries

- **Source:**
  - the source system;
  - the path and line range, or the session and message ids;
  - the source SHA-256;
  - the kit and importer versions;
  - the import run.
- **Timestamps:** created in JARVIS, imported, reviewed, last edited.
- **Review status:**
  - `pending_review` (the default for everything imported);
  - `approved`, `rejected`, `superseded` or `quarantined`.
  - Also recorded: who reviewed it, when, and a note.
- **Access:**
  - the minimum role (viewer, operator or admin);
  - the agent scope (one agent, or shared);
  - sensitivity (internal, confidential or restricted).
- **History:**
  - a version number and the full history in `memory_item_versions`, so every
    edit can be shown and undone;
  - deletions are soft at first, then purged by an admin;
  - a content hash for deduplication.

### 5.3 Search

PostgreSQL full-text search: a `tsvector` with a GIN index, plus an optional
trigram index for names. It runs inside the existing database. Embeddings
would need an AI service: not without your explicit approval, and never for
store F.

### 5.4 Imported text is data, never instructions

- **In prompts:** imported items reach a model only as quoted reference
  blocks, labelled with their source. A fixed rule states that they are not
  instructions and cannot authorise anything.
- **Tools and approvals:** they cannot add tools, change settings or approve
  anything. Approvals stay exactly as they are.
- **Instruction-like items:** items with instruction-like text (override
  attempts, approval bypasses, shell commands) are flagged in review and stay
  `pending_review` until you look at them.
- **JARVIS's rules:** its own approval rules are imported as reference text
  only. The Command Center's rules are code, and imported text cannot change
  them.

### 5.5 Edit, delete, export, back up, audit

- **Dashboard:** a Knowledge & Memory page to search, review, edit, delete,
  approve and reject items, and to resolve conflicts.
- **Export:** a JSON export, with store F left out unless you ask.
- **Backup:** the existing `backup.ps1` dumps cover everything.
- **Audit:** every change is written to the existing audit log.

### 5.6 Credentials: a separate procedure, never imported

1. **What exists:** the inventory lists which credentials JARVIS has, by name
   only (section 2.5).
2. **When to re-enter them:** only when the Command Center has a real
   integration for the service. Enter them in its encrypted settings, as you
   do with the Anthropic key.
3. **New keys:** prefer new, separately scoped keys over reusing JARVIS's, so
   that either system can be shut off without the other.
4. **Rotation:** any credential found in the conversation history, memory or
   logs is listed (type and count, never the value) for you to rotate. It is
   redacted from the archive.
5. **WhatsApp:** a paired WhatsApp session is never copied or moved.
   - When JARVIS is retired, unlink the device in WhatsApp (Linked devices).
   - Pair the Command Center separately once it has a WhatsApp integration
     (0.6.0).

## 6. Phase 5: importer design (built after your approval)

A Command Center command (`python -m app.jarvis_import`) run inside the API
image. Its inputs:
- the verified database backup, read-only;
- the profile backup, restored into a folder;
- the discovery summary, as a cross-check.

| Feature | How |
|---|---|
| Dry run | It does everything except write. It produces the import plan: per record, imported, duplicate, excluded (with the reason), quarantined or conflict. It also gives counts per store and agent. |
| Deduplication and compaction | Normalized content hash per store. Live rows (`active = 1`, including compaction summaries) become the transcript. Rows archived by compaction (`active = 0, compacted = 1`) are kept as archived history, linked to the summary that replaced them, and not shown twice. Rewound or superseded rows (`active = 0, compacted = 0`) are **excluded**, with the reason recorded: they may hold something you deliberately took back, such as a pasted password. Continuations are chained to their parent sessions. |
| Secret detection and quarantine | The kit's detectors, plus redaction before anything is stored. A record with a secret is stored redacted and flagged `quarantined`. Its secret type goes on your rotation list. Raw values are never stored. |
| Restricted data | Personal data and Credit Repair terms send a record to store F, or leave it out until you approve that class. |
| Reasoning | Reasoning columns are never selected. `<think>` blocks are removed from message text. |
| Conflict review | Existing Command Center records (sites, leads, settings, agent records) are **never overwritten**. A match goes into a conflict queue with both versions, for you to decide. |
| Agent mapping | By the table in 2.1. Scheduled-job sessions map by job id; everything else maps to the Supervisor. You can remap in review. |
| Batches and checkpoints | 200 records per transaction. `import_runs` keeps the position, so an interrupted run continues where it stopped. Each source record has a stable key, so a re-run never duplicates. |
| Audit | One `import_runs` row per run, one `import_records` row per source record (action, reason, target), and audit-log entries. |
| Rollback | Deletes exactly the rows one run created, using the ids in `import_records`, inside a transaction. It then checks that the counts are back to the pre-run values. |
| Verification report | Counts in versus out per store; samples you can open; secrets found (types only); conflicts; excluded records by reason. |
| No external AI | The importer is rule-based. Summarising with a model needs your separate approval and never applies to store F. |

**Sample first:**
1. The first test import takes 5 sessions (about 50 messages), 10 profile
   files and the memory entries.
2. You review them.
3. Then 10 % of the records, then all of them.

**Isolated test database:** every test import runs against a **separate test
copy** of the Command Center (Docker project `cc-jarvis-test`, its own volumes
and ports), never against your running installation.

## 7. Workflows and skills (all created disabled, as drafts)

| JARVIS | Becomes | State after import |
|---|---|---|
| morning-brief (08:00) | the daily briefing workflow (`cc-daily-briefing`); format reference: 7 sections, 150 to 250 words | draft, disabled |
| triage-inbox (hourly) | needs the email-reading integration (not built). Kept as a written specification: categories lead, answer, refund, bug, feature request, spam. Every reply goes through approval. Spam is never archived silently. | specification only |
| Lead handling and proposals | lead intake through `cc-lead-intake`; a proposal draft from your template (the same five fields); **sending only after approval** | template imported for review |
| roas-report (06:00) | an ads report in Social Media & Marketing, with your limits (daily cap, 20 % shift, actions off) as settings | draft, disabled (sample data until a Meta integration exists) |
| ad-actions | budget changes stay approval-only, per object | not converted (the Command Center already has this) |
| weekly-sweep (Monday 07:00), dig | research tasks for SEO & Content | draft, disabled |
| write-hooks (10:00), schedule-posts | drafts in Social Media & Marketing; publishing needs approval per post, platform and time | draft, disabled |
| Website watchdog | your sites registered in monitoring; the checks are reimplemented, the script is never run | sites added **paused** until you approve |
| support-metrics (Sarah) | a support-counts section (counts only, no customer text) for the daily briefing in Email & Customer Communications. Your 8d4c340 wording is kept as reference. | draft, disabled |
| coding-lane, plan-before-code, post-as | no equivalent | reference only |
| Hooks and plugins found on the PC | never run. Where needed, their logic is rewritten as a disabled draft workflow behind approvals. Otherwise they are reference only. | reference only, or draft, disabled |

Each draft keeps:
- the trigger and schedule;
- the inputs and outputs;
- error handling and retry limits (taken from the skill text, or the Command
  Center defaults);
- duplicate prevention (one reply per lead);
- logging.

## 8. Phase 6: validation (what you will get)

| Item | Where |
|---|---|
| Source inventory | the kit's `discovery-summary.md` (Phases 2 and 3) |
| Proposed agent mappings | 2.1, 7, and in the importer's dry-run plan per record |
| Importable versus excluded | the dry-run plan (counts and reasons) |
| Memory architecture | section 5 |
| Workflow conversion plan | section 7 |
| Test results | the kit's results in 4; the importer's results with the test import |
| Outstanding security risks | section 9 |
| Exact test-import commands | below; usable once the importer exists |

**Test-import commands (planned).** The two `jarvis-*` scripts are part of
the importer, which is built only after your approval. Until then, only the
first command exists.

```powershell
# 1. In the kit folder: restore the profile backup into a working folder
#    (it gets restored\profile and restored\content)
Set-Location C:\InfiniteAI\JarvisMigration\jarvis-migration-kit
powershell -NoProfile -ExecutionPolicy Bypass -File .\jarvis-kit.ps1 -Action VerifyBackup -Archive "<the .ccjb file>" -ExtractTo C:\InfiniteAI\JarvisMigration\restored

# 2. In the Command Center folder (the release that contains the importer)
Set-Location C:\InfiniteAI\v0.5.1\InfiniteAI-CommandCenter
#    Start an isolated test copy (its own Docker project, volumes and ports)
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-test-stack.ps1 -Start
#    Dry run on a sample, then import the sample into the test copy, then the report
$restored = "C:\InfiniteAI\JarvisMigration\restored"
$db = "C:\InfiniteAI\JarvisMigration\backups\jarvis-state-20261019-105625.db"
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target test -Mode DryRun -Sessions 5 -Restored $restored -BackupDb $db
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target test -Mode Import -Sessions 5 -Restored $restored -BackupDb $db
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target test -Mode Report
#    Undo the test import, or remove the test copy entirely
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target test -Mode Rollback -Run <run-id>
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-test-stack.ps1 -Remove
```

## 9. Outstanding security risks

1. **Credentials inside history.** The conversation history and memory may
   contain credentials pasted into chats (the app redacts its display for this
   reason). The kit counts them. They must be rotated, not migrated.
2. **Customer and Credit Repair data in history.** It must not reach an AI
   service. The importer keeps it in store F, with no external calls.
3. **Unapproved behaviour from imported text.** The owner-changed rules
   (automatic email, proposals, WhatsApp) must not come back through imported
   text. Section 5.4 prevents it, and review is mandatory.
4. **Prompt injection.** Imported web or tool text could contain instructions:
   for example a fetched page saying "ignore previous instructions". It is
   flagged and never treated as instructions.
5. **The backup file and passphrase.** Whoever has both can read the profile
   copy.
   - Secret files are left out and known secret formats are redacted.
   - A secret in a format the kit does not recognise, or inside a PDF or
     image, could remain. `backup-files.json` lists the files that could not
     be scanned.
   - Keep the passphrase away from the file.
6. **JARVIS keeps running.**
   - Its schedules and automatic sends continue until you switch them off in
     JARVIS.
   - The migration does not stop them, as you asked.
   - After the migration, running both could double-answer leads.
7. **Unverified on your PC.** The kit is tested only in the cloud sandbox.
   - Windows paths and Docker Desktop file sharing are first exercised on your
     PC.
   - The kit stops before running when a check fails, and it cannot write to
     the JARVIS folders.
8. **WhatsApp session.** If JARVIS was paired with WhatsApp, the session folder
   gives full access to the account. The kit never copies it. Unlink it when
   JARVIS is retired.

## 10. What I need from you

1. **Run the kit** (`migration/jarvis/README.md`) and send
   `discovery-summary.md`. Use `-Check -ListOtherProfiles`, then optionally
   `-Full`.
2. **Approve or change the design.** In particular:
   - the six stores (section 5);
   - the conversation archive being searchable but **not** in prompts by
     default;
   - store F (customer data kept encrypted and admin-only, or not imported at
     all).
3. **Decide what to do with the rules that bypass approval.** Proposed: import
   them as reference only, with the Command Center's approvals unchanged.
4. **Decide which classes of data to import.** Proposed default:
   - knowledge, memory and skills: yes;
   - archive: yes (redacted);
   - workspace history: yes;
   - customer personal data: only into store F;
   - Credit Repair: not until the legal review (COMMERCIALIZATION.md B5).
5. **Tell me about any hooks or plugins the inventory finds.** None ship with
   JARVIS, so they would be your own additions. Say what each does.
6. **Approve the importer build.** It is built and tested here on this branch
   with synthetic data. Then the first sample import runs into the isolated
   test copy on your PC.

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
