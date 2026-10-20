# Infinite AI Command Center 0.7.0: release notes

Release date: 2026-10-20 · **NO-GO for installation and for any JARVIS
import** (security notice below; `releases/GO_NO_GO_0.7.0.md`) · package
`InfiniteAI-CommandCenter-0.7.0-windows.zip` in `command-center/releases/`,
with its SHA-256 in the `.sha256` file next to it · no Windows upgrade guide:
there is no upgrade to make while 0.7.0 is NO-GO (section 5)

> **Security notice, 2026-10-20: 0.7.0 is NO-GO, for installation and for any
> JARVIS import.** 0.7.0 is built on 0.6.1, which the security notice of the
> same day declared NO-GO, and it carries 0.6.1's JARVIS importer unchanged,
> so it has the same high-severity defect (reproduced three times
> independently on the exact published 0.6.1 code). The text of a JARVIS
> message that an admin withheld comes back when a **later** JARVIS backup
> is imported. JARVIS's compaction stores copies of messages under new
> identifiers, and the importer withholds only the identifier that was
> withheld, so a copy is imported with its text. Operators, admins, the
> Supervisor's history and recall, and newly promoted memory items can then
> read it (viewers cannot), and the verification commands still report
> success. Web research does not change this.
>
> - Do not install 0.7.0, 0.6.1 or 0.6.0, and do not import JARVIS data
>   with 0.7.0 or any 0.6.x release.
> - If you are still on 0.5.0, stay there. A corrected 0.6.2 is being
>   prepared, with a direct 0.5.0 -> 0.6.2 guide; it will be published only
>   after its security reviews pass. Web research will be offered for
>   installation only in a 0.7 release that also contains 0.6.2's fix,
>   tested on its own upgrades from 0.5.0 and from 0.6.2.
> - An installation of 0.6.0 or 0.6.1 that has imported nothing holds no
>   JARVIS data and is not exposed: leave it as it is and import nothing.

0.7.0 adds **web research**: the Research Gateway, through which agents can
search the web with a private SearXNG search service on your PC, every query
screened before it leaves. It is **switched off**, and the upgrade leaves it
off: nothing searches, nothing of SearXNG is downloaded or started, and your
`.env` is not changed. Switching it on is a series of steps, each one your
decision (section 3). The full description is `docs/RESEARCH_GATEWAY.md`.

**Not installed.** This release has not been installed on your PC, and it
must not be (security notice above). What follows describes the package as
built and tested in the sandbox.

**What the upgrade changes in your installation.**
- **Two database migrations.** `0008_research` adds the research tables
  (empty) and the default research permissions of the eight agents. In the
  Credit Repair database, `0002_research_index` adds the two tables of the
  match index (section 2). No existing table or row is changed.
- **The Credit Repair match index is rebuilt at every start,** also while
  research is off (section 2).
- **Access logs no longer hold query strings** (section 2).
- **Viewers see less of the Supervisor's plans** (section 2).
- **`smoke.ps1` has eight checks**: the new one, for information only, reads
  `PASS  web research (information only)  off`.

**What it does not change.** It leaves all of these as they are:
- your `.env`, byte for byte: no new key is needed, and `SEARXNG_SECRET`
  (the search service's key) is not added; `research.ps1 -Enable` creates it
  later, if you switch the search service on;
- the PostgreSQL and n8n volumes;
- users, stored secrets and settings (there is no research mode setting yet:
  web research is off);
- the agents and their on/off states, and your approval rules;
- schedules (research is never scheduled), sites and leads;
- the n8n workflows and credentials.

No new container, network, volume or image is added. Memory in AI prompts
stays off.

## 1. What 0.7.0 adds (switched off)

- **The Research Gateway** (in the worker): the only code that searches the
  web for the Command Center. Each search passes the same checks, and any of
  them can refuse: research switched on, the edition, the agent's
  permission, who started it, the outbound screen, the cache, the daily
  limits, the provider. Any error refuses; nothing is retried beyond one
  retry on a connection error or a 500 from SearXNG.
- **The outbound screen.** It refuses a query holding a secret, an e-mail
  address or a phone number (however it is written), a lead's or ticket's
  name, domain or phone number, a Credit Repair client's name, e-mail or
  phone number (through a match-only index), anything shaped like a Credit
  Repair reference, a 6-word phrase of the JARVIS archive or memory, an
  internal host or IP address, a client's business or website (unless you
  allow it), credit topics, and search-engine commands. A refusal keeps a
  reason code only, never the text.
