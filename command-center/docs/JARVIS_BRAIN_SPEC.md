# JARVIS Brain: developer specification (0.6.0; withhold identity 0.6.2)

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
     summary are empty, and the session has `sensitivity = 'restricted'`. A row that is
     not stored (a system prompt, a rewound or a purged row) counts too.
   - A file holding restricted data, in its text or in its path, is excluded.
   - The data stays in the JARVIS backup. Credit Repair work belongs in the Credit
     Repair module.
5. **Customer data.**
   - E-mail addresses, phone numbers (including E.164 and WhatsApp ids) are masked as
     `[email]` and `[phone]`.
   - Text that already contains those stand-ins counts as masked.
   - These are always `sensitivity = 'customer'`:
     - any record with a masked value, a file whose path held one included;
     - every message of Sarah's sessions (staff `sarah`);
     - every message of a session from a customer-facing gateway channel (WhatsApp,
       SMS, e-mail, Telegram, Messenger and similar, `source.CUSTOMER_CHANNEL`; not the
       owner's own Slack, the CLI or cron), and the session itself
       (`flags.customer_channel`);
     - every message whose tool name looks like mail, WhatsApp, SMS, CRM, lead,
       invoice or client handling
       (`(?i)mail|smtp|imap|whatsapp|sms|message|lead|crm|contact|proposal|invoice|client|customer`);
     - the same work done through a generic tool: a message whose tool-call arguments
       (a terminal command, a script's options) or whose tool result's file paths name
       it (`source.CUSTOMER_WORK`: mail, SMTP, IMAP, himalaya, WhatsApp, SMS, lead,
       CRM, contact, proposal, invoice, client, customer as words or path parts, and
       Sarah's files and scripts);
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
python -m app.cli brain-import repair   [--yes] [--actor T]   # 0.6.2: the withhold identity repair (2.9); without --yes: what is pending
python -m app.cli brain-import fingerprints --forget-other-key [--yes]   # 0.6.2: purge fingerprints made with another DB password
python -m app.cli brain-import decisions-export --out FILE [--removals-only]  # review decisions keyed by source_key (test copy)
python -m app.cli brain-import decisions-apply  --in FILE --run ID --actor ADMIN_EMAIL [--yes]  # after the production import
python -m app.cli brain-import decisions-apply  --in FILE --removals-only [--actor ADMIN_EMAIL] [--yes]  # restore.ps1
python -m app.cli restore-carry export --out FILE                 # restore.ps1: users' access and the audit log
python -m app.cli restore-carry apply  --in FILE [--yes]          # restore.ps1: after pg_restore, before the start
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
- `heartbeat_at` is refreshed by a background task every 30 seconds. The same task
  checks (`pg_locks`, its own backend) that the lock connection still holds the run's
  lock; when it does not, or the connection fails, the run is cancelled and marked
  `interrupted`, since another import could start.
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
  `source = {db: {name, bytes, sha256, wal_present: false}, profile: {manifest_sha256, files}, columns_read: {...}, pre_backup_sha256, discovery_check}`
  (`source` is written when the run starts).

**`resume`:**
- It refuses unless `source.db.sha256` and `source.profile.manifest_sha256` match.
- It continues after the checkpoint. It is idempotent, because of the unique keys.
  A checkpoint at `finish` is past every phase: only the report is written.
- Its report keeps the run's `source.discovery_check` (resume takes no `--discovery`).

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
   messages and their tool calls, conflicts, drafts and the run's own report, and
   the run's `import_records`: `before`, and `reason` and `source_key` (keys made of
   the backup's row identifiers aside), which also hold no e-mail address or phone
   number.
6. No withheld message or restricted session has text.
7. `columns_read` holds no reasoning or system-prompt column.
8. Full-text search finds a sample of the imported messages and items.
9. (0.6.2) `withheld_identity_holds`: `brain_withhold_pending()` counts no readable
   row matching a withhold; every source row the identity rules (2.9) match in the
   run's sessions is stored withheld (by key, or by its JARVIS row for a re-keyed
   row); every `duplicate` record of the run whose target's key has a stone has one
   itself (restricted sessions aside).
10. (0.6.2) `withheld_hold_no_fingerprint`: no withheld row, and no rollback copy
    marked withheld, has a `source_fp` other than `w1:*` (or `''`, a released hold).
11. (0.6.2) `purged_stay_purged`: no item of the run has a purge-tombstoned key, a
    purged entry digest or a fingerprinted `source_sha256` (purges made by the end
    of the run).
12. (0.6.2) `database_guard_quiet`: no row stored or updated by the run carries
    `flags.withheld_by_admin.guard` (the trigger had to blank it: an importer bug).

It writes `report.verification` (that key alone, and only while the run is still
`completed`: a rollback during verify refuses the write).

**`rollback`.** It refuses (exit 2, listing ids) when:
- a later run that is not rolled back has records whose targets belong to this run,
  or messages in this run's sessions. Roll back in reverse order.
- a later run that is not rolled back has a record with the same
  `(target_table, target_id)` or the same `source_key` as one of this run's
  `updated` records: restoring `before` would leave that row in neither run's state.
  Roll back in reverse order.
- any of the run's items was reviewed or edited since (version > 1, or a status
  other than `pending_review`/`quarantined`, or deleted), unless
  `--discard-reviewed` is given. The count is shown before confirmation.
- any of its conflicts was resolved with an effect whose target changed again
  since (its version moved on), or whose before version was redacted since (a
  revert would make `[redacted]` the item's text): revert those by hand.

Otherwise it does all of this in one transaction:
- reverts each resolved conflict's `effects`, restoring the before version or status
  as a new `reverted` version, with the item's flags as they were (`flags_before`)
  and every risk flag the restored text calls for; a restored text stays `approved`
  only if it was approved with those risk flags, else it is `quarantined`. A hash a
  `keep_existing` decision added to `flags.rejected_source_sha256` is taken out;
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

Since 0.6.1:
- a row a rollback puts back (`deleted_row`, or an `updated` row) whose source key
  has an admin's withhold tombstone comes back without its text, flagged
  `withheld_by_admin`, keeping its sensitivity; its promoted items are emptied
  (`archive.withhold_copies`, cause `rollback`). The result and the audit entry
  add `withheld_reinserted` (a count);
- an item emptied because its source message was withheld (`flags.source_withheld`)
  never blocks a rollback and is never reverted by one: it keeps no text and stays
  as it is. The result and the audit entry list it in `kept_withheld_copies`
  (ids), outside `reverted`.

Since 0.6.2 (2.9): every row a rollback puts back is matched against the stones
without its text: its key has a stone, or its JARVIS row (session, row id, equal or
unknown role) is a stone's binding → back withheld as a member; it shares only a
stone's uid (same role) → back withheld as a hold (`review = 'pending'`, with a
pending stone on its key, so the holds list shows it and the next import compares the
text). Every withheld row and copy put back has `source_fp = 'w1:<state>'` (a
released hold keeps `''`). A `before` holding `source_key` (key adoption) restores the
old key. Stones and exemptions are never deleted: an inherited withhold is as
permanent as the admin's own. The database guard runs on every put-back and never
contradicts these rules.

`rollback_run(session, run_id, actor, discard_reviewed=False) -> dict` is also
called by the API.

**`rescan`** re-cleans every stored brain text and JSONB value in place with the
current detectors:
- changed items get a `redacted` version;
- messages that are now restricted withhold their session;
- the items promoted from the messages of a session it withholds are emptied, and
  so is any promoted item whose message is withheld but which still holds text
  (`archive.sweep_withheld_copies`), and every rollback copy of a withheld message
  that still holds it (`archive.sweep_withheld_records`); counts `copies_withheld`,
  `copy_versions_blanked` and `rollback_copies_blanked`, also in a dry run, whose
  changes are rolled back;
- older versions' text is re-cleaned too.

Since 0.6.2 it runs the withhold identity repair first (`identity.repair_rows`;
the Python part of the repair in its last pass).

It reports counts and writes the audit entry `brain.rescanned`.

**`decisions-export` / `decisions-apply`.**
- Export writes `{source_key, content_hash, status, reviewed_by, review_note,
  deleted, edited}` for every imported item a person reviewed or edited in this
  database. Text travels only for an edit: `title, body, tags, kind, agent` for an
  item whose content a person changed, and never for a customer or Credit Repair
  item, whose decision is marked `edit_not_exported` instead (apply lists it, to be
  edited and reviewed by hand).
- Apply, after the production import, applies a decision only where the
  production item has the same `source_key` **and** the same `content_hash` as at
  export time. The change is a version with note `decision carried over from the
  test copy`.
- Anything else is listed, not applied.
- Admin-only decisions keep needing admin: apply runs as an admin actor given with
  `--actor`.
- **Removals** travel too (file format version 3), as source keys and reasons
  only, never text: every message an admin withheld (tombstone reason `withheld
  by an admin`) and every item an admin purged (`purged by an admin`). Apply
  writes the same tombstones in production, withholds the message or deletes and
  purges the item where production holds it, and keeps the tombstone for one it
  has not imported yet, so later imports keep both out. Removals apply to the
  whole database, not only to `--run`. A removal the memory service refuses (a
  purge named by a conflict decision of an import not rolled back, a Credit
  Repair item without the sensitive-data permission) is listed, not applied.
- Since 0.6.1: apply refuses while an import, a rollback or a rescan holds the
  lock. A withhold's tombstone is written in a savepoint of its own, so a withhold
  refused here still leaves it (the next import brings the message back
  withheld); a message production holds no row of (not imported yet, or removed by
  a rollback or an import) has its rollback copies under older row ids blanked and
  the items promoted from an earlier row of it emptied (`memory.source_withheld`,
  cause `decisions-apply`). Each withhold and purge is audited as the API audits it
  (`archive.message_withheld` with `via: decisions-apply`, `memory.purged`), and
  the withhold empties production's own promoted copies.
- Not carried over (redo by hand): edits of customer and Credit Repair items,
  conflict decisions, workflow drafts reviewed or converted, promoted messages,
  redacted versions.
- **Version 4 (0.6.2).** A withhold travels as every stone (2.9) that is certain or
  kept: `{source_key, reason, anchor, session, row, role, tool_call_id, uid,
  uid_known, via, review}` (`review` `''` or `kept`); a purge as `{source_key,
  reason}`; and `releases: [{anchor, session, row}]` (the exemptions). Holds still
  pending are not exported (the live import finds them again). No text and no
  fingerprint. `--removals-only` writes only `removals` and `releases`
  (`removals_only: true`, no `decisions`). Versions 3 and 4 are read; a newer
  version is refused. A version 3 withhold gets its binding from its key
  (`identity.parse_key`: the row form gives the JARVIS row, the uid form the uid).
- Apply, per withhold: the stone (with its binding, group and `via
  decisions-apply`, or `restore`; a kept hold stays kept) in its own savepoint;
  then the rows it names (`identity.rows_for`: by key, and by JARVIS row with an
  equal or unknown role) are withheld with `review.withhold_message`; a row that
  only shares its uid is held for review (`review.hold_message`, audited
  `archive.message_held`, outcome `held for review`); with no row, its copies are
  emptied (`kept for later imports`). Releases come after the removals: the
  exemption, then a row held in that group for that JARVIS row is released as the
  API releases it (`released`); otherwise `kept for later imports` or `already
  done`. The result and `brain.decisions_applied` count `releases` too.
- **`--removals-only`** (restore.ps1, 6.6 and `DEPLOY_WINDOWS.md`): no `--run`,
  item decisions ignored, the actor `system (restore)` (with the sensitive-data
  permission: every decision in the file was an admin's) unless `--actor` names an
  active admin; `brain.decisions_applied` has target `brain` and `"via":
  "restore"`, and every withhold and purge is audited with `via: restore`.
- (review 1) Every removal carries `by` and `at` (who decided, when; ids and times
  only). Removals apply with the admins' own withholds first (`anchor` empty), then
  the stones that join them, then purges, whatever order the keys sort in; and
  `review.withhold_message` anchors a group to the admin's own stone when the row
  it withholds is already a member stone, so a copy never becomes a group's root.
  The `--removals-only` file of the same installation also carries
  `purged_items: [{item, created_at, by, at}]` (every purge, from
  `brain_purged_items`, also of items without a source key) and
  `purge_fingerprints: [{fp, source_key, kind, by, at, via}]` (the live keyed
  fingerprints; HMACs, never text). Apply then stores the fingerprints made with
  this database's password (others are counted: `other database password`),
  purges again the very item a restore brought back (same id **and** creation
  time; `purged`, else `already done`), and writes back on every stone, tombstone
  and purge record it added the file's `by`, `at` and (withholds) `via`.

**Audit actions:** `brain.import_started`, `brain.import_completed`,
`brain.import_failed`, `brain.import_rolled_back`, `brain.rescanned`,
`brain.decisions_exported`, `brain.decisions_applied` (actor `--actor`, default
`importer (cli)`), and `memory.source_withheld` for each item a withhold empties.
Since 0.6.2: `brain.withhold_identity_repaired` (cause and counts, only when
something changed), `archive.message_held`, `archive.hold_kept`,
`archive.hold_released` and `brain.purge_fingerprints_forgotten`.

**`status`** also reports `migration_0007_brain_withhold`,
`withheld_copies_with_text` (promoted items whose message is withheld but which
still hold text) and `withheld_rollback_copies_with_text` (rollback copies of a
withheld message that still hold it). `jarvis-import.ps1 -Target Production`
requires the migrations in every mode, and a `withheld_copies_with_text` of 0 for
`-Mode Import` and `-Mode Resume` only (the other modes print the count; `-Mode
Rescan` empties them).

Since 0.6.2 `status` prints one more line (and returns the same values as keys):
`migration 0008_brain_withhold_identity present; identity repair done; readable rows
matching a withhold: N; held for review: H; withheld rows with a text fingerprint: M;
rows blanked by the database guard: G; quotes of removed sources: Q; purge
fingerprints made with another key: X` (`MISSING` and `not done` where they apply).
`jarvis-import.ps1` reads it (6.6). **`repair`** without `--yes` prints what is
pending and changes nothing; with `--yes` it takes the import lock (refused, exit 2,
while it is held), runs `identity.repair` and prints the counts. **`fingerprints
--forget-other-key --yes`** deletes only the purge fingerprints made with another
database password (2.3) and audits how many.

### 2.2 Sessions and messages

**Columns.** Probe with `PRAGMA table_info` and read only these:
- sessions: `id, title, source, started_at, ended_at, end_reason,
  parent_session_id, model_config, message_count, tool_call_count, archived`
  (`display_name` is not read: on a gateway it is the name of the person JARVIS
  chatted with, never a title);
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
| Excluded | `active = 0` with `compacted` 0, NULL or absent → `rewound or superseded`. No content, no tool calls and no tool name → `empty`. Role `system` → `system prompt`. Orphans (`--all`) → `no session`. A tombstoned key → `purged by an admin`. An excluded row with content or tool calls is still checked for restricted data (content and arguments, as a stored row would be): if it holds any, the session is withheld (0.4); the row itself is never stored. |
| Compaction | `compacted = 1` → state `compacted`. Hermes writes the summary and the turns it carries on as new live rows right after the compacted ones; only that block is compared. Walking the live rows that follow a run of compacted rows: a row whose (role, tool_name, tool_call_id, content hash, tool_calls hash) equals a compacted row of the session not matched yet → `duplicate`, reason `carried-on copy of a compacted message`, and that compacted row is used up (one copy per compacted row); the first row with no such counterpart is the summary (imported) and the walk goes on; the next row with none ends the block. Rows after the block are never copies, even when they say what a compacted row said. Excluded rows are skipped. |
| Adjacent duplicates | For user and assistant rows only: (role, tool_name, tool_call_id, content hash, tool_calls hash) equal to the previous user or assistant row → `duplicate`. Tool results are never de-duplicated. |
| Content | User and assistant: cut to 200,000 characters, `clean(…, "chat")`, cut to 50,000. Tool results: cut to 20,000, then cleaned, then cut to 4,000 (`truncated`). |
| `tool_calls` | Parse both the OpenAI (`function.name/arguments`) and the flat (`name/arguments`) shape → `[{name, arguments}]`. Arguments are cleaned as JSON or chat and cut to 500. The message's sensitivity is the highest of its content and arguments. A restricted argument restricts the message. |
| Customer by default | Staff `sarah`, a customer-facing channel, a tool name matching the customer pattern, or tool-call arguments or a tool result's file paths naming customer work (0.5) → at least `customer` |
| Restricted | Any restricted message → **the whole session withheld** (0.4): every message `withheld`, no text, labels empty; title and summary `''`; action `withheld` with reason `restricted data in this conversation: <types>` |
| Labels | `labels_for(role, cleaned, tool_names=…, tool_error=…)`; none for withheld, duplicate or excluded rows |
| Session derived fields | Counts; `labels` as counts per label; sensitivity = highest message level (restricted > customer > general), at least `customer` with `flags.title_masked` or `flags.customer_channel`; summary = first user message (one line, 200) ` → ` last assistant message (one line, 300), from imported, non-withheld rows; title = `sessions.title` cleaned (`""` if restricted or absent) |

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

**Since 0.6.2 (2.9):**
- **No fingerprint of withheld text.** A withheld row and its rollback copies keep
  `source_fp = 'w1:live' | 'w1:compacted' | 'w1:rewound'` (the state only); a stored
  row is "the same" as its source row while the state is (`identity.same_source`).
  `message_uid` and `absorbed_message_uids` are read when present; every row stores
  `source_uid`.
- **Held rows.** Before writing, `Processor.held_rows` applies the identity rules of
  2.9 to the session; a matched row is inserted withheld (action `withheld`, reason
  `withheld by an admin[: <rule>]` or `held for an admin's review: <rule>`) and its
  key inherits a stone in the admin's group. A withheld row whose release marker is
  set (`flags.withheld_by_admin.released_at`, `source_fp ''`) and that no stone names
  any more is written again from the source (`updated`, reason `released by an
  admin: restored from the source`).
- **Rewound twins.** A live row whose (content hash, tool call) equals an earlier
  rewound row of the session is that row's twin (Hermes's in-place compaction tail,
  `/retry`); twins and the importer's duplicate links carry a withhold, unless both
  rows carry different uids.
- **Key adoption.** A stored row whose key this backup no longer has is re-keyed in
  place when JARVIS changed only its key: the same JARVIS row and role, or (after a
  renumbering) the same uid and role while its old row id is gone. The row keeps its
  id; `source_key`, `source_uid`, `source_message_id` and `position` are updated
  (`updated`, reason `key changed since an earlier import`, `before` with the old
  key). No conversation is stored twice.
- **Key collisions.** A key that names a stored row of another JARVIS row still in
  the backup → `excluded`, `key collision: JARVIS gave this key to another row`; a
  stoned key now naming another present JARVIS row → `excluded`, `key collision:
  JARVIS gave the key of a withheld message to another row`. No row is overwritten
  with another row's text or withheld because of another message.
- **A newly restricted conversation** blanks every readable row of it (also rows the
  backup no longer holds), and the items promoted from its deleted rows are emptied.
- **Title.** A session flagged `title_withheld` (an admin hid it with a withhold)
  keeps an empty title through later imports. A history answer names a
  conversation by its title ("(source: <title> · message N)"); once the title is
  hidden, every quote of the conversation's other messages names it as recall names
  one without a title ("(source: session <id> · message N)"), the quote itself kept:
  at the withhold, at start-up and after a rescan (`quotes.relabel_hidden_titles`,
  counted in `quotes_of_removed_sources` until then), and at read time in every task
  and request reader (`quotes.hidden_titles`).

### 2.3 Profile files → memory items

**Common fields.** Every item has `origin = 'import'` and `status = 'pending_review'`.
The status is `quarantined` when any secret was redacted, or when a risk flag is set
(instruction-like, approval conflict, script, plugin, custom, owner profile).
- `source_system = 'jarvis'`.
- `source_ref`: the profile-relative path (content files: `jarvis-content/<rel>`),
  cleaned, plus `#entry` or `#role <id>`. A path is cleaned part by part
  (`text.clean_path()`): contact data is masked, and restricted data is looked for in
  the whole path too.
- `source_key = jarvis:file:<area>/<rel>`. Memory entries get
  `#entry-<first 16 hex of sha256 of the normalised entry>`; role blocks get
  `#role-<id>`. A path that holds contact or restricted data is never stored: its
  key is `jarvis:file:<area>/path-sha256:<sha256 of the path>`.
- Keys are stored unmasked (records, conflicts, tombstones), so other source text
  stands in a key only when cleaning would leave it as it is (`text.key_part`): a
  path, role id or job name holding a secret or anything else cleaning changes
  stands as `sha256-<key_digest>` (32 base32 characters of its SHA-256), and
  `source_ref` shows it cleaned.
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
- Titles are cut to 300 characters. An empty title falls back to the cleaned file
  name (`<cleaned file name> entry N` for an entry).

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
  `restricted data`; a file whose path is restricted is excluded with
  `restricted data in its path` (and has no entries).
- **Contact data in a path.** Every item of the file is `customer`.
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
- **Purge identity (0.6.2).** A purged item is also recognised under another key:
  a memory entry whose entry digest equals a purged entry's (moved or copied to
  another memory file); a file whose bytes equal a purged file's in the same backup;
  a file whose keyed fingerprint is stored (`brain_purged_content.fp =
  'k1:<key id>:' || HMAC(K, source_sha256)`, K derived from the database password:
  `HMAC-SHA256("infinite-ai/jarvis-brain/purge-fingerprint/v1", password)`; no new
  setting, `pg_dump` never holds it). Such a part → `excluded`, `purged by an admin:
  the same text as an item an admin purged`, and its key gets a purge tombstone.
  `review.purge` stores the fingerprint (also of an incoming version); an import
  learns it for a legacy purge. Fingerprints made with another key id (the password
  changed) make the import refuse until `fingerprints --forget-other-key --yes`.
  With no password, purges hold by key, digest and same-backup copies only. Items
  that came back after a purge (key tombstoned after a restore, entry digest or
  fingerprint imported after the purge) are quarantined with risk flag
  `purged_before` by the repair (2.9); nothing is purged automatically.
