# Project status: Infinite AI Command Center

Last updated: 2026-10-20 (UTC) · version 0.6.1 · branch `jarvis-migration`

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

## Where it is being built

Development and testing happen in **Anthropic's cloud sandbox** (a Linux
container with Docker 29), which has no connection to the owner's PC or
servers. Version **0.5.0** has been installed by the owner on the Windows 11
PC: six of seven smoke checks passed, and the version check failed ("the API
reports 0.2.0"). The cause was found and fixed in **0.5.1** (see "Completed in
0.5.1"), which has not been run on Windows yet. **0.6.0** (the JARVIS Brain)
is finished and tested in the sandbox and **waits for the owner's approval**:
it has not been installed, and none of JARVIS's real data has been imported
anywhere. **0.6.1** (a JARVIS Brain patch: withholding a message that has a
promoted memory copy) is finished and tested in the sandbox; it is needed
before the JARVIS test-copy import. **Nothing has been run on a real CWP server**; see "Cloud sandbox vs
Windows and servers" below.

## Milestones

| Milestone | State |
|---|---|
| M1: dashboard, PostgreSQL, n8n, AI supervisor, read-only WordPress monitor | Done; 0.2.0 verified on the owner's PC |
| M1b: Windows 11 deployment package (Docker Desktop + WSL 2) | Done; 0.2.0 verified on the owner's PC |
| M2: commercial-ready architecture (workflow-engine interface, editions, settings split, SBOM) | Done in the sandbox; blockers in COMMERCIALIZATION.md |
| M3: specialist agents and the isolated Credit Repair module | Done in the sandbox, with **mock** outward integrations |
| M4 (0.3.0): the seven official agents, complete WordPress monitoring, restricted SSH to CWP servers, approval-based WP-CLI repairs | Done; 0.3.0 installed on the owner's PC (smoke tests passed); tested against disposable WordPress installs only; needs a first, careful run on a test server |
| UI (0.4.0): official dashboard redesign: dark command-center interface, AI command input, new sidebar sections | Done; installed on the owner's PC (six smoke tests passed) |
| 0.5.0: dashboard to the reference with real data only; approvals name the exact action and target; host-key scan fixed | Done; installed on the owner's PC (version check failed: stale build copies, fixed in 0.5.1). Remaining differences and owner decisions: docs/REFERENCE_DESIGN_GAP.md |
| 0.5.1: images can no longer be built from stale files without the build stopping; plugin updates only to the approved, current, newer WordPress.org release | **Done in the sandbox**; not yet run on the PC |
| 0.6.0: the JARVIS Brain: memory with review and versions, JARVIS's conversation archive, the importer with a test copy, agents reading approved memory as reference | **Done in the sandbox**; waits for the owner's approval to install; no real JARVIS data imported |
| 0.6.1: withholding a message that has promoted memory copies works and removes its text from every promoted copy, the history answers quoting them and the import records; database errors never log row values | **Done in the sandbox**; to install after 0.6.0 is verified on the PC; needed before the test-copy import |
| M5: real integrations (email, publishing, ads, notifications) | Needs owner approval and credentials |

Estimated MVP completion: **about 85%**. This is an estimate, weighted by the
work remaining (Windows and server verification, real integrations, owner
decisions), not a measurement.

## The seven agents and the AI Supervisor

The AI Supervisor is separate from the seven agents (its own section on the
AI Agents page): it plans requests, delegates to the agents and summarises.

| Agent | Works for real today | Mock in this build | Needs approval |
|---|---|---|---|
| WordPress Security & Repair | Security scans (exposures, suspicious and conditional redirects, cloaking, injected scripts, iframes and spam, TLS), weekly security report, WP-CLI diagnosis over restricted SSH | | every repair (restore home/siteurl, plugin deactivate/activate/update, reinstall of a WordPress.org plugin, core reinstall, maintenance mode, cache and rewrite flush, quarantine debug.log); production: an admin types the name |
| SEO & Content | Read-only on-page audits, keyword briefs, blog drafts | WordPress publishing | publishing |
| Social Media & Marketing (Postonomous) | Social drafts, ROAS/CPA reports and anomaly flags | Postonomous publishing; ad data is sample data; budget changes | publishing; any budget change |
| VPS & Server Management (CWP/SSH) | Server overview, service status through cc-gate | | restarting an allowlisted service |
| Email & Customer Communications | Lead scoring, proposal drafts, support triage and reply drafts, n8n lead intake | Sending email | sending a proposal or reply |
| Website & Infrastructure Monitoring | Registration, uptime (24 h / 7 d / 30 d) and incidents, TLS expiry and protocol, redirect chains, read-only port/DNS checks | | |
| Credit Repair & Dispute Management (isolated) | Encrypted client store in its own database, compliance timing checks, FCRA dispute-letter drafts | Mailing | mailing a letter; module off until an admin enables it |

Every approved mock action is listed on the **Outbox** page as
"recorded, not sent". Server repairs are real: they run on your server
through cc-gate, only after approval, and only if the server's own
`allow_changes` switch is on.

## JARVIS migration and the JARVIS Brain (0.6.0, waiting for approval)

**Nothing has been installed or imported.** 0.6.0 is built and tested in the
sandbox with synthetic data only. The GO/NO-GO report is
[releases/GO_NO_GO_0.6.0.md](releases/GO_NO_GO_0.6.0.md). Installing it, importing the real JARVIS
backup into a test copy and importing into the live installation are three
separate approvals (docs/RELEASE_NOTES_0.6.0.md section 8). JARVIS stays
installed and running; nothing here changes it.

- **Design and source analysis:**
  [docs/JARVIS_MIGRATION_DESIGN.md](docs/JARVIS_MIGRATION_DESIGN.md) (what
  JARVIS is and where its data lives, the owner's changes that bypass
  approval, staff-to-agent mapping, risks; sections 5 to 10 describe what
  0.6.0 built) and the specification
  [docs/JARVIS_BRAIN_SPEC.md](docs/JARVIS_BRAIN_SPEC.md).
- **Read-only kit** (`migration/jarvis/`, kit 0.1.0): encrypted backup of the
  profile and content without secret files, and a counts-only inventory, in a
  container with no network and the JARVIS folders mounted read-only.
- **The JARVIS Brain (0.6.0)**: see "Completed in 0.6.0" below.

## Completed in 0.6.1 (one migration; no new tables, no new keys)

Full list, upgrade, going back and tests:
[docs/RELEASE_NOTES_0.6.1.md](docs/RELEASE_NOTES_0.6.1.md); step by step on
Windows: [releases/UPGRADE_0.6.0_TO_0.6.1_WINDOWS.md](releases/UPGRADE_0.6.0_TO_0.6.1_WINDOWS.md).

- Withholding a message empties every promoted copy and all its versions;
  the copy stays as a placeholder that is never approved, reopened or edited
  again. One routine for the admin, import, rescan, decisions-apply, rollback
  and a start-up check. Agents, prompts, history answers, viewers, operators
  and exports never read a copy of a withheld message.
- Purge is no longer blocked by dismissed or keep-both conflicts.
- Withhold, purge and redact wait for no import (409 instead); promote locks
  the message; history answers yield to a removal.
- Database errors are logged and returned without row values; the app's
  PostgreSQL user logs terse errors.
- Audit entries for every emptied copy, with IDs and counts only.
- Migration `0007_brain_withhold`; importer 1.0.1; the production import
  needs 0.6.1.

## Completed in 0.6.0 (one migration, nine new tables; no new keys)

Full list, upgrade steps, rollback and the owner's approvals:
[docs/RELEASE_NOTES_0.6.0.md](docs/RELEASE_NOTES_0.6.0.md).

- **Memory**: business facts, knowledge, agent notes, decisions, projects,
  accomplishments and problems; JARVIS's operating instructions and skills as
  reference text. Each item has a source, a review status, a sensitivity
  (general or customer) and a version history. Nothing is used before a
  person approves it.
- **Conversation archive**: JARVIS's sessions and messages, cleaned,
  searchable by staff member, date and label; customer conversations admin
  only; restricted conversations kept as empty rows.
- **Cleaning before anything is stored**: secrets (keys, tokens, passwords,
  webhook addresses, passwords on command lines in 15 forms) become
  `[REDACTED:<kind>]`; SSNs, card and bank numbers, dates of birth and credit
  files withhold the whole conversation; every e-mail address and phone
  number is masked (yours included); long messages are kept as excerpts
  (4,000 characters of a tool result, 50,000 of other messages); reasoning is
  never read.
- **Imported text is data**: JARVIS's approval-bypass rules come in
  quarantined, need an admin and never reach a prompt; the Command Center's
  approvals are code and no import changes a setting, agent, schedule or
  approval (tested: a proposal still waits for a person).
- **Workflows stay off**: JARVIS's jobs become drafts that never run; a
  converted schedule is created disabled; watched websites are offered as
  paused sites.
- **The importer** (`scripts\jarvis-import.ps1`): a plan that writes nothing,
  staged runs, resume after an interruption, verify, rollback of one run, a
  re-run that adds nothing, a dump of the database before a live import, and
  typed confirmations for the live installation. It runs on the private
  network only (it reaches the database, not the internet), from the image
  of the installation it imports into. Decisions made in the test copy,
  including withheld messages and purged items, carry over to the live
  import.
- **Withheld stays withheld**: text withheld by the import, an admin or a
  rescan never returns through a re-import or a rollback, and quoted copies
  in history answers are removed.
- **Test copy** (`scripts\brain-test.ps1`): its own Docker project on port
  8190, database and secrets, no n8n, a worker without internet.
- **Agents** read only approved items in their scope; the AI Supervisor
  answers questions about JARVIS's history from the archive and memory
  locally (no AI model reads them; with Claude set up, the question itself
  still goes to Claude for planning).
  Memory in AI prompts is off until an admin turns it on.

## Completed in 0.5.1 (no database migrations, no new keys)

Full list and the exact upgrade steps: [docs/RELEASE_NOTES_0.5.1.md](docs/RELEASE_NOTES_0.5.1.md).

- **The cause of "API reports 0.2.0"**: every packaged file had one fixed
  timestamp, and Docker (BuildKit) reuses files from its saved copy of the
  build context when size and timestamp match, in any folder of the project.
  The version files never change size, so the API was built with 0.2.0's
  `__init__.py` and the dashboard with 0.4.0's `package.json`; all other code
  was current. `--no-cache` did not help. The earlier explanation (a container
  not replaced) was wrong.
- **Fix, three layers**: per-build timestamps in the package; both images stop
  the build if the copied code is not the release version; `install.ps1` and
  `start.ps1` refresh the build files first (new `prepare-build.ps1`), refuse
  a folder that mixes releases, and pass the version to the build. The
  installer never creates new keys over existing data.
- **Messages**: a page left open during an update says "This page is out of
  date"; the mismatch banner, the Knowledge Base and the guides give the real
  cause; the smoke test stays strict and names both versions.
- **Plugin updates (C3-07, security)**: cc-gate 1.2.0 installs only the version
  the Command Center names (the approved one), only if it is newer and is
  WordPress.org's current release (asked directly), and verifies it
  afterwards; the Command Center offers no approval for an older "update".
- Two independent review rounds (51 reviewer agents) checked the fix; every
  confirmed finding is fixed.

## Completed in 0.5.0 (no database migrations; API additions only)

Full list and upgrade notes: [docs/RELEASE_NOTES_0.5.0.md](docs/RELEASE_NOTES_0.5.0.md).

- **The owner's ten requests**: seven agent cards; Quick Actions (scan all
  websites, SEO audit, check servers, generate a report, open n8n); a
  Service Status panel where each row says what was checked and when; PC
  Resources and the Intern 2 card shown as **Not connected** (no source
  exists for either); a denser layout closer to the reference; task activity
  by category over 24 h / 7 d / 30 d. Every value comes from the API.
