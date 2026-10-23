# Infinite AI Command Center 0.6.2: release notes

Release date: TODO (after its verdict) · upgrade from 0.5.0, 0.6.0 or 0.6.1,
each straight to 0.6.2 · package `InfiniteAI-CommandCenter-0.6.2-windows.zip`
in `command-center/releases/` on the GitHub branch `jarvis-migration`, with its
SHA-256 in the `.sha256` file next to it · step-by-step Windows guide:
`releases/UPGRADE_0.5.0_TO_0.6.2_WINDOWS.md` (0.5.0 straight to 0.6.2, the
owner's path; there is no separate 0.6.0 or 0.6.1 guide: an installation of a
withdrawn 0.6.x is covered by that guide's notes, section 4 below) · verdict:
`releases/GO_NO_GO_0.6.2.md`

> **Status: pending its verdict.** 0.6.2 replaces 0.6.0 and 0.6.1, which are
> NO-GO for installation and for any JARVIS import (security notice of
> 2026-10-20). Do not install 0.6.2 until `releases/GO_NO_GO_0.6.2.md` says GO.

0.6.2 is a JARVIS Brain security release. In 0.6.0 and 0.6.1, the text of a
JARVIS message that an admin withheld came back when a later JARVIS backup was
imported. 0.6.2 changes how a withhold works: **decide once, replay exactly,
hold what is new.** You decide on one JARVIS backup file in the test copy, where
JARVIS's whole history is visible at once; the live installation imports that
same file and replays your decisions word for word; and in any later backup,
nothing new in a conversation that holds a withheld message becomes readable
unless it is provably the same as a readable stored row or you release it.
Nothing is withheld only because its words match. Section 3 states the
contract 0.6.2 keeps (C1 to C6) and the defaults it uses; section 7 lists what
it does not cover.

**What the upgrade changes in your installation.**
- **One database migration, `0008_brain_withhold_identity`.** It adds 17
  columns to the JARVIS Brain's list of removed sources (the exact JARVIS row a
  withhold names: conversation, row, role, tool call, message id and time; the
  admin's withhold it belongs to, how it joined, whether it waits for an
  admin's review and its computed relations, who kept it, whether it hides the
  title, and the JARVIS backup the decision was taken on), two columns to
  archived messages (the JARVIS message id and the ids a fold absorbed), and
  seven tables: releases and left-readable choices bound to their exact row,
  the decision backups (SHA-256 and whether Settle finished them), the links
  between conversations (append-only: a rollback or a restore never removes
  one), the conversations an admin stopped importing, keyed fingerprints of
  purged items, every purge by item id and creation time, and maintenance
  markers (repair, the legacy lock, the newest import's tripwire). It also
  adds 13 indexes, a database guard (a trigger that keeps every row a withhold
  names without text, matched by exact identity), a trigger that never lets an
  import clear a conversation's lineage, three refusals (a run of an older
  importer, any archived message that older code would write, a purge by older
  code), the repair and its read-only count of what is left. It removes the
  0.6.x text fingerprints of withheld rows. It runs in the database before the
  application serves its first request, and refuses while a 0.6.0 or 0.6.1
  import is still running. On a database with nothing imported (your PC today)
  it finds nothing to repair, locks nothing and writes no audit entry.
- **The importer is version 1.1.0.**
- **The start-up check now runs the withhold repair** (below), and the worker
  tries it again every minute until it has run completely.
- **`restore.ps1` keeps JARVIS Brain decisions** across a restore (section 2,
  "Restores").

**What it does not change.** It leaves all of these as they are:
- your `.env` and encryption keys (no new setting, no new key);
- the PostgreSQL and n8n volumes;
- users, stored secrets and settings;
- the agents and their on/off states, and your approval rules;
- schedules, sites and leads;
- the n8n workflows and credentials.

It imports nothing and switches nothing on. Memory in AI prompts stays off.

## 1. What was wrong in 0.6.0 and 0.6.1

**The main defect (high).** A withhold was tied to one name: the key the
importer derives from a JARVIS row. But JARVIS keeps the same message under
other names in ordinary use, and every later import stored those with their
text:
- **compaction:** JARVIS archives long conversations and carries messages on
  as new rows. A carried-on copy came back after a second compaction, or once
  the original was pruned. After a single compaction, the withheld original
  was rewound and deleted while its live copy was imported;
