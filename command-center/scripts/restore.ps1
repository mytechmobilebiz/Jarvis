# Restores the databases from a folder written by backup.ps1. This REPLACES the
# current data, so it first takes a backup of the current state and asks you to
# type RESTORE (or pass -Yes). The application is stopped during the restore
# and started again afterwards. Use the same .env the backup was taken with.
#
# What the restore must not undo is kept (0.6.2). Before anything is stopped it is
# saved in backups\ (keys, ids, e-mail addresses and the audit log; never message
# text, never a password hash):
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
param(
  [Parameter(Mandatory = $true)][string]$BackupFolder,
  [switch]$Yes,
  [switch]$SkipBrainRemovals,
  [switch]$KeepRestoredAccess
)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$helper = "cc-restore-helper-$stamp"
$script:helperUp = $false

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
function Show-Lines($lines) { foreach ($line in $lines) { Write-Host "  $line" } }
function Fail($text) { Stop-Helper; Write-Host "RESTORE STOPPED: $text" -ForegroundColor Red; exit 1 }

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

# --- 1. Save what the restore must not undo, before anything is stopped ---------------------------------------------
$backups = Join-Path $root "backups"
$removals = ""
$access = ""
$hasBrain = $false
$lost = @()
if ($dumps -contains "command_center") {
  $brain = docker compose exec -T postgres psql -U postgres -d command_center -tAc "SELECT count(*) FROM schema_migrations WHERE version = '0006_brain'" 2>$null
  $hasBrain = ("$brain".Trim() -eq "1")
  Write-Host "`nSaving what the restore must not undo (keys, ids, e-mail addresses and the audit log; never message text)..."
  if (-not (Start-Helper)) {
    Write-Host "  a helper container of the api image could not be started (docker compose run api)" -ForegroundColor Yellow
  } else {
    $inside = "/tmp/access-before-restore-$stamp.json"
    $out = Invoke-Helper restore-carry export --out $inside
    $code = $LASTEXITCODE
    Show-Lines $out
    $target = Join-Path $backups "access-before-restore-$stamp.json"
    if ($code -eq 0) {
      docker cp "${helper}:$inside" $target
      if ($LASTEXITCODE -eq 0 -and (Test-Path $target -PathType Leaf)) { $access = $target }
    }
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
    $counts = docker compose exec -T postgres psql -U postgres -d command_center -tAc "SELECT count(*) FILTER (WHERE reason = 'withheld by an admin') || ' withheld messages and copies, ' || count(*) FILTER (WHERE reason = 'purged by an admin') || ' purged items with a source key' FROM brain_tombstones" 2>$null
    $what = "every withhold, purge and release made in the JARVIS Brain after the backup is undone, and that text is readable again until it is withheld or purged again by hand (the current database holds $("$counts".Trim()); the safety backup above still holds them)"
    if ($SkipBrainRemovals) { $lost += "BRAIN REMOVALS NOT KEPT (-SkipBrainRemovals): $what." }
    else { $missing += "the JARVIS Brain's withholds, purges and releases (-SkipBrainRemovals accepts that $what)" }
  }
  if ($missing.Count -gt 0) {
    Fail "could not save $($missing -join '; nor '); nothing was changed. Start the Command Center and try again, or pass the override named above."
  }
}

# --- 2. The restore -------------------------------------------------------------------------------------------------
Write-Host "`nStopping the application (the database keeps running)..."
docker compose stop api worker dashboard n8n
if ($LASTEXITCODE -ne 0) { Fail "could not stop the application" }

$failed = @()
foreach ($db in $dumps) {
  Write-Host "Restoring $db..."
  docker compose cp (Join-Path $folder "$db.dump") "postgres:/tmp/restore-$db.dump"
  if ($LASTEXITCODE -ne 0) { $failed += $db; continue }
  # One transaction per database: a failed restore leaves that database as it was.
  docker compose exec -T postgres pg_restore -U postgres -d $db --clean --if-exists --single-transaction --exit-on-error "/tmp/restore-$db.dump"
  if ($LASTEXITCODE -ne 0) { $failed += $db }
  docker compose exec -T postgres rm -f "/tmp/restore-$db.dump"
}

