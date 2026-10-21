# Restores the databases from a folder written by backup.ps1. This REPLACES the
# current data, so it first takes a backup of the current state and asks you to
# type RESTORE (or pass -Yes). The application is stopped during the restore
# and started again afterwards. Use the same .env the backup was taken with.
#
# What the restore must not undo is kept (0.6.2). After the application is
# stopped (so no decision can be made in between) it is saved in backups\
# (keys, ids, e-mail addresses and the audit log; never message text, never a
# password hash):
#   - removals-before-restore-<time>.json: the JARVIS Brain's withholds, purges
#     (also of items without a source key), releases, title choices and purge
#     fingerprints;
#   - access-before-restore-<time>.json: who may sign in and what they may read,
#     and the audit log.
# After the restore, BEFORE any service starts (a helper container of the api
# image that serves nothing), the database is migrated, the audit entries made
# after the backup are put back, deactivations, revoked permissions and lowered
# roles made after the backup are applied again, every sign-in is ended, and the
# removals are applied again. Only then do the services start. User changes the
# restore does revert (a user created, a permission granted after the backup...)
# are listed at the end.
#
# An interrupted restore is finished, never lost: backups\restore-pending.json
# (and a mark in the database) say that the files above are not applied again
# yet. While it is there, start.ps1 starts nothing and the api and the worker
# refuse to start; the next restore.ps1 applies those files first (it never
# exports again from the restored data), and -FinishPending only applies them
# and starts the services.
#
# The restore stops before changing anything when these cannot be saved, and
# ends with exit 1 (services not started) when they cannot be applied again.
# Overrides for a file that cannot be saved, each saying what it gives up:
#   -SkipBrainRemovals   restore without the Brain's removals: withholds and
#                        purges made after the backup are undone and their text
#                        is readable again.
#   -KeepRestoredAccess  restore without the users' access: users and
#                        permissions are exactly as in the backup.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\restore.ps1 -BackupFolder .\backups\20261018-120000
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\restore.ps1 -FinishPending
param(
  [string]$BackupFolder = "",
  [switch]$Yes,
  [switch]$SkipBrainRemovals,
  [switch]$KeepRestoredAccess,
  [switch]$FinishPending
)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$helper = "cc-restore-helper-$stamp"
$script:helperUp = $false
$backups = Join-Path $root "backups"
# Written before the stop, deleted once what the restore must not undo is applied again (see above).
$pendingFile = Join-Path $backups "restore-pending.json"

# A one-off container of the api image, attached to the database: it runs the commands below and serves nothing.
function Start-Helper {
  if ($script:helperUp) { return $true }
  docker compose run -d --no-deps --name $helper api python -c "import time; time.sleep(7200)" | Out-Null
  if ($LASTEXITCODE -ne 0) { return $false }
  $script:helperUp = $true
  return $true
}
function Stop-Helper {
  if ($script:helperUp) { docker rm -f $helper 2>$null | Out-Null; $script:helperUp = $false }
}
function Invoke-Helper {  # python -m app.cli <args> in the helper: its output; $LASTEXITCODE is its exit code
  docker exec $helper python -m app.cli @args 2>&1
}
function Invoke-Psql($sql) {  # one query on command_center: its output; $LASTEXITCODE is psql's exit code
  docker compose exec -T postgres psql -U postgres -d command_center -tAc $sql 2>$null
}
function Show-Lines($lines) { foreach ($line in $lines) { Write-Host "  $line" } }
function Fail($text) { Stop-Helper; Write-Host "RESTORE STOPPED: $text" -ForegroundColor Red; exit 1 }

