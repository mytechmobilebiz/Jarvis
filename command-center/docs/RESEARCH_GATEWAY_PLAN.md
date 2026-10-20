# Research Gateway: SearXNG primary, DeepSeek fallback (proposal)

**Status: proposal only. Nothing is implemented or installed.** No code, table, setting, container, image, key or file was created or changed. No search engine, SearXNG instance or DeepSeek endpoint was called. The plan waits for the owner's approval. Date: 2026-10-20 (revision 2, after four reviews).

**Base:** the released 0.6.0 code (package `InfiniteAI-CommandCenter-0.6.0-windows.zip`, SHA-256 `a687f737...36a8a1`, GitHub branch `jarvis-migration`). Where 0.5.1 (branch `ccr-eb27a2af-8u7i39`) differs, it is noted.

## GO/NO-GO

| Question | Recommendation |
|---|---|
| The architecture in this plan | **GO to build and test in the sandbox** (phases 2 and 3 of section 9) once you approve decisions 1 to 7 in section 11. Nothing reaches your PC before a packaged, tested 0.7.0 and your approval to install it. |
| SearXNG as the primary provider | **GO.** The proposed configuration (section 2) was run three times in an isolated sandbox: healthy, private, no published port, JSON only, read-only, not root, recovers after a restart. Real result quality and blocking by the search engines are unknown until the connectivity test on your PC (section 7.7), which needs its own approval. |
| DeepSeek as the paid fallback | **NO-GO for now.** Its documentation could not be read directly (only search-engine excerpts). Its policies store data in China and may allow training use. Revisit for 0.7.2 after the pages are confirmed and you accept the terms. |
| Page fetching | **NO-GO for 0.7.0.** It is a later, optional 0.7.1, with its own SSRF tests. |
| Installing anything now | **NO-GO.** The order is: 0.6.0 (your upgrade), then the 0.6.1 patch, then 0.7.0 with research switched off, and then you switch it on step by step. |
| Release number | **0.7.0** for the Research Gateway; Messaging & Channels moves to 0.8.0 (decision 2). |

**Evidence tags used below.** Full paths are at the end.

| Tag | Source |
|---|---|
| [S] | The first real sandbox run of SearXNG (observations, compose, settings) |
| [S2] | The second sandbox run, which used this plan's exact compose and settings files, merged with the real 0.6.0 `docker-compose.yml` (`draft-validation.md`) |
| [S3] | The third sandbox run, on the final files of section 2 (including the preference lock and the empty `limiter.toml`): 15 checks passed, among them healthy, no published port, JSON 200 and HTML 403, private network with no route out, read-only configuration and root file system, user 977, recovery after a restart, the worker reaches SearXNG, no search text in the container log, every other container unchanged; merged with the real 0.6.0 `docker-compose.yml`, SearXNG is on `research` and `research_egress` only and no new port is published |
| [C1] [C2] [C3] | The code investigations (backend and agents; research, memory and UI; security controls) |
| [D] | The DeepSeek docs facts (search-engine excerpts, not page reads) |
| [DR] | The earlier DeepSeek-only draft |
| [CR] | Reviewers' critiques of that draft |
| [X1] [X2] | The SearXNG investigations (Docker; API and settings) |
| [O] | How a new service fits the compose files and scripts |
| [R] | Raw official SearXNG files from the master branch |
| [RT] | Raw official SearXNG files at the pinned commit `d48c4b555` |

---

## 0. Summary and recommendation

**What this is.** One Research Gateway inside the backend. It is the only code allowed to search the web for the Command Center's agents.
- It asks a **self-hosted SearXNG** container first. That container is private, has no published port and returns JSON only.
- **DeepSeek Search and Fetch** are an optional paid fallback, off by default.
- Every result comes back in one JSON shape, with the URL, title, snippet, source, dates and a citation number.
- Web text is stored apart from JARVIS memory. It is never treated as instructions, and it can never start an action that needs approval.

### Build first
1. **The gateway core, with a mock provider only.** This covers:
   - the provider interface;
   - one function that puts every query into a standard form, followed by the outbound screen. The screen also checks the leads, the tickets, a match-only index of Credit Repair clients, and the JARVIS archive and memory;
   - per-agent permissions, with the trigger read from the database;
   - budgets and rate limits;
   - the cache;
   - the query log;
   - the new tables;
   - the Research Center page in mock mode;
   - all tests.
2. **SearXNG as the first real provider.** It comes as a separate, optional Compose file, off until the owner switches it on.
3. **Search-only research capabilities with risk READ** on a few agents (section 10). They run without approval. Approval-gated actions stay exactly as they are.

### Do not build yet
- **The DeepSeek fallback.** Two things must happen first: someone opens DeepSeek's pages and confirms the facts, and the owner accepts DeepSeek's terms (storage in China, and possible training use).
- **Page fetching of any kind.** That includes advisory pages for the WordPress and server agents. The local fetch service and DeepSeek Fetch are later, optional phases.
- **AI summaries of web text.** The same goes for drafts that use web text.
- **Research started by n8n or by schedules.**
- **Automatic fallback.**
- **A Valkey container.**

### Owner decisions needed first
The full list is in section 11. These are the most important:
- the release number (proposed: Research is 0.7.0 and Messaging moves to 0.8.0);
- who may start research (proposed: people, and Supervisor requests typed on the dashboard);
- the agent matrix and the starting caps (proposed: 50 searches a day at first);
- whether software names and versions typed by a person may be sent to the search engines;
- whether DeepSeek may ever be used;
- the real connectivity test on the PC.

### What could not be verified
- **DeepSeek.** The sandbox blocks `api-docs.deepseek.com` (DNS failure and a proxy 403). Every DeepSeek fact here comes from search-engine excerpts of the official pages, not from reading the pages [D]. Some sources conflict: the rate limit (5 or 100 per second) and the currency (USD or CNY). Section 13 lists what must be confirmed.
- **SearXNG docs.** `docs.searxng.org` is blocked. Its facts come from the official source files on `raw.githubusercontent.com`. The files that matter were read at the pinned commit `d48c4b555` and are identical to master, or differ only in formatting [RT].
- **Real search results.** The sandbox containers had no internet. So we know that SearXNG starts, answers in JSON, fails cleanly and recovers. We do not know the quality of real DuckDuckGo or Brave results, or how quickly they block a home IP [S][S2].
- **The owner's PC.** It was not visible. Unknown: IPv6 in Docker Desktop, how Windows bind-mount permissions look inside the container, other Docker networks, and whether remote access is switched on. The 0.6.0 release notes say this installation does not use `EXTRA_CA_BUNDLE`, so a TLS-intercepting proxy is unlikely.

### Side finding: a 0.6.0 defect, separate from research
- **The fault.** `brain/store.py:277` (`quarantine_item`) writes a version row with `change='quarantined'`. The CHECK at `migrations/0006_brain.sql:110-112` does not allow that value. The defect is confirmed by a test run and recorded as item 12 of `releases/GO_NO_GO_0.6.0.md`.
- **What the owner sees.** An admin cannot withhold an archived message once it has a promoted copy that is not quarantined. Nothing changes and the page shows an error.
- **Workaround** (from GO_NO_GO): delete and purge the promoted copy first.
- **Fix.** GO_NO_GO already plans a 0.6.1 patch before the test-copy import: a new idempotent migration and a regression test. This plan assumes that patch.

---

## 1. Architecture proposal

### 1.1 Components and where the code lives

All of these are proposed; none exist yet.

| Component | Where | What it does |
|---|---|---|
| Research client | `backend/app/research/__init__.py`, given to handlers as `ctx.research` | Tied to one agent, one task and one trigger. It offers only `search()` and `fetch()`. It holds no key and has no other way out. |
| Gateway pipeline | `research/gateway.py` | Runs the checks in order. Any step can refuse. |
| Canonical form | `research/canon.py` | Puts a query into one standard form before it is screened. The screened string is exactly the string sent (section 5, T1). |
| Outbound screen | `research/screen.py` | Checks every query and URL before it leaves. It reuses `brain/text.clean()` and `instruction_flags()` [C3], and adds the store checks in section 5, T1. |
| Credit Repair match index | `credit_repair/blind_index.py`, kept in the Credit Repair database | HMAC values of client names, e-mails, phones and references. The function only answers "matches" or "no match". Nothing is decrypted for the gateway. |
| URL checks for fetch (later) | `research/urls.py` | Scheme, no user name or password, ports 80 and 443, no IP literals, public DNS only. It reuses `agents/http.resolve_public` with an empty allow list, and refuses a name that does not resolve [CR]. |
| Policy, budgets, rate limits | `research/policy.py`, `research/budget.py` | Per-agent rules and atomic counters in PostgreSQL. The trigger is resolved from the database, never from task parameters. |
| Cache | `research/cache.py` | Shared PostgreSQL cache; it never records who asked. |
| Normaliser and cleaner | `research/normalize.py` | Cuts every field to its cap first. Then it maps every provider's answer to one schema (section 3.6), cleans the text and flags instruction-like content. |
| Providers | `research/providers/base.py`, `mock.py`, `searxng.py`, `deepseek.py`, `local_fetch.py` | One interface. The mock is the default. |
| Local fetch service (optional, later) | `research/fetch_service.py`, run in its own container | Fetches and cleans pages with SSRF protection. It holds no secrets and has no database access (section 3.4). |
| Store and health | `research/store.py`, `research/health.py` | Research tables, plus `integration_status` rows `research_searxng`, `research_deepseek` and `research_fetch`. |
| API router | `backend/app/api/research.py` | Read endpoints, promotion, policy, settings, purge, connectivity test. |
| Changes to existing backend files | `agents/base.py` (a capability declares that it uses research; `AgentContext.research`; `integrations` may be empty); `worker/runner.py` (a smaller context for research capabilities; trigger resolution); `tasks.py` (an internal set for the test capability, refused by direct runs, both planners and retries); `supervisor/planner.py` (escape `<request>`; the new rule intent; the query-from-request rule); `supervisor/agent.py` (screen planned steps before the plan is stored; a separate citation list); `agents/seo.py` (the `keyword_brief` pattern); `agents/drafting.py` (fix `untrusted()`); `api/work.py` (screen before creating a task; `task_view` hides research details from viewers); `api/supervisor.py` (`request_view` hides research steps from viewers); `brain/policy.py` (`web_source` added to `RISK_FLAGS`); `brain/store.py` (`edit_item` rules for web items); `customer_settings.py` (secret `deepseek_api_key`); `licensing.py` (feature `web_research`); `config.py`; `app.cli check-config` | [C1][C2][CR] |
| Migrations | `backend/app/migrations/00NN_research.sql` (0008 if 0.6.1 takes 0007), plus one migration in the Credit Repair database for its match index | New tables only (section 3.14). |
| Compose and config | `docker-compose.research.yml`, `research/searxng/settings.yml`, `research/searxng/limiter.toml` | Section 2. |
| Scripts | New: `scripts/research.ps1` and `scripts/compose-env.ps1` (a shared helper). Updated: `init-env.ps1/.sh`, `preflight.ps1`, `install.ps1`, `start.ps1`, `stop.ps1`, `backup.ps1`, `restore.ps1`, `status.ps1`, `smoke.ps1`, `uninstall.ps1`, `brain-common.ps1`, `brain-test.ps1`, `build-package.py`, `generate-sbom.py` | [O] |
| Dashboard | `dashboard/src/pages/Research.tsx`, `shell/nav.ts`, `api.ts`, `Settings.tsx` (a "Research" card), `Brain.tsx` (labels and filter), `Supervisor.tsx` (a separate "Web sources" list), `dashboard/service.ts` (status row) | [C2][O] |

### 1.2 Data flow: agent, gateway, provider, normalized results, Supervisor

```
 Operator on the dashboard                     Supervisor request typed on the dashboard
 (Research Center, or Run on an agent)                  │
        │                                               ▼
        │                                  supervisor.plan_request (worker; Claude or rules)
        │                                  research steps only when research is on AND the request came
        │                                  from the dashboard; Claude's query must use the operator's words
        ▼                                               │
 API: canonical form + screen (no network) ────► each research step: canonical form + screen ──┐
        │ blocked → reason code only, no task row       blocked → step, rationale and note removed │
        ▼                                                                                        ▼
 tasks.create_task (risk READ → no approval; nothing about approvals changes)
        │
 worker claims the task (the only process that reaches SearXNG or the internet)
        │
 capability handler, e.g. seo_content.research_topic ── ctx.research (no ssh, integrations or llm)
        ▼
 ┌──────────────────────── Research Gateway (backend/app/research, in the worker) ─────────────────────┐
 │ 1 research on? edition has web_research? agent allowed? never credit_repair                        │
 │   trigger read from the database: dashboard run, dashboard request, or a person's retry            │
 │ 2 canonical form, then screen again (defence in depth): secrets, restricted data, contacts,        │
 │   leads, tickets, Credit Repair index, private JARVIS text, internal hosts, clients, search syntax  │
 │ 3 cache lookup; identical requests in flight are merged                                            │
 │ 4 reserve a budget and rate slot (own short database transaction)                                  │
 │ 5 provider: SearXNG (engines= minus cooling engines) ─(down or all engines failing, AND allowed)─► │
 │   DeepSeek (manual by default)                                                                     │
 │ 6 validate, cut each field to its cap, normalize, clean, flag (instruction_like, etc.)             │
 │ 7 store query, results, sources, usage, health (own short transactions)                            │
 └────────────────────────────────────────────────────────────────────────────────────────────────────┘
        │                                               ▲
        ▼                                               │ JSON
   POST http://searxng:8080/search (private network)  /  https://api.deepseek.com/v1/tools/search
        │
 AgentResult: a fixed summary line ("Web search #123: 7 sources") + data.citations
        │
 summarize_request: the AI sees only the fixed line
        ▼
 Supervisor reply (no web text in it)  +  a separate "Web sources (unverified)" list rendered from data.citations
        │
 Research Center history   ·   "Promote" (operator)  →  pending JARVIS Brain item (web_source flag)  →  an admin reviews it
```

### 1.3 Networks

```
 existing:  [postgres] [db-init] [api] ─────────── internal (internal: true, no route out)
            [worker] ── internal + egress (internet; already today, for monitors, model API, SSH)
            [n8n] ── internal + egress          [dashboard] ── internal + edge (127.0.0.1:8090)

 new (only when research is on):
            [worker] ── research (internal: true) ── [searxng] ── research_egress (bridge, SearXNG alone)
                                                                       └──► DuckDuckGo, Brave, Wikipedia
 later, optional:
            [worker] ── research_fetch (internal: true) ── [research-fetch] ── fetch_egress ──► web pages
            [worker] ── egress ──► api.deepseek.com (HTTPS; key in a header)
```

- **What SearXNG cannot reach.** It is not on `internal`, so it cannot reach PostgreSQL, the API or n8n. It is not on the shared `egress` network, so it cannot reach n8n or the worker there [O]. [S2] confirmed the merged layout: the worker gains `research` and keeps `internal` and `egress`, and no new port is published.
- **What SearXNG can reach.** `research_egress` is a plain bridge. Like any bridge, it reaches the internet and also the home network: the router, a NAS, and anything listening on the PC's own LAN address. The `extra_hosts` lines only blank out Docker Desktop's two host names; they do not block IP addresses. The risk is low, because SearXNG's engine URLs are fixed and the image proxy is off and locked. The rule: never switch on a SearXNG engine or plugin that fetches a URL a caller supplies.
- **What the API cannot reach.** The API is not on `research`, so it never talks to SearXNG. It reads the health rows the worker writes.
- **A rule enforced in code, not by the network.** The worker already has internet access, so the network layout cannot force every web lookup through the gateway. The code must enforce that rule [O].

