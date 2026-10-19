# JARVIS Brain: developer specification (0.6.0)

This is the contract the JARVIS Brain is built against. The owner-facing explanation
is `docs/JARVIS_MIGRATION_DESIGN.md`. Paths are relative to `command-center/`.

This is revision 2, after an adversarial design review (38 findings, all addressed
below).

Release number: the JARVIS Brain is **0.6.0**. The Messaging & Channels plan
moves to 0.7.0.

## 0. Rules that hold everywhere

1. **Imported text is reference data.**
   - It never becomes an instruction, an approval, a setting or a running job.
   - Approvals are decided only by `app/tasks.py` from each capability's risk.
   - A workflow draft becomes a schedule only through an admin, and that schedule is
     created **disabled**.
2. **Read-only sources.**
   - The JARVIS database backup is opened only through `jarvis_kit.open_backup`
     (`mode=ro&immutable=1`, `query_only`, and an authorizer that allows reads only).
   - Profile files are opened only for reading; links are never followed.
   - Columns are always listed explicitly. Reasoning columns
     (`jarvis_kit.REASONING_COLUMN`), `system_prompt`, model, cost and token columns
     are never selected.
   - `<think>` blocks are removed.
   - The importer refuses a backup that has a non-empty `-wal` or `-journal` file
     next to it (`immutable=1` would silently ignore it).
3. **Secrets never stored.**
   - Every text goes through `app.brain.text.clean()`:
     - the kit's detectors;
     - secrets inside sentences;
     - positional literals in scripts;
     - secret URL parameters;
     - long random tokens;
     - whole configuration dumps;
     - JSON value by value.
   - Kind `"chat"` is used for every text except code, shell and config files, which
     use their own kind.
   - Secret types and counts are stored and reported (for the credential rotation
     list); values never are.
   - A record that held a secret is imported as **`quarantined`** (memory items), or
     flagged `secrets_redacted` (archive). Only an admin may approve it, and it never
     reaches an AI prompt.
4. **Restricted data never stored.** That covers SSNs, card numbers, bank details,
   dates of birth and a client's credit file (`text.RESTRICTED_PATTERNS`).
   - **A session in which any message holds restricted data is withheld entirely.**
     Every message is kept as a row with `withheld = true` and no text, the title and
     summary are empty, and the session has `sensitivity = 'restricted'`.
   - A file holding restricted data is excluded.
   - The data stays in the JARVIS backup. Credit Repair work belongs in the Credit
     Repair module.