- **Conflict dedupe.** `dedupe_key = sha256(kind, source_key or URL, incoming
  sha256)`. An existing conflict (in any status) → no new conflict.
- **Content folder.** `--content` text files that are `not from a template` →
  `knowledge` with `agent = ''` and `flags.from_content_folder`. Template matches are
  excluded with `template`.

### 2.4 Workflow drafts

There is one draft per job in `cron/jobs.json`. The key is `jarvis:job:<id>` (else
`jarvis:job:name:<name, or its digest (2.3)>#<index>`). A job whose id appears
earlier in `jobs.json` is keyed `jarvis:job:<id>#<n>` (n = 2, 3, …) and recorded as
`duplicate`, reason `the same job id appears earlier in jobs.json`; the first job
with that id names it for staff attribution. The website watchdog's key is
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
keyed `jarvis:url:<key_digest of the normalised URL>` (never the address: its path
can hold a webhook secret or an e-mail address). A URL that cleaning would change is
excluded (`held a secret or personal data`); the others are compared with
`sites.url`:
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

### 2.9 Withhold identity (0.6.2)

0.6.0 and 0.6.1 tied a withhold to one `source_key`; JARVIS stores the same message
under other keys (compaction copies, adjacent repeats, uid backfill, uid reuse,
renumbering), and each came back with its text. Since 0.6.2 a withhold is bound to the
identity of the JARVIS message (`app/brain/identity.py`, migration
`0008_brain_withhold_identity`).

