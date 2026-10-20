# Upgrade your Command Center from 0.6.0 to 0.6.1 (Windows PowerShell)

Date: 2026-10-20 · for the installation in
`C:\InfiniteAI\v0.6.0\InfiniteAI-CommandCenter` (the folder the 0.5.0 → 0.6.0
guide installed) · every command below was run in the cloud sandbox
(PowerShell 7.4, against a running Command Center), except the download itself
and the Windows-only commands (`Get-PSDrive C`, `Expand-Archive`).

**Still on 0.5.0?** Follow `releases/UPGRADE_0.5.0_TO_0.6.0_WINDOWS.md` first
and send me its `upgrade-report.txt`; then this guide.

**What this upgrade does:** it replaces the program files and rebuilds the
API, worker and dashboard images. It then recreates the API, worker,
dashboard and n8n containers from a new folder, so n8n restarts once.
PostgreSQL keeps running. One database migration, `0007_brain_withhold`,
replaces one check on a JARVIS Brain table and adds two indexes.

**What it does not do:**
- It does not delete or reset any Docker volume, and it does not reset
  PostgreSQL or n8n.
- It does not change your `.env`. The new folder gets a copy.
- It does not touch JARVIS, import any JARVIS history, or activate any
  workflow.
- It does not contain DeepSeek or any other web-search feature.

You run every step yourself. Nothing here runs on its own.

## 0. Which release, and why

| Release | Where | SHA-256 of the zip |
|---|---|---|
| **0.6.1** | GitHub `mytechmobilebiz/Jarvis`, branch `jarvis-migration`, `command-center/releases/InfiniteAI-CommandCenter-0.6.1-windows.zip` | `fe750b5d4787cdb83cf08c72df87c594284ee3d617f9c72afa0c1c948139e722` |

0.6.1 fixes the 0.6.0 defect where an admin could not withhold an archived
JARVIS message that had a promoted memory copy. It also makes withholding and
purging remove the text of every copy. What changed, going back, and the
tests: `docs/RELEASE_NOTES_0.6.1.md` and `releases/GO_NO_GO_0.6.1.md`.

**Windows PowerShell 5.1.** As for 0.6.0: every script parses and is plain
ASCII, but your PC is the first Windows run. If anything fails, stop and send
the output.

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
`C:\InfiniteAI\upgrade-0.6.1` (no passwords or keys).

## Before installation

### A. Names and the current installation

```powershell
$old  = "C:\InfiniteAI\v0.6.0\InfiniteAI-CommandCenter"
$new  = "C:\InfiniteAI\v0.6.1\InfiniteAI-CommandCenter"
$work = "C:\InfiniteAI\upgrade-0.6.1"
New-Item -ItemType Directory -Force -Path $work | Out-Null
Test-Path "$old\.env"
Test-Path "C:\InfiniteAI\v0.6.1"
Set-Location $old
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\status.ps1 2>&1 | Tee-Object "$work\status-before.txt"
docker ps -a --format "table {{.Names}}\t{{.Status}}\t{{.Image}}" | Tee-Object "$work\containers-before.txt"
```

- **What you should see:**
  - `Test-Path` prints `True`, then `False`.
  - `status.ps1` lists the five `infinite-command-center` containers
    (postgres, api, worker, dashboard, n8n) as running or healthy.
  - `status.ps1` shows `Dashboard/API: HTTP 200` and `n8n: HTTP 200`, and
    version 0.6.0 for this folder, the API and the dashboard.
- **STOP if:**
  - the first `Test-Path` prints `False`: set `$old` to the folder that holds
    your `.env`;
  - a container is missing or restarting;
  - a probe is not HTTP 200.
- **If the second `Test-Path` prints `True`:** you extracted 0.6.1 before.
  - If you already ran `install.ps1` from it, stop and send me `status.ps1`.
  - Otherwise move it aside (nothing is deleted) and continue:
    `Rename-Item "C:\InfiniteAI\v0.6.1" "v0.6.1-old-extract"`.

### B. Prerequisites and disk space

```powershell
Set-Location $old
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\preflight.ps1 2>&1 | Tee-Object "$work\preflight.txt"
docker system df | Tee-Object "$work\docker-df.txt"
Get-PSDrive C | Select-Object @{n="FreeGB_C";e={[math]::Round($_.Free/1GB,1)}}
```

`preflight.ps1` only reads (it is the same in 0.6.0 and 0.6.1). It checks:
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

### C. The volumes that hold your data

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

### D-2. Your current agent configuration (read-only)

This records, in readable form, which agents your installation has, which are
switched on, how many schedules each agent has (and how many are enabled), and
whether the Credit Repair module is switched on. Step 2 after the upgrade
reads it again and compares.