# --- Applying the saved files again (after pg_restore, or to finish an earlier restore) ---------------------------
# A helper container (no api, worker, dashboard or n8n runs): restore-carry apply migrates the restored database, puts
# back the audit entries, applies the access narrowing again and ends every sign-in; decisions-apply --removals-only
# applies the Brain's removals again (as "system (restore)", keeping who decided each and when). Each one also clears
# its part of the database's mark (the api and the worker start only once none is left).
$script:problems = @()
$script:kept = @()
$script:reverted = @()
$script:again = -1
function Invoke-Reapply([string]$accessFile, [string]$removalsFile) {
  if (-not (Start-Helper)) {
    $script:problems += "the helper container could not be started"
    return
  }
  if ($accessFile) {
    docker cp $accessFile "${helper}:/tmp/access.json"
    $code = $LASTEXITCODE
    $out = @()
    if ($code -eq 0) {
      $out = Invoke-Helper restore-carry apply --in /tmp/access.json --yes
      $code = $LASTEXITCODE
    }
    Show-Lines $out
    foreach ($line in $out) {
      if ("$line" -match '^\s*kept: (.+)$') { $script:kept += $Matches[1] }
      if ("$line" -match '^\s*(reverted|not applied again): (.+)$') { $script:reverted += $Matches[2] }
    }
    if ($code -ne 0) { $script:problems += "the users' access and the audit log (exit $code)" }
  } else {
    $out = Invoke-Helper migrate
    Show-Lines $out
    if ($LASTEXITCODE -ne 0) { $script:problems += "the database migration (exit $LASTEXITCODE)" }
  }
  if ($removalsFile) {
    docker cp $removalsFile "${helper}:/tmp/removals.json"
    $code = $LASTEXITCODE
    $text = ""
    $out = @()
    if ($code -eq 0) {
      # Nothing else runs, so the import lock is free; tried again (10 s) if something still holds it.
      for ($try = 1; $try -le 4; $try++) {
        $out = Invoke-Helper brain-import decisions-apply --removals-only --in /tmp/removals.json --yes
        $code = $LASTEXITCODE
        $text = ($out | ForEach-Object { "$_" }) -join "`n"
        if ($code -eq 0) { break }
        if ($code -ne 2 -or $text -notmatch 'is running' -or $try -eq 4) { break }
        Write-Host "  another change to the JARVIS Brain holds the lock; trying again in 10 seconds..."
        Start-Sleep -Seconds 10
      }
    }
    Show-Lines $out
    if ($code -ne 0) {
      $script:problems += "the JARVIS Brain's withholds, purges and releases (exit $code)"
    } elseif ($text -match '(?m)^\s*(removal|purge|release) not applied') {
      $script:problems += "some of the JARVIS Brain's withholds, purges or releases (the 'not applied' lines above)"
    } else {
      # decisions-apply prints "removals applied: <outcome> <n>, ..." (tests/unit/test_brain_ops.py runs this
      # pattern against the CLI's own output). What was already in the restored backup is "already done".
      $script:again = 0
      if ($text -match '(?m)^removals applied: (.+)$') {
        foreach ($part in ($Matches[1] -split ', ')) {
          if ($part -match '^(.+) (\d+)$' -and $Matches[1] -ne "already done") { $script:again += [int]$Matches[2] }
        }
      }
    }
  }
  Stop-Helper
}
function Show-Reapply([string]$accessFile, [string]$removalsFile) {
  Write-Host "Finish it with:  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\restore.ps1 -FinishPending" -ForegroundColor Red
  Write-Host "or apply them by hand, then start the services (the files hold keys, ids and e-mail addresses only):" -ForegroundColor Red
  Write-Host "  docker compose run -d --no-deps --name cc-restore-helper api python -c `"import time; time.sleep(3600)`"" -ForegroundColor Red
  if ($accessFile) {
    Write-Host "  docker cp `"$accessFile`" cc-restore-helper:/tmp/access.json" -ForegroundColor Red
    Write-Host "  docker exec cc-restore-helper python -m app.cli restore-carry apply --in /tmp/access.json --yes" -ForegroundColor Red
  } else {
    Write-Host "  docker exec cc-restore-helper python -m app.cli migrate" -ForegroundColor Red
  }
  if ($removalsFile) {
    Write-Host "  docker cp `"$removalsFile`" cc-restore-helper:/tmp/removals.json" -ForegroundColor Red
    Write-Host "  docker exec cc-restore-helper python -m app.cli brain-import decisions-apply --removals-only --in /tmp/removals.json --yes" -ForegroundColor Red
  }
  Write-Host "  docker rm -f cc-restore-helper" -ForegroundColor Red
  Write-Host "  Remove-Item -LiteralPath `"$pendingFile`"" -ForegroundColor Red
  Write-Host "  .\scripts\start.ps1 -NoBuild" -ForegroundColor Red
}
function Show-AccessLists {
  if ($script:kept.Count -gt 0) {
    Write-Host "`nAccess changes made after the backup, applied again:" -ForegroundColor Green
    foreach ($line in $script:kept) { Write-Host "  $line" -ForegroundColor Green }
  }
  if ($script:reverted.Count -gt 0) {
    Write-Host "`nWARNING: the restore reverts these user changes made after the backup (redo them on the Users page if still wanted):" -ForegroundColor Yellow
    foreach ($line in $script:reverted) { Write-Host "  $line" -ForegroundColor Yellow }
  }
}

