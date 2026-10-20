# Shows container state and whether the dashboard and n8n answer. Read-only.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\status.ps1
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
. (Join-Path $PSScriptRoot "compose-env.ps1")
$research = Use-ComposeEnv $root
$envValues = @{}
if (Test-Path ".env") { foreach ($line in Get-Content ".env") { if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $envValues[$Matches[1]] = $Matches[2].Trim() } } }
$port = if ($envValues["DASHBOARD_PORT"]) { $envValues["DASHBOARD_PORT"] } else { "8090" }
$n8nPort = if ($envValues["N8N_PORT"]) { $envValues["N8N_PORT"] } else { "5680" }
docker compose ps
$expected = if (Test-Path "VERSION") { (Get-Content "VERSION" -TotalCount 1).Trim() } else { "unknown" }
Write-Host "This folder: version $expected ($root)"
$apiVersion = (docker compose exec -T api python -c "import app; print(app.__version__)" 2>$null | Out-String).Trim()
if ($apiVersion) {
  $color = if ($apiVersion -eq $expected) { "Green" } else { "Yellow" }
  Write-Host "Running API container: version $apiVersion" -ForegroundColor $color
} else { Write-Host "Running API container: not answering" -ForegroundColor Red }
try {
  $dashboardVersion = (Invoke-RestMethod "http://127.0.0.1:$port/version.json" -TimeoutSec 5).version
  $color = if ($dashboardVersion -eq $expected) { "Green" } else { "Yellow" }
  Write-Host "Running dashboard: version $dashboardVersion" -ForegroundColor $color
} catch { Write-Host "Running dashboard: version not available" -ForegroundColor Red }
# Which folder the running API container was created from (another folder means the containers were last created from there).
$apiId = (docker compose ps -q api 2>$null | Out-String).Trim()
if ($apiId) {
  $labels = docker inspect $apiId --format '{{json .Config.Labels}}' 2>$null | Out-String | ConvertFrom-Json
  if ($labels) { Write-Host "Running API container was created from: $($labels.'com.docker.compose.project.working_dir')" }
}
foreach ($probe in @(@("Dashboard/API", "http://127.0.0.1:$port/api/health"), @("n8n", "http://127.0.0.1:$n8nPort/healthz/readiness"))) {
  try { $code = (Invoke-WebRequest $probe[1] -UseBasicParsing -TimeoutSec 5).StatusCode; Write-Host "$($probe[0]): HTTP $code" -ForegroundColor Green }
  catch { Write-Host "$($probe[0]): not answering" -ForegroundColor Red }
}
# Web research's search service: the container only (whether agents may search is set in the Research Center).
$project = Get-ComposeProjectName
$searxng = @(docker ps -a -q --filter "label=com.docker.compose.project=$project" --filter "label=cc.research=searxng" 2>$null | Where-Object { $_ })
$state = ""
if ($searxng.Count -gt 0) {
  $state = (docker inspect --format '{{.State.Status}}{{if .State.Health}}, {{.State.Health.Status}}{{end}}' $searxng[0] 2>$null | Out-String).Trim()
}
if (-not $research.Listed) {
  if ($state) { Write-Host "Web research search service (SearXNG): switched off, but its container is still there ($state); run scripts\research.ps1 -Disable" -ForegroundColor Yellow }
  else { Write-Host "Web research search service (SearXNG): switched off" }
} elseif (-not $research.Active) {
  Write-Host "Web research search service (SearXNG): switched on, but SEARXNG_SECRET is missing (see above)" -ForegroundColor Yellow
} elseif (-not $state) {
  Write-Host "Web research search service (SearXNG): switched on, not running (start.ps1 starts it)" -ForegroundColor Yellow
} else {
  $color = if ($state -eq "running, healthy") { "Green" } else { "Yellow" }
  Write-Host "Web research search service (SearXNG): $state" -ForegroundColor $color
}
exit 0
