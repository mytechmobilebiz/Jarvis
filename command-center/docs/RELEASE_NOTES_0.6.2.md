# Infinite AI Command Center 0.6.2: release notes

Release date: TODO (after its verdict) · upgrade from 0.5.0, 0.6.0 or 0.6.1,
each straight to 0.6.2 · package `InfiniteAI-CommandCenter-0.6.2-windows.zip`
in `command-center/releases/` on the GitHub branch `jarvis-migration`, with its
SHA-256 in the `.sha256` file next to it · step-by-step Windows guide:
`releases/UPGRADE_0.6.1_TO_0.6.2_WINDOWS.md` (with sections for 0.5.0 and
0.6.0) · verdict: `releases/GO_NO_GO_0.6.2.md`

> **Status: pending its verdict.** 0.6.2 replaces 0.6.0 and 0.6.1, which are
> NO-GO for installation and for any JARVIS import (security notice of
> 2026-10-20). Do not install 0.6.2 until `releases/GO_NO_GO_0.6.2.md` says GO.

0.6.2 is a JARVIS Brain security release. In 0.6.0 and 0.6.1, the text of a
JARVIS message that an admin withheld came back when a later JARVIS backup was
imported. 0.6.2 ties every withhold to the JARVIS message itself, not to one
of the names the importer gave it, so the text stays out whatever JARVIS does
with the message later. It repairs, at its first start, every database where
the text already came back. Purged memory items, restores of older backups and
every reader (people, agents, the Supervisor, search and the API) are covered
too. Nothing is withheld only because its words match: section 3 lists exactly
what is withheld on its own and what waits for an admin.

**What the upgrade changes in your installation.**
- **One database migration, `0008_brain_withhold_identity`.** It adds columns
  to the JARVIS Brain's list of removed sources, one column to archived
  messages, three small tables (releases of held rows, fingerprints of purged
  items, and repair markers), a database guard and two functions. It runs in
  the database before the application serves its first request. On a database
  with nothing imported (your PC today) it finds nothing to repair and writes
  no audit entry.
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

**A withhold follows the JARVIS message.**
- A withhold records the JARVIS row the admin withheld: its conversation, row
  number, role, tool call and message id. Each other name of the same message
  gets the same record, in the admin's group.
- **Certain matches** are withheld for good and can never be released: the
  same JARVIS row under another key, the importer's own copies (a carried-on
  copy, a repeat right after the message), the copies JARVIS made of the
  withheld row in a continuation or branch (with its message id, or with a
  fresh one at the row's very time; also when they were stored before the
  withhold), a message merged into another (also one stored before the
  withhold), and the same message id with the same text (also restated after
  a compaction, or containing it).
- **Uncertain matches are held for review**, never withheld for good on a
  guess: the same message id with different text (the evidence says when it
  contains the withheld text), the exact same text later in the same
  conversation or one continued from it, the same text sent again after a
  `/retry` or a rewind, and what the upgrade repair found. A held row has no
  text, and an admin keeps it withheld or releases it (below). A hold stays a
  hold until an admin decides; no later compaction turns it into a certain
  match.
- **Text alone never withholds anything.** The same words in another
  conversation, similar words, text the admin saw before the withhold, a
  later repeat that JARVIS gave its own message id, parallel tool results, a
  continuation's or branch's copy of another message with the same text, and
  the parent of a restricted branch all stay readable. Section 3 has the full
  list.
- **A release follows the message.** It names a JARVIS row and covers that
  row's own copies (compaction generations, the protected tail, a branch with
  the same message id), the row after JARVIS renumbered it or gave it a
  message id, and nothing overrides it except the withheld message's own row
  or its own text. Each copy it covers is recorded, so it outlives the
  original. An upgrade hold the next import disproves (another tool call,
  another message id) is released by that import, audited.
- **No fingerprint of a withheld text is kept.** A withheld row keeps only its
  state (`w1:live`, `w1:compacted`, `w1:rewound`); 0.6.x fingerprints are
  removed by the migration. Texts are compared only in memory, during an
  import or a withhold.
- **Rollback copies.** The withhold also blanks, at once, the rollback copy
  of a row of the message that an import deleted (the original JARVIS rewound
  in an in-place compaction, a branch's rewound copy: the same message id and
  text, or, without message ids, the rewound row the same import wrote
  again). A rollback of that import brings the row back withheld. Another
  message with the same words keeps its rollback copy.
- **A database guard** blanks any row the withhold names, whatever code writes
  it. `verify` reports it if the guard ever had to act.
- **Repair.** Every database where 0.6.0 or 0.6.1 let the text come back is
  repaired during the migration, before the first request; the repair runs
  again at every start, before every import, with `rescan --yes` and with the
  new `brain-import repair --yes`.
- **Changed keys.** When JARVIS changes only a message's key (message ids
  filled in, rows renumbered), the stored row takes the new key instead of the
  conversation being stored twice. A key that JARVIS gave to another, still
  present row is left out as a key collision: no row is ever overwritten with
  another row's text, or withheld because of another message.

