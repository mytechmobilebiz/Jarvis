# Infinite AI Command Center 0.6.0: release notes

Release date: 2026-10-20 · upgrade from 0.5.0 or 0.5.1 (section 4) · package
`InfiniteAI-CommandCenter-0.6.0-windows.zip` in `command-center/releases/` on
the GitHub branch `jarvis-migration`, with its SHA-256 in the `.sha256` file
next to it

0.6.0 adds the **JARVIS Brain**: the Command Center's own memory, and a way to
bring JARVIS's knowledge and conversation history into it, first into a test
copy on your PC and only after your approval into the live installation.

**Not installed, nothing imported.** This release has not been installed on
your PC, and none of JARVIS's real data has been imported anywhere. Each of
those is a step you approve (section 8). JARVIS stays installed and running
throughout; nothing here changes it.

**What an upgrade changes in your data:** one database migration
(`0006_brain`) adds nine new, empty tables. No existing table, setting, key,
volume, agent or approval rule is changed, and no new key is needed. Memory in
AI prompts is **off** until an admin switches it on.

## 1. What 0.6.0 adds

- **Memory** (JARVIS Brain page, Memory): business facts, knowledge, agent
  notes, decisions, projects, accomplishments and problems, plus JARVIS's
  operating instructions and skills as reference text. Every item has a
  source, a review status (pending, quarantined, approved, rejected,
  archived), a sensitivity (general or customer) and a full version history.
  Nothing is used by an agent until a person approves it.
- **Conversation archive**: JARVIS's sessions and messages, cleaned (section
  2), searchable, with the staff member, the date and the labels
  accomplishment, problem and decision.
- **Agents use memory as reference only.** Each agent reads only approved
  items in its scope; the AI Supervisor answers questions about JARVIS's
  history ("what problems did JARVIS have with SSL?") locally, from the
  archive and approved memory, without any AI model. Memory goes into AI
  prompts only if an admin turns it on in Settings, and then only approved,
  general items, never customer data, never anything flagged.
- **The importer** (`scripts\jarvis-import.ps1`): a plan that writes nothing;
  staged imports (5 sessions, about 10 %, then everything); verify; resume
  after an interruption; rollback of a single run; a re-run adds nothing.
- **Workflows and skills stay off.** JARVIS's scheduled jobs become drafts
  that never run. Converting one (an admin, after review) creates a Command
  Center schedule that is **disabled**. Websites JARVIS watched are offered
  as sites that start **paused**.
- **A test copy** (`scripts\brain-test.ps1`): a separate Docker project on
  port 8190 with its own database and secrets, no n8n, and a worker without
  internet, so you can import and review everything before the live
  installation sees any of it.

## 2. What is protected

- **Secrets are never stored.** Passwords, API keys, tokens, webhook
  addresses and passwords on command lines are replaced by
  `[REDACTED:<kind>]` before anything is written. The import report lists the
  kinds and counts (never the values) so you can rotate them.
- **Restricted data keeps no text at all.** A conversation that holds an SSN,
  a card or bank number, a date of birth or a client's credit file is
  withheld entirely: its messages are kept as empty rows, for counting only.
  Credit Repair material stays in the Credit Repair module.
- **Customer contact details are masked** (`[email]`, `[phone]`), and
  conversations with customers are visible to admins only and never reach an
  AI model.
- **Private reasoning is never read** from JARVIS's database, and reasoning
  tags in message text are removed.
- **Imported text is data, never instructions.** JARVIS's rules that bypass
  approval ("Email is pre-approved", "Always send proposals without asking")
  are imported quarantined, need an admin, and never reach a prompt even when
  approved. The Command Center's approvals are code; no import changes a
  setting, an agent's state, a schedule or an approval. Sending a proposal
  still waits for a person, exactly as before (tested).
- **Withheld stays withheld.** Text withheld by an import, by an admin or by
  a rescan never comes back through a later import or a rollback, and its
  copies in history answers are removed.
- **The importer has no network** and runs from the live installation's own
  image, with the backup and the restored files mounted read-only.

## 3. Before you upgrade: from 0.5.0 directly

You can go **straight from your installed 0.5.0 to 0.6.0**; installing 0.5.1
first is not needed. 0.6.0 contains every 0.5.1 change, including the fix for
the "the API reports 0.2.0" problem you saw with 0.5.0. Both paths were
tested on a copy of a 0.5.0 installation with your history (section 9):
0.5.0 → 0.6.0, and 0.5.0 → 0.5.1 → 0.6.0.

## 4. Upgrade (when you approve it)

Your current folder is `C:\InfiniteAI\v0.5.0\InfiniteAI-CommandCenter`. Run the
steps in one PowerShell window, in order.

