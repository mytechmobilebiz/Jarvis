# Infinite AI Command Center 0.5.0: release notes

Release date: 2026-10-19 · upgrade from 0.4.0 · package
`InfiniteAI-CommandCenter-0.5.0-windows.zip` (its SHA-256 is in the `.sha256`
file next to it, in `command-center/releases/`)

0.5.0 brings the dashboard closer to the approved reference design, using
only real data. It also makes approvals name the exact action and target,
and fixes the host-key scan, which could not reach a real server.

**Nothing to migrate.** There are no new database migrations, no new keys
and no new required settings. The database, the Docker volumes, the agents
and their on/off states, the approval rules and the existing API all stay as
they are. The API only gained fields and one new read-only endpoint
(`/api/stats/task-activity`).

## Your ten requests

| # | Request | In 0.5.0 |
|---|---|---|
| 1 | Seven individual agent cards | One card per agent, each with its own colour and icon. A card shows the agent's state (Active once it has really run, Needs attention after a failure in the last 24 h, Switched off, Not in this edition), what it is doing now (running, queued, awaiting approval) or when it last ran, and its 24-hour success rate, labelled as such. A card opens that agent's page. |
| 2 | Quick Actions | In the sidebar: **Scan all websites**, **Run SEO audit** (asks which website or page), **Check servers** (service status on every enabled server with a verified host key), **Generate report** (a security or uptime report, saved to Drafts & reports), and **Open n8n**. Open n8n is shown to admins, and only when an editor address is configured. Each action runs a real read-only task and then links to it. When an action cannot run, the button is disabled and says why (agent switched off, no websites, no verified server). |
| 3 | Service status panel | Rows for n8n (the workflow engine), PostgreSQL / API, Worker, AI (Claude) or the rule-based planner, CWP servers, Email and Postonomous. Each row says what was checked and how long ago, from the API's own checks. A service that has not answered recently shows Unknown, not Running. |
| 4 | Sidebar footer showed v0.2.0 | The footer now shows **both** versions: the dashboard you are looking at, and the API it talks to. When they differ, a yellow banner says so on every page, with the fix. The smoke test has a new seventh check, "versions match this folder", and `status.ps1` prints the folder version, the running API version and the folder the API container was started from. The footer had been showing the version reported by the running API container, so on your PC that container was still running 0.2.0 code. The new check shows where it comes from. See DEPLOY_WINDOWS.md, "If the version warning appears". |
| 5 | CPU, RAM and disk gauges only after real telemetry | A **PC Resources** panel that says **Not connected**. The Command Center runs inside Docker and cannot measure the Windows PC, so no gauge is drawn. Gauges come only once a real source exists (an owner decision; see REFERENCE_DESIGN_GAP.md, C2-18). |
| 6 | Intern 2 panel, Not Connected until verified | An **Intern 2** card in the sidebar that says **Not connected**. No Intern 2 integration exists yet. It can show Connected only after a verified heartbeat (pairing with its own token), which is not built yet. |
| 7 | Density and hierarchy closer to the reference | Reference layout and sizes: a welcome header, four stat cards plus date/time and system status, the agent card row, then Workflow Activity, PC Resources, Service Status, Recent Tasks and Active Workflows. The sidebar is denser, with less-used pages under **More**. Tablet (icon rail) and phone (menu) layouts are kept. |
| 8 | Statistics tied to real data | Every figure comes from the API: agents (switched on, really run), websites (monitored, down, degraded, not checked yet), tasks today against yesterday at the same time, website uptime with its coverage, and task activity by category over 24 h, 7 days or 30 days, in your time zone. Missing data shows Loading, Unavailable, Not updated or Not checked yet, never a made-up value. |
| 9 | Preserve database, volumes, agents, approval rules, APIs | No migrations and no schema change; the existing endpoints answer as before. Tested by upgrading the shipped 0.4.0 zip with data in it (below). |
| 10 | No fake task activity, uptime or devices | No sample figures remain. Uptime counts only checks that really ran. Devices and PC resources say Not connected. The **System Uptime** tile in the reference has no data source, so it is not shown (owner decision C2-17). |

## Approvals name the exact action and target (clarification 3)

- **Email** (proposals, support replies): the approval shows the recipient,
  the subject and the opening of the exact draft, and binds a fingerprint
  (hash) of its text. If the draft is changed or swapped after the request,
  the approval no longer applies.
- **Publishing** (WordPress, Postonomous): platform, account and the exact
  draft, bound the same way.
- **WordPress repairs**: the site, the server and its pinned key, and what
  the change will do. For a plugin update or reinstall this is the version a
  recent diagnosis found ("run a diagnosis first" if there is none).
