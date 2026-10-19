# Deploying to a Windows 11 PC (Docker Desktop + WSL 2)

This puts the whole Command Center on your PC. It runs in its own Docker
Compose project, **`infinite-command-center`**, with its own containers,
network and volumes, so it does not touch XAMPP or other Docker projects
(including the Foundation Starter, project `infinite-ai-command-center`).

| What | Where |
|---|---|
| Installation package | `InfiniteAI-CommandCenter-0.5.1-windows.zip` (in the GitHub repo at `command-center/releases/`, with a `.sha256` file) |
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

1. Download `InfiniteAI-CommandCenter-0.5.1-windows.zip` and, optionally,
   check it against the `.sha256` file:
   `Get-FileHash .\InfiniteAI-CommandCenter-0.5.1-windows.zip -Algorithm SHA256`
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
| `POSTGRES_PASSWORD`, `CC_DB_PASSWORD`, `N8N_DB_PASSWORD`, `CREDIT_REPAIR_DB_PASSWORD` | Database superuser and the three application roles (the Credit Repair role is separate; the main application cannot open that database) |
| `CC_SERVICE_TOKEN` | n8n calling the Command Center API |
| `CC_SECRETS_KEY` | Encrypts API keys entered on the Settings page. **Back it up.** |
| `CREDIT_REPAIR_DATA_KEY` | Encrypts Credit Repair client fields. **Back it up**; without it that data cannot be read. |
| `N8N_WEBHOOK_SECRET` | Command Center calling n8n's event webhook |
| `N8N_ENCRYPTION_KEY` | Encrypts n8n's stored credentials. **Back it up.** |
| `N8N_OWNER_PASSWORD` | n8n editor sign-in |
| `ANTHROPIC_API_KEY` (optional) | Deployment default for Claude; normally leave empty and enter your key on the dashboard's Settings page instead (stored encrypted) |
| `DASHBOARD_PORT`, `N8N_PORT`, `TZ` | Local ports and time zone |

Upgrading from 0.5.0 to 0.5.1 needs no new keys and changes no data: there
are no migrations, `.env` stays as it is, and the agents, approval rules,
PostgreSQL and n8n volumes are not touched. 0.5.1 fixes the wrong version
numbers after an upgrade (on the owner's PC the 0.5.0 smoke test failed with
"the API reports 0.2.0 but this folder is 0.5.0", and the dashboard showed
v0.4.0): only the files holding the version numbers had been built from old
copies Docker kept; all other code was current. **Do not follow the 0.5.0
banner's advice to run install.ps1 from an older folder**; install 0.5.1 as
in "Updating" below, then reload the dashboard page. Exact steps, and what
0.5.1 changes on servers (cc-gate 1.2.0 for plugin updates):
[RELEASE_NOTES_0.5.1.md](RELEASE_NOTES_0.5.1.md).

Upgrading from 0.4.0 to 0.5.0 needs no new keys and changes no
data: there are no new migrations, and the agents, their on/off states and the
approval rules stay as they are. Follow "Updating" below. One thing to know
first: pending approvals from 0.4.0 for a repair, a service restart, an email
or a publication cannot be approved after the upgrade (0.5.0 approvals name
the exact draft, version or key, and those older requests do not). Reject them
and request them again. See [RELEASE_NOTES_0.5.0.md](RELEASE_NOTES_0.5.0.md).

Upgrading from 0.3.0 to 0.4.0 needs no new keys and changes no data: 0.4.0
is the redesigned dashboard; the database, agents and approval rules are
unchanged.

Upgrading from 0.2.0 to 0.3.0 needs no new keys; follow "Updating" below. The
first start renames the agents to the seven official ones and keeps all your
data, switched-off agents included.

Upgrading from 0.1.0: `install.ps1` adds the three keys introduced since
then (`CC_SECRETS_KEY`, `CREDIT_REPAIR_DB_PASSWORD`, `CREDIT_REPAIR_DATA_KEY`)
to your existing `.env` and changes nothing else in it. To do only that step:
`.\scripts\init-env.ps1 -AddMissing`.

