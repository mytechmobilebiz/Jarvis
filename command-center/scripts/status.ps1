# Shows container state and whether the dashboard and n8n answer. Read-only.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\status.ps1
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
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
# Which folder the running API container was created from (an older install folder means it was not replaced).
$apiId = (docker compose ps -q api 2>$null | Out-String).Trim()
if ($apiId) {
  $labels = docker inspect $apiId --format '{{json .Config.Labels}}' 2>$null | Out-String | ConvertFrom-Json
  if ($labels) { Write-Host "Running API container was created from: $($labels.'com.docker.compose.project.working_dir')" }
}
foreach ($probe in @(@("Dashboard/API", "http://127.0.0.1:$port/api/health"), @("n8n", "http://127.0.0.1:$n8nPort/healthz/readiness"))) {
  try { $code = (Invoke-WebRequest $probe[1] -UseBasicParsing -TimeoutSec 5).StatusCode; Write-Host "$($probe[0]): HTTP $code" -ForegroundColor Green }
  catch { Write-Host "$($probe[0]): not answering" -ForegroundColor Red }
}
exit 0