5. **Customer data.**
   - E-mail addresses, phone numbers (including E.164 and WhatsApp ids) are masked as
     `[email]` and `[phone]`.
   - Text that already contains those stand-ins counts as masked.
   - These are always `sensitivity = 'customer'`:
     - any record with a masked value;
     - every message of Sarah's sessions (staff `sarah`);
     - every message whose tool name looks like mail, WhatsApp, SMS, CRM or lead
       handling (`(?i)mail|smtp|imap|whatsapp|sms|message|lead|crm|contact|proposal`);
     - Hermes `USER.md` entries (the owner's own profile).
   - Customer records are seen by **admins only** in the dashboard. They are read by
     the Supervisor and Email & Customer Communications only, never sent to an AI
     model, and never quoted in recall answers.
   - Names and street addresses are not detected. That is why whole customer-facing
     sessions are marked customer.
6. **No network and no AI.**
   - The importer, recall and the memory service make no network or model call.
   - Memory reaches an AI prompt only through `AgentMemory.prompt_block()`:
     - only when the owner sets `brain_ai_context = approved_general` (default `off`);
     - only approved, general items of kinds business, knowledge, agent_note and
       project;
     - never items with a risk flag (`policy.RISK_FLAGS`);
     - fenced: angle brackets inside are replaced.
7. **Nothing existing is overwritten.**
   - Existing memory items, sites, schedules, agents and approval rules are never
     changed by an import.
   - A clash becomes a `memory_conflicts` row for a person to decide.
   - A resolution that changes an existing row records the before state in
     `effects`, so rollback can revert it.
8. **Honest and safe output.**
   - Console output, logs, errors, audit details and reports carry counts, ids, kinds
     and safe labels only. They never carry message text, titles, file contents or
     exception text that could contain row values.
   - Store errors as `<ExceptionClass> in <phase> at <source_key>`.
   - The engine uses `hide_parameters=True`.
   - Anything inferred (labels, staff attribution) is shown as inferred.

## 1. What already exists (keep these contracts)

| File | Contents |
|---|---|
| `backend/app/migrations/0006_brain.sql` | All tables. Idempotent. No foreign key into a pre-0.6.0 table: `test_brain_schema.py` restores a pre-0.6.0 dump with `restore.ps1`'s flags. |
| `backend/app/models.py` (end) | `ImportRun`, `ImportRecord` (with `before`), `MemoryItem`, `MemoryItemVersion`, `ArchiveSession`, `ArchiveMessage` (with `source_fp`, `tool_call_id`), `MemoryConflict` (with `dedupe_key`, `effects`), `WorkflowDraft` (with `source_fp`; `schedule_id` is not a foreign key), `BrainTombstone` |
| `backend/app/brain/__init__.py` | `KINDS`, `AGENTS`, `STAFF`, `STAFF_AGENT`, `SKILLS`, `JOBS`, `staff_of_path`, `IMPORTER_VERSION` |
| `backend/app/brain/jarvis_kit.py`, `templates.json` | Byte-identical copies of `migration/jarvis/` (unit test) |
| `backend/app/brain/text.py` | `clean(text, kind="chat")` → `Cleaned(text, secret_types, restricted, masked, think_removed)` with `.withheld`, `.sensitivity`, `.flags()`, `.merge()`; `sanitize`, `most_sensitive`, `instruction_flags`, `topics`, `content_hash`, `truncate`, `one_line`, `labels_for(...)`, `excerpt`, `RESTRICTED_PATTERNS` |
| `backend/app/brain/policy.py` | `POLICIES`, `policy_for`, `agent_items/agent_archive/agent_messages(policy, general_only=)`, `needs_admin(kind, agent, status, flags)`, `RISK_FLAGS`, `BUSINESS_KINDS`, `PROMPT_KINDS`, `PersonScope(role, sensitive)` with `person_items/person_sessions/person_messages` |
| `backend/app/brain/store.py` | `BrainError`; `create_item`, `edit_item`, `review_item`, `delete_item`, `restore_item`, `purge_item`, `versions`; `search_memory`, `search_archive`, `search_was_filtered`; `AgentMemory(session, agent)` with `.relevant(query, limit, kinds, general_only)`, `.history(query, labels, limit, since, general_only)`, `.prompt_block(query)`; `ai_context_allowed`, `AI_CONTEXT_KEY`, `MEMORY_NOTE`, `search_terms` |
| `backend/tests/unit/test_brain_text.py`, `tests/integration/test_brain_store.py`, `tests/integration/test_brain_schema.py` | Contract tests (keep passing; add to them) |

**Access summary** (`policy.py` holds the code):

| Who | Memory items | Conversation archive |
|---|---|---|
| Viewer | approved, general, business kinds | none (overview counts only) |
| Operator | all but quarantined; general only | general sessions and messages |
| Admin | all, including customer and quarantined | all, including customer; restricted sessions only with the sensitive permission (metadata only) |
| Credit Repair items (any role) | need the sensitive-data permission | — |
| Approving | operator for plain items; admin when `policy.needs_admin` (instruction, skill and workflow kinds; Credit Repair items; quarantined; any `RISK_FLAGS` flag) | — |

**Memory kinds map to the owner's categories:**

| Owner's category | How it is stored |
|---|---|
| 1. Long-term business memory | `business` |
| 2. Shared verified knowledge | `knowledge`, `agent = ''`, approved |
| 3. Agent-specific memory | any kind with `agent = <key>`, including `agent_note` |
| 4. Historical conversation search | `archive_sessions`, `archive_messages` |
| 5. Project and task context | `project` |
| 6. Approved operating instructions | `instruction`, approved by an admin. These are reference only; code still enforces approvals. |
| 7. Skills and workflow references | `skill`, `workflow`, `workflow_drafts` |
| 8. Knowledge updates and version history | `memory_item_versions` |

## 2. Importer (`backend/app/brain/source.py`, `importer.py`, `cli.py`)

### 2.1 Commands

```
python -m app.cli brain-import plan     --db F --profile D [--content D] SELECTION [--report FILE] [--discovery FILE]
python -m app.cli brain-import run      --db F --profile D [--content D] SELECTION [--label T] [--actor T] [--report FILE] [--discovery FILE] [--pre-backup-sha256 H]
python -m app.cli brain-import resume   --run ID --db F --profile D [--content D] [--report FILE]
python -m app.cli brain-import verify   --run ID --db F --profile D [--content D] [--report FILE]
python -m app.cli brain-import rollback --run ID --yes [--discard-reviewed] [--actor T]
python -m app.cli brain-import status   [--run ID]
python -m app.cli brain-import rescan   [--yes]          # re-clean every stored brain text with the current detectors
python -m app.cli brain-import decisions-export --out FILE  # review decisions keyed by source_key (test copy)
python -m app.cli brain-import decisions-apply  --in FILE --run ID [--yes]  # apply them after the production import
```

**Selection** (`SELECTION`):
- `--sample N`: N sessions spread evenly over the time-ordered session list.
- `--files-sample M`: up to M profile files spread over the categories, plus every
  memory entry. The default with `--sample` is 10.
- `--all`: every session, every file, and orphan messages.
- `--since DATE --until DATE`: dates in the customer timezone. These select **whole
  sessions** by `started_at`.
- `--no-files`.
- Staged plan (documented for the owner): `--sample 5` → review → `--sample` about
  10 % → review → `--all`.

**Exit codes:** `0` done; `1` failed or verification failed; `2` refused before any
change.

**`--discovery`** is the kit's `discovery-summary.json`. The total session and
message counts in the backup must equal it, or the command refuses.

**`plan`** writes **nothing** to the database. It reads the target database
read-only, for duplicates, conflicts and tombstones. It prints counts and, with
`--report`, writes JSON (contents in 2.8).

**Liveness:**
- `run`, `resume` and `rollback` hold `pg_advisory_lock(7314002213, run_id)` on a
  dedicated connection for their whole life. Each run is locked separately.
- One import at a time is also enforced with `pg_try_advisory_lock(7314002213, 0)`,
  plus the unique index.
- A run is "alive" only while its lock is held. Others use `pg_try_advisory_lock` to
  decide, never the heartbeat age.
- `heartbeat_at` is refreshed by a background task every 30 seconds.
- Every status change is conditional:
  `UPDATE … WHERE id = :id AND status = :expected`. Zero rows → abort.
- A `running` row whose lock is free is marked `interrupted` by whoever notices.

**`run`:**
- Phases in this order: files → jobs → sessions → orphans (`--all`) → finish.
- Batches are bounded: at most 25 sessions, 2,000 messages or 20 MB of text, and
  200 file or job records.
- Each batch is one transaction. It writes the target rows, `import_records` and the
  checkpoint `{phase, index}`.
- Each source record is written inside a SAVEPOINT. On `BrainError`, `DataError`,
  `IntegrityError` or a cleaning failure, that record is rolled back and recorded as
  `excluded` with reason `invalid record: <ExceptionClass>`, and the run continues.
- On another exception: `failed`, with `error` = `<Class> in <phase> at <source_key>`.
- On SIGTERM or SIGINT: `interrupted`. The checkpoint stays either way.
- The finished run stores `counts`, `report` (2.8) and
  `source = {db: {name, bytes, sha256, wal_present: false}, profile: {manifest_sha256, files}, columns_read: {...}, pre_backup_sha256}`.

**`resume`:**
- It refuses unless `source.db.sha256` and `source.profile.manifest_sha256` match.
- It continues after the checkpoint. It is idempotent, because of the unique keys.

**`verify`** recomputes the selection, then checks the following:

1. Every selected source record has exactly one record in this run, or a `duplicate`
   pointing at an earlier run.
2. The reverse set is empty: no row with `import_run_id = run` lacks a record.
3. For every session: source rows = imported + withheld + excluded + duplicate.
4. With `--all`, the total over all runs equals the backup's message count, orphans
   included.
5. No TEXT or JSONB column of **any** brain table row created or updated by the run
   matches `SECRET_PATTERNS`, `text.RESTRICTED_PATTERNS` or the chat-secret rules, or
   holds a high-entropy token. That covers items and their versions, sessions,
   messages and their tool calls, conflicts, drafts and the run's own report.
6. No withheld message or restricted session has text.
7. `columns_read` holds no reasoning or system-prompt column.
8. Full-text search finds a sample of the imported messages and items.

It writes `report.verification`.

**`rollback`.** It refuses (exit 2, listing ids) when:
- a later run that is not rolled back has records whose targets belong to this run,
  or messages in this run's sessions. Roll back in reverse order.
- any of the run's items was reviewed or edited since (version > 1, or a status
  other than `pending_review`/`quarantined`, or deleted), unless
  `--discard-reviewed` is given. The count is shown before confirmation.