### 1.4 The worker's role
- It is the **only process** that calls SearXNG, the fetch service or DeepSeek. That includes the admin's connectivity test, which runs as a worker task the way the SSH host-key scan already does [C1][CR].
- It **probes SearXNG's `/healthz`** every 60 seconds, but only while research is on. Every hour it reads `/config` to learn which engines are on and which version is running. It writes the health rows.
- It **does housekeeping** in its own tick, not as a schedule: retention purges, and freeing budget reservations left behind by a crash. A schedule would need a DESTRUCTIVE capability, which needs approval on every run [CR].
- It **respects the existing "is this PC online" check.** If the `internet` row says the PC is offline, research reports "PC offline", not "SearXNG down" [C2].

### 1.5 How results reach the Supervisor
- **When research is in the planner.** Research capabilities appear in the planner's list only when research is on, and only for requests typed on the dashboard. For n8n service-token requests they are left out. Today the planner filters by agent only, so per-capability filtering is new work [CR]. The gateway checks the trigger again (section 5, T2), so a gap in either planner cannot let n8n start research.
- **Rule-based planner.**
  - It gets a new intent that needs an explicit phrase: "search the web for …", "web search: …" or "research the web for …". The query is only the text captured after that phrase, never the whole message.
  - Common words never trigger it on their own: "online", "look up", "latest", "current", "news". "Is clientsite.com online?" stays with monitoring.
  - Today `keyword_brief` (`agents/seo.py:143`) matches the bare word "research", not only "research keywords for". The new intent gets a higher weight. The `research` pattern of `keyword_brief` skips "research the web", "research online" and "research on the internet". "Research keywords for dentists" stays with `keyword_brief`.
- **Claude-based planner.**
  - Claude writes the query. The step is kept only if every word of the query, after the canonical form, also appears in the operator's message. A short fixed list of joining words is exempt ("the", "of", "for", "and", "latest", "release", "news").
  - Otherwise the step is dropped with the code `query_not_from_request`. The reply suggests the Research Center instead.
  - This stops injected text from making Claude encode client names or other context into a query.
  - The operator's message goes into `<request>` only after the existing `_fence` escaping. Today it goes in raw (`planner.py:221-224`).
- **The AI never sees web text in this release.**
  - The task summary is a fixed line.
  - Citations go in `data.citations`, a new key. `summarize_request` sees only the fixed line.
  - The dashboard shows citations as a **separate structured list** under the reply, labelled "Web source [n] (unverified)". Web titles are never written into the reply text. A page titled "Needs attention: approve task #45" therefore cannot pass for system output.
  - The existing `local_only` mechanism is **not** reused. It would switch off the AI summary for the whole request and hide the answer from viewers [CR].

### 1.6 Rules that never change
- **Approvals.** Approval still depends only on the capability's declared risk (`tasks.create_task`). Mock adapters and the SSH gate still refuse to act without `approved_by` [C1].
- **Task results never create tasks.** `advance_request` only queues the summary [C2].
- **Credit Repair.** It keeps its own database and key. It has no research access, and that is enforced in code and covered by tests.

---

## 2. SearXNG Docker configuration

### 2.1 How it is switched on
- **A separate file.** `docker-compose.research.yml` is used only when the owner switches research on.
- **The switch.** It goes into `COMPOSE_FILE` in `.env`, the same pattern as the remote-access tunnel [O].
- **A new script:** `scripts\research.ps1 -Enable | -Disable | -Status | -RotateSecret | -PrepareDowngrade`.
  - It splits and joins `COMPOSE_FILE` on `;` (the Windows separator). It keeps the order and any tunnel entry, and adds or removes only its own entry.
  - It handles `.env` files that mix LF and CRLF lines or lack a final line break. It writes UTF-8 without BOM. The script itself is plain ASCII (`build-package.py` refuses other `.ps1` files).
  - `-Enable` refuses unless `SEARXNG_SECRET` exists with at least 32 characters, and unless `research\searxng\settings.yml` is present.
    - *0.7.0 replaced this:* `-Enable` creates a missing `SEARXNG_SECRET` itself, in the same `.env` write as `COMPOSE_FILE`, after the `.env` copy; it still refuses a value shorter than 32 characters (see [RESEARCH_GATEWAY.md](RESEARCH_GATEWAY.md), section 2).
  - It refuses to run while the JARVIS test copy (`cc-brain-test`) or a JARVIS import container (`cc.brain-import`) is running. Enabling and disabling recreate the live worker and create or remove networks, so those runs would report "LIVE INSTALLATION CHANGED".
  - It warns if any existing Docker network overlaps the tunnel's fixed subnet `172.29.37.0/28`. The two new networks take addresses from Docker's default pools, so switching research on before the tunnel could later cause "Pool overlaps".
  - It finishes by calling `start.ps1 -NoBuild`.
- **Fixing the tunnel steps.** The tunnel's enable and rollback commands in `docs/REMOTE_ACCESS_PLAN.md` must change to do the same. Today they add the line only when it is missing, and their rollback deletes the whole line [O].
- **Why a separate file and not a profile in `docker-compose.yml`.** The JARVIS Brain test copy and the importer call Compose with explicit `-f` files. They therefore never see SearXNG, and the test copy's automated checks (every base service listed, worker on `internal` only, no internet) stay valid without changes [O].
- **SearXNG never blocks the core start.**
  - `start.ps1` pulls the SearXNG image only when `docker image inspect <tag@digest>` fails. An offline boot or a Docker Hub limit therefore does not matter once the image is on the PC.
  - It starts every other service first, with `COMPOSE_FILE` left as it is: `docker compose up -d` with the list from `docker compose config --services`, minus `searxng`. The worker keeps its research network and is not recreated.
  - Then it runs `docker compose up -d --no-deps searxng` on its own. If that fails (pull failed, settings missing), it prints a warning and exits 0. The gateway reports "unreachable".
- **A missing secret never blocks other scripts.** Compose reads every file in `COMPOSE_FILE` for every command. With `${SEARXNG_SECRET:?}` and no secret, even `backup.ps1` would fail. The new helper `scripts\compose-env.ps1` is used by `install.ps1`, `start.ps1`, `stop.ps1`, `backup.ps1`, `restore.ps1`, `status.ps1` and `uninstall.ps1`. If the research file is listed but the secret is missing or shorter than 32 characters, it sets `COMPOSE_FILE` for that script's process only, without the research file, and prints a warning. `.env` is not changed. The `:?` stays in the compose file: an empty `SEARXNG_SECRET` would be accepted by SearXNG as an empty HMAC key.
- **Enabling recreates the worker once, and so does disabling,** because it joins or leaves a network. Switch research on or off when no repair is running.

### 2.2 `docker-compose.research.yml` (copy-ready)

```yaml
# OPTIONAL Research Gateway search service (SearXNG). Not used unless research is switched on with
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\research.ps1 -Enable
# which adds this file to COMPOSE_FILE in .env (entries separated by ';'; other entries, such as the
# tunnel, are kept).
#
# What SearXNG can and cannot reach:
# - "research" (internal: true) is shared only with the worker, which calls http://searxng:8080/search.
#   PostgreSQL, the API, n8n and the dashboard are not on it.
# - "research_egress" is a plain bridge used by SearXNG alone, for its calls to the search engines.
#   Like any bridge it can also reach the home network and this PC's LAN address; the extra_hosts
#   lines below only blank out Docker Desktop's host names. Never switch on a SearXNG engine or
#   plugin that fetches a URL supplied by a caller (image proxy, URL-type engines).
# - No published port: nothing on this PC or the local network can open SearXNG.
# start.ps1 starts every other service first and this one on its own: if SearXNG cannot start,
# everything else runs as before.
# The official image is used unmodified (AGPL-3.0-or-later). Each installation pulls it from the
# registry; it is never bundled or re-hosted. It is configured only by the read-only folder
# research/searxng and the environment below. SEARXNG_LIMITER, SEARXNG_PUBLIC_INSTANCE,
# SEARXNG_IMAGE_PROXY and SEARXNG_METHOD would override settings.yml: never set them.

services:
  searxng:
    # Pinned by tag AND digest (Docker uses the digest). Tested in the sandbox on 2026-10-20.
    image: ${REGISTRY:-docker.io}/searxng/searxng:2026.10.4-d48c4b555@sha256:76b0bf285aca014c7191fc4d9234c4bfb358624ac33d8883833d496c059ec072
    restart: unless-stopped
    user: "977:977"
    read_only: true
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    environment:
      # Keep ':?' : an empty value would be accepted as an empty key.
      SEARXNG_SECRET: ${SEARXNG_SECRET:?run scripts/init-env.ps1 -AddMissing}  # 0.7.0: "run scripts/research.ps1 -Enable"
      FORCE_OWNERSHIP: "false"
      # The image listens on "::" (IPv6) by default and crashed at start without IPv6.
      GRANIAN_HOST: "0.0.0.0"
      GRANIAN_PORT: "8080"
      # No request lines (and so no query text) in the container log.
      GRANIAN_LOG_ACCESS_ENABLED: "false"
    volumes:
      # The whole folder, read-only: a single-file mount leaves /etc/searxng writable,
      # because the image declares it a VOLUME. Fails instead of creating an empty folder.
      - type: bind
        source: ./research/searxng
        target: /etc/searxng
        read_only: true
        bind:
          create_host_path: false
      - searxng_cache:/var/cache/searxng
    tmpfs:
      - /tmp:size=64m
    mem_limit: 512m
    cpus: 1.0
    pids_limit: 256
    # Docker Desktop's names for the Windows host point nowhere here (same as the tunnel overlay).
    # This blocks the names only, not the host's IP addresses.
    extra_hosts:
      - "host.docker.internal:192.0.2.1"
      - "gateway.docker.internal:192.0.2.1"
    logging:
      driver: json-file
      options:
        max-size: "1m"
        max-file: "3"
    healthcheck:
      # The image's own Python; no reliance on wget or curl being present.
      test: ["CMD", "/usr/local/searxng/.venv/bin/python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    labels:
      cc.research: "searxng"
    networks: [research, research_egress]

  worker:
    # Adds one network to the worker; its other networks stay (the same merge the tunnel overlay uses).
    networks:
      research: {}

networks:
  research:
    internal: true
  research_egress: {}

volumes:
  searxng_cache: {}
```

### 2.3 `research/searxng/settings.yml` (copy-ready)

```yaml
# SearXNG for the Command Center Research Gateway: a private, JSON-only search backend for the worker.
# No browser users and no public access. Mounted read-only. Changes ship only with releases.
# The secret is NOT in this file: it comes from SEARXNG_SECRET in .env.

use_default_settings:
  engines:
    keep_only:
      - duckduckgo
      - brave
      - wikipedia

general:
  debug: false
  instance_name: "Command Center research"
  enable_metrics: false

search:
  safe_search: 1
  autocomplete: ""
  favicon_resolver: ""
  formats:
    - json
  ban_time_on_fail: 5
  max_ban_time_on_fail: 120
  suspended_times:
    SearxEngineAccessDenied: 3600
    SearxEngineCaptcha: 3600
    SearxEngineTooManyRequests: 900
    cf_SearxEngineCaptcha: 1296000
    cf_SearxEngineAccessDenied: 86400
    recaptcha_SearxEngineCaptcha: 604800

server:
  limiter: false
  public_instance: false
  image_proxy: false
  method: "POST"

# Request fields cannot change these. "categories" is deliberately NOT locked:
# locking it would make SearXNG ignore the engines= field the gateway uses.
preferences:
  lock:
    - image_proxy
    - safesearch
    - autocomplete
    - favicon_resolver
    - method

valkey:
  url: false

outgoing:
  request_timeout: 3.0
  max_request_timeout: 6.0
  retries: 0

plugins: {}
```

And `research/searxng/limiter.toml`, which contains only comments:

```toml
# Empty on purpose. The limiter is off (server.limiter: false in settings.yml).
# This file only stops a "missing limiter.toml" warning at start-up.
```

### 2.4 Each choice in one line

Labels in the last column:
- **Proven [S]** or **Proven [S2]**: shown in a real sandbox run. [S2] used these exact files with a stand-in worker.
- **From code**: read in SearXNG's source, not run.
- **New**: must be checked on the development instance (section 7.4).

| Choice | Why | Proven? |
|---|---|---|
| Image `tag@sha256` behind `${REGISTRY:-docker.io}` | Exactly the bytes that were tested. The repo's mirror setting still works. | Proven [S]. The digest matched Docker Hub, and the pull through `mirror.gcr.io` worked. The tag's index digest was re-read on Docker Hub today. |
| `user: "977:977"` | The image sets no USER, so it would run as root. 977 is its own unprivileged user [X1]. | Proven [S][S2] |
| `read_only: true` | The container cannot change its own files. | Proven [S][S2] |
| `cap_drop: [ALL]`, `no-new-privileges` | No Linux privileges are needed. | Proven [S][S2] |
| `tmpfs /tmp` (64 MB) | SearXNG's SQLite caches default to /tmp [X1]. | Proven [S] |
| `GRANIAN_HOST 0.0.0.0` | The image default `::` crashed with "Address family not supported" without IPv6. | Proven [S], crash and fix both |
| `FORCE_OWNERSHIP false` | No ownership changes are attempted on start. The entrypoint prints an expected warning. | Proven [S][S2] |
| `GRANIAN_LOG_ACCESS_ENABLED false` | No request lines in the logs. | Proven [S2] (no access-log line naming a search) |
| Config folder mounted read-only (long syntax) | With a single-file mount, `/etc/searxng` stayed writable. | Proven [S2] |
| Named cache volume | `/var/cache/searxng` stays writable and survives restarts. | Proven [S] |
| 512 MB, 1 CPU, 256 processes | Caps. Idle use was 80–85 MiB with 8–11 processes and threads (two runs). | Proven at idle [S][S2]; not measured under load |
| `extra_hosts` to 192.0.2.1 | Docker Desktop's host names cannot reach the PC. Copied from the tunnel overlay. It blocks names only, not IPs. | Accepted [S2]; the effect needs Docker Desktop |
| Logs 1 MB × 3 | Bounded disk use. | Proven [S] (accepted and running) |
| Healthcheck with the image's Python | No dependence on wget or curl. | Proven [S][S2] |
| No `ports:` | Not reachable from the PC or the local network. | Proven [S][S2] (merged config publishes only the existing 127.0.0.1:8090 and 127.0.0.1:5680) |
| Networks `research` + `research_egress` | Private link to the worker, plus outbound traffic used only by SearXNG. | Layout proven [S2]. Reachability tests with the real core stack are new (section 7.5) |
| `restart: unless-stopped` | Same as the other services. | Recovery after `docker restart` proven [S][S2]. The policy itself (a crash or a Docker restart) was not exercised; a kill test is in section 7.4 |
| Worker joins `research` | The worker reaches SearXNG by name. | Proven [S2] with a stand-in worker; the merged config keeps the worker on `internal` and `egress` |
| `keep_only` duckduckgo, brave, wikipedia | Two of the three default-on general web engines, plus Wikipedia. The third, `google cse`, uses Google's Custom Search page without an API key; it was not checked. `wikidata` is also on by default (infoboxes only). Startpage and Mojeek stay off under `keep_only` [S][X2]. Wikipedia appears as an infobox, which the gateway turns into one reference result. | Proven that exactly these three load [S]; defaults read at the pinned tag [RT]; real results not proven (no internet) |
| `formats: [json]` | The JSON API is on and the HTML results page is off. | Proven [S][S2]: JSON 200, HTML 403 |
| `limiter: false` | The limiter needs Valkey. It allows only 4 non-HTML API requests per hour per client network (/32 for IPv4). It also rejects requests without `text/html` in Accept, and some bot-like User-Agents [X2][RT]. The gateway does its own rate limiting. | Setting accepted [S]; behaviour from code |
| Empty `limiter.toml` | Stops a start-up warning. | New |
| `public_instance: false` | `true` forces the limiter and the image proxy, and exits without Valkey [X2]. | Setting accepted [S]; behaviour from code |
| `image_proxy: false` plus `preferences.lock` | `false` only stops SearXNG from writing proxy links. The proxy route (`/image_proxy` at the pinned tag, `/image` in master) stays. It is guarded only by an HMAC with `SEARXNG_SECRET`, follows redirects and has no private-address filter [RT]. The lock stops a request from switching it on, and from changing safe search, autocomplete, favicons or the method. The `categories` lock alone affects `engines=`, and it is not set [RT]. | `image_proxy` accepted [S]; route and lock from code; lock is new |
| Secret only from `SEARXNG_SECRET` | Compose refuses to start without the variable. SearXNG exits if the key is still "ultrasecretkey". | Compose refusal proven [S]; the exit from code [X1][X2] |
| `enable_metrics: false` | Nothing is recorded. The `/stats` and `/stats/errors` routes still exist, but stay empty [RT]. | Accepted [S] |
| `safe_search: 1`, `autocomplete: ""` | Moderate filter. Nothing is sent per keystroke. | Accepted [S] |
| `request_timeout 3.0`, `max_request_timeout 6.0`, `retries 0` | Each search waits about 3 seconds for the engines. 6 s caps only a `timeout_limit` sent by a caller, and the gateway sends none [RT]. Retries never hammer an engine. | Accepted [S]; timing from code |
| `suspended_times` set explicitly | Pins today's values against changes on upgrade. Waits after "access denied" (60 min instead of 3) and 429 (15 min instead of 3) are longer than shipped, to protect the owner's home IP. | Accepted [S2]; shipped values read at the tag [RT] |
| `plugins: {}` | No plugins. The settings loader replaces the whole plugin list [RT]. The start-up download of ClearURLs rules comes only from the tracker-remover plugin, so it should stop. The gateway strips tracking parameters itself. | Accepted [S2]; the ClearURLs error seen in [S] was absent from [S2]'s warnings (indirect proof) |
| No Valkey | Not needed when the limiter and public mode are off [X1]. | Proven [S] |

