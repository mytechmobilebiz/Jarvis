# Deploying to a Windows 11 PC (Docker Desktop + WSL 2)

This puts the whole Command Center on your PC. It runs in its own Docker
Compose project, **`infinite-command-center`**, with its own containers,
network and volumes, so it does not touch XAMPP or other Docker projects
(including the Foundation Starter, project `infinite-ai-command-center`).

| What | Where |
|---|---|
| Source | GitHub `mytechmobilebiz/Jarvis`, branch `ccr-eb27a2af-8u7i39`, folder `command-center/` |
| Suggested location on the PC | `C:\InfiniteAI\Jarvis\command-center` (outside `C:\xampp`) |
| Dashboard | http://localhost:8090 (bound to 127.0.0.1 only) |
| n8n editor | http://localhost:5680 (bound to 127.0.0.1 only) |
| Data | Docker volumes `infinite-command-center_pgdata` and `infinite-command-center_n8n_data` |

Ports were chosen to avoid XAMPP (80, 443, 3306, Tomcat 8080) and an n8n
already running on 5678. Change them in `.env` if the preflight reports a clash.

## Prerequisites

- Windows 11 with Docker Desktop running, using the WSL 2 engine and Linux containers.
- Git for Windows (or download the branch as a ZIP from GitHub).
- About 5 GB free disk space and an internet connection for the first build
  (images and packages are downloaded from Docker Hub, PyPI and npm).

## Install (PowerShell, as your normal user, no administrator needed)

The scripts are run with `-ExecutionPolicy Bypass` for that one process only;
the machine-wide execution policy is not changed. Read any script first if you
wish: `preflight.ps1` and `status.ps1` are read-only.

```powershell
# 1. Get the code
New-Item -ItemType Directory -Force C:\InfiniteAI | Out-Null
Set-Location C:\InfiniteAI
git clone --branch ccr-eb27a2af-8u7i39 https://github.com/mytechmobilebiz/Jarvis.git
Set-Location C:\InfiniteAI\Jarvis\command-center

# 2. Check the machine (changes nothing)
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\preflight.ps1

# 3. Create .env with fresh random secrets (prints your sign-in passwords once)
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\init-env.ps1 -AdminEmail you@yourdomain.com

# 4. Build and start (first run: 5-15 minutes)
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1

# 5. Verify end to end
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1
```

`smoke.ps1` must end with `SMOKE TEST PASSED`. Until it does on your PC, the
application has not been verified there.

Then open http://localhost:8090 and sign in with the email and password that
`init-env.ps1` printed (they are also in `.env`).

## Environment variables

`init-env.ps1` writes all of these; see `.env.example` for the full list.

| Variable | Purpose |
|---|---|
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | First dashboard admin; the email is also the n8n owner |
| `POSTGRES_PASSWORD`, `CC_DB_PASSWORD`, `N8N_DB_PASSWORD` | Database superuser and the two application roles |
| `CC_SERVICE_TOKEN` | n8n calling the Command Center API |
| `N8N_WEBHOOK_SECRET` | Command Center calling n8n's event webhook |
| `N8N_ENCRYPTION_KEY` | Encrypts n8n's stored credentials. **Back it up.** |
| `N8N_OWNER_PASSWORD` | n8n editor sign-in |
| `ANTHROPIC_API_KEY` (optional) | Claude for planning and summaries; metered, capped by `LLM_DAILY_REQUEST_CAP` |
| `DASHBOARD_PORT`, `N8N_PORT`, `TZ` | Local ports and time zone |

## Day to day

```powershell
.\scripts\status.ps1     # what is running (read-only)
.\scripts\stop.ps1       # stop; data is kept
.\scripts\start.ps1 -NoBuild
.\scripts\backup.ps1     # dumps both databases to .\backups\<timestamp>\
```

(Prefix each with `powershell -NoProfile -ExecutionPolicy Bypass -File` if
script execution is restricted.)

Never run `docker compose down -v` unless you intend to delete all data.

## Updating

```powershell
Set-Location C:\InfiniteAI\Jarvis\command-center
.\scripts\backup.ps1
git pull
.\scripts\start.ps1      # rebuilds; database migrations apply automatically
.\scripts\smoke.ps1
```

## Trying it against a throwaway WordPress

The test overlay adds a disposable local WordPress the monitor can watch:

```powershell
docker compose -f docker-compose.yml -f docker-compose.testing.yml up -d --build
```

Add `http://wp-test` on the Websites page. Remove it with
`docker compose -f docker-compose.yml -f docker-compose.testing.yml rm -sf wp-test wp-test-db wp-test-setup`
followed by `docker volume rm infinite-command-center_wp_test_html`.

## Troubleshooting

- **Port in use**: set `DASHBOARD_PORT` / `N8N_PORT` in `.env`, then `start.ps1`.
- **`docker compose up` fails pulling images**: check Docker Desktop is signed in or that Docker Hub is reachable.
- **n8n unhealthy**: `docker compose logs --tail=100 n8n`. First start takes about a minute.
- **Cannot sign in to n8n over plain HTTP from another machine**: by design; n8n is local-only until a TLS reverse proxy is added.
