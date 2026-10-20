# Upgrade your Command Center from 0.5.0 to 0.6.0 (Windows PowerShell)

Date: 2026-10-20 · for the installation in
`C:\InfiniteAI\v0.5.0\InfiniteAI-CommandCenter` · every command below was
run in the cloud sandbox (PowerShell 7.4, against a running Command Center), except the
download itself and the Windows-only commands (`Get-PSDrive C`, `Expand-Archive`).

**What this upgrade does:** it replaces the program files and rebuilds the
API, worker and dashboard images. It then recreates the API, worker,
dashboard and n8n containers from a new folder, so n8n restarts once.
PostgreSQL keeps running. One database migration adds nine empty JARVIS
Brain tables.

**What it does not do:**
- It does not delete or reset any Docker volume, and it does not reset
  PostgreSQL or n8n.
- It does not change your `.env`. The new folder gets a copy.
- It does not touch JARVIS, import any JARVIS history, or activate any
  workflow.
- It does not contain DeepSeek or any other web-search feature. That comes
  in a later release, after its own approval.

You run every step yourself. Nothing here runs on its own.

## 0. Which release, and why

| Release | Where | SHA-256 of the zip | State |
|---|---|---|---|
| **0.6.0 (recommended)** | GitHub `mytechmobilebiz/Jarvis`, branch `jarvis-migration`, `command-center/releases/InfiniteAI-CommandCenter-0.6.0-windows.zip` | `a687f737de043ae6a69c4dcc3f15ae71d71a5c3688bebef62b53122e4036a8a1` | latest completed, tested and published release |
| 0.5.1 (fallback only) | the same folder, on branch `jarvis-migration` or `ccr-eb27a2af-8u7i39` | `569077933fd21576de1f4c534a6ab4c66eaae02fd365fc502d5c6a01b727d45c` | contains only the version fix and the plugin-update pin; 0.6.0 includes both |

I checked both copies on GitHub: each zip has exactly the SHA-256 in its
`.sha256` file.

**You do not need 0.5.1 first.** 0.6.0 contains every 0.5.1 change,
including the fix for "the API reports 0.2.0". Both routes were tested:
0.5.0 → 0.6.0 and 0.5.0 → 0.5.1 → 0.6.0.

**How 0.6.0 was tested** (cloud sandbox: Linux, Docker 29, the scripts in
PowerShell 7.4; synthetic data):

- **Backend:** 853 passed, 0 failed.
- **Dashboard:** 218 passed; type check clean; production build OK.
- **End-to-end:** 98 passed, 0 failed, including 19 browser checks of the new
  JARVIS Brain page.
- **Upgrade from a rebuilt copy of your history:** passed.
  - The rebuild covered 0.2.0, then 0.4.0 and 0.5.0 in one folder, and
    reproduced your "API reports 0.2.0".
  - Data, encrypted secrets, n8n and its encryption key, agents and volumes
    were identical before and after.
  - An unrelated Docker project was untouched.
- **Security review:** six independent reviewers. All critical and high
  findings are fixed. None is open.

Details: `docs/RELEASE_NOTES_0.6.0.md` and `releases/GO_NO_GO_0.6.0.md`.

**Windows PowerShell 5.1.** The scripts have not been run under Windows
PowerShell 5.1, because the sandbox has no Windows.

- Every script parses and is plain ASCII.
- The scripts this upgrade runs are close to the ones you already ran:
  - `backup.ps1`, `preflight.ps1` and `restore.ps1` are unchanged from your
    0.5.0;
  - `install.ps1`, `smoke.ps1`, `start.ps1` and `status.ps1` changed by 5 to
    18 lines;
  - `prepare-build.ps1` is new and small.
- A scan found no PowerShell 6/7-only syntax in them.

Your PC is the first Windows run: if anything fails, stop and send the
output.

**One known defect, not in the upgrade path.** In 0.6.0, an admin cannot
withhold an archived JARVIS message once it has a promoted copy that is not
quarantined. The page shows an error and nothing changes (found on
2026-10-20 and confirmed by a test run). It matters only when you review
imported JARVIS data, and nothing is imported by this upgrade.

- **Workaround:** delete and purge the promoted copy first, then withhold
  the message.
- **Fix:** a small 0.6.1 patch (one migration and a test), to be released and
  approved before the test-copy import.

