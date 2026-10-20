# Upgrade your Command Center from 0.5.0 to 0.6.0 (Windows PowerShell)

Date: 2026-10-20 · for the installation in
`C:\InfiniteAI\v0.5.0\InfiniteAI-CommandCenter` · every command below was
checked against the scripts in the 0.6.0 package.

**What this upgrade does:** it replaces the program files and rebuilds the
API, worker and dashboard containers from a new folder. One database
migration adds nine empty JARVIS Brain tables.

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

## 1. Preflight (read-only: changes nothing)

Open **Windows PowerShell** as your normal user (not as administrator). Keep
this one window open for every step: the names you set here are needed later.

```powershell
$old  = "C:\InfiniteAI\v0.5.0\InfiniteAI-CommandCenter"
$new  = "C:\InfiniteAI\v0.6.0\InfiniteAI-CommandCenter"
$work = "C:\InfiniteAI\upgrade-0.6.0"
New-Item -ItemType Directory -Force -Path $work | Out-Null
Test-Path "$old\.env"          # must print True
Test-Path "C:\InfiniteAI\v0.6.0" # must print False (the new folder must not exist yet)
docker version --format "{{.Server.Version}}"   # Docker Desktop must answer
docker compose version
Get-PSDrive C | Select-Object @{n="FreeGB";e={[math]::Round($_.Free/1GB,1)}}   # at least 10 GB free
Set-Location $old
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\status.ps1
```

Expected:
- `True`, then `False`.
- A Docker version and a Compose version.
- At least 10 GB free.
- `status.ps1` shows the `infinite-command-center` containers running, and
  `Dashboard/API: HTTP 200` and `n8n: HTTP 200`. Your 0.5.0 may report the
  API as 0.2.0: that is the known 0.5.0 problem that 0.6.0 fixes.

If `Test-Path "$old\.env"` prints `False`, stop: set `$old` to the folder
that holds your `.env`.

If `Test-Path "C:\InfiniteAI\v0.6.0"` prints `True`, you extracted 0.6.0
before. If you have already run `install.ps1` from it, stop here and send me
the output of `status.ps1`. Otherwise rename it out of the way (nothing is
deleted) and continue:
`Rename-Item "C:\InfiniteAI\v0.6.0" "v0.6.0-old-extract"`.