- any of its conflicts was resolved with an effect whose target changed again
  since (its version moved on).

Otherwise it does all of this in one transaction:
- reverts each resolved conflict's `effects`, restoring the before version or status
  as a new `reverted` version;
- restores `import_records.before` for every `updated` row;
- deletes, in order, the run's conflicts, drafts (except converted ones, which are
  reported: their disabled schedules belong to the Command Center), items, messages
  and the sessions it created;
- recomputes every derived field of the sessions it touched but did not create:
  counts, labels, sensitivity and summary. Source fields come back from `before`.
- verifies that no row with `import_run_id = run` remains (converted drafts
  excepted), and that each touched session's counts equal its remaining rows;
- sets `rolled_back` with the deleted counts in `report.rollback`;
- writes the audit entry `brain.import_rolled_back` (ids and counts only).

Items promoted from archive messages have `import_run_id = NULL` and survive a
rollback. Their `source_ref` then reads "source removed".

`rollback_run(session, run_id, actor, discard_reviewed=False) -> dict` is also
called by the API.

**`rescan`** re-cleans every stored brain text and JSONB value in place with the
current detectors:
- changed items get a `redacted` version;
- messages that are now restricted withhold their session;
- older versions' text is re-cleaned too.

It reports counts and writes the audit entry `brain.rescanned`.

**`decisions-export` / `decisions-apply`.**
- Export writes `{source_key, content_hash, status, kind, agent, title, body, tags,
  reviewed_by, review_note}` for every imported item a person reviewed or edited in
  this database.
- Apply, after the production import, applies a decision only where the
  production item has the same `source_key` **and** the same `content_hash` as at
  export time. The change is a version with note `decision carried over from the
  test copy`.
- Anything else is listed, not applied.
- Admin-only decisions keep needing admin: apply runs as an admin actor given with
  `--actor`.

**Audit actions:** `brain.import_started`, `brain.import_completed`,
`brain.import_failed`, `brain.import_rolled_back`, `brain.rescanned`,
`brain.decisions_applied` (actor `--actor`, default `importer (cli)`).

### 2.2 Sessions and messages

**Columns.** Probe with `PRAGMA table_info` and read only these:
- sessions: `id, title, display_name, source, started_at, ended_at, end_reason,
  parent_session_id, model_config, message_count, tool_call_count, archived`;
- messages: `id, session_id, role, content, tool_name, tool_call_id, tool_calls,
  timestamp, active, compacted, message_uid`.

| Topic | Rule |
|---|---|
| Keys | `jarvis:session:<id>`. Messages: `jarvis:message:uid:<message_uid>` when that column exists, else `jarvis:message:<session_id>:<id>`. |
| `source_fp` | sha256 of (role, content, tool_name, tool_calls, active, compacted) |
| Order | `ORDER BY id`, with timestamp only as a tiebreaker. `position` = 0-based index among **all** the session's source rows (stable across backups). |
| Staff | Session id matches `CRON_SESSION` → job id → `cron/jobs.json` (`id` → `name`; accept both `{"jobs":[…]}` and the setup marker shapes) → `templates.json` `cron_jobs[name].role`; else `jarvis`. `agent = STAFF_AGENT[staff]`. `origin` is `scheduled` or `chat`. Attribution is stored as `flags.attribution = 'job id' \| 'default'`. |
| Channel | `sessions.source` through `kit.label()` |
| Lineage | `_delegate_from` → `delegated`; `_branched_from` or a parent with end_reason `branched` → `branch`; a parent with `compression` → `continuation` |
| Times | A number or numeric string → seconds if < 1e11, milliseconds if < 1e14, microseconds if < 1e17, else NULL. Naive ISO text → the customer `timezone` setting, with `flags.time_assumed_local`. Unparsable → NULL. The sort key is `(started_at IS NULL, started_at, id)`. |
| Excluded | `active = 0` with `compacted` 0, NULL or absent → `rewound or superseded`. No content, no tool calls and no tool name → `empty`. Role `system` → `system prompt`. Orphans (`--all`) → `no session`. A tombstoned key → `purged by an admin`. |
| Compaction | `compacted = 1` → state `compacted`. A live row whose (role, tool_name, content hash, tool_calls hash) equals a compacted row of the same session → `duplicate`, reason `carried-on copy of a compacted message`. |
| Adjacent duplicates | For user and assistant rows only: (role, tool_name, tool_call_id, content hash, tool_calls hash) equal to the previous user or assistant row → `duplicate`. Tool results are never de-duplicated. |
| Content | User and assistant: cut to 200,000 characters, `clean(…, "chat")`, cut to 50,000. Tool results: cut to 20,000, then cleaned, then cut to 4,000 (`truncated`). |
| `tool_calls` | Parse both the OpenAI (`function.name/arguments`) and the flat (`name/arguments`) shape → `[{name, arguments}]`. Arguments are cleaned as JSON or chat and cut to 500. The message's sensitivity is the highest of its content and arguments. A restricted argument restricts the message. |
| Customer by default | Staff `sarah`, or a tool name matching the customer pattern (0.5) → at least `customer` |
| Restricted | Any restricted message → **the whole session withheld** (0.4): every message `withheld`, no text, labels empty; title and summary `''`; action `withheld` with reason `restricted data in this conversation: <types>` |
| Labels | `labels_for(role, cleaned, tool_names=…, tool_error=…)`; none for withheld, duplicate or excluded rows |
| Session derived fields | Counts; `labels` as counts per label; sensitivity = highest message level (restricted > customer > general); summary = first user message (one line, 200) ` → ` last assistant message (one line, 300), from imported, non-withheld rows; title cleaned (`""` if restricted) |

**Re-import (a newer backup):**
- The same message key with the same `source_fp` → `duplicate`.
- A changed `source_fp`:
  - now compacted → the row is updated (`updated`, with `before` = the old row's
    fields);
  - now rewound or superseded → the row is deleted (`updated`, reason
    `rewound since an earlier import`, `before` = the whole row);
  - content changed → re-cleaned and updated with `before`.
- An existing key whose session differs → `excluded`, reason `key collision` (no
  overwrite).
- Sessions that grew get their new messages, and their derived fields are
  recomputed. Their source fields (title, ended_at, end_reason, counts) are updated
  with `before`.
- Sessions in the archive but missing from the new backup are counted in the report
  (`missing_from_source`). They are never deleted.

### 2.3 Profile files → memory items