```powershell
$agentSql = @'
SELECT line FROM (
  SELECT n.k, n.name || ' on=' || coalesce(s.enabled::text, 'true') || ' schedules=' || (SELECT count(*) FROM schedules c WHERE c.agent = n.name) || ' enabled_schedules=' || (SELECT count(*) FROM schedules c WHERE c.agent = n.name AND c.enabled) AS line
    FROM (VALUES (1,'supervisor'),(2,'wordpress_security'),(3,'seo_content'),(4,'social_marketing'),(5,'server_management'),(6,'communications'),(7,'monitoring'),(8,'credit_repair')) AS n(k, name)
    LEFT JOIN agent_state s ON s.name = n.name
  UNION ALL SELECT 9, 'other_agent_names ' || coalesce(string_agg(DISTINCT x.name, ',' ORDER BY x.name), 'none') FROM (SELECT name FROM agent_state UNION SELECT agent FROM schedules) x WHERE x.name NOT IN ('supervisor','wordpress_security','seo_content','social_marketing','server_management','communications','monitoring','credit_repair')
  UNION ALL SELECT 10, 'credit_repair_module ' || coalesce((SELECT CASE WHEN value->>'enabled' = 'true' THEN 'switched on' ELSE 'off' END FROM app_settings WHERE key = 'credit_repair_enabled'), 'off')
) t ORDER BY k;
'@
function Save-Agents($folder, $file) {
  Set-Location $folder
  & {
    docker compose exec -T api python -c "from app.agents.registry import all_agents; a = all_agents(); print(len(a), 'agents:', ', '.join(sorted(a)))"
    $agentSql | docker compose exec -T postgres psql -U postgres -d command_center -At
  } 2>&1 | Set-Content -Encoding ASCII $file
}
Save-Agents $old "$work\agents-before.txt"
Get-Content "$work\agents-before.txt"
```

- **What you should see:** 11 lines.
  - `8 agents: communications, credit_repair, monitoring, seo_content, server_management, social_marketing, supervisor, wordpress_security`
  - one line per agent, for example
    `seo_content on=false schedules=0 enabled_schedules=0`;
  - `other_agent_names none`;
  - `credit_repair_module off` (or `switched on` if you turned it on).
- **Check it against the dashboard:** open the **AI Agents** page. Each
  agent's on/off switch must match its `on=` value. An agent you never
  switched shows `on=true`.
- **STOP if:**
  - a line says `ERROR` or `Traceback`;
  - the first line is not `8 agents: ...`;
  - `other_agent_names` is not `none`: an agent name the Command Center does
    not know is in your data, so send me the file first;
  - the AI Agents page and the `on=` values disagree.

The AI keys and model settings the agents use live in your `.env`. The upgrade
keeps the `.env` byte for byte (the `env` line of step D, and step F).

**JARVIS (read-only):** note how it runs now.

```powershell
$j = Get-Process | Where-Object { $_.ProcessName -match 'hermes|jarvis' } | Select-Object ProcessName, Id, StartTime | Out-String
if (-not $j.Trim()) { $j = "(no process named hermes or jarvis)" }
$j | Tee-Object "$work\jarvis-before.txt"
```

If this prints nothing, JARVIS runs under another name; note it your usual way
(for example send it a test message now and again at the end).

### E. Full backup, and proof that it restores

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

### F. Keep `.env` and the encryption keys safe

```powershell
New-Item -ItemType Directory -Force -Path "C:\InfiniteAI\keys-backup-0.6.0" | Out-Null
Copy-Item "$old\.env" "C:\InfiniteAI\keys-backup-0.6.0\env-0.6.0.txt"
Copy-Item "$old\.env" (Join-Path $bk.FullName "env-at-backup.txt")
(Get-FileHash "$old\.env").Hash -eq (Get-FileHash "C:\InfiniteAI\keys-backup-0.6.0\env-0.6.0.txt").Hash
```

Prints `True`. `.env` holds all your passwords and the encryption keys
(`CC_SECRETS_KEY`, `N8N_ENCRYPTION_KEY`, `CREDIT_REPAIR_DATA_KEY`): without
it, the backup's stored secrets cannot be decrypted. Keep these copies private.
Also copy `C:\InfiniteAI\keys-backup-0.6.0` to a USB stick or your password
manager. Never send or paste it anywhere. Your original `.env` stays where it
is, untouched.

### G-1. Download and check the package

Download (it saves to your Downloads folder):
https://github.com/mytechmobilebiz/Jarvis/raw/jarvis-migration/command-center/releases/InfiniteAI-CommandCenter-0.6.1-windows.zip

```powershell
$zip = "$env:USERPROFILE\Downloads\InfiniteAI-CommandCenter-0.6.1-windows.zip"
$hash = (Get-FileHash $zip -Algorithm SHA256).Hash
$hash | Tee-Object "$work\zip-sha256.txt"
$hash -eq "FE750B5D4787CDB83CF08C72DF87C594284EE3D617F9C72AFA0C1C948139E722"
```

