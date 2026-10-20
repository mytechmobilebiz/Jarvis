# Infinite AI Command Center 0.6.1: release notes

Release date: 2026-10-20 · upgrade from 0.6.0 (or from 0.5.0 after the 0.6.0
guide) · package `InfiniteAI-CommandCenter-0.6.1-windows.zip` in
`command-center/releases/` on the GitHub branch `jarvis-migration`, with its
SHA-256 in the `.sha256` file next to it · step-by-step Windows guide:
`releases/UPGRADE_0.6.0_TO_0.6.1_WINDOWS.md`

> **Security notice, 2026-10-20: 0.6.1 is NO-GO, for installation and for any
> JARVIS import.** A verification of the published 0.6.1 found a high-severity
> defect, reproduced three times independently on the exact published code.
> The text of a JARVIS message that an admin withheld comes back when a
> **later** JARVIS backup is imported. JARVIS's compaction stores copies of
> messages under new identifiers, and the importer withholds only the
> identifier that was withheld, so a copy is imported with its text.
> Operators, admins, the Supervisor's history and recall, and newly promoted
> memory items can then read it (viewers cannot), and the verification
> commands still report success. 0.6.0 has the same defect.
>
> - Do not install 0.6.1, and do not import JARVIS data with any 0.6.x
>   release.
> - If you are still on 0.5.0, stay there. 0.6.2 corrects the defect
>   ([RELEASE_NOTES_0.6.2.md](RELEASE_NOTES_0.6.2.md)), with a direct
>   0.5.0 -> 0.6.2 guide; it is pending its verdict
>   (`releases/GO_NO_GO_0.6.2.md`) and is installed only after that says GO.
> - An installation of 0.6.0 or 0.6.1 that has imported nothing holds no
>   JARVIS data and is not exposed: leave it as it is and import nothing.

0.6.1 is a JARVIS Brain patch. It fixes the 0.6.0 defect where an admin could
not withhold an archived JARVIS message once it had a promoted memory copy.
A withhold now removes the message's text from every promoted memory copy
(and all of its versions), from the history answers and reference notes that
quote them, and from the import records' rollback copies; an emptied copy can
then be deleted and purged. Section 6 lists what a withhold does not change,
such as the conversation's title and related messages.

**What the upgrade changes in your installation.**
- **One database migration, `0007_brain_withhold`.** It replaces one check on
  the JARVIS Brain version table so that it allows one more value. It also
  adds two indexes that make the new copy and quote look-ups fast. It adds no
  table and changes no data.
- **Short database error messages.** On every start, `db-init` sets
  `log_error_verbosity = terse` for the Command Center's own database user, so
  a refused row's values never reach the PostgreSQL log.
- **A start-up check.** It empties promoted copies of withheld messages that
  0.6.0 left with text. On your PC nothing has been imported, so it finds
  nothing.

**What it does not change.** It leaves all of these as they are:
- your `.env` and encryption keys;
- the PostgreSQL and n8n volumes;
- users, stored secrets and settings;
- the agents and their on/off states, and your approval rules;
- schedules, sites and leads;
- the n8n workflows and credentials.

It imports nothing and switches nothing on. Memory in AI prompts stays off.

## 1. What was wrong in 0.6.0

- **The withhold failed.** When a promoted copy of the message existed and was
  not already quarantined, withholding the message wrote a version entry
  (`quarantined`) that the database's check did not allow. The whole withhold
  was rolled back: the message, its promoted copies and its rollback copies
  kept their text, and no tombstone was written. The page showed an error.
- **The failure wrote text into logs.** PostgreSQL's error detail ("Failing
  row contains ...") holds up to 64 characters of each column of the refused
  row, so the copy's title and text went into the PostgreSQL log, and the same
  detail into the API's error log.
- **A successful withhold left copies behind.** Where it worked (an already
  quarantined copy), the copy kept its full text and every version, and an
  admin could approve it again. Copies made before a rollback and re-import
  were not found. Withholds made by an import or a rescan never touched the
  copies at all.

## 2. What 0.6.1 changes

**Withholding empties every copy, for good.**
- When an admin withholds a message, every memory item promoted from it is
  emptied. That includes pending, approved, rejected, archived, quarantined,
  deleted and admin-edited copies.
- Emptied means the title, text, tags and review notes of the item and of
  **every earlier version** are removed. The item stays as a quarantined
  placeholder (`Withheld: the archived message it was promoted from was
  withheld`) with its IDs, version numbers and history of who did what. It
  can never be approved, reopened or edited again; an admin can delete and
  purge it.
- Copies are found by the message's ID, by its stable source key (a new flag,
  so a copy is still found after a rollback and re-import), or, for copies
  made by 0.6.0, by their source reference.
- Lines of history answers that quote the message or its copies, including
  quotes of older rows of the same message, are replaced with a removal note,
  also where a request summary copies them; reference notes that name a copy
  are removed.
- Rollback copies of the message in the import records are blanked.

**One routine for every way a message gets withheld.** The same steps run
when:
- an admin withholds a message;
- an import writes a restricted conversation;
- a rescan withholds a conversation;
- decisions-apply carries over a withhold from the test copy, also when the
  message no longer has a row;
