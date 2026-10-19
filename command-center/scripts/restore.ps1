# Restores the databases from a folder written by backup.ps1. This REPLACES the
# current data, so it first takes a backup of the current state and asks you to
# type RESTORE (or pass -Yes). The application is stopped during the restore
# and started again afterwards. Use the same .env the backup was taken with.
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

if ($failed.Count -gt 0) { Fail "restoring $($failed -join ', ') failed (those databases were left unchanged); see the messages above" }
if (-not $started) { Fail "the data was restored but the services did not start; see: docker compose logs --tail=100 api worker n8n" }
Write-Host "Restore complete. Run .\scripts\smoke.ps1 to check the system." -ForegroundColor Green
exit 0