- **Five research capabilities, read-only, no approval:** the Supervisor's
  web search, SEO & Content's topic research, Social & Marketing's trend
  research, and the advisory checks of WordPress Security and Server
  Management (a software name and version a person types). Each returns a
  fixed summary line and a separate list of citations: the AI never reads
  web text. Credit Repair, Monitoring and Communications cannot search.
- **Who can search:** admins and operators, from the Research Center, an
  agent's Run or a Supervisor request they type on the dashboard. Never
  viewers, n8n, the service token or a schedule.
- **Limits:** 50 searches a day in all, 30 per person, per agent 20
  (Supervisor), 20 (SEO & Content), 10 (Social & Marketing), 10 (WordPress
  Security), 5 (Server Management); one search every 3 seconds, in bursts
  of up to 3.
- **Research Center** (dashboard, admins and operators): new research,
  history with citations, sources, usage, the search service's status,
  blocked attempts; for admins the mode, limits, retention, agent
  permissions, the connectivity test and purge. A Research card on the
  Settings page (admins) holds the mode and limits.
- **The search service** (optional): `docker-compose.research.yml` and the
  read-only folder `research/searxng/`, the official SearXNG image pinned by
  tag and digest, engines DuckDuckGo, Brave and Wikipedia, no published
  port. It is used only after `scripts\research.ps1 -Enable`.
- **Not in 0.7.0:** DeepSeek (no provider, no key field, no setting),
  page fetching, paid search APIs, recurring searches, AI summaries of web
  text, and web results in the JARVIS Brain.

## 2. Changes outside web research

- **Credit Repair match index.** At every start, the API and the worker each
  rebuild a match-only index of the Credit Repair clients: they decrypt each
  client's name, e-mail address and phone number inside the Command Center
  and store only HMAC values (keyed from `CREDIT_REPAIR_DATA_KEY`) in the
  Credit Repair database. This happens even while research is off, and once
  a client exists the Credit Repair access log shows two
  `system:research-index research_index_rebuild` entries per start, with the
  number of clients. Nothing leaves the PC. Without Credit Repair clients
  the index is empty.
- **Access logs.** The API's and the dashboard's access lines record each
  request's path without its query string, for every route, so the text of
  a JARVIS Brain search no longer reaches them. nginx's access lines also no
  longer carry the referer, the browser's user agent or `X-Forwarded-For`.
  nginx's error lines still quote the whole request (for example while the
  API is unreachable). This settles the "Access logs" point that 0.6.1 left
  open (`RELEASE_NOTES_0.6.1.md`, section 6).
