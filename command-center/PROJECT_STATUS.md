# Project status: Infinite AI Command Center

Last updated: 2026-10-18 (UTC) · version 0.3.0 · branch `ccr-eb27a2af-8u7i39`

## Where it is being built

Development and testing happen in **Anthropic's cloud sandbox** (a Linux
container with Docker 29), which has no connection to the owner's PC or
servers. Version **0.2.0** has been installed by the owner on the Windows 11
PC (all six smoke tests passed). Version **0.3.0** has not been run on
Windows yet, and **nothing has been run on a real CWP server**; see "Cloud
sandbox vs Windows and servers" below.

## Milestones

| Milestone | State |
|---|---|
| M1: dashboard, PostgreSQL, n8n, AI supervisor, read-only WordPress monitor | Done; 0.2.0 verified on the owner's PC |
| M1b: Windows 11 deployment package (Docker Desktop + WSL 2) | Done; 0.2.0 verified on the owner's PC |
| M2: commercial-ready architecture (workflow-engine interface, editions, settings split, SBOM) | Done in the sandbox; blockers in COMMERCIALIZATION.md |
| M3: specialist agents and the isolated Credit Repair module | Done in the sandbox, with **mock** outward integrations |
| M4 (0.3.0): the seven official agents, complete WordPress monitoring, restricted SSH to CWP servers, approval-based WP-CLI repairs | **Done in the sandbox**, tested against disposable WordPress installs only; needs one run on the PC and a first, careful run on a test server |
| M5: real integrations (email, publishing, ads, notifications) | Needs owner approval and credentials |

Estimated MVP completion: **about 80%**. This is an estimate, weighted by the
work remaining (Windows and server verification, real integrations, owner
decisions), not a measurement.

## The seven agents and the AI Supervisor

The AI Supervisor is separate from the seven agents (its own section on the
Agents page): it plans requests, delegates to the agents and summarises.

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
| Backend unit + integration (pytest, real PostgreSQL 18) | **425 passed, 0 failed** | API, auth, roles, approvals (binding, production, run-time re-checks), worker, supervisor, all agents, the gate and its installer, cc-wp and the site helper (real programs), SSH through a real asyncssh server running the real cc-gate, repairs, monitoring checks incl. hostile-input timing, Credit Repair isolation |
| Dashboard (Vitest) | **19 passed** | sign-in, approvals incl. typed production confirmation, servers page, partial server checks, records pages |
| End-to-end, running stack (pytest + Playwright Chromium) | **39 passed** in each of the last 3 consecutive full runs | everything from 0.2.0, plus a disposable WordPress server with **real OpenSSH, sudo, WP-CLI and cc-gate installed by install-gate.sh**: the key can do nothing but run cc-gate; cloaking malware found and its plugin deactivated after approval; hijacked home/siteurl repaired; debug.log quarantined; a planted WP-CLI config ignored; a local plugin never reinstalled from WordPress.org; service restart through the sudo rule; production confirmation; audit on both sides; an impostor host key refused |
| Server programs on Python 3.6 (EL8's platform-python) | **passed** | cc-gate, cc-wp and cc-site-helper run, time out and cap output correctly |
| Upgrade 0.2.0 → 0.3.0 from the zips (PowerShell 7.4) | **passed** | the shipped 0.2.0 installed, data added (a site, a switched-off agent, a pending approval, a Credit Repair client); 0.3.0 installed over it with the copied `.env`: migrations 0004 and 0005 applied, agents renamed, the agent still off, the old approval approved and run, Credit Repair data readable, smoke test passed |
| Package lifecycle from the 0.3.0 zip (PowerShell 7.4) | **all steps passed** | backup (three dumps), status, stop/start, uninstall keeping data, reinstall with data kept, uninstall deleting data (final backup first), fresh install with a new `.env`; a decoy Docker project left untouched |

## Cloud sandbox vs Windows and servers

| Item | Cloud sandbox | Windows 11 PC / your servers |
|---|---|---|
| 0.2.0 install and smoke test | verified | **verified by the owner** |
| 0.3.0 install, upgrade from 0.2.0, smoke test | verified (PowerShell 7.4) | not yet |
| Scripts under Windows PowerShell 5.1 | static checks only (ASCII-only, no 6/7-only syntax) | 0.2.0 scripts verified; 0.3.0 scripts unchanged since |
| cc-gate on EL8/EL9 (AlmaLinux/Rocky) with CWP | tested on Alpine Linux with OpenSSH, sudo 1.9 and Python 3.12; Python 3.6 compatibility checked separately | **not yet: nothing has been run on a CWP server** |
| Operations that download from WordPress.org (core checksums, core reinstall, plugin update/reinstall) | implemented, refusal and failure paths tested; success paths **not verified** (the sandbox cannot reach WordPress.org) | not yet |
| Monitoring real internet sites | blocked by the sandbox's network | not yet |
| Claude API with a real key | mocked only | not yet |

## Blockers and open questions

1. **Your last message ended mid-sentence** after item 5 ("... explicit
   approvals for production"). Please send anything that followed.
2. **First server run**: set up a test (non-production) server first,
   following docs/SERVER_SETUP.md, with `allow_changes` false until the
   read-only checks look right.
3. **Credit Repair** must not hold real client data before legal review
   (COMMERCIALIZATION.md B5).
4. **n8n licensing for a commercial edition** (B1) and **remote-repair terms**
   (B8) need decisions before selling.
5. **Repository** (B3): the Command Center sits inside the Jarvis repository,
   which is public; it should move to its own private repository.

## Next steps

1. Owner: upgrade the PC to 0.3.0 (DEPLOY_WINDOWS.md, "Updating": back up,
   extract the new zip, copy `.env`, run `install.ps1`) and send the
   smoke-test output.
2. Owner: on a test server, follow SERVER_SETUP.md; on the Servers page scan
   and compare the host key, then run Test connection and a server-side check.
3. Next development: an owner-acknowledged administrator baseline, the first
   real integration you choose (for example email through your SMTP
   account), and whatever followed item 5 of your last message.
