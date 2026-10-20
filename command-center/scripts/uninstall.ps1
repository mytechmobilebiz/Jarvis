# Removes the Command Center's containers, network and locally built images.
# Your data (database and n8n volumes) is KEPT unless you pass -DeleteData.
# Web research's search service goes too (its container, its two networks and its
# cache volume, which holds no data), also when it is no longer in COMPOSE_FILE;
# its image is kept, like the other pulled images.
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
. (Join-Path $PSScriptRoot "compose-env.ps1")
$null = Use-ComposeEnv $root
$project = Get-ComposeProjectName

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
  $global:LASTEXITCODE = 1  # stays 1 if the script cannot run at all
  & (Join-Path $PSScriptRoot "backup.ps1")
  if ($LASTEXITCODE -ne 0) { Write-Host "Backup failed; not deleting data. Re-run with -SkipBackup to override." -ForegroundColor Red; exit 1 }
}

# With -DeleteData, the anonymous volumes this project's containers use go too. "down --volumes" misses one that
# Compose handed on to a recreated container (db-init's empty one, on installations from before 0.7.0) and it carries
# no project label, so they are named here, before the containers go.
$anonymous = @()
if ($DeleteData) {
  $unnamed = @(docker volume ls -q --filter "label=com.docker.volume.anonymous" | Where-Object { $_ })
  foreach ($id in @(docker ps -aq --filter "label=com.docker.compose.project=$project" | Where-Object { $_ })) {
    foreach ($mount in @(docker inspect --format '{{range .Mounts}}{{.Type}} {{.Name}}{{println}}{{end}}' $id)) {
      $parts = @("$mount".Trim().Split(" ") | Where-Object { $_ })
      if ($parts.Count -eq 2 -and $parts[0] -eq "volume" -and $unnamed -contains $parts[1] -and $anonymous -notcontains $parts[1]) {
        $anonymous += $parts[1]
      }
    }
  }
}

if ($DeleteData) { docker compose down --volumes --remove-orphans } else { docker compose down --remove-orphans }
if ($LASTEXITCODE -ne 0) { Write-Host "docker compose down failed" -ForegroundColor Red; exit 1 }
$present = @(docker volume ls -q | Where-Object { $_ })
foreach ($volume in @($anonymous | Where-Object { $present -contains $_ })) {
  docker volume rm $volume | Out-Null
  if ($LASTEXITCODE -eq 0) { Write-Host "removed volume $volume (anonymous, of this project's containers)" }
}

# Web research: "down" removes only what the files in use declare, so these go by their labels.
foreach ($key in @("research", "research_egress")) {
  foreach ($network in @(docker network ls -q --filter "label=com.docker.compose.project=$project" --filter "label=com.docker.compose.network=$key" | Where-Object { $_ })) {
    docker network rm $network | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "removed network $($project)_$key" }
  }
}
foreach ($volume in @(docker volume ls -q --filter "label=com.docker.compose.project=$project" --filter "label=com.docker.compose.volume=searxng_cache" | Where-Object { $_ })) {
  docker volume rm $volume | Out-Null
  if ($LASTEXITCODE -eq 0) { Write-Host "removed volume $volume (web research's search cache, no data)" }
}
$searxngImages = @(docker image ls --format '{{.Repository}}:{{.Tag}}' | Where-Object { $_ -match '(^|/)searxng/searxng:' })
if ($searxngImages.Count -gt 0) {
  Write-Host "Kept the web research search image (other projects may use it); remove it with: docker image rm $($searxngImages -join ' ')"
}

# Only the images this project built itself; pulled images (postgres, n8n,
# nginx, python, node, searxng) may be shared with other projects and are left alone.
foreach ($image in @("infinite-command-center/backend:local", "infinite-command-center/dashboard:local")) {
  docker image inspect $image *> $null
  if ($LASTEXITCODE -eq 0) { docker image rm $image | Out-Null; Write-Host "removed image $image" }
}

$left = @(docker volume ls --filter "label=com.docker.compose.project=$project" --format '{{.Name}}' | Where-Object { $_ })
$present = @(docker volume ls -q | Where-Object { $_ })
$left += @($anonymous | Where-Object { $present -contains $_ })
if ($left.Count -gt 0) { Write-Host "Data volumes kept: $($left -join ', ')" } else { Write-Host "No data volumes remain." }
Write-Host "Uninstalled. The folder $root (including .env with its secrets) is still on disk; delete it yourself when you no longer need it."