### 2.5 `SEARXNG_SECRET`: how it is made and stored
- **Generated** by `scripts\init-env.ps1`, like every other secret: `New-Secret 32`, which is 32 random bytes from the system's CSPRNG in base64url (43 characters). `init-env.sh` gets the same with `secrets.token_urlsafe(32)`.
- **Added on upgrade.** It goes into the fresh `.env` template and into the `-AddMissing` upgrade list (`$upgradeKeys`), so an upgrade adds it without touching existing lines. `preflight.ps1` lists it as an "addable" key.
  - *0.7.0 replaced this:* an upgrade never adds it (`.env` stays byte for byte while research is off), and preflight neither requires nor adds it; `research.ps1 -Enable` creates it when the search service is switched on (see [RESEARCH_GATEWAY.md](RESEARCH_GATEWAY.md)).
- **Stored only in `.env`.** That file is private and already part of the backup instructions. It is passed only to the `searxng` service, never to `x-api-env`, so the API and worker never see it.
- **Not in the settings file.** `build-package.py` refuses any packaged file that contains a `.env` secret value [O].
- **What it protects.** It is the only key to SearXNG's image-proxy route, which is still served. It also keys SearXNG's small engine-data cache [R].
  - Rotating it is harmless; the cache is rebuilt.
  - Leaking it is not harmless. Anything on the `research` network could then make SearXNG fetch any URL from `research_egress`, including home-network addresses.
- **Rotating it.** Use `research.ps1 -RotateSecret`. It replaces the value in one write and then recreates only `searxng`. Never delete the line by hand while research is on: until it is back, every compose command would skip the research file (section 2.1).

### 2.6 Upgrades: pinning a new tag
SearXNG is a rolling release with several tags a day. Its engines can break when the sites they read change [X1][X2]. The steps:
1. In the sandbox, run the development-instance checks (section 7.4) against the new tag. Check that its JSON keys, default engines and `engines=` handling still match. Read the changed source files at the new commit.
2. Read the tag's index digest from Docker Hub and compare it with GHCR and the mirror.
3. Change the single `image:` line in `docker-compose.research.yml` (tag and digest).
4. Ship it in the next Command Center release, or in a small research-only patch release.
5. Install as usual. `start.ps1` pulls the new image only because the pinned digest is missing. If the pull fails, the core starts without SearXNG.
6. Rollback means the previous release folder, which keeps the old pin.

**Never** use `latest`. Never build a custom SearXNG image, and never bundle or re-host the image (AGPL, section 4.1).

**Settings changes ship only with releases.** The folder `research/searxng` is inside the release folder. An upgrade extracts a new folder and copies only `.env` [O]. A change the owner made by hand (for example adding `wikidata`) would quietly disappear. Engine choices (decision 17) are therefore release changes.

### 2.7 Other files that change for the container [O]

| File | Change |
|---|---|
| `init-env.ps1/.sh`, `preflight.ps1` | Add `SEARXNG_SECRET`. Update preflight's disk note: the 383 MB image plus database growth (section 6). *0.7.0 replaced this:* only a new `.env` gets it; `research.ps1 -Enable` creates it on an upgraded one (see [RESEARCH_GATEWAY.md](RESEARCH_GATEWAY.md)). |
| New `compose-env.ps1` | Shared helper (section 2.1). Used by `install.ps1`, `start.ps1`, `stop.ps1`, `backup.ps1`, `restore.ps1`, `status.ps1`, `uninstall.ps1`. |
| `start.ps1` | Pull only when the digest is missing; start the other services first, then `searxng` on its own; a warning, never a failure. |
| `status.ps1` | Shows the SearXNG container's state and health from `docker inspect --format '{{.State.Health.Status}}'`, filtered by the label `cc.research`. No sign-in needed. (`status.ps1` does not sign in, and `/api/system/status` needs a signed-in user.) |
| `smoke.ps1` | Reports research as information only: off, healthy, degraded or all engines failing. It fails only on an API error, never on an engine's state. `install.ps1` stops when the smoke test fails. |
| `uninstall.ps1` | Removes the networks `infinite-command-center_research` and `infinite-command-center_research_egress` and the volume `infinite-command-center_searxng_cache`, whether or not the research file is loaded. Prints one line saying the `searxng/searxng` image is kept, with the command to remove it. |
| `brain-common.ps1` | For the single live service `com.docker.compose.service=searxng`, it leaves only the start time out of the fingerprint. It keeps the container ID, image ID and configuration, and prints the excluded entry. A SearXNG restart during a JARVIS test then does not report "LIVE INSTALLATION CHANGED", but a changed SearXNG still does. |
| `brain-test.ps1` | The seed sets the research mode to `off`, and its "something that could act is still on" check includes it. |
| `build-package.py` | Add the new files to its REQUIRED list. |
| `generate-sbom.py` | Add `searxng/searxng` (AGPL-3.0-or-later). |
| `docker-compose.testing.yml` | A mock search service. |
| Docs | README, DEPLOY_WINDOWS, REMOTE_ACCESS_PLAN (the `;` editing rule and the subnet note), COMMERCIALIZATION (a new AGPL row), `.env.example`, the 0.7.0 release notes (rollback steps). |

`docker-compose.brain-test.yml` and `docker-compose.brain-import.yml` need no change.

---

## 3. Research Gateway integration plan

### 3.1 Provider interface

```python
class ResearchProvider(Protocol):
    name: str                     # "mock" | "searxng" | "deepseek" | "local_fetch"
    async def search(self, q: SearchRequest) -> ProviderSearchResult: ...
    async def fetch(self, url: str) -> ProviderFetchResult: ...          # "not supported" for searxng
    async def health(self) -> ProviderHealth: ...
```

- Every provider takes an injectable `httpx` transport, the same test pattern as `FakeWeb` and `ReadOnlyHttpClient(transport=...)` [C2].
- No provider may be used unless the owner switched it on. There is no silent failover to an unapproved provider.

### 3.2 SearXNG provider
- **A narrow client of its own.** It does not use `ReadOnlyHttpClient`, which refuses private addresses [O].
  - The address is fixed to `http://searxng:8080`. `check-config` refuses any other value, so queries can never go to a public SearXNG.
  - `check-config` also refuses `searxng` and `research-fetch` in `MONITOR_ALLOWED_PRIVATE_HOSTS`. Otherwise monitoring's `check_url`, which n8n can start, could query SearXNG without screening.
  - Paths: `POST /search`, `GET /healthz`, `GET /config` only.
  - `follow_redirects=False`, `trust_env=False`, responses capped at 2 MB.
- **The request.** `POST /search` with exactly these form fields:
  - `q`: the canonical, screened text;
  - `format=json`;
  - `engines=`: the allowed engines that are not cooling down, comma-separated;
  - `language`, `time_range` (day, week, month or year), `pageno`.
- **Fields it never sends.** It never sends `categories` or any `category_*` field: when both are present, SearXNG **adds** the category's engines to the list, so a cooling engine would still be queried [RT `webadapter.py`]. It never sends `safesearch` (locked), `timeout_limit`, or any other preference field.
- **A fixed header.** It sends `X-Real-IP: 127.0.0.1`. Any valid value silences the harmless "X-Forwarded-For nor X-Real-IP header is set!" line (confirmed in code [RT]).
- **Why POST.** POST keeps the query out of URLs. httpx logs full URLs at INFO in the worker [C2].
- **Search syntax.** SearXNG reads its own syntax inside `q`:
  - `!engine` and `!category` change which engines run;
  - `:xx` changes the language;
  - `!!bang` returns a redirect even for JSON [RT `webapp.py:663-664`].

  The screen refuses any word that starts with `!`, `:` or `<`, with code `search_syntax`.
- **Answers.**

  | SearXNG answer | Gateway status |
  |---|---|
  | 200 with results | `ok`, or `partial` if some engines are in `unresponsive_engines` |
  | 200, no results, some engines answered | `empty` |
  | 200, no results, **every** enabled engine unresponsive or "Suspended: …" | `provider_down`; not cached, not counted as "no results" [S obs. 7] |
  | Every allowed engine is cooling down (gateway) | `provider_down` with the cool-down end time; SearXNG is not called |
  | Any 3xx | `bad_request` (code `searxng_redirect`); no retry |
  | 403 | `provider_misconfigured` (JSON switched off); no retry |
  | 400 `{"error": …}` | `bad_request`; no retry |
  | 500 `{"error": "search error"}` | Retry once |

- **What it never relies on.** `number_of_results` is absent from the JSON at the pinned tag. The seven top-level keys were seen in the sandbox: answers, corrections, infoboxes, query, results, suggestions, unresponsive_engines [S][S2][RT]. The gateway reads only `results`, `infoboxes` and `unresponsive_engines`.

### 3.3 DeepSeek provider: optional fallback and fetch

**Every fact here is unverified (from search excerpts) [D].**

**Client**
- Pinned to `https://api.deepseek.com`, POST only, paths `/v1/tools/search` and `/v1/tools/fetch`.
- No redirects; a redirect is an error. `trust_env=False`; connects through `public_only_transport`; TLS always verified [DR].
- The key comes only from the existing encrypted secrets store (`deepseek_api_key` in `customer_settings.SECRETS`, Fernet with `CC_SECRETS_KEY`). It is decrypted per call and sent only in the `Authorization: Bearer` header.
- There is no `.env` fallback. The key never goes to the browser, Git, logs, task rows or prompts.
  - The browser cannot call DeepSeek anyway, because of the CSP `connect-src 'self'` [C2].
  - The Settings page shows only the last 4 characters.
  - The DeepSeek key needs its own format check (length and characters only), because the existing check is written for Anthropic keys [C1].
- **It is a full API key.** It also pays for DeepSeek model calls from the same balance [CR]. The dedicated account with a small balance is therefore the real spending limit.

**Search**
- Sends `query` and `count` (default 10), and nothing else.
- The query is at most 128 characters. Longer queries are refused, never cut, so the screened text is exactly the sent text.
- `ori_question` is never sent.
- `localization` is never sent, because it would hand DeepSeek an IP address [D].
- **No search operators of any kind.** The excerpts list `site:`, `-site:`, `-term`, phrases, `lang:`, `after:` and `before:` together; all are equally unconfirmed [D]. Until the docs are opened, the DeepSeek screen refuses them.

**Fetch**
- Sends `url` only, after the URL checks in section 5 (T4).
- URLs longer than 2,048 characters are refused with a reason code, never cut. DeepSeek's documented limit is 8,192 characters. The gateway rebuilds the URL from its parsed parts and sends that canonical form, so the screened URL and the sent URL are identical [CR].

**Errors and retries**
- Our policy: retry only on 408, 429 and 5xx, at most 2 times, with back-off. `Retry-After` is honoured up to a cap if DeepSeek sends it.
- The total time stays well inside the capability's timeout [CR].
- 402 stops all DeepSeek calls ("balance empty"). 401 marks the key invalid. Other 4xx errors are not retried.

**Validation**
- Types are checked strictly.
- `rerank_score` and `authority` values outside their ranges are clamped, not treated as failures. The API was opened on 2026-10-17 and may still change [CR].

### 3.4 Local fetch service: optional, separately secured, later phase
A separate container, `research-fetch`. It is the "separately secured fetch service" the owner asked for.
- **Image and environment.**
  - It uses the already-built backend image (`infinite-command-center/backend:local`, command `python -m app.research.fetch_service`) with `pull_policy: never`, like the worker.
  - It has its own healthcheck. The image's own healthcheck probes the API on port 8000 and would stay "unhealthy" forever.
  - It carries the label `cc.research: "fetch"`.
  - Its environment holds **no** secrets: no `DATABASE_URL`, no keys, no Credit Repair settings. This matters because the worker's environment holds every deployment secret, including `CREDIT_REPAIR_DATA_KEY` [C1]. The module must start with `config.py`'s defaults only.
- **Hardening.** Read-only, `cap_drop: [ALL]`, `no-new-privileges`, tmpfs /tmp, 256 MB memory and 64 processes (guesses, not measured), no ports, the same `extra_hosts` lines as SearXNG.
- **Networks.** `research_fetch` (`internal: true`, worker and fetcher only) plus `fetch_egress` (fetcher only). It shares no network with SearXNG.
- **Logging.** The `httpx` and `httpcore` loggers are at WARNING. It logs only codes, never URLs or text.
- **Behaviour.**
  - GET only; ports 80 and 443; no user name or password in the URL; no IP literals.
  - `resolve_public` with an **empty** allow list on every hop, connecting only to checked addresses (`PublicOnlyBackend`, which resists DNS rebinding).
  - Redirects are followed by hand, at most 5 hops, each re-checked and re-screened, as `SiteChecker._walk` does [C3]. For an agent with a domain allow list, the list is checked again on every hop.
  - 600 KB body cap, 20 s per read, 60 s overall.
  - Only `text/html` and `text/plain`. Text is extracted with the existing `PageParser` (no scripts or styles).
  - **Cleaning happens here.** The text is cut to 100 KB first. Then `text.clean()` and `instruction_flags()` run in a subprocess with a hard 10-second kill. The Brain cleaner's table check grows with the square of the input: 100 KB took 1.84 s and 200 KB took 7.06 s in a read-only probe [CR]. Python's `asyncio.wait_for` cannot interrupt CPU-bound work, so it must never run on hostile pages inside the worker's single event loop.
  - It returns `{final_url, status, title, text, truncated, redirects, flags}`. No cookies and no JavaScript.

