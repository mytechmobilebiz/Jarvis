# Removes the Command Center's containers, network and locally built images.
# Your data (database and n8n volumes) is KEPT unless you pass -DeleteData.
# Nothing outside the Compose project "infinite-command-center" is touched:
# not XAMPP, not other Docker projects, not shared base images.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1 -DeleteData
param(
  [switch]$DeleteData,
  [switch]$Yes,         # skip the confirmation prompts (for scripted use)
  [switch]$SkipBackup   # with -DeleteData: do not take a final backup first
)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$project = "infinite-command-center"

Write-Host "This removes the containers and images of '$project' in $root."
if ($DeleteData) { Write-Host "-DeleteData: the PostgreSQL and n8n volumes will be DELETED permanently." -ForegroundColor Red }
if (-not $Yes) {
  $answer = Read-Host "Type UNINSTALL to continue"
  if ($answer -ne "UNINSTALL") { Write-Host "Cancelled."; exit 1 }
  if ($DeleteData) {
    $answer = Read-Host "Type DELETE-DATA to confirm deleting all Command Center data"
    if ($answer -ne "DELETE-DATA") { Write-Host "Cancelled."; exit 1 }
  }
}

if ($DeleteData -and -not $SkipBackup) {
  Write-Host "Taking a final backup first..."
  & (Join-Path $PSScriptRoot "backup.ps1")
  if ($LASTEXITCODE -ne 0) { Write-Host "Backup failed; not deleting data. Re-run with -SkipBackup to override." -ForegroundColor Red; exit 1 }
}

if ($DeleteData) { docker compose down --volumes --remove-orphans } else { docker compose down --remove-orphans }
if ($LASTEXITCODE -ne 0) { Write-Host "docker compose down failed" -ForegroundColor Red; exit 1 }

# Only the images this project built itself; pulled images (postgres, n8n,
# nginx, python, node) may be shared with other projects and are left alone.
foreach ($image in @("infinite-command-center/backend:local", "infinite-command-center/dashboard:local")) {
  docker image inspect $image *> $null
  if ($LASTEXITCODE -eq 0) { docker image rm $image | Out-Null; Write-Host "removed image $image" }
}

$left = docker volume ls --filter "label=com.docker.compose.project=$project" --format '{{.Name}}'
if ($left) { Write-Host "Data volumes kept: $($left -join ', ')" } else { Write-Host "No data volumes remain." }
Write-Host "Uninstalled. The folder $root (including .env with its secrets) is still on disk; delete it yourself when you no longer need it."
