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
foreach ($probe in @(@("Dashboard/API", "http://127.0.0.1:$port/api/health"), @("n8n", "http://127.0.0.1:$n8nPort/healthz/readiness"))) {
  try { $code = (Invoke-WebRequest $probe[1] -UseBasicParsing -TimeoutSec 5).StatusCode; Write-Host "$($probe[0]): HTTP $code" -ForegroundColor Green }
  catch { Write-Host "$($probe[0]): not answering" -ForegroundColor Red }
}
exit 0
