# Project status: Infinite AI Command Center

Last updated: 2026-10-18 (UTC) · version 0.1.0 · branch `ccr-eb27a2af-8u7i39`

## Where it is being built

All development and testing so far has happened in **Anthropic's cloud
sandbox** (a Linux container with Docker 29), not on the owner's Windows PC.
The sandbox has no connection to that PC. Nothing in this document has been
verified on Windows yet; see "Cloud sandbox vs Windows" below.

## Milestones

| Milestone | State |
|---|---|
| M1: dashboard, PostgreSQL, n8n, AI supervisor, read-only WordPress monitor | **Done** in the cloud sandbox (tests below) |
| M1b: Windows 11 deployment package (Docker Desktop + WSL 2) | **Done** in the sandbox; needs one run on the PC (`install.ps1`) |
| M2: commercial-ready architecture (workflow-engine interface, editions, settings split, SBOM) | **Done** in the sandbox; plan and blockers in COMMERCIALIZATION.md |
| M3: remaining agent modules, incl. the isolated Credit Repair Agent | Planned |
| M4: real integrations (email, publishing, ads, notifications) | Needs owner approval and credentials |

Estimated MVP completion: **about 55%**. This is an estimate, weighted by
the work remaining, not a measurement.

## Completed

- Backend (FastAPI, PostgreSQL 18, SQL migrations): sign-in with hashed
  session tokens and lockout; viewer/operator/admin roles; scoped n8n
  service token; CSRF protection; audit log; health and status endpoints.
- Agent framework: capabilities with declared risk; the approval gate in the
  task layer; a read-only HTTP client that blocks private addresses; worker
  with `SKIP LOCKED` claiming, timeouts, retries, stale-task reaping; cron
  scheduler; events with delivery to n8n and backoff.
- AI supervisor: plans requests with Claude (structured output, refusal
  handling, daily request cap) or, with no API key, with deterministic rules;
  summaries; daily briefing; hourly platform health review.
- WordPress Monitor (read-only): uptime, response time, TLS expiry and
  validity, WordPress detection and version, plugins and themes seen,
  XML-RPC, public user listing (count only), readme.html, debug.log,
  directory listing, end-of-life PHP; alerts on change only.
- n8n 2.43.7 on PostgreSQL: owner account, credentials and workflows
  provisioned at start-up; event router (with a placeholder notification step
  that sends nothing) and the weekday morning-briefing workflow.
- Dashboard: sign-in, overview, supervisor console, agents, websites,
  approvals, tasks, drafts and reports, schedules, integrations, activity log.
- Windows scripts (PowerShell 5.1+): `install`, `preflight` (read-only),
  `init-env`, `start`, `status`, `stop`, `backup`, `smoke`, `uninstall`.
- Clean package builder: only git-tracked files, no tests or dev files,
  refuses to build if a local secret would be included.
- Commercial-ready architecture (M2): a `WorkflowEngine` interface (n8n,
  generic HMAC-signed webhook, none); Personal / Professional / Agency
  editions with enforced agent, site and user limits; offline Ed25519-signed
  license files; customer settings (database, admin-only) separated from
  deployment settings (.env); customer API keys stored encrypted and never
  shown again; per-task AI provider resolution; user management; SBOM
  (CycloneDX); COMMERCIALIZATION.md with the blockers to distribution.

## Active

- M3: the remaining agent modules, starting with the isolated Credit Repair
  Agent's data layer (design in COMMERCIALIZATION.md section 4).

## Test results (latest runs, cloud sandbox)