- **What you should see:** the hash, then `True`.
- **STOP if:** it prints `False`. Delete the file, download it again, and do
  not go on until it matches.

## Installation

### G-2. Extract into the new folder and copy `.env` (a copy; the original stays)

```powershell
Expand-Archive -Path $zip -DestinationPath "C:\InfiniteAI\v0.6.1"
Get-Content "$new\VERSION"
Test-Path "$new\.env"
Copy-Item "$old\.env" "$new\.env"
if (Test-Path "$old\templates\proposal-template.html") { Copy-Item "$old\templates\proposal-template.html" "$new\templates\" }
(Get-FileHash "$old\.env").Hash -eq (Get-FileHash "$new\.env").Hash
```

- **What you should see:** `0.6.1`, then `False` (no `.env` in the new folder
  yet), then `True`.

Explorer's right-click, Extract All, into `C:\InfiniteAI\v0.6.1` gives the
same folder.

### G-3. Install 0.6.1 from the new folder

```powershell
if ((Test-Path "$new\VERSION") -and (Get-Content "$new\VERSION" -TotalCount 1).Trim() -eq "0.6.1") {
  Set-Location $new
  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 2>&1 | Tee-Object "$work\install.txt"
} else { Write-Host "STOP: $new is not the extracted 0.6.1 folder" -ForegroundColor Red }
```

(Pasted lines keep running after an error, so the `if` makes sure the
installer only runs from the 0.6.1 folder.)

This takes 5 to 15 minutes. It builds the new images and recreates the API,
worker, dashboard and n8n containers; n8n is down for about a minute while it
restarts, and keeps its workflows, credentials and data volume. PostgreSQL
keeps running and keeps its volume. The database migration
`0007_brain_withhold` replaces one check on the JARVIS Brain version table
(it allows one more value); it adds no table and changes no data. `db-init`
sets short error messages for the Command Center's own database user, so a
refused row's values never reach the PostgreSQL log.

Near the end it must show these **seven** checks and `SMOKE TEST PASSED`,
followed by three short lines: where the sign-in details are, keeping `.env`
private, and reloading an open dashboard page.