**Stones.** Every part of a withheld message's identity is one more `brain_tombstones`
row with reason `withheld by an admin` (a stone): `source_key`, the binding
(`source_session_id`, `source_row_id`, `role`, `tool_call_id`, `message_uid`,
`uid_known`), its group (`anchor_key`: `''` for the admin's own decision, else that
decision's key), how it joined (`via`), its review (`''` member, `pending` hold,
`kept` hold an admin kept) and `after_run` (the newest import run when the decision
was taken). Every reader that honours a tombstone by key honours a stone.
`brain_withhold_exemptions (anchor_key, source_session_id, source_row_id)` records an
admin's release, bound to the content released (`source_uid`, `content_chars`, -1 while
unknown, and `content_fp`, an HMAC of the released row's readable text keyed from the
database password, set once that row is readable and dropped when that text is withheld
again): a later version of the row with other content is not covered.

**Rules** (`importer.Processor.held_rows`, per JARVIS session):

| Rule | Matches | Outcome |
|---|---|---|
| key | the row's key has a stone bound to this JARVIS row, or unbound, or bound to a row gone from the backup with the same role | the stone's review; bound to another present row → key collision (2.2) |
| JARVIS row | `(session, row id)` is a stone's binding, role equal or unknown | member |
| fold | a uid in `absorbed_message_uids` is a certain stone's uid (never a fold member's own uid: that is the survivor's message) | member, never releasable; from a fold only its own copies (its uid and its text) |
| prune twin | an archived (`compacted` 1) row of the conversation of the admin's own bound row, with its uid, role, call id and timestamp and a higher row id, while that bound row is a tool or assistant row Hermes rewrote in place since the withhold (its length differs from the stored withheld row's) and holds other text | member `pruned by JARVIS: the original` (Hermes `rewrite_pruned_rows` provenance) |
| link | the importer's duplicate of a matched row, or its rewound twin | the matched row's review |
| uid | the row's uid is a certain stone's uid, role equal or unknown | member when its text equals the group's (in memory) or is Hermes's restatement of it under that uid; else a hold: `same message id, nothing to compare` (no text of the message in the backup), `same message id, contains the withheld text`, or `same message id, different text` |
| restated | a user row starting with Hermes's replay header whose request equals a group's text, in the message's conversation family, under a new uid | a hold `restated after a compaction, new message id` |
| same text | identical (role, tool call, content, tool calls) to a member of the group in this conversation or a continuation or branch ancestor (no depth limit) | member `copied by JARVIS` in a copy position (a twin, or a child row timed at or before the child started); else a hold `same text`. Never for a row the admin saw (stored by a run up to `after_run`), an exempted row, or a row with its own uid when the group's uid was known |

