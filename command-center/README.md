# Infinite AI Command Center

A privately hosted AI automation platform for Infinite Marketing: a dashboard,
an AI supervisor that plans work and hands it to specialist agents, an approval
gate for anything that reaches the outside world, and n8n for cross-system
workflows. Everything runs in Docker on one machine and is reachable only from
that machine (127.0.0.1) until you deliberately put a TLS proxy in front.

Status, test results and open items: [PROJECT_STATUS.md](PROJECT_STATUS.md).
Install on Windows 11: [docs/DEPLOY_WINDOWS.md](docs/DEPLOY_WINDOWS.md).
Commercial edition plan and blockers: [COMMERCIALIZATION.md](COMMERCIALIZATION.md).
Third-party components and licenses: [SBOM.md](SBOM.md).

## What is in the box

```
 browser ──► dashboard (nginx + React)  :8090 on 127.0.0.1
                 │ /api
                 ▼
            api (FastAPI) ───────────────┐
                 │                       │ events (webhook + secret)
                 ▼                       ▼
            PostgreSQL 18  ◄──── worker ────► n8n 2.43.7  :5680 on 127.0.0.1
          (command_center,      │  agents      │ lead-intake webhook,
           n8n, credit_repair   │  scheduler   │ briefings; calls back
           databases)           │              │ with the service token
                                ▼              ▼
                     monitored websites   (future: email, Slack, ...)
                     (GET-only, public
                      addresses only)
```

## The AI Supervisor and the seven agents

The **AI Supervisor** plans requests (Claude or deterministic rules) and hands
the work to the seven agents; it never acts on a site or server itself.

| Agent | Does | Needs approval for |
|---|---|---|
| WordPress Security & Repair | Security scans (exposures, suspicious redirects, cloaking, injected scripts/spam, TLS), security reports, WP-CLI diagnosis over restricted SSH | every repair (restore home/siteurl, plugin deactivate/activate/update, reinstall of a WordPress.org plugin, core reinstall, maintenance mode, cache flush, quarantine debug.log); each one checks its result and reports a part-way failure loudly |
| SEO & Content | Read-only SEO audits, keyword briefs, blog drafts | publishing to WordPress (mock) |
| Social Media & Marketing (Postonomous) | Social drafts; ad report on **sample data** | publishing via Postonomous (mock); budget changes (mock) |
| VPS & Server Management (CWP/SSH) | Server overview and service checks through cc-gate | restarting an allowlisted service |
| Email & Customer Communications | Lead scoring, proposals, client-message triage and reply drafts | sending email (mock) |
| Website & Infrastructure Monitoring | Uptime (24 h / 7 d / 30 d), TLS, redirects, server reachability; read-only | nothing |
| Credit Repair & Dispute Management | Isolated module: compliance timing checks, dispute-letter drafts | mailing a letter (mock); off until an admin enables it |

"Mock" means the action is recorded in the **Outbox** page and nothing leaves
the machine. Server and WordPress repairs are real: they run on your server
through cc-gate after approval (production: an admin types the target's name).
See [docs/SERVER_SETUP.md](docs/SERVER_SETUP.md).

| Part | What it does |
|---|---|
| `backend/app/api` | REST API: sign-in, roles, sites, tasks, approvals, supervisor, integrations, audit log |
| `backend/app/agents` | Agent framework (capabilities with declared risk), the read-only HTTP client, the agents |
| `backend/app/credit_repair` | The Credit Repair store: its own database, login role, encryption key and access log |
| `backend/app/integrations` | Outward integrations (all mocks in this build) and the n8n client |
| `backend/app/ssh` | SSH to servers: key, pinned host keys, the allowlist, audit trail (cc-gate only) |
| `server/` | What you install on a server: cc-gate, cc-wp (WP-CLI in a clean environment), cc-site-helper, install-gate.sh, example config |
| `backend/app/supervisor` | The AI supervisor: plans requests (Claude or rules), summarises, daily briefing, health review |
| `backend/app/worker` | Runs tasks (PostgreSQL `SKIP LOCKED` queue), cron schedules, delivers events to n8n |
| `backend/app/migrations` | The database schema, as plain SQL |
| `backend/app/workflow.py` | The workflow-engine boundary (n8n, generic signed webhook, none) |
| `backend/app/licensing.py` | Editions, entitlements, signed license files |
| `backend/app/customer_settings.py`, `secrets.py` | Customer settings and encrypted customer secrets (deployment settings stay in `.env`) |
| `dashboard/` | The web dashboard |
| `n8n/` | Workflows and credentials provisioned into n8n at start-up |
| `scripts/` | Windows (PowerShell 5.1+) install, start, stop, status, backup, smoke test, uninstall |
| `postgres/provision.sh` | Creates the databases and login roles on every start (idempotent) |
| `templates/` | Your own proposal template (optional; not in git) |

## Safety model

- **Risk levels are code, not prompts.** Every agent capability declares
  `read`, `draft`, `external`, `financial` or `destructive`. The task layer
  holds anything above `draft` for a human approval; neither an agent nor the
  AI planner can change that.
- **Outward integrations are mocks** in this build and additionally refuse to
  run without an approval on the task.
- **Monitoring is read-only**: the agents' HTTP client sends only GET/HEAD and
  refuses private, loopback, shared (100.64/10) and metadata addresses, and
  connects only to the address it checked (no SSRF or DNS rebinding into the
  stack); redirects to foreign domains are recorded, never followed by the checks.
- **Servers only through cc-gate**: a non-root account whose key can run nothing
  but an allowlist of operations; host keys pinned after a person compares the
  fingerprint; every command audited; changes need approval, production changes
  an admin who types the target's name, and an approval only covers the exact
  target it was given for (re-linked, renamed or newly production targets are
  refused); WP-CLI runs as the site's own user in a clean environment; the
  server's own `allow_changes` switch can refuse all changes.
- **The n8n service token** can submit requests and acknowledge events; it
  cannot read dashboard data or approve anything.
- **Model text is data**: agent output and fetched pages go to the summariser
  wrapped as data, and the planner can only choose from real capabilities.
- Everything a person changes is in the audit log.
- **Credit Repair data is isolated**: a separate database and login role
  (the main application's role cannot connect to it), field encryption with
  its own key, a per-user permission (off for everyone, admins included),
  its own access log, and the supervisor and AI never see it; outside the
  store a client is only a reference such as `CR-0001`.

## Developing

```bash
# backend unit + integration tests (needs a PostgreSQL; see backend/tests/conftest.py)
cd backend && pip install -r requirements-dev.txt && pytest
# dashboard
cd dashboard && npm ci && npm test && npm run build
# whole stack with a throwaway local WordPress, then the end-to-end suite
./scripts/init-env.sh && docker compose -f docker-compose.yml -f docker-compose.testing.yml up -d --build
pip install playwright==1.56.0 pytest-playwright && pytest -c e2e/pytest.ini e2e
# clean Windows package (only tracked files; refuses if a secret would be included)
python scripts/build-package.py
```
