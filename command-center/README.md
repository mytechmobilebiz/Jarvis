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

## The agents

| Agent | Does | Needs approval for |
|---|---|---|
| AI Supervisor | Plans requests (Claude or rules) and hands them to the agents below; summaries, daily briefing, health review | nothing (it only delegates) |
| WordPress & Hosting Monitor | Read-only uptime, TLS, WordPress version and exposure checks; read-only server checks (DNS, TCP connect, TLS) | nothing (read-only) |
| Sales & Proposals | Scores leads, drafts proposals from `templates/proposal-template.html` | sending a proposal (mock email) |
| Client Support | Triages client messages, drafts replies | sending a reply (mock email) |
| Content & Social | Drafts blog and social posts | publishing (mock WordPress / social) |
| SEO & Research | Read-only on-page SEO audits; keyword and content briefs | nothing (read-only / drafts) |
| Ads & Analytics | Ad performance report on **sample data** (no ad account connected) | any budget change (financial, mock) |
| Credit Repair | Isolated module: compliance timing checks, dispute-letter drafts from an encrypted store | mailing a letter (mock); off until an admin enables it |

"Mock" means the action is recorded in the **Outbox** page and nothing leaves
the machine. Real integrations need your approval and credentials (see
PROJECT_STATUS.md).

| Part | What it does |
|---|---|
| `backend/app/api` | REST API: sign-in, roles, sites, tasks, approvals, supervisor, integrations, audit log |
| `backend/app/agents` | Agent framework (capabilities with declared risk), the read-only HTTP client, the agents |
| `backend/app/credit_repair` | The Credit Repair store: its own database, login role, encryption key and access log |
| `backend/app/integrations` | Outward integrations (all mocks in this build) and the n8n client |
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
  refuses private, loopback and metadata addresses (no SSRF into the stack).
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
