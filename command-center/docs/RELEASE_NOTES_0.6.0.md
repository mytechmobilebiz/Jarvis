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

| Test | Result |
|---|---|
| Backend (pytest, real PostgreSQL 18), the final merged code | **852 passed, 0 failed**, of which 306 are JARVIS Brain tests. Cleaning: secrets in text, JSON, tables and 15 command-line forms; restricted data withholds the conversation; contact details masked; reasoning tags removed; a re-clean changes nothing. Importer: plan writes nothing; a sample run then a re-run that adds nothing; duplicates (a repeated job id, a source key recorded twice); every conflict kind and its de-duplication; resume after a failure mid-batch, after the last checkpoint and after a lost lock; verify; rollback of one run, newest first, with conflict decisions undone. Withheld stays withheld through a re-import, a rollback and a rescan, and quoted copies in history answers are removed. Access by role and sensitivity (viewer, operator, admin; customer and quarantined items). **Authority**: JARVIS's "pre-approved" and "without asking" rules are quarantined, need an admin, never reach a prompt even when approved; the import changes no setting, agent state, schedule or approval; a proposal still waits for a person's approval. **Workflows**: drafts never run; converting one creates a **disabled** schedule; watched sites come in paused |
| Dashboard (Vitest, TypeScript check, production build) | **218 passed**, type check clean, production build succeeded. The JARVIS Brain page per role: search, conversations, provenance, review, conflicts (admin-only sides locked), memory editing with versions, the rotation list by import report, nothing restricted shown to a role that may not see it |
| End-to-end, operations (`testing/brain-check.sh`: PowerShell 7.4 scripts, Docker, the synthetic JARVIS installation with planted secrets and personal data) | **98 passed, 0 failed, 0 skipped**. Your live installation is played by a running 0.6.0 stack; its fingerprint (images, containers and their start times, volumes, networks) was **unchanged after every step**, and so were an unrelated Docker project and the test database. The read-only pre-check of the live installation; every script parses (ASCII only); refusals before anything runs (a folder with a `.env`, a secret equal to a live one, a port in use, 12 bad import requests); the test copy: its own project, images and session cookie, only 127.0.0.1:8190, no n8n, a worker that can neither connect out nor resolve names (a control container can), refused overlays that would add n8n or internet; the hardened importer container (read-only system and source, no capabilities, no network); the pre-import dump checked by `pg_restore -l` and SHA-256; restart persistence and Stop/Start. Then the import: Plan (writes nothing), a 2-session sample, verify, everything, verify; the brain API and a Supervisor recall; rows kept across a restart; **no planted secret or restricted value in the brain tables**; rollback of both runs left **zero** brain rows. Seeding the test copy from a backup disables schedules, cancels open tasks, rejects approvals and deletes secrets; Remove needs the exact words and leaves nothing behind |
| The JARVIS Brain page in a browser (Playwright, Chromium; part of the run above) | **all 19 checks passed**. **Admin**: every tab; search finds an imported item; the item shows its source (`knowledge/product.md`); Approve; Edit saves a new version and sends it back to review; approved again; a customer item opens; conversations listed. **Viewer**: no conversation, workflow, review or import tab; search finds the approved item but never a customer item; neither a customer item nor a conversation opens by its address. **Operator**: conversation and review tabs; never a customer item in search; neither a customer nor a quarantined item opens by its address. **No planted secret or restricted value on any page visited** |
| Your upgrade path, from the package (nested Docker, PowerShell 7.4) | **passed: 73 checks, no failure** apart from the intended one below. It reproduced your history: 0.2.0, then 0.4.0 and 0.5.0 over the same folder, with a site, a lead, a switched-off agent and a stored API key; at 0.5.0 it reproduced your failure ("the API reports 0.2.0"). **0.5.0 → 0.6.0 directly** (section 4 step by step): backup, checksum, a new folder; without the `.env` it was refused with nothing created and no container touched; with it, **all seven smoke checks passed** (API and dashboard 0.6.0), `.env` byte-for-byte unchanged. **The live-import path** from the 0.6.0 folder: Status, Plan (writes nothing), the import refused without the typed words, then imported (41 items, 35 sessions, 157 messages, 4 drafts, 4 schedules, disabled) after a checked dump; no planted value in the database; Verify; Rollback left none of the run's rows. **Going back** to 0.5.0 (smoke test passed), forward again (the brain item kept), back again and `restore.ps1` with the pre-upgrade backup. **0.5.0 → 0.5.1 → 0.6.0**. At every step the data fingerprint matched the original: users, encrypted secrets and the key that decrypts them, settings, agents and their on/off states, sites, leads, schedules, n8n workflows, credentials and encryption key, volumes. An unrelated Docker project kept its container and start time throughout |
| Base images | The sandbox pulls Docker Hub images through Google's mirror (`mirror.gcr.io`) because Docker Hub rate-limits it (HTTP 429). Each image's digest was compared with Docker Hub's own: **all 5 identical** (postgres 18.6-alpine, python 3.12-slim, node 22-alpine, nginx 1.27-alpine, n8n 2.43.7). Your PC keeps pulling from Docker Hub: the mirror is a setting of the test runs only (`REGISTRY`, default `docker.io`) |
| Code review | An adversarial design review (38 findings, addressed before building). Then six independent code reviewers (privacy, data integrity, API and access control, agents, dashboard, operations): 57 findings, each checked by a separate verifier against the latest code: 18 confirmed, 23 partly confirmed, 17 refuted. The 2 critical and 4 high ones are fixed with tests, and the fixes were verified again (that pass found one regression in a script, fixed with a guard test). Medium and low ones are fixed, except the known limitations in section 11 |

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