**Common fields.** Every item has `origin = 'import'` and `status = 'pending_review'`.
The status is `quarantined` when any secret was redacted, or when a risk flag is set
(instruction-like, approval conflict, script, plugin, custom, owner profile).
- `source_system = 'jarvis'`.
- `source_ref`: the profile-relative path, plus `#entry` or `#role <id>`.
- `source_key = jarvis:file:<area>/<rel>`. Memory entries get
  `#entry-<first 16 hex of sha256 of the normalised entry>`; role blocks get
  `#role-<id>`.
- `source_sha256` of the raw bytes of that part.
- `source_at`: the file mtime, or the date in a report's file name.
- Sensitivity is always passed from `clean()`, raised to `customer` by the rules in
  0.5.

**Reading and cleaning:**
- Walk with `kit.walk`, classify with `kit.classify`, read with
  `kit.read_file`/`as_text`, compare with `kit.compare_with_templates`.
- Clean Markdown and text with `"chat"`, code and shell with their kind, config with
  `"config"`.
- Cut bodies to `store.MAX_BODY` with `flags.truncated`.
- Titles are cut to 300 characters. An empty title falls back to
  `<file name> entry N`.

| What | Becomes |
|---|---|
| `knowledge/*.md` (Jarvis) | `business` for `product`, `targets`, `org`; else `knowledge`; `agent = ''` |
| `knowledge/<role>/*.md` | `business` for `faq`, `tone`, `refund-policy`, `leads`, `proposal-instructions`; `instruction` for `autopilot`; else `knowledge`; `agent = STAFF_AGENT[role]`. `knowledge/websites.md` → `knowledge` for `monitoring`, plus site conflicts (2.5) |
| A template copy that still has placeholders (`identical` or `same content as a template`, and `PLACEHOLDER_MARKERS` > 0) | excluded: `unfilled template` |
| An identical shipped default without placeholders | imported with `flags.template = 'shipped default'` |
| `memories/MEMORY.md` | One `knowledge` item per `§` entry (else per list item), `agent = ''` |
| `memories/USER.md` | One `business` item per entry, `flags.owner_profile = true`, `sensitivity = 'customer'` |
| `SOUL.md`, `AGENTS.md`, `.hermes.md` | `instruction` for `supervisor` |
| `operations.md` | Each `<!-- jarvis-setup:role:<id>:start -->` … `end -->` block → `instruction` for `STAFF_AGENT[id]`; the rest → `instruction` for `supervisor` |
| `skills/<name>/SKILL.md` | `skill`. Agent and flags from `SKILLS`: `skill, staff, capabilities, compatibility, note, frontmatter{name, description, version}` (frontmatter values cleaned). An unknown skill → `supervisor`, `reference_only`, `flags.custom = true`. |
| `plugins/**`, `hooks/**` (text) | `skill`, `flags.plugin_or_hook = true`, `executable = false`, agent `supervisor` |
| `scripts/**` (text) | `skill`, `flags.script = true`, `executable = false`, `language`; agent by staff path |
| `workspace/reports/*.md`, `workspace/briefs/*.md` | `project`. The agent comes from the file-name prefix role (`sarah-` → communications and customer by default, `scout-`, `ava-`, `bobby-`, `tom-`); else `supervisor` |
| `workspace/proposals/sent-log.jsonl` | One `project` item, "Proposals JARVIS sent": counts per month and status only |
| `cron/jobs.json` | Workflow drafts (2.4) |
| Everything else | excluded with a reason: `setting names only`, `workspace state`, `binary`, `large template file` (an .html file over 200 KB: copy it to `templates/` by hand), `not imported (<category>)` |

**More rules for files:**
- **Restricted data.** A file whose text is restricted is excluded with
  `restricted data`.
- **Instructions.** Instructions whose `instruction_flags` include `approval_bypass`
  or `autonomous_action` get `flags.approval_conflict = true` and a conflict of kind
  `instruction`, title `JARVIS rule that bypasses approval (<path>)`, with
  `incoming_item_id`.
- **Same key:**
  - with the same `source_sha256` → `duplicate`;
  - with a different hash → an **incoming item** with `source_key = NULL`,
    `flags.replaces_source_key`, status pending or quarantined, holding the full
    cleaned text. A conflict of kind `source_changed` links `existing_id` and
    `incoming_item_id`.
  - If the existing item's `flags.rejected_source_sha256` already lists this hash,
    nothing is created: `duplicate`, reason `incoming version rejected before`.
- **Content duplicates.** The same `content_hash` as an existing, non-deleted item →
  `duplicate`.
- **Same title.** The same kind, agent and title (case-insensitive) with different
  content → the incoming item, `flags.conflict = true`, plus a conflict of kind
  `memory_item`.
- **Tombstoned keys** → `excluded`, reason `purged by an admin`.
- **Conflict dedupe.** `dedupe_key = sha256(kind, source_key or URL, incoming
  sha256)`. An existing conflict (in any status) → no new conflict.
- **Content folder.** `--content` text files that are `not from a template` →
  `knowledge` with `agent = ''` and `flags.from_content_folder`. Template matches are
  excluded with `template`.

### 2.4 Workflow drafts

There is one draft per job in `cron/jobs.json`. The key is `jarvis:job:<id>` (else
`jarvis:job:name:<name>#<index>`). The website watchdog's key is
`jarvis:watchdog:check_websites`.

| Field | Value |
|---|---|
| Mapping | `JOBS[name]`. An unknown job → `supervisor`, `reference_only` |
| `cron` | `schedule.expr` when it is valid for `kit.CRON_EXPR` and `croniter`, else `''` |
| `schedule_display` | As given |
| `timezone` | The customer `timezone`. The notes say: "JARVIS ran cron in the PC's local time; Command Center schedules run in UTC". |
| `enabled_in_source` | The job's `enabled` |
| `deliver` | Platform prefix only, never a number or channel id |
| `spec` | The job's prompt plus the mapped skill's text, cleaned with `"chat"`, at most 20,000 characters |
| `approval` | `{jarvis: <cron_mode from config.yaml, else "unknown">, jarvis_rules: <instruction flags of spec>, command_center: "needs approval (<risk>)"` or `"runs without approval (read or draft only)"}` |
| `error_handling` | `{jarvis: ≤ 5 cleaned one-line sentences of the skill text mentioning error, fail, retry, stop, budget or blocker, command_center: "one attempt; a failure is shown in Automation Logs, and the next scheduled run tries again"}` |
| `duplicate_guard` | `{jarvis_job_enabled, overlaps, rule: "The draft never runs. Converting it creates a disabled schedule. Switch the JARVIS job off before enabling that schedule, so the work is not done twice."}` |
| `history` | From the **source database**: every session with this job id, whatever the sample. `{runs, first, last, with_problems, last_runs: 5 dates, from: "the JARVIS backup"}` |
| `notes` | `JOBS[name].note`, overlaps, timezone |
| `source_fp` | sha256 of (name, cron, enabled, deliver, prompt) |