if (-not $BackupFolder -and -not $FinishPending) { Fail "name the backup folder: -BackupFolder .\backups\<time> (or -FinishPending)" }

# --- 0. An earlier restore that did not finish ----------------------------------------------------------------------
# Its files are applied first, never exported again: the database holds the restored data without what the restore
# must not undo, and an export now would save that loss.
if (Test-Path -LiteralPath $pendingFile -PathType Leaf) {
  $pending = $null
  try { $pending = Get-Content -Raw -LiteralPath $pendingFile | ConvertFrom-Json } catch { $pending = $null }
  if ($null -eq $pending) { Fail "$pendingFile cannot be read. It names the files an earlier restore saved (removals-before-restore-*.json and access-before-restore-*.json in backups\): apply them by hand (docs\DEPLOY_WINDOWS.md, Backup and restore), then delete it" }
  $pendingAccess = "$($pending.access)"
  $pendingRemovals = "$($pending.removals)"
  foreach ($file in @($pendingAccess, $pendingRemovals)) {
    if ($file -and -not (Test-Path -LiteralPath $file -PathType Leaf)) { Fail "an earlier restore did not finish, and a file it saved is missing: $file. Nothing was changed. Put the file back and run this again; only to give up what it holds (the decisions made after that backup stay undone), see docs\DEPLOY_WINDOWS.md, Backup and restore" }
  }
  Write-Host "An earlier restore (of $($pending.backup), started $($pending.at)) did not finish applying what it must not undo." -ForegroundColor Yellow
  Write-Host "Applying it first, from the files it saved then (nothing is exported again from the restored data)..." -ForegroundColor Yellow
  Invoke-Reapply $pendingAccess $pendingRemovals
  if ($script:problems.Count -gt 0) {
    Write-Host "`nNOT applied again: $($script:problems -join '; ')." -ForegroundColor Red
    Show-Reapply $pendingAccess $pendingRemovals
    Fail "an earlier restore's files were NOT applied, so nothing was started or restored; run the commands above"
  }
  Remove-Item -LiteralPath $pendingFile -Force
  Write-Host "The earlier restore is finished: what it must not undo is applied again." -ForegroundColor Green
  if ($FinishPending -and -not $BackupFolder) {
    Write-Host "`nStarting the application again..."
    $global:LASTEXITCODE = 1  # stays 1 if the script cannot run at all
    & (Join-Path $PSScriptRoot "start.ps1") -NoBuild
    $started = $LASTEXITCODE -eq 0
    Show-AccessLists
    if ($pendingAccess) { Write-Host "Every sign-in was ended: everyone signs in again." -ForegroundColor Yellow }
    if (-not $started) { Fail "what the earlier restore must not undo is applied again, but the services did not start; see: docker compose logs --tail=100 api worker n8n" }
    Write-Host "Restore finished. Run .\scripts\smoke.ps1 to check the system." -ForegroundColor Green
    exit 0
  }
  $script:kept = @()
  $script:reverted = @()
  $script:again = -1
} else {
  # The database's own mark, for a restore whose marker file is not in this folder (it ran from another folder).
  $mark = Invoke-Psql "SELECT CASE WHEN to_regclass('public.brain_maintenance') IS NULL THEN '' ELSE (xpath('/table/row/d/text()', query_to_xml('SELECT detail::text AS d FROM brain_maintenance WHERE name = ''restore_reapply_pending''', false, false, '')))[1]::text END"
  if ($LASTEXITCODE -ne 0) { Fail "could not read the database (docker compose exec postgres psql failed); nothing was changed. Is the stack running? Try again." }
  if ("$mark".Trim()) {
    Fail "this database's last restore did not finish applying what it must not undo (the database says so: $("$mark".Trim())), and $pendingFile is not in this folder. Run restore.ps1 -FinishPending in the folder that ran that restore, or apply the files it names by hand (docs\DEPLOY_WINDOWS.md, Backup and restore). Nothing was changed."
  }
  if ($FinishPending) {
    Write-Host "No restore is waiting to be finished." -ForegroundColor Green
    exit 0
  }
}