Holds come only from the uid, restated and same-text rules and the upgrade heuristics,
and an admin keeps or releases them (3); each carries an `evidence` sentence. Certain
matches (provenance only) are never releasable. A release writes an exemption; the hold
rules skip that JARVIS row for that group from then on while it holds the content
released.

**Invariants.** I1 a row whose key has a stone is withheld; I2 a row whose JARVIS row
is a stone's binding is withheld; I3 no withheld row or copy keeps a text fingerprint;
I4 an exempted row is never held again by the hold rules; I5 nothing outside the
database (decisions files, API responses, audit, logs) holds a fingerprint.

**Database guard.** A `BEFORE INSERT OR UPDATE` trigger on `archive_messages` blanks
any row I1 or I2 names, whatever code writes it, and stamps
`flags.withheld_by_admin.guard` (verify check 12 reports it).

**Repair** (`brain_withhold_repair(cause)` in SQL, then `identity.settle`): a stone
for every 0.6.0 withhold without one; every unbound stone bound to the row it named
when the admin decided; a stone for every duplicate the importer recorded of a
stoned row; readable rows that are the same message withheld (by key and JARVIS row:
members; by uid and by 0.6.x's own fingerprint in the conversation's lineage after
the withhold: holds); readable rows of restricted sessions withheld; every withheld
fingerprint converted to `w1:`; then the promoted copies emptied, quotes scrubbed,
sessions recomputed and items that came back after a purge quarantined. It runs in
the migration (before any request), at every start-up check, before every import, in
`rescan --yes` and in `repair --yes`. It is idempotent and audits counts only when
something changed. `brain_withhold_pending()` counts what it would still change,
read-only.

