# 0.6.0 (JARVIS Brain): GO/NO-GO report

Date: 2026-10-20 · package `InfiniteAI-CommandCenter-0.6.0-windows.zip`,
SHA-256 `PKG_SHA` · branch `jarvis-migration` · details in
[RELEASE_NOTES_0.6.0.md](RELEASE_NOTES_0.6.0.md)

## Verdict

| Step | Verdict | Why |
|---|---|---|
| 1. Install 0.6.0 on your PC (upgrade from 0.5.0) | **GO, with your approval** | Tested step by step on a copy of your history: all seven smoke checks passed, and data, keys, n8n, agents and unrelated Docker projects were unchanged. Going back to 0.5.0 was also tested. It changes no existing data and imports nothing. |
| 2. Test copy plus import of the real JARVIS backup into it | **GO, with your approval** | Separate Docker project with its own database, no n8n and a worker without internet. Your live installation was unchanged after every step of the end-to-end check. It reads only the backup copy. |
| 3. Import into the live installation | **NO-GO for now** | The real backup has not been read by any 0.6.0 code. Secret and personal-data detection is pattern-based (names and street addresses are not detected). Your review of the real data in the test copy (step 2) must come first. After that review, this needs a separate approval. |
| 4. Memory in AI prompts, converted schedules switched on | **Not now** | Optional, later. Both are off by default and stay off until an admin changes them. |

Nothing in this list has been done. I have no access to your PC; the
installed 0.5.0, JARVIS and your live database are untouched.

## 1. Tests (cloud sandbox, final 0.6.0 code)

| Test | Result |
|---|---|
| Backend (pytest, real PostgreSQL 18) on the final merged branch | **852 passed, 0 failed** (306 JARVIS Brain tests) |
| Dashboard (Vitest), type check, production build | **218 passed**, type check clean, build succeeded |
| End-to-end: test copy, staged import, verify, rollback, restart (`testing/brain-check.sh`) | **98 passed, 0 failed, 0 skipped**; live-installation stand-in unchanged after every step |
| JARVIS Brain page in a browser (admin, operator, viewer) | **19 of 19 passed**: search, provenance, approve and edit with versions, restricted data hidden per role, no planted value on any page |
| Your upgrade path from the package (0.5.0 → 0.6.0; 0.5.0 → 0.5.1 → 0.6.0; back to 0.5.0; restore) | **passed** (73 checks); data fingerprint identical at every step |
| Live-import path in the upgrade test (typed confirmation, dump, import, verify, rollback) | **passed**; no planted value in the database; rollback left none of the run's rows |
| Approval-bypass instructions from JARVIS | **cannot override** the Command Center's approvals (tested): quarantined, admin only, never in a prompt; no setting, agent, schedule or approval changed by an import; a proposal still waits for a person |
| Imported workflows | **stay disabled** (tested): drafts never run; a converted schedule is created disabled; watched sites start paused |
| Performance, synthetic backup the size of yours (49,618 messages, 1.66 GB) | plan 10 min 18 s, import 9 min 58 s, verify 2 min 16 s (all checks passed), rollback 2 s; importer memory 105 to 120 MB |
| Base images through Google's mirror (Docker Hub rate-limited the sandbox, HTTP 429) | **5 of 5 digests identical** to Docker Hub's own; your PC still pulls from Docker Hub |

## 2. Security review

- A design review (38 findings) was folded into the specification before
  building.
- Six independent code reviewers (privacy, data integrity, API and access
  control, agents, dashboard, operations) reported 57 findings. A separate
  verifier checked each one against the latest code:
  - 18 confirmed, 23 partly confirmed, 17 refuted;
  - all **2 critical and 4 high** are fixed with tests, and the fixes were
    verified again. That second pass found one script regression, which is
    fixed and guarded by a test.