- **repeats:** a message repeated right after itself came back once either
  copy was edited, rewound or deleted;
- **new message ids:** when JARVIS started giving its rows message ids, every
  key changed. The whole conversation was stored a second time, the withheld
  message with its text;
- **reused ids:** when a message id moved to another row, 0.6.1 could
  overwrite or withhold an unrelated message;
- **the test copy:** a withhold carried to the live installation by its key
  did not hold when the live backup named the message otherwise.

Operators and admins could then read the text in search and in the
conversation views, and so could the Supervisor's history and recall, the
conversation summary and its search, and anyone who promoted the message
again. `verify`, the start-up check and `rescan` reported nothing.

**Also fixed:**
- an import that restricted a conversation and, in the same run, deleted a row
  of it left that row's promoted memory items with their text, and agents
  read them;
- a conversation restricted by a later import kept the text of rows the
  backup no longer held (an admin with the sensitive-data permission could
  read it);
- the start-up check skipped cleaning history answers while an import held the
  lock, and nothing checked them when they were read, so operators could read
  quotes 0.6.0 had left;
- a line of a request summary could survive every clean-up;
- memory items promoted by 0.6.0 from a message that was withheld while it had
  no row were never emptied or hidden.

**Purges had the same weakness.** A purged memory entry that JARVIS moved to
another memory file, and a purged file that was renamed or copied, came back
as new items.

**Restores undid privacy decisions.** Restoring a backup older than a withhold
or a purge brought the text back silently.

## 2. What 0.6.2 changes

**Decide once, on one backup (the test copy).**
- **Withhold.** A withhold names the exact JARVIS row (conversation, row,
  role, tool call and time, plus its message id when JARVIS gives one) and the
  JARVIS backup it is decided on. The other stored rows of the same message
  are withheld with it at once: the same JARVIS id with the same text, and the
  importer's own recorded copies. The **withhold dialog** names the decision
  backup, counts those rows and the rows that share the message's id with
  other text (held for your review), and lists, **ticked**, the stored rows of
  the conversation and its continuations and branches that equal, contain,
  restate or quote the text, each with what links it. Untick one to leave it
  readable: that is recorded as your decision and survives every later import,
  rollback and restore. It also offers to hide the conversation's title
  (ticked for the conversation's first user message) and lists the memory
  items that contain the text, for you to purge.
- **Finish decisions (Settle).** `jarvis-import.ps1 -Mode Settle -BackupDb
  <the file>` (`brain-import settle --backup <file>`) goes through every
  conversation family that holds a withhold on that backup, also sessions a
  sample import skipped, with each withheld text in memory, once. It runs on
  the newest backup that wrote the family (an older one is refused). Withheld
  for good, only by provenance (contract C3): the exact row; the same message
  id, role and text; a row that absorbed the withheld message's id; the same
  tool call with the same text in the family; the importer's own recorded
  copy; and JARVIS's copy into a continuation or branch at the very time and
  text of exactly one parent row (and, for a withhold made in the copy, that
  parent row). **Held for your review**, with the relation computed by
  comparison after removing JARVIS's wrappers (the replay header, the reply
  pointer, the Discord note): every other row of the family that is
  identical, contains the text (16 characters or more), is part of it (its
  own text, or a reply quote, of 16 characters or more), restates it, or shares its message id, tool call or time with other
  text, and a stale stored row at its role and time; also a row stored from an
  earlier backup that this backup no longer holds (JARVIS renumbered or
  deleted it) whose stored text is identical, contains it, is part of it or
  restates it. Until Settle has run, the
  dashboard says **Finish decisions needed** and the decisions export
  refuses; it also refuses while a row Settle held waits for your review.
- **Review** (Review tab, "Held for review", group "Found when finishing
  decisions"). Rows that look independent (their own message id, another
  time, not a copy JARVIS made) are pre-selected for release; nothing is
  released until you confirm, and a row with any relation to the withheld text
  is released only after you type `RELEASE`. A release covers only that
  exact row and the content it had; its text comes back at the next import of
  a backup that holds it (nothing computed from the text is stored), and
  promoted copies emptied meanwhile stay empty.
