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
          (command_center,      │  agents      │ calls back with the
           n8n databases)       │  scheduler   │ service token
                                ▼              ▼
                     monitored websites   (future: email, Slack, ...)
                     (GET-only, public
                      addresses only)
```

| Part | What it does |
|---|---|
| `backend/app/api` | REST API: sign-in, roles, sites, tasks, approvals, supervisor, integrations, audit log |
| `backend/app/agents` | Agent framework (capabilities with declared risk), the read-only HTTP client, the agents |
| `backend/app/supervisor` | The AI supervisor: plans requests (Claude or rules), summarises, daily briefing, health review |
| `backend/app/worker` | Runs tasks (PostgreSQL `SKIP LOCKED` queue), cron schedules, delivers events to n8n |
| `backend/app/migrations` | The database schema, as plain SQL |
| `backend/app/workflow.py` | The workflow-engine boundary (n8n, generic signed webhook, none) |
| `backend/app/licensing.py` | Editions, entitlements, signed license files |
| `backend/app/customer_settings.py`, `secrets.py` | Customer settings and encrypted customer secrets (deployment settings stay in `.env`) |
| `dashboard/` | The web dashboard |
| `n8n/` | Workflows and credentials provisioned into n8n at start-up |
| `scripts/` | Windows (PowerShell 5.1+) install, start, stop, status, backup, smoke test, uninstall |

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
