# Research Gateway: the 0.7.0-dev development build

**Status (2026-10-20): built and tested in the cloud sandbox only. It is not installed on the owner's PC, and it
must not be yet.** `VERSION` says `0.7.0-dev`; `scripts\install.ps1` refuses a `-dev` folder ("a development build,
not for installation") unless the sandbox's tests set `CC_ALLOW_DEV_BUILD=1`, and `scripts/build-package.py` refuses
to put a `-dev` build into `releases/`. No release zip exists. Web research is **off by default** and stays off after
an upgrade until an admin switches it on.

The design is [RESEARCH_GATEWAY_PLAN.md](RESEARCH_GATEWAY_PLAN.md) (section numbers below refer to it). The owner
approved the SearXNG-first direction on 2026-10-20, with DeepSeek, page fetching, paid search APIs and recurring
searches excluded.

## 1. What this build contains

| Part | What it does |
|---|---|
| Research Gateway (`backend/app/research/`, runs in the worker) | The only code that searches the web for the Command Center. Every search goes through the same steps, and any step can refuse: mode, edition and agent policy; the trigger read from the database; canonical form and the outbound screen; cache; budget and rate slot; the provider; cleaning and citations; the logs (plan 1.2). |
| Providers | `mock` (a fixed fake answer; nothing leaves the PC) and `searxng` (the private container below). `fetch()` always refuses with `not_supported`. |
| Outbound screen | Refuses secrets, e-mail addresses, phone numbers (with any punctuation between the digits, tens words such as "ninety nine", or the letters O and l for 0 and 1), long number runs, leads' and tickets' names, domains and phone numbers, Credit Repair clients (a match-only HMAC index in the Credit Repair database, rebuilt at every start and refusing every search while it was made with another key or after a failed rebuild), anything shaped like a Credit Repair reference ("CR-0001"), private JARVIS text (6-word phrases, also when joined by punctuation), internal host names, client names, IP addresses and search syntax. A person's name is matched by any two of its words wherever they are in the query, with or without an apostrophe ("O'Neil", "ONeil"). A refusal stores a reason code only (plus a lead or ticket ID, shown only to people with the sensitive-data permission), never the text or a hash of it. A name in the leads, tickets or Credit Repair index is refused with one code, `personal_record`, at the same step and in the same words. What a refusal can still tell is in section 4. |
| Agent capabilities (risk READ, no approval) | `supervisor.web_search`, `seo_content.research_topic`, `social_marketing.trend_research`, `wordpress_security.check_advisories` and `server_management.check_advisories` (a software name and version a person types), and the admin-only `supervisor.research_connectivity_test`. Each returns a fixed summary line and a separate list of citations. Credit Repair, Monitoring and Communications cannot search. |
| Limits | 50 searches a day in all, 30 per person, per agent 20 (Supervisor), 20 (SEO & Content), 10 (Social & Marketing), 10 (WordPress Security), 5 (Server Management); at most 1 search every 3 seconds, bursts of 3. A cap of 0 refuses. |
| Research Center (dashboard, admins and operators) | New research, history with citations, sources, usage, provider status, blocked attempts, and (admins) the mode, caps, retention, agent permissions, the connectivity test and purge. Viewers do not see it, and every `/api/research/*` endpoint answers 403 to viewers and to the n8n service token. |
| Search service (optional overlay) | `docker-compose.research.yml` with the read-only folder `research/searxng/` (`settings.yml`, `limiter.toml`): the official SearXNG image, pinned by tag and digest (`2026.10.4-d48c4b555@sha256:76b0bf28...c072`), engines DuckDuckGo, Brave and Wikipedia, JSON only, no published port. |
| Scripts | `scripts\research.ps1` (`-Status`, `-Enable`, `-Disable`, `-RotateSecret`) and the helper `scripts\compose-env.ps1`; changes to `init-env.ps1/.sh`, `preflight.ps1`, `install.ps1`, `start.ps1`, `stop.ps1`, `status.ps1`, `smoke.ps1`, `backup.ps1`, `restore.ps1`, `uninstall.ps1`, `brain-common.ps1` and `brain-test.ps1`. |
| Database | Migration `0008_research.sql` (new tables only, no foreign keys into older tables; 0007 is kept for the 0.6.1 patch) and `0002_research_index.sql` in the Credit Repair database. |

**Not in this build:** DeepSeek (no provider, no key field, no setting that could turn it on), page fetching, paid
search APIs, research from schedules, n8n or the service token, AI summaries of web text, and promotion of web
results to the JARVIS Brain.

## 2. How it is switched on (later, after the owner's approval)

There are two separate switches. Neither is on after an install or upgrade.

1. **The search container** (on the PC):
   `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\research.ps1 -Enable`.
   It adds `docker-compose.research.yml` to `COMPOSE_FILE` in `.env` (entries separated by `;`; another entry such
   as the remote-access tunnel stays), after copying `.env` to `.env.before-research-<time>`. It then runs
   `start.ps1 -NoBuild`: the worker is recreated once to join the private `research` network, and PostgreSQL, n8n,
   the API and the dashboard keep running. It refuses while the JARVIS Brain test copy or a JARVIS import is running,
   without `SEARXNG_SECRET` (`init-env.ps1 -AddMissing` adds it; `install.ps1` does that on every upgrade), and when
   Compose does not accept the new file list. `-Status` shows the state; `-Disable` undoes everything.
2. **The research mode** (in the dashboard, admins only): Research Center, Policy and settings.
   `off` (the default), `mock` (a fake answer, for trying the screens; nothing leaves the PC) or `live`
   (the private SearXNG). Live needs the typed confirmation `ENABLE LIVE RESEARCH`. Switching back to off takes
   effect at once: the mode is read before every search, and the worker stops probing SearXNG.

Suggested order: install 0.7.0 (research off), try mock, run `research.ps1 -Enable`, and switch to live only after
the owner approves the real connectivity test of plan section 7.7, which has not been run.

## 3. Every outbound data flow

| From | To | What is sent | When |
|---|---|---|---|
| Worker | SearXNG, `http://searxng:8080` on the private `research` network | `POST /search`: the screened query in canonical form, `format=json`, the engine names, language, time range, page; header `X-Real-IP: 127.0.0.1`. `GET /healthz` every 60 s and `GET /config` every hour. | Live mode only. The address is fixed: `check-config` and the gateway refuse any other `SEARXNG_URL`. |
| SearXNG | DuckDuckGo, Brave, Wikipedia (HTTPS, through `research_egress` and the PC's internet connection) | The query text, the language and moderate safe search. Each engine sees the PC's public IP address. No user, client or Command Center identity, no cookies from the Command Center. | For each live search that was not answered from the cache. SearXNG at the pinned tag sends engine requests over HTTPS only. |
| Docker | The image registry (Docker Hub, or `REGISTRY`) | An image download | Once, by `start.ps1`, when the pinned image is not on the PC yet. |
| The person's browser | A cited page | Nothing, until a person clicks a citation; links open with `rel="noopener noreferrer nofollow"`. | On a click. |

Nothing else leaves the PC because of research: the API is not on the `research` network; DeepSeek and page fetching
do not exist in this build; web text never reaches an AI prompt (the AI sees only the fixed summary line), a finding,
an event, n8n, an approval, a schedule or a task creation. In mock mode nothing leaves at all.

**Kept on the PC:** the query log (the canonical text of searches that were sent; blocked attempts as a code only),
results and sources (30 days), the cache, daily usage totals (2 years), audit entries (IDs, codes and counts only).
SearXNG writes no request lines to its container log (`GRANIAN_LOG_ACCESS_ENABLED=false`); the canary checks found no
search text in it. A query the gateway refuses when its task runs (a lead, a client or archive text added after it was
queued) loses its text from the task's parameters and from the stored plan of the Supervisor request behind it.

The API's and the dashboard's access logs record each request's path without its query string, for every route: the
text of a JARVIS Brain search (`GET /api/brain/search?q=...`), which 0.6.x logged, no longer reaches them. nginx still
quotes the whole request in an error line, for example while the API is unreachable.

## 4. Fail-closed rules (each has a test)

- Research is off by default; mock and live need an admin; live needs the typed confirmation.
- Any error in screening, policy, budget, cache or store refuses the query, and the provider is not called; at intake
  (a direct run, a retry, a planned Supervisor step) no task or stored plan holds it.
- A failed rebuild of the Credit Repair match index at start (any error but a changed key) never stops the start:
  the class is logged and every search is refused (`credit_repair_index_incomplete`) until a start rebuilds it.
- A missing or unreadable agent policy refuses; `credit_repair` is refused in code whatever its policy row says.
- Unreadable research settings refuse every search; so does an edition without `web_research`.
- A task that carries an approval, or whose trigger (read from the database) is not a dashboard run, a Supervisor
  request typed on the dashboard, a person's retry or the admin's connectivity test, is refused. Creating a schedule
  for a research capability is rejected.
- Budget and rate slots are reserved before the call, atomically, in their own transaction; a crash frees a slot after
  10 minutes; a cap of 0 refuses.
- SearXNG unreachable, misconfigured (403), redirecting, oversized (over 2 MB) or answering malformed JSON gives a
  status code, nothing is cached, and nothing is retried beyond one retry on a connection error or a 500.
- The SearXNG container: no published port, user 977, read-only root file system and configuration folder, every
  capability dropped, `no-new-privileges`, 512 MB / 1 CPU / 256 processes, JSON only (the HTML page answers 403),
  limiter, public mode, image proxy, autocomplete and favicons off and locked against request fields; only the image
  proxy route remains, guarded by an HMAC with `SEARXNG_SECRET`.
- A missing or short `SEARXNG_SECRET` never stops the core: every script leaves the research file out for its own
  process (warning printed, `.env` unchanged), and Compose itself refuses to start SearXNG with an empty secret.
  While it is missing, the worker runs without the research network (it is recreated once) and an already running
  SearXNG container is left as it is; `init-env.ps1 -AddMissing` (or `install.ps1`) adds a new secret, and the next
  `start.ps1` recreates SearXNG with it.

### What a refusal can still tell

Every signed-in user can read the leads and the support tickets. So when a name is refused as `personal_record` and
it is in neither, the person who typed it learns that it is in the Credit Repair records. Nothing else in the answer
differs: the message, the step, the code and the work done are the same for a lead's, a ticket's and a client's name,
and the record ID behind a refusal is shown only to people with the sensitive-data permission (who may see the
clients anyway). That one answer cannot be hidden without refusing names nobody has stored, so it is limited and
visible: after 5 `personal_record` refusals in one day (UTC), nothing more of that person's is screened that day
(`too_many_blocked`, at intake and when a queued task runs), whatever their daily search cap, and the first time each
day an audit entry `research.personal_record_limit` names the person and the count. Apart from that, a person's
refusals per day are limited to their daily search cap.

Names are matched as stored: a lead's two name words anywhere in a query refuse it, so a lead with a common name, or
one planted through the public lead form, refuses more searches (it fails closed, and the Blocked list shows the lead to
people with the sensitive-data permission).

## 5. Operating it

- `status.ps1` shows the SearXNG container's state and health (no sign-in needed); `smoke.ps1` reports web research
  for information (off, mock, or the search service's state) and fails only if the research API does not answer.
- `start.ps1` starts every other service first and SearXNG last, on its own. If SearXNG cannot start (the image is
  missing and the registry cannot be reached, `research\searxng\settings.yml` is missing), it prints a warning and
  everything else runs; web research then reports the search service as unreachable.
- `backup.ps1` and `restore.ps1` cover the research tables with the rest of the database (no change in how they work).
- `research.ps1 -RotateSecret` replaces `SEARXNG_SECRET` in one write (after a copy of `.env`) and recreates only
  the SearXNG container. Do not delete the line by hand while the search service is switched on.
- `uninstall.ps1` also removes the SearXNG container, the two research networks and the cache volume (a cache, no
  data), even when `COMPOSE_FILE` no longer lists the research file; it keeps the SearXNG image and prints the command
  to remove it.
- A new SearXNG tag is pinned only after `testing/research-check.sh` passes on it in the sandbox (plan 2.6), and
  ships with a release; a settings change by hand on the PC would be lost at the next upgrade.

## 6. Rollback

1. **Switch research off** (seconds; keeps everything): Research Center, mode `off`. Searches stop at once; the
   worker stops probing SearXNG.
2. **Remove the search container**: `research.ps1 -Disable`. It removes the SearXNG container, takes
   `docker-compose.research.yml` out of `COMPOSE_FILE` (another entry stays), runs `start.ps1 -NoBuild` (the worker
   is recreated without the research network; nothing else restarts), then removes the cache volume and the two
   networks. The image (about 383 MB) is kept; the script prints the command that removes it.
3. **Going back to 0.6.x**: go back to the release you came from, normally **0.6.1**. Run step 2 first (otherwise
   `.env` still names a file the 0.6.x folder does not have, and every `docker compose` command there fails), take a
   backup, and install the 0.6.1 folder as its release notes describe. 0.6.0 also starts on this schema (0.6.0 works
   with `0007_brain_withhold`), but it brings back the withhold defect and the points in `RELEASE_NOTES_0.6.1.md`
   section 4, so prefer 0.6.1. Checked in the sandbox: the packaged 0.6.1 backend starts on a database this build
   migrated (0001-0008) and used, signs in, runs tasks and ignores the research tables (its migration runner applies
   only the files it has, and nothing refers to the new tables). Two things to know first:
   - a research task still queued is failed by 0.6.x as an unknown capability (nothing is searched);
   - 0.6.x shows every task's parameters and results to viewers, research tasks included, and every Supervisor
     request's message, reply and summary. Purging research records in the Research Center first ("PURGE RESEARCH")
     clears research tasks' queries and citations and the query and rationale of web-search steps in stored
     Supervisor plans. It does not clear a Supervisor request's message, reply or summary: a search the Supervisor
     ran was asked for in the operator's own words, so viewers under 0.6.x still read those requests.
4. **Purging**: the admin's purge (typed `PURGE RESEARCH`) removes queries, results, sources, the cache, research
   tasks' parameters and citations, and the query and rationale of web-search steps in stored Supervisor plans (the
   request's plan and its planning task's result), older than the chosen age; the worker does the same at the end of
   the log retention (90 days). Supervisor requests keep their message, reply and summary, as every request does. It
   does not clear container logs or earlier backups; recreate the SearXNG container to clear its (query-free) log.

## 7. What the sandbox checked, and what it could not

All on 2026-10-20, in the sandbox's own Compose projects, with every other container left as it was.

- **Development instance** (plan 7.4, `testing/research-check.sh`, 35 checks passed): the real overlay and settings;
  `docker compose` refuses to run without `SEARXNG_SECRET`; healthy; no published port; user 977; read-only root and
  configuration; JSON 200 and HTML 403; `/config` shows exactly the three engines and the pinned version; no internet
  here, so every engine is unresponsive and the backend's own provider reports `provider_down`; `unreachable` while
  stopped; healthy again within 6 s of a start or restart; back by itself after its main process is killed; the
  image's default `::` address fails without IPv6 (the reason for `GRANIAN_HOST`); no search text in its log. A
  test-only JSON engine on a mock upstream showed real result parsing, the safe-search lock, the image and favicon
  proxies fetching nothing for a caller, and a burst of 200 searches (87 MiB and 15 processes at peak).
- **Full stack** (plan 7.5): `install.ps1` of 0.7.0-dev (refused without `CC_ALLOW_DEV_BUILD=1`); `research.ps1
  -Enable`, `-RotateSecret` and `-Disable` (PostgreSQL, n8n, the API and the dashboard kept their containers; only
  the worker was recreated); the layout (SearXNG on `research` and `research_egress` only, the worker on `internal`,
  `egress` and `research`, nothing else on `research`, no new port; also merged with the tunnel overlay); SearXNG
  could not reach PostgreSQL, the API, n8n or the dashboard by name or address; viewers and the n8n service token got
  403; with research off or in mock mode a packet capture on SearXNG's interface saw no connection; in live mode the
  runs reached it and ended `provider_down`; the restart recovery; the start-up fallbacks (image and registry
  missing, `settings.yml` missing, `SEARXNG_SECRET` missing) with `status`, `smoke`, `backup`, `restore`, `stop` and
  `install` still working; `.env` edits on files with mixed line endings, a BOM, quotes and a tunnel entry;
  `uninstall.ps1` with the research file no longer listed.
- **Rollback** (plan 8): the 0.6.1 backend from the released package
  (`releases/InfiniteAI-CommandCenter-0.6.1-windows.zip`, SHA-256 checked), run from source in the sandbox, on a
  database this build migrated (0001-0008) and used (research in mock, a queued research task, a blocked-attempt row,
  the Supervisor's cap set to 3, a Credit Repair client): it applied no migration, started, signed in, ran a Supervisor
  request, failed the queued research task as an unknown capability and left the research tables and policy rows as
  they were; going forward again applied nothing and rebuilt the Credit Repair match index. The earlier check with the
  released 0.6.0 backend was made before the 0.6.1 merge, on a database without `0007_brain_withhold`.
- **Access logs**: the dashboard's nginx image with this build's `nginx.conf`, in a throwaway container: the
  configuration test passes, and the request lines of `/?q=...`, `/api/brain/search?q=...` and `/assets/...?v=...`
  carry no query string; the error line for the unreachable API still quotes it.
- **Not verified:** real search results, blocking by the search engines and CAPTCHAs (the sandbox has no internet);
  Windows PowerShell 5.1 itself (the scripts are checked for 5.1-only syntax and run under PowerShell 7.4); Docker
  Desktop's bind-mount permissions and IPv6 on the owner's PC; the JARVIS Brain test copy was not started (its seed
  statements and the fingerprint change were checked on their own).
