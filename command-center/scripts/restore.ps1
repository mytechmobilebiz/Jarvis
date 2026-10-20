# Restores the databases from a folder written by backup.ps1. This REPLACES the
# current data, so it first takes a backup of the current state and asks you to
# type RESTORE (or pass -Yes). The application is stopped during the restore
# and started again afterwards. Use the same .env the backup was taken with.
#
# JARVIS Brain privacy decisions are kept (0.6.2): the messages an admin withheld,
# the items an admin purged and the held rows an admin released are exported
# before the restore (backups\removals-before-restore-<time>.json, keys and ids
# only, never text) and applied again after the restart, so restoring an older
# backup does not bring back what was removed after it.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\restore.ps1 -BackupFolder .\backups\20261018-120000
param(
  [Parameter(Mandatory = $true)][string]$BackupFolder,
  [switch]$Yes
)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
function Fail($text) { Write-Host "RESTORE STOPPED: $text" -ForegroundColor Red; exit 1 }

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

# JARVIS Brain (0.6.2): every withhold, purge and release, exported while the api still runs. Keys and JARVIS ids
# only; the file stays in backups\ next to the safety backup. Without it the restore still runs (the safety backup
# holds the decisions), and the warning says what to redo.
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$inside = "/tmp/removals-before-restore-$stamp.json"
$removals = ""
$redo = "Withholds and purges made in the JARVIS Brain after this backup must then be made again by hand (the safety backup above still holds them)."
if ($dumps -contains "command_center") {
  $brain = docker compose exec -T postgres psql -U postgres -d command_center -tAc "SELECT count(*) FROM schema_migrations WHERE version = '0006_brain'" 2>$null
  $api = docker compose ps -q --status running api 2>$null
  if ("$brain".Trim() -ne "1") {
    Write-Host "No JARVIS Brain in the current database: no withhold or purge to keep."
  } elseif (-not "$api".Trim()) {
    Write-Host "WARNING: the api is not running, so the JARVIS Brain's withholds and purges could not be exported first. $redo" -ForegroundColor Yellow
  } else {
    Write-Host "`nExporting the JARVIS Brain's withholds, purges and releases (keys and ids only)..."
    $out = docker compose exec -T api python -m app.cli brain-import decisions-export --removals-only --out $inside 2>&1
    $code = $LASTEXITCODE
    foreach ($line in $out) { Write-Host "  $line" }
    $target = Join-Path (Join-Path $root "backups") "removals-before-restore-$stamp.json"
    if ($code -eq 0) {
      docker compose cp "api:$inside" $target
      $code = $LASTEXITCODE
      if ($code -eq 0 -and (Test-Path $target -PathType Leaf)) { $removals = $target }
    }
    docker compose exec -T api rm -f $inside 2>$null | Out-Null
    if ($removals) {
      Write-Host "Saved: $removals"
    } else {
      Write-Host "WARNING: the JARVIS Brain's withholds and purges could not be exported (exit $code). $redo" -ForegroundColor Yellow
    }
  }
}

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

Write-Host "`nStarting the application again..."
$global:LASTEXITCODE = 1  # stays 1 if the script cannot run at all
& (Join-Path $PSScriptRoot "start.ps1") -NoBuild
$started = $LASTEXITCODE -eq 0

# JARVIS Brain (0.6.2): the withholds, purges and releases exported above, applied again to the restored data
# (decisions-apply --removals-only, as "system (restore)"). The start-up check may hold the import lock for a moment:
# then decisions-apply refuses (exit 2) and is tried again, ten seconds later, up to three times.
function Show-Reapply {
  Write-Host "Apply them when the services run (this file holds keys and ids only):" -ForegroundColor Red
  Write-Host "  docker compose cp `"$removals`" api:/tmp/removals.json" -ForegroundColor Red
  Write-Host "  docker compose exec -T api python -m app.cli brain-import decisions-apply --removals-only --in /tmp/removals.json --yes" -ForegroundColor Red
}
$reapplied = $true
if ($removals -and $started -and $failed -notcontains "command_center") {
  Write-Host "`nApplying the JARVIS Brain's withholds, purges and releases again..."
  $reapplied = $false
  $text = ""
  docker compose cp $removals "api:$inside"
  $code = $LASTEXITCODE
  if ($code -eq 0) {
    for ($try = 1; $try -le 4; $try++) {
      $out = docker compose exec -T api python -m app.cli brain-import decisions-apply --removals-only --in $inside --yes 2>&1
      $code = $LASTEXITCODE
      $text = ($out | ForEach-Object { "$_" }) -join "`n"
      if ($code -eq 0) { $reapplied = $true; break }
      if ($code -ne 2 -or $text -notmatch 'is running' -or $try -eq 4) { break }
      Write-Host "  the start-up check is still running; trying again in 10 seconds..."
      Start-Sleep -Seconds 10
    }
    foreach ($line in $out) { Write-Host "  $line" }
    docker compose exec -T api rm -f $inside 2>$null | Out-Null
  }
  if ($reapplied) {
    # decisions-apply prints "removals applied: <outcome> <n>, ..." (tests/unit/test_brain_ops.py runs this pattern
    # against the CLI's own output). What was already in the restored backup is "already done".
    $again = 0
    if ($text -match '(?m)^removals applied: (.+)$') {
      foreach ($part in ($Matches[1] -split ', ')) {
        if ($part -match '^(.+) (\d+)$' -and $Matches[1] -ne "already done") { $again += [int]$Matches[2] }
      }
    }
    Write-Host "Withholds and purges made after this backup were applied again ($again). Run .\scripts\jarvis-import.ps1 -Target Production -Mode Status." -ForegroundColor Green
  } else {
    Write-Host "The JARVIS Brain's withholds and purges were NOT applied again (exit $code)." -ForegroundColor Red
    Show-Reapply
  }
}

if ($failed.Count -gt 0) { Fail "restoring $($failed -join ', ') failed (those databases were left unchanged); see the messages above" }
if (-not $started) {
  if ($removals -and $failed -notcontains "command_center") {
    Write-Host "The JARVIS Brain's withholds and purges made after this backup are not applied yet." -ForegroundColor Red
    Show-Reapply
  }
  Fail "the data was restored but the services did not start; see: docker compose logs --tail=100 api worker n8n"
}
if (-not $reapplied) { Fail "the data was restored, but the JARVIS Brain's withholds and purges still have to be applied again (commands above)" }
Write-Host "Restore complete. Run .\scripts\smoke.ps1 to check the system." -ForegroundColor Green
exit 0