if (-not (Test-Path $BackupFolder -PathType Container)) { Fail "no such folder: $BackupFolder" }
$folder = (Resolve-Path $BackupFolder).Path
$dumps = @()
foreach ($db in @("command_center", "n8n", "credit_repair")) {
  if (Test-Path (Join-Path $folder "$db.dump")) { $dumps += $db }
}
if ($dumps.Count -eq 0) { Fail "no database dumps (*.dump) in $folder" }
Write-Host "Will restore: $($dumps -join ', ') from $folder"
Write-Host "The current data in those databases will be replaced." -ForegroundColor Yellow
if (-not $Yes) {
  $answer = Read-Host "Type RESTORE to continue"
  if ($answer -ne "RESTORE") { Fail "cancelled; nothing was changed" }
}

Write-Host "`nBacking up the current state first..."
$global:LASTEXITCODE = 1  # stays 1 if the script cannot run at all
& (Join-Path $PSScriptRoot "backup.ps1")
if ($LASTEXITCODE -ne 0) { Fail "the safety backup failed; nothing was changed" }

# --- 1. Stop, then save what the restore must not undo --------------------------------------------------------------
$removals = ""
$access = ""
$hasBrain = $false
$lost = @()
if ($dumps -contains "command_center") {
  # Fail closed: a probe that cannot be read never counts as "no JARVIS Brain" (the removals would go unsaved).
  $brain = Invoke-Psql "SELECT count(*) FROM schema_migrations WHERE version = '0006_brain'"
  if ($LASTEXITCODE -ne 0 -or @("0", "1") -notcontains "$brain".Trim()) { Fail "could not read the database's migrations (docker compose exec postgres psql failed); nothing was changed. Try again." }
  $hasBrain = ("$brain".Trim() -eq "1")
  if ($hasBrain) {
    # An import, rollback or rescan (brain-import, its own container) would keep writing while the data is replaced.
    $busy = Invoke-Psql "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted AND objsubid = 2 AND classid = 3019034917 AND objid <> 4294967295 AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    if ($LASTEXITCODE -ne 0 -or "$busy".Trim() -notmatch '^\d+$') { Fail "could not check whether a JARVIS Brain import is running (docker compose exec postgres psql failed); nothing was changed. Try again." }
    if ([int]"$busy".Trim() -gt 0) { Fail "an import, rollback, rescan or another change to the JARVIS Brain is running; nothing was changed. Try again when it has finished." }
  }
}

# Stopped BEFORE anything is saved: a withhold, purge, release or keep made after the save would be in neither file
# nor in the safety backup, and the restore would undo it. The database keeps running; the helper needs only it.
Write-Host "`nStopping the application (the database keeps running)..."
docker compose stop api worker dashboard n8n
if ($LASTEXITCODE -ne 0) { Fail "could not stop the application" }