- The critical and high findings were:
  - personal data and numeric secrets in a tool's JSON output were missed
    when only the key said what they were (for example an SSN under
    `"ssn"`, a PIN under `"pin"`);
  - secret-bearing web addresses could be kept in import records;
  - a rollback dropped an item's risk flags;
  - copies of withheld text survived in history answers;
  - the live import refused to run, and a script error blocked every import.
- Medium and low findings are fixed, except those listed in section 3.
- RELEASE_CHECK

## 3. Unresolved defects and limitations (none blocks steps 1 and 2)

1. **Detection is pattern-based.** Names and street addresses are not
   detected, and a secret in an unusual format can be missed. This is the
   reason for step 3's NO-GO until your review. `brain-import rescan`
   re-cleans everything later.
2. A system prompt that mentions credit files withholds every conversation
   that carries it, which errs on the safe side.
3. Long file names with capitals and digits can show as
   `[REDACTED:high_entropy_token]`.
4. Date filters on the JARVIS Brain page use whole days in UTC.
5. Review lists show 100 items and 200 conflicts at a time.
6. A test copy seeded from a backup cannot check its own sign-in.
7. The database size for real data is unknown: synthetic text compresses
   unusually well. Keep 3 GB free on Docker Desktop's drive.
8. Still open from earlier releases (RELEASE_NOTES_0.5.1.md section 5):
   - the gate cannot see an approval;
   - there has been no run on a real server;
   - remote access is not set up.

## 4. Data preservation (upgrade test on a copy of your history)

Compared before and after every step: users, encrypted secrets (and that
the stored API key still decrypts), settings, agents and their on/off states,
sites, leads, schedules, n8n workflows, credentials and **encryption key**,
and the Docker volumes. **Identical at every step**: after 0.5.0 → 0.6.0,
after the live import and its rollback, after going back to 0.5.0, after
forward again, after `restore.ps1`, and after 0.5.0 → 0.5.1 → 0.6.0. `.env`
stayed byte-for-byte the same. An unrelated Docker project kept its
container and start time. `install.ps1` without the copied `.env` stops
and changes nothing.

## 5. Your v0.5.0: do you need 0.5.1 first?

**No.** You can upgrade straight from your installed 0.5.0 to 0.6.0. It
contains every 0.5.1 change, including the fix for "the API reports 0.2.0".
Both routes were tested (section 4). Steps: RELEASE_NOTES_0.6.0.md
section 4.

## 6. The approvals I need from you, exactly

1. **"Approve installing 0.6.0"**: you run RELEASE_NOTES_0.6.0.md
   section 4 on your PC:
   - `backup.ps1` from `C:\InfiniteAI\v0.5.0\InfiniteAI-CommandCenter`;
   - check the zip's SHA-256;
   - extract it into `C:\InfiniteAI\v0.6.0`;
   - copy `.env`;
   - run `install.ps1`.

   All seven smoke checks must pass.
2. **"Approve the test-copy import of the real backup"**: DEPLOY_WINDOWS.md,
   "JARVIS Brain: test copy and import", steps 1 to 5:
   - it reads `jarvis-state-20261019-105625.db` and the restored kit files;
   - it writes only to the test copy;
   - you review the result at http://127.0.0.1:8190 and rotate the
     credentials the import report names.
3. **Later, separately: "Approve the live import"**, after your review
   (step 7 there, which asks you to type `IMPORT INTO PRODUCTION`).
4. **Optional, later:** memory in AI prompts (Settings); switching on a
   converted schedule after switching the same job off in JARVIS.

Retiring JARVIS is not part of this release.

## 7. What was not done or not verified

- Nothing was run on your PC, Windows PowerShell 5.1 or Docker Desktop. The
  scripts were run in PowerShell 7.4 on Linux and parse-checked for 5.1.
- The real JARVIS backup and the 50,114 real messages were not read; every
  import used synthetic data with planted secrets and personal details.
- No schedule, email, WhatsApp message, WordPress or server action was
  activated, sent or run.