**Record the state before the upgrade.** This prints no passwords or keys,
only counts, names and fingerprints (hashes).

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
$n8nSql = "SELECT 'n8n_workflows', count(*)::text FROM workflow_entity UNION ALL SELECT 'n8n_credentials', coalesce(md5(string_agg(id||data,'|' ORDER BY id)),'none') FROM credentials_entity;"
function Save-State($folder, $file) {
  Set-Location $folder
  & {
    $sql | docker compose exec -T postgres psql -U postgres -d command_center -At
    $n8nSql | docker compose exec -T postgres psql -U postgres -d n8n -At
    "n8n_encryption_config " + (docker compose exec -T n8n sh -c "sha256sum < /home/node/.n8n/config | cut -c1-16")
    docker volume inspect infinite-command-center_pgdata infinite-command-center_n8n_data --format "{{.Name}} {{.CreatedAt}}"
    "env " + (Get-FileHash "$folder\.env" -Algorithm SHA256).Hash
  } | Set-Content -Encoding ASCII $file
  docker ps -a --format "{{.Names}} {{.ID}} {{.CreatedAt}}" | Where-Object { $_ -notmatch '^infinite-command-center-' } | Sort-Object | Set-Content -Encoding ASCII ($file -replace '\.txt$', '-other-containers.txt')
}
Save-State $old "$work\before.txt"
Get-Content "$work\before.txt"
```

Expected: 14 lines:
- the 8 database lines (`users`, `secret_values`, `app_settings`,
  `agent_state` with each agent `=true`/`=false`, `sites`, `leads`,
  `schedules`, `schedules_enabled`);
- `n8n_workflows`, `n8n_credentials` and `n8n_encryption_config`;
- the two volume lines;
- the `env` line.

If a line shows `ERROR` or is missing, stop and send the output.

**JARVIS (read-only):** note how JARVIS runs now, so you can see afterwards
that the upgrade did not restart it:

```powershell
Get-Process | Where-Object { $_.ProcessName -match 'hermes|jarvis' } | Select-Object ProcessName, Id, StartTime | Tee-Object "$work\jarvis-before.txt"
```

(If this prints nothing, JARVIS runs under another name; compare it your
usual way.)

## 2. Back up, and check the backup

```powershell
Set-Location $old
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup.ps1
$bk = Get-ChildItem "$old\backups" -Directory | Sort-Object Name | Select-Object -Last 1
$bk.FullName
Copy-Item "$old\.env" "C:\InfiniteAI\env-backup-0.5.0.txt"
```

`backup.ps1` must end with `Backup written to ...`. It lists
`command_center.dump`, `n8n.dump` and `credit_repair.dump`, each larger than
0 bytes. Keep `env-backup-0.5.0.txt` private: it holds your passwords and
encryption keys, and without them the backup cannot be decrypted.

Check that each dump can be read (lists its contents, changes nothing):

```powershell
foreach ($db in "command_center", "n8n", "credit_repair") {
  $f = Join-Path $bk.FullName "$db.dump"
  if (-not (Test-Path $f)) { Write-Host "MISSING $db.dump" -ForegroundColor Red; continue }
  docker compose cp $f "postgres:/tmp/verify-$db.dump" | Out-Null
  $entries = (docker compose exec -T postgres pg_restore -l "/tmp/verify-$db.dump" | Measure-Object -Line).Lines
  docker compose exec -T postgres rm -f "/tmp/verify-$db.dump"
  Write-Host ("{0}: {1} entries, {2:N0} bytes, SHA-256 {3}" -f $db, $entries, (Get-Item $f).Length, (Get-FileHash $f).Hash.Substring(0,16))
}
```

Each database must show more than 0 entries. If one shows `MISSING` or 0
entries, stop. (`credit_repair` is missing only if that database never
existed.)

## 3. Download and check the package

Download (it saves to your Downloads folder):
https://github.com/mytechmobilebiz/Jarvis/raw/jarvis-migration/command-center/releases/InfiniteAI-CommandCenter-0.6.0-windows.zip

```powershell
$zip = "$env:USERPROFILE\Downloads\InfiniteAI-CommandCenter-0.6.0-windows.zip"
(Get-FileHash $zip -Algorithm SHA256).Hash
```

It must print exactly
`A687F737DE043AE6A69C4DCC3F15AE71D71A5C3688BEBEF62B53122E4036A8A1`.
If it does not, delete the file, download it again, and do not go on until
it matches.

## 4. Extract into the new folder

```powershell
Expand-Archive -Path $zip -DestinationPath "C:\InfiniteAI\v0.6.0"
Get-Content "$new\VERSION"     # must print 0.6.0
Test-Path "$new\.env"          # must print False
```

(Explorer's right-click, Extract All, into `C:\InfiniteAI\v0.6.0` does the
same.)

## 5. Copy your `.env` into the new folder (the original stays where it is)

```powershell
Copy-Item "$old\.env" "$new\.env"
if (Test-Path "$old\templates\proposal-template.html") { Copy-Item "$old\templates\proposal-template.html" "$new\templates\" }
(Get-FileHash "$old\.env").Hash -eq (Get-FileHash "$new\.env").Hash   # must print True
```

## 6. Install 0.6.0 from the new folder

```powershell
Set-Location $new
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

This takes 5 to 15 minutes; it builds the new images and recreates the
API, worker and dashboard containers. Your data stays in the same volumes.
It must end with these **seven** lines and `SMOKE TEST PASSED`:

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

If it stops with `this folder has no .env`, step 5 was missed: nothing was
changed; copy `.env` and run it again. If the build fails with
`toomanyrequests` (Docker Hub's download limit), wait an hour, or sign in
with `docker login`, and run `install.ps1` again. Any other failure: stop
and send the whole output.

## 7. Health checks after the upgrade

1-7. The seven smoke checks above (API, n8n, sign-in, versions, worker,
   n8n round trip, Supervisor).

8. **Versions and folder:**

   ```powershell
   Set-Location $new
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\status.ps1
   ```

   It must show:
   - `This folder: version 0.6.0`;
   - `Running API container: version 0.6.0` and
     `Running dashboard: version 0.6.0`;
   - `Running API container was created from: C:\InfiniteAI\v0.6.0\InfiniteAI-CommandCenter`;
   - `Dashboard/API: HTTP 200` and `n8n: HTTP 200`.

9. **Your data is unchanged** (the same fingerprints as before):

   ```powershell
   Save-State $new "$work\after.txt"
   Compare-Object (Get-Content "$work\before.txt") (Get-Content "$work\after.txt")
   ```

   It must print **nothing**: users, encrypted secrets, settings, agents
   (on/off), sites, leads, schedules, n8n workflows, credentials and
   encryption key, both volumes (same creation time) and `.env` are
   identical.

10. **Other Docker projects untouched:**

    ```powershell
    Compare-Object (Get-Content "$work\before-other-containers.txt") (Get-Content "$work\after-other-containers.txt")
    ```

    It must print nothing (the same containers, the same IDs and creation
    times).

11. **The new database migration, and nothing imported:**

    ```powershell
    "SELECT string_agg(version, ' ' ORDER BY version) FROM schema_migrations; SELECT 'memory items ' || count(*) FROM memory_items; SELECT 'archived messages ' || count(*) FROM archive_messages; SELECT 'import runs ' || count(*) FROM import_runs;" | docker compose exec -T postgres psql -U postgres -d command_center -At
    ```

    It must show `0006_brain` at the end of the first line, and `memory items 0`,
    `archived messages 0`, `import runs 0`.

12. **Dashboard:** reload http://localhost:8090. The sidebar shows
    `Dashboard v0.6.0 · API v0.6.0` and a **JARVIS Brain** entry with
    "No JARVIS import yet". Check the AI Agents page: the same agents
    switched on and off as before. Check Settings: "JARVIS Brain in AI
    prompts" is off.

13. **JARVIS untouched:**

    ```powershell
    Get-Process | Where-Object { $_.ProcessName -match 'hermes|jarvis' } | Select-Object ProcessName, Id, StartTime
    Get-Content "$work\jarvis-before.txt"
    ```

    The same process IDs and start times.

From now on, run the day-to-day scripts from
`C:\InfiniteAI\v0.6.0\InfiniteAI-CommandCenter`. Keep the old folder and
its `backups` folder until 0.6.0 has run well for a while.

## 8. Rollback

**Quick, data kept (if 0.6.0 misbehaves):**

```powershell
Set-Location "C:\InfiniteAI\v0.5.0\InfiniteAI-CommandCenter"
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

- **What it does:** it rebuilds the 0.5.0 containers from the old folder.
- **Your data:** it stays, including anything done in 0.6.0. The nine new
  JARVIS Brain tables stay in the database, unused and empty; 0.5.0 ignores
  them.
- **Going forward again:** installing 0.6.0 later finds them as they were.

This was tested.

**Full restore (only for a disaster):** after the quick rollback, from the
0.5.0 folder:

```powershell
Set-Location "C:\InfiniteAI\v0.5.0\InfiniteAI-CommandCenter"
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\restore.ps1 -BackupFolder "<the backup folder printed in step 2>"
```

- **The prompt:** it asks you to type `RESTORE`. Anything else cancels and
  changes nothing.
- **What it does:** it first backs up the current state. Then it puts the
  Command Center, n8n and Credit Repair databases back to the moment of step
  2, losing everything done since.
- **It needs the original `.env`:** the same `.env` the backup was taken
  with (your `env-backup-0.5.0.txt` holds it).

This was tested with `-Yes`, which skips the typed confirmation.

**Never** run `docker compose down -v`, `uninstall.ps1 -DeleteData`, or
delete the `infinite-command-center_pgdata` or `infinite-command-center_n8n_data`
volumes: they hold your data.

## 9. What comes later (each needs its own approval)

- The test copy and the import of your real JARVIS backup into it
  (`docs/DEPLOY_WINDOWS.md`, "JARVIS Brain: test copy and import").
- The live JARVIS import, after your review.
- The Research Gateway (SearXNG/DeepSeek web research): planned for a later
  release, not in 0.6.0.

**What to send me after the upgrade:** the last part of the `install.ps1`
output (from the seven checks), the `status.ps1` output, and whether steps
9 to 13 printed what they should. Do not paste anything from `.env`.