### 3.5 Fallback rules
- **Default: `manual`.** When SearXNG is `provider_down`, the Research Center shows "Retry with DeepSeek" to operators, for agents whose policy allows DeepSeek.
- **`auto`.** An owner decision. It retries on DeepSeek only for `provider_down`, `unreachable` or a timeout. It never does so for `empty`: a real "no results" is not sent to a second, paid provider.
- **A stricter data rule for DeepSeek.** The query that goes to DeepSeek must pass the stricter DeepSeek rule (section 10): class G only. That means no owner or client business names, no software names or version numbers, no CVE numbers and no search operators.
- **Fetch order (later).** Local fetch service first. DeepSeek Fetch is used only if the local fetch failed, the owner switched DeepSeek Fetch on and the agent's policy allows it.

### 3.6 Normalized result schema

```json
{
  "query_id": 123,
  "status": "ok | partial | empty | provider_down | unreachable | bad_request | blocked | refused_budget | refused_rate | refused_policy | failed",
  "provider": "searxng",
  "provider_version": "2026.10.4-d48c4b555",
  "cached": false,
  "query": "WordPress 6.9 security release",
  "options": {"language": "en-US", "time_range": "month", "count": 10},
  "retrieved_at": "2026-10-20T14:03:11Z",
  "unresponsive": [{"engine": "brave", "reason": "timeout"}],
  "results": [
    {
      "rank": 1,
      "citation": "[1]",
      "result_id": 4567,
      "url": "https://wordpress.org/news/2026/10/...",
      "domain": "wordpress.org",
      "title": "WordPress 6.9.1 Security Release",
      "snippet": "…",
      "type": "web | reference",
      "source": {"provider": "searxng", "engines": ["duckduckgo", "brave"], "site_name": null},
      "published_at": "2026-10-14T00:00:00Z",
      "retrieved_at": "2026-10-20T14:03:11Z",
      "provider_score": 2.5,
      "authority": null,
      "flags": ["instruction_like"]
    }
  ]
}
```

| Field | From SearXNG [X2][RT] | From DeepSeek [D] (unverified) |
|---|---|---|
| url, title | `url`, `title` (title cut to 200 characters) | `url`, `title` |
| snippet | `content` (cut to 1,200 characters) | `snippet` (`summary` kept separately, for possible later use) |
| source | `engine` / `engines` | `site_name` (`site_icon` never stored or shown) |
| published_at | `publishedDate` (ISO; often null for web results) | `date` (RFC 3339 or null) |
| retrieved_at | our clock, UTC | our clock, UTC |
| provider_score | `score` | `rerank_score` (0–3) |
| authority | — | `authority` (−1 to 4) |
| rank | the gateway's position after de-duplication | same |
| type `reference` | each `infobox` (Wikipedia) becomes one result with its URL | — |

**Cleaning, in this order.**
1. Cut every field to its cap **before** any cleaning: title 200 characters, snippet 1,200, infobox text 1,200.
2. Strip NUL bytes, control characters and invisible characters.
3. Run `text.clean()` for secrets and contacts.
4. Set `instruction_like` from `instruction_flags()`.
5. Keep only links that start with `http:` or `https:`.

### 3.7 Citations
- Each result gets a number `[n]` within its query, plus a stable `result_id`.
- **The list.** Supervisor pages and the Research Center show citations as a structured list, each one prefixed "Web source [n] (unverified):". Each entry shows the title, domain, published date (may be empty), retrieved date and time (UTC), provider and engines, and full URL.
- **Rendering.** Each web string goes through `brain.text.one_line`, loses Markdown control characters (`#`, `*`, `_`, backticks, `[`, `]`, `>`, leading `-`) and is length-capped. It is rendered as plain text, never through the Markdown component, and never inside the reply text.
- Each page is stored once in `research_sources`, keyed by a hash of its normalized URL, so it can be cited by many queries.
- A fetched page (later) is labelled "requested URL", with the final URL when the local fetch service saw one. DeepSeek Fetch returns no final URL [D].

### 3.8 Timeouts and retries

| Call | Timeout | Retries |
|---|---|---|
| Gateway to SearXNG | 10 s overall. SearXNG itself waits about 3 s for the engines [RT]. | Once on connection error or 500, after 1 s |
| Gateway to fetch service (later) | 70 s overall | None |
| Gateway to DeepSeek (later) | Connect 10 s, read 30 s, 45 s overall | Section 3.3 |
| Research capability | `timeout_seconds` 90 | — |

- Budget reservations and query-log rows are written in their own short transactions, so a task timeout cannot erase them [CR].

### 3.9 Caching and de-duplication
- **Cache key.** sha256 of: kind, canonical query (lower case, spaces collapsed), language, time range, page and count. Safe search is fixed in settings. The provider is not part of the key, so a cached answer serves the same question from either provider. The key never includes who asked.
- **How long entries live.**

  | Kind | Lifetime |
  |---|---|
  | Search | 6 hours |
  | Search with `time_range=day` | 1 hour |
  | Fetch (later) | 24 hours |
  | "No results" | 1 hour |
  | Failures | Never cached |

- **Duplicate requests.** Identical requests that run at the same time are merged with a PostgreSQL advisory lock on the cache key.
- **Duplicate results.** SearXNG already merges the same URL from different engines [X2]. The gateway also normalizes URLs before it de-duplicates: lower-case scheme and host, no default port, no fragment, no trailing slash, and common tracking parameters removed (`utm_*`, `fbclid`, `gclid`, `mc_eid`).
- **Cache hits** are free, are recorded as `cached`, and are checked against the asking agent's domain policy.

### 3.10 Rate limits and budgets
All counters are atomic in PostgreSQL: `UPDATE … WHERE used < cap RETURNING`, in their own short transaction, committed before the call. A crashed worker's reservation is freed after 10 minutes [CR].

**Expected volume (baseline).** A small agency, research started only from the dashboard: about 10 to 50 searches per working day, or about 220 to 1,100 a month. Supervisor-planned steps count against the person who typed the request.

| Limit | SearXNG (no money; protects the home IP from upstream blocks) | Local fetch (later) | DeepSeek (money; later) |
|---|---|---|---|
| Spacing | 1 query per 3 s, bursts of 3 | 1 per s; 1 per domain per 10 s | At most 1 search and 1 fetch per s; 1 fetch per domain per 10 s |
| Global per day | **50 at first.** This is an unmeasured guess: raise it toward 200 only after 2 to 4 weeks of engine-suspension data (decision 4d). | 30 | 50 requests (search and fetch together) |
| Per agent per day | Section 10. The agent caps add up to more than the global cap; the global cap binds first, and the agent caps stop one agent from using all of it. | Section 10 | Section 10 (they add up to exactly 50) |
| Per person per day | 30 | 20 | 20 |
| Money | — | — | $1.00 a month, or CNY 7 if the account is billed in CNY. Warnings at 80% and 100%. 402 stops everything. |

- **Which DeepSeek cap binds.**
  - At the full 50 a day, the money cap stops DeepSeek after about 1,333 requests (USD) or 1,400 (CNY), about day 27 of a month. Both caps therefore mean something.
  - DeepSeek's own limit is account-wide and shared by every key on the account. That includes any other use of the account, such as coding tools. The sources disagree on its size: 5 per second (copy of the guide, DeepSeek's own notes) or 100 searches and 25 fetches per second (newer excerpts) [CR][D]. Our pacing stays at 1 per second or less, and 429 is handled either way.
- **What counts as billed.**
  - Every DeepSeek request that got an HTTP 200 with a non-empty result counts as billed, even if the gateway then rejects it.
  - A client-side timeout after the request was sent counts as "possibly billed" against the cap. DeepSeek bills by its own outcome [D][CR].
  - With the owner's approval, a monthly check of DeepSeek's balance endpoint can reconcile the totals.
- **Price storage.** The price is stored as an integer in micro-units **together with its currency** (USD or CNY), its source URL and the date it was checked. The monthly cap uses the same currency.
- **Settings storage.** The existing settings framework supports only text, choice, integer and timezone values, and draws every setting in the Business card [CR]. Lists (domains, trigger sources) live in `research_policy`. Research settings get their own endpoint and a "Research" card.
- **When a mode change takes effect.** The worker reads the research mode without a cache before each call, so "off" takes effect at once.

### 3.11 Provider health monitoring
- **`research_searxng` row** (`integration_status`, written by the worker):
  - `last_ok_at` and `last_error`;
  - the version from `/config`, compared with the pinned tag;
  - which engines are enabled;
  - each engine's state: `ok`, `failing`, or `suspended` with its reason and end time;
  - consecutive failures.
- **Comparing versions.** `/config` reports `2026.10.4+d48c4b555`, possibly ending in `+dirty`. The Docker tag uses `-` [R `version.py`]. The gateway replaces `+` with `-` and removes a `+dirty` suffix before comparing. `provider_version` keeps the tag form.
- **SearXNG states shown to people.**

  | State | Meaning |
  |---|---|
  | Healthy | Working |
  | Degraded | Some engines failing or suspended |
  | All engines failing | `/healthz` works, but every engine was unresponsive in the last 3 queries or is suspended |
  | Unreachable | `/healthz` fails |
  | Off | Research switched off |

- **How long engines stay suspended with this configuration** [RT]:

  | Cause | Suspension |
  |---|---|
  | Timeout or connection error | 5 s |
  | HTTP 429 (too many requests) | 15 min |
  | Access denied (HTTP 402/403) or CAPTCHA | 60 min |
  | Cloudflare access denied | 1 day |
  | reCAPTCHA | 7 days |
  | Cloudflare CAPTCHA | 15 days |

  This state lives in SearXNG's memory, so a container restart clears it [RT `processors/abstract.py:29`].
- **The "all engines failing" case.**
  - The message is built from the actual reason and its configured time. For example: "SearXNG is running, but DuckDuckGo asked for a CAPTCHA (paused for 60 minutes) and Brave refused access (paused for 60 minutes). Results come back at 15:20." After a Cloudflare or reCAPTCHA block, the message says it can take 1 to 15 days.
  - The gateway does not retry those queries.
  - Fallback follows section 3.5.