1. **Name the two folders and back up.** The `Test-Path` line must print
   `True`; if it prints `False`, set `$old` to the folder that holds your
   `.env`.

   ```powershell
   $old = "C:\InfiniteAI\v0.5.0\InfiniteAI-CommandCenter"
   $new = "C:\InfiniteAI\v0.6.0\InfiniteAI-CommandCenter"
   Test-Path "$old\.env"
   Set-Location $old
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup.ps1
   Copy-Item "$old\.env" "C:\InfiniteAI\env-backup-0.5.0.txt"
   ```

   `backup.ps1` writes the Command Center, n8n and Credit Repair databases to
   `$old\backups\<time>`. Keep `env-backup-0.5.0.txt` private: it holds your
   passwords and encryption keys.

2. **Download and check the package** from GitHub, repository
   `mytechmobilebiz/Jarvis`, branch `jarvis-migration`, folder
   `command-center/releases`: the `.zip` and its `.sha256`:
   - https://github.com/mytechmobilebiz/Jarvis/raw/jarvis-migration/command-center/releases/InfiniteAI-CommandCenter-0.6.0-windows.zip
   - https://github.com/mytechmobilebiz/Jarvis/raw/jarvis-migration/command-center/releases/InfiniteAI-CommandCenter-0.6.0-windows.zip.sha256

   Then:

   ```powershell
   Get-FileHash "$env:USERPROFILE\Downloads\InfiniteAI-CommandCenter-0.6.0-windows.zip" -Algorithm SHA256
   Get-Content "$env:USERPROFILE\Downloads\InfiniteAI-CommandCenter-0.6.0-windows.zip.sha256"
   ```

   The two hashes must be identical (case does not matter).

3. **Extract into a new folder.** In Explorer: right-click the zip, Extract
   All, `C:\InfiniteAI\v0.6.0`. Then `Get-Content "$new\VERSION"` must print
   `0.6.0`.

4. **Bring your `.env` (unchanged) and your proposal template, then install:**

   ```powershell
   Copy-Item "$old\.env" "$new\.env"
   if (Test-Path "$old\templates\proposal-template.html") { Copy-Item "$old\templates\proposal-template.html" "$new\templates\" }
   Set-Location $new
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
   ```

   It must end with all **seven** checks `PASS`, including
   `versions match this folder  API 0.6.0, dashboard 0.6.0`, and
   `SMOKE TEST PASSED`. If the `.env` copy was missed, it stops and changes
   nothing: copy it and run it again.