```
PASS  API health  ok
PASS  n8n readiness  200
PASS  sign in  as <your admin e-mail> (admin)
PASS  versions match this folder  API 0.6.1, dashboard 0.6.1
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
    half-replaced. To get 0.6.0 running again straight away, use the quick
    rollback in section H. It rebuilds 0.6.0 and needs internet access.

## After installation

### 1. Dashboard and API report 0.6.1

```powershell
Set-Location $new
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\status.ps1 2>&1 | Tee-Object "$work\status-after.txt"
```

It must show all of these:
- `This folder: version 0.6.1`;
- `Running API container: version 0.6.1`;
- `Running dashboard: version 0.6.1`;
- `Running API container was created from: C:\InfiniteAI\v0.6.1\InfiniteAI-CommandCenter`;
- `Dashboard/API: HTTP 200`;
- `n8n: HTTP 200`.

Then reload http://localhost:8090: the sidebar shows
`Dashboard v0.6.1 · API v0.6.1`.

### 2. All seven agents and the Supervisor, configured as before

```powershell
Save-Agents $new "$work\agents-after.txt"
Get-Content "$work\agents-after.txt"
Compare-Object (Get-Content "$work\agents-before.txt") (Get-Content "$work\agents-after.txt")
```

- **What you should see:** the same 11 lines as in step D-2, starting with
  `8 agents: communications, credit_repair, monitoring, seo_content, server_management, social_marketing, supervisor, wordpress_security`
  (the seven agents and the Supervisor).
- **`Compare-Object` prints nothing:** every agent is switched on or off as
  before, with the same schedules, and the Credit Repair module is unchanged.
- **On the AI Agents page,** the switches are as you noted in step D-2.
- **If `Compare-Object` prints a line:** stop and send me both files.
  - An `enabled_schedules` count can change only if someone changed a
    schedule during the upgrade.
  - Anything else differing is unexpected.

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
  values stay the same. They are therefore compared by ID and type only.
- both volumes, with the same creation time;
- your `.env`, byte for byte;
- every container of your other Docker projects.

**If a line differs:** send me both files and do not roll back yet.
- Leads, sites or schedules can change with normal use between the two
  readings (for example a lead arriving through n8n). That is not data loss.
- `users`, `secret_values`, `n8n_credentials_yours`, `n8n_encryption_config`,
  the volume lines and `env` must never differ.

### 5. The 0.6.1 JARVIS Brain changes are in place, and nothing was imported

```powershell
"SELECT string_agg(version, ' ' ORDER BY version) FROM schema_migrations; SELECT 'memory items ' || count(*) FROM memory_items; SELECT 'archived messages ' || count(*) FROM archive_messages; SELECT 'import runs ' || count(*) FROM import_runs; SELECT 'schedules enabled ' || count(*) FROM schedules WHERE enabled; SELECT 'app role error logs ' || coalesce(array_to_string(rolconfig, ','), 'default') FROM pg_roles WHERE rolname = 'command_center';" | docker compose exec -T postgres psql -U postgres -d command_center -At | Tee-Object "$work\brain-after.txt"
docker compose exec -T api python -m app.cli brain-import status 2>&1 | Select-String "0007" | ForEach-Object { $_.Line } | Tee-Object -Append "$work\brain-after.txt"
```

- **The database:**
  - the first line ends with `0006_brain 0007_brain_withhold`;
  - `memory items 0`, `archived messages 0`, `import runs 0` (unless you
    already imported into this installation, which nothing so far asked you
    to do);
  - `schedules enabled` has the same number as `schedules_enabled` in step D;
  - `app role error logs log_error_verbosity=terse`.
- **The last line:** `migration 0007_brain_withhold present; withheld copies
  with text: 0`.
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
  "Upgrade 0.6.0 -> 0.6.1, finished " + (Get-Date -Format "yyyy-MM-dd HH:mm")
  "old folder (rollback): $old"
  "new folder (live now): $new"
  "zip SHA-256: " + (Get-Content "$work\zip-sha256.txt")
  Get-Content "$work\backup.txt"
  "keys copy: C:\InfiniteAI\keys-backup-0.6.0\env-0.6.0.txt (private)"
  "data unchanged: " + $(if (-not (Compare-Object (Get-Content "$work\before.txt") (Get-Content "$work\after.txt"))) { "yes" } else { "NO - see before.txt / after.txt" })
  "other Docker projects unchanged: " + $(if (-not (Compare-Object (Get-Content "$work\before-other-containers.txt") (Get-Content "$work\after-other-containers.txt"))) { "yes" } else { "NO" })
  "smoke test: " + $(if (Select-String -Quiet "SMOKE TEST PASSED" "$work\smoke-after.txt") { "passed" } else { "NOT passed" })
  "agent configuration unchanged: " + $(if (-not (Compare-Object (Get-Content "$work\agents-before.txt") (Get-Content "$work\agents-after.txt"))) { "yes" } else { "NO - see agents-before.txt / agents-after.txt" })
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

Keep the old folder, its `backups` folder and `C:\InfiniteAI\keys-backup-0.6.0`
until 0.6.1 has run well for a while. From now on, run the day-to-day scripts
from `C:\InfiniteAI\v0.6.1\InfiniteAI-CommandCenter`.

## H. Rollback

**Quick, data kept (if 0.6.1 misbehaves):**

```powershell
$old = "C:\InfiniteAI\v0.6.0\InfiniteAI-CommandCenter"   # the folder of step A
if ((Test-Path "$old\VERSION") -and (Get-Content "$old\VERSION" -TotalCount 1).Trim() -eq "0.6.0") {
  Set-Location $old
  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
} else { Write-Host "STOP: $old is not your 0.6.0 folder" -ForegroundColor Red }
```

- **What it does:** it rebuilds the 0.6.0 containers from the old folder.
  - This needs internet access (Docker Hub, PyPI, npm). Docker's build cache
    usually still holds the 0.6.0 layers, so it is quick.
  - On `toomanyrequests`, wait or `docker login`, then run it again.
- **Your data:** it stays, including anything done in 0.6.1. Migration
  `0007_brain_withhold` stays: 0.6.0 works with it, because it only allows
  one more value. The short error messages for the app's database user stay
  too, which is harmless.
- **Going forward again:** installing 0.6.1 later finds everything as it was.
- **After a rollback,** run the day-to-day scripts from the 0.6.0 folder
  again. The 0.6.1 folder's `start.ps1` would rebuild 0.6.1.

This was tested.

**Full restore (only for a disaster):** after the quick rollback, from the
0.6.0 folder:

```powershell
$old = "C:\InfiniteAI\v0.6.0\InfiniteAI-CommandCenter"   # the folder of step A
$backupFolder = "<the backup folder in upgrade-report.txt>"
if ((Test-Path "$old\VERSION") -and (Get-Content "$old\VERSION" -TotalCount 1).Trim() -eq "0.6.0" -and (Test-Path "$backupFolder\command_center.dump")) {
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
  with (`C:\InfiniteAI\keys-backup-0.6.0\env-0.6.0.txt` holds it).

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
  The Research Gateway is planned for 0.7.0, after its own approval.
- **The test-copy import of your real JARVIS backup** still needs its own
  approval. 0.6.1 is the release it was waiting for.