**The withhold dialog** (JARVIS Brain page) first counts the other stored rows
of the same JARVIS message (withheld too) and those held for your review. It
offers:
- "Also withhold K other messages with exactly this text in this
  conversation and its continuations and branches", unticked;
- "Hide the conversation title", ticked when the message is the conversation's
  first user message (a title can repeat it). It also hides the title in the
  continuations and branches that carry it (JARVIS moves the title to a
  continuation and names a branch "title #2"), and it is kept with the
  withhold: a conversation imported again (after a rollback or a restore) and
  the live installation (decisions file) hide it too. A history answer or
  request summary that quoted another message of that conversation keeps the
  quote but names the conversation as "session <id>" instead of the hidden
  title, at once, and the start-up check does the same for older answers;
- the imported memory items whose text contains the message, as links, so you
  can purge them. Nothing is purged automatically.

**Held for review** is a new panel for admins. It shows each held row's
evidence and never its text: where it is, its length, the rule that held it,
the withheld message it may be, and its neighbours. The withheld message is
described only to an admin who may open it (an admin without the
sensitive-data permission sees no id, position, time or length of a Credit
Repair or restricted message), and the withhold dialog counts rows outside
your access in one sentence of their own. **Keep withheld** makes it
permanent. **Release** asks for confirmation; its text comes back at the next
import of a backup that holds it, and promoted copies emptied meanwhile stay
empty. Certain matches are never listed and cannot be released.

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

**Purges.**
- A purged memory entry is recognised in any memory file by the digest
  already in its key.
- A copy of a purged file in the same backup is recognised by its bytes.
- A purged file that is renamed later is recognised by a keyed fingerprint of
  its bytes. The key is derived from the database password, so a fingerprint
  means nothing outside your database and no new secret exists.
- An item that already came back is quarantined for an admin (only admins see
  it, no agent reads it); nothing is purged automatically.
- If the database password changes, imports stop until the old password is
  restored or `brain-import fingerprints --forget-other-key --yes` forgets the
  old fingerprints (audited). The purges themselves stay.

**Decisions from the test copy (file version 4).** Each withhold travels with
the JARVIS row it names and the admin's title choice, as do the holds an
admin kept, the holds still waiting for review (held for review on live too)
and the rows an admin released; never text or a fingerprint. When live
stores no row of a withheld message, decisions-apply still withholds what the
live import stored of it: the copy JARVIS wrote again of a row the import
deleted as rewound, and a message JARVIS merged it into. Version 3 files
(0.6.0, 0.6.1) are still read, but a version 3 withhold names only a key:
export again with 0.6.2.

**Restores.** Before it stops anything, `restore.ps1` saves every withhold
(with the title choice), every purge (by key, and by item id and creation time,
so manual and promoted items and incoming versions are covered too), every
release, the keyed purge fingerprints, who decided each and when
(`backups\removals-before-restore-<time>.json`, keys and ids only), and the
users' access and the audit log (`backups\access-before-restore-<time>.json`,
e-mail addresses and audit entries; no password hash). It uses a helper
container of the api image, so the api need not be running. After the restore,
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

**Importer and scripts.**
- New commands: `brain-import repair [--yes]`, `brain-import fingerprints
  --forget-other-key [--yes]`, `decisions-export --removals-only` and
  `decisions-apply --removals-only` (no import run needed), and
  `python -m app.cli restore-carry export|apply` (restore.ps1: the users'
  access and the audit log across a restore).
- An importer older than 1.1.0 (0.6.0, 0.6.1) is refused on a 0.6.2 database
  (migration `0008`), before it writes anything.
- `brain-import status` prints one more line: migration
  `0008_brain_withhold_identity`, whether the identity repair is done,
  readable rows matching a withhold, held for review, withheld rows with a
  text fingerprint, rows blanked by the database guard, quotes of removed
  sources, and purge fingerprints made with another key.
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

## 3. The decisions and the defaults

| Question | 0.6.2 |
|---|---|
| What is withheld without asking you | Certain matches only (section 2). Never because of text alone |
| What waits for you | Held rows: the same message id with other text, the same text again in the conversation or a continuation of it, and what the upgrade repair found |
| "Also withhold messages with exactly this text" | Off; you tick it per withhold |
| Hide the conversation title | On when the message is the conversation's first user message; your choice in the dialog |
| Holds expire | Never; they stay withheld until you keep or release them |
| A release | Takes effect at the next import of a backup holding that row; promoted copies emptied while it was held stay empty |
| Certain matches | Never releasable |
| Purges of items that came back | Quarantined for an admin; never purged automatically |
| Purge fingerprints | Keyed from the database password; no new setting; the importer container still gets only `DATABASE_URL`, `TZ` and `ENVIRONMENT` |
| Restores | Withholds, purges, releases, title choices, deactivations, revoked permissions, lowered roles and audit entries made after the backup are applied again before anything serves; other user changes it reverts are listed |

Each default can be changed later without a migration.