**Re-import:**
- The same key and `source_fp` → `duplicate`.
- A different one → a conflict of kind `workflow` holding the incoming draft as JSON.
  The draft is never overwritten, and a converted draft is never touched.

**Overlaps** with an existing Command Center automation add a conflict of kind
`schedule`.

**The website watchdog** (`scripts/check_websites.py` present) becomes:
- `monitoring.check_all_sites`, compatibility `mapped`;
- overlap: "the wordpress-uptime-sweep schedule already checks every registered site
  every 5 minutes".

### 2.5 Sites

The URLs in `knowledge/websites.md` are normalised to `https://host/path` and
compared with `sites.url`:
- a URL that is already registered → recorded as `duplicate`;
- otherwise a conflict of kind `site`, titled `JARVIS watched <host>`.

`use_incoming` on that conflict (operator) registers the site **paused**
(`enabled = false`, `environment = 'production'`). It respects the edition's site
limit and is recorded in `effects`.

### 2.6 What counts as a source record

These each get exactly one record per run:
- every session;
- every message of a selected session;
- every orphan (`--all`);
- every walked profile and content file, including excluded ones;
- every memory entry and role block;
- every job;
- every website URL.

### 2.7 Tests

**Test files:**
- `backend/tests/integration/test_brain_import.py`
- `tests/integration/test_brain_rollback.py`
- `tests/unit/test_brain_text.py` (extend)

**Fixture.** `migration/jarvis/tests/make_fixture.py`, by path. Never change its
built counts. Add rows to **your copy** of the fixture database inside the tests.
The tests also add a brain-specific planted set: the review's leaked phrasings, a
NUL in a tool result, a 250 KB knowledge file, a blank-first-line entry, carried-on
copies, parallel identical tool results, a WAL file, a naive ISO time and a
`message_uid` column.

**Required coverage:**
- `plan` writes nothing (row counts of every table are unchanged);
- a sample run, then a re-run that adds nothing;
- an incremental re-import from a modified copy (grown session, compacted and
  rewound rows, a changed file, a changed job, a deleted session);
- resume after a failure forced in the middle of a batch;
- a per-record failure is excluded and the run continues;
- liveness: a second run is refused, and rollback is refused while the lock is held;
- `verify` passes, and fails on a planted secret, on an orphan row and on a count
  mismatch;
- rollback exactness: counts back to the pre-run values, a shared session's derived
  fields restored, conflict effects reverted, refusal with later dependent runs and
  with reviewed items;
- no planted value in any brain table (every text and JSONB column), in stdout or in
  `import_runs.error`;
- withheld sessions hold no text;
- masking, customer by default, and USER.md entries as customer;
- staff attribution, lineage and timestamps;
- every conflict kind and its dedupe;
- tombstones;
- drafts never enabled, and no schedule created by an import;
- WAL refusal and the discovery-count mismatch refusal;
- the source files are unchanged;
- CLI exit codes;
- decisions export and apply.

### 2.8 The report (`plan`, `run`, `verify`)

`report = {selection, totals: {sessions, messages, files, jobs, urls}, by_action, by_reason, by_type, by_agent, by_staff, by_kind, by_sensitivity, secrets: {types: {type: count}, where: {messages, files, drafts}}, restricted: {types, sessions_withheld, files_excluded}, masked: {email_address, phone_number}, conflicts: {kind: count}, drafts: [{name (template job name or "custom job N"), compatibility, enabled_in_source}], time_range: {first, last}, missing_from_source, discovery_check, verification?, rollback?}`.

It holds no message text, no titles and no file names except template paths. The
credential rotation list is `secrets.types`, shown as "rotate these kinds of
credential".

## 3. API (`backend/app/api/brain.py`, `backend/app/brain/review.py`)

**Router and errors:**
- `APIRouter(prefix="/api/brain")`, added to `main.py`.
- The person's scope is `PersonScope(role, user.can_access_sensitive)`, loaded from
  `users`.
- Every mutation writes `audit.record(...)` with **ids, kinds, statuses and counts
  only**, then commits.
- `BrainError` → 400. Out of scope or missing → 404. Role refused → 403.

**Output shapes.**
- `MemoryItemOut` = `{id, kind, kind_label, agent, title, body, tags, sensitivity,
  status, origin, flags, source_system, source_ref, source_at, import_run_id,
  version, created_at, created_by, updated_at, updated_by, reviewed_by, reviewed_at,
  review_note, deleted_at, needs_admin}`.
- `ArchiveSessionOut` = the `archive_sessions` columns except `search`.
- `ArchiveMessageOut` = `{id, session_id, position, role, content, truncated,
  tool_name, tool_calls, at, state, sensitivity, withheld, labels, redactions,
  flags}`.
- `ImportRunOut` = every column except `report`, which comes only with the single
  run.