- **What viewers see.**
  - A research task's parameters and results (the query and its
    citations) are hidden.
  - A Supervisor request that asked for a web search shows viewers a fixed
    text, "Request with a web search (visible to operators and admins)",
    instead of its message, reply and summary, also where an approval
    quotes it.
  - Planner notes are hidden from viewers on every Supervisor plan (also the
    planning task's log), and a planning task's reply reads "The reply is
    shown on the Supervisor request" (the request shows its own reply).
  - Operators and admins see everything as before.
- **Editions.** The `web_research` feature is only in the owner's internal
  `developer` edition. The sellable editions (Personal, Professional and
  Agency) leave it out until the editions decision and the legal review
  (`COMMERCIALIZATION.md`, B9).
- **Scripts.**
  - `install.ps1` refuses a development build (`X.Y.Z-dev`).
  - Every script that runs `docker compose` reads which Compose files are in
    use through the new helper `compose-env.ps1`: a missing `SEARXNG_SECRET`
    can never stop the core services.
  - `status.ps1` prints one more line, `Web research search service
    (SearXNG): switched off`; `preflight.ps1` prints the same as an
    `[info]` line.
  - `uninstall.ps1` also removes the research networks and cache volume if
    they exist, and keeps the SearXNG image.
  - `db-init` no longer gets an anonymous Docker volume (an empty folder its
    image declares). Since 0.6.x, each recreated `db-init` took over the old
    one, so `uninstall.ps1 -DeleteData` and `brain-test.ps1 -Action Remove`
    left one empty volume behind while saying nothing was left. Both now
    remove it, also the one an installation from before 0.7.0 still has,
    and count it in their final check.
  - The JARVIS Brain test copy switches research off in its copy, and its
    check of the live installation tolerates a SearXNG restart.
- **Software bill of materials.** `SBOM.md` and `sbom.cdx.json` were
  generated again from a backend image built from 0.7.0 (fresh dependency
  resolution). The SearXNG image is listed as optional. One indirect Python
  package is newer than in 0.6.1: certifi 2026.10.19 (was 2026.10.18).

## 3. Web research, when you switch it on

Each step is your decision; stop after any of them. The details are in
`docs/RESEARCH_GATEWAY.md`, section 2.

1. **Mock mode** (Research Center, admins): searches get a fixed fake
   answer. Nothing leaves the PC and no container is added.
2. **The search service**: `powershell -NoProfile -ExecutionPolicy Bypass
   -File .\scripts\research.ps1 -Enable`. It copies `.env` to
   `.env.before-research-<time>`, then in one write adds
   `docker-compose.research.yml` to `COMPOSE_FILE` (a remote-access entry
   stays) and creates `SEARXNG_SECRET` (never printed). It downloads the
   SearXNG image once (about 100 MB, 383 MB on disk) and recreates the
   worker; the other services keep running. Agents still cannot search.
   It refuses a read-only `.env`, and replaces `.env` in one step with the
   same permissions: if the write fails (a lock held by antivirus or sync
   software, a full disk), it stops with `.env` unchanged (exit 1) and
   starts nothing.
3. **The real connectivity test** (not run yet): live mode with the typed
   confirmation `ENABLE LIVE RESEARCH`, one fixed search ("Wikipedia"), then
   back to mock or off unless you decide otherwise.
4. **Live mode** for the agents.

**Every outbound data flow once it is on** (none while it is off or in
mock mode, apart from the one image download in step 2):
- **Worker → SearXNG** (private network, live mode only): the screened
  query, the engine names, language, time range, page; a health check every
  60 s and a configuration read every hour.
- **SearXNG → DuckDuckGo, Brave, Wikipedia** (HTTPS, through your internet
  connection): the query text, the language and moderate safe search, for
  each live search not answered from the cache. Each engine sees your PC's
  public IP address; no user, client or Command Center identity is sent.
- **Docker → the image registry** (Docker Hub, or `REGISTRY`): the SearXNG
  image download, once, after step 2.
- **Your browser → a cited page**: only when a person clicks a citation.

Nothing else leaves the PC because of research: the API is not on the
research network, and web text never reaches an AI prompt, a finding, an
event, n8n, an approval, a schedule or a new task.

## 4. Your open decisions: the defaults built in

You approved the SearXNG-first approach, release 0.7.0, admin and operator
access only, no viewer access, 50 searches a day, and no DeepSeek, page
fetching, paid search APIs or recurring searches. The other decisions of the
design (`docs/RESEARCH_GATEWAY_PLAN.md`, section 11) are still open; 0.7.0 is
built with the proposed defaults:
- **Caps (4):** 30 per person; per agent as in section 1; Monitoring and
  Communications none. An admin changes them in the Research Center.
- **Client names and domains, class C (4a):** off for every agent, SEO &
  Content included. An admin can allow it per agent.
- **Public credit topics (5):** refused for every agent. Changing this
  needs a release.
- **Software names and versions a person types, class S (6):** sent to
  SearXNG for the advisory checks of WordPress Security and Server
  Management and for the Supervisor; never taken from scan or SSH output.
  An admin can remove it per agent.
- **Private-text check (7):** always on (no setting).
- **Retention (14):** sources 30 days, query logs 90 days, usage totals 2
  years. An admin changes them in the Research Center.
- **Engines (17):** DuckDuckGo, Brave, Wikipedia. Changing them needs a
  release.
- **Editions (18):** web research only in the owner's internal edition
  (section 2). Changing it needs a release.
- **The real connectivity test (15):** not run; it needs your approval.

## 5. Upgrade

**Not now.** 0.7.0 is NO-GO for installation (security notice at the top).
Stay on 0.5.0, your PC today, and do not install 0.6.0 or 0.6.1 on the way:
both are NO-GO too. The way to web research will be 0.5.0 -> 0.6.2 -> a 0.7
release that contains 0.6.2's fix, or 0.5.0 straight to that release, each
with its own guide and upgrade tests. There is no 0.7.0 guide.

What installing this package does, as tested in the sandbox:
- **No new keys, no `.env` change.** The new folder gets a copy of `.env`;
  `install.ps1` leaves it byte for byte. If it prints `Added to .env`, stop:
  that `.env` is from before 0.2.0. n8n restarts once, as in every upgrade.
- **Web research stays off.** After the upgrade, `status.ps1` shows `Web
  research search service (SearXNG): switched off`, and Docker has no
  `searxng/searxng` image, no research network and no SearXNG volume.

## 6. Going back

Go back to the release you came from: **0.5.0** (or, once it is published,
0.6.2). Never to 0.6.1 or 0.6.0: they are NO-GO (security notice).
- **Quick, data kept:** `install.ps1` from the 0.5.0 folder, as in
  `RELEASE_NOTES_0.6.0.md` section 5. 0.5.0 ignores the JARVIS Brain tables,
  the research tables and the Credit Repair index tables (its migration
  runner applies only the files it has, and nothing in it refers to them).
  Checked in the sandbox with the backend of the released 0.5.0 package on a
  database 0.7.0 had migrated and used (`docs/RESEARCH_GATEWAY.md`, section
  8).
- **If web research was never switched on** (the usual case), going back
  needs no extra step: `.env` is unchanged, and no research task, query or
  container exists. The one difference under 0.5.0: Supervisor requests in
  which someone typed a web-search phrase ("search the web for ...") show
  their message, reply and plan notes to viewers again, as 0.5.0 shows every
  request.
- **If web research was used:** first `research.ps1 -Disable` (otherwise
  `.env` names a Compose file the 0.5.0 folder does not have), and purge
  research records in the Research Center ("PURGE RESEARCH"), because 0.5.0
  shows every task's parameters and results to viewers. A research task
  still queued is failed by 0.5.0 as an unknown capability; nothing is
  searched. `docs/RESEARCH_GATEWAY.md`, section 7, has the details.
- **Full restore (disaster only):** `restore.ps1` with the backup taken
  before the upgrade. It keeps the research tables as they are (the backup
  does not hold them); if research had been used, purge research records
  afterwards, because their task and request numbers may then name other
  tasks and requests. Prefer the quick route.

## 7. Tests (cloud sandbox)

Synthetic data only, never real JARVIS or Credit Repair data.

| Test | Result |
|---|---|
| Backend (pytest, real PostgreSQL 18), fresh database, Docker restore tests required | TODO (lead) |
| Dashboard (Vitest), type check, production build | TODO (lead) |
| PowerShell: every script parses under PowerShell 7.4 and is ASCII; the `.env` rules on a synthetic 0.6.1 `.env` (LF and CRLF) | TODO (lead) |
| Development instance and full stack (`testing/research-check.sh`, plan 7.4 and 7.5) | TODO (lead) |
| Upgrade tests from the package (0.6.1 → 0.7.0 with `.env` unchanged and no SearXNG objects, 0.5.0 → 0.7.0, going back to 0.5.0, restoring a 0.6.1 backup under 0.7.0, `research.ps1 -Enable` on the upgraded `.env` in the sandbox) | TODO (lead): in `releases/GO_NO_GO_0.7.0.md`; they test the package, they do not change its NO-GO |

**Reviews.** TODO (lead).

## 8. Known limitations and what stays open

- **A refusal can still tell one thing.** Every signed-in user can read the
  leads and support tickets, so a name refused as `personal_record` that is
  in neither is in the Credit Repair records. Everything else about the
  refusal is the same for a lead and a client. This one bit is limited: after
  5 such refusals in a day, nothing more of that person's is screened that
  day, and an audit entry (`research.personal_record_limit`) shows it to the
  admins. One person's searches are screened one at a time, so searches
  sent at the same moment do not get past the 5: one that waits more than
  3 seconds for its turn is refused unscreened (`busy`), and a refusal that
  could not be recorded is answered `screen_error`.
- **The screen is pattern- and list-based.** A paraphrase of private text,
  or a name that is in no store, is not caught. Names are matched as
  stored: a lead with a common name, or one planted through the public lead
  form, refuses more searches. A name particle ("de", "van", "al") counts
  only when the name has fewer than two other words.
- **Credit Repair references** typed without a separator and without a
  leading zero (`cr1234`) pass the screen, so that "CR2032 battery" stays
  searchable. That covers every reference until the 1,000th client.
- **The Credit Repair match index** decrypts every client at every start,
  even with research off (section 2).
- **Not verified:** real search results, blocking by the search engines and
  CAPTCHAs (the sandbox has no internet); Windows PowerShell 5.1 itself (the
  scripts are checked for 5.1-only syntax and run under PowerShell 7.4);
  Docker Desktop's bind-mount permissions and IPv6 on your PC.
- **Once live:** searches leave from your home IP address; the engines may
  answer with CAPTCHAs or blocks, which pause that engine for a while.
- **Before research is offered to customers:** the engines' terms and the
  SearXNG licence (AGPL) need the legal review in `COMMERCIALIZATION.md`
  (B9).
- **The JARVIS importer of 0.6.1, unchanged:** a withheld message's text
  comes back when a later JARVIS backup is imported (security notice at the
  top). 0.7.0 is NO-GO for installation and for any JARVIS import until a
  0.7 release carries 0.6.2's fix.
- **Still open from 0.6.x:** the JARVIS Brain's detection is pattern-based
  (names and street addresses are not detected), so the live import stays
  NO-GO until you have reviewed the test copy. From earlier releases: the
  server gate cannot see an approval; no run on a real server; remote access
  is not set up.