## Your proposal template (optional)

The Sales agent drafts proposals from `templates\proposal-template.html` if
the file exists, otherwise from a plain built-in template without prices.
Put your own template there (placeholders such as `{{CLIENT_NAME}}` and
`{{CLIENT_COMPANY}}`; see `templates\README.md`). The folder
is read-only to the application and is not included in backups or git.

## The dashboard (redesigned in 0.4.0, closer to the reference in 0.5.0)

The Dashboard page shows the four stat cards (AI agents, websites, tasks
today, website uptime), a card for each of the seven agents, task activity by
category, service status, recent tasks and active workflows. Every value comes
from the Command Center itself. Where nothing can be measured it says so:
**PC Resources** and the **Intern 2** card in the sidebar show "Not connected"
because no source for them exists yet. **Quick Actions** in the sidebar run
read-only checks and reports (and, for admins, open the n8n editor). The
bottom of the sidebar shows the dashboard's and the API's versions.

Type a request into the command input at the top of every page (or press
<kbd>Ctrl</kbd>+<kbd>K</kbd>) and press **Execute**; it goes to the AI
Supervisor with your sign-in and the usual approval rules. The answer appears
under the input. The sidebar holds every area; the **Knowledge Base** page
explains each one.

The microphone button next to the input is optional voice dictation. It
appears only in a browser that supports speech recognition (Chrome or Edge)
when the dashboard is opened as `http://localhost:8090` (browsers allow the
microphone only on secure addresses, and `localhost` counts as one). The first
time, the dashboard asks for your consent and the browser asks for microphone
permission. Chrome and Edge send the audio to Google or Microsoft to turn it
into text. Dictation only fills the input; nothing runs until you press
Execute.

## Servers and WordPress repairs (new in 0.3.0)

The **CWP / Servers** page connects the Command Center to your CWP servers through a
restricted, non-root SSH account that can only run an allowlist of operations
(`cc-gate`). Setting a server up is something you do on the server yourself;
follow [SERVER_SETUP.md](SERVER_SETUP.md). The `server` folder of this package
holds what you copy there. Until you do, everything else works as before and no
SSH connection is made.

**Scan host key** is carried out by the worker (since 0.5.0; before, the scan ran
in the API container, which has no route to the internet, and could not reach
a real server). It reads only the key the server presents; nothing is trusted
until you compare the fingerprint and press Trust.

## Remote access (optional, not set up)

Reaching the dashboard from outside the PC (for example at
`https://command.infinitemarketingai.com` through Cloudflare Tunnel and
Cloudflare Access) is planned but **not switched on**: see
[REMOTE_ACCESS_PLAN.md](REMOTE_ACCESS_PLAN.md). It needs the owner's approval at
each step, opens no router ports and leaves the local http://localhost:8090
unchanged. Its files (`docker-compose.tunnel.yml`, `remote-access\tunnel.conf`)
do nothing until `.env` switches them on.

## Credit Repair module