if ($dumps -contains "command_center") {
  Write-Host "`nSaving what the restore must not undo (keys, ids, e-mail addresses and the audit log; never message text)..."
  if (-not (Start-Helper)) {
    Write-Host "  a helper container of the api image could not be started (docker compose run api)" -ForegroundColor Yellow
  } else {
    # The removals first: their export writes an audit entry, which the audit log saved next then holds.
    if ($hasBrain) {
      $inside = "/tmp/removals-before-restore-$stamp.json"
      $out = Invoke-Helper brain-import decisions-export --removals-only --out $inside
      $code = $LASTEXITCODE
      Show-Lines $out
      $target = Join-Path $backups "removals-before-restore-$stamp.json"
      if ($code -eq 0) {
        docker cp "${helper}:$inside" $target
        if ($LASTEXITCODE -eq 0 -and (Test-Path $target -PathType Leaf)) { $removals = $target }
      }
    }
    $inside = "/tmp/access-before-restore-$stamp.json"
    $out = Invoke-Helper restore-carry export --out $inside
    $code = $LASTEXITCODE
    Show-Lines $out
    $target = Join-Path $backups "access-before-restore-$stamp.json"
    if ($code -eq 0) {
      docker cp "${helper}:$inside" $target
      if ($LASTEXITCODE -eq 0 -and (Test-Path $target -PathType Leaf)) { $access = $target }
    }
  }
  Stop-Helper
  if ($access) { Write-Host "Saved: $access" }
  if ($removals) { Write-Host "Saved: $removals" }
  $missing = @()
  if (-not $access) {
    $what = "users and permissions will be exactly as in the backup: a deactivation, a revoked sensitive-data permission or a lowered role made after it is undone (and the old sign-ins work again), and the audit entries made after it are gone from the database (the safety backup above still holds them)"
    if ($KeepRestoredAccess) { $lost += "ACCESS NOT KEPT (-KeepRestoredAccess): $what." }
    else { $missing += "the users' access and the audit log (-KeepRestoredAccess accepts that $what)" }
  }
  if ($hasBrain -and -not $removals) {
    $counts = Invoke-Psql "SELECT count(*) FILTER (WHERE reason = 'withheld by an admin') || ' withheld messages and copies, ' || count(*) FILTER (WHERE reason = 'purged by an admin') || ' purged items with a source key' FROM brain_tombstones"
    if ($LASTEXITCODE -ne 0 -or -not "$counts".Trim()) { $counts = "an unknown number of them (they could not be counted)" }
    $what = "every withhold, purge and release made in the JARVIS Brain after the backup is undone, and that text is readable again until it is withheld or purged again by hand (the current database holds $("$counts".Trim()); the safety backup above still holds them)"
    if ($SkipBrainRemovals) { $lost += "BRAIN REMOVALS NOT KEPT (-SkipBrainRemovals): $what." }
    else { $missing += "the JARVIS Brain's withholds, purges and releases (-SkipBrainRemovals accepts that $what)" }
  }
  if ($missing.Count -gt 0) {
    Write-Host "`nStarting the application again (nothing was restored)..."
    $global:LASTEXITCODE = 1
    & (Join-Path $PSScriptRoot "start.ps1") -NoBuild
    Fail "could not save $($missing -join '; nor '); nothing was changed. Try again, or pass the override named above."
  }
}

# --- 2. The restore -------------------------------------------------------------------------------------------------
# From here until they are applied again, backups\restore-pending.json names the saved files (see the top).
$carry = ($dumps -contains "command_center") -and ($access -or $removals)
if ($carry) {
  New-Item -ItemType Directory -Force -Path $backups | Out-Null
  $marker = @{ access = $access; removals = $removals; backup = $folder; at = (Get-Date).ToString("o") } | ConvertTo-Json
  Set-Content -LiteralPath $pendingFile -Value $marker -Encoding UTF8
  if (-not (Test-Path -LiteralPath $pendingFile -PathType Leaf)) {
    Write-Host "`nStarting the application again (nothing was restored)..."
    & (Join-Path $PSScriptRoot "start.ps1") -NoBuild
    Fail "could not write $pendingFile; nothing was changed"
  }
}