- a rollback re-inserts a message an admin had withheld;
- the start-up check runs.

**Nothing reads a copy of a withheld message.**
- Agents never read one, whatever its status, even before it is emptied: not
  through agent memory, AI prompts, the Supervisor's history answers or
  reference notes.
- Viewers and operators don't see one until it is empty, and admin exports
  leave it out.

**Purge.**
- Dismissed and keep-both conflict decisions no longer block a purge.
- An emptied copy can be purged even when a conflict decision names it.
- The purge dialog for a promoted item says that the conversation message it
  came from is kept, and must be withheld separately.

**No two changes at once.**
- Withhold, purge and redact answer "an import, rollback or rescan is running:
  try again when it has finished" (409) instead of racing an import.
- Promote locks the message, so a promote and a withhold of the same message
  cannot cross.
- Decisions-apply takes the import lock.
- A history answer waits for a removal that is running (the task is queued
  again, it never holds a worker slot).

**Database errors never carry text.**
- The API answers "the database refused this change; nothing was saved" (409
  or 500).
- The API and the worker log only the error's class and SQLSTATE, never the
  row's values.
- The app's PostgreSQL user logs terse errors (above).

**Audit records with IDs and counts only.**
- New entries: `memory.source_withheld` (one per emptied copy, with its cause:
  admin, import, rescan, decisions-apply, rollback or start-up check) and
  `brain.decisions_exported`.
- More keys in existing entries: `archive.message_withheld` (the copies,
  versions blanked, quotes removed, tombstone), `memory.purged` and
  `memory.version_redacted` (quotes removed).
- Rescan reports how many copies it emptied (`copies_withheld`,
  `copy_versions_blanked`). A rollback records how many withheld messages it
  put back without text (`withheld_reinserted`) and which emptied copies it
  left as they are (`kept_withheld_copies`); every copy it empties gets its
  own `memory.source_withheld` entry.
- No entry holds a title, text, tag, note, source reference or search query.

**Importer and scripts.**
- The importer is version 1.0.1.
- `brain-import status` prints whether `0007_brain_withhold` is present and
  how many withheld copies still hold text.
- `jarvis-import.ps1 -Target Production` needs 0.6.1's migration on the live
  installation for every mode. `Import` and `Resume` also need a 0.6.1
  importer and that count to be 0; every other mode (Plan, Status, Verify,
  Rollback, ExportDecisions, ApplyDecisions, Rescan) runs and shows the
  count.
- `brain-precheck.ps1` prints both.

**Software bill of materials.** `SBOM.md` and `sbom.cdx.json` were generated
again from a backend image built from 0.6.1. Three indirect Python packages
are newer than in 0.6.0, because a build today resolves them anew: httpcore2
and httpx2 2.15.1 (were 2.14.0) and websockets 17.3 (was 17.2).
`requirements.txt` pins only the direct dependencies, so a build on your PC
can get later versions of indirect ones (minor versions too, as above).

**Dashboard (JARVIS Brain page).**
- The withhold dialog counts the promoted copies first and says exactly what
  is removed and what cannot be reached.
- An emptied item shows that its text was removed, with no Approve, Reopen or
  Edit.
- The review queue's Approve button is disabled for it.

## 3. Upgrade

- **From 0.6.0:** follow `releases/UPGRADE_0.6.0_TO_0.6.1_WINDOWS.md`, which
  covers preflight, verified backups, the agent configuration before and after,
  the checksum, the install from a new folder, the checks and the rollback.
- **From 0.5.0 (your PC today):** first install 0.6.0 with
  `releases/UPGRADE_0.5.0_TO_0.6.0_WINDOWS.md`. Once its upgrade report is
  good, follow the 0.6.1 guide. The direct route 0.5.0 → 0.6.1 was also
  tested (section 5).
- **No new keys, no `.env` change.** The new folder gets a copy of `.env`.
  n8n restarts once, as in every upgrade.
- **Before the JARVIS test-copy import:** use 0.6.1 for the test copy too
  (`DEPLOY_WINDOWS.md`, "JARVIS Brain: test copy and import").

## 4. Going back

- **Quick, data kept:** `install.ps1` from the 0.6.0 folder (guide section H).
  0.6.0 works with migration `0007_brain_withhold`, because it only allows
  one more value; the indexes and the terse setting are harmless.
- **What changes on 0.6.0 after 0.6.1 ran:**
  - 0.6.0 does not know the `source_withheld` flag. An emptied copy holds no
    text, so re-approving it there shows only the placeholder. The next start
    of 0.6.1 quarantines it again.
  - On 0.6.0 a withhold of a message with a promoted copy now succeeds
    (the check allows `quarantined`), but it leaves the copy's text in a
    quarantined item, as 0.6.0 always did. On 0.6.0, delete and purge the
    copy before withholding the message. The next start of 0.6.1 empties it.
  - 0.6.0 may refuse to roll back an import while a conflict decision names
    an emptied copy.
- **Full restore (disaster only):** `restore.ps1` with the backup taken before
  the upgrade. It brings back text that was withheld after that backup, and
  drops later tombstones. Prefer the quick route.