5. **Reload** the dashboard (http://localhost:8090). The sidebar shows
   `Dashboard v0.6.0 · API v0.6.0` and a new **JARVIS Brain** entry, empty
   ("No JARVIS import yet").

6. **Optional checks:** `.\scripts\status.ps1` (version 0.6.0 for the folder,
   the API and the dashboard), and
   `Compare-Object (Get-Content "$new\.env") (Get-Content "C:\InfiniteAI\env-backup-0.5.0.txt")`
   (prints nothing when `.env` is unchanged).

From then on, run the day-to-day scripts from the new folder. Keep the old
folder until 0.6.0 is verified.

## 5. Going back

- **Quick (data kept):** run `install.ps1` from the 0.5.0 folder. Your data
  stays; the JARVIS Brain tables stay in the database, unused by 0.5.0, and
  installing 0.6.0 again finds them as they were. Tested.
- **If JARVIS data was imported into the live installation** and you want it
  out: first roll back each import run, newest first, from the 0.6.0 folder
  (`jarvis-import.ps1 -Target Production -Mode Rollback -Run <n>`, type
  `ROLL BACK PRODUCTION`), then go back as above.
- **Full restore (only for a disaster):** after going back,
  `.\scripts\restore.ps1 -BackupFolder <the backup of step 1>` from the 0.5.0
  folder. It puts the Command Center, n8n and Credit Repair data back to the
  moment of the backup, losing everything done since. It does not remove the
  JARVIS Brain tables (they are not in that backup): roll back the imports
  first if they should go. Tested.

## 6. Backups

- `backup.ps1` before the upgrade (step 1) and before any live import.
- The live import writes its own dump of the `command_center` database to
  `import-reports\pre-import\` before it changes anything, checks it and prints
  its SHA-256. A rollback (`-Mode Rollback`) is the normal way back; restoring
  that dump is only for a disaster.
- Your verified JARVIS database backup
  (`C:\InfiniteAI\JarvisMigration\backups\jarvis-state-20261019-105625.db`) is
  only read. Delete the restored JARVIS files, the decisions file and the
  pre-import dump after the import is verified (DEPLOY_WINDOWS.md step 8).

## 7. The JARVIS import, step by step

`DEPLOY_WINDOWS.md`, "JARVIS Brain: test copy and import", has the exact
commands:

1. folders (0.6.0 extracted into `C:\InfiniteAI\v0.6.0-brain-test`, no `.env`);
2. a read-only pre-check of the live installation;
3. the test copy (`brain-test.ps1 -Action Start`);
4. the staged import into the test copy: plan, 5 sessions, about 10 %, all;
5. your review in the test copy at http://127.0.0.1:8190, and the export of
   your decisions;
6. the upgrade of the live installation (section 4);
7. the live import (`-Target Production`, typed confirmation), verify, and
   your decisions applied;
8. clean-up.

## 8. Your approvals

Nothing below has been done. Each needs your explicit go-ahead:

1. **Install 0.6.0** on the live installation (section 4).
2. **Run the test copy and import the real JARVIS backup into it** (section 7,
   steps 1 to 5). This reads the backup copy only, on your PC; nothing leaves
   it.
3. **Import into the live installation** (step 7), after your review in the
   test copy.
4. **Optional, later:** allow approved, general memory in AI prompts
   (Settings), and switch on any converted schedule, after switching the same
   job off in JARVIS.

Retiring JARVIS is not part of this release.

## 9. Tests (cloud sandbox)

All on the final 0.6.0 code, in the cloud sandbox: Linux, Docker 29, the
scripts in PowerShell 7.4. Nothing ran on your PC, your websites or your
servers, and none of your real JARVIS data was used: every import test used
synthetic data with planted secrets and personal details.

TEST_TABLE

## 10. Performance (synthetic data the size of your JARVIS)

A synthetic JARVIS backup built to match yours: 2,355 sessions, 49,618
messages and a 1,660 MB file (yours: 2,355 sessions, 50,114 messages,
1,679 MB). It has about 1 GB of message text, with most tool results between
60 and 250 KB, plus planted contact details and one planted SSN in a file. It
was measured in the cloud sandbox (4 CPUs, 15 GB of memory), each step as its
own process, on the final 0.6.0 code:

| Step | Time | Peak memory of the importer | Database afterwards |
|---|---|---|---|
| Plan (writes nothing) | 10 min 18 s | 105 MB | unchanged |
| Import everything | 9 min 58 s | 110 MB | 143 MB |
| Verify | 2 min 16 s | 120 MB | 143 MB |
| Roll back that run | 2 s | 104 MB | all of the run's rows removed |

Verify passed every check: each of the 52,122 records once, all 49,618
messages, no secret or restricted value found, no reasoning column read, and
search found all 40 sampled texts. The rollback deleted the 2,355 sessions,
49,618 messages, 41 items, 4 drafts and 6 conflicts of the run.

What this tells you, and what it does not:

- **Memory stays flat** at about 110 to 120 MB: the importer reads one session
  at a time and never loads the whole backup.
- **Time**: about 10 minutes each for the plan and the import, mostly cleaning
  text on one processor core. Expect the same order of magnitude on your PC.
  Docker Desktop reading the backup from a Windows folder can be slower than
  this sandbox. The staged runs (5 sessions, then about 10 %) take a fraction
  of that.
- **Disk**: synthetic text compresses unusually well (1 GB of text became a
  103 MB table). Your real conversations will compress less. If nothing
  compressed, the archive would take about as much as the message text
  itself, about 1.2 GB if yours is like the synthetic backup. Keep at least
  3 GB free on the drive Docker Desktop uses: room for the test copy and for
  the live installation.
- **Not a real-data result.** It says nothing about how many of your
  conversations will be withheld or redacted, how many conflicts you will need
  to decide, or whether your data holds formats the cleaner misses. Those come
  from the plan and the import into the test copy (section 7, steps 3 to 5),
  which is the next thing to approve.

## 11. Known limitations

- **Detection is pattern-based.** Names and street addresses are not
  detected, and a secret in an unusual format can be missed. Your review in
  the test copy is the second line of defence, and `brain-import rescan`
  re-cleans everything when the detectors improve.
- **A system prompt that mentions credit files** (FCRA, credit reports) now
  withholds every conversation that carries it, to be safe.
- **Long file names** with capitals and digits can be shown as
  `[REDACTED:high_entropy_token]`; their records are unaffected.
- **Date filters** on the JARVIS Brain page use whole days in UTC, not your
  local day.
- **Review lists** show the 100 most recent items and 200 conflicts at a
  time; the rest appear as earlier ones are decided.
- **A test copy seeded from a backup** cannot check its own sign-in (your
  live accounts are in it); the other checks run.
- **Behind a TLS-intercepting proxy**, `EXTRA_CA_BUNDLE` should point to a
  file inside the installation folder: newer Docker builds refuse a build file
  outside it. Your installation does not use this setting.
- **Not yet run on your side.** Everything here was tested in the cloud
  sandbox only, with synthetic data. The speed on your PC, Windows paths and
  Docker Desktop file sharing are first exercised there.
- **Still open from earlier releases:** see RELEASE_NOTES_0.5.1.md section 5
  (the gate cannot verify an approval itself; first runs on a real server and
  real sites; remote access).