$failed = @()
foreach ($db in $dumps) {
  Write-Host "Restoring $db..."
  docker compose cp (Join-Path $folder "$db.dump") "postgres:/tmp/restore-$db.dump"
  if ($LASTEXITCODE -ne 0) { $failed += $db; continue }
  # One transaction per database: a failed restore leaves that database as it was.
  docker compose exec -T postgres pg_restore -U postgres -d $db --clean --if-exists --single-transaction --exit-on-error "/tmp/restore-$db.dump"
  if ($LASTEXITCODE -ne 0) { $failed += $db }
  docker compose exec -T postgres rm -f "/tmp/restore-$db.dump"
  if ($db -eq "command_center" -and $carry -and $failed -notcontains $db) {
    # The database's own mark: the api and the worker refuse to start until each part is applied again (whoever
    # starts them, from whichever folder). Created with the table an older dump does not have yet (0008's own).
    $parts = "'access', $(if ($access) { 'true' } else { 'false' }), 'removals', $(if ($removals) { 'true' } else { 'false' }), 'access_file', '$([IO.Path]::GetFileName($access))', 'removals_file', '$([IO.Path]::GetFileName($removals))', 'backup', '$((Split-Path -Leaf $folder).Replace("'", "''"))', 'at', now()"
    Invoke-Psql "CREATE TABLE IF NOT EXISTS brain_maintenance (name TEXT PRIMARY KEY, done_at TIMESTAMPTZ NOT NULL DEFAULT now(), detail JSONB NOT NULL DEFAULT '{}'::jsonb); INSERT INTO brain_maintenance (name, detail) VALUES ('restore_reapply_pending', jsonb_build_object($parts)) ON CONFLICT (name) DO UPDATE SET detail = EXCLUDED.detail, done_at = now()" | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Host "  (the database's restore mark could not be written; $pendingFile still guards start.ps1)" -ForegroundColor Yellow }
  }
}
if ($carry -and $failed -contains "command_center") {
  Remove-Item -LiteralPath $pendingFile -Force  # that database is unchanged: nothing to apply again
}

# --- 3. Applied again BEFORE anything serves the restored data -------------------------------------------------------
if (($dumps -contains "command_center") -and ($failed -notcontains "command_center") -and ($access -or $removals)) {
  Write-Host "`nApplying them again before anything starts..."
  Invoke-Reapply $access $removals
}

if ($script:problems.Count -gt 0) {
  Write-Host "`nNOT applied again: $($script:problems -join '; ')." -ForegroundColor Red
  Show-Reapply $access $removals
  Fail "the data was restored, but what it must not undo was NOT applied again, so the services were NOT started (nothing serves the restored data); run the commands above"
}
if ($carry -and (Test-Path -LiteralPath $pendingFile -PathType Leaf)) { Remove-Item -LiteralPath $pendingFile -Force }
if ($script:again -ge 0) {
  Write-Host "Withholds and purges made after this backup were applied again ($($script:again))." -ForegroundColor Green
}

# --- 4. Start ---------------------------------------------------------------------------------------------------------
Write-Host "`nStarting the application again..."
$global:LASTEXITCODE = 1  # stays 1 if the script cannot run at all
& (Join-Path $PSScriptRoot "start.ps1") -NoBuild
$started = $LASTEXITCODE -eq 0

Show-AccessLists
if ($access) { Write-Host "Every sign-in was ended: everyone signs in again." -ForegroundColor Yellow }
foreach ($line in $lost) { Write-Host "`n$line" -ForegroundColor Yellow }

if ($failed.Count -gt 0) { Fail "restoring $($failed -join ', ') failed (those databases were left unchanged); see the messages above" }
if (-not $started) { Fail "the data was restored (and what it must not undo applied again), but the services did not start; see: docker compose logs --tail=100 api worker n8n" }
if ($hasBrain) { Write-Host "Check the JARVIS Brain with: .\scripts\jarvis-import.ps1 -Target Production -Mode Status" }
Write-Host "Restore complete. Run .\scripts\smoke.ps1 to check the system." -ForegroundColor Green
exit 0