## 5. Tests (cloud sandbox)

Synthetic data only, never real JARVIS data.

| Test | Result |
|---|---|
| Backend (pytest, real PostgreSQL 18), fresh database, Docker restore tests required | **965 passed, 0 failed, 0 skipped** (853 in 0.6.0). 420 are JARVIS Brain tests (307 in 0.6.0); 112 are new for 0.6.1 |
| The eight areas you asked for | withholding before promotion; withholding after promotion (pending, approved, rejected, archived, quarantined, deleted and admin-edited copies, several copies, copies made before a rollback and re-import, 0.6.0 copies); purging promoted memories; conflict handling; test-copy review decisions (export and apply, with and without the message's row, refused withholds keeping their tombstone); later live-import consistency (re-import, changed source, resume, restricted conversations, rescan, start-up check); rollback; agent access (agent memory, AI prompts, history answers, reference notes, every role, export). Planted markers are searched for in every text column of every table, and in the logs |
| Each fix proves its test | each code fix of the two review rounds was undone once in a scratch copy: its test failed, and passed again with the fix. Two first-round exceptions: a redundant guard (another check covers it), and the worker's first quote lock, which no test caught; the second round replaced that lock with one whose test fails when it is undone |
| Dashboard (Vitest), type check, production build | **225 passed**, type check clean, build succeeded (`version.json`: 0.6.1) |
| End-to-end, operations (`testing/brain-check.sh`, PowerShell 7.4, Docker) | **100 passed, 0 failed, 0 skipped**, including the 19 browser checks of the Brain page and the new check that the app's PostgreSQL user logs terse errors (a refused row's values never reach the log). The sandbox's running installation, playing yours, was unchanged from start to end |
| Speed, synthetic data the size of your backup | the agents' memory check took 13 ms and the operators' item count 12 ms (an earlier build of 0.6.1 took 323 ms and 503 ms, before its new withheld-message rule was rewritten to use a new index) |
| Upgrade tests from the package (0.5.0 → 0.6.0 → 0.6.1, 0.5.0 → 0.6.1, going back to 0.6.0) and the guide run | in `releases/GO_NO_GO_0.6.1.md` (they run on this package, so they are recorded next to it) |

**Reviews.**
- Three read-only investigations mapped every copy of message text, the
  withhold and purge lifecycle, and every read path.
- A written specification came next, then the build.
- Three independent reviewers (privacy, correctness and upgrade safety, test
  adequacy) reported 25 findings. A separate skeptic per finding confirmed 24;
  all are fixed.
- A second, independent round (concurrency and speed at your data's size,
  privacy, upgrade and operations) reported 16 findings. 13 were confirmed,
  and all are fixed.

## 6. Known limitations and what stays open

- **Not changed by a withhold:**
  - the conversation's title, which can come from the first message, and
    recall quotes it with other lines;
  - related messages (a paired tool result, a summary, a copy in a branch),
    which must be withheld one by one;
  - the message's fingerprint (`source_fp`, an unsalted SHA-256), which the
    import needs; no API returns it.
- **Outside the Command Center's reach:**
  - the JARVIS backup, and `backup.ps1` dumps made before the withhold;
  - saved exports and decisions files, and the test copy;
  - text already sent to an AI model, and drafts written with memory in a
    prompt (off by default);
  - old PostgreSQL row versions until VACUUM, and Docker container logs.
- **Old log lines:** the upgrade does not remove log lines that 0.6.0 already
  wrote. If a withhold ever failed on 0.6.0 (on your PC nothing was imported,
  so it never did):
  - recreate the live containers: in the live installation's folder
    (`Set-Location C:\InfiniteAI\v0.6.1\InfiniteAI-CommandCenter`) run
    `docker compose down` (never `-v`), then
    `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1 -NoBuild`
    (n8n restarts too); `docker restart` keeps the log;
  - for a 0.6.0 test copy, in the test copy's folder run
    `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\brain-test.ps1 -Action Remove`
    and type `REMOVE TEST COPY` when asked. This deletes the test copy with
    its database, including its imports and review decisions.
- **The start-up check is skipped, not waited for,** while an import,
  rollback or rescan holds the lock, or a history answer is being written. If
  `brain-import status` (in the live installation's folder:
  `docker compose exec -T api python -m app.cli brain-import status`) then
  still shows withheld copies with text above 0, restart the api and the
  worker there with `docker compose restart api worker` (both run the check
  when they start). Or, in the same folder, run
  `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target Production -Mode Rescan`
  and type `RESCAN PRODUCTION` when asked. Agents never read such copies
  meanwhile.
- **Two admin removals at the same moment:** one may get the 409 "an import,
  rollback or rescan is running"; try again.
- **Access logs:** uvicorn's and nginx's access logs record full request
  lines, including Brain search queries. Changing that is a product-wide
  decision for later.
- **Still open from 0.6.0:** detection is pattern-based (names and street
  addresses are not detected), so the live import stays NO-GO until you have
  reviewed the test copy. From earlier releases: the server gate cannot see
  an approval; no run on a real server; remote access is not set up.
