# Project status: Infinite AI Command Center

Last updated: 2026-10-18 (UTC) · version 0.2.0 · branch `ccr-eb27a2af-8u7i39`

## Where it is being built

All development and testing so far has happened in **Anthropic's cloud
sandbox** (a Linux container with Docker 29), not on the owner's Windows PC.
The sandbox has no connection to that PC. Nothing in this document has been
verified on Windows yet; see "Cloud sandbox vs Windows" below.

## Milestones

| Milestone | State |
|---|---|
| M1: dashboard, PostgreSQL, n8n, AI supervisor, read-only WordPress monitor | **Done** in the cloud sandbox |
| M1b: Windows 11 deployment package (Docker Desktop + WSL 2) | **Done** in the sandbox; needs one run on the PC (`install.ps1`) |
| M2: commercial-ready architecture (workflow-engine interface, editions, settings split, SBOM) | **Done** in the sandbox; plan and blockers in COMMERCIALIZATION.md |
| M3: the seven specialist agents, incl. the isolated Credit Repair module | **Done** in the sandbox, with **mock** outward integrations |
| M4: real integrations (email, publishing, ads, notifications) | Needs owner approval and credentials |

Estimated MVP completion: **about 70%**. This is an estimate, weighted by the
work remaining (Windows verification, real integrations, owner decisions),
not a measurement.

## The agents (M3)

The seven agents below are **inferred** from the Foundation Starter and the
brief; the full handoff never arrived. Please confirm or correct them.

| Agent | Works for real today | Mock / sample in this build | Needs approval |
|---|---|---|---|
| AI Supervisor | Plans requests (Claude or rules), delegates, summarises, daily briefing, health review | | |
| WordPress & Hosting Monitor | Read-only website checks; read-only server checks (DNS, one TCP connect per port, TLS on 443); never SSH or panel credentials | | |
| Sales & Proposals | Lead scoring, spam detection, proposal drafts (HTML) from your template or a built-in one without prices; n8n lead-intake webhook | Sending (mock email) | sending a proposal |
| Client Support | Triage (category, priority, sentiment), reply drafts (Claude or templates) | Sending (mock email) | sending a reply |
| Content & Social | Blog and social drafts (Claude or templates) | Publishing (mock WordPress / social) | publishing |
| SEO & Research | Read-only on-page audit (title, description, headings, indexability, images, structured data, robots.txt, sitemap); keyword briefs | | |
| Ads & Analytics | ROAS / CPA report and anomaly flags | **Sample data** (no ad account connected); budget changes are mock | any budget change (financial) |
| Credit Repair (isolated) | Encrypted client store in its own database; compliance timing checks (contract, 3-business-day wait, advance fees); FCRA dispute-letter drafts, one per bureau | Mailing (mock postal mail) | mailing a letter; module off until an admin enables it |

Every approved mock action is listed on the **Outbox** page as
"recorded, not sent". Nothing leaves the machine.

## Completed

- Backend (FastAPI, PostgreSQL 18, SQL migrations): sign-in with hashed
  session tokens and lockout; viewer/operator/admin roles; scoped n8n
  service token; CSRF protection; audit log; health and status endpoints.
- Agent framework: capabilities with declared risk; the approval gate in the
  task layer; a read-only HTTP client that blocks private addresses; worker
  with `SKIP LOCKED` claiming, timeouts, retries, stale-task reaping; cron
  scheduler; events with delivery to n8n and backoff.
- AI supervisor: plans with Claude (structured output, refusal handling,
  daily request cap) or deterministic rules; never plans Credit Repair work.
- The seven agents above, the Leads, Client support, Outbox and Credit Repair
  pages, a sensitive-data permission per user, and an n8n lead-intake
  webhook (header secret) that hands web-form leads to the Sales agent.
- Credit Repair isolation: separate database and login role (the main role
  cannot connect), field encryption with its own key, per-user permission
  (off for admins too), read-only for viewers, own access log, references
  only (`CR-0001`) anywhere else. Details in COMMERCIALIZATION.md section 4.
- n8n 2.43.7 on PostgreSQL: owner, credentials and three workflows
  provisioned at start-up (event router with a placeholder notification step
  that sends nothing, weekday morning briefing, lead intake).
- Windows scripts (PowerShell 5.1+): `install`, `preflight` (read-only),
  `init-env` (now also `-AddMissing` for upgrades), `start`, `status`,
  `stop`, `backup` (all three databases), **`restore`** (new; safety backup
  first, typed confirmation), `smoke`, `uninstall`.
- Commercial-ready architecture (M2): workflow-engine interface, editions
  with enforced limits, signed offline licenses, customer vs deployment
  settings, encrypted customer API keys, SBOM.

## Test results (latest runs, cloud sandbox)

