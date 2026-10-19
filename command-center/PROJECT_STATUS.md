# Project status: Infinite AI Command Center

Last updated: 2026-10-19 (UTC) · version 0.5.0 · branch `ccr-eb27a2af-8u7i39`

## Where it is being built

Development and testing happen in **Anthropic's cloud sandbox** (a Linux
container with Docker 29), which has no connection to the owner's PC or
servers. Version **0.4.0** has been installed by the owner on the Windows 11
PC (all six smoke tests passed; the sidebar footer still showed v0.2.0, see
"Completed in 0.5.0"). Version **0.5.0** has not been run on Windows yet, and
**nothing has been run on a real CWP server**; see "Cloud sandbox vs Windows
and servers" below.

## Milestones

| Milestone | State |
|---|---|
| M1: dashboard, PostgreSQL, n8n, AI supervisor, read-only WordPress monitor | Done; 0.2.0 verified on the owner's PC |
| M1b: Windows 11 deployment package (Docker Desktop + WSL 2) | Done; 0.2.0 verified on the owner's PC |
| M2: commercial-ready architecture (workflow-engine interface, editions, settings split, SBOM) | Done in the sandbox; blockers in COMMERCIALIZATION.md |
| M3: specialist agents and the isolated Credit Repair module | Done in the sandbox, with **mock** outward integrations |
| M4 (0.3.0): the seven official agents, complete WordPress monitoring, restricted SSH to CWP servers, approval-based WP-CLI repairs | Done; 0.3.0 installed on the owner's PC (smoke tests passed); tested against disposable WordPress installs only; needs a first, careful run on a test server |
| UI (0.4.0): official dashboard redesign: dark command-center interface, AI command input, new sidebar sections | Done; installed on the owner's PC (six smoke tests passed) |
| 0.5.0: dashboard to the reference with real data only; approvals name the exact action and target; host-key scan fixed | **Done in the sandbox**; not yet run on the PC. Remaining differences and owner decisions: docs/REFERENCE_DESIGN_GAP.md |
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

## Completed in 0.5.0 (no database migrations; API additions only)

Full list and upgrade notes: [docs/RELEASE_NOTES_0.5.0.md](docs/RELEASE_NOTES_0.5.0.md).

- **The owner's ten requests**: seven agent cards; Quick Actions (scan all
  websites, SEO audit, check servers, generate a report, open n8n); a
  Service Status panel where each row says what was checked and when; PC
  Resources and the Intern 2 card shown as **Not connected** (no source
  exists for either); a denser layout closer to the reference; task activity
  by category over 24 h / 7 d / 30 d. Every value comes from the API.
- **v0.2.0 in the footer**: the footer showed the version reported by the
  running API container, so on the PC that container was still running 0.2.0
  code. The exact cause cannot be seen from here. 0.5.0 shows the dashboard's
  and the API's versions, warns on every page when they differ, and the smoke
  test (now **seven** checks) and `status.ps1` check and print them.
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

