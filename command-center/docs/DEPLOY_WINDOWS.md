# Deploying to a Windows 11 PC (Docker Desktop + WSL 2)

This puts the whole Command Center on your PC. It runs in its own Docker
Compose project, **`infinite-command-center`**, with its own containers,
network and volumes, so it does not touch XAMPP or other Docker projects
(including the Foundation Starter, project `infinite-ai-command-center`).

| What | Where |
|---|---|
| Installation package | `InfiniteAI-CommandCenter-0.1.0-windows.zip` (in the GitHub repo at `command-center/releases/`, with a `.sha256` file) |
| Source | GitHub `mytechmobilebiz/Jarvis`, branch `ccr-eb27a2af-8u7i39`, folder `command-center/` |
| Install location on the PC | `C:\InfiniteAI\InfiniteAI-CommandCenter` (outside `C:\xampp`) |
| Dashboard | http://localhost:8090 (bound to 127.0.0.1 only) |
| n8n editor | http://localhost:5680 (bound to 127.0.0.1 only) |
| Data | Docker volumes `infinite-command-center_pgdata` and `infinite-command-center_n8n_data` |

Ports were chosen to avoid XAMPP (80, 443, 3306, Tomcat 8080) and an n8n
already running on 5678. Change them in `.env` if the preflight reports a clash.
No database port is published, so XAMPP's MySQL/MariaDB is never involved.

## Prerequisites

- Windows 11 with Docker Desktop running, using the WSL 2 engine and Linux containers.
- About 5 GB free disk space and an internet connection for the first build
  (images and packages are downloaded from Docker Hub, PyPI and npm).
- Nothing else: no Python, Node.js or Git is needed on the PC for the zip route.

## Install (PowerShell, as your normal user, no administrator needed)

The scripts are run with `-ExecutionPolicy Bypass` for that one process only;
the machine-wide execution policy is not changed. `preflight.ps1` and
`status.ps1` are read-only; read any script first if you wish.

1. Download `InfiniteAI-CommandCenter-0.1.0-windows.zip` and, optionally,
   check it against the `.sha256` file:
   `Get-FileHash .\InfiniteAI-CommandCenter-0.1.0-windows.zip -Algorithm SHA256`
2. Extract it to `C:\InfiniteAI` (you get `C:\InfiniteAI\InfiniteAI-CommandCenter`).
3. Open PowerShell and run:

```powershell
Set-Location C:\InfiniteAI\InfiniteAI-CommandCenter
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 -AdminEmail you@yourdomain.com
```

`install.ps1` checks the machine, creates `.env` with fresh random secrets
(and prints the dashboard and n8n passwords once), validates the
configuration inside the application image, starts everything (the first
start creates the database roles, databases and tables) and runs the smoke
test. The first run takes 5-15 minutes. It must end with
`SMOKE TEST PASSED`; until it does on your PC, the application has not been
verified there. Re-running it is safe.

Then open http://localhost:8090 and sign in with the email and password that
were printed (they are also in `.env`).

Prefer Git? `git clone --branch ccr-eb27a2af-8u7i39 https://github.com/mytechmobilebiz/Jarvis.git`
and run the same command from `Jarvis\command-center`.

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

## Uninstall

```powershell
.\scripts\uninstall.ps1               # removes containers and images, KEEPS your data
.\scripts\uninstall.ps1 -DeleteData   # also deletes the data, after taking a final backup
```

Only the project `infinite-command-center` is touched. Shared base images
(postgres, n8n, nginx) are left in place for other projects; remove them in
Docker Desktop if nothing else uses them.

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

Your data lives in Docker volumes and your settings in `.env`, so an update
only replaces the program files:

```powershell
Set-Location C:\InfiniteAI\InfiniteAI-CommandCenter
.\scripts\backup.ps1
# extract the new zip to C:\InfiniteAI\InfiniteAI-CommandCenter-new, then:
Copy-Item .\.env ..\InfiniteAI-CommandCenter-new\.env
Set-Location ..\InfiniteAI-CommandCenter-new
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1   # rebuilds; migrations apply automatically
```

(With Git instead: `git pull`, then `install.ps1`.) Once the new folder is
verified, the old one can be deleted; keep its `backups\` folder.

## Trying it against a throwaway WordPress

The test overlay (in the Git repository, not in the zip) adds a disposable
local WordPress the monitor can watch:

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