| Suite | Result | What it covers |
|---|---|---|
| Backend unit + integration (pytest, real PostgreSQL 18) | **130 passed, 0 failed** | API, auth, roles, approvals, worker, scheduler, supervisor, WordPress checks, TLS probe, Claude provider (mocked API), schema, licenses, editions, encrypted secrets, settings, users, workflow engines |
| Dashboard (Vitest) | **9 passed, 0 failed** | sign-in, approvals, supervisor, viewer restrictions, safe Markdown |
| End-to-end, running stack (pytest + Playwright Chromium) | **21 passed**; 8 further consecutive full runs passed (one earlier intermittent failure, not reproduced since) | real n8n round trip, real local WordPress, read-only verified from WordPress's access log, browser flows, Settings page |
| `smoke.ps1` against the stack (PowerShell 7.4) | **6 of 6 passed** | health, sign-in, worker, n8n round trip, supervisor |
| Package lifecycle from the zip (PowerShell 7.4) | **all steps passed** | install, status, backup (restorable), stop/start, uninstall keeping data, reinstall, uninstall deleting data; a decoy Docker project left untouched |
| `docker compose config` | valid, base and test overlay | |

## Cloud sandbox vs Windows

| Item | Cloud sandbox | Windows 11 PC |
|---|---|---|
| Containers build and run, all services healthy | verified | **not yet** |
| n8n provisioning, webhook round trip, n8n calling the API | verified | not yet |
| WordPress monitor against a real (local, throwaway) WordPress | verified | not yet |
| Scripts under PowerShell **7.4** (Linux) | verified | n/a |
| Scripts under Windows PowerShell **5.1** | static checks only (ASCII-only, no 6/7-only syntax or parameters); Microsoft's analyzer could not be installed (PowerShell Gallery blocked here) | **not yet** |
| `preflight.ps1` Windows-only parts (`Get-NetTCPConnection`, `wsl.exe`) | not runnable | not yet |
| Bind mounts from a Windows path through Docker Desktop | not testable | not yet |
| Monitoring real internet sites, `api.wordpress.org` version lookup | blocked by the sandbox's DNS | not yet |
| Claude API with a real key | mocked only (no key) | not yet |

Verification on the PC = `install.ps1` finishing with `SMOKE TEST PASSED`.

## Blockers and open questions

1. **Full handoff not received.** Only the Foundation Starter arrived. The
   seven agents are inferred and need your confirmation (proposed:
   WordPress & Hosting Monitor, Credit Repair, Sales & Proposals, Client
   Support, Content & Social, SEO & Research, Ads & Analytics).
2. **Not yet run on Windows** (see the table above).
3. **Credit Repair Agent** needs your decisions and legal review before it
   handles real client data (federal CROA and FCRA rules and state law);
   see COMMERCIALIZATION.md once written.
4. **n8n licensing for a commercial edition**: n8n's Sustainable Use License
   does not allow including it in a paid product without a commercial (Embed)
   agreement. The personal deployment is fine; distribution is blocked until
   resolved (a workflow-engine interface is planned in M2).
5. **Repository**: the Command Center sits inside the Jarvis repository,
   which also holds the purchased Jarvis source. Before distribution it
   should move to its own repository.
6. **Intermittent UI test** (`test_supervisor_console`): failed twice, both
   times right after a stack restart; not reproduced in 8 consecutive full
   runs since. The suite now keeps a screenshot of any failure.

## Remaining integrations (all need owner approval and credentials)

| Integration | State today |
|---|---|
| Claude (Anthropic API) | Implemented; inactive until `ANTHROPIC_API_KEY` is set (metered) |
| Owner notifications (email, Slack, WhatsApp) | Placeholder node in n8n; sends nothing |
| Email sending (proposals, replies) | Not implemented (mock planned) |
| WordPress publishing / changes | Not implemented; monitor is read-only by design |
| Social (Postiz / Meta), ads (Meta / Google), analytics | Not implemented (mock planned) |
| CWP server checks | Planned as read-only network checks only; never SSH or root credentials |
| Credit bureau / dispute services | Not planned without legal review |

## Next steps

1. Owner: run `install.ps1` on the PC and send back the `smoke.ps1` output.
2. Owner: confirm the seven agents, or send the full handoff.
3. Owner: decide on blockers B1 (n8n licensing) and B3 (own repository) in COMMERCIALIZATION.md.
4. M3: the remaining agents (mock integrations, approval-gated), Credit Repair
   in an isolated database.