Two adversarial review rounds by independent agents ran on the SSH layer,
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
| Backend unit + integration (pytest, real PostgreSQL 18) | **501 passed, 0 failed** (0.5.0) | API, auth, roles, approvals (binding, production, run-time re-checks), worker, supervisor, all agents, the gate and its installer, cc-wp and the site helper (real programs), SSH through a real asyncssh server running the real cc-gate, repairs, monitoring checks incl. hostile-input timing, Credit Repair isolation |
| Dashboard (Vitest) | **166 passed** (0.5.0) | the 0.5.0 dashboard (stat cards, agent cards, service status, PC resources and Intern 2 not connected, task activity by category, recent tasks, active workflows), Quick Actions, the version line and mismatch banner, the offline banner, approvals with exact targets and typed confirmations, plus everything from 0.4.0: sign-in, sidebar sections by role, the command input, voice input gating, keyboard paths, Credit Repair details hidden without permission |
| End-to-end, running stack (pytest + Playwright Chromium) | **41 passed** in each of the last 3 consecutive full runs (0.5.0 production build) | the 0.5.0 dashboard (seven agent cards, service status from real checks, PC Resources not connected), the command input running a real request, every sidebar section opening without errors (including the pages under More), everything from 0.2.0, plus a disposable WordPress server with **real OpenSSH, sudo, WP-CLI and cc-gate installed by install-gate.sh**: the host key scanned **by the worker** and pinned; the key can do nothing but run cc-gate; cloaking malware found and its plugin deactivated after approval; hijacked home/siteurl repaired; debug.log quarantined; a planted WP-CLI config ignored; a local plugin never reinstalled from WordPress.org; service restart through the sudo rule; production confirmation (and the typed name to leave production); audit on both sides; an impostor host key refused |
| PowerShell scripts | **all 11 parse** with PowerShell's own parser; ASCII-only | the parser check is new in 0.5.0, after the upgrade test found a parse error in the new smoke check (fixed before release) |
| Upgrade 0.4.0 → 0.5.0 from the zips (PowerShell 7.4) | **passed** | the shipped 0.4.0 zip (checksum verified) installed, smoke test passed; data added (a site, a switched-off agent, a production server, a lead with a pending email approval, supervisor requests). Before upgrading, the new smoke test run against the old containers **failed** its version check as intended ("the API reports 0.4.0 but this folder is 0.5.0"). 0.5.0 installed over it with the copied `.env`: no new migrations; **all seven smoke checks passed**; `status.ps1` showed folder 0.5.0, API 0.5.0 and the new folder; data kept, the agent still off; the 0.4.0 email approval refused with "the target changed since this was requested" and rejected; a new one named the recipient, subject and draft and ran (recorded in the Outbox as mock); the host-key scan ran as a worker task; with no internet, a site check recorded nothing as down and set the offline state; uninstall with data deletion took a final backup (three dumps) and left an unrelated Docker project untouched |
| Server programs on Python 3.6 (EL8's platform-python) | **passed** | cc-gate, cc-wp and cc-site-helper run, time out and cap output correctly |
| Upgrade 0.3.0 → 0.4.0 from the zips (PowerShell 7.4) | **passed** | the shipped 0.3.0 zip installed (smoke test passed), data added (a site, a switched-off agent, a lead with a pending send approval, supervisor requests); 0.4.0 installed over it with the copied `.env`: no new migrations, data kept, the agent still off, the old approval approved and run (recorded in the Outbox), new dashboard served with `microphone=(self)`, smoke test passed; uninstall with data deletion took a final backup and left an unrelated Docker project untouched |
| Upgrade 0.2.0 → 0.3.0 from the zips (PowerShell 7.4) | **passed** | the shipped 0.2.0 installed, data added (a site, a switched-off agent, a pending approval, a Credit Repair client); 0.3.0 installed over it with the copied `.env`: migrations 0004 and 0005 applied, agents renamed, the agent still off, the old approval approved and run, Credit Repair data readable, smoke test passed |
| Package lifecycle from the 0.3.0 zip (PowerShell 7.4) | **all steps passed** | backup (three dumps), status, stop/start, uninstall keeping data, reinstall with data kept, uninstall deleting data (final backup first), fresh install with a new `.env`; a decoy Docker project left untouched |

## Cloud sandbox vs Windows and servers

| Item | Cloud sandbox | Windows 11 PC / your servers |
|---|---|---|
| 0.2.0 install and smoke test | verified | **verified by the owner** |
| 0.3.0 install, upgrade from 0.2.0, smoke test | verified (PowerShell 7.4) | **verified by the owner** (six smoke tests passed) |
| 0.4.0 upgrade from 0.3.0, smoke test, new dashboard | verified (PowerShell 7.4, Chromium) | **verified by the owner** (six smoke tests passed; the footer showed v0.2.0, see 0.5.0) |
| 0.5.0 upgrade from 0.4.0, seven smoke checks, new dashboard | verified (PowerShell 7.4, Chromium) | not yet |
| Voice dictation | the button, consent and gating tested in Chromium with a simulated recogniser; real speech recognition **not tested** (the sandbox has no microphone and no Google/Microsoft speech service) | not yet: try it in Chrome or Edge at `http://localhost:8090` |
| Scripts under Windows PowerShell 5.1 | static checks only (ASCII-only, no 6/7-only syntax); every script parses with PowerShell 7.4's parser | 0.4.0 scripts verified by the owner; smoke, status, start, install, restore and uninstall changed in 0.5.0 |
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

## Planned 0.6.0: Messaging & Channels (after 0.5.0 is verified)

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

**Decisions needed before 0.6.0 starts:**

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

## Next steps

1. Owner: back up, verify the 0.5.0 zip against its `.sha256`, and upgrade
   (DEPLOY_WINDOWS.md, "Updating": extract to a new folder, copy `.env`, run
   `install.ps1`). All **seven** smoke checks should pass. If "versions match
   this folder" fails, send the output of `.\scripts\status.ps1`.
2. Owner: send the Windows results and a screenshot of the dashboard (the
   screenshot sent with the 0.4.0 results did not arrive here).
3. Owner: reject and re-request any repair, restart, email or publication
   approvals still pending from 0.4.0.
4. Owner decisions: the open items in docs/REFERENCE_DESIGN_GAP.md (Intern 2,
   PC resources, system uptime, C3-11 to C3-14); for remote access, run
   `scripts\dns-inventory.ps1` and send the report (docs/REMOTE_ACCESS_PLAN.md);
   the six 0.6.0 messaging decisions above.
5. Owner: on a test server, follow SERVER_SETUP.md; on the CWP / Servers page
   scan and compare the host key (this now works from the PC), then run Test
   connection and a server-side check.
6. Next development, after 0.5.0 is verified on the PC: whatever the owner
   decides from item 4, pinning the plugin version in cc-gate's update
   (C3-07), an owner-acknowledged administrator baseline, and the first real
   integration you choose (for example email through your SMTP account).