- **Server host key**: replacing a key that was trusted before (even one
  later removed) needs the server's name typed, says which key it replaces,
  and raises a critical event. Approvals given for that server and its sites
  stop applying.
- **Production**: taking a server or a site out of production needs its name
  typed, because it ends the typed confirmation for its changes.
- **Mock delivery is labelled**: approvals and the Outbox say "mock: recorded in the Outbox, not sent" for integrations that are not
  live.
- **Linking a site to a server** checks the site key against the list the
  server's cc-gate reported. Run **Test connection** on the server first.

Read-only monitoring still runs automatically, as before.

## Fixes

- **Host-key scan reaches real servers.** Since 0.3.0, "Scan host key" ran in
  the API container, which by design has no route out of Docker. It could
  never reach a real CWP server. It only worked in the test setup. The scan
  now runs in the worker, as the read-only task "Read host key". The page
  works exactly as before. If the worker is not running, the page says so
  at once.
- **Offline is not downtime.** When a website does not answer at all, the
  monitor first checks that the PC itself is online (api.wordpress.org,
  www.cloudflare.com, www.google.com). If none of them answers, the site is
  marked "not checked", not "down". Its uptime is not lowered, and a banner
  says "No internet on this PC". If your network blocks those hosts, set
  `MONITOR_CONTROL_URLS` in `.env` (see `.env.example`).
- Honest states throughout: an integration never called shows grey, not
  green; never-checked sites are counted; and the notification bell shows
  "N+" when there may be more events.

## Also in this package (inactive, nothing changes unless you act)

- `scripts\dns-inventory.ps1`: a **read-only** DNS report for
  infinitemarketingai.com, for the remote-access plan. It only reads public
  DNS and writes a text file.
- `docker-compose.tunnel.yml` and `remote-access\tunnel.conf`: the
  Cloudflare Tunnel files from docs/REMOTE_ACCESS_PLAN.md. They are **not
  used** unless you enable them in `.env` after approving the plan.
  `start.ps1` refuses to start a half-configured tunnel.

## Upgrade notes

1. Back up first: `.\scripts\backup.ps1` in the 0.4.0 folder.
2. Check the zip against the `.sha256` file.
3. Extract to a new folder, copy `.env` from the old folder, and run
   `install.ps1`, as in DEPLOY_WINDOWS.md, "Updating".
4. The smoke test now has **seven** checks. All seven should pass. If
   "versions match this folder" fails, follow DEPLOY_WINDOWS.md, "If the
   version warning appears".
5. **Pending approvals from 0.4.0** for a repair, a service restart, an email
   or a publication **can no longer be approved**. They do not carry the
   exact target that 0.5.0 binds, so approving one is refused with "the
   target changed since this was requested". Reject them and request them
   again. Changes approved in 0.4.0 that are still waiting to run are refused
  the same way when they start. Changes that already ran are not affected.

## Tests (cloud sandbox)

| Suite | Result |
|---|---|
| Backend, unit and integration (pytest, PostgreSQL 18) | see PROJECT_STATUS.md, "Test results" |
| Dashboard (Vitest) | see PROJECT_STATUS.md |
| End-to-end on the running stack (Playwright Chromium; disposable WordPress with real OpenSSH and cc-gate) | see PROJECT_STATUS.md |
| Upgrade 0.4.0 → 0.5.0 from the zips (PowerShell 7.4) | see PROJECT_STATUS.md |

Nothing was run on your PC, your websites or your servers.

## Known limitations

- The gate runs `wp plugin update` without pinning a version. If WordPress.org
  publishes a newer version between the approval and the run, that newer
  version is installed. The approval shows the version found when it was
  requested. Pinning needs a cc-gate change, planned for a later release.
- At 1440 × 900 the dashboard needs about 1,280 px of height. The reference
  fits in 1,024, because 0.5.0 keeps the Recent Tasks and Active Workflows
  panels below the fold rather than shrinking text.
- Owner decisions still open (REFERENCE_DESIGN_GAP.md): what Intern 2 is and
  how it connects; a source for PC resources; system uptime; approval
  expiry and self-approval (C3-12, C3-13); on-demand checks of any public
  URL (C3-11); automatic outbound data flows (C3-14); C3-09 and C3-10 are
  deferred.
- Minor: an offline sweep still counts as a website scan in task activity.
  Agent cards keep their last colour under "Not updated" during an outage.
  Credit-repair task *counts* (never their details) are visible to users
  without the sensitive-data permission. The schedule switches on the
  Workflows page pause without asking. The sign-in page still uses the
  older logo tile.
