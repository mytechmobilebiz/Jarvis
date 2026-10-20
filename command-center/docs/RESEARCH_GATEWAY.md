# Research Gateway: web research in 0.7.0

**Status (2026-10-20): release 0.7.0 includes web research, switched off by default. It is not installed on the
owner's PC, and it is NO-GO for installation and for any JARVIS import** (below). Installing or upgrading to 0.7.0
leaves research off: `.env` is not changed, nothing of SearXNG is downloaded or started, and no search can leave the
PC. Switching it on later is a series of separate steps (section 2), each one the owner's decision; `research.ps1
-Enable` and the real connectivity test each need the owner's approval. Development builds (`X.Y.Z-dev`) are refused
by `scripts\install.ps1`, and `scripts/build-package.py` will not write one into `releases/`.

> **Security notice, 2026-10-20: 0.7.0 is NO-GO, for installation and for any JARVIS import.** 0.7.0 is built on
> 0.6.1, which the security notice of the same day declared NO-GO, and carries its JARVIS importer unchanged: the
> text of a JARVIS message that an admin withheld comes back when a later JARVIS backup is imported (compaction
> copies of a message have other identifiers, and only the withheld identifier is withheld), and operators, admins,
> the Supervisor's history and recall and newly promoted memory items can then read it. Web research does not change
> this. Stay on 0.5.0, and do not install 0.6.0 or 0.6.1 on the way: web research will be offered for installation
> only in a 0.7 release that also contains the corrected 0.6.2's fix, tested on its own upgrades from 0.5.0 and
> from 0.6.2. `docs/RELEASE_NOTES_0.7.0.md` has the full notice.

The design is [RESEARCH_GATEWAY_PLAN.md](RESEARCH_GATEWAY_PLAN.md) (section numbers below refer to it). The owner
approved the SearXNG-first direction on 2026-10-20, with DeepSeek, page fetching, paid search APIs and recurring
searches excluded; the defaults built for the decisions still open are in section 5.

## 1. What 0.7.0 contains

| Part | What it does |
|---|---|
| Research Gateway (`backend/app/research/`, runs in the worker) | The only code that searches the web for the Command Center. Every search goes through the same steps, and any step can refuse: mode, edition and agent policy; the trigger read from the database; canonical form and the outbound screen; cache; budget and rate slot; the provider; cleaning and citations; the logs (plan 1.2). |
| Providers | `mock` (a fixed fake answer; nothing leaves the PC) and `searxng` (the private container below). `fetch()` always refuses with `not_supported`. |
| Outbound screen | Refuses secrets, e-mail addresses, phone numbers (with any punctuation between the digits, tens words such as "ninety nine", or the letters O and l for 0 and 1), long number runs, leads' and tickets' names, domains and phone numbers, Credit Repair clients (a match-only HMAC index in the Credit Repair database, rebuilt at every start and refusing every search while it was made with another key or version or after a failed rebuild), anything shaped like a Credit Repair reference ("CR-0001"), private JARVIS text (6-word phrases, also when joined by punctuation), internal host names, client names, IP addresses and search syntax. A person's name is matched by any two of its words wherever they are in the query, with or without an apostrophe ("O'Neil", "ONeil"). A refusal stores a reason code only (plus a lead or ticket ID, shown only to people with the sensitive-data permission), never the text or a hash of it. A name in the leads, tickets or Credit Repair index is refused with one code, `personal_record`, at the same step and in the same words. What a refusal can still tell is in section 4. |
| Agent capabilities (risk READ, no approval) | `supervisor.web_search`, `seo_content.research_topic`, `social_marketing.trend_research`, `wordpress_security.check_advisories` and `server_management.check_advisories` (a software name and version a person types), and the admin-only `supervisor.research_connectivity_test`. Each returns a fixed summary line and a separate list of citations. Credit Repair, Monitoring and Communications cannot search. |
| Limits | 50 searches a day in all, 30 per person, per agent 20 (Supervisor), 20 (SEO & Content), 10 (Social & Marketing), 10 (WordPress Security), 5 (Server Management); at most 1 search every 3 seconds, bursts of 3. A cap of 0 refuses. |
| Research Center (dashboard, admins and operators) | New research, history with citations, sources, usage, provider status, blocked attempts, and (admins) the mode, caps, retention, agent permissions, the connectivity test and purge. Viewers do not see it, and every `/api/research/*` endpoint answers 403 to viewers and to the n8n service token. |
| Search service (optional overlay) | `docker-compose.research.yml` with the read-only folder `research/searxng/` (`settings.yml`, `limiter.toml`): the official SearXNG image, pinned by tag and digest (`2026.10.4-d48c4b555@sha256:76b0bf28...c072`), engines DuckDuckGo, Brave and Wikipedia, JSON only, no published port. Not used until `research.ps1 -Enable`. |
| Scripts | `scripts\research.ps1` (`-Status`, `-Enable`, `-Disable`, `-RotateSecret`) and the helper `scripts\compose-env.ps1`; changes to `init-env.ps1/.sh` (a new `.env` gets `SEARXNG_SECRET`; an existing one is never given it), `preflight.ps1`, `install.ps1`, `start.ps1`, `stop.ps1`, `status.ps1`, `smoke.ps1`, `backup.ps1`, `restore.ps1`, `uninstall.ps1`, `brain-common.ps1` and `brain-test.ps1`. |
| Database | Migration `0008_research.sql` (new tables only, no foreign keys into older tables; it changes no existing row) and `0002_research_index.sql` in the Credit Repair database. |

**Not in 0.7.0:** DeepSeek (no provider, no key field, no setting that could turn it on), page fetching, paid search
APIs, research from schedules, n8n or the service token, AI summaries of web text, and promotion of web results to
the JARVIS Brain.

**Running even while research is off.** At every start, the API and the worker rebuild the Credit Repair match index:
they decrypt each Credit Repair client's name, e-mail address and phone number inside the Command Center and store
only HMAC values in the Credit Repair database. Nothing leaves the PC. Each rebuild adds a
`system:research-index research_index_rebuild` entry (with the number of clients) to the Credit Repair access log, so
the log shows two such entries per start (API and worker) once a client exists.

## 2. How it is switched on (later, step by step)

Neither switch is on after an install or upgrade. Each step below is a separate decision of the owner; stop after any
of them.

0. **Install or upgrade to 0.7.0** (research off). `.env` stays byte for byte as it was; the two migrations add the
   research tables (empty except the 8 default `research_policy` rows) and the Credit Repair match index tables
   (filled at every start, section 1); `status.ps1` shows "Web research search service (SearXNG): switched off"; `smoke.ps1` reports
   `web research (information only)  off`.
1. **Try the screens without the internet: mock mode** (dashboard, admins only): Research Center, Policy and settings,
   mode `mock`. Searches get a fixed fake answer; nothing leaves the PC, and no container is added. The blocked
   attempts, the limits and the citations can be tried here.
2. **Add the search container on the PC** (needs the owner's approval):
   `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\research.ps1 -Enable`.
   - It refuses while the JARVIS Brain test copy or a JARVIS import is running, when `COMPOSE_FILE` is set more than
     once, when `.env` sets `SEARXNG_SECRET` more than once, when `SEARXNG_SECRET` is shorter than 32 characters
     (give it a new value with `-RotateSecret` first), and when Compose does not accept the new file list. A refusal
     changes nothing (exit code 2).
   - It copies `.env` to `.env.before-research-<time>`, then makes one write: `docker-compose.research.yml` joins
     `COMPOSE_FILE` (entries separated by `;`; another entry such as the remote-access tunnel stays) and, on an
     upgraded `.env`, `SEARXNG_SECRET` is created in the same write (32 random bytes, as `init-env.ps1` makes it for
     a new `.env`; never printed; no stored data depends on it).
   - It then runs `start.ps1 -NoBuild`: the SearXNG image is downloaded once from the registry (about 100 MB, 383 MB
     on disk), the worker is recreated once to join the private `research` network, and PostgreSQL, n8n, the API and
     the dashboard keep running. `-Status` shows the state; `-Disable` undoes everything (section 7).
   - Agents still cannot search: the mode is unchanged.
3. **The real connectivity test** (plan 7.7; needs the owner's approval; not run yet): an admin switches the mode to
   `live` (typed confirmation `ENABLE LIVE RESEARCH`), runs the connectivity test in the Research Center (one fixed
   general search, "Wikipedia", counted and logged like any other), checks which engines answered, and switches back
   to `mock` or `off` unless the owner decides to keep live.
4. **Live research for the agents** (the owner's decision after step 3): mode `live`. Admins and operators can then
   search from the Research Center, from an agent's Run and from Supervisor requests typed on the dashboard, within
   the limits of section 1 and the defaults of section 5.

Switching back to off takes effect at once: the mode is read before every search, and the worker stops probing
SearXNG.

## 3. Every outbound data flow

With research off (the state after install or upgrade), and in mock mode, research sends nothing anywhere, apart
from the one image download that `research.ps1 -Enable` (section 2, step 2) starts while the mode is still off: the
Docker row below.

| From | To | What is sent | When |
|---|---|---|---|
| Worker | SearXNG, `http://searxng:8080` on the private `research` network | `POST /search`: the screened query in canonical form, `format=json`, the engine names, language, time range, page; header `X-Real-IP: 127.0.0.1`. `GET /healthz` every 60 s and `GET /config` every hour. | Live mode only. The address is fixed: `check-config` and the gateway refuse any other `SEARXNG_URL`. |
| SearXNG | DuckDuckGo, Brave, Wikipedia (HTTPS, through `research_egress` and the PC's internet connection) | The query text, the language and moderate safe search. Each engine sees the PC's public IP address. No user, client or Command Center identity, no cookies from the Command Center. | For each live search that was not answered from the cache. SearXNG at the pinned tag sends engine requests over HTTPS only. |
| Docker | The image registry (Docker Hub, or `REGISTRY`) | An image download | Once, by `start.ps1`, after `research.ps1 -Enable`, when the pinned image is not on the PC yet. |
| The person's browser | A cited page | Nothing, until a person clicks a citation; links open with `rel="noopener noreferrer nofollow"`. | On a click. |

Nothing else leaves the PC because of research: the API is not on the `research` network; DeepSeek and page fetching
do not exist in 0.7.0; web text never reaches an AI prompt (the AI sees only the fixed summary line), a finding, an
event, n8n, an approval, a schedule or a task creation.

**Kept on the PC:** the query log (the canonical text of searches that were sent; blocked attempts as a code only),
results and sources (30 days), the cache, daily usage totals (2 years), audit entries (IDs, codes and counts only).
SearXNG writes no request lines to its container log (`GRANIAN_LOG_ACCESS_ENABLED=false`); the canary checks found no
search text in it. A query the gateway refuses when its task runs (a lead, a client or archive text added after it was
queued) loses its text from the task's parameters and from the stored plan of the Supervisor request behind it.

The API's and the dashboard's access logs record each request's path without its query string, for every route: the
text of a JARVIS Brain search (`GET /api/brain/search?q=...`), which 0.6.x logged, no longer reaches them. nginx's
access lines also no longer carry the referer, the browser's user agent or `X-Forwarded-For`. nginx still quotes the
whole request in an error line, for example while the API is unreachable.

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
- Install and upgrade never add `SEARXNG_SECRET`, and nothing requires it while research is off: `preflight.ps1` does
  not ask for it, and `install.ps1` leaves an existing `.env` as it is. `research.ps1 -Enable` creates it when it is
  missing (step 2 of section 2).
- A missing or short `SEARXNG_SECRET` while the research file is listed in `COMPOSE_FILE` never stops the core: every
  script leaves the research file out for its own process (warning printed, `.env` unchanged), and Compose itself
  refuses to start SearXNG with an empty secret. Meanwhile the worker runs without the research network (it is
  recreated once) and an already running SearXNG container is left as it is. When the secret is missing,
  `research.ps1 -Enable` adds a new one and starts SearXNG with it. When it is shorter than 32 characters, `-Enable`
  refuses: `research.ps1 -RotateSecret` gives it a new value, then `research.ps1 -Enable` reconnects the worker.
- `research.ps1` never changes a read-only `.env` (refused before anything changes), and replaces `.env` in one step
  (written next to it, then swapped in). A write that fails (a lock held by antivirus or sync software, a full disk,
  no permission) leaves `.env` as it was, exits 1 and runs nothing after it: no `start.ps1`, no "switched on".

### What a refusal can still tell

Every signed-in user can read the leads and the support tickets. So when a name is refused as `personal_record` and
it is in neither, the person who typed it learns that it is in the Credit Repair records. Nothing else in the answer
differs: the message, the step, the code and the work done are the same for a lead's, a ticket's and a client's name,
and the record ID behind a refusal is shown only to people with the sensitive-data permission (who may see the
clients anyway). That one answer cannot be hidden without refusing names nobody has stored, so it is limited and
visible: after 5 `personal_record` refusals in one day (UTC), nothing more of that person's is screened that day
(`too_many_blocked`, at intake and when a queued task runs), whatever their daily search cap, and the first time each
day an audit entry `research.personal_record_limit` names the person and the count. One person's queries are counted
and screened one at a time, so searches sent at the same moment cannot get past the 5. A search that waits more than
3 seconds for that person's turn is refused without being screened (`refused_budget`, `busy`; recorded by the gateway,
not at intake, where the row would need a second database connection while others wait), so the searches waiting for
a turn hold their database connections for at most 3 seconds. A search refused before its turn (research switched
off, the edition, the agent's policy, settings that cannot be read) is recorded on a second connection too: the API
gives the request's own connection back first, so a burst of such refusals never holds the API's connection pool
either (a step the Supervisor plans is checked in the worker, in the plan's own transaction). A refusal whose row could
not be written is answered `screen_error`, never with its own code, because it would not be counted. Apart from that,
a person's refusals per day are limited to their daily search cap.

Names are matched as stored: a lead's two name words anywhere in a query refuse it, so a lead with a common name, or
one planted through the public lead form, refuses more searches (it fails closed, and the Blocked list shows the lead to
people with the sensitive-data permission). A name particle ("de", "la", "van", "al" and the like) counts as a name word
only when the name has fewer than two other words: "Maria de la Cruz" is matched by "Maria" and "Cruz" (in any order,
or as the full name), not by "Santa Cruz de Tenerife"; "Al Smith" is still matched by "Smith, Al".

Credit Repair references are refused by their shape at the credit-topic step, whether or not the client exists:
`CR-0001`, `cr0001`, `CR 0012` and spelled out. Typed without a separator and without a leading zero (`cr1234`,
`CR 1234`), a reference passes, so that "CR2032 battery" stays searchable. That covers every reference until the
1,000th client (`CR-1000`); the screen would need a change before then.

Detection is pattern- and list-based: a paraphrase of private text, or a name found in no store, is not caught. Every
query that passes stays visible in the Research Center.

## 5. Owner decisions still open: defaults as built

The owner approved the approach, the release number, admin and operator access only, no viewer access, 50 searches a
day, and no DeepSeek, page fetching, paid search APIs or recurring searches. The other decisions of plan section 11
are still open; 0.7.0 is built with the proposed defaults below, all of them closed rather than open where the
proposal allowed either.

| Decision (plan 11) | Default in 0.7.0 | Where it lives | Who can change it |
|---|---|---|---|
| 4 caps | 50 a day in all (approved), 30 per person; per agent 20 Supervisor, 20 SEO & Content, 10 Social & Marketing, 10 WordPress Security, 5 Server Management; Monitoring and Communications none | `research_policy` (seeded by `0008_research.sql`); `research_settings`, an `app_settings` row that is absent (the code's defaults apply) until an admin saves the settings | An admin, in the Research Center (Policy and settings) |
| 4a class C (client names and domains) | Off for every agent, SEO & Content included | `research_policy.data_classes` | An admin can add C per agent in the Research Center |
| 5 public credit topics (FCRA, bureaus, disputes) | Refused for every agent | `app/research/screen.py` (credit words and reference shapes) | A release change |
| 6 typed software names and versions (class S) | Sent to SearXNG for `check_advisories` of WordPress Security and Server Management, and for the Supervisor (G, S, O); never generated from scan or SSH output | `research_policy.data_classes` | An admin can remove S per agent in the Research Center |
| 7 private-text check (6-word phrases of the JARVIS archive and memory) | Always on | `app/research/screen.py`, step 9 | A release change (no setting) |
| 14 retention | Sources 30 days, query logs 90 days, usage totals 730 days | `research_settings` | An admin, in the Research Center |
| 17 engines | DuckDuckGo, Brave, Wikipedia | `research/searxng/settings.yml` (`keep_only`) and `app/research/health.py` | A release change (a hand edit on the PC is lost at the next upgrade) |
| 18 editions | `web_research` only in the owner's internal `developer` edition; Personal, Professional and Agency leave it out until this decision and the legal review (COMMERCIALIZATION.md, B9) | `app/licensing.py` | A release change |
| 15 the real connectivity test | Not run | | The owner's approval (section 2, step 3) |

## 6. Operating it

- `status.ps1` shows the SearXNG container's state and health (no sign-in needed), or "switched off"; `smoke.ps1`
  reports web research for information (off, mock, or the search service's state) and fails only if the research API
  does not answer.
- `start.ps1` starts every other service first and SearXNG last, on its own, and only once `research.ps1 -Enable`
  listed it. If SearXNG cannot start (the image is missing and the registry cannot be reached,
  `research\searxng\settings.yml` is missing), it prints a warning and everything else runs; web research then reports
  the search service as unreachable.
- `backup.ps1` and `restore.ps1` cover the research tables with the rest of the database (no change in how they work).
- `research.ps1 -RotateSecret` replaces `SEARXNG_SECRET` in one write (after a copy of `.env`) and recreates only
  the SearXNG container. Do not delete the line by hand while the search service is switched on.
- `uninstall.ps1` also removes the SearXNG container, the two research networks and the cache volume (a cache, no
  data), even when `COMPOSE_FILE` no longer lists the research file; it keeps the SearXNG image and prints the command
  to remove it.
- A new SearXNG tag is pinned only after `testing/research-check.sh` passes on it in the sandbox (plan 2.6), and
  ships with a release; a settings change by hand on the PC would be lost at the next upgrade.

## 7. Rollback

1. **Switch research off** (seconds; keeps everything): Research Center, mode `off`. Searches stop at once; the
   worker stops probing SearXNG.
2. **Remove the search container**: `research.ps1 -Disable`. It removes the SearXNG container, takes
   `docker-compose.research.yml` out of `COMPOSE_FILE` (another entry stays), runs `start.ps1 -NoBuild` (the worker
   is recreated without the research network; nothing else restarts), then removes the cache volume and the two
   networks. The image (about 383 MB) is kept; the script prints the command that removes it. `SEARXNG_SECRET` stays
   in `.env`; nothing uses it while the file is not listed.
3. **Going back to an earlier release**: go back to the release you came from: **0.5.0**, the owner's PC today (or,
   once it is published, 0.6.2). **Never 0.6.1 or 0.6.0**: they are NO-GO (security notice at the top), and so is
   0.7.0 itself.
   - **If research was never switched on** (no `research.ps1 -Enable`, the mode never left `off`), nothing extra is
     needed: `.env` is unchanged, and no research task, query or search container can exist (intake refuses a
     search while research is off, before any task is created). Install the 0.5.0 folder as
     `RELEASE_NOTES_0.6.0.md` section 5 describes. Under 0.5.0, viewers see nothing research-related; the only
     difference is that Supervisor requests in which someone typed a web-search phrase ("search the web for ...")
     show their message, reply and plan notes to viewers again, as 0.5.0 shows every request (0.7.0 shows viewers a
     fixed text for them). The new tables, the research policy rows and the Credit Repair match index stay unused.
   - **If research was used**: run step 2 first (otherwise `.env` still names a file the 0.5.0 folder does not have,
     and every `docker compose` command there fails), take a backup, and install the 0.5.0 folder. Two things to know
     first:
     - a research task still queued is failed by 0.5.0 as an unknown capability (nothing is searched);
     - 0.5.0 shows every task's parameters and results to viewers, research tasks included, and every Supervisor
       request's message, reply and summary. Purging research records in the Research Center first ("PURGE
       RESEARCH") clears research tasks' queries and citations and the query and rationale of web-search steps in
       stored Supervisor plans. It does not clear a Supervisor request's message, reply or summary: a search the
       Supervisor ran was asked for in the operator's own words, so viewers under 0.5.0 still read those requests.
   - Checked in the sandbox: the backend of the released 0.5.0 package (SHA-256 checked) starts on a database 0.7.0
     migrated (0001-0008) and used, applies no migration, signs in, runs a Supervisor request, fails a queued
     research task as an unknown capability and leaves the research tables as they were (its migration runner
     applies only the files it has, and nothing in it refers to the JARVIS Brain or research tables); 0.7.0 then
     starts again on it without a migration. The same check passed with the packaged 0.6.1 backend, which matters
     only for an installation that already has 0.6.1.
4. **Restoring a backup taken before 0.7.0** (`restore.ps1`) keeps the research tables as they are: the backup does
   not hold them, and 0008 runs again at the next start without changing them. If research had been used, purge
   research records afterwards (Research Center, "PURGE RESEARCH", 0 days): their task and request numbers may now
   name other tasks and requests, because the restored numbering starts again from the backup's.
5. **Purging**: the admin's purge (typed `PURGE RESEARCH`) removes queries, results, sources, the cache, research
   tasks' parameters and citations, and the query and rationale of web-search steps in stored Supervisor plans (the
   request's plan and its planning task's result), older than the chosen age; the worker does the same at the end of
   the log retention (90 days). Supervisor requests keep their message, reply and summary, as every request does. It
   does not clear container logs or earlier backups; recreate the SearXNG container to clear its (query-free) log.

## 8. What the sandbox checked, and what it could not

All on 2026-10-20, in the sandbox's own Compose projects, with every other container left as it was. The development
build `0.7.0-dev` was checked first; the 0.7.0 release changes are checked by the test suite and the `.env` checks
below. The upgrade tests from the packaged 0.6.1 are recorded with the package (`releases/`).

- **Development instance** (plan 7.4, `testing/research-check.sh`, 35 checks passed): the real overlay and settings;
  `docker compose` refuses to run without `SEARXNG_SECRET`; healthy; no published port; user 977; read-only root and
  configuration; JSON 200 and HTML 403; `/config` shows exactly the three engines and the pinned version; no internet
  here, so every engine is unresponsive and the backend's own provider reports `provider_down`; `unreachable` while
  stopped; healthy again within 6 s of a start or restart; back by itself after its main process is killed; the
  image's default `::` address fails without IPv6 (the reason for `GRANIAN_HOST`); no search text in its log. A
  test-only JSON engine on a mock upstream showed real result parsing, the safe-search lock, the image and favicon
  proxies fetching nothing for a caller, and a burst of 200 searches (87 MiB and 15 processes at peak).
- **Full stack** (plan 7.5, on the development build): `install.ps1` (the `-dev` build refused without
  `CC_ALLOW_DEV_BUILD=1`); `research.ps1 -Enable`, `-RotateSecret` and `-Disable` (PostgreSQL, n8n, the API and the
  dashboard kept their containers; only the worker was recreated); the layout (SearXNG on `research` and
  `research_egress` only, the worker on `internal`, `egress` and `research`, nothing else on `research`, no new port;
  also merged with the tunnel overlay); SearXNG could not reach PostgreSQL, the API, n8n or the dashboard by name or
  address; viewers and the n8n service token got 403; with research off or in mock mode a packet capture on SearXNG's
  interface saw no connection; in live mode the runs reached it and ended `provider_down`; the restart recovery; the
  start-up fallbacks (image and registry missing, `settings.yml` missing, `SEARXNG_SECRET` missing) with `status`,
  `smoke`, `backup`, `restore`, `stop` and `install` still working; `.env` edits on files with mixed line endings, a
  BOM, quotes and a tunnel entry; `uninstall.ps1` with the research file no longer listed.
- **The 0.7.0 `.env` rules** (PowerShell 7.4, the release's scripts on a synthetic 0.6.1 `.env`, LF and CRLF, Docker
  replaced by a stand-in): `init-env.ps1 -AddMissing` and `preflight.ps1` leave `.env` byte for byte and ask for no
  research key; `research.ps1 -Status` says the secret is not set yet; `-Enable` copies `.env` first (the copy is
  byte-identical to the original), writes `COMPOSE_FILE` and a 43-character `SEARXNG_SECRET` in one write, never
  prints it, gives it to Compose's check only, and runs `start.ps1` without it in the environment; an empty line is
  filled in place, a listed research file gets only the secret, a tunnel entry stays, a short value or two lines are
  refused with nothing changed. For `-Enable`, `-RotateSecret` and `-Disable`, run as an ordinary user: a read-only
  `.env` is refused (exit 2) with nothing copied, changed or started; when the new text cannot be written, or cannot
  be swapped in, each exits 1 with `.env` byte for byte as it was, no temporary file left, no "switched on", "new
  value" or "switched off" line and no `start.ps1`. Outside Windows (`pwsh` as an ordinary user, umask 022), a `.env`
  with mode 0600 is still 0600 after each of them, and so are its `.env.before-research-*` copies (the new `.env` used
  to take the temporary file's mode, 0644). A short secret gets the hint `-RotateSecret, then -Enable` from
  `preflight.ps1`, the Compose helper and `-Status`, and `status.ps1`'s SearXNG line says the secret is "missing or
  shorter than 32 characters"; a missing one gets `-Enable`.
- **Concurrent searches by one person** (the test suite): 25 at once, more than the API's database connection pool
  (10 + 10), with research in mock mode and a lead's name: at most 5 `personal_record` answers, each recorded, the
  rest `too_many_blocked` or `busy`, no pool timeout, done in about 4 seconds (before: 6 `personal_record` answers, 5
  pool timeouts and an unrecorded refusal after 34 seconds). With research switched off (the default), 25 at once are
  all answered `research_off` and recorded, and another API request sent during them is answered at once, all within
  a second (before: after 30 seconds, 5 pool timeouts, refusals not recorded, and the other request failed with a pool
  timeout too).
- **db-init's volume** (Compose projects of their own, removed afterwards): with this `docker-compose.yml`, `up`,
  `up --force-recreate` and `down --volumes` leave no anonymous volume. An installation whose `db-init` got one under
  0.6.x keeps it mounted after the upgrade (Compose hands it on); `uninstall.ps1 -DeleteData` and `brain-test.ps1
  -Action Remove` (on a test copy started under 0.6.x) now remove it, and before this fix both said nothing was left
  while one volume was.
- **Rollback** (plan 8): the backends of the released 0.5.0 and 0.6.1 packages
  (`releases/InfiniteAI-CommandCenter-0.5.0-windows.zip` and `-0.6.1-windows.zip`, SHA-256 checked), each run from
  source in the sandbox on a database that 0.7.0 (0.5.0) or the development build (0.6.1) migrated (0001-0008) and
  used (research in mock, a queued research task, a blocked-attempt row, the Supervisor's cap set to 3, a Credit
  Repair client): each applied no migration, started, signed in, ran a Supervisor request, failed the queued research
  task as an unknown capability and left the research tables and policy rows as they were; going forward again
  applied nothing and rebuilt the Credit Repair match index.
- **Access logs**: the dashboard's nginx image with this release's `nginx.conf`, in a throwaway container: the
  configuration test passes, and the request lines of `/?q=...`, `/api/brain/search?q=...` and `/assets/...?v=...`
  carry no query string; the error line for the unreachable API still quotes it.
- **Not verified:** real search results, blocking by the search engines and CAPTCHAs (the sandbox has no internet);
  Windows PowerShell 5.1 itself (the scripts are checked for 5.1-only syntax and run under PowerShell 7.4); Docker
  Desktop's bind-mount permissions and IPv6 on the owner's PC; the JARVIS Brain test copy was not started (its seed
  statements and the fingerprint change were checked on their own).