| Route | Who | Behaviour |
|---|---|---|
| `GET /overview` | user | `{memory:{total, by_status, by_kind, by_agent}, archive:{sessions, messages, withheld_messages, first_at, last_at, by_staff}, review:{pending_items, quarantined_items, open_conflicts}, workflows:{drafts, by_status}, imports:{runs, latest: ImportRunOut\|null}, ai_context, instance_label}`. Counts follow the person's scope; viewers get archive totals only. |
| `GET /items` | user | `q, kind, agent, status, include_deleted (admin), limit ≤ 200, offset` → `{items:[MemoryItemOut + excerpt + rank], total, filtered}` |
| `GET /items/{id}` | user | `{item, versions}`. Versions only for operators and admins. |
| `POST /items` | operator; admin when `needs_admin` for the result | `{kind, title, body, agent?, tags?}` → `{item}`, `memory.created` |
| `PATCH /items/{id}` | operator; admin when the old **or** new state `needs_admin`; moving into or out of `credit_repair` needs admin + sensitive | `{title?, body?, tags?, kind?, agent?, note?}` → `memory.edited` |
| `POST /items/{id}/review` | operator; admin when `needs_admin` | `{decision: approve\|reject\|archive\|reopen, note?}` → `memory.<decision>` |
| `DELETE /items/{id}` | admin | soft delete → `memory.deleted` |
| `POST /items/{id}/restore` | admin | `memory.restored` |
| `DELETE /items/{id}/purge` | admin | only deleted items. Deletes the item, its versions and its conflicts, and writes a tombstone for `source_key` → `memory.purged` |
| `POST /items/{id}/versions/{version}/redact` | admin | Blanks that version's title and body (a `redacted` marker) → `memory.version_redacted` |
| `GET /search` | user | `q, scope=all\|memory\|archive, kind, agent, staff, label, since, until, limit ≤ 50` → `{memory:{hits,total}, archive:{hits:[{message, session:{id,title,staff,agent,started_at}, excerpt}], total}, filtered}`. Archive results only for operators and admins. |
| `GET /sessions` | operator | `q, staff, agent, origin, label, since, until, limit ≤ 200, offset` → `{sessions, total}` |
| `GET /sessions/{id}` | operator | `limit ≤ 500, offset` → `{session, messages, total, hidden}`. `hidden` = messages outside the scope. |
| `POST /messages/{id}/withhold` | admin | content `''`, `withheld`, flags note; session derived fields recomputed → `archive.message_withheld` |
| `POST /messages/{id}/promote` | operator | `{kind, title, agent?}` → a pending item: origin `promoted`, `import_run_id NULL`, `source_ref = "JARVIS session <source id> · message <position>"`, sensitivity = the highest of the message and its session → `memory.promoted`. A withheld message → 400. |
| `GET /conflicts` | operator | `status` → `{conflicts, total}` |
| `POST /conflicts/{id}/resolve` | operator (`instruction`, `source_changed` and `workflow`: admin) | `{resolution, note?}` → `memory.conflict_resolved`; see the resolution rules below |
| `GET /workflows`, `GET /workflows/{id}` | operator | `{drafts}` / `{draft, schedule_exists}` |
| `PATCH /workflows/{id}` | admin | `{params?, cron?}`. Params are validated by the capability model, cron by `valid_cron` → `workflow_draft.updated` |
| `POST /workflows/{id}/review` | operator | `{decision: reviewed\|rejected\|draft, note?}` → `workflow_draft.reviewed` |
| `POST /workflows/{id}/convert` | admin | `{jarvis_job_is_off: bool}` → `{draft, schedule}`, `workflow_draft.converted`; see the conversion rules below |
| `GET /agents` | user | `{agents:[policy.describe() + {used_by: [...], items:{approved, by_kind}, sessions}]}` |
| `GET /skills` | user | `{skills:[MemoryItemOut]}` of kind skill, through `person_items` (viewers: none) |
| `GET /imports` | operator | `{runs:[ImportRunOut]}` |
| `GET /imports/{id}` | operator | `{run (with report), records_by_action, records_by_type, can_roll_back, reviewed_items}` |
| `POST /imports/{id}/rollback` | admin | `{confirm: "<id>", discard_reviewed?: bool}` → `importer.rollback_run` → `{run}`. A wrong confirm → 400 `confirm: type the run number` |
| `GET /export` | admin | `include_customer?` → JSON of approved items: general only, unless asked |

`used_by` lists where an agent actually reads memory: `"task results (reference
notes)"` for every agent except Credit Repair, plus `"AI drafting prompts (when
allowed)"` for SEO & Content, Social Media & Marketing and Email & Customer
Communications, plus `"answers about JARVIS's history"` for the Supervisor.

**Conflict resolutions.** Only `open` conflicts can be resolved, and each resolution
records `effects`.

| Resolution | Effect |
|---|---|
| `keep_existing` | The incoming item is rejected. For `source_changed`, its sha256 is added to the existing item's `flags.rejected_source_sha256`. |
| `use_incoming` (`memory_item`) | The existing item is archived (effect: its previous status). The incoming item stays pending. |
| `use_incoming` (`source_changed`) | The existing item is edited to the incoming item's full title and body. Its `source_sha256` and `source_at` are updated, and it goes back to pending (effect: its previous version). The incoming item is purged. |
| `use_incoming` (`instruction`) | The item stays a pending reference. |
| `use_incoming` (`site`) | The site is registered paused (2.5). |
| `use_incoming` (`workflow`) | The draft is replaced by the incoming JSON when it is not converted (effect: the old draft JSON). |
| `keep_both`, `dismissed` | No change to the items. |

**Workflow conversion.** It refuses:
- `reference_only`;
- a draft that is not `reviewed`;
- an empty or invalid cron;
- parameters the capability rejects;
- an existing schedule named `jarvis-<draft name>`;
- `enabled_in_source` without `jarvis_job_is_off`.

It creates `Schedule(name="jarvis-<name>", agent, capability, params, cron,
enabled=False, description="From JARVIS job <name> (draft <id>); created disabled")`
and sets `status = 'converted'` and `schedule_id`.

**Customer setting.** `brain_ai_context`: `Spec("JARVIS Brain in AI prompts", ...,
"choice", choices=("off", "approved_general"))`, default `"off"`.

**Instance label.** `CC_INSTANCE_LABEL` (deployment setting, default empty) appears
in `/api/system/status` and in `/overview`, for the test copy's banner.

**Session cookie.** `SESSION_COOKIE_NAME` (deployment setting, default `cc_session`)
lets the test copy use `cc_session_brain_test`.

## 4. Agents (`agents/base.py`, `worker/runner.py`, `agents/drafting.py`, `supervisor/`, `api/work.py`, `api/supervisor.py`)

- **Context.** `AgentContext.memory: AgentMemory | None = None`. `Worker.execute`
  sets `AgentMemory(session, agent_name)` for agents with a policy, and `None`
  otherwise. It is read-only.
- **Reference notes (every agent except Credit Repair):**
  - After a capability succeeds, the worker adds
    `result.data.memory_notes = [{id, kind, title}]` (at most 5). These are the
    approved, **general** items in the agent's scope that match the task's topic
    words, taken from string params such as url host, topic, title and category.
  - Nothing is added when nothing matches.
  - This is local, needs no AI and changes nothing the agent does.
  - The task view shows them as "Reference notes from memory".
- **Drafting.** `drafting.write(..., memory_query: str | None = None)` prefixes the
  prompt with `await ctx.memory.prompt_block(memory_query)` when non-empty.
  - In that case `MEMORY_NOTE` is appended to the system prompt and
    `ctx.log("used N memory item(s) in the prompt")` is called.
  - The queries come from the operator's parameters, never customer text:
    - `blog_draft`: topic;
    - `social_drafts`: topic;
    - `support_reply`: **category only**;
    - `keyword_brief`: topic.