# --- 3. Applied again BEFORE anything serves the restored data -------------------------------------------------------
# A helper container (no api, worker, dashboard or n8n runs yet): restore-carry apply migrates the restored database,
# puts back the audit entries, applies the access narrowing again and ends every sign-in; decisions-apply
# --removals-only applies the Brain's removals again (as "system (restore)", keeping who decided each and when).
$problems = @()
$kept = @()
$reverted = @()
$again = -1
function Show-Reapply {
  Write-Host "Apply them, then start the services (the files hold keys, ids and e-mail addresses only):" -ForegroundColor Red
  Write-Host "  docker compose run -d --no-deps --name cc-restore-helper api python -c `"import time; time.sleep(3600)`"" -ForegroundColor Red
  if ($access) {
    Write-Host "  docker cp `"$access`" cc-restore-helper:/tmp/access.json" -ForegroundColor Red
    Write-Host "  docker exec cc-restore-helper python -m app.cli restore-carry apply --in /tmp/access.json --yes" -ForegroundColor Red
  } else {
    Write-Host "  docker exec cc-restore-helper python -m app.cli migrate" -ForegroundColor Red
  }
  if ($removals) {
    Write-Host "  docker cp `"$removals`" cc-restore-helper:/tmp/removals.json" -ForegroundColor Red
    Write-Host "  docker exec cc-restore-helper python -m app.cli brain-import decisions-apply --removals-only --in /tmp/removals.json --yes" -ForegroundColor Red
  }
  Write-Host "  docker rm -f cc-restore-helper" -ForegroundColor Red
  Write-Host "  .\scripts\start.ps1 -NoBuild" -ForegroundColor Red
}
if (($dumps -contains "command_center") -and ($failed -notcontains "command_center") -and ($access -or $removals)) {
  Write-Host "`nApplying them again before anything starts..."
  if (-not (Start-Helper)) {
    $problems += "the helper container could not be started"
  } else {
    if ($access) {
      docker cp $access "${helper}:/tmp/access.json"
      $code = $LASTEXITCODE
      $out = @()
      if ($code -eq 0) {
        $out = Invoke-Helper restore-carry apply --in /tmp/access.json --yes
        $code = $LASTEXITCODE
      }
      Show-Lines $out
      foreach ($line in $out) {
        if ("$line" -match '^\s*kept: (.+)$') { $kept += $Matches[1] }
        if ("$line" -match '^\s*(reverted|not applied again): (.+)$') { $reverted += $Matches[2] }
      }
      if ($code -ne 0) { $problems += "the users' access and the audit log (exit $code)" }
    } else {
      $out = Invoke-Helper migrate
      Show-Lines $out
      if ($LASTEXITCODE -ne 0) { $problems += "the database migration (exit $LASTEXITCODE)" }
    }
    if ($removals) {
      docker cp $removals "${helper}:/tmp/removals.json"
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
          Write-Host "  an import, rollback or rescan holds the lock; trying again in 10 seconds..."
          Start-Sleep -Seconds 10
        }
      }
      Show-Lines $out
      if ($code -ne 0) {
        $problems += "the JARVIS Brain's withholds, purges and releases (exit $code)"
      } elseif ($text -match '(?m)^\s*(removal|purge|release) not applied') {
        $problems += "some of the JARVIS Brain's withholds, purges or releases (the 'not applied' lines above)"
      } else {
        # decisions-apply prints "removals applied: <outcome> <n>, ..." (tests/unit/test_brain_ops.py runs this
        # pattern against the CLI's own output). What was already in the restored backup is "already done".
        $again = 0
        if ($text -match '(?m)^removals applied: (.+)$') {
          foreach ($part in ($Matches[1] -split ', ')) {
            if ($part -match '^(.+) (\d+)$' -and $Matches[1] -ne "already done") { $again += [int]$Matches[2] }
          }
        }
      }
    }
  }
  Stop-Helper
}

if ($problems.Count -gt 0) {
  Write-Host "`nNOT applied again: $($problems -join '; ')." -ForegroundColor Red
  Show-Reapply
  Fail "the data was restored, but what it must not undo was NOT applied again, so the services were NOT started (nothing serves the restored data); run the commands above"
}
if ($again -ge 0) {
  Write-Host "Withholds and purges made after this backup were applied again ($again)." -ForegroundColor Green
}

# --- 4. Start ---------------------------------------------------------------------------------------------------------
Write-Host "`nStarting the application again..."
$global:LASTEXITCODE = 1  # stays 1 if the script cannot run at all
& (Join-Path $PSScriptRoot "start.ps1") -NoBuild
$started = $LASTEXITCODE -eq 0

if ($kept.Count -gt 0) {
  Write-Host "`nAccess changes made after the backup, applied again:" -ForegroundColor Green
  foreach ($line in $kept) { Write-Host "  $line" -ForegroundColor Green }
}
if ($reverted.Count -gt 0) {
  Write-Host "`nWARNING: the restore reverts these user changes made after the backup (redo them on the Users page if still wanted):" -ForegroundColor Yellow
  foreach ($line in $reverted) { Write-Host "  $line" -ForegroundColor Yellow }
}
if ($access) { Write-Host "Every sign-in was ended: everyone signs in again." -ForegroundColor Yellow }
foreach ($line in $lost) { Write-Host "`n$line" -ForegroundColor Yellow }

if ($failed.Count -gt 0) { Fail "restoring $($failed -join ', ') failed (those databases were left unchanged); see the messages above" }
if (-not $started) { Fail "the data was restored (and what it must not undo applied again), but the services did not start; see: docker compose logs --tail=100 api worker n8n" }
if ($hasBrain) { Write-Host "Check the JARVIS Brain with: .\scripts\jarvis-import.ps1 -Target Production -Mode Status" }
Write-Host "Restore complete. Run .\scripts\smoke.ps1 to check the system." -ForegroundColor Green
exit 0