**Withhold** (an admin, decisions-apply): `identity.group_rows` follows, to a fixed
point (capped at 1,000 rows), the same JARVIS row, the same uid (member when the text
equals, else a hold) and the importer's duplicates; each gets a stone in the admin's
group and loses its text; keys of copies without a row get stones too, including the
keys of rows of the message an import deleted and kept only as rollback copies
(`identity.deleted_copies`: the same uid and role with the same text, or an archived
generation in the family; without uids, the importer's rewound twin, a row of the same
conversation deleted as rewound by the run that stored this one or an earlier one, with
the same text), so the withhold blanks those copies at once and a rollback puts the row
back withheld. Another message with the same words keeps its rollback copy. Identical text
elsewhere in the lineage is withheld only when the admin ticks it
(`include_identical`). The title is hidden by default for the conversation's first
user message (`hide_title`).

**Fail-closed on purpose:** an adjacent verbatim repeat; in a backup without uids, a
later verbatim repeat in the lineage (a hold); a same-role uid collision with nothing
to compare; a fold composite; 0.6.1's victim of a reused uid; the upgrade heuristics
(holds). **Always readable:** identical text in another conversation, similar text,
identical text the admin saw, a later repeat with its own uid, parallel tool results,
a row that took over a key, another role, twins of readable messages, related turns,
new turns of a child, delegated sessions, the parent of a restricted branch, an
independent repeat first seen compacted, a released hold. The full tables, with a test
per case, are `tests/integration/test_brain_withhold_identity.py` and
`test_brain_withhold_overreach.py`.

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
| `DELETE /items/{id}/purge` | admin | 409 while an import, a rollback or a rescan holds the lock. Only deleted items. Deletes the item, its versions and its conflicts, and writes a tombstone for `source_key` (a promoted item has none: the audit entry is its record, and its message is withheld separately, never by a purge) → `memory.purged` with `origin`, `message`, `source_withheld` and `answers_scrubbed`. Refused while a decided conflict of an import not rolled back changed a site, a draft or another item (except an item that import created itself), or this item when it was not emptied |
| `POST /items/{id}/versions/{version}/redact` | admin | 409 while an import, a rollback or a rescan holds the lock. Blanks that version's title and body (a `redacted` marker) → `memory.version_redacted` |
| `GET /search` | user | `q, scope=all\|memory\|archive, kind, agent, staff, label, since, until, limit ≤ 50` → `{memory:{hits,total}, archive:{hits:[{message, session:{id,title,staff,agent,started_at}, excerpt}], total}, filtered}`. Archive results only for operators and admins. |
| `GET /sessions` | operator | `q, staff, agent, origin, label, since, until, limit ≤ 200, offset` → `{sessions, total}` |
| `GET /sessions/{id}` | operator | `limit ≤ 500, offset` → `{session, messages, total, hidden}`. `hidden` = messages outside the scope. |
| `GET /messages/{id}/promoted` | admin | `{message, count, items:[{id, status, deleted}]}`: every memory item promoted from the message (what a withhold empties); `items` lists those in the admin's scope |
| `POST /messages/{id}/withhold` | admin | 409 while an import, a rollback or a rescan holds the lock. Optional body `{include_identical: false, hide_title: null}`. Content `''`, `withheld`, flags note, admin tombstone (its stone, 2.9), and every other stored row of the same JARVIS message withheld too (2.9: members, and holds for the same uid with other text); identical text elsewhere only with `include_identical`; the title hidden with `hide_title` (default: when it is the conversation's first user message); its rollback copies blanked (also under older row ids); every promoted item emptied (below); quotes removed; session derived fields recomputed → `{message, session, copies:{items, versions_blanked}, answers_scrubbed, rollback_copies_blanked, also_withheld, held_for_review, identical_withheld, keys_tombstoned, title_hidden}`, `archive.message_withheld` (the same keys), `memory.source_withheld` per emptied item. All or nothing |
| `GET /messages/{id}/withhold-preview` | admin | read-only: `same_message_rows`, `held_rows`, `identical_text_rows` (ids, positions, roles, times, states; rows outside the scope counted in one total, `rows_outside_your_scope`, never per list), `rowless_copies` (a count, including rows of the message an import deleted, kept only as rollback copies), `items_with_this_text` (imported items in the scope whose text contains the message's, 32+ characters: `[{id, title}]`, to purge by hand), `first_user_message`. Never text |
| `GET /holds` | admin | every withheld row whose stone is pending, in the scope, with its evidence and never its text: `{message, session, title (null when hidden), position, role, at, content_chars, state, rule, withheld_message:{id, position, role, at, content_chars}, compare: identical text\|different text\|contains the withheld text\|nothing to compare, evidence, neighbours}`. `withheld_message` is described only when it is in the admin's scope; otherwise `{id: null, position: null, role, at: null, content_chars: null}` (an admin without the sensitive-data permission learns nothing of a Credit Repair or restricted row) |
| `POST /holds/{id}/keep` | admin | 409 during an import. The stone becomes `kept` (permanent) → `archive.hold_kept {message, session, anchor, rule}` |
| `POST /holds/{id}/release` | admin | 409 during an import. 400 when a certain stone names its JARVIS row ("it is the same message as a withheld one: it cannot be released"). Deletes the pending stone, writes the exemption, sets the release marker; its text comes back at the next import of a backup holding it; promoted copies emptied meanwhile stay empty → `archive.hold_released {message, session, anchor, rule}` |
| `POST /messages/{id}/promote` | operator | `{kind, title, agent?}` → a pending item: origin `promoted`, `import_run_id NULL`, `source_ref = "JARVIS session <source id> · message <position>"`, sensitivity = the highest of the message and its session → `memory.promoted`. A withheld message, or one with an admin's withhold tombstone → 400; since 0.6.2 also a row that is the same JARVIS message as a withheld one (its key has a stone, its JARVIS row is a stone's binding, or its uid is a stone's uid with an equal or unknown role and no release of that group names it) → 400 "this message is the same JARVIS message as one an admin withheld: it cannot be promoted". A readable row with identical text that the admin saw stays promotable. The message's row is locked: a withhold of it waits for the promote and then empties the new item, or the promote waits for the withhold and is refused. |
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

**Withholding a message that has promoted items (0.6.1).** An item promoted from a
message (`origin = 'promoted'`) names it by `flags.message_id`, by
`flags.message_source_key` (kept by a rollback and a new import, which change the
row id), or, promoted by 0.6.0, by its `source_ref` while its `message_id` names no
row (`archive.promoted_copies`). Whoever withholds the message (an admin,
decisions-apply, an import, a rescan, a rollback) empties every such item, whatever
its status and deleted ones too (`store.empty_withheld_copy`):
- the item and every version lose their title, text, tags and notes (versions read
  `[redacted]`), including an admin's own edits: no version can be told apart;
- the item becomes `quarantined` with `flags.source_withheld`, titled "Withheld:
  the archived message it was promoted from was withheld", as a new `redacted`
  version; ids, provenance flags, `source_ref`, dates and deletion stay; the content
  hash keeps no fingerprint of the text;
- it is never approved, reopened or edited again (400); rejecting, archiving,
  deleting, restoring and purging stay possible. `source_withheld` is a risk flag,
  and agents never read such an item, nor a promoted item whose message is withheld
  (`policy.withheld_source`, in `policy.agent_items`): its `message_id` row is
  withheld, a row with its `message_source_key` is withheld or that key has an
  admin's withhold tombstone, or (promoted by 0.6.0) its `message_id` names no row
  and its `source_ref` names a withheld row (read out of the `source_ref`, so it is
  found by index). Nobody lists such an item while it still holds text, nor one
  flagged `source_withheld` that still holds it (0.6.0 flagged a copy quarantined at
  promotion and kept its text) (`policy.person_items`): from 0.6.2 not an admin
  either (0.6.1 listed it to admins until it was emptied). `brain-import status`
  and the pre-check count it until the start-up check or a rescan empties it; the
  emptied item is listed again by the role rules.

An import empties them for every row it stores without its text: a row of a
conversation that is, or becomes, restricted (also when the session is created again
after a rollback) and a row an admin's tombstone names. It does so once per batch,
right before the batch commits, and never looks for quotes of the rows the batch
inserted (nothing can have read them yet); the rows it deletes (rewound or compacted
since an earlier import) lose their quotes then too. A withhold also removes the
quotes of the message's older rows (the rows its `import_records` name, never a
`duplicate` record's).

At start-up (`app/bootstrap.py`, logged by class only on a failure, never stopping
the start) and in `rescan --yes`, `archive.sweep_withheld_copies` empties the
promoted items a 0.6.0 database left with their text, `archive.sweep_withheld_records`
blanks the rollback copies of withheld messages that 0.6.0 left under an older row id
(it blanked them by row id only), and `quotes.refresh_quotes` removes the quotes of
withheld, deleted or emptied sources that tasks kept; an emptied item that is
approved or waiting for review again (0.6.0, after going back to it, allowed that) is
quarantined again. The start-up check runs under the import lock and the quote lock,
and is skipped, not waited for, while an import, a rollback or a rescan holds the
one, or a task quoting the brain holds the other: if `brain-import status` still
shows `withheld copies with text` above 0, start again or run `rescan --yes`.

Since 0.6.2: the start-up check runs the withhold identity repair (2.9) when it gets
the import lock, and refreshes quotes even when it does not (an import repairs before
it imports); the worker tries it again every minute until it ran completely. Items
promoted from a restricted conversation, and 0.6.0 copies (`message_id` only) of a
message withheld while it had no row, are matched by `policy.withheld_source()` too.
`refresh_quotes` also removes request-summary lines (`quotes.QUOTE_LINE`) that quote a
removed source, and every route returning a task or a request strips quotes of
removed sources when it reads them (`quotes.strip_dead`, `strip_summary`), so a quote
is never served even before the start-up check has run.

A task that quotes JARVIS Brain text into its result (the Supervisor's `recall`, a
`summarize_request` that copies a recall's local-only answer, and reference notes)
holds the quote lock (`quotes.lock_quotes`, advisory lock (7314002213, -1)) shared
until it commits; every removal of quotes (`quotes.scrub_quotes`: a withhold, a
purge, a redaction, a rollback, an import; and `refresh_quotes`) takes it
exclusively first, and finds the quoting tasks through the index `tasks_quotes_idx`.
So a quote is either committed before the removal looks for it, or read after the
removal committed. Nothing holds it around a call to an AI model: a summary written
by a model copies no brain text and takes no lock. A recall never waits for it: while
a removal holds or waits for it, the worker queues the recall again (not an attempt,
like a busy site). Reference notes never wait for it either: they are left out of
that task. A rescan takes it only for its last pass (the copies, then the quotes).

Database errors reach responses and logs by their classes and SQLSTATE only
(`db.describe_error`): 409 for an `IntegrityError`, 500 otherwise, "the database
refused this change; nothing was saved". The `command_center` role logs at
`log_error_verbosity = terse` (`postgres/provision.sh`), and the app sends values only
as bind parameters (which PostgreSQL leaves out of the log while
`log_parameter_max_length_on_error` is 0, its default), so a refused row's values
never reach the PostgreSQL log (`testing/brain-check.sh` checks both).

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
- **Withhold key guard (0.6.2).** `policy.agent_messages()` never returns a row whose
  `source_key` has a withhold stone, and `agent_items()` (and `person_items()` for
  every role but admin) never returns an item whose `source_key` has a purge
  tombstone: both hold before any repair runs (a restore of an older dump).

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

**Withhold dialog (0.6.2):** it loads `withhold-preview` and states "N other stored rows
are the same JARVIS message and are withheld too; M are held for your review", and, in a
sentence of its own, how many rows outside the admin's scope it does not list; the
unticked box "Also withhold K other messages with exactly this text in this
conversation and its continuations and branches"; "Hide the conversation title" (ticked for the first user message); the
imported memory items that contain the text, as links. When the preview cannot be
loaded, the withhold still works (the server decides the title).

**Held for review (admins, Review tab):** each held row's evidence (never its text),
what its rule means, **Keep withheld**, and **Release** with the confirmation "Its
text comes back at the next import; promoted copies stay empty." Chips:
`withheld_by_admin.review` pending ("held for review") and kept, `guard` ("withheld by
the database guard"), released, `title_withheld` ("title hidden by an admin"),
`purged_before`.

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

1. **Folders.** The live install stays where it is. The release under test is
   extracted into a **new** folder, `C:\InfiniteAI\v<version>-brain-test\InfiniteAI-CommandCenter`
   (0.6.2: `v0.6.2-brain-test`), which has no `.env`. The test copy runs only from
   there.
2. **Before the test.** `scripts\brain-precheck.ps1` reads the live install,
   read-only, and records a fingerprint (6.4).
3. **The test copy.** `scripts\brain-test.ps1 -Action Start [-SeedFromBackup <folder
   of a backup.ps1 run>]` starts it.
4. **The staged import.** `scripts\jarvis-import.ps1 -Target Test …`: plan, then
   sample 5, then review, then about 10 %, then review, then `-All`.
5. **The owner reviews** in the test copy at `http://127.0.0.1:8190`. Review
   decisions are exported with `-Mode ExportDecisions`.
6. **Approval.** The owner approves the production upgrade (a separate step): backup,
   install the release (0.6.2) from a new folder, then smoke.
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
       deletes sessions and secret values; marks pending events `skipped`; deletes
       the live installation's purge fingerprints (0.6.2: keyed with its database
       password, which the test copy does not have; the purges stay by key);
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
- `schema_migrations` and whether `0006_brain`, `0007_brain_withhold` and
  `0008_brain_withhold_identity` are present;
- the promoted items of withheld messages that still hold text (the same rules as
  `archive.withheld_copies`, parity-tested);
- with 0008: `brain_withhold_pending()` (counts only, a read-only transaction) and,
  in words, the readable rows matching a withhold, the rows held for review and
  whether the identity repair is done; without it, "0.6.2 not installed";
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
- `-Mode Plan|Import|Resume|Verify|Rollback|Status|ExportDecisions|ApplyDecisions|Rescan|Repair`
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
  `schema_migrations` holds `0006_brain`, `0007_brain_withhold` and (0.6.2)
  `0008_brain_withhold_identity`; the importer is 1.1.0 or later; the image of the
  running api equals `BRAIN_IMPORT_IMAGE`. Each refusal names 0.6.2 ("install 0.6.2
  on the live installation first").
- 0.6.2 gate for `Import` and `Resume`: the status line (2.1) says `identity repair
  done` (else: let the live installation start once, or `-Mode Repair`), `withheld
  copies with text: 0` and `purge fingerprints made with another key: 0`. Readable
  rows matching a withhold, held rows, quotes of removed sources and guard counts are
  printed, not required: an import repairs first.
- `-Mode Repair` runs `brain-import repair --yes` in `brain-importer-nosource`
  (typed confirmation `REPAIR PRODUCTION`).
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

**`scripts/restore.ps1` (0.6.2, review 1).** Before stopping anything, in a helper
container of the api image (`docker compose run -d --no-deps --name
cc-restore-helper-<time> api`, removed afterwards; the api need not run), it runs
`restore-carry export` (users' access and the audit log) and, when
`schema_migrations` holds `0006_brain`, `decisions-export --removals-only`, and keeps
the files as `backups\access-before-restore-<time>.json` and
`backups\removals-before-restore-<time>.json`. A file it cannot save stops the
restore before any change, unless `-KeepRestoredAccess` / `-SkipBrainRemovals`
accepts that loss (printed). After `pg_restore`, and BEFORE `start.ps1`, a helper runs
`restore-carry apply --in ... --yes` (audit entries made after the backup put back when
the backup is an earlier state of this database; `migrate`; deactivations, revoked
permissions and lowered roles made after the backup applied again, when the restored
users are this database's own, never leaving no active admin; every sign-in ended;
the reverted widening changes listed) and `decisions-apply --removals-only --yes`
(tried again ten seconds later, three times, if the import lock is held). Any failure,
or a `not applied` line, leaves the services stopped, prints the commands to run, and
ends with exit 1. It prints how many removals it applied again (`already done` is not
counted) and, after the start, the user changes the restore reverts. Windows
PowerShell 5.1, ASCII only. `tests/unit/test_restore_ps1.py` runs it under PowerShell
with a stand-in `docker`.

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
12. (0.6.2) `restore.ps1` with a stand-in `docker`: removals and access saved before
    the stop, applied again after `pg_restore` and before `start.ps1` (retried while
    the lock is held), and a failed re-apply leaving the services stopped with exit 1; after the import, a message withheld through
    the API stays withheld through a re-import of the same backup (verify checks 9
    to 12), `Status` prints the 0.6.2 line, `-Mode Repair` runs, and the version 4
    decisions file names its JARVIS row and holds no text.
