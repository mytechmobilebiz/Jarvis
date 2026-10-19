# Reference design vs 0.4.0: differences and the 0.5.0 plan

The owner's original dashboard design (a 1536×1024 screenshot, kept out of
this public repository) was compared element by element with 0.4.0 as
installed. Four independent reviews ran, each checked by a separate verifier:

1. **Visual**: layout, colours (sampled from the image), type, spacing, icons:
   57 differences.
2. **Data**: for every value the reference shows, the real source in this
   application, or why it cannot be shown truthfully: 26 elements.
3. **Approvals** (owner clarification 3): 24 checks.
4. **Real data only** (owner clarification 2): 22 checks.

**Binding rules** (owner, 2026-10-19): match the reference closely while
keeping the real functionality; every value from actual application data,
with clear empty, disconnected or unavailable states; every consequential
external action needs an explicit approval that names the exact action and
target; preserve the database, Docker volumes, agents, approval rules and
APIs (additive read-only API changes only).

**Status after 0.5.0** (2026-10-19): the items marked "0.5.0" below are in
0.5.0 (see [RELEASE_NOTES_0.5.0.md](RELEASE_NOTES_0.5.0.md)), with one
exception: C3-07 shows the plugin version a diagnosis found, but cc-gate
does not yet pin that version when it updates. Items marked "deferred" or
"owner decision" are unchanged.

## Summary

- The reference is **denser** than 0.4.0 everywhere: 70 px header, 208 px
  sidebar, 8 px radii, flat lighter-navy surfaces, Segoe UI type. It fits in
  1536×1024; 0.4.0 needs 1,719 px of height at 1440 px wide.
- **Biggest structural gaps**: a 5-card stat row with round icon badges; a
  row of 7 agent cards; a multi-series *Workflow Activity* chart with a range
  selector; *System Resources* gauges; a *Service Status* list; a *Recent
  Tasks* table; *Active Workflows* with toggles; sidebar *Quick Actions*; a
  solid blue active menu item; a green *Execute* button beside the input.
- **Every number in the reference is illustrative** and must not be copied:
  7 Active, 12 sites, 48 tasks, ↑23 %, 99.9 % "System Uptime", CPU 18 % /
  RAM 42 % / Disk 68 % with "Ryzen 9 5950X / 64 GB / C: Drive", "Intern 2
  Connected", all eight service states, the five task rows and the seven
  workflow names.
- **Cannot be shown truthfully today** (owner decisions below): the Windows
  PC's CPU, memory and C: drive; a "Docker running" check; any Intern 2
  connection; a platform-uptime history.
- **The audits found real gaps** beyond looks: some approvals do not name
  their exact target (a wrongly linked site could make an SSH repair change
  a different WordPress install; email and publish approvals show bare ids),
  and some indicators were not backed by a current check (status stays green
  after the API stops answering; mock email sends show as "sent"; a network
  outage on the PC is recorded as client-site downtime).

## The owner's ten requests (2026-10-19) and what 0.5.0 does

