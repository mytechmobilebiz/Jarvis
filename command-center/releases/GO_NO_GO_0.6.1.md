# 0.6.1 (JARVIS Brain patch): GO/NO-GO report

Date: 2026-10-20 · package `InfiniteAI-CommandCenter-0.6.1-windows.zip`,
SHA-256 `fe750b5d4787cdb83cf08c72df87c594284ee3d617f9c72afa0c1c948139e722` ·
branch `jarvis-migration` · details in
[docs/RELEASE_NOTES_0.6.1.md](../docs/RELEASE_NOTES_0.6.1.md) · Windows steps in
[UPGRADE_0.6.0_TO_0.6.1_WINDOWS.md](UPGRADE_0.6.0_TO_0.6.1_WINDOWS.md)

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
> - If you are still on 0.5.0, stay there. A corrected 0.6.2 is being
>   prepared, with a direct 0.5.0 -> 0.6.2 guide. It will be published only
>   after its security reviews pass.
> - An installation of 0.6.0 or 0.6.1 that has imported nothing holds no
>   JARVIS data and is not exposed: leave it as it is and import nothing.

## Verdict (superseded by the security notice above)

| Step | Verdict | Why |
|---|---|---|
| 1. Install 0.6.0 on your PC (from 0.5.0) | **GO, with your approval** (unchanged) | `UPGRADE_0.5.0_TO_0.6.0_WINDOWS.md`, run end to end twice in the sandbox. |
| 2. Install 0.6.1 after 0.6.0 is verified on your PC | **GO, with your approval** | Fixes 0.6.0 item 12 (the withhold defect and its log leak). Tested from the package: 0.6.0 → 0.6.1 and 0.5.0 → 0.6.1 with your history, going back to 0.6.0 and forward again; all smoke checks passed; data, keys, `.env`, n8n, agents and an unrelated Docker project unchanged. |
| 3. Test copy plus import of the real JARVIS backup into it | **GO after step 2**, with your approval | 0.6.1 was the condition. Use the 0.6.1 package for the test copy. |
| 4. Import into the live installation | **NO-GO for now** | Unchanged from 0.6.0: detection is pattern-based; your review of the real data in the test copy comes first, then a separate approval. |
| 5. Memory in AI prompts, converted schedules switched on | **Not now** | Unchanged. |

Nothing in this list has been done. I have no access to your PC; the
installed 0.5.0, JARVIS and your live database are untouched.

## 1. Tests (cloud sandbox, final 0.6.1 code and package)

| Test | Result |
|---|---|
| Backend (pytest, real PostgreSQL 18), fresh database | **965 passed, 0 failed, 0 skipped** (420 JARVIS Brain tests; 112 new) |
| Dashboard (Vitest), type check, production build | **225 passed**, clean, built (`version.json` 0.6.1) |
| End-to-end, operations and the Brain page in a browser (`testing/brain-check.sh`) | **100 passed, 0 failed, 0 skipped** (19 browser checks); the sandbox installation playing yours unchanged from start to end |
| Upgrade test from the package (nested Docker, PowerShell 7.4, `up7`) | **passed**, see section 2 |
| The 0.6.0 → 0.6.1 Windows guide, run end to end (PowerShell 7.4, on the sandbox installation running 0.6.0) | **passed**: every step printed what the guide says (checksum `True`; the backups trial-restored completely; seven smoke checks; 0.6.1 everywhere; migrations end with `0007_brain_withhold`; `log_error_verbosity=terse`; 0 withheld copies with text); the report says data, agent configuration and other Docker projects unchanged. Then the guide's quick rollback (section H) went back to 0.6.0 (smoke test passed) and forward to 0.6.1 again (passed) |
| The final package | the document check's 13 corrections changed three documents inside the zip (the release notes, DEPLOY_WINDOWS.md, PROJECT_STATUS.md), so the zip was built again. Compared file by file with the package the upgrade test and the guide run used, only those three files and MANIFEST.txt differ; every program file is identical. The final package was then installed on the sandbox installation: **all seven smoke checks passed** (API and dashboard 0.6.1) |
| Package contents | 252 files; VERSION, API, dashboard and SBOM say 0.6.1; importer 1.0.1; migration 0007 included; no tests, `testing/`, `releases/` or `.env` |