- **Export** (`-Mode ExportDecisions`): a version 5 file with every decision
  word for word (keys, exact rows, review state, who and when, the decision
  backup's SHA-256, the conversation links, stopped conversations), never
  text.

**Replay exactly (live).** `jarvis-import.ps1 -Target Production -Mode Import
-Decisions <file>` applies the decisions first and then imports, so the
database guard blanks every row a withhold names as it is written: a withheld
message is never readable on live, not even during the import. When the
backup file (its SHA-256) and the importer version are the ones the decisions
were taken on, every row ends in the state it has on the test copy. When they
are not, the script warns, and those conversations are imported as a later
backup (below): fail-safe.

**Hold what is new (later backups).** In a conversation family that holds a
withheld message (the conversation, its continuations and branches, and
conversations that copied its rows; links are kept once seen), a later backup
never adds text to a stored row and never makes one readable. Each new row is
withheld (provably the withheld message: its exact row, JARVIS's copy of it
with the same id, time, role and call, or a row that absorbed it), readable
(identical in role, tool call, time and text to a readable stored row of the
family, readable by at least the same people), or **held for your review**
with its computed relation. A new conversation of the family keeps its title
hidden until you release its rows; it comes back at the next import only when
that backup holds every withheld text of the family and the title has no
relation to any of them (JARVIS names a conversation after its first message),
otherwise it stays hidden. The Review tab's group "New in a later
backup" lists them by conversation, with **Release this conversation's rows
with no relation** (offered only when the backup held every withheld text of
that family) and **Stop importing this conversation**. Conversations outside
every family import as before. `-Mode Plan` counts the rows a later backup
would hold, per withheld conversation. A tripwire counts the readable rows an
import writes that hold a withheld text it found in the backup: inside a
family they are held and `verify` fails; outside, `brain-import status` lists
them by location for admins (never text, never a fingerprint).

**No fingerprint of a withheld text is kept.** A withheld row keeps only its
state; texts are compared only in memory, during an import, Settle or a
withhold. The 0.6.x fingerprints of withheld rows are removed by the
migration.

**The database guard** blanks any row a withhold names by exact identity
(the same conversation and row, role, tool call, time, and never another
message id: JARVIS reuses row numbers after a snapshot restore), whatever code
writes it. `verify` reports it if the guard ever had to act. The repair runs
at every start, before every import, with `rescan --yes` and with
`brain-import repair --yes`.

**Promote** refuses any row of a withheld message, and a row that shares a
held row's message id until you have reviewed the hold.

**Every reader respects a withhold and a restriction:** viewers, operators and
admins (search, conversations, history answers, request summaries), agents
(memory, AI prompts, reference notes), the Supervisor's recall and every API
route. History answers and request summaries are checked again when they are
read, so a quote of a removed source, or one naming a hidden title, is never
served even before the start-up check has run. A memory item promoted from a withheld message that
still holds its text (one 0.6.0 left) is listed to nobody, admins included,
until the start-up check or a rescan empties it; 0.6.1 listed it to admins.
**AI drafts** (blog posts, social posts, keyword briefs, support replies)
record the memory items their AI prompt held. When one of them is withheld
(through the message it was promoted from), purged, deleted or redacted, the
draft's title and text, its task's summary and the approval cards naming it
are replaced by a removal note, at once and when they are read (a draft the
model was writing at that moment is stored already removed). A pending
approval to send or publish such a draft no longer matches it and is refused.
Drafts written before 0.6.2 recorded nothing and are not reached (section 7).

**Purges.**
- A purged memory entry is recognised in any memory file by the digest
  already in its key.
- Purging the incoming version of a changed file keeps no plain hash of it
  (a purge 0.6.0 or 0.6.1 made kept one in the item's flags: the start-up
  check, and the legacy reset before its rollback, turn it into a keyed
  fingerprint, so the purged version stays out after the reset too):
  the API shows how many changed versions were rejected, never their hashes;
  the keyed fingerprint leaves that version out of the next import, and a
  later change of the file still arrives for review (the item it would
  replace is never purged with it, also not by a restore).
- A copy of a purged file in the same backup is recognised by its bytes.
- A purged file that is renamed later is recognised by a keyed fingerprint of
  its bytes. The key is derived from the database password, so a fingerprint
  means nothing outside your database and no new secret exists.
- An item that already came back is quarantined for an admin (only admins see
  it, no agent reads it); nothing is purged automatically.
- If the database password changes, imports stop until the old password is
  restored or `brain-import fingerprints --forget-other-key --yes` forgets the
  old fingerprints (audited). The purges themselves stay.

**Decisions from the test copy (file version 5).** The file holds every
decision word for word: each withhold and hold with the exact JARVIS row it
names, its review state, who decided and when, who kept it, whether it hides
the title, and the SHA-256 of the backup it was decided on; every release and
left-readable choice bound to its exact row; the decision backups, the
conversation links and the conversations an admin stopped importing; and the
review decisions on imported memory items. It holds no text and no
fingerprint of a message or a file; a purged memory entry's key, as every
memory entry key since 0.6.0, carries a 64-bit digest of the entry's text,
which confirms a guess of the entry offline. The file says so (`handling`),
and `decisions-export` prints it: keep the file like a backup and delete it
once it is applied. A version 4 file (0.6.2 development builds) applies
fail-safe: its withholds apply, its releases do not (each is listed: release
the row again). A version 3 file (0.6.0, 0.6.1) names only a key: it applies
only through the legacy reset (section 4); export again with 0.6.2.

**Restores.** `restore.ps1` first checks that the installed application is
this folder's: it stops before any change when the api image is another
version than this folder's `VERSION` (every folder tags its images the same)
or when the containers were created from another folder. It then takes the
safety backup, checks that no JARVIS
Brain import, rollback or rescan is running, and stops the application. Only
then (so nothing can be decided in between) does it save every withhold
(with the title choice), every purge (by key, and by item id and creation time,
so manual and promoted items and incoming versions are covered too), every
release and every identical text an admin left readable, every keep, the keyed
purge fingerprints, who decided each and when
(`backups\removals-before-restore-<time>.json`, keys and ids only), and the
users' access and the audit log, including the entry of that export
(`backups\access-before-restore-<time>.json`, e-mail addresses and audit
entries; no password hash). It uses a helper container of the api image, so
the api need not be running. A probe of the database that fails stops the
restore before any change (it never counts as "no JARVIS Brain"). After the restore,
**before any service starts**, the helper puts back the audit entries made
after the backup, applies again every deactivation, revoked sensitive-data
permission and lowered role made after it (never leaving no active admin),
ends every sign-in, and applies the removals again
(`decisions-apply --removals-only`, as `system (restore)`, keeping who decided
each and when, the admins' own withholds first). Only then do the services
start. It lists the user changes the restore does revert (a user created, a
permission granted or a role raised, a password changed after the backup). It
stops before any change when it cannot save these files, and leaves the
services stopped (exit 1, with the commands to run) when it cannot apply them
again; `-SkipBrainRemovals` and `-KeepRestoredAccess` accept the loss
explicitly and say what is lost.

An interrupted restore (Ctrl+C, a closed window, a reboot, a Docker restart
after `pg_restore`) is never mistaken for a finished one:
`backups\restore-pending.json`, written before the restore and deleted only
once the saved files are applied again, names them, and the database keeps a
mark of its own, written by the same command in the database container as
`pg_restore`, once it has committed (a closed window ends the script, not that
command). While either is there, `start.ps1` starts nothing, and the api
and the worker refuse to start (also after a plain `docker compose up` or from
another folder). The next `restore.ps1` applies those files first, never
exporting again from the restored data; `restore.ps1 -FinishPending` only
applies them and starts the services. Each apply clears its part of the
database's mark, and `restore.ps1` acts on `restore-pending.json` only as far
as the mark still asks: with no mark left (the files were applied, but the
marker stayed), it applies nothing again (that would undo what admins decided
since) and renames the stale marker. Only 0.6.2 and later read the mark:
never start 0.6.0 or 0.6.1 on a restored database (section 5).

**Version redactions** made after the backup are kept by a restore too: the
removals file names each one (item id, creation time, version, who and when;
no text), and it is applied again; when the restored backup predates the edit
that made it an older version, the redacted text is the item's current text
there: it is emptied, and the item deleted (restore it with new text, or
purge it).

**Importer and scripts.**
- New commands: `brain-import repair [--yes]`, `brain-import fingerprints
  --forget-other-key [--yes]`, `decisions-export --removals-only` and
  `decisions-apply --removals-only` (no import run needed), and
  `python -m app.cli restore-carry export|apply` (restore.ps1: the users'
  access and the audit log across a restore).
- An importer older than 1.1.0 (0.6.0, 0.6.1) is refused on a 0.6.2 database
  (migration `0008`), before it writes anything, and so is every archived
  message that code would store (also an import that was already running when
  0.6.2 was installed: it stops at its next write); so is a purge by 0.6.0 or
  0.6.1 code.
- `brain-import legacy-reset --export FILE [--yes]` (`jarvis-import.ps1 -Mode
  LegacyReset`): JARVIS Brain data that 0.6.0 or 0.6.1 imported is locked
  (section 4); the reset writes every decision to a version 5 file, rolls
  those imports back and lifts the lock. A version 3 decisions file (0.6.0,
  0.6.1) applies only through it (`--decisions FILE --actor ADMIN_EMAIL`);
  `decisions-apply` refuses it. A version 4 file (0.6.2 development builds)
  applies fail-safe: its withholds apply, its releases do not (release those
  rows again).
- When two changes to the JARVIS Brain meet (two admins at once, or an admin
  and an import), the second is refused with a message that names what can
  hold it (an import, rollback or rescan, or an admin's withhold, release,
  keep, purge or redaction); 0.6.1 always said an import was running.
- `brain-import status` prints more lines: migration
  `0008_brain_withhold_identity`, whether the identity repair is done,
  readable rows matching a withhold, held for review, withheld rows with a
  text fingerprint, rows blanked by the database guard, quotes of removed
  sources, and purge fingerprints made with another key; the withhold
  decisions not finished on their backup, the rows Settle found that wait for
  review and the rows a later backup held; the legacy lock; conversations an
  admin stopped importing and the newest import's tripwire. The dashboard
  shows the same counters to admins (Overview, "Withhold decisions"; Review,
  "Held for review"), and `brain-precheck.ps1` prints them.
- New commands: `brain-import settle --backup FILE` (`jarvis-import.ps1
  -Mode Settle`, typed confirmation `FINISH DECISIONS IN PRODUCTION` on
  live) and `brain-import run ... --decisions FILE` (`-Mode Import
  -Decisions`).
- `verify` has four more checks (9 to 12): every row that is a withheld
  message is withheld; no withheld row keeps a text fingerprint; purged items
  stay purged; the database guard never had to act.
- `jarvis-import.ps1 -Target Production` needs 0.6.2 on the live installation:
  migration `0008_brain_withhold_identity` and importer 1.1.0 in every mode;
  `Import` and `Resume` also need the identity repair done and no purge
  fingerprint made with another password. New: `-Mode Repair`.
- `brain-precheck.ps1` prints the identity counts.
- `brain-test.ps1 -SeedFromBackup` leaves out the live installation's purge
  fingerprints (the test copy's password differs; the purges stay).

**Audit records with IDs and counts only.** New entries:
`brain.withhold_identity_repaired`, `archive.hold_kept`,
`archive.hold_released`, `archive.message_held` (a row decisions-apply holds)
and `brain.purge_fingerprints_forgotten`. `archive.message_withheld` adds
`also_withheld`, `held_for_review`, `identical_withheld`, `keys_tombstoned`
and `title_hidden`. `brain.decisions_exported` and `brain.decisions_applied`
count releases, and a restore's re-apply says `"via": "restore"`. A restore
also writes `user.updated` (actor `system (restore)`) for each access change it
applies again and `system.restore_carried` with counts, and keeps the audit
entries made after the backup. No entry holds a title, text, note or search
query.

## 3. The contract, and the defaults

**What 0.6.2 promises** (the review attacks this, and only this). The
decision backup is the JARVIS backup file you withheld on in the test copy and
finished with Settle; a later backup is any other file.

| | On the decision backup (test copy, and live importing the same file) | On a later backup |
|---|---|---|
| **C1 Same backup** | After Settle and the export, live imports the same file (same SHA-256 and importer version) with the decisions file: every JARVIS row has the same state as on the test copy (withheld, held or readable, with the same review state). No withheld text is readable on live at any moment of that import | (not applicable: another file is a later backup) |
| **C2 Later backups** | (not applicable) | In a conversation family that holds a decision: (a) no stored row gains text or readability without an admin's release; (b) every new row is withheld (exact or copy identity of a withheld row), readable only when identical in role, tool call, time and text to a readable stored row of the family, or held for review with computed evidence; (c) conversations outside every family import as a plain import |
| **C3 Certainty** | Withheld for good, never listed, only on provenance: the exact JARVIS row; the same message id with the same text; an absorbed message id; the same tool call with the same text in the family; JARVIS's copy (same id, time, role and call); a copy-position row with a unique origin at the same time; an admin's keep. Everything else withheld is listed, releasable and carries computed evidence | The same certain rules for a new row; everything else in the family is held, never withheld for good. A release covers only the exact row and its content |
| **C4 Readers** | A withheld or held row has no text for any reader: admins (with or without the sensitive-data permission) and operators; agents, the Supervisor's history and recall; search and the sessions API; summaries, promoted copies, quotes and AI drafts written with removed memory; the titles of held new conversations | The same |
| **C5 Durability** | C1 to C4 hold after a restart, a rollback and a re-import, a `restore.ps1` restore and a re-import, and an upgrade from 0.5.0 | The same |
| **C6 Decisions** | Every admin decision (withhold, keep, release, left readable, hide title, stop importing), with who and when, survives the events of C5. Audit entries are never deleted | The same |

**Out of contract** (section 7, for your sign-off): paraphrases; verbatim
copies in conversations with no recorded link; edits JARVIS makes inside a
withheld conversation.

**Your answers to the owner questions, and the defaults 0.6.2 is built with.**
Each default is the fail-closed choice; an answer only switches an option.

| Question | Default in 0.6.2 |
|---|---|
| Q1 Later JARVIS backups: new messages in a conversation that holds a withheld message | Held until you look; one click releases the rows with no relation to the withheld text. (Not imported at all: "Stop importing this conversation". Shown automatically unless related: not in 0.6.2.) |
| Q2 How often you import later backups, and long-running chats | No default; the answer sizes your review work |
| Q3 Data imported by 0.6.0 or 0.6.1 | Locked until imported again from the JARVIS backup (legacy reset, section 4). Going back after 0.6.2 means going back to 0.5.0 only (section 5) |
| Q4 Live imports the identical file you reviewed | Yes: the import checks the file's SHA-256 and the importer version; if they differ, the conversations with a withheld message arrive held for review |
| Q5 Every hold Settle found is reviewed before the export | Yes: live then applies only your own decisions; on the test copy matching rows stay hidden until you review them |
| Q6 Nothing computed from a withheld text is ever stored | Yes. A released row's text comes back at the next import of a backup holding it. When JARVIS deleted the withheld message itself, the one-click release is not offered for that conversation: review row by row, or stop importing it |
| Q7 Edits, prunes and deletions JARVIS makes inside a withheld conversation are not copied | Accepted: the Command Center keeps what it had and never brings text back |
| Q8 Outside withheld conversations | (a) a "Replying to" quote of a withheld message is held for review; (b) any other row that contains a withheld text is listed to admins by location (tripwire), never hidden |

**Also unchanged defaults:** hide the conversation title when the withheld
message is its first user message (your choice in the dialog); holds never
expire; purges of items that came back are quarantined for an admin, never
purged automatically; purge fingerprints are keyed from the database
password (no new setting); restores apply again every withhold, purge,
release, title choice, version redaction and access narrowing made after the
backup before anything serves.

## 4. Upgrade

- **From 0.5.0, straight to 0.6.2:** follow
  `releases/UPGRADE_0.5.0_TO_0.6.2_WINDOWS.md` step by step (the owner's
  path). No intermediate release is installed. After `install.ps1`, the last
  migration is `0008_brain_withhold_identity`.
- **From 0.6.0 or 0.6.1 (withdrawn):** there is no separate guide; the same
  guide's notes cover it. Its step A stops when the API reports 0.6.0 or 0.6.1:
  send the output of `.\scripts\status.ps1` before going on.
- **JARVIS Brain data that 0.6.0 or 0.6.1 imported is locked.** The migration
  finds it (an import run by an importer older than 1.1.0 that was not rolled
  back, or a withhold made before 0.6.2) and locks it: no reader shows it, an
  admin included (search, conversations, memory items and the items promoted
  from it, agents, the Supervisor); imports, Settle, the decisions export and
  every withhold, keep and release refuse with a message that names the reset;
  `-Mode Status` and `brain-precheck.ps1` say `LOCKED`. Reset it in the live
  installation's folder:
  `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target Production -Mode LegacyReset`
  (type `RESET LEGACY BRAIN DATA`; a dump of the database is taken first). It
  binds each withhold to its exact JARVIS row where the stored data names one,
  writes every decision (withholds, purges, review decisions on imported
  items) to a version 5 file in the reports folder, rolls those imports back
  and lifts the lock; nothing an admin decided is lost, no audit entry is
  deleted, and the withheld keys stay withheld throughout. Then import the
  same JARVIS backup again with that file (`-Mode Import -All -Decisions
  <file> -Actor <admin email>`) and finish the decisions on it (`-Mode
  Settle`; review what it holds). A version 3 decisions file from a 0.6.x test
  copy goes with the reset: `-Mode LegacyReset -Decisions <file> -Actor <admin
  email>`.
- **Then check the JARVIS Brain** in the live installation's folder:
  `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target Production -Mode Status`.
  It must say `identity repair done`; if it says `not done`, run `-Mode
  Repair` (type `REPAIR PRODUCTION`). If `held for review` is above 0, review
  the rows on the JARVIS Brain page ("Held for review").
- **Finish or stop any import before upgrading.** The migration refuses to
  run while an import by 0.6.0 or 0.6.1 is running (finish or stop it, then
  start 0.6.2 again). An import still running when 0.6.2 starts is stopped by
  the database at its next write; run it again with 0.6.2 (after the legacy
  reset above).
- **No new keys, no `.env` change.** The new folder gets a copy of `.env`.
  n8n restarts once, as in every upgrade.
- **The test copy:** use 0.6.2 for it too
  (`C:\InfiniteAI\v0.6.2-brain-test`, `DEPLOY_WINDOWS.md`, "JARVIS Brain: test
  copy and import"). If a test copy on 0.6.x already holds review decisions,
  upgrade it as well (its data is locked: run the legacy reset there too),
  import the backup again, run Settle and **export the decisions again**: a
  version 5 file names the JARVIS rows and the backup they were decided on.
- **Changing the database password** later stops imports until the old one is
  restored or the old fingerprints are forgotten (section 2, "Purges").

## 5. Going back

- **Going back means going back to 0.5.0 only.** Never install 0.6.0 or
  0.6.1 over a 0.6.2 database (they are NO-GO): their code reads none of
  0.6.2's decisions (releases, holds, legacy lock, Settle) and ignores the
  restore mark, so after an unfinished restore it would serve every withhold,
  purge and access change made after the backup as undone.
- **To 0.5.0:** as section H of `releases/UPGRADE_0.5.0_TO_0.6.2_WINDOWS.md`
  describes (H-1, quick rollback, data kept); 0.5.0 ignores the JARVIS Brain
  tables.
- **Full restore (disaster only):** `restore.ps1` from the 0.6.2 folder, with
  a backup taken before the upgrade. It applies again the withholds, purges,
  releases and access narrowing made after that backup, before anything
  serves (section 2, "Restores"). The `restore.ps1` of an older version does
  not; use 0.6.2's.

## 6. Tests (cloud sandbox)

Synthetic data only, never real JARVIS data.

| Test | Result |
|---|---|
| Backend (pytest, real PostgreSQL 18), fresh database, Docker restore tests required | TODO |
| Every confirmed 0.6.1 exploit, replayed with its leak inverted, plus the over-withholding cases | TODO |
| Upgrade repair: a real 0.6.1 database made by the 0.6.1 importer, then 0.6.2 started | TODO |
| Dashboard (Vitest), type check, production build | TODO |
| End-to-end, operations (`testing/brain-check.sh`) | TODO |
| Speed: migration and import at the size of your backup (about 50,000 messages) | TODO (the migration alone: about 3 s on synthetic data) |
| Upgrade tests from the package (0.5.0, 0.6.0 and 0.6.1 → 0.6.2, going back) and the guide run | in `releases/GO_NO_GO_0.6.2.md` |

## 7. Known limitations and what stays open

**Residual risks (for your sign-off).** No design closes these; they are
outside the contract of section 3.
- **Paraphrases, summaries and titles a model wrote** stay readable: the
  conversation title (hidden by default when the first user message is
  withheld), compaction summaries, and replies or tool results that restate
  the withheld text in other words. Catching them would need matching by
  meaning, which requirement 2 forbids (it would also withhold unrelated
  text).
- **Verbatim copies in conversations with no recorded link:** a delegated
  sub-agent's conversation; a new chat where someone pastes the text; JARVIS's
  session search output in another conversation; memory files (purge them:
  the withhold dialog lists the memory items that contain the text). The
  tripwire lists such rows to admins, by location, when the backup holds the
  withheld text.
- **An old JARVIS rotation** with no `_compressed_from` marker, no copied
  times, and its parent deleted before any import saw it, is not linked to the
  withheld conversation: its copy imports as an unrelated conversation.
- **On the test copy only,** between a withhold and Settle: copies that the
  withhold's immediate blanking did not find stay readable until Settle runs
  (the export refuses until then, so live never sees this window). The same
  window exists after the legacy reset, between the re-import and Settle.
- **Withheld conversations do not follow JARVIS** (owner Q1 and Q7): JARVIS's
  later edits and deletions inside them are not reflected, and new turns there
  wait for your review.
- **A release brings back text you cannot see first,** and only at the next
  import of a backup holding that row (owner Q6). The evidence says how it
  relates to the withheld text; certain matches cannot be released.
- **Restore windows** beyond the targeted fixes (section 2, "Restores").
  Optional, later: `restore.ps1` could keep the decision, audit, redaction and
  access tables instead of applying them again.
- **Optional:** asking the JARVIS (Hermes) maintainers for a "copied from"
  message id on /branch, /retry and rotation copies would remove most of the
  remaining uncertainty. It does not block this release.

**Other limitations.**
- **AI drafts written before 0.6.2** recorded no memory items: a withhold or
  purge does not reach them. Text a draft already carried out of the Command
  Center (an Outbox entry of a send or publish) is a record of what went out
  and is not changed.
- **Text copied across kinds of records** (a withheld message written into
  MEMORY.md, a purged item quoted in a message) is listed in the withhold
  dialog, not matched automatically.
- **Whole-file purges made in the test copy travel by key.** A file renamed in
  the live JARVIS before the live import comes back as a pending item: purge
  it again (its fingerprint is then kept). Memory entries are covered by their
  digest.
- **Whole-file purges that already came back under a new key on 0.6.x**
  cannot be recognised (no data was kept); they are pending items to purge
  again.
- **A purge 0.6.0 or 0.6.1 made of an incoming file version, after the legacy
  reset,** keeps out the purged bytes by their keyed fingerprint; since the
  reset rolls the item back, JARVIS's current file (if it is that version) is
  purged under its key, so later versions of that file stay out too until an
  admin restores the item.
- **A redaction removes every AI draft written with that item,** also one
  written from its current text (drafts record items, not versions).
- **Memory entry keys** keep a 64-bit digest of the entry's text, as since
  0.6.0; re-keying them is planned for 0.7.x. A purged entry's key is in the
  database, in backups, in decisions files and in restore.ps1's
  `removals-before-restore-<time>.json`: keep and delete those files like
  backups.
- **A changed database password** stops imports until it is restored or the
  fingerprints are forgotten (audited); the purges stay either way.
- **A restore reverts the user changes that widen access** made after the
  backup: a user created, reactivated, given the sensitive-data permission or
  a higher role, and a changed password (the older one works again). It lists
  each one; redo them by hand. (Narrowing changes are applied again, and every
  sign-in is ended.) A backup of another installation keeps its own users as
  they are.
- **Purges made before 0.6.2 of items without a source key** (manual and
  promoted items, incoming versions) are not in the purge record 0.6.2 starts:
  a restore of a backup older than such a purge brings the item back, to purge
  again by hand. Purges with a source key travel by key, as before.
- **From 0.6.1, still open:** what a withhold cannot reach outside the
  Command Center (the JARVIS backup itself, older `backup.ps1` dumps, saved
  exports, text already sent to an AI model); access logs record request
  lines; detection is pattern-based (names and street addresses are not
  detected), so the live import stays NO-GO until you have reviewed the test
  copy.