| # | Request | 0.5.0 |
|---|---|---|
| 1 | Seven individual agent cards | A row of 7 cards for the **real** seven agents (the reference's separate "Marketing" and "Postonomous" agents and missing Monitoring agent do not exist): agent colour and filled icon, short name, keywords built only from capabilities that exist, status (Active / Switched off / Not in this edition / Needs attention), current work from the new `active` counts ("2 running · 1 queued", or "Idle · last ran 5 min ago"), a bar that is the **24-hour success rate** (labelled as such; empty with "No runs in 24 h"), and *Open* to the agent's page. No invented progress percentages: tasks have no progress field. |
| 2 | Quick Actions | Sidebar group, operators and admins only: *Scan all websites* (`monitoring.check_all_sites`), *Run SEO audit* (`seo_content.audit_page`, asks for the page), *Check servers* (`server_management.check_services` for each enabled server with a pinned host key), *Generate report* (security report or uptime report, saved to Drafts & reports), *Open n8n* (admins; the new `n8n_editor_url`). All are read-only or draft-only, so they run without approval; each reports the real task it created and is disabled with the reason when it cannot run (agent off, no sites, no servers, already running). |
| 3 | Service-status panel with verified connections | Each row shows what was checked and when: n8n ("Running · checked 40 s ago" only if probed within ~3 min, else "Not checked since …"), PostgreSQL and API (from the last successful status poll), Worker (heartbeat), AI (Claude: last call OK / failed, from the new `llm` fields; rule-based otherwise), CWP servers ("No servers added" / "Verified 12 min ago" / "Not checked in 3 days"), Email and Postonomous ("Mock: recorded, not sent"). No "Docker running" row: no container has, or should have, the Docker socket. |
| 4 | Footer shows v0.2.0 | Done (e5c443a): the dashboard shows its own build version, a warning banner when the API reports a different one, `/version.json`; `smoke.ps1` checks both against the folder's VERSION; `status.ps1` prints the running API's version and the folder its container was created from. |
| 5 | CPU, RAM, disk gauges only with real host telemetry | **No gauges in 0.5.0.** A compact panel says "PC resources · Not connected: the Command Center runs inside Docker and cannot measure this PC". Options for the owner below. |
| 6 | Intern 2 panel, Not Connected until verified | Sidebar card "Intern 2 · Not connected" with a neutral device glyph (no product photo) and a grey dot. Its state comes from one function that returns *not connected* until a verified heartbeat exists, so no code path can show *Connected* without one. Intern 2 does not appear anywhere in the code: the owner needs to say what it is and how it should connect. |
| 7 | Density and hierarchy closer to the reference | New tokens (below), a fixed set of rows: title → stat row → agents → Workflow Activity / PC resources / Service Status → Recent Tasks / Active Workflows. The Websites, alerts and approvals panels move: site problems go to the Websites card and menu badge, alerts stay under the bell and in Automation Logs, and pending approvals get an amber strip under the title whenever any are waiting (never hidden). |
| 8 | Statistics tied to real backend data | All tiles and panels from the API; the honesty fixes in the audit table below; additive read-only endpoints (e6be4e0): task activity by category and local time bucket, today vs yesterday by the same clock time, per-agent work in progress, AI call health, n8n editor URL, uptime coverage. |
| 9 | Preserve database, volumes, agents, approval rules, APIs | No schema change for the dashboard; existing endpoints keep their shapes (fields only added). Approval changes in the audit table only make approvals **stricter** (more exact target, more re-checks). |
| 10 | No fake activity, uptime or devices | As above. The reference's sample numbers, service states, task rows and workflow names are not copied. |

## Visual differences and the 0.5.0 changes

**Colours** (sampled from the reference):

| Token | 0.4.0 | 0.5.0 |
|---|---|---|
| Page background | `#060a14` + blue/purple glows | `#081320`, flat |
| Header | translucent `#070c1b` with blur | solid `#091423`, 1 px `#162130` rule |
| Sidebar | blends into the page | `#0a1624`, 1 px `#1c2737` right border |
| Cards | `#0d1426`, coloured glows and top lines on tiles | `#0f1a2b`, border `#1f2a3c`, no glows |
| Text / secondary / muted | `#e8edf7` / `#b7c1d8` / `#8591ad` | `#f3f7ff` / `#ccd6e8` / `#9aa4bd` (all above 6:1) |
| Active menu item | translucent blue tint + side bar | solid blue `#0f3681 → #0a3fa8`, `#4295e2` edge |
| Execute | blue, inside the input | green, beside the input; `#00854f` instead of the reference's `#009b5b` (3.7:1, fails WCAG AA with white text) |
| Agent colours | none | one colour per real agent, used on cards, chart, table and icons (validated for colour-vision deficiency before use) |

**Type**: Segoe UI (Windows' own font, exact match on the owner's PC) first,
bundled Inter as the fallback elsewhere; title 30 px/700, section 22 px/700,
panel titles 20 px/700, values 26 px/600, body 13.5 px; no uppercase
micro-labels on the dashboard; numbers in tabular figures.

**Shape and spacing**: radius 8 px (6 px for menu items and buttons, 5 px for
table status pills); header 70 px; sidebar 208 px; stat cards about 100 px
high with 66 px round icon badges; denser panels.

**Header**: line-art infinity logo with "Infinite AI / COMMAND CENTER";
command input about 550 px with a search icon and "Ask or give a command…";
Ctrl/Cmd+K stays (mentioned in the tooltip, no visible chip); gear then bell;
round avatar with name and role. The bell badge stays because pending
approvals must stay visible.

**Sidebar**: the reference's twelve items with its icons; *Social Media /
Postonomous* shown as "Postonomous"; the operations pages stay reachable
(Approvals, Tasks, AI Supervisor and Activity log always visible; Leads,
Client support, Drafts & reports, Outbox and Integrations under a "More"
toggle that keeps any badge visible); Quick Actions; the Intern 2 card; the
system footer with versions.

**Phone and tablet**: the same style; stat cards in two columns; agent cards
as a horizontal, swipeable row; chart legend below the chart; Quick Actions
and the Intern 2 card in the menu drawer.

**Other pages** pick up the new tokens automatically; the dashboard-only
components use their own classes so other pages do not shift.

## Element by element: where each value comes from

| Reference element | Real source | Honest state when missing |
|---|---|---|
| AI Agents "7 Active" | `/api/agents` enabled ∧ entitled | "6 of 7 active" (amber) when one is off; "—" while loading |
| Websites "12 Monitored" | `/api/sites` enabled sites | "1 down" / "2 degraded" when there are problems; "0 · Add a website" |
| Tasks Today "48 ↑ 23 %" | `/api/stats/task-activity` today vs yesterday by the same time (supervisor bookkeeping excluded) | "No comparison yet" when yesterday had fewer than 5; never an invented percentage |
| "99.9 % System Uptime, last 30 days" | **Website** uptime: check-weighted 30-day average of enabled sites, with coverage (`first_check_at`) | Labelled "Website uptime"; "since 12 Oct" on a young install; "Not enough data yet" with very few checks |
| Date, time, "System Online" | Browser clock; status poll succeeded within ~30 s, database answered, a worker alive | "Worker not reporting" (amber); "Cannot reach the Command Center · last update 10:21" (red) |
| Agent cards | `/api/agents` (+ `active`) | see request 1 |
| Workflow Activity | `/api/stats/task-activity` (range 24h, 7d or 30d, in the browser's time zone; one grouped query) | Flat lines with "No tasks finished in the last 24 hours" |
| System Resources | **None for the PC** | "PC resources · Not connected" |
| Service Status | see request 3 | per row |
| Recent Tasks | `/api/tasks` (latest, supervisor bookkeeping hidden); duration = finished − started, live while running | "No tasks yet" |
| Active Workflows | `/api/schedules` (operators can pause/resume: an internal, audited change, not an external action), then n8n workflows read-only when `N8N_API_KEY` is set | "Add N8N_API_KEY to list n8n workflows"; never placeholder rows. The reference's "Auto Post to WordPress", "Email Follow-up" and "Postonomous Distribution" would be external actions that need approval each time, so they can never be automatic toggles |
| Quick Actions | capabilities above | disabled with the reason |
| Intern 2 | **none** | "Not connected" |

## Owner decisions needed

1. **PC resources** (CPU, RAM, disk): containers can only see Docker's WSL 2
   virtual machine (about half the PC's memory, Docker's own virtual disk),
   never the Windows CPU model or the C: drive. Options: (a) leave the panel
   as "Not connected"; (b) show the Docker VM's figures, labelled as such;
   (c) a small Windows-side collector (for example a scheduled PowerShell task
   reading Win32_Processor, Win32_OperatingSystem and Win32_LogicalDisk) that
   sends readings to a new endpoint with its own token: a new component and
   write path that needs approval; (d) the latest overview of a chosen CWP
   server, labelled with its name and time.
2. **Intern 2**: what is it (a PC, a phone, a robot or speaker device, a
   second Command Center?), how should it connect, and what should it do? A
   real connection needs device pairing with its own token and a heartbeat.
3. **System uptime**: (a) keep "Website uptime" (0.5.0 default); (b) record
   the Command Center's own availability from now on ("collecting since …"
   until 30 days exist); (c) show "running since …".
4. **Ad report sample data**: keep the clearly labelled sample report, or
   replace it with "no ad account connected" until a real read-only ad API is
   connected.
5. **Approval policy details** (C3-11 to C3-14 below): whether ad-hoc checks of
   unregistered domains are in scope; an expiry for approvals (for example 24
   hours); self-approval by the requester (reasonable for a one-person
   business; please confirm); whether configured outbound flows (events to
   n8n, request text to Claude) count as standing approval.
6. **Display name**: set your account's display name to "MyTec" on the
   Settings page if you want the header to read like the reference; it is not
   hard-coded.

## Audit findings and when they are fixed

Approvals (C3) and real-data (C2) checks that found a gap or need a decision.
"0.5.0" means fixed in the next release, with tests.

| Id | Status | Finding | Fixed in |
|---|---|---|---|
| C3-01 | gap | A wrongly linked site key makes a real SSH repair change a different WordPress install than the one the approval names | 0.5.0 |
| C3-02 | gap | Email approvals do not name the recipient or the draft, and the draft can be swapped (or not yet exist) when the approval is given | 0.5.0 |
| C3-03 | gap | WordPress and social publish approvals show bare ids: no site name or URL, no production confirmation, no binding | 0.5.0 |
| C3-04 | gap | Switching an agent off does not stop its pending or already-approved changes | 0.5.0 |
| C3-05 | gap | Re-pinning a host key takes one click with nothing enforced, drops the 'changed key' signal, and does not invalidate approvals already given | 0.5.0 |
| C3-06 | gap | Production can be demoted to staging or test with one dropdown change, which removes the admin-and-typed-name requirement for new requests | 0.5.0 |
| C3-07 | gap | Repair approvals do not record the exact value or version the change will apply (main cases: plugin update target version, restored URL) | 0.5.0 |
| C3-08 | gap | Approval cards do not show who or what asked for the action (owner, AI planner from an n8n request, schedule, retry) | 0.5.0 |
| C3-09 | gap | The ad budget approval names only a campaign id and an amount (mock) | before a live ads adapter |
| C3-10 | gap | The dispute-letter mailing approval names only a letter id (mock) | before a live mailing adapter |
| C3-11 | needs-owner-decision | On-demand read-only checks can target any public URL or host, not only registered sites and servers, including via n8n | owner decision |
| C3-12 | needs-owner-decision | Approvals never expire, and an approved change can run long after it was approved | owner decision |
| C3-13 | needs-owner-decision | The requester can approve their own change, and staging or test changes need only one operator click | owner decision |
| C3-14 | needs-owner-decision | Automatic outbound data flows: event forwarding to a webhook or n8n, and site and request text sent to the Anthropic API | owner decision |
| C2-01 | gap | Mock email sends set leads to 'proposal_sent' and tickets to 'replied', shown in green as delivered | 0.5.0 |
| C2-02 | gap | A mock certified-mail run stamps the letter mailed and marks its items 'disputed' | 0.5.0 |
| C2-03 | gap | After a failed poll, the last good status stays on screen as if current ('All systems operational', 'Database connected') | 0.5.0 |
| C2-04 | gap | n8n or webhook shows 'connected' from any past success, with no staleness limit | 0.5.0 |
| C2-05 | gap | 'All systems operational' covers 4 internal services only, counts 'not checked' as fine, and appears while a site is down | 0.5.0 |
| C2-06 | gap | 'Active AI agents 6 of 7' and green dots mean only 'switched on', even for agents that have never run | 0.5.0 |
| C2-07 | gap | 'Website uptime (24 h)' shows a percentage from however few checks exist | 0.5.0 |
| C2-08 | gap | Some tiles take a green (good) colour when there is no data or nothing has loaded | 0.5.0 |
| C2-09 | gap | Website summaries hide unchecked sites and mix paused sites into counts and averages | 0.5.0 |
| C2-10 | gap | 'Open issues' and the Issues column use only the latest check, so nightly security findings disappear within 15 minutes | 0.5.0 |
| C2-11 | gap | The 'Emails sent' and 'WordPress publishes' tiles count mock-recorded actions under sent or published labels (hints disclose mock) | 0.5.0 |
| C2-12 | gap | Synthetic ad figures are labelled in the report and task summary, but not in the individual findings or the AI summary instructions | 0.5.0 |
| C2-13 | gap | Missing values become 0 ('0 running · 0 queued'; server overview 'up 0 day(s), load 0') | 0.5.0 |
| C2-14 | gap | Some counts are limited to a 24 h or newest-N window without saying so | 0.5.0 |
| C2-15 | gap | Fixed schedule and time-zone statements on live pages can contradict the real settings | 0.5.0 |
| C2-17 | needs-owner-decision | The reference's 'System Uptime 99.9%' tile has no data source | owner decision |
| C2-18 | needs-owner-decision | The reference's System Resources panel has no collector, and in-container metrics would not describe the Windows PC | owner decision |
| C2-19 | needs-owner-decision | The reference's Service Status rows and Intern 2 card show states the backend cannot verify, or that would be false | owner decision |
| C2-20 | needs-owner-decision | Other reference elements must be redefined against real data before they are built | owner decision |
| C2-21 | gap | 'Tasks completed today' and the 24 h chart count the AI Supervisor's own planning and summary bookkeeping as completed tasks | 0.5.0 |
| C2-22 | gap | A network outage on the owner's PC is recorded as client-site downtime, and time the PC is asleep or off silently drops out of the uptime window | 0.5.0 |

Checks that passed:

| Id | Finding |
|---|---|
| C3-16 | Every change, external and financial capability (16 in total) is held for human approval by the task layer, not by the agent or planner |
| C3-17 | Repair and restart approvals name the action and resolved target, pin it, and re-check it three times; production needs an admin and the typed name |
| C3-18 | Staging and test repairs and server changes also always need approval |
| C3-19 | There are no automatic re-runs of approved changes; a manual retry needs a new approval |
| C3-20 | Schedules run only read and draft work, and anything consequential would still wait for approval |
| C3-21 | The service token cannot approve, run capabilities directly, or change sites or servers |
| C3-22 | The AI Supervisor's plans can only create tasks, and the task layer decides on approval |
| C3-23 | Automatic monitoring touches only registered sites over GET/HEAD; SSH reads run only on request, on configured servers, with allowlisted read operations. 0.5.0 adds one exception (C2-22): when a site gives no answer at all, a GET to api.wordpress.org, www.cloudflare.com and www.google.com tells an offline PC from a site that is down (no site data sent; the owner may prefer api.wordpress.org alone) |
| C3-24 | Admin-only direct actions are human, audited, and send nothing to websites or servers (confirmation is uneven; see C3-01, C3-05, C3-06) |
| C2-16 | No seed or demo data; most figures come from live API data and show their limits |

## Not copied from the reference, on purpose

- The sample figures, service states, task rows and workflow names.
- A "Docker running" row (no container should hold the Docker socket).
- The device photo on the Intern 2 card (it would imply a paired device).
- The 3.7:1 green of the Execute button (replaced by an accessible green).
- Agents that do not exist (a separate Marketing and Postonomous agent) and
  capabilities that do not exist ("Backups", "Inbox Management",
  "Campaigns", "Autonomous Posting").