Off by default. To try it, sign in as admin, tick **Sensitive data** for your
own user on the Settings page, open **Credit Repair**, read the compliance
notice and enable the module. Use made-up test data only until the module has
had legal review (see COMMERCIALIZATION.md, blocker B5). Client data lives in
its own database (`credit_repair`) and is encrypted with
`CREDIT_REPAIR_DATA_KEY`; `backup.ps1` includes it.

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
.\scripts\backup.ps1     # dumps the databases to .\backups\<timestamp>\
.\scripts\restore.ps1 -BackupFolder .\backups\<timestamp>   # replaces current data; backs it up first, asks to confirm
```

Keep a copy of `.env` with your backups: the encryption keys in it are needed
to read stored credentials and Credit Repair data.

(Prefix each with `powershell -NoProfile -ExecutionPolicy Bypass -File` if
script execution is restricted.)

Never run `docker compose down -v` unless you intend to delete all data.

## Updating

Your data lives in Docker volumes and your settings in `.env`, so an update
only replaces the program files:

```powershell
# Your current installation folder (the one with .env), and a new folder for the new version:
$old = "C:\InfiniteAI\InfiniteAI-CommandCenter"
$new = "C:\InfiniteAI\new\InfiniteAI-CommandCenter"   # Extract All of the zip into C:\InfiniteAI\new creates this
Set-Location $old
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup.ps1
# now extract the new zip into C:\InfiniteAI\new, then:
Copy-Item "$old\.env" "$new\.env"
if (Test-Path "$old\templates\proposal-template.html") { Copy-Item "$old\templates\proposal-template.html" "$new\templates\" }
Set-Location $new
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1   # rebuilds; migrations apply automatically
```

If `.env` was not copied, `install.ps1` stops with "this folder has no .env, but
this PC already has Command Center data" (from 0.5.1): it never creates new
keys over existing data.

If remote access is switched on, the copied `.env` keeps it on; its token stays
in `C:\InfiniteAI\secrets`, outside the installation folder.

From 0.5.1, `install.ps1` gives the files Docker builds from a fresh
timestamp and the build checks that the copied code is this folder's
version, so extracting into a new folder (as above) or over the old one both
give current images. If the dashboard was open in a browser during the
update, reload the page afterwards: an open page keeps the previous version's
code until it is reloaded.

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

- **"This page is out of date" banner**: the page was open during an update and still runs the previous dashboard. Reload the page.
- **Version mismatch banner, or the smoke test's "versions match this folder" fails**: first reload the dashboard page. If it stays, the API or the dashboard was not built from the files of the version you installed. Up to 0.5.0, Docker could build a new release with out-of-date copies of the files that hold the version numbers (it kept them from an earlier build, whether the zip was extracted over the old folder or into a new one; `--no-cache` did not help). From 0.5.1, `install.ps1` and `start.ps1` refresh those files before every build and the build stops if the copied code is the wrong version, so run `install.ps1` again from the new version's folder (with the `.env` copied there), then `smoke.ps1`. `.\scripts\status.ps1` prints the folder's version, the versions the API and the dashboard report, and the folder the API container was created from.
- **The build stops with "... in this build says X, but this release is Y: Docker used an out-of-date copy ..."**: the check above caught an image about to be built from old files; nothing was changed. Run `install.ps1` again (it refreshes the files first). If it stops again, send the message and the output of `status.ps1`.
- **"Could not prepare the build folders: this folder mixes releases"**: the folder's `VERSION` file and its code name different versions (an extraction that was interrupted, or files skipped when extracting over an older folder). Extract the zip again into a new, empty folder, copy `.env` there and run `install.ps1` from it. Nothing was built or changed.
- **"No internet on this PC" banner**: the monitor could not reach api.wordpress.org, www.cloudflare.com or www.google.com, so websites are marked "not checked" instead of down. If your network blocks those hosts on purpose, set `MONITOR_CONTROL_URLS` in `.env` to addresses that answer (see `.env.example`), then `start.ps1 -NoBuild`.
- **Scan host key says the worker is not running**: `.\scripts\status.ps1`, then `start.ps1 -NoBuild`.
- **No microphone button**: open the dashboard as `http://localhost:8090` (not the PC's IP address) in Chrome or Edge, and sign in as an admin or operator; viewers cannot send commands. If the browser blocked the microphone, allow it from the padlock/site icon in the address bar.
- **Port in use**: set `DASHBOARD_PORT` / `N8N_PORT` in `.env`, then `start.ps1`.
- **`docker compose up` fails pulling images**: check Docker Desktop is signed in or that Docker Hub is reachable.
- **n8n unhealthy**: `docker compose logs --tail=100 n8n`. First start takes about a minute.
- **Cannot sign in to n8n over plain HTTP from another machine**: by design; n8n is local-only until a TLS reverse proxy is added.