- **`supervisor.recall`:**
  - `@capability(risk=Risk.READ, params=RecallParams{question: str 1..500, limit:
    int = 8, 1..30})`.
  - Intents (weight 2): "jarvis"; "previously" or "in the past"; "what did … do or
    accomplish"; "what problems or decisions".
  - It reads **general-only** (`general_only=True`):
    - archive messages labelled accomplishment, problem or decision that match the
      question's topic words;
    - when nothing matches, the newest, saying "nothing matched the topic words;
      showing the newest";
    - approved items of kinds decision, accomplishment, problem, project and
      business.
  - The answer is Markdown with three sections. Each bullet carries the date, staff,
    excerpt (≤ 240) and source (session title or id · message position).
  - It also states the coverage ("searched N imported sessions from X to Y") and
    that the labels come from keyword matching.
  - When nothing has been imported, it says so.
  - `result.summary` holds counts only.
  - `result.data = {local_only: true, answer: [≤ 15 lines], counts, coverage}`.
  - It writes **no artifact**.
- **Summary.** In `summarize_request`, when any task's `result.data.local_only` is
  true:
  - no model is called;
  - `template_summary` appends those tasks' `answer` lines;
  - `request.plan["local_only"] = true`;
  - `_task_digest` never carries `answer`.
- **Viewers.**
  - `GET /api/tasks`, `/api/tasks/{id}` and `/api/supervisor/requests*` remove
    `result.data.answer` and `memory_notes` for viewers.
  - For a request with `plan.local_only`, viewers get the summary line "Answer about
    JARVIS's history (visible to operators and admins)".
- **Untouched.** The planner prompt and the daily briefing do not change.

## 5. Dashboard (`dashboard/src/pages/Brain.tsx`, `Brain.css`, `api.ts`, `shell/nav.ts`, `icons.tsx`, `App.tsx`, tests)

**Route and banner:**
- Route `/brain`, titled "JARVIS Brain".
- It goes in `OPS_NAV` after "AI Supervisor", with a new `brain` icon, visible to
  every signed-in user. The API applies the scopes. Viewers see Overview, Search
  (memory only), Agent knowledge and Skills.
- When `/api/system/status.instance_label` is set, a fixed banner across the shell
  reads: "TEST COPY · <label> · not your live Command Center".

**Tabs** (kept in `?tab=`):

| Tab | Contents |
|---|---|
| Overview | Import status, counts, pending, quarantined, conflicts, the AI-context setting with a link to Settings |
| Search | Memory and archive hits with sources; open an item, or a message in its session |
| Conversations | Sessions with filters; open the messages. A withheld session reads "Withheld: this conversation contained restricted data; it is kept only in the JARVIS backup". Customer messages hidden from operators show "hidden: customer data (admins only)". |
| Agent knowledge | Per agent: what it reads, `used_by`, counts |
| Skills | Mapping and compatibility |
| Workflows | Disabled drafts, with schedule, trigger, approval, error handling, duplicate guard, history and notes. Review; convert (admin) with the "the JARVIS job is switched off" confirmation. |
| Review | Quarantined (admin) and pending items: approve, reject, edit. Open conflicts: resolve. |
| Import history | Runs: counts, verification, credential rotation list (types), roll back (admin; typed run number; shows reviewed-item counts and the discard option) |

**Item modal:**
- text, flags as plain-English chips, source, versions (operators and admins);
- edit; delete, restore, purge and redact version for admins;
- promote and withhold on messages.

**Honesty:**
- loading is not empty;
- "No JARVIS import yet" before any import;
- windows and limits are stated;
- labels read "matched by keywords";
- a "customer data (masked)" pill;
- "quarantined: held a secret (removed) or an instruction-like rule; an admin must
  review it".

**Tests (Vitest):**
- every tab;
- viewer, operator and admin controls;
- the rollback typed confirm;
- the convert confirmation;
- the empty state;
- the banner;
- the nav entry and title.

The KB page gets a JARVIS Brain article and an `AREAS` row.

## 6. Isolated test instance, importer container, Windows scripts

### 6.1 Folder layout and sequence (owner)

1. **Folders.** The live install stays where it is. 0.6.0 is extracted into a **new**
   folder, `C:\InfiniteAI\v0.6.0-brain-test\InfiniteAI-CommandCenter`, which has no
   `.env`. The test copy runs only from there.
2. **Before the test.** `scripts\brain-precheck.ps1` reads the live install,
   read-only, and records a fingerprint (6.4).
3. **The test copy.** `scripts\brain-test.ps1 -Action Start [-SeedFromBackup <folder
   of a backup.ps1 run>]` starts it.
4. **The staged import.** `scripts\jarvis-import.ps1 -Target Test …`: plan, then
   sample 5, then review, then about 10 %, then review, then `-All`.
5. **The owner reviews** in the test copy at `http://127.0.0.1:8190`. Review
   decisions are exported with `-Mode ExportDecisions`.
6. **Approval.** The owner approves the production upgrade (a separate step): backup,
   install 0.6.0 from a new folder, then smoke.
7. **The production import.**
   - `-Target Production -Mode Plan`, then `Import` (typed confirmation; a
     `command_center`-only verified dump first), then `Verify`, then
     `ApplyDecisions`.
   - Converted schedules stay disabled until the owner switches JARVIS's jobs off
     and enables them.
8. **Clean-up.** `brain-test.ps1 -Action Remove`. Delete
   `C:\InfiniteAI\JarvisMigration\restored` (plaintext) and `.env.brain-test`.

### 6.2 `docker-compose.brain-test.yml` (an overlay, used only by `brain-test.ps1`)

- **Project.** `name: cc-brain-test` at the top. That way a command without `-p`
  still never resolves to production.
- **Images.** `api`, `worker` and `dashboard` use
  `infinite-command-center-brain-test/{backend,dashboard}:local`. `pull_policy:
  never` is set on the worker.
- **Dashboard port.** `ports: !override ["127.0.0.1:${BRAIN_TEST_PORT:-8190}:8080"]`.
- **Worker network.** `networks: !override [internal]` (no internet).
- **n8n.** `profiles: ["never"]`.
- **Restart policy.** `restart: "no"` on every service.
- **Environment.** `WORKFLOW_ENGINE: none`, `LLM_PROVIDER: rule_based`,
  `ANTHROPIC_API_KEY: ""`, `N8N_EDITOR_URL: ""`,
  `CC_INSTANCE_LABEL: "cc-brain-test"`, `SESSION_COOKIE_NAME: cc_session_brain_test`.
- **Not what keeps AI off.** AI is kept off by the network and by fresh keys. A
  seeded `ai_provider` row cannot reach the internet.

### 6.3 `scripts/brain-test.ps1 -Action Start|Status|Stop|Remove [-SeedFromBackup <folder>]`

- **Compose call.** Always `docker compose -p cc-brain-test -f docker-compose.yml -f
  docker-compose.brain-test.yml --env-file .env.brain-test`. It never sets
  `COMPOSE_*` variables.