- **v0.2.0 in the footer**: 0.5.0 shows the dashboard's and the API's
  versions, warns on every page when they differ, and the smoke test (now
  **seven** checks) and `status.ps1` check and print them. The cause was found
  in 0.5.1 (stale build copies of the version files, see above); the
  explanation given in 0.5.0 (a container still running 0.2.0 code) was wrong.
- **Approvals name the exact action and target**: email (recipient, subject,
  the draft's text and a hash of it), publishing (platform, account, draft),
  repairs (site, server and pinned key, the version a recent diagnosis
  found), host-key replacement and leaving production (the name typed).
  Mock delivery is labelled as such.
- **Host-key scan fixed**: since 0.3.0 the scan ran in the API container,
  which has no route out of Docker, so it could not reach a real server. It
  now runs in the worker as a read-only task. The Servers page is unchanged.
- **Offline is not downtime**: a site that gives no answer is recorded as
  down only after a control check shows the PC is online; otherwise it is
  "not checked" and a banner says the PC has no internet. The control hosts
  can be replaced with `MONITOR_CONTROL_URLS`.
- Pending 0.4.0 approvals for repairs, restarts, emails and publications
  must be requested again after the upgrade (they do not name the exact
  target).

## Completed in 0.4.0 (frontend only; no database or API changes)

- **Shell**: top bar with the Infinite AI logo, the **AI command input** and
  **Execute** (it creates an AI Supervisor request through the existing API,
  with the same sign-in, roles and approval rules; viewers cannot send),
  optional **voice dictation** (shown only where it can work, asks for consent
  first, only fills the input), notifications, settings and the account menu.
  Sidebar: Dashboard, AI Agents, Workflows, Websites, SEO & Marketing,
  CWP / Servers, Email, Credit Repair, Social Media / Postonomous, Automation
  Logs, Knowledge Base, Settings, then Approvals, Tasks, AI Supervisor, Leads,
  Client support, Drafts & reports, Outbox, Integrations and Activity log.
  Desktop, tablet (icon rail) and phone (menu) layouts.
- **Dashboard**: active AI agents, websites monitored, tasks completed today,
  website uptime, local date and time, worker and service health, a 24-hour
  task chart with a table view, agents, websites, approvals, alerts and
  recent requests. Every value comes from the API.
- **New pages**: Workflows (schedules, n8n, on-demand requests), SEO &
  Marketing, Email, Social Media / Postonomous, Automation Logs (filterable
  event stream) and Knowledge Base (guides checked claim by claim against
  the code: about 280 statements, 26 corrected).
- **Existing pages** restyled without behaviour changes.
- **Accessibility**: keyboard paths for menus, dialogs, the chart and
  clickable rows; visible focus; WCAG AA contrast; a title for each page.
- **Security**: the dashboard still loads nothing from other sites (the Inter
  typeface, SIL Open Font License, is bundled); the microphone is allowed
  for this site only (`Permissions-Policy: microphone=(self)`); Credit Repair
  task details stay hidden from users without the sensitive-data permission.
- Final review of the redesign: 48 findings from security, correctness,
  accessibility and visual reviewers, each checked by a separate verifier;
  33 confirmed and fixed, the fixes verified again.

## Completed in 0.3.0

- **Seven official agents** with data migration from 0.2.0 (tasks, approvals,
  schedules, artifacts, outbox, events and switched-off agents carried over).
- **Monitoring**: website registration (environment production/staging/test;
  staging and test need an admin), uptime and incidents, TLS, hop-by-hop
  redirect checks that never visit a foreign domain, cloaking checks with
  phone, desktop and crawler visitors, injected-content checks resistant to
  hostile pages (every pattern linear on 600 KB), per-site deadlines so one
  slow or broken site cannot stop a sweep, weekly security report.
- **Servers (CWP/SSH)**: Ed25519 key generated and stored encrypted in the
  Command Center; host key pinned only after a person compares the
  fingerprint; connections only to the checked address; a dedicated non-root
  account whose key can only run **cc-gate** (forced command, allowlisted
  operations, checked arguments, no shell); WP-CLI runs as each site's own
  user through **cc-wp** with a clean environment; every request audited on
  both sides; operation time budgets that stop the program and report
  "outcome unknown" (critical) when a change may not have finished.
- **Repairs**: approval bound to the exact target (re-linked, renamed or newly
  production targets are refused, also at run time); one server-side task per
  WordPress install at a time (others wait in the queue, not in a worker
  slot); check, database backup, one change, confirmation that it took effect,
  HTTP before/after comparison; a part-way failure raises a critical
  `repair.failed` event with the backup file.
- **Dashboard**: Servers page, server link and repairs on the website page,
  production approvals with typed confirmation, partial server checks shown
  as such.
- Docs: [docs/SERVER_SETUP.md](docs/SERVER_SETUP.md) (server setup, what the
  key can and cannot do, undo), DEPLOY_WINDOWS.md (upgrade from 0.2.0).

## Security reviews

**0.6.0 (JARVIS Brain)**: an adversarial design review (38 findings, all
addressed in the specification before building), then six independent code
reviewers (privacy, data integrity, API and access control, agents,
dashboard, operations) with 57 findings, each checked by a separate verifier
against the latest code; the operations verifier found one more (a
regression that stopped every import). Of these 58: 18 confirmed, 23 partly
confirmed, 17 refuted. The
2 critical and 4 high ones are fixed with tests and the fixes verified again;
the medium and low ones are fixed except those listed as known limitations in
RELEASE_NOTES_0.6.0.md section 11.

**0.3.0 to 0.5.1**: two adversarial review rounds by independent agents ran on the SSH layer,
the gate, approvals, repairs and the monitoring checks:

1. 51 findings; 49 survived verification and were fixed.
2. A verification of those fixes found 43 items still open or introduced by
   the fixes (several duplicates). All were fixed, each fix reviewed again by
   a separate agent, and every fix has a regression test.

Known limits that remain (by design or not yet done):

- WP-CLI answers that load WordPress (plugins, administrators, home/siteurl,
  maintenance mode) run the site's own `wp-config.php`, must-use plugins and
  drop-ins, so a compromised site can influence them. The core checksum check
  runs before WordPress loads and does not depend on the site's code.
- New administrators are reported against the previous check; there is no
  owner-acknowledged list of known administrators yet.
- For CWP addon-domain layouts, a changed siteurl that looks like WordPress
  living in its own folder is reported as a warning (and its restore is
  refused) until "siteurl" is set for that site in the gate configuration.
- A redirect to an invalid address served only to a phone or crawler visitor
  is not flagged; on the home page it is reported as down.

## Test results (latest runs, cloud sandbox)

| Suite | Result | What it covers |
|---|---|---|
| Backend unit + integration (pytest, real PostgreSQL 18), 0.6.1 | **965 passed, 0 failed, 0 skipped** (0.6.1), 420 of them JARVIS Brain tests | everything below, plus the 0.6.1 withhold and purge tests (RELEASE_NOTES_0.6.1.md section 5) |
| Dashboard (Vitest), type check, production build, 0.6.1 | **225 passed**; type check clean; build succeeded | the withhold dialog's copy count and wording, the emptied item, the disabled Approve |
| End-to-end, JARVIS Brain operations, 0.6.1 | **100 passed, 0 failed, 0 skipped**, including the 19 browser checks | as below, plus terse database error logs for the app's user |
| Backend unit + integration (pytest, real PostgreSQL 18), final 0.6.0 code | **853 passed, 0 failed** (0.6.0), 307 of them JARVIS Brain tests | cleaning (secrets in text, JSON, tables and 15 command-line forms; restricted data withholds; contact details masked; reasoning removed); the importer (plan, staged runs, a re-run that adds nothing, duplicates, every conflict kind, resume after failures and a lost lock, verify, rollback newest first); withheld stays withheld through re-import, rollback and rescan; access by role and sensitivity; JARVIS's approval-bypass rules quarantined and never in a prompt, a proposal still waits for a person; drafts never run, converted schedules disabled; everything from 0.5.1 |
| Dashboard (Vitest), type check, production build | **218 passed** (0.6.0); type check clean; build succeeded | the JARVIS Brain page per role (search, conversations, provenance, review, conflicts, memory editing with versions, the rotation list), plus everything from 0.5.1 |
| End-to-end, JARVIS Brain operations (testing/brain-check.sh, PowerShell 7.4, Docker) | **98 passed, 0 failed, 0 skipped**, including 19 checks of the Brain page in a browser | the pre-check of the live installation (read-only, unchanged at every step), the test copy (separate project, port 8190, worker without internet, no n8n), the hardened importer container, the staged import of a synthetic JARVIS with planted secrets and personal data, verify, rollback, restart persistence, refusals; the Brain page in Chromium as admin, operator and viewer |
| Upgrade from the owner's 0.5.0 to 0.6.0 from the package (nested Docker, PowerShell 7.4) | **passed** (73 checks; run 6 on the final package) | the owner's history reproduced (0.5.0 with "the API reports 0.2.0"); RELEASE_NOTES_0.6.0.md section 4 step by step, all seven smoke checks; the live-import path, verify and rollback; going back to 0.5.0, forward again, `restore.ps1`; 0.5.0 → 0.5.1 → 0.6.0; data, encrypted secrets, n8n workflows, an owner-created n8n credential, n8n's encryption key, agents, volumes and an unrelated Docker project unchanged throughout (the Command Center's own two n8n credentials are re-encrypted at every n8n start by design and were compared by ID and type) |
| Performance, synthetic backup the size of the owner's (2,355 sessions, 49,618 messages, 1.66 GB) | plan 10 min 18 s, import 9 min 58 s, verify 2 min 16 s, rollback 2 s; importer peak memory 105 to 120 MB | synthetic data only; not a result for the real backup (RELEASE_NOTES_0.6.0.md section 10) |
| Backend unit + integration (pytest, real PostgreSQL 18) | **524 passed, 0 failed** (0.5.1) | the 0.5.1 additions (plugin updates pinned to the approved, newer, current WordPress.org release and verified afterwards; no downgrade approvals; gates older than 1.2.0 refused before any backup; every version number agrees with `VERSION`; the package builder's refusal and timestamps; the build arguments; the Dockerfile check run for real; the script wiring), plus API, auth, roles, approvals (binding, production, run-time re-checks), worker, supervisor, all agents, the gate and its installer, cc-wp and the site helper (real programs), SSH through a real asyncssh server running the real cc-gate, repairs, monitoring checks incl. hostile-input timing, Credit Repair isolation |
| Dashboard (Vitest) | **171 passed** (0.5.1); type check clean | a page left open during an update ("This page is out of date"), the mismatch banner's wording, `VERSION` equal to the built-in version, the 0.5.0 dashboard (stat cards, agent cards, service status, PC resources and Intern 2 not connected, task activity by category, recent tasks, active workflows), Quick Actions, the version line and mismatch banner, the offline banner, approvals with exact targets and typed confirmations, plus everything from 0.4.0: sign-in, sidebar sections by role, the command input, voice input gating, keyboard paths, Credit Repair details hidden without permission |
| End-to-end, running stack (pytest + Playwright Chromium) | **3 consecutive full runs passed** (0.5.1 production build): 40 passed + 1 skipped (expected on a fresh stack: the server-page test runs before the test server exists), 41, 41 | the test server runs **cc-gate 1.2.0**, which refuses the old two-argument plugin update; the 0.5.0 dashboard (seven agent cards, service status from real checks, PC Resources not connected), the command input running a real request, every sidebar section opening without errors (including the pages under More), everything from 0.2.0, plus a disposable WordPress server with **real OpenSSH, sudo, WP-CLI and cc-gate installed by install-gate.sh**: the host key scanned **by the worker** and pinned; the key can do nothing but run cc-gate; cloaking malware found and its plugin deactivated after approval; hijacked home/siteurl repaired; debug.log quarantined; a planted WP-CLI config ignored; a local plugin never reinstalled from WordPress.org; service restart through the sudo rule; production confirmation (and the typed name to leave production); audit on both sides; an impostor host key refused |
| PowerShell scripts | **all 12 parse** with PowerShell's own parser; ASCII-only | the parser check is new in 0.5.0, after the upgrade test found a parse error in the new smoke check (fixed before release) |
| `prepare-build.ps1` (testing/prepare-build-check.sh, from the package) | **5 of 5 passed** | normal folder; a folder inside one named `dist`; a read-only file (refreshed, stays read-only); a folder mixing releases (refused before touching anything, no version passed on); no `VERSION` file (refused) |
| Stale-build regression (testing/stale-build-check.sh, 0.5.0 zip then the 0.5.1 package) | **6 of 6 passed** | a plain build after extracting over 0.5.0 gives 0.5.1 in both images; a deliberately stale copy stops both builds, naming the file; prepare-build.ps1 passes the version to both builds and the checked build is current. Fails on 0.4.0 → 0.5.0, the bug it guards against |
| Upgrade from the owner's 0.5.0 to 0.5.1 from the package (PowerShell 7.4) | **passed** | the owner's history with a persistent Docker setup: 0.2.0 installed, a site added; 0.4.0 and 0.5.0 extracted over the same folder (0.5.0 reproduced "the API reports 0.2.0", dashboard 0.4.0); a lead, a switched-off agent and a stored API key added. Then RELEASE_NOTES_0.5.1.md section 3 step by step: backup, checksum, a new folder; `install.ps1` without the `.env` refused (nothing created, no container touched); with the copied `.env` **all seven smoke checks passed** (API and dashboard 0.5.1); `.env` byte-for-byte unchanged, the stored key still decrypts, data kept, volumes the same, n8n data kept, an unrelated Docker project untouched. Extracting 0.5.1 over the old 0.5.0 folder instead also passed all seven checks |
| Upgrade 0.4.0 → 0.5.0 from the zips (PowerShell 7.4) | **passed** | the shipped 0.4.0 zip (checksum verified) installed, smoke test passed; data added (a site, a switched-off agent, a production server, a lead with a pending email approval, supervisor requests). Before upgrading, the new smoke test run against the old containers **failed** its version check as intended ("the API reports 0.4.0 but this folder is 0.5.0"). 0.5.0 installed over it with the copied `.env`: no new migrations; **all seven smoke checks passed**; `status.ps1` showed folder 0.5.0, API 0.5.0 and the new folder; data kept, the agent still off; the 0.4.0 email approval refused with "the target changed since this was requested" and rejected; a new one named the recipient, subject and draft and ran (recorded in the Outbox as mock); the host-key scan ran as a worker task; with no internet, a site check recorded nothing as down and set the offline state; uninstall with data deletion took a final backup (three dumps) and left an unrelated Docker project untouched |
| Server programs on Python 3.6 (EL8's platform-python) | **passed** | cc-gate, cc-wp and cc-site-helper run, time out and cap output correctly (0.5.0); all three compile and cc-gate 1.2.0's version comparison and WordPress.org lookup run (0.5.1) |
| Upgrade 0.3.0 → 0.4.0 from the zips (PowerShell 7.4) | **passed** | the shipped 0.3.0 zip installed (smoke test passed), data added (a site, a switched-off agent, a lead with a pending send approval, supervisor requests); 0.4.0 installed over it with the copied `.env`: no new migrations, data kept, the agent still off, the old approval approved and run (recorded in the Outbox), new dashboard served with `microphone=(self)`, smoke test passed; uninstall with data deletion took a final backup and left an unrelated Docker project untouched |
| Upgrade 0.2.0 → 0.3.0 from the zips (PowerShell 7.4) | **passed** | the shipped 0.2.0 installed, data added (a site, a switched-off agent, a pending approval, a Credit Repair client); 0.3.0 installed over it with the copied `.env`: migrations 0004 and 0005 applied, agents renamed, the agent still off, the old approval approved and run, Credit Repair data readable, smoke test passed |
| Package lifecycle from the 0.3.0 zip (PowerShell 7.4) | **all steps passed** | backup (three dumps), status, stop/start, uninstall keeping data, reinstall with data kept, uninstall deleting data (final backup first), fresh install with a new `.env`; a decoy Docker project left untouched |

## Cloud sandbox vs Windows and servers

| Item | Cloud sandbox | Windows 11 PC / your servers |
|---|---|---|
| 0.2.0 install and smoke test | verified | **verified by the owner** |
| 0.3.0 install, upgrade from 0.2.0, smoke test | verified (PowerShell 7.4) | **verified by the owner** (six smoke tests passed) |
| 0.4.0 upgrade from 0.3.0, smoke test, new dashboard | verified (PowerShell 7.4, Chromium) | **verified by the owner** (six smoke tests passed; the footer showed v0.2.0, see 0.5.0) |
| 0.5.0 upgrade from 0.4.0, seven smoke checks, new dashboard | verified (PowerShell 7.4, Chromium) | **installed by the owner**: six of seven smoke checks passed; the version check failed (API 0.2.0: stale build copies, fixed in 0.5.1) |
| 0.5.1 upgrade from the owner's 0.5.0, seven smoke checks | verified (PowerShell 7.4): the owner's history reproduced, then RELEASE_NOTES_0.5.1.md section 3 step by step | not yet |
| 0.6.0 upgrade from the owner's 0.5.0 (and through 0.5.1), seven smoke checks, going back | verified (PowerShell 7.4, nested Docker): the owner's history reproduced, then RELEASE_NOTES_0.6.0.md section 4 step by step | not yet: waits for the owner's approval |
| JARVIS import (test copy and live path) | verified with a synthetic JARVIS installation with planted secrets and personal data, and a synthetic backup of 2,355 sessions and 49,618 messages (1.66 GB) for speed and memory | not yet: the real backup has not been read by any 0.6.0 code; waits for the owner's approval |
| Voice dictation | the button, consent and gating tested in Chromium with a simulated recogniser; real speech recognition **not tested** (the sandbox has no microphone and no Google/Microsoft speech service) | not yet: try it in Chrome or Edge at `http://localhost:8090` |
| Scripts under Windows PowerShell 5.1 | static checks only (ASCII-only, no 6/7-only syntax); every script parses with PowerShell 7.4's parser | 0.5.0 scripts run by the owner (install, smoke test); 0.5.1 adds prepare-build.ps1 and changes install, start, smoke, status, restore and uninstall |
| cc-gate on EL8/EL9 (AlmaLinux/Rocky) with CWP | tested on Alpine Linux with OpenSSH, sudo 1.9 and Python 3.12; Python 3.6 compatibility checked separately | **not yet: nothing has been run on a CWP server** |
| Operations that download from WordPress.org (core checksums, core reinstall, plugin update/reinstall) | implemented, refusal and failure paths tested; success paths **not verified** (the sandbox cannot reach WordPress.org) | not yet |
| Monitoring real internet sites | blocked by the sandbox's network (which let the offline control check be tested for real: nothing recorded as down) | not yet |
| Host-key scan of a real server | the worker's scan tested against the disposable server; a real public server cannot be reached from the sandbox | not yet |
| Claude API with a real key | mocked only | not yet |

## Owner decisions (2026-10-19)

- **0.4.0 is approved for local Windows testing**, subject to a backup and
  checksum verification first. The owner installs and tests it; nothing here
  touches the PC, the websites or the servers.
- **Reference design received.** The dashboard should match it closely while
  keeping the real functionality. The remaining differences are documented in
  [docs/REFERENCE_DESIGN_GAP.md](docs/REFERENCE_DESIGN_GAP.md). No new release
  is made for cosmetic changes until 0.4.0 has been verified on the PC.
- **All values from real data** ("All value…" in the earlier message): no
  hardcoded or fabricated statistics, connections, uptime, tasks or agent
  activity; clear empty, disconnected or unavailable states when data is
  missing.
- **Approvals** (the earlier item 5): every production WordPress repair, CWP
  server modification, SSH change, publication, email, advertising change
  and other consequential external action needs explicit approval that names
  the exact action and target. Read-only monitoring may run automatically
  within the approved scope.

## Proposed: Research Gateway (SearXNG primary, DeepSeek optional), waiting for approval

A plan only, nothing built: [docs/RESEARCH_GATEWAY_PLAN.md](docs/RESEARCH_GATEWAY_PLAN.md),
with the proposed SearXNG files in [docs/research-gateway/](docs/research-gateway/).

- **What it proposes:**
  - one gateway in the worker; agents get READ search capabilities that need
    no approval and can never trigger an approval-gated action;
  - a private SearXNG container (no published port, JSON only, hardened;
    run three times in the sandbox);
  - DeepSeek as an optional fallback later. It is NO-GO for now: its
    documentation was only seen through search excerpts, and its policies
    store data in China.
- **Outbound screening:** secrets, contacts, Credit Repair and private JARVIS
  text never reach a search engine.
- **Proposed release:** 0.7.0 for Research, with Messaging moving to 0.8.0.
  That is one of the owner decisions in the plan's section 11.

## Planned 0.7.0: Messaging & Channels (after 0.6.0 is verified)

Builds on the existing approvals, secrets store, audit log, worker and n8n;
nothing is rebuilt. Each phase is a tested, upgradeable step.

1. **Foundations, mock providers only**: channels (tokens encrypted with the
   existing secrets key), contacts with consent, conversations and messages;
   duplicates dropped by the provider's message ID; rate limits; audit
   entries. A "Messaging & Channels" page that shows Connected only after a
   real sign-in and a health check against the provider.
2. **Telegram owner bot** (long polling: outbound connections only, no public
   port; dashboard and n8n stay on localhost). The account is linked with a
   one-time code generated and confirmed in the signed-in dashboard; a chat
   ID or phone number alone grants nothing. Commands come from a fixed list
   of read-only actions and report drafts. Approvals are never decided in
   chat: the bot links to the dashboard. Message text is data only.
3. **Telegram customer bot**: answers only from business knowledge the owner
   approves; qualifies leads into the existing Leads records; collects
   contact details only after consent; hands over to a person (assignment
   and queue). Any agent action goes through the normal approval rules.
4. **WhatsApp Cloud API**: needs a public HTTPS webhook, so a separate
   messaging gateway that checks Meta's `X-Hub-Signature-256` on every
   message and the verify-token handshake (Telegram webhooks, if used, are
   checked with the secret-token header). WhatsApp stays mock-only until the
   gateway exists and the owner approves it.
5. **Setup guides** (BotFather, Meta Business: requirements, limits, fees,
   linking to Meta's current pricing rather than quoting figures), the full
   test suite and the Windows upgrade test, keeping all data and credentials.

Fixed whatever the answers below: Credit Repair data never flows through
messaging; nothing is sent or created with real providers without the
owner's approval; the dashboard and the n8n editor stay bound to localhost.

**Decisions needed before 0.7.0 starts:**

1. Who the "authorized customers" are and how they prove who they are (today
   clients are records, not users; giving them their own reports needs a
   client model and a sign-in method).
2. Which calendar service bookings use (Google Calendar, Calendly, Cal.com).
3. Where the public gateway runs (a small VPS or a tunnel service).
4. Whether customer bots may answer automatically from approved knowledge
   (proposed: replies within an ongoing conversation yes; follow-ups,
   reminders and anything the business starts need approval).
5. Which alert severities go to Telegram or WhatsApp (business-initiated
   WhatsApp messages outside the 24-hour window need paid, approved
   templates; the Telegram Bot API is free).
6. How long message history is kept, and the wording of the consent request.

## Blockers and open questions

1. **First server run**: set up a test (non-production) server first,
   following docs/SERVER_SETUP.md, with `allow_changes` false until the
   read-only checks look right.
2. **Credit Repair** must not hold real client data before legal review
   (COMMERCIALIZATION.md B5).
3. **n8n licensing for a commercial edition** (B1) and **remote-repair terms**
   (B8) need decisions before selling.
4. **Repository** (B3): the Command Center sits inside the Jarvis repository,
   which is public; it should move to its own private repository.
5. **Remote access** (docs/REMOTE_ACCESS_PLAN.md): waiting for the DNS
   inventory report and the owner's approval; no DNS, Cloudflare or tunnel
   change is made before that.
6. **Plugin-update approvals are not bound on the server** (known
   limitation, RELEASE_NOTES_0.5.1.md section 5): cc-gate 1.2.0 installs only
   the version the Command Center names, and only WordPress.org's current,
   newer release, but it cannot see the approval itself. Binding it would need
   approvals signed outside the Command Center.
7. **JARVIS Brain (0.6.0)**: waits for the owner's approvals (install, a
   test-copy import of the real backup, then the live import; release notes
   section 8). Detection of secrets and restricted data is pattern-based:
   names and street addresses are not detected, so the owner's review in the
   test copy is required before the live import. Nothing is imported or
   activated before that, and nothing runs on the PC from here.

## Next steps

1. Owner: read docs/RELEASE_NOTES_0.6.0.md and decide on the approvals in its
   section 8. Nothing is installed or imported before that. Then 0.6.1
   (docs/RELEASE_NOTES_0.6.1.md), after 0.6.0 is verified on the PC.
2. If approved, owner: upgrade the live installation straight from 0.5.0 to
   0.6.0 (section 4: back up, check the zip against its `.sha256`, extract
   into `C:\InfiniteAI\v0.6.0`, copy `.env`, run `install.ps1`). Installing
   0.5.1 first is not needed. All **seven** smoke checks should pass, with API
   and dashboard 0.6.0. If anything fails, send the output of `install.ps1`
   and `.\scripts\status.ps1`.
3. If approved, owner, with 0.6.1 installed: the test copy (from the 0.6.1
   package) and the import of the real JARVIS backup into it (DEPLOY_WINDOWS.md, "JARVIS Brain: test copy and import"), then
   the review there; send the import report (it lists kinds and counts, never
   values) and rotate the credentials it names.
4. Only after that review and a separate approval: the live import.
5. Still open from before: the items in docs/REFERENCE_DESIGN_GAP.md, the DNS
   inventory for remote access (docs/REMOTE_ACCESS_PLAN.md), the six 0.7.0
   messaging decisions above, and a test server for SERVER_SETUP.md.
6. Next development, after 0.6.0 is verified on the PC: whatever the owner
   decides from items 3 to 5, an owner-acknowledged administrator baseline,
   and the first real integration you choose.