**Withheld on purpose (fail-closed), also when it might not be the message:**
- a verbatim repeat right after the withheld message;
- in a backup without message ids, a later verbatim repeat in the same
  conversation (held: you can release it);
- a message sharing the message id with the same role when nothing in the
  backup can be compared (in its own conversation, continuations and
  branches; elsewhere it is held); decisions-apply does the same on live
  when no row of the withheld message is stored there;
- a copy in a branch or continuation at the very time of a withheld row
  that JARVIS no longer has (held: you can release it);
- a message JARVIS merged another one into;
- a row that 0.6.1 itself rewrote and withheld under a reused id;
- what the upgrade repair finds by the old fingerprints or message ids (held:
  you can release it).

**Always readable:** the same short text in another conversation ("Thanks,
that is all."); similar text; identical text stored before the withhold; a
later repeat with its own message id; parallel tool results; an unrelated row
that took over a key; the same id with another role; copies of messages that
are not withheld; the paired tool result, compaction summary and other turns;
new turns of a continuation or branch; a delegated (subagent) conversation;
the parent of a restricted branch; a repeat first seen already compacted with
its own id; a released hold; a memory entry with other text or a file with the
same name and other bytes; an unrelated row's rollback copy.

## 4. Upgrade

- **From 0.6.1, 0.6.0 or 0.5.0, straight to 0.6.2:** follow
  `releases/UPGRADE_0.6.1_TO_0.6.2_WINDOWS.md` (its sections for 0.5.0 and
  0.6.0 cover those). No intermediate release is installed. After
  `install.ps1`, the last migration is `0008_brain_withhold_identity`.
- **Then check the JARVIS Brain** in the live installation's folder:
  `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target Production -Mode Status`.
  It must say `identity repair done`; if it says `not done`, run `-Mode
  Repair` (type `REPAIR PRODUCTION`). If `held for review` is above 0, review
  the rows on the JARVIS Brain page ("Held for review").
- **Finish or stop any import before upgrading.** The migration locks rows; an
  import running alongside it can make PostgreSQL abort one of the two, and
  the next start tries again.
- **No new keys, no `.env` change.** The new folder gets a copy of `.env`.
  n8n restarts once, as in every upgrade.
- **The test copy:** use 0.6.2 for it too
  (`C:\InfiniteAI\v0.6.2-brain-test`, `DEPLOY_WINDOWS.md`, "JARVIS Brain: test
  copy and import"). If a test copy on 0.6.x already holds review decisions,
  upgrade it as well and **export the decisions again**: a version 4 file
  names the JARVIS rows.
- **Changing the database password** later stops imports until the old one is
  restored or the old fingerprints are forgotten (section 2, "Purges").

## 5. Going back

- **Quick, data kept, to 0.6.1 or 0.6.0:** `install.ps1` from that version's
  folder. Its code runs on the 0.6.2 schema (every new column has a default)
  and the database guard keeps blanking every row a withhold names. But that
  code is NO-GO for imports: import nothing until you are on 0.6.2 again
  (`jarvis-import.ps1` of 0.6.2 refuses a live installation without importer
  1.1.0, and the 0.6.2 database itself refuses an import run of an older
  importer before it writes anything). Back on 0.6.2, its start repairs what
  0.6.1 code left readable by key, JARVIS row or uid; a copy an older import
  stored under a new key with no uid is repaired by the next 0.6.2 import.
- **To 0.5.0:** as the 0.5.0 section of the 0.6.2 guide describes; 0.5.0
  ignores the JARVIS Brain tables.
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

- **Paraphrases stay readable:** the conversation title (hidden by default when
  the first user message is withheld), compaction summaries, and replies or
  tool results that restate the withheld text. Catching them would need
  matching by meaning, which would also withhold unrelated text. The withhold
  dialog lists memory items that contain the text, so you can purge them.
- **An old JARVIS without message ids:** if every stored copy of a withheld
  message was physically deleted before any 0.6.2 import saw a copy, a later
  copy cannot be recognised, because no fingerprint is kept. Current JARVIS
  gives every row a message id, copies keep it, and rewinds are soft.
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
- **A release brings back text you cannot see first.** The evidence says what
  it is, and certain matches cannot be released.
- **`/retry` in a gateway conversation after an import:** the gateway deletes
  the original rows before it sends the text again, so nothing in the backup
  can be compared and the re-sent message stays readable. In the CLI (rows
  rewound, not deleted) it is held.
- **A hidden title hides the continuations and branches that carry the same
  title,** also when a branch's first message differs (only the title, never
  a message).
- **0.6.1's victim of a reused id** (a row it rewrote and withheld) stays
  withheld.
- **Memory entry keys** keep a 64-bit digest of the entry's text, as since
  0.6.0; re-keying them is planned for 0.7.x.
- **One upgrade gap until the next import:** a copy that shares only its
  message id with the withheld message, under a row key, whose fingerprint
  changed (for example its compaction state) stays readable after the upgrade
  until the next import, which repairs before it imports. `status` cannot
  count it; `verify` checks it after that import.
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