- **Checks before every action.** It refuses:
  - Compose older than 2.24.4 (the version is parsed with a regex; Docker Desktop
    suffixes are fine);
  - a folder that is the live install: `.env` present, or the folder equals the
    `com.docker.compose.project.working_dir` label of the running
    `infinite-command-center` api container;
  - unless `docker compose … config --format json` shows: name `cc-brain-test`;
    worker networks exactly `internal`; the only published port `127.0.0.1:<port>`;
    no n8n service; every image under `infinite-command-center-brain-test/`.
- **`.env.brain-test`.** Written on first start with fresh secrets, never `.env`. It
  refuses if any value equals the live install's `.env` value for the same key (read
  from the live folder found through the container label, when available).
- **Start.**
  - Runs `prepare-build.ps1` and passes `CC_RELEASE_VERSION`.
  - With `-SeedFromBackup`:
    1. starts postgres and db-init only;
    2. drops and recreates the `command_center` database;
    3. `pg_restore` (no `--clean`) of the backup's `command_center.dump`;
    4. in one transaction: disables every schedule; cancels queued, running and
       awaiting-approval tasks; rejects pending approvals with note `test copy`;
       deletes sessions and secret values; marks pending events `skipped`;
    5. then starts api, worker and dashboard.
  - Waits for health and checks `/api/system/status` against `VERSION`.
  - Prints `http://127.0.0.1:<port>` and the sign-in. After a seed, that is the
    production accounts.
- **Remove.** Typed confirmation `REMOVE TEST COPY`, then `down --volumes
  --remove-orphans` for `cc-brain-test` only, then the test images are removed.
- **Fingerprint.** Every action prints the production fingerprint before and after
  (6.4) and fails loudly if it changed.

### 6.4 Production fingerprint and pre-check (`scripts/brain-precheck.ps1`, read-only)

**Fingerprint:**
- image IDs of `infinite-command-center/backend:local` and `dashboard:local`;
- for each container labelled `com.docker.compose.project=infinite-command-center`:
  name, ID, image ID, StartedAt;
- the volume names with that label.

**Pre-check report** (written to `C:\InfiniteAI\JarvisMigration\precheck-<time>.txt`):
- the fingerprint;
- the live folder (from the label) and its `VERSION`;
- the API version (`docker compose exec -T api python -c "import app; print(app.__version__)"`);
- container health;
- `schema_migrations` and whether `0006_brain` is present;
- counts of agents enabled, schedules (enabled/total), sites, pending approvals and
  users;
- whether the n8n and postgres healthchecks pass.

It changes nothing. The same report is run again after the test to show nothing
changed.

### 6.5 Importer container (`docker-compose.brain-import.yml`)

**Service.** `brain-importer`, under `profiles: ["brain-import"]`:
- image `${BRAIN_IMPORT_IMAGE}`, set by the script to the image the running api uses;
- `entrypoint: ["python", "-m", "app.cli", "brain-import"]`;
- network `internal` only;
- environment **only**: `DATABASE_URL` (built from `CC_DB_PASSWORD`), `TZ`,
  `ENVIRONMENT`;
- `read_only: true`, `tmpfs: /tmp`, `cap_drop: [ALL]`,
  `security_opt: [no-new-privileges:true]`, `mem_limit: 3g`, `pids_limit: 256`;
- binds in long syntax, `read_only: true`, `bind: {create_host_path: false}`:
  - `${BRAIN_IMPORT_DB}` → `/jarvis/state.db`;
  - `${BRAIN_IMPORT_PROFILE}` → `/jarvis/profile`;
  - optional `${BRAIN_IMPORT_CONTENT}` → `/jarvis/content`;
  - `${BRAIN_IMPORT_REPORTS}` → `/reports` (writable, `create_host_path: false`).
- `restart: "no"`, plus a fixed container label `cc.brain-import=1`.

It is used with the test project or the production project:
`docker compose -p <project> -f docker-compose.yml -f docker-compose.brain-import.yml
--env-file <env> --profile brain-import run --rm --no-deps -T brain-importer …`.

### 6.6 `scripts/jarvis-import.ps1`

**Parameters.**
- `-Target Test|Production`
- `-Mode Plan|Import|Resume|Verify|Rollback|Status|ExportDecisions|ApplyDecisions|Rescan`
- `-BackupDb <file>`
- `-Restored <folder with profile\ and content\>`
- `-Sample N` / `-FilesSample M` / `-All` / `-Since` / `-Until`
- `-Run id`
- `-Discovery <discovery-summary.json>`
- `-Reports <folder>` (default `C:\InfiniteAI\JarvisMigration\import-reports`)
- `-DiscardReviewed`

**Checks.** It refuses:
- a database or profile inside `%LOCALAPPDATA%\hermes`;
- a non-empty `<db>-wal` or `<db>-journal`;
- missing folders (it creates nothing on the source side);
- when free space on the drive holding Docker's data, or on the system drive, is
  below 3 × the database size.

**Test target.** The project is `cc-brain-test` with `.env.brain-test`. The
production fingerprint is compared before and after.

**Production target:**
- Feature check: `brain-import status` exits 0 and prints `IMPORTER_VERSION`;
  `schema_migrations` holds `0006_brain`; the image of the running api equals
  `BRAIN_IMPORT_IMAGE`.
- Typed confirmations: `IMPORT INTO PRODUCTION` for Import and Resume,
  `ROLL BACK PRODUCTION` for Rollback.
- Before Import:
  - a `pg_dump -Fc` of **`command_center` only**, from the production postgres
    container found by project label;
  - checked with `pg_restore -l`;
  - its SHA-256 passed to `--pre-backup-sha256`;
  - its path printed.
- Undo is the importer rollback. Restore is disaster recovery: it loses everything
  since the dump.

**Paths** go in through the environment variables of 6.5. No path is written into a
`-v` string.

### 6.7 `testing/brain-check.sh` (sandbox, end to end)

It runs under PowerShell 7 in Docker:
1. the fixture and a copy of `command-center` without a `.env` (the test folder);
2. `brain-precheck.ps1`;
3. `brain-test.ps1 -Action Start`;
4. config assertions;
5. `jarvis-import.ps1 -Target Test`: Plan, then Import with sample 2, Verify, Import
   with all, Verify;
6. API checks: login on 127.0.0.1:8190 with the test cookie; overview; search;
   sessions; a recall through the Supervisor, waiting for the worker;
7. the worker cannot reach the internet: run `python -c "socket.create_connection(('1.1.1.1', 443), 3)"`
   in the worker container; it must fail;
8. restart of api, worker and postgres, then the data persists;
9. Rollback, then zero rows;
10. Remove;
11. the production fingerprint is unchanged throughout, and no planted value is in
    the test database (`pg_dump` of the brain tables, grep).