| Suite | Result | What it covers |
|---|---|---|
| Backend unit + integration (pytest, real PostgreSQL 18) | **158 passed, 0 failed** | API, auth, roles, approvals, worker, supervisor, all agents, mock integrations and their approval gates, Credit Repair isolation and compliance gates, prompt-injection wrapping, schema, licenses, editions, secrets, settings, users |
| Dashboard (Vitest) | **13 passed, 0 failed** | sign-in, approvals, supervisor, viewer restrictions, Credit Repair menu and enable gate, leads, outbox |
| End-to-end, running stack (pytest + Playwright Chromium) | **27 passed** in each of the last 3 consecutive full runs | real n8n round trip and lead-intake webhook, real local WordPress, read-only verified from WordPress's access log (monitor and SEO audit), mock proposal email after approval, support triage, Credit Repair data absent from the main database and the main role refused by the Credit Repair database, all dashboard pages in a browser |
| Upgrade 0.1.0 → 0.2.0 from the zips (PowerShell 7.4) | **passed** | the shipped 0.1.0 installed and smoke-tested; 0.2.0 installed over it with the copied `.env`: missing keys added, migrations applied, data kept, smoke test passed |
| Backup and restore (PowerShell 7.4) | **passed** | three dumps; changes made after the backup rolled back in both the main and Credit Repair databases; smoke test passed after restore |
| Package lifecycle from the 0.2.0 zip (PowerShell 7.4) | **all steps passed** | fresh install, status, stop/start, uninstall keeping data, reinstall with data kept, uninstall deleting data (final backup taken); a decoy Docker project left untouched |
| `docker compose config` | valid, base and test overlay | |

Problems found and fixed during this milestone's runs: two support tasks for
the same message could overwrite each other's result (fixed with a row lock;
5 repeated runs clean); the sidebar could not scroll on a 720-pixel-high
window, hiding Sign out (fixed); a test locator matched two "Settings" links
(test fixed); upgrading from 0.1.0 would have stopped on missing `.env` keys
(fixed: `install.ps1` adds them).

## Cloud sandbox vs Windows

| Item | Cloud sandbox | Windows 11 PC |
|---|---|---|
| Containers build and run, all services healthy | verified | **not yet** |
| n8n provisioning, webhook round trip, lead intake, n8n calling the API | verified | not yet |
| WordPress monitor and SEO audit against a real (local, throwaway) WordPress | verified | not yet |
| Install, upgrade, backup, restore, uninstall under PowerShell **7.4** (Linux) | verified | n/a |
| Scripts under Windows PowerShell **5.1** | static checks only (ASCII-only, no 6/7-only syntax); Microsoft's analyzer could not be installed (PowerShell Gallery blocked here) | **not yet** |
| `preflight.ps1` Windows-only parts (`Get-NetTCPConnection`, `wsl.exe`) | not runnable | not yet |
| Monitoring real internet sites, `api.wordpress.org` version lookup | blocked by the sandbox's DNS | not yet |
| Claude API with a real key | mocked only (no key) | not yet |

Verification on the PC = `install.ps1` finishing with `SMOKE TEST PASSED`.

## Blockers and open questions

1. **Full handoff not received.** The seven agents are inferred; please
   confirm them or send the handoff.
2. **Not yet run on Windows** (see the table above).
3. **Credit Repair** must not hold real client data before legal review
   (CROA, FCRA, the FTC Telemarketing Sales Rule, Georgia and other state
   law, data-security rules): COMMERCIALIZATION.md blocker B5.
4. **n8n licensing for a commercial edition** (B1): fine for your own use;
   distribution needs n8n's commercial agreement or another engine.
5. **Repository** (B3): the Command Center sits inside the Jarvis
   repository; it should move to its own repository before distribution.
6. **Intermittent UI test** (`test_supervisor_console`) seen twice in
   earlier milestones right after stack restarts; not seen in the 15 full
   end-to-end runs since.

## Remaining integrations (all need owner approval and credentials)

| Integration | State today |
|---|---|
| Claude (Anthropic API) | Implemented; inactive until a key is entered on the Settings page (metered) |
| Owner notifications (email, Slack, WhatsApp) | Placeholder node in n8n; sends nothing |
| Email sending (proposals, support replies) | Mock: recorded in the Outbox |
| WordPress publishing | Mock; the monitor and SEO audit are read-only by design |
| Social publishing (Postiz / Meta) | Mock |
| Ad platforms (Meta / Google Ads) | Sample data; budget changes mock |
| Certified mail for dispute letters | Mock; only after legal review |
| CWP servers | Read-only network checks only; never SSH, root or panel credentials |

## Next steps

1. Owner: run `install.ps1` on the PC (or upgrade an existing 0.1.0 install,
   see docs/DEPLOY_WINDOWS.md) and send back the smoke-test output.
2. Owner: confirm the seven agents; add your proposal template to
   `templates\proposal-template.html` if you want it used.
3. Owner: decide which real integration comes first (for example email
   through your SMTP account) and provide credentials through the dashboard
   once that integration exists.
4. Owner: decisions on B1, B3 and B5 in COMMERCIALIZATION.md.