- **The gateway's own engine cool-down.**
  - It keeps a cool-down per engine, copied from SearXNG's reason and time, in the `research_searxng` row.
  - It leaves cooling engines out of the `engines=` list and sends no `categories`.
  - Without it, a restart (which clears SearXNG's memory) would make SearXNG hammer a site that just blocked the PC.
- **DeepSeek health** comes only from real calls (status, 401/402/429 flags, last success). It is never probed, because probes cost money.
- **Surfacing.**
  - `/api/research/health` and the dashboard's Service status panel gain rows ("Web research: SearXNG", "Page fetch", "DeepSeek") [O].
  - `status.ps1` reads the container's health directly (section 2.7).
  - The daily briefing and health review get numbers and codes only.

### 3.12 Query log and audit log
- **Query log (`research_queries`).** Every attempt gets a row, including blocked ones: time, agent, capability, person, trigger, provider, mode, status, reason code, result count, cached, billable, cost, latency.
  - **Blocked attempts store a reason code only: no text and no hash.** A plain hash of an SSN, a phone number or a lead's name could be reversed by trying values.
  - When a lead or ticket match caused the block, the row stores that lead or ticket **ID** (operators can see it). A poisoned lead record therefore shows up.
  - The worker writes it, in its own transactions.
- **Audit log** (`audit.record`, API actions only, IDs and counts only, never text [C1]):

  | Entry | When |
  |---|---|
  | `research.settings_changed`, `research.mode_changed` | Settings and mode changes (typed confirmation for live and for DeepSeek) |
  | `research.policy_changed` | Per-agent policy changes |
  | `research.promoted` | An operator promotes a source |
  | `research.purged` | An admin purges research data |
  | `research.test_requested` | An admin starts a connectivity test |
  | `secret.set`, `secret.removed` | Existing entries, used for `deepseek_api_key` |

- The existing `task.created` entry already leaves out parameters. The existing `supervisor.request` entry keeps the first 500 characters of the operator's message, as today for every request.

### 3.13 Screening before anything is stored
- **Why.** Task parameters are written when the task is created, before the gateway runs [CR]. The plan is written to `supervisor_requests.plan` and to the `plan_request` result, and its notes are logged as warnings.
- **Direct runs** are screened in the API (`api/work.py`) before `create_task`. A blocked query is refused with a reason code, so no task row ever holds the text.
- **Planned steps** are screened in `plan_request` before `create_task` **and before `plan.as_dict()` is stored**.
  - A blocked step is removed from `plan.steps`.
  - Its rationale is dropped (Claude's rationale, up to 500 characters, usually restates the query).
  - Its note is a code only, for example "dropped supervisor.web_search: blocked (contact_detail)". Notes are logged as warnings today (`supervisor/agent.py`), so they must hold codes only.
- **What viewers see.**
  - `task_view` hides the parameters and `result.data` of research capabilities from viewers.
  - `request_view` hides the parameters and rationale of research steps in `plan`.
  - Today `task_view` strips only `answer` and `memory_notes`, and `request_view` only rewrites local-only summaries. Both files are in the change table.
- **What remains.** The operator's own request text stays in `supervisor_requests` and in the audit entry, as for any request today.

### 3.14 New tables (one migration, new tables only)
- **Design rules** [CR]:
  - Every statement is idempotent.
  - **No foreign keys into tables that existed before**, so `restore.ps1 --clean` of older backups keeps working. References to tasks, requests and memory items are plain `BIGINT` columns that the code checks.
  - No change to any existing table: promoted items use the existing `origin='promoted'` (section 3.17).
- **Tables.**

  | Table | Main fields |
  |---|---|
  | `research_queries` | id, created_at, finished_at, task_id, request_id, agent, capability, requested_by, trigger_source CHECK (`dashboard`, `supervisor`, `retry`, `test`), kind CHECK (`search`, `fetch`), query_text (canonical, ≤128; NULL when blocked), target_url (≤2,048; NULL when blocked), blocked_by_ref (for example `lead:42`; NULL otherwise), options JSONB, provider, mode CHECK (`mock`, `live`), status, reason_code, http_status, result_count, cache_key (NULL when blocked), billable CHECK (`no`, `yes`, `possibly`), unit_price_micro, currency, cost_micro, latency_ms, fallback_of (id) |
  | `research_results` | id, query_id → research_queries ON DELETE CASCADE, rank, source_id → research_sources ON DELETE SET NULL, snippet (≤1,200), provider_score, authority, engines JSONB, type |
  | `research_sources` | id, url, url_sha256 UNIQUE, domain, title (≤300), site_name, published_at, first_retrieved_at, last_retrieved_at, provider, fetched_text (≤100 KB; fetch only), content_sha256, truncated, flags JSONB, status CHECK (`active`, `withheld`, `purged`), expires_at, promoted_item_id |
  | `research_cache` | cache_key PK, kind, provider, response JSONB (already cleaned), created_at, expires_at, hits |
  | `research_usage_daily` | PK (day, provider, agent, kind), reserved, sent, billable, possibly_billable, cache_hits, blocked, failed, cost_micro, currency |
  | `research_rate` | provider PK, last_sent_at, burst_tokens |
  | `research_policy` | agent PK, search_enabled, fetch_enabled, deepseek_search, deepseek_fetch, daily caps, data_classes JSONB, fetch_allow_domains JSONB, blocked_domains JSONB, updated_by, updated_at |

- **In the Credit Repair database** (its own migration): `cr_research_index (client_id, kind, mac)`.
  - `mac` is an HMAC-SHA256 with a key derived from `CREDIT_REPAIR_DATA_KEY` (HKDF, label "research-index").
  - Kinds: name bigrams and full name, e-mail, phone digits, reference.
  - The Credit Repair module fills it when a client is created or changed, and once for existing clients.
- **Existing tables, new rows only.**
  - `integration_status` rows `research_searxng`, `research_fetch`, `research_deepseek`.
  - `secret_values` row `deepseek_api_key`.
  - `app_settings` keys: mode (`off`, `mock`, `live`), fallback (`off`, `manual`, `auto`), fetch provider, retention days, the DeepSeek price in micro-units with its currency, source URL and the date it was checked.
- **`credit_repair`** is refused in code, whatever `research_policy` says.

### 3.15 API endpoints
All use the existing session cookie, the CSRF header and the existing role checks. No query text goes in a GET query string, because uvicorn's access log records query strings [DR].

| Method and path | Who | What |
|---|---|---|
| `POST /api/agents/{agent}/tasks` (existing) | operator | Runs a research capability; screened before the task is created. Refuses internal capabilities. |
| `GET /api/research/health` | signed-in user | Mode, provider health, engine states, "PC online" |
| `GET /api/research/usage?from=&to=` | operator | Today's usage against caps; counts and cost per day, agent, provider |
| `GET /api/research/queries?agent=&status=&provider=&from=&to=` | operator | History |
| `GET /api/research/queries/{id}` | operator | One query with its citations |
| `GET /api/research/sources/{id}` | operator | Source detail; content shown as plain text; promoted item shown only through the Brain's own `person_items` filter |
| `POST /api/research/queries/{id}/retry-deepseek` | operator | Manual fallback (only when the policy allows it) |
| `POST /api/research/sources/{id}/promote` | operator | Creates a pending (or quarantined) Brain item: `knowledge` or `agent_note` only, a named non-Credit-Repair agent |
| `GET /api/research/blocked` | operator | Time, agent, reason code and the lead or ticket ID only |
| `GET` / `PATCH /api/research/settings` | admin | Mode, fallback, caps, retention; typed confirmation for `live` and DeepSeek |
| `GET` / `PATCH /api/research/policy/{agent}` | admin | Per-agent matrix |
| `PUT` / `DELETE /api/settings/secrets/deepseek_api_key` (existing) | admin | Set or remove the key |
| `POST /api/research/test` | admin | Queues the internal connectivity-test task (source `research_test`). It is the only way to start it: the capability is refused by direct runs, both planners and retries. |
| `POST /api/research/purge` | admin | `{older_than_days, include_usage, confirm: "PURGE RESEARCH"}` |
| `GET /api/brain/items?source=web` (change) | per Brain rules | Lets web-sourced items be reviewed separately |

Viewers get only `/api/research/health` unless the owner decides otherwise (decision 13).

### 3.16 Research Center screens
Route `/research`, in the Operations sidebar after "JARVIS Brain" [C2]. Operators and admins only (viewers: decision 13).

1. **Overview.** Status per provider (SearXNG, Page fetch, DeepSeek), with engine states, the suspension end times, and "PC offline" and "all engines failing" messages. Also: today's usage against caps per agent, this month's DeepSeek cost against the budget (with its currency), and recent errors as plain codes.
2. **New research** (operators).
   - Choose an allowed agent, then Search (Fetch later).
   - The query box has a 128-character counter. Options: results count, language, time range.
   - A local pre-check warns before sending ("This would be blocked: it contains an e-mail address"). It uses patterns only and never downloads lead, client or archive data. The server's check is the one that counts.
   - A fixed notice says where the text goes: "Sent to DuckDuckGo, Brave and Wikipedia from this PC's internet address", or the DeepSeek notice.
3. **Search history.** Time, requesting agent, person, trigger, provider, engines, status, result count, cached, cost.
4. **Result detail.**
   - A banner: "Unverified web content".
   - Numbered citations, rendered as plain text (section 3.7): title, domain, URL, snippet, engines, published date, retrieved date.
   - An "instruction-like" warning badge where it applies.
   - No site icons and no raw HTML. Links are shown only for `http(s)` URLs, with `rel="noopener noreferrer nofollow"`. React 18.3.1 does not block `javascript:` links, so the page must [C3].
   - Buttons: "Promote to Brain" and "Retry with DeepSeek".
5. **Sources.** Every distinct page seen: first and last seen, times cited, and whether it was promoted. The promoted item's state appears only as far as the viewer's Brain role allows.
6. **Summaries.** The Supervisor replies that used research, with their citations. AI summaries appear only if the owner later allows them.
7. **Usage and costs.** By day, agent and provider. Shows the price used, its currency, its source URL and the date it was checked.
8. **Provider status and errors.** Health history, engine suspensions, error codes, the version-drift warning.
9. **Blocked attempts.** Time, agent, reason, and the lead or ticket ID that caused it.
10. **Promoted research records.**
    - Built with the Brain's own filter, `person_items(PersonScope(role, can_access_sensitive))`.
    - Operators never see quarantined or customer items. A row they may not see shows "promoted (not visible to your role)".
    - Links go to the Brain Review tab.
11. **Policy and settings** (admins). Mode, fallback, the DeepSeek key field, caps, retention, the per-agent matrix, domain lists, the connectivity test and purge.

### 3.17 JARVIS Brain integration
- **Three source types stay separate.**

  | Source | Where it lives | Trust |
  |---|---|---|
  | Reviewed memory | `memory_items` | Trusted after review |
  | Imported JARVIS conversations | archive tables | Reference only |
  | Web research | `research_*` tables | Untrusted |

  The gateway lives outside `app/brain`, because the Brain spec forbids network calls there [C2].
- **Never automatic.** A source becomes memory only when an operator clicks **Promote** and an **admin approves**.
- **Who does what, exactly.**
  - Operators may **create** pending or quarantined web items through the research promote endpoint. That endpoint does its own checks and does not call `_need_admin` on the result. `_need_admin` stays unchanged.
  - Only admins may **edit or approve** web items. The existing routes already require an admin for any item with a risk flag.
- **Promotion** follows `review.promote_message` [C1]:
  - `store.create_item` with kind `knowledge` or `agent_note` only (never instruction, skill or workflow), `origin='promoted'`, and `source_system` set to `'web:searxng'`, `'web:local-fetch'` or `'web:deepseek'`;
  - `source_ref` = the URL; `source_at` = the published date, or else the retrieval date; `source_key='research:' + sha256(url + retrieved_at)`; `source_sha256` = the content hash;
  - `flags = {web_source: true, custom: true, research_source_id, research_query_id, provider, engines, retrieved_at, published_at}`, plus `instruction_like` when detected;
  - status `pending_review`, or `quarantined` when something was redacted or looks instruction-like;
  - the body starts with "From the web (unverified): URL, retrieved DATE";
  - the target agent must be named. It is never shared (`agent=''`) and never `credit_repair`. Shared items are read by every agent's memory, including Credit Repair's [C3]. Restricted data is refused, as `create_item` already does.
- **Sensitivity.** The existing rule stays. If the text holds a contact detail, or the `[email]`/`[phone]` stand-ins the cleaner left, the item becomes a "customer" item. Customer items are admins-only and excluded from recall and notes. The Promote dialog says so before saving: "This page contains contact details: only admins will see the item." The operator may shorten the excerpt first; cleaning runs again.
- **`web_source` is added to `RISK_FLAGS`.** Approval and editing are then admin-only, and the item never reaches an AI prompt (`prompt_block` skips risk flags) [C3].
- **Edit rules.** While `flags.web_source` is set, `edit_item` refuses any kind other than `knowledge` or `agent_note`. It also refuses changing the agent to shared or to `credit_repair`. A web item therefore can never become an instruction, skill or workflow, and so never a schedule. Today `edit_item` accepts any kind and keeps the flags (`store.py:205-215`).
- **Credit Repair's memory.** Its policy also excludes `web_source` items, as a second guard.
- **Recall and reference notes.** Today they do not check risk flags [CR]. Proposed: approved web items appear there labelled "From web research", with the URL and retrieval date. The alternative, excluding them, is owner decision 11.
- **Going back to 0.6.x fails closed.** 0.6.x does not know `web_source`. It does know `custom`, which is already one of its risk flags (`policy.py:36`). On 0.6.x, web items therefore stay admin-only for approval and editing and stay out of AI prompts, even if the pre-downgrade step is forgotten. That step still archives every web-sourced item, as a second safeguard (section 8).

### 3.18 How agents call it
- **The capability.** A research capability is an ordinary capability with risk **READ** that declares it uses research:

  ```python
  @capability("research_topic", risk=Risk.READ, research=ResearchUse(search=True), params=ResearchParams,
              timeout_seconds=90, description="Search the public web (SearXNG) and return citations.")
  async def research_topic(self, ctx, params):
      res = await ctx.research.search(params.query, count=params.count, time_range=params.time_range)
      return AgentResult(summary=f"Web search #{res.query_id}: {len(res.results)} sources",
                         data={"citations": res.citations()})
  ```

- **No approval needed.** READ capabilities run straight away. This does not change approvals, because the approval decision is made only from risk in `tasks.create_task` [C1].
- **A smaller context.** The runner gives research capabilities a reduced `AgentContext`: no `ssh`, no `integrations`, no `llm`, and the read-only `http` client only where a capability needs it. A coding slip therefore cannot run an inventory command and put host names or command output into a query. Today every capability gets the full context (`worker/runner.py:240-256`).
- **They can never trigger approval-gated actions.**
  - The research client has only `search` and `fetch`. It has no integrations, SSH, approvals, schedules or task creation.
  - The gateway refuses to run inside a task that carries an approval.
  - Tests count rows in tasks, approvals, outbox, `server_commands` and schedules before and after.
- **Proposed capabilities (0.7.0, search only).**

  | Capability | Agent |
  |---|---|
  | `web_search` | `supervisor` |
  | `research_topic` | `seo_content` |
  | `trend_research` | `social_marketing` |
  | `check_advisories` | `wordpress_security`, `server_management` |

- **`check_advisories` in 0.7.0.**
  - The input is a software name and version that a person **types**. It is never generated from scan or SSH output: custom plugin and theme names often contain the client's business name.
  - The output is citations, plus CVE IDs taken from the snippets only when they match `^CVE-\d{4}-\d{4,}$`.
  - It never creates Findings or events. Today finding titles go to the AI summary and warning events go to n8n, so web text must stay out of them.
  - Fetching advisory pages moves to 0.7.1, through the local fetch service, with the allow list checked on every redirect hop. Structured sources such as NVD's JSON API are an option to consider then.
- **Refusals** return an `AgentResult` with a reason code; they are not exceptions, so the logs stay intact [CR].

### 3.19 Where 0.5.1 differs
- **Missing in 0.5.1** [C1][C3]:
  - the `brain` package, so there is no `text.clean()` cleaner, no strong `_fence`, no `memory_items`, no archive to check against, and nothing to promote into;
  - migration `0006`, so a research migration there would collide with 0.6.0's numbering;
  - `hide_parameters=True` on the database engine;
  - the hiding of some task-result data from viewers.
- **The same in both:** the HTTP client, secrets, task layer, planner and compose networks. The SearXNG overlay itself would work on 0.5.1.
- **Proposal: build only on 0.6.x** (with the 0.6.1 patch).

---

## 4. SearXNG versus DeepSeek

| | SearXNG (self-hosted, primary) | DeepSeek Search and Fetch (optional fallback) |
|---|---|---|
| Cost | $0 per query. Costs PC resources (section 6). | $0.75 per 1,000 successful requests per API, or CNY 5 per 1,000 in other sources. Accounts can hold USD or CNY balances. Billing starts 2026-11-01 00:00 Beijing time (2026-10-31 16:00 UTC). All unverified [D][CR]. |
| Privacy and jurisdiction | Runs on the owner's PC. Each query goes to DuckDuckGo, Brave and Wikipedia from the **owner's home IP**, with no account, no cookies and a random browser profile [X2]. Those companies' own policies apply; they were not reviewed. | Tied to the account. Under DeepSeek's general privacy policy (unverified excerpts; nothing specific to Search or Fetch), personal data is stored in the People's Republic of China. Under the Open Platform terms, the developer is the data controller for its end users [D]. |
| Data retention and training | SearXNG keeps no search results (no result cache; WARNING-level logs; metrics off) [X2]. The upstream engines' retention is unknown. | API input is kept while the account exists, possibly longer for legal reasons. The general privacy policy and the consumer terms describe using inputs to train or improve the services, with an opt-out. The API and Open Platform terms say nothing on training. Whether API traffic is excluded is unknown [D][CR]. |
| Quality and coverage risks | Two scraped web engines plus Wikipedia. DuckDuckGo is "reverse-engineered … subject to … bot detection" [X2]. Blocks pause an engine (with this configuration): 15 min for 429, 60 min for access denied or a CAPTCHA, 1 to 15 days for Cloudflare or reCAPTCHA blocks. With only two general engines, one long block halves coverage. Upstream changes can break an engine until SearXNG is updated. Many results have no date. | Its own index, with a relevance score, an "authority" score and summaries. The API is only days old (opened 2026-10-17). Coverage is unknown [D][CR]. |
| Rate limits | None of its own (limiter off); upstream limits are unpublished and unmeasured. The gateway self-limits to protect the home IP. | Account-wide, shared by every key on the account: 5 per second each for Search and Fetch (copy of the guide, DeepSeek's own notes), or 100 and 25 per second (newer excerpts). Also a per-domain fetch limit. Unverified [D][CR]. |
| Page fetch | None. Covered by the local fetch service (section 3.4, later). | Fetch returns Markdown. Redirect handling, private-address blocking, size limits and robots.txt are not documented [D]. |
| Operations burden | One more container. Rolling releases; engines break; we pin and re-test. Watch engine health. | Account, top-ups, key rotation, price and currency checks. |
| Licence | AGPL-3.0-or-later (section 4.1). | Commercial terms of service. |
| Offline or failure behaviour | Fails fast: 35 ms, then 3 ms per query with no internet. Every engine is listed as unresponsive. Recovers in under a minute after `docker restart` [S][S2]. | 5xx, 504, 429, 402. An outage is outside our control. |
| Setup effort | The compose file and settings above, one secret, one script; about 30 minutes of owner time. | Read 4 pages, create a dedicated account with a small balance, store the key; about 30 minutes. |

### 4.1 Licence (AGPL-3.0-or-later)
- **Running the official image unmodified** on the owner's PC is the case planned here. It is configured only by a mounted `settings.yml` and environment variables, and used only by the owner's own software over a private network. In practice this triggers no source-offer duty.
  - Section 13's duty applies when you **modify** the program and offer the modified version to remote network users [X2][O].
  - That configuration does not count as modification is the common reading. It is not written in the licence.
- **Conveying any copy, modified or not, has duties.** Bundling the image in a release package, or pushing it to a registry for customers, falls under the AGPL's conveying terms (sections 4 to 6): the licence text plus the source, or a written offer [R `LICENSE`]. Proposal: never bundle or re-host the image. Each installation pulls the official digest-pinned image itself from Docker Hub, GHCR or the mirror.
- **Building a modified image** would also require offering its full source. Proposal: never build a custom image.
- **Commercial editions** that install SearXNG on customer PCs need a legal review and a new row in COMMERCIALIZATION.md, next to n8n's B1 [O].

### 4.2 Recommendation
- **Search: SearXNG** as the only live provider at first.
- **Page fetching: the local fetch service**, when needed (0.7.1).
- **DeepSeek: opt-in, manual fallback only.** It stays off by default. It is switched on only after the owner has confirmed the docs and accepted the jurisdiction and the training terms. Even then it receives only class G queries (section 10).

---

## 5. Security assessment

| # | Threat | Control (E = exists today, N = new) |
|---|---|---|
| T1 | Secrets, customer records, Credit Repair data or private JARVIS text leave in a query or URL | N: screening before anything is stored and again in the gateway. It reuses `brain/text.clean()` (E) [C3]. The screened string is exactly the string sent. The steps, in order:<br>**(0) Canonical form.** NFKC; remove format characters (zero-width, bidi, soft hyphen); map Unicode dashes and spaces to ASCII; collapse whitespace. Refuse more than 128 characters (never cut). Refuse words that mix scripts, and any remaining non-ASCII digits. Build an ASCII "skeleton" with look-alike letters folded, for the word rules; Cyrillic "еquifax" becomes "equifax". Build a digits-only view, so spaced or dotted numbers ("4 0 4 5 5 5 0 1 9 9") are seen whole.<br>**(1) Search syntax:** refuse words starting with `!`, `:` or `<`.<br>**(2) Encoded data:** refuse long hex, base32 or base64-like tokens.<br>**(3) Restricted data** (SSN, card, date of birth, bank details), on the text and the digits-only view: always refused.<br>**(4) Credit-file words**, on the skeleton: refused by default. This also blocks public topics such as "FCRA amendments 2026" (owner decision 5).<br>**(5) Secrets:** API-key shapes, bearer tokens, secret URL parameters, JWTs, long random tokens, plus an exact in-memory match against the Command Center's own secrets.<br>**(6) Contacts:** e-mail addresses and phone numbers, including spelled-out forms such as "jane at example dot com".<br>**(7) Leads and tickets:** names (at least two tokens), `Lead.company` (at least two tokens, with a stop-list of common words), the `Lead.website` domain, and ticket e-mail domains (except common mail providers). The row ID that caused a block is logged. Lead names can come from the public lead form, so a poisoned record shows up.<br>**(8) Credit Repair clients:** HMACs of name bigrams, e-mails, phone digits and references are checked against the Credit Repair match index. Nothing is decrypted.<br>**(9) Private JARVIS text:** for queries of 6 or more words, every 6-word window is checked as a phrase against the existing full-text indexes of the archive and of memory items. A hit refuses with `matches_private_text` (owner decision 7).<br>**(10) Internal names and addresses:** private IPs, `localhost`, `*.local`, container names, registered server hosts and IPs, file paths.<br>**(11) Client business names and domains**, unless the agent's policy allows class C.<br>**(12) DeepSeek only:** class G only. Version-number patterns, CVE IDs, names in the local software inventory, owner and client names, and any search operator are refused.<br>**(13) Blocked domains.**<br>**(14) Instruction-like text** is flagged, not refused.<br>**Provenance rule:** queries come only from what a person typed, or from words of the operator's message (planner rule, section 1.5). They are never built from memory, the archive, task results, scan output, leads, tickets or Credit Repair data.<br>**Known limits:** paraphrased private text, and names of people found in no store, are not caught. Detection is pattern- and list-based [C3]. Every query is visible in the Research Center. |
| T2 | Research started by text an outsider controls | N: the worker resolves the trigger from the database, never from parameters.<br>A direct run (`source='dashboard'`, created by a signed-in operator) is allowed.<br>A planned step (`source='supervisor'` with a `request_id`) is allowed only if `SupervisorRequest.source == 'dashboard'` and the requester is not `service:…`. Today every planned step is created with `source='supervisor'`, whoever submitted the request; only the request row records `n8n` [C3].<br>A person's retry (`source='retry'`, created by a user) is allowed.<br>`source='schedule'`, anything created by `service:…`, and the n8n service token are refused.<br>The connectivity-test capability sits in an internal set that direct runs, both planners and retries refuse. It runs only from `POST /api/research/test` (admin).<br>Research capabilities are also left out of the planner for n8n requests. Today n8n's lead intake and daily briefing do reach the planner (E) [C3][CR]. |
| T3 | SSRF from page fetching we do ourselves | No page fetching in 0.7.0. Later, N: the local fetch service (section 3.4) runs in its own container with no secrets. It uses `resolve_public` with an empty allow list on every hop and connects only to checked addresses (E: `PublicOnlyBackend`, which resists DNS rebinding). It ignores proxy settings, walks redirects by hand (at most 5, each re-checked against the agent's allow list), caps size and time, and blocks Docker Desktop's host names. The worker never fetches research pages itself; `check_advisories` does not use `ReadOnlyHttpClient`, which follows up to 5 redirects to any public host (`agents/http.py:273-274`). |
| T4 | SSRF and leaks through DeepSeek Fetch (later) | N: DeepSeek's servers download the page, so the PC's own network is not reachable. But **the URL itself** leaks. Before sending: `http(s)` only, no user or password, ports 80 and 443, no IP literals, public DNS only with failure counted as refusal, fragments removed, secret-looking parameters refused. `/wp-admin`, `/wp-login.php`, `nonce=`, `preview=`, `token=`, `key=` and registered server hosts are refused. 2,048 characters at most: longer URLs are refused, never cut. The URL is rebuilt from its parts, and that exact form is screened and sent. Whether DeepSeek follows redirects or blocks private targets is not documented, so the design does not rely on it [D]. |
| T5 | Redirects | Local fetch (later): hop by hop, each re-checked (above). SearXNG: its engines follow redirects inside SearXNG on fixed engine URLs. The image-proxy route still exists (`/image_proxy` at the pinned tag) and fetches any URL whose HMAC matches `SEARXNG_SECRET`, following redirects. It is protected by the secret plus network isolation, and its preference is locked off. A 3xx from `/search` is mapped to `bad_request`. DeepSeek: unknown, labelled "requested URL". |
| T6 | Prompt injection from web text | E: the planner sees only the capability list, the site list and the operator's message; task results never create tasks [C2].<br>N: in this release **no web text reaches any AI model**. Summaries are templates. Citations live in `data.citations` and are shown as a separate plain-text list. `check_advisories` creates no Findings or events. Instruction-like pages get a badge, can be approved only by an admin, and never reach a prompt (`web_source` is a risk flag).<br>N: the planner's `<request>` block is escaped with `_fence`, and planner queries must use the operator's own words (section 1.5).<br>If AI summaries are allowed later: web text goes inside a strongly escaped block (the `_fence` method, which replaces angle brackets), at most 1,000 characters per excerpt, with a fixed note that it is untrusted data and cannot authorise anything. Prerequisite fixes: `drafting.untrusted()` removes only the exact lower-case closing tag, so upper-case or nested tags bypass it; the summary's `<results>` and `<data>` blocks are not escaped (`json.dumps` keeps `<`) [C2][C3]. |
| T7 | Research triggers an action | Research can never cause: server or SSH commands (`server_management.restart_service`), WordPress updates or repairs (`wordpress_security` `repair_*`), customer e-mails (`communications.send_proposal`, `send_reply`), publishing (`seo_content.publish_draft`, `social_marketing.publish_draft`), payments or ad budgets (`social_marketing.change_budget`), certified mail (`credit_repair.mail_dispute_letter`), schedule creation, or n8n workflow activation (n8n workflows are activated only by its entrypoint at install [C3]). Guarantees: READ-only capabilities; a client with search and fetch only; a reduced context with no SSH, integrations or model; refusal inside approved tasks; row-count tests; approval rules unchanged. |
| T8 | Logs leak queries or keys | N: queries go to SearXNG by POST, so they are not in URLs. The worker's and the fetch service's `httpx` and `httpcore` loggers are set to WARNING; today httpx logs every URL at INFO [C2]. Granian's access log is off (proven [S2]). SearXNG logs at WARNING [X2], but its error-context lines and its `logger.exception` on search errors could include upstream text [R]; a canary test checks this, including forced 500 errors (section 7.4). Container logs are capped at 1 MB × 3. Errors are fixed codes, because a failed task's error goes into an event that is forwarded to n8n [DR]. Research events are info level with no text. No query text in GET URLs. Audit entries hold IDs and counts. Blocked attempts store no text and no hash. **Purging research records does not purge container logs or earlier backups**; the purge dialog says so, and the purge procedure recreates the SearXNG and fetch containers, which removes their logs (section 8). |
| T9 | Key leak or runaway cost (DeepSeek) | N: the key lives only in the encrypted store (E). It is a full API key that can also pay for model calls, so use a dedicated DeepSeek account with a small prepaid balance (a natural hard limit). Add the $1.00 (or CNY 7) monthly cap and the 50-a-day cap; 402 stops all calls. |
| T10 | Hostile or malformed responses | N: 2 MB read cap; strict types with clamped scores; every field cut to its cap before cleaning; invisible characters stripped; `site_icon` never loaded; citations rendered as plain text, never through the Markdown component (E: that component renders no raw HTML) [C2]. |
| T11 | Credit Repair reached through research | N: `credit_repair` gets no `ctx.research`; the gateway refuses its ID. Credit Repair client data typed by an operator is caught by the match index (T1 step 8). Web items are never shared or targeted at Credit Repair, and its memory policy excludes them. Tests check all of these. E: its own database, role and key; the Supervisor never routes to it [C3]. |
| T12 | Owner's home IP blocked by engines | Queries leave from the home IP. If DuckDuckGo or Brave flag it, the owner's **own browser** may see CAPTCHAs too. N: low rates; a 50-a-day starting cap; longer suspensions; the gateway's own cool-down, which survives SearXNG restarts. |
| T13 | SearXNG used as a proxy into the home network | `research_egress` is a plain bridge and can reach LAN and host IPs (`extra_hosts` blocks names only). N: no published port; only the worker shares a network with it; the image proxy is off, locked and HMAC-gated; no plugins; a rule never to switch on engines or plugins that fetch caller-supplied URLs; `searxng` refused in `MONITOR_ALLOWED_PRIVATE_HOSTS`. |
| T14 | One hostile page stalls the worker | The Brain cleaner's table check grows with the square of the input, and `asyncio.wait_for` cannot interrupt CPU-bound regex work [CR]. N: every field is cut to its cap before cleaning (snippets 1,200 characters). Fetched pages (later) are cleaned only in the fetch container, in a subprocess with a hard kill. A test checks that a 2 MB hostile table is refused or cleaned within a fixed time while a monitoring task still completes. |

### 5.1 SearXNG's own risks
- **Image proxy.** The route stays registered. It fetches any URL with a valid HMAC, follows redirects and has no private-address filter [RT]. It is protected by the secret (never in the worker, logs or packages), the lock, and the network. There is no published port.
- **Start-up download of ClearURLs tracker rules.** The first sandbox run logged a failed fetch [S]. It tries `cdn.jsdelivr.net`, then `rules2.clearurls.xyz`, then `rules1.clearurls.xyz` [RT]. Only the tracker-remover plugin starts it. With `plugins: {}` it was absent from [S2]'s warnings.
- **Runs as root by default** (the image sets no USER). We force `977:977`.
- **Bot-detection warnings** about a missing X-Real-IP or limiter.toml are noise while the limiter is off [S][S2]. The fixed header and the empty `limiter.toml` silence them.
- **Custom CA certificates.** The image's certificate-folder method needs root [X1]. SearXNG also accepts `outgoing.verify: <path to a full CA bundle>`, which works as uid 977 [R `settings_outgoing.rst`]. The 0.6.0 release notes say this installation does not use `EXTRA_CA_BUNDLE`. If that ever changes, a release ships a variant settings file selected with `SEARXNG_SETTINGS_PATH`, plus a read-only bundle mount.
- **AGPL:** section 4.1.

### 5.2 DeepSeek's risks
- Under DeepSeek's general privacy policy (unverified excerpts; nothing specific to Search or Fetch), personal data is stored in the PRC, and API input is kept while the account exists, possibly longer. Purging the Command Center does not delete DeepSeek's copy [D].
- The general policy and the consumer terms describe training or improvement use with an opt-out. Whether API traffic is excluded must be confirmed (section 13).
- Queries reveal what the business is looking into.
- The key is a full API key on a shared, account-wide rate limit.
- Commercial customers would have to disclose DeepSeek as a processor. Some may forbid processors in China.

### 5.3 Existing approvals stay mandatory
Nothing in this design changes `tasks.create_task`, `decide_approval`, the approval bindings or the production typed confirmation [C1].

---

## 6. Resource and cost estimates

**SearXNG (the PC)**

| Item | Figure | Basis |
|---|---|---|
| Idle memory | 80–85 MiB, 8–11 processes and threads | Measured in two sandbox runs [S][S2] |
| Limits | 512 MB memory, 1 CPU, 256 processes | Proven to run. Peak under load is not measured |
| Image on disk | About 98 MB compressed (98,233,735 bytes, amd64), 383 MB unpacked | Docker Hub and the sandbox |
| Cache volume and /tmp | Expected under 50 MB; /tmp capped at 64 MB | Not measured |
| Logs | At most 3 MB | Compose setting |
| Bandwidth per search | Assumed 100–400 KB downloaded and 1–3 KB uploaded: one request each to DuckDuckGo, Brave and Wikipedia | Assumption; measured on the PC during the approved test |
| Bandwidth per month | Baseline (220–1,100 searches): 22–440 MB. At the starting cap (50 a day): 150–600 MB. If raised to 200 a day: 0.6–2.4 GB | Same assumption |
| Local fetch (later) | At most 600 KB per page. At 30 pages a day, at most about 0.5 GB a month | Cap |
| Fetch service limits (later) | 256 MB, 64 processes | Guesses, not measured |

**Database and backups**

| Volume | Growth |
|---|---|
| Baseline (about 1,100 queries a month, 10 results each, logs kept 90 days, sources 30 days) | About 10–60 MB |
| At the starting caps, with fetch (30 pages a day of up to 100 KB) | Up to about 0.35 GB |
| If caps are raised to 200 searches and 100 fetches a day | Up to about 0.6 GB |

`backup.ps1` dumps the whole `command_center` database, so backups grow by a similar, compressed amount [O]. Preflight's disk note ("about 5 GB") gains the image and this growth.

**DeepSeek** (later, opt-in)
- The price is $0.75 per 1,000 successful requests per API, or CNY 5 per 1,000; it comes from excerpts and is **unverified**.
- Billing starts 2026-11-01 00:00 Beijing time (2026-10-31 16:00 UTC). It is not documented whether direct API calls are free before then, so assume they are billed.
- DeepSeek does not bill its own failures, timeouts or empty results. The gateway still counts a 200 it rejected as billed, and a client-side timeout as "possibly billed" [D][CR].

| Example month | DeepSeek searches | DeepSeek fetches | Cost (USD) |
|---|---|---|---|
| Baseline: fallback for about 5% of 220–1,100 SearXNG searches (the 5% is an assumption, to be measured) | 11–55 | 0 | $0.01–$0.04 |
| The same, plus 5 manual DeepSeek fetches per working day (22 days) | 11–55 | 110 | $0.09–$0.12 |
| Every request the daily cap allows (50 a day × 30 = 1,500) | 1,500 together | | $1.13, but the $1.00 monthly cap stops DeepSeek at about 1,333 requests |
| Heavy (100 searches and 50 fetches a day) | 3,000 | 1,500 | $3.38. **Not possible with the proposed caps**; it would need the daily cap raised to 150 and the monthly cap to about $3.50 |
| Proposed caps | 50 requests a day | | $1.00 a month (or CNY 7) |

**Owner's time**

| Task | Time |
|---|---|
| Reading this plan and deciding | 1–2 hours |
| Switching research on | About 30 minutes |
| Each real connectivity test | About 15 minutes |
| Monthly check (status, engines, budget, price) | About 15 minutes |
| Reviewing promotions | As used |
| Upgrades | Part of normal release installs |

---

## 7. Testing procedure

### 7.1 Principles
- **Mock first.** All automated tests use the mock provider or fake transports (`httpx.MockTransport`, like the existing `FakeWeb` [C2]).
- **No accidental network.** A guard in the test setup fails any test that opens a real connection.
- **Never on the live installation.**

### 7.2 Unit tests
- **Request building.**
  - Query limits: 1–128 characters after the canonical form, never all whitespace; longer queries refused, never cut.
  - SearXNG form fields are exactly `q`, `format`, `engines`, `language`, `time_range`, `pageno`: never `categories`, `category_*`, `safesearch` or `timeout_limit`. A cooling engine is absent from `engines`.
  - DeepSeek fields: `ori_question` and `localization` never present; the key in the header only; URLs over 2,048 characters refused.
- **Canonical form and evasion.** Each of these must be blocked:
  - a full-width SSN (`０７８-０５-１１２０`);
  - `jane.doe<U+200B>@example.com`;
  - "jane.doe at example dot com";
  - the spaced phone "4 0 4 5 5 5 0 1 9 9";
  - Cyrillic "еquifax";
  - NBSP and en-dash variants;
  - mixed-script words;
  - non-ASCII digits;
  - long hex, base32 and base64 tokens.
- **Search syntax.** "!!g x", "!wp x", ":de x" and "<3 x" are refused. A 3xx answer is mapped to `bad_request`.
- **Normalization.**
  - SearXNG: `results`, `infoboxes` and `unresponsive_engines` mapped; `number_of_results` absent; null dates.
  - DeepSeek: clamped scores, empty `title`, unknown extra fields.
  - Both: fields cut before cleaning; invisible characters removed; `javascript:` and `data:` URLs dropped.
- **Status mapping.**
  - `ok`, `partial`, `empty`, and `provider_down` when every engine is unresponsive, "Suspended: …" or cooling down.
  - SearXNG 403 counts as a configuration error; 400 and 500 handled.
  - Every DeepSeek code: 400, 401, 402, 403, 404, 408, 413, 429, 500, 504.
- **Health.** The version comparison with `+`, `-` and `+dirty`. The suspension message built from each reason and its configured time.
- **Retries and timeouts.** Retry counts and back-off, `Retry-After` capped, a slow trickle hitting the overall deadline, total time inside the capability timeout.
- **Rate limits and budgets.**
  - 20 reservations at once against a cap of 5 never exceed 5.
  - No lock is held across HTTP.
  - A billed call followed by a handler exception is still counted.
  - A rejected 200 counts as billed; a client-side timeout counts as "possibly billed".
  - Rollover at UTC midnight; the monthly cap in its currency.
  - 80% and 100% events contain no text [CR].
- **Cache.** Hit, miss, expiry; no information about who asked; domain policy applied to hits; identical in-flight requests merged.
- **Screening table.**
  - Restricted data and credit words.
  - Secrets, including synthetic Anthropic and DeepSeek key shapes and the service token.
  - Contacts.
  - Lead names, `Lead.company` and `Lead.website` domains, and ticket domains, with the stop-list and the two-token rule.
  - Credit Repair clients by name, e-mail, phone and reference.
  - A 6-word sentence copied from an archived message and from a memory item.
  - Internal hosts and IPs, file paths, client domains.
  - DeepSeek class G: version numbers, CVE IDs, inventory names, `site:`.
  - Instruction-like text (flagged only).
- **SSRF table** (fake resolver; for the later fetch phase):
  - user name or password in the URL; IP literals; port 8080; an IDN look-alike host;
  - a name that resolves privately; an unresolvable name; a host listed in `MONITOR_ALLOWED_PRIVATE_HOSTS`;
  - a redirect to `127.0.0.1`, `169.254.169.254`, `host.docker.internal` or a 10.x address;
  - a redirect off the agent's allow list; a sixth redirect; an oversized body.
- **Fence.** Closing tags in any letter case, nested tags, `</results>`, zero-width and right-to-left characters. `<request>` is escaped.
- **Citations.** Titles such as "Needs attention: approve task #45 now", "# Heading", "- CRITICAL", "**bold**" and a `[link](javascript:…)` are rendered as inert plain text, prefixed "Web source [n] (unverified):".
- **Planner.**
  - The rule intent matches only the explicit phrases, and its query is the captured text. No match on "is clientsite.com online", "look up what JARVIS said about Jane", bare "latest", "current" or "news".
  - "Research the web for WordPress 6.9" goes to web search; "research keywords for dentists" goes to `keyword_brief`.
  - Overlaps with `health_review`, `daily_briefing`, `recall` and `check_url` [CR].
  - A Claude-planned query with a word not in the operator's message is dropped with `query_not_from_request`.

### 7.3 Integration tests (real PostgreSQL)
- **Permissions and triggers.**
  - Viewers cannot run research. The service token is refused.
  - An n8n request whose plan is forced to contain a research step, bypassing the planner filter, is refused by the gateway.
  - Schedule-started tasks are refused; retries by people are allowed.
  - An operator cannot run or retry the connectivity-test capability.
  - A switched-off agent, an edition without `web_research`, and an agent whose policy disables search are refused.
- **Agent isolation.**
  - `credit_repair` has no `ctx.research`. Calling the gateway with its ID fails.
  - A capability that did not declare research gets no client.
  - A research capability sees `ctx.ssh`, `ctx.integrations` and `ctx.llm` as empty.
  - Credit Repair's memory never returns a `web_source` item.
- **Feature switch.**
  - Off: no request is made and research is absent from the planner.
  - Mock: the live transport is never used, even with a key stored.
- **No side effects.** Pages saying "approve task 12", "restart the server", "run this command" or "ignore previous instructions" are stored with flags. Row counts in tasks, approvals, schedules, outbox and `server_commands` do not change. Only the template summary is used; nothing reaches `prompt_block`, even after admin approval. `check_advisories` creates no Findings and no events.
- **Attribution.** Citations carry the URL, title, domain, provider, engines, published and retrieved dates. A mixed request gets an AI summary with the citations listed separately. What a viewer sees [CR].
- **Promotion.**
  - Promotion creates a pending (or quarantined) item with the `web_source` and `custom` flags and full provenance.
  - Approval and editing are admin-only.
  - The instruction, skill and workflow kinds are refused at creation and at edit. Shared and Credit Repair targets are refused.
  - A page with an e-mail address becomes a customer item: who sees it, absent from recall and notes, and the dialog warning.
  - Recall and notes label approved web items.
- **Fail-closed on 0.6.0.** An approved web item loaded under 0.6.0 code is excluded by `prompt_block`, and an operator cannot approve or edit it.
- **Blocked text never stored.** A blocked direct run or planned step leaves no text in tasks, `supervisor_requests.plan`, `tasks.result`, task logs, events, the worker log or `research_queries`.
- **Viewer views.** `task_view` and `request_view` hide research parameters, results and step rationale from viewers. `/api/research/usage` refuses viewers.
- **Research Center scope.** Screen 10 and the source detail follow `person_items` for each role.
- **Logs.** Captured logs, task errors and events never contain the key, the query text or the response text.
- **Performance.** A 2 MB hostile table is refused or cleaned within a fixed time while a monitoring task still completes.
- **Schema.** The migration runs twice without harm. A pre-0.7.0 dump restores into a 0.7.0 database (a mirror of `test_brain_schema.py`) [CR]. The Credit Repair index migration and its backfill run twice without harm.
- **Retention and purge.** Including research task parameters and results.

### 7.4 An isolated development SearXNG instance (sandbox, like the earlier runs)
A separate Compose project (`sxng-dev`) on its own `internal: true` network, with no published ports, running the **pinned** image and the files from section 2. Checks:
1. `docker compose config` refuses to run without `SEARXNG_SECRET`.
2. Healthy as 977 with a read-only root filesystem and no capabilities; `/etc/searxng` not writable.
3. Startup on IPv4 only works with `GRANIAN_HOST=0.0.0.0`.
4. JSON returns 200 and HTML returns 403. `/config` lists exactly duckduckgo, brave and wikipedia, and its version matches the tag after normalisation.
5. With no internet, every engine is unresponsive and the gateway reports `provider_down`.
6. **Restart recovery.** `docker restart` brings it back healthy in under a minute. The gateway reports `unreachable` in the meantime, a queued research task fails with a clear code, and a retry then works. No other service restarts.
7. **Restart policy.** Killing the main process inside the container (`docker kill` would count as a manual stop) brings it back under `unless-stopped`.
8. `plugins: {}` removes the ClearURLs download at start, and the empty `limiter.toml` removes its warning.
9. **Locks.** A request with `image_proxy=1` or `safesearch=0` in the form does not change the result page settings. `engines=` still works with the lock in place.
10. **Cooling engines.** A request with `engines=duckduckgo,wikipedia` and no categories does not query brave.
11. **Canary logs.** A canary query, forced engine errors and forced 500 errors never put the canary into `docker logs`.
12. **Test engine.** A test-only engine in a test-only settings file, using SearXNG's generic `json_engine` (present at the pinned tag [RT]) pointed at a mock upstream on the internal network. This exercises real result parsing without internet.
13. **Load.** A burst of 200 queries against that test engine: memory and processes stay under the limits. A burst against real engines would only exercise the fast-fail path in the sandbox, and would risk CAPTCHAs on the PC.

### 7.5 Full stack in the sandbox
- **Setup.** `docker-compose.testing.yml` gains a mock search service.
- **The real overlay with the core stack.** Run `-f docker-compose.yml -f docker-compose.testing.yml -f docker-compose.research.yml`, once more with the tunnel overlay.
  - From `docker compose config --format json`: `searxng` has no ports; its networks are exactly `research` and `research_egress`; the worker's are `internal`, `egress` and `research`; postgres, db-init, api, dashboard and n8n are not on `research`.
  - At runtime: from inside `searxng` (using the image's Python), connections to `postgres:5432`, `api:8000`, `n8n:5678` and `dashboard:8080` fail. From the worker, `http://searxng:8080/healthz` returns 200. `docker port searxng` shows nothing.
  - Container IDs and start times of postgres, n8n, api and dashboard do not change across enable and disable.
- **Start-up fallbacks.** With the image missing and no registry, with `settings.yml` missing, and with `SEARXNG_SECRET` missing:
  - the core starts and the worker is not recreated;
  - `backup.ps1`, `stop.ps1`, `restore.ps1`, `status.ps1` and `install.ps1` still work;
  - `smoke.ps1` passes with research shown as information.
- **Playwright runs:**
  - the Research Center for each role, including screen 10's visibility;
  - the 128-character counter;
  - the provider notice;
  - citation links refusing `javascript:`, and forged "Needs attention" titles shown inert;
  - a Supervisor request with the separate citation list;
  - promote, then admin approval;
  - "all engines failing" shown with the right times.
- **Upgrade and rollback.**
  - Upgrade 0.6.x to 0.7.0 with all data kept.
  - Switch research on, run `research.ps1 -PrepareDowngrade`, roll back to 0.6.x. Check that `install.ps1`, `backup.ps1` and `status.ps1` work in the 0.6.x folder.
  - Also roll back **without** the preparation step, to show that web items stay admin-only and out of prompts.
- **JARVIS test copy.** Seeded from a backup with research mode `live`: the seed switches it off, and the check counts it. `research.ps1` refuses while the test copy runs. A SearXNG restart during a test run does not trip the live fingerprint; a changed SearXNG image does.
- **Uninstall.** The research networks and volume are removed or listed even when the overlay is no longer in `COMPOSE_FILE`.

### 7.6 Existing regression suites
All must pass:
- the backend suite (853 tests passed on 0.6.0);
- the dashboard Vitest suite;
- three consecutive end-to-end Playwright runs;
- `testing/brain-check.sh`, where the test copy must have no SearXNG and no internet;
- `stale-build-check.sh`;
- the package and SBOM checks, including plain-ASCII `.ps1` files.

### 7.7 Real connectivity tests (only with the owner's written approval, on the owner's PC)
The sandbox cannot reach the internet from containers, and it cannot reach DeepSeek at all.

1. **SearXNG.**
   - The owner switches research on in mock mode first.
   - Then, after approval, three fixed public queries go out through DuckDuckGo, Brave and Wikipedia, for example "WordPress 6.9 release notes".
   - Only these are printed: status, engines that answered or failed, result count, timing, and `docker stats` for SearXNG during the three queries.
   - Cost $0. The owner's IP is visible to those three services.
2. **DeepSeek** (later, separately approved).
   - Before it, the owner opens the four DeepSeek pages and confirms the facts, including the currency and the rate limit.
   - It uses a probe shipped in the release, with that release's provider code. It runs in a throwaway container that is not on the Command Center's networks.
   - The key is typed at a hidden prompt and passed on standard input, never with `-e` or as an argument [CR].
   - It sends one Search and one Fetch of fixed public items, about $0.0015 (or CNY 0.01). It prints only status, timing, field presence and whether the schema matched.

---

## 8. Rollback procedure

1. **Feature switch** (seconds; keeps data). Research mode `off` is the default.
   - Capabilities refuse at once and leave the planner.
   - Queued research tasks are cancelled. This is new: today switching an agent off cancels only tasks that need approval [DR].
   - The Research Center becomes read-only.
2. **Stop and remove SearXNG without touching other services:**
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\research.ps1 -Disable
   ```
   It refuses while the JARVIS test copy or an import is running. Then it does the following:
   ```powershell
   docker compose -f docker-compose.yml -f docker-compose.research.yml rm -sf searxng
   # removes only docker-compose.research.yml from COMPOSE_FILE in .env (split and joined on ';'; a tunnel entry stays)
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1 -NoBuild   # recreates the worker without the research network
   docker volume rm infinite-command-center_searxng_cache
   docker network rm infinite-command-center_research infinite-command-center_research_egress
   ```
   - **Optional:** remove the image (about 383 MB).
   - **What changes:** PostgreSQL, n8n, the API and the dashboard keep running unchanged. Their configuration does not change, so Compose does not recreate them. The worker restarts once.
3. **Going back to 0.6.x.**
   - The project's migrations only go forward. The research migrations only add tables, have no foreign keys into older tables and change no existing table [CR].
   - The steps:
     1. Run `research.ps1 -PrepareDowngrade`. It archives web-sourced memory items (status `archived`, a change 0.6.0 allows). Then it does everything `-Disable` does. It refuses to report success while `COMPOSE_FILE` still names `docker-compose.research.yml`. Without this, every plain `docker compose` command in the 0.6.x folder would fail, because that folder has no research file and the copied `.env` still names it.
     2. Take a backup (`backup.ps1` already covers the research tables).
     3. Install the 0.6.x folder as its release notes describe.
   - The 0.7.0 release notes list these steps.
   - If step 1 is forgotten, web items still fail closed on 0.6.x (the `custom` flag, section 3.17). The compose error would stop the install until `COMPOSE_FILE` is fixed.
   - The research tables then sit unused. A documented full-removal script (`DROP TABLE research_*`, and the Credit Repair index) runs only after a backup, by an admin, with a typed confirmation.
4. **Removing the DeepSeek key.**
   - Settings, then Research, then Remove key (the existing `DELETE /api/settings/secrets/deepseek_api_key`, audited without the value).
   - Revoke the key in the DeepSeek console.
   - Purging here never deletes DeepSeek's own copy [D].
5. **Purging research records.**
   - An admin action with the typed confirmation "PURGE RESEARCH". It deletes queries, results, sources and cache, and scrubs research task parameters and results. Usage totals are optional.
   - The dialog states what it does **not** purge: container logs, earlier backups, the operator's request text in `supervisor_requests` and the audit log, and DeepSeek's copy.
   - The procedure then recreates `searxng` (and `research-fetch`, later) with `docker compose up -d --force-recreate --no-deps searxng`, which removes their logs.
   - Promoted Brain items stay, because they are reviewed memory. Delete them in the Brain if wanted.
   - Daily retention (proposed): sources 30 days unless promoted; query and blocked logs 90 days; usage totals 2 years; cache until it expires.
6. **Uninstall.** `uninstall.ps1` removes the research networks and volume even when the overlay is no longer listed, and says the SearXNG image is kept.

---

## 9. Recommended implementation sequence

| Phase | What happens | What the owner approves |
|---|---|---|
| 0. Now | Read this plan. Separately: the 0.6.0 withhold defect (already planned for 0.6.1 in GO_NO_GO) | Decisions 1–7 at least (section 11), and the 0.6.1 patch |
| 1. 0.6.1 (small) | New idempotent migration widening the version CHECK, a regression test, package | Installing 0.6.1 after 0.6.0 is verified (or instead of it) |
| 2. Build in the sandbox | Gateway core, mock provider, tables, the canonical form and full screen (including the Credit Repair index and the archive phrase check), trigger resolution, policy, budgets, cache, the reduced context, Brain rules, viewer filtering, Research Center (mock), planner changes, fence fixes, the separate citation list; all tests in sections 7.2, 7.3, 7.5 and 7.6 | Nothing new; screenshots offered for review |
| 3. SearXNG in the sandbox | Overlay, settings, `research.ps1`, `compose-env.ps1`, script changes, SearXNG provider, health rows, the development-instance checks (section 7.4), the real-overlay network checks (section 7.5) | Nothing new |
| 4. Release 0.7.0 | Package with research **off**. Release notes list every outbound data flow and the rollback steps | Installing 0.7.0 (only after 0.6.x is verified on the PC) |
| 5. On the PC | Switch research on in mock mode, then the real SearXNG connectivity test, then live SearXNG at 50 a day | Each step separately |
| 6. After 2–4 weeks | Review engine suspensions and usage; maybe raise the caps | Decision 4d |
| 7. 0.7.1 (optional) | Local fetch service; advisory page fetches through it | Decision 10 and its own test |
| 8. 0.7.2 (optional) | DeepSeek fallback, manual first | Decision 8: docs confirmed, jurisdiction and training terms accepted, dedicated account, the 2-request test |
| Later, each decided separately | AI summaries over fenced web text; research in drafts; n8n or scheduled research; automatic fallback; web items in AI prompts | — |

**Release number.**
- **Recommended:** the Research Gateway as **0.7.0**, with **Messaging & Channels moved to 0.8.0**.
- **Why:**
  - Messaging has six open owner decisions (PROJECT_STATUS.md). Research needs fewer.
  - Research needs no public port, tunnel or DNS change.
  - It builds what Messaging phase 1 needs anyway: a mock-first provider interface, encrypted provider keys, health rows, atomic rate limits, outbound screening and a strong fence for untrusted text.
- **If Messaging goes first,** research becomes 0.8.0. Migration numbers follow the order of release.

---

## 10. Agent access matrix

**Data classes a query may contain**

| Class | Meaning |
|---|---|
| **G** | General public topic: industry topics, public news, general how-to questions. No version numbers, CVE IDs or company names of the owner or clients. |
| **S** | Public software names and versions, and CVE numbers, **typed by a person**. Never generated from scan or SSH output. |
| **O** | The owner's own public business names and domains, from an admin-kept list |
| **C** | Client businesses' public names and domains (registered sites) |
| **X** | **Never, for anyone:** personal data of leads, customers or contacts; lead companies and websites; anything about Credit Repair or its clients; restricted data; secrets; internal infrastructure; text from tasks, approvals, the audit log, Brain memory or the JARVIS archive |

**Agents** (IDs and names from `agents/registry.py` [C1]; proposed defaults)

| Agent ID | Name | Search via SearXNG (per day) | Fetch via local service (later, per day) | DeepSeek (when switched on; search/fetch per day) | Classes (SearXNG) | Classes (DeepSeek) | Fetch domains |
|---|---|---|---|---|---|---|---|
| `supervisor` | AI Supervisor | yes (20) | none by default; if allowed, only URLs that appear verbatim in the operator's message (5) | 15 / 10 (verbatim URLs only) | G, S, O (planner queries must use the operator's words) | G | URLs from the operator's message only |
| `seo_content` | SEO & Content | yes (20) | yes (15) | 10 / 10 | G, O; C only if the owner allows | G | any public domain not blocked |
| `social_marketing` | Social Media & Marketing (Postonomous) | yes (10) | yes (5) | 5 / 0 | G, O | G | any public domain not blocked |
| `wordpress_security` | WordPress Security & Repair | yes (10), `check_advisories` with typed names and versions only | 0.7.1: advisory allow list only, every hop re-checked (10) | none | S only, never site URLs | — | wordpress.org, wpscan.com, patchstack.com, nvd.nist.gov, cve.org (owner to confirm) |
| `server_management` | VPS & Server Management (CWP/SSH) | yes (5), `check_advisories` with typed package names and versions only | 0.7.1: advisory allow list only (5) | none | S only, never host names, IPs, user names, paths or command output | — | nvd.nist.gov, cve.org, control-webpanel.com (owner to confirm) |
| `monitoring` | Website & Infrastructure Monitoring | no | no | none | — | — | It already checks registered sites directly |
| `communications` | Email & Customer Communications | no | no | none | — | — | It works with leads and tickets (highest leak risk); revisit later |
| `credit_repair` | Credit Repair & Dispute Management | **none, enforced in code** | **none** | **none** | — | — | — |

- **Totals.**
  - SearXNG agent caps add up to 65 against the starting global cap of 50; the global cap binds first.
  - Fetch caps (later) add up to 40 against the global 30.
  - DeepSeek caps add up to exactly 50 (30 searches, 20 fetches), equal to its global cap.
- **Supervisor routing.** The Supervisor plans research only from dashboard requests and never routes to Credit Repair (E) [C1].

**People**

| Role | Start research | View results and citations | View blocked log, usage, costs | Promote to Brain | See promoted records (screen 10) | Approve or edit web items | Settings, key, policy, purge, tests |
|---|---|---|---|---|---|---|---|
| admin | yes | yes | yes | yes | all, per Brain rules | **yes (admin only)** | yes; typed confirmation for live mode, DeepSeek and purge |
| operator | yes (allowed agents) | yes | yes | yes (creates a pending or quarantined item) | not quarantined, not customer items | no | no |
| viewer | no | no by default (decision 13) | no | no | approved general items only, if decision 13 allows | no | no |
| n8n service token | **no** | no | no | no | no | no | no |
| `can_access_sensitive` | no effect, because Credit Repair has no research and web items never target Credit Repair | | | | | | |

---

## 11. Owner decisions needed

1. **The overall approach.** The gateway runs in the worker. SearXNG is primary. DeepSeek is an optional fallback. Everything is off by default.
2. **Release numbering.** Research 0.7.0 with Messaging 0.8.0 (proposed), or the reverse.
3. **Who may start research.** Proposed: people, and Supervisor requests typed on the dashboard. Not n8n, not schedules.
4. **The agent matrix and caps** (section 10):
   - (a) whether SEO & Content may use client names and domains (class C);
   - (b) whether Communications stays without research;
   - (c) the advisory allow lists (for 0.7.1);
   - (d) the starting caps: 50 SearXNG searches a day overall, 30 per person, to be raised only after 2–4 weeks of engine data.
5. **Public credit-topic research** (FCRA, bureaus, disputes, with no personal data). Proposed: blocked for every agent.
6. **Software names and versions.** Whether names and versions a person types may go to DuckDuckGo, Brave and Wikipedia. Proposed: yes for SearXNG only, never generated from scan output, never to DeepSeek.
7. **The private-text check.** Refuse any query of 6 or more words that repeats a 6-word phrase from the JARVIS archive or memory. Proposed: on. Common phrasing may sometimes be refused; the operator then rewords.
8. **DeepSeek at all.** It means:
   - storage in the PRC while the account exists;
   - possible training use under the general terms (the opt-out or an API exclusion must be confirmed);
   - a key that also pays for model calls.

   If yes:
   - a dedicated account with a small prepaid balance;
   - a $1.00 (or CNY 7) monthly cap and 50 requests a day;
   - confirming the four pages first.
9. **Fallback mode.** `off`, `manual` (proposed) or `auto`.
10. **Page fetching.** None in 0.7.0 (proposed). Then the local fetch service in 0.7.1. DeepSeek Fetch only as a fallback.
11. **Web-sourced memory.**
    - Admin approval and editing only, never in AI prompts, always targeted at a named agent (proposed).
    - In recall and reference notes: shown with a label (proposed), or excluded.
12. **AI summaries over web text.** Proposed: not in 0.7.0.
13. **Viewers.** Proposed: no research history; only the health line.
14. **Retention.** Sources 30 days, logs 90 days, usage 2 years.
15. **The real connectivity tests on the PC**, one for SearXNG and one for DeepSeek, each separately.
16. **The 0.6.1 patch** for the withhold defect, and when to install it (already planned in GO_NO_GO).
17. **Engines.** DuckDuckGo, Brave and Wikipedia (proposed). Adding `google cse` or `wikidata` would be a release change after a development check.
18. **Commercial editions.** Which editions get `web_research`; the AGPL legal review; customer disclosure wording.

---

## 12. Open questions

1. Is remote access, with `COMPOSE_FILE`, already switched on at the PC? This decides how `research.ps1` edits `.env` the first time.
2. Is the owner comfortable that searches leave from the home IP, with a small risk of CAPTCHAs in the owner's own browser?
3. Do any client contracts forbid processors in China (relevant only if DeepSeek is used)?
4. Should research history be searchable by text in the Research Center? That would need a POST search endpoint so the text stays out of logs.
5. Should a later hardening step move all provider calls out of the worker into a dedicated container with no database credentials (Variant A2 in [DR])?
6. Does the PC have other Docker networks (other projects) that could overlap the tunnel's `172.29.37.0/28`?
7. Is screening `Lead.company` with the two-token rule and a stop-list acceptable, given that a lead's company may share words with a public topic?
8. The 0.6.0 release notes say the installation does not use `EXTRA_CA_BUNDLE`. Is that still true? If not, SearXNG needs the `outgoing.verify` variant (section 5.1).

---

## 13. Facts to verify before implementation

**DeepSeek** (from search excerpts only, never read from the pages [D]). Open https://api-docs.deepseek.com/guides/search/, /api/search/, /api/fetch/ and /quick_start/pricing/, and confirm:
1. The endpoints, Bearer authentication, and every request and response field name and range (`query` 1–128 characters, `count` 1–50, `rerank_score`, `authority`, `date`, `summary`, `content`).
2. The operators (`site:`, `-site:`, `-term`, phrases, `lang:`, `after:`, `before:`). None is used until confirmed.
3. The error table and retry guidance; the JSON shape of error bodies; whether `Retry-After` is sent.
4. The rate limit: 5 per second each, or 100 searches and 25 fetches per second; that it is account-wide across keys; the per-domain fetch limit.
5. The price and currency: $0.75 or CNY 5 per 1,000 successful requests per API, and **which currency the owner's account is billed in**. Also what is not billed, the billing start (2026-11-01 00:00 Beijing), and whether direct REST calls are free before then.
6. Fetch behaviour: redirects, private-address blocking, robots.txt, content types, size limits, timeouts.
7. Retention, regional availability, and **training use**: whether the opt-out exists for API traffic, or API traffic is excluded.
8. Whether a key can be limited to Search and Fetch, or given a spending limit (if not, it is a full API key).
9. The DeepSeek key format, for the settings check.

**SearXNG.** Closed by reading the source at the pinned commit `d48c4b555` [RT], plus the sandbox:
- the JSON keys, with no `number_of_results` [S][S2][RT];
- the shipped `suspended_times`: 180, 3600, 180, 1296000, 86400 and 604800 s;
- `json_engine` exists;
- `google cse` and `wikidata` are on by default;
- the X-Real-IP warning is silenced by any valid header;
- suspensions are held in memory and reset on restart.

Still to check:
1. That `plugins: {}` removes the ClearURLs download, and `favicon_resolver`, the explicit `suspended_times` and `preferences.lock` behave as read. Supported by code and [S2]; recheck on the development instance and the PC.
2. That `engines=` without `categories` skips a cooling engine, and that the lock does not affect `engines=` (from code; development check).
3. Whether engine-error log lines or `logger.exception` output contain query text (canary test).
4. Memory and CPU under load (test engine); bandwidth per query (on the PC).
5. Real result quality and blocking behaviour from a residential IP (only on the PC, with approval).

**The owner's PC and Docker Desktop:**
1. That a read-only bind mount of `research\searxng` from Windows is readable by uid 977.
2. That `GRANIAN_HOST=0.0.0.0` works there.
3. That the overlay's worker-network merge works (proven in [S2] with the real compose file, on Linux).
4. That `docker compose down --volumes` with the overlay loaded removes `searxng_cache`.
5. That `mirror.gcr.io` carries the image (proven in the sandbox) if `REGISTRY` is set.
6. That naming services in `docker compose up -d` keeps Compose from pulling `searxng`.
7. Whether an `EXTRA_CA_BUNDLE` proxy exists (release notes say no).

**Code.** These were confirmed by reading and will be covered by tests:
1. The `untrusted()` fence bypass (it removes only the exact lower-case closing tag).
2. The unescaped `<request>` block (`planner.py:221-224`) and `<results>` block (`supervisor/agent.py`).
3. httpx logging full URLs at INFO, given the worker's INFO logging (`worker/__main__.py`).
4. That `check_public_host` lets unresolvable names through ("fail-open") [C3], so research URL checks must call `resolve_public` directly.

---

### Evidence files (absolute paths)
- [S] `/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/rg/sandbox/observations.md`, `.../rg/sandbox/compose.yaml`, `.../rg/sandbox/settings.yml`
- [S2] `/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/rg/draft-validation.md` (files run: `.../rg/runtime/` and `.../rg/draftcompose/`)
- [C1] [C2] [C3] `/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/rg/evidence-code.json` (keys `backend-agents`, `research-memory-ui`, `security-controls`)
- [D] `/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/rg/deepseek-docs.json`
- [DR] `/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/rg/deepseek-draft.md`
- [CR] `/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/rg/deepseek-critiques.json`
- [X1] `/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/rg/searxng-docker.json`
- [X2] `/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/rg/searxng-api-settings.json`
- [O] `/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/rg/cc-ops-fit.json`
- [R] `/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/sx/` and `/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/sx2/` (master; for example `searx_version.py`, `searx_cache.py`, `LICENSE`, `docs_admin_settings_settings_outgoing.rst`, `searx_preferences.py`)
- [RT] `/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/sxtag/` (pinned commit `d48c4b555`: `searx_webadapter.py`, `searx_webapp.py`, `searx_settings.yml`, `searx_settings_loader.py`, `searx_search___init__.py`, `searx_search_processors_abstract.py`, `searx_engines_json_engine.py`, `searx_engines_google_cse.py`, `searx_botdetection_*.py`, `searx_data_tracker_patterns.py`, `searx_plugins_tracker_url_remover.py`, `searx___init__.py`). The Docker Hub tag API, re-checked: `2026.10.4-d48c4b555` has index digest `sha256:76b0bf28…c072` and amd64 size 98,233,735 bytes.
- **Code read directly** (in `/home/user/Jarvis/.worktrees/jarvis-migration/command-center/`):
  - Compose and config: `docker-compose.yml`, `docker-compose.tunnel.yml`, `docker-compose.brain-import.yml`, `.env.example`, `backend/Dockerfile`.
  - Scripts: `scripts/init-env.ps1`, `start.ps1`, `status.ps1`, `install.ps1`, `backup.ps1`, `uninstall.ps1`, `brain-common.ps1`, `brain-test.ps1`, `build-package.py`.
  - Docs: `docs/REMOTE_ACCESS_PLAN.md`, `docs/RELEASE_NOTES_0.6.0.md`, `releases/GO_NO_GO_0.6.0.md`, `PROJECT_STATUS.md`.
  - Backend: `backend/app/agents/registry.py`, `agents/base.py`, `agents/http.py`, `agents/drafting.py`, `agents/seo.py`, `brain/store.py`, `brain/review.py`, `brain/policy.py`, `brain/text.py`, `migrations/0006_brain.sql`, `api/brain.py`, `api/work.py`, `api/supervisor.py`, `api/system.py`, `supervisor/agent.py`, `supervisor/planner.py`, `tasks.py`, `worker/runner.py`, `worker/__main__.py`, `credit_repair/store.py`, `models.py`, `config.py`, `licensing.py`, `worker/forwarder.py`.

**Reminder: nothing in this document has been implemented, installed, configured or called.**