## 2. The upgrade test, step by step

On a rebuilt copy of your history, with synthetic data:

1. **Your history:** 0.2.0, then 0.4.0 and 0.5.0 over one folder. It
   reproduced "the API reports 0.2.0". Then a site, a lead, a switched-off
   agent, a stored API key and an n8n credential of your own were added.
2. **0.5.0 → 0.6.0 (the published zip, checksum OK).** All seven smoke checks
   passed and the data fingerprint was identical. A synthetic JARVIS import
   followed (41 items, 35 conversations, 157 messages).
3. **The 0.6.0 defect, reproduced.** Promoting a message, then withholding it,
   gave HTTP 500. The copy kept the text, and search still found it.
4. **0.6.0 → 0.6.1 (checksum OK, new folder, `.env` copied).**
   - All seven smoke checks passed, and status showed 0.6.1 for the folder,
     the API and the dashboard.
   - `.env` was byte-for-byte unchanged, and the data fingerprint was
     identical to 0.5.0's: users, encrypted secrets, settings, agents, sites,
     leads, schedules, n8n workflows, your n8n credential, n8n's encryption
     key and the volumes.
   - Migrations end with `0007_brain_withhold`. The import's rows are
     unchanged. The app's database user is `log_error_verbosity=terse`, and
     `brain-import status` says "migration 0007_brain_withhold present;
     withheld copies with text: 0".
5. **The same withhold on 0.6.1:** HTTP 200.
   - Both promoted copies, including the one made on 0.6.0, are quarantined
     and empty.
   - Search no longer finds the text.
   - The PostgreSQL log has **no** row values.
6. **Going back to 0.6.0** (with 0007 applied): all seven smoke checks
   passed, with identical data. Forward to 0.6.1 again, identical data.
7. **Rolling back the import on 0.6.1:** no rows of the run left; data
   identical.
8. **0.5.0 → 0.6.1 directly:** after `restore.ps1` put the 0.5.0 data back
   (identical), 0.6.1 installed from a new folder. All seven smoke checks
   passed, `.env` unchanged, data identical.
9. **Unrelated Docker project:** the same container with the same start time
   throughout.

## 3. Reviews

- **Design and build:**
  - three read-only investigations;
  - a specification;
  - the build;
  - three reviewers (privacy, correctness and upgrade safety, test adequacy)
    with 25 findings, 24 confirmed by a skeptic per finding, all fixed;
  - a second, independent round (concurrency and speed at your data's size,
    privacy, upgrade and operations) with 16 findings, 13 confirmed, all
    fixed.
- **Each fix proves its test:** each code fix was undone once and its test
  failed, with two first-round exceptions: a redundant guard (another check
  covers it), and the worker's first quote lock, which the second round
  replaced with one whose test fails when it is undone.
- **The documents** (release notes, this guide, DEPLOY_WINDOWS.md,
  PROJECT_STATUS.md) were checked claim by claim against the code and the
  logs: 119 claims, 35 flagged, 13 upheld by a skeptic and corrected
  (overstatements and a wrong count of new tests; none in the guide's
  commands or expected outputs).

## 4. What stays open

See RELEASE_NOTES_0.6.1.md section 6. In short:
- a withhold does not change the conversation's title, related messages or
  the message's fingerprint;
- text outside the Command Center (backups made before, exports, the test
  copy, AI providers, Docker logs) is out of its reach;
- detection stays pattern-based;
- the open items from earlier releases (the gate cannot see an approval, no
  real server run, no remote access) remain.

## 5. The approvals I need from you, in order

1. **"Approve installing 0.6.0"** (if not given yet): follow
   `UPGRADE_0.5.0_TO_0.6.0_WINDOWS.md`, and send `upgrade-report.txt`.
2. **"Approve installing 0.6.1"**: after 0.6.0's report is good, follow
   `UPGRADE_0.6.0_TO_0.6.1_WINDOWS.md`, and send its `upgrade-report.txt`.
3. **"Approve the test-copy import of the real backup"**, with 0.6.1:
   `DEPLOY_WINDOWS.md`, "JARVIS Brain: test copy and import", steps 1 to 5.
4. **Later, separately: "Approve the live import"**, after your review.