**JARVIS migration kit.** The kit inside 0.6.0 is byte-for-byte the kit
0.1.0 you already used (`jarvis-migration-kit-0.1.0.zip`, SHA-256
`77f9351ea634e03dcdc47585f19506b3f981c9711127bf31e040b68e16d77bca`). This
upgrade does not use it.

## How to use this guide

Do it at a quiet time: nobody using the dashboard, n8n or the agents. The
upgrade restarts n8n as well, so a workflow that is running at that moment is
interrupted. The whole upgrade takes about 30 to 45 minutes, and the Command
Center is unavailable for about 5 to 15 of them.

Open **Windows PowerShell** as your normal user (not as administrator) and
keep **this one window** open from start to end: later steps use the names
set in step A.

Run this first. It allows scripts in **this window only**, and closing the
window undoes it; nothing changes for the rest of Windows. Without it, Windows'
default policy can block even `Get-FileHash`:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
```

Each step ends with a **STOP rule**. If what you see does not match, stop
there and send me the output. Nothing has been changed until step G (install),
apart from the backup files and the copies in step F.

The guide writes everything you need to send me into
`C:\InfiniteAI\upgrade-0.6.0` (no passwords or keys).

## Before installation

### A. Names and the current installation (your item 1: version and containers)

```powershell
$old  = "C:\InfiniteAI\v0.5.0\InfiniteAI-CommandCenter"
$new  = "C:\InfiniteAI\v0.6.0\InfiniteAI-CommandCenter"
$work = "C:\InfiniteAI\upgrade-0.6.0"
New-Item -ItemType Directory -Force -Path $work | Out-Null
Test-Path "$old\.env"
Test-Path "C:\InfiniteAI\v0.6.0"
Set-Location $old
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\status.ps1 2>&1 | Tee-Object "$work\status-before.txt"
docker ps -a --format "table {{.Names}}\t{{.Status}}\t{{.Image}}" | Tee-Object "$work\containers-before.txt"
```

- **What you should see:**
  - `Test-Path` prints `True`, then `False`.
  - `status.ps1` lists the five `infinite-command-center` containers
    (postgres, api, worker, dashboard, n8n) as running or healthy.
  - `status.ps1` shows `Dashboard/API: HTTP 200` and `n8n: HTTP 200`.
  - Your 0.5.0 may report the API as 0.2.0: that is the known 0.5.0 problem
    that 0.6.0 fixes.
- **STOP if:**
  - the first `Test-Path` prints `False`: set `$old` to the folder that holds
    your `.env`;
  - a container is missing or restarting;
  - a probe is not HTTP 200.
- **If the second `Test-Path` prints `True`:** you extracted 0.6.0 before.
  - If you already ran `install.ps1` from it, stop and send me `status.ps1`.
  - Otherwise move it aside (nothing is deleted) and continue:
    `Rename-Item "C:\InfiniteAI\v0.6.0" "v0.6.0-old-extract"`.

### B. Prerequisites and disk space (your items 5 and 7)

```powershell
Set-Location $old
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\preflight.ps1 2>&1 | Tee-Object "$work\preflight.txt"
docker system df | Tee-Object "$work\docker-df.txt"
Get-PSDrive C | Select-Object @{n="FreeGB_C";e={[math]::Round($_.Free/1GB,1)}}
```

`preflight.ps1` only reads (it is the same in 0.5.0 and 0.6.0). It checks:
- Docker Desktop is running, Compose is available, Linux containers on WSL 2;
- the two ports (used by this Command Center is fine);
- your other Docker projects, which are left untouched;
- the free disk space;
- that `.env` has every required secret.

Docker Desktop keeps its images and volumes on drive C: by default, so the C:
figure covers Docker storage too.

- **STOP if:**
  - any line says `[FAIL]`;
  - it says `.env is from an older version; install.ps1 will add ...`. The
    installer would then append keys to your `.env`, so send me the output
    first;
  - there are fewer than 10 GB free on C:.

### C. The volumes that hold your data (your item 6)

```powershell
foreach ($c in "infinite-command-center-postgres-1", "infinite-command-center-n8n-1") {
  docker inspect $c --format '{{.Name}}: {{range .Mounts}}[{{.Type}} {{.Name}} {{.Destination}}] {{end}}'
}
docker volume ls --filter name=infinite-command-center | Tee-Object "$work\volumes-before.txt"
```

- **What you should see:**
  - postgres uses the volume `infinite-command-center_pgdata` (at
    `/var/lib/postgresql`);
  - n8n uses `infinite-command-center_n8n_data` (at `/home/node/.n8n`).
- **STOP if:** the names differ. The checks below assume these names.

### D. A fingerprint of your data (no passwords or keys are shown)

```powershell
$sql = @'
SELECT 'users', md5(string_agg(concat_ws(',',id,email,role,password_hash,can_access_sensitive,is_active),'|' ORDER BY id)) FROM users
UNION ALL SELECT 'secret_values', md5(string_agg(concat_ws(',',name,ciphertext,hint),'|' ORDER BY name)) FROM secret_values
UNION ALL SELECT 'app_settings', md5(string_agg(concat_ws(',',key,value::text),'|' ORDER BY key)) FROM app_settings
UNION ALL SELECT 'agent_state', string_agg(name||'='||enabled,',' ORDER BY name) FROM agent_state
UNION ALL SELECT 'sites', md5(string_agg(concat_ws(',',id,name,url,client,enabled,environment),'|' ORDER BY id)) FROM sites
UNION ALL SELECT 'leads', md5(string_agg(concat_ws(',',id,company,email,score,status),'|' ORDER BY id)) FROM leads
UNION ALL SELECT 'schedules', md5(string_agg(concat_ws(',',id,name,agent,capability,cron,enabled),'|' ORDER BY id)) FROM schedules
UNION ALL SELECT 'schedules_enabled', count(*)::text FROM schedules WHERE enabled;
'@
$n8nSql = "SELECT 'n8n_workflows', count(*)::text || ' ' || coalesce(md5(string_agg(id||':'||name,'|' ORDER BY id)),'none') FROM workflow_entity UNION ALL SELECT 'n8n_credentials_yours', coalesce(md5(string_agg(id||data,'|' ORDER BY id)),'none') FROM credentials_entity WHERE id NOT IN ('ccApiToken000001','ccWebhookSecret1') UNION ALL SELECT 'n8n_credentials_cc', coalesce(string_agg(id||':'||type,',' ORDER BY id),'none') FROM credentials_entity WHERE id IN ('ccApiToken000001','ccWebhookSecret1');"
function Save-State($folder, $file) {
  Set-Location $folder
  & {
    $sql | docker compose exec -T postgres psql -U postgres -d command_center -At
    $n8nSql | docker compose exec -T postgres psql -U postgres -d n8n -At
    "n8n_encryption_config " + (docker compose exec -T n8n sh -c "sha256sum < /home/node/.n8n/config | cut -c1-16")
    docker volume inspect infinite-command-center_pgdata infinite-command-center_n8n_data --format "{{.Name}} {{.CreatedAt}}"
    "env " + (Get-FileHash "$folder\.env" -Algorithm SHA256).Hash
  } 2>&1 | Set-Content -Encoding ASCII $file
  $others = @(docker ps -a --format "{{.Names}} {{.ID}} {{.CreatedAt}}" | Where-Object { $_ -notmatch '^infinite-command-center-' } | Sort-Object)
  if ($others.Count -eq 0) { $others = @("(no other containers)") }
  $others | Set-Content -Encoding ASCII ($file -replace '\.txt$', '-other-containers.txt')
}
Save-State $old "$work\before.txt"
Get-Content "$work\before.txt"
```

- **What you should see:** 15 lines:
  - 8 database lines: `users`, `secret_values`, `app_settings`,
    `agent_state`, `sites`, `leads`, `schedules`, `schedules_enabled`;
  - `n8n_workflows` (your number of n8n workflows and a fingerprint of their
    IDs and names);
  - `n8n_credentials_yours` (the credentials you created in n8n, or `none`);
  - `n8n_credentials_cc` (the Command Center's two credentials);
  - `n8n_encryption_config`;
  - 2 volume lines with their creation time;
  - the `env` line.

  `agent_state` lists only the agents whose on/off setting was ever changed
  (for example `seo_content=false`). A line can be empty after the `|` when
  that table is empty (`app_settings|`): that is fine.
- **STOP if:** any line says `ERROR`, or one of the 15 lines is missing.

**JARVIS (read-only):** note how it runs now.

```powershell
$j = Get-Process | Where-Object { $_.ProcessName -match 'hermes|jarvis' } | Select-Object ProcessName, Id, StartTime | Out-String
if (-not $j.Trim()) { $j = "(no process named hermes or jarvis)" }
$j | Tee-Object "$work\jarvis-before.txt"
```

If this prints nothing, JARVIS runs under another name; note it your usual way
(for example send it a test message now and again at the end).

### E. Full backup, and proof that it restores (your item 3)

```powershell
Set-Location $old
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup.ps1
if ($LASTEXITCODE -ne 0) { Write-Host "BACKUP FAILED (exit $LASTEXITCODE): stop here and send me the output" -ForegroundColor Red }
$bk = Get-ChildItem "$old\backups" -Directory | Sort-Object Name | Select-Object -Last 1
"backup folder: " + $bk.FullName | Tee-Object "$work\backup.txt"
foreach ($db in "command_center", "n8n", "credit_repair") {
  $f = Join-Path $bk.FullName "$db.dump"
  if (-not (Test-Path $f)) { "MISSING $db.dump" | Tee-Object -Append "$work\backup.txt"; continue }
  $tmp = "restore_check_$db"
  docker compose cp $f "postgres:/tmp/restore-check-$db.dump" | Out-Null
  docker compose exec -T postgres dropdb -U postgres --if-exists $tmp
  docker compose exec -T postgres createdb -U postgres $tmp
  docker compose exec -T postgres pg_restore -U postgres --no-owner --no-privileges --exit-on-error -d $tmp "/tmp/restore-check-$db.dump"
  $rc = $LASTEXITCODE
  $tables = (docker compose exec -T postgres psql -U postgres -d $tmp -Atc "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'" | Out-String).Trim()
  docker compose exec -T postgres dropdb -U postgres $tmp
  docker compose exec -T postgres rm -f "/tmp/restore-check-$db.dump"
  $line = "{0}: {1:N0} bytes, SHA-256 {2}, trial restore exit {3}, {4} tables" -f $db, (Get-Item $f).Length, (Get-FileHash $f).Hash, $rc, $tables
  $line | Tee-Object -Append "$work\backup.txt"
}
docker compose exec -T postgres psql -U postgres -Atc "SELECT 'temporary databases left: ' || count(*) FROM pg_database WHERE datname LIKE 'restore_check_%'"
```

What the trial restore does: it copies each dump into the PostgreSQL
container and restores it into a **new, temporary database**
(`restore_check_...`). It then counts that database's tables and **deletes
it** again. Your real databases are not touched; this proves that each backup
file restores completely.

- **What you should see:**
  - `backup.ps1` ends with `Backup written to ...`;
  - each of the three databases shows `trial restore exit 0` and more than 0
    tables;
  - the last line says `temporary databases left: 0`.
- **STOP if:**
  - it printed `BACKUP FAILED`;
  - any of the three dumps is `MISSING` (your installation has had all three
    databases since 0.2.0);
  - an exit code is not 0, or a table count is 0.

The `NOTICE: database ... does not exist, skipping` lines are normal.

### F. Keep `.env` and the encryption keys safe (your item 4)

```powershell
New-Item -ItemType Directory -Force -Path "C:\InfiniteAI\keys-backup-0.5.0" | Out-Null
Copy-Item "$old\.env" "C:\InfiniteAI\keys-backup-0.5.0\env-0.5.0.txt"
Copy-Item "$old\.env" (Join-Path $bk.FullName "env-at-backup.txt")
(Get-FileHash "$old\.env").Hash -eq (Get-FileHash "C:\InfiniteAI\keys-backup-0.5.0\env-0.5.0.txt").Hash
```

Prints `True`. `.env` holds all your passwords and the encryption keys
(`CC_SECRETS_KEY`, `N8N_ENCRYPTION_KEY`, `CREDIT_REPAIR_DATA_KEY`): without
it, the backup's stored secrets cannot be decrypted. Keep these copies private.
Also copy `C:\InfiniteAI\keys-backup-0.5.0` to a USB stick or your password
manager. Never send or paste it anywhere. Your original `.env` stays where it
is, untouched.

### G-1. Download and check the package (your item 2)

Download (it saves to your Downloads folder):
https://github.com/mytechmobilebiz/Jarvis/raw/jarvis-migration/command-center/releases/InfiniteAI-CommandCenter-0.6.0-windows.zip

```powershell
$zip = "$env:USERPROFILE\Downloads\InfiniteAI-CommandCenter-0.6.0-windows.zip"
$hash = (Get-FileHash $zip -Algorithm SHA256).Hash
$hash | Tee-Object "$work\zip-sha256.txt"
$hash -eq "A687F737DE043AE6A69C4DCC3F15AE71D71A5C3688BEBEF62B53122E4036A8A1"
```

- **What you should see:** the hash, then `True`.
- **STOP if:** it prints `False`. Delete the file, download it again, and do
  not go on until it matches.

## Installation

### G-2. Extract into the new folder and copy `.env` (a copy; the original stays)

```powershell
Expand-Archive -Path $zip -DestinationPath "C:\InfiniteAI\v0.6.0"
Get-Content "$new\VERSION"
Test-Path "$new\.env"
Copy-Item "$old\.env" "$new\.env"
if (Test-Path "$old\templates\proposal-template.html") { Copy-Item "$old\templates\proposal-template.html" "$new\templates\" }
(Get-FileHash "$old\.env").Hash -eq (Get-FileHash "$new\.env").Hash
```

- **What you should see:** `0.6.0`, then `False` (no `.env` in the new folder
  yet), then `True`.

Explorer's right-click, Extract All, into `C:\InfiniteAI\v0.6.0` gives the
same folder.

### G-3. Install 0.6.0 from the new folder

```powershell
if ((Test-Path "$new\VERSION") -and (Get-Content "$new\VERSION" -TotalCount 1).Trim() -eq "0.6.0") {
  Set-Location $new
  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 2>&1 | Tee-Object "$work\install.txt"
} else { Write-Host "STOP: $new is not the extracted 0.6.0 folder" -ForegroundColor Red }
```

(Pasted lines keep running after an error, so the `if` makes sure the
installer only runs from the 0.6.0 folder.)

This takes 5 to 15 minutes: it builds the new images and recreates the API,
worker and dashboard containers. PostgreSQL and n8n keep their volumes, and
the database migration `0006_brain` adds the empty JARVIS Brain tables.

Near the end it must show these **seven** checks and `SMOKE TEST PASSED`,
followed by three short lines: where the sign-in details are, keeping `.env`
private, and reloading an open dashboard page.

```
PASS  API health  ok
PASS  n8n readiness  200
PASS  sign in  as <your admin e-mail> (admin)
PASS  versions match this folder  API 0.6.0, dashboard 0.6.0
PASS  worker running  planner: ...
PASS  event round trip through n8n  event ... acknowledged by n8n execution ...
PASS  supervisor request  request ... completed (...)
SMOKE TEST PASSED
```

- **If it stops with `this folder has no .env`:** step G-2 was missed and
  nothing was changed. Copy `.env` and run it again.
- **If the build fails with `toomanyrequests`:** that is Docker Hub's
  download limit, and the running system is unchanged. Wait an hour, or sign
  in with `docker login`, and run it again.
- **Any other FAIL:** stop and send me `$work\install.txt`.
  - If the failure came before `4/5`, the running system is unchanged.
  - If it came at `4/5 Starting` or later, the containers may already be
    half-replaced. To get 0.5.0 running again straight away, use the quick
    rollback in section H. It rebuilds 0.5.0 and needs internet access.

## After installation

### 1. Dashboard and API report 0.6.0

```powershell
Set-Location $new
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\status.ps1 2>&1 | Tee-Object "$work\status-after.txt"
```

It must show all of these:
- `This folder: version 0.6.0`;
- `Running API container: version 0.6.0`;
- `Running dashboard: version 0.6.0`;
- `Running API container was created from: C:\InfiniteAI\v0.6.0\InfiniteAI-CommandCenter`;
- `Dashboard/API: HTTP 200`;
- `n8n: HTTP 200`.

Then reload http://localhost:8090: the sidebar shows
`Dashboard v0.6.0 · API v0.6.0`.

### 2. All seven agents and the Supervisor

```powershell
docker compose exec -T api python -c "from app.agents.registry import all_agents; a = all_agents(); print(len(a), 'agents:', ', '.join(sorted(a)))" | Tee-Object "$work\agents-after.txt"
```

It must print
`8 agents: communications, credit_repair, monitoring, seo_content, server_management, social_marketing, supervisor, wordpress_security`
(the seven agents and the Supervisor). On the AI Agents page, the same agents
are switched on and off as before (step 3 checks this in the data too).

### 3 and 4. Your data, n8n workflows, keys and configuration are unchanged

```powershell
Save-State $new "$work\after.txt"
Compare-Object (Get-Content "$work\before.txt") (Get-Content "$work\after.txt")
Compare-Object (Get-Content "$work\before-other-containers.txt") (Get-Content "$work\after-other-containers.txt")
```

Both `Compare-Object` lines must print **nothing**. That means these are
identical before and after:
- users, the encrypted stored secrets, the settings;
- the agents' on/off states;
- sites, leads and schedules (and how many are enabled);
- the n8n workflows, the n8n credentials you created, and n8n's
  encryption key file.

  n8n re-imports the Command Center's own two credentials from your `.env`
  at every start, by design, so their stored form changes while their
  values stay the same. They are therefore compared by name and type only.
- both volumes, with the same creation time;
- your `.env`, byte for byte;
- every container of your other Docker projects.

**If a line differs:** send me both files and do not roll back yet.
- Leads, sites or schedules can change with normal use between the two
  readings (for example a lead arriving through n8n). That is not data loss.
- `users`, `secret_values`, `n8n_credentials_yours`, `n8n_encryption_config`,
  the volume lines and `env` must never differ.

### 5. The JARVIS Brain dashboard is there, and nothing was imported

```powershell
"SELECT string_agg(version, ' ' ORDER BY version) FROM schema_migrations; SELECT 'memory items ' || count(*) FROM memory_items; SELECT 'archived messages ' || count(*) FROM archive_messages; SELECT 'import runs ' || count(*) FROM import_runs; SELECT 'schedules enabled ' || count(*) FROM schedules WHERE enabled;" | docker compose exec -T postgres psql -U postgres -d command_center -At | Tee-Object "$work\brain-after.txt"
```

- **The database:**
  - the first line ends with `0006_brain`;
  - `memory items 0`, `archived messages 0`, `import runs 0`;
  - `schedules enabled` has the same number as `schedules_enabled` in step D.
- **The dashboard:** the sidebar has a **JARVIS Brain** entry that opens with
  "No JARVIS import yet".
- **Settings:** "JARVIS Brain in AI prompts" is off.

### 6. Smoke tests and health checks

The seven smoke checks ran at the end of G-3. Run them once more on their own:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 2>&1 | Tee-Object "$work\smoke-after.txt"
docker ps --filter "label=com.docker.compose.project=infinite-command-center" --format "table {{.Names}}\t{{.Status}}"
```

- **smoke.ps1:** all seven `PASS`, then `SMOKE TEST PASSED`.
- **Containers:** postgres, api, dashboard and n8n show `(healthy)`, and the
  worker shows `Up`.

### 7. JARVIS is still running

```powershell
Get-Process | Where-Object { $_.ProcessName -match 'hermes|jarvis' } | Select-Object ProcessName, Id, StartTime
Get-Content "$work\jarvis-before.txt"
```

The same process IDs and start times as in step D, so the upgrade did not
restart it. Then use JARVIS as you normally do (send it one message) to see it
answer. The upgrade never touches JARVIS's folders or processes.

### 8. Upgrade report and rollback checkpoint

```powershell
$report = "$work\upgrade-report.txt"
& {
  "Upgrade 0.5.0 -> 0.6.0, finished " + (Get-Date -Format "yyyy-MM-dd HH:mm")
  "old folder (rollback): $old"
  "new folder (live now): $new"
  "zip SHA-256: " + (Get-Content "$work\zip-sha256.txt")
  Get-Content "$work\backup.txt"
  "keys copy: C:\InfiniteAI\keys-backup-0.5.0\env-0.5.0.txt (private)"
  "data unchanged: " + $(if (-not (Compare-Object (Get-Content "$work\before.txt") (Get-Content "$work\after.txt"))) { "yes" } else { "NO - see before.txt / after.txt" })
  "other Docker projects unchanged: " + $(if (-not (Compare-Object (Get-Content "$work\before-other-containers.txt") (Get-Content "$work\after-other-containers.txt"))) { "yes" } else { "NO" })
  "smoke test: " + $(if (Select-String -Quiet "SMOKE TEST PASSED" "$work\smoke-after.txt") { "passed" } else { "NOT passed" })
  Get-Content "$work\agents-after.txt"
  Get-Content "$work\brain-after.txt"
  Select-String -Path "$work\status-after.txt" -Pattern "version|created from|HTTP" | ForEach-Object { $_.Line }
} | Set-Content -Encoding ASCII $report
Get-Content $report
```

Send me the content of `upgrade-report.txt`. It contains no passwords or keys.
It is also your **rollback checkpoint**:
- the old folder;
- the backup folder, with its trial-restore results;
- the keys copy;
- the data fingerprint you can compare against later.

Keep the old folder, its `backups` folder and `C:\InfiniteAI\keys-backup-0.5.0`
until 0.6.0 has run well for a while. From now on, run the day-to-day scripts
from `C:\InfiniteAI\v0.6.0\InfiniteAI-CommandCenter`.

## H. Rollback

**Quick, data kept (if 0.6.0 misbehaves):**

```powershell
$old = "C:\InfiniteAI\v0.5.0\InfiniteAI-CommandCenter"   # the folder of step A
if ((Test-Path "$old\VERSION") -and (Get-Content "$old\VERSION" -TotalCount 1).Trim() -eq "0.5.0") {
  Set-Location $old
  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
} else { Write-Host "STOP: $old is not your 0.5.0 folder" -ForegroundColor Red }
```

- **What it does:** it rebuilds the 0.5.0 containers from the old folder.
  - This needs internet access (Docker Hub, PyPI, npm). Docker's build cache
    usually still holds the 0.5.0 layers, so it is quick.
  - On `toomanyrequests`, wait or `docker login`, then run it again.
- **Your data:** it stays, including anything done in 0.6.0. The nine new
  JARVIS Brain tables stay in the database, unused and empty; 0.5.0 ignores
  them.
- **Going forward again:** installing 0.6.0 later finds them as they were.
- **After a rollback,** run the day-to-day scripts from the 0.5.0 folder
  again. The 0.6.0 folder's `start.ps1` would rebuild 0.6.0.

This was tested.

**Full restore (only for a disaster):** after the quick rollback, from the
0.5.0 folder:

```powershell
$old = "C:\InfiniteAI\v0.5.0\InfiniteAI-CommandCenter"   # the folder of step A
$backupFolder = "<the backup folder in upgrade-report.txt>"
if ((Test-Path "$old\VERSION") -and (Get-Content "$old\VERSION" -TotalCount 1).Trim() -eq "0.5.0" -and (Test-Path "$backupFolder\command_center.dump")) {
  Set-Location $old
  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\restore.ps1 -BackupFolder $backupFolder
} else { Write-Host "STOP: check the folder and the backup folder" -ForegroundColor Red }
```

- **The prompt:** it asks you to type `RESTORE`. Anything else cancels and
  changes nothing.
- **What it does:** it first backs up the current state. Then it puts the
  Command Center, n8n and Credit Repair databases back to the moment of step
  E, losing everything done since.
- **It needs the original `.env`:** the same `.env` the backup was taken
  with (`C:\InfiniteAI\keys-backup-0.5.0\env-0.5.0.txt` holds it).

This was tested with `-Yes`, which skips the typed confirmation.

**Never** run any of these, because they destroy your data:
- `docker compose down -v`;
- `uninstall.ps1 -DeleteData`;
- deleting the `infinite-command-center_pgdata` or
  `infinite-command-center_n8n_data` volumes.

## What this upgrade does not do (each later step needs its own approval)

- **The JARVIS Brain:**
  - it imports no JARVIS messages;
  - it activates no imported workflow or schedule;
  - memory in AI prompts stays off.
- **External systems:** it changes nothing on your websites, CWP servers, DNS
  or payment systems.
- **Web research:** it installs no DeepSeek or other web-research feature.
  The Research Gateway is planned for a later release.
- **Before the test-copy import:** the 0.6.1 patch for the withhold defect
  (section 0).
