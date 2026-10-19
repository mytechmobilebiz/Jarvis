# Builds (first time) and starts the Command Center, then waits until it answers.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1
param([switch]$NoBuild)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not (Test-Path ".env")) { Write-Host "No .env yet. Run .\scripts\init-env.ps1 first." -ForegroundColor Red; exit 1 }

$envValues = @{}
foreach ($line in Get-Content ".env") { if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $envValues[$Matches[1]] = $Matches[2].Trim() } }
$dashboardPort = if ($envValues["DASHBOARD_PORT"]) { $envValues["DASHBOARD_PORT"] } else { "8090" }
$n8nPort = if ($envValues["N8N_PORT"]) { $envValues["N8N_PORT"] } else { "5680" }

# Remote access (docker-compose.tunnel.yml, see docs\REMOTE_ACCESS_PLAN.md): if .env enables it,
# check its files first, so a missing token does not leave a half-started system.
if ($envValues["COMPOSE_FILE"] -match 'docker-compose\.tunnel\.yml') {
  $tokenFile = if ($envValues["CLOUDFLARED_TOKEN_FILE"]) { $envValues["CLOUDFLARED_TOKEN_FILE"] } else { ".\secrets\cloudflared-token.txt" }
  foreach ($needed in @($tokenFile, ".\remote-access\tunnel.conf")) {
    if (-not (Test-Path $needed -PathType Leaf)) {
      Write-Host "Remote access is switched on in .env (COMPOSE_FILE), but $needed is missing." -ForegroundColor Red
      Write-Host "Restore the file, or switch remote access off as described in docs\REMOTE_ACCESS_PLAN.md (section 10)." -ForegroundColor Red
      exit 1
    }
  }
}

# Also with -NoBuild: compose builds an image that is missing, and that build must be checked too.
$global:LASTEXITCODE = 1
& (Join-Path $PSScriptRoot "prepare-build.ps1")
if ($LASTEXITCODE -ne 0) { exit 1 }

$composeArgs = @("compose", "up", "-d")
if (-not $NoBuild) { $composeArgs += "--build" }
Write-Host "docker $($composeArgs -join ' ')"
& docker @composeArgs
if ($LASTEXITCODE -ne 0) { Write-Host "docker compose up failed (exit $LASTEXITCODE). See: docker compose logs --tail=100" -ForegroundColor Red; exit 1 }

function Wait-Url($url, $label, $seconds) {
  $deadline = (Get-Date).AddSeconds($seconds)
  while ((Get-Date) -lt $deadline) {
    try {
      $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
      if ($r.StatusCode -eq 200) { Write-Host "  $label is up" -ForegroundColor Green; return $true }
    } catch { }
    Start-Sleep -Seconds 3
  }
  Write-Host "  $label did not answer within $seconds s" -ForegroundColor Red
  return $false
}

Write-Host "Waiting for the services (n8n's first start takes a minute)..."
$apiOk = Wait-Url "http://127.0.0.1:$dashboardPort/api/health" "Dashboard + API" 240
$n8nOk = Wait-Url "http://127.0.0.1:$n8nPort/healthz/readiness" "n8n" 300
docker compose ps

if ($apiOk -and $n8nOk) {
  Write-Host ""
  Write-Host "Dashboard:  http://localhost:$dashboardPort   (sign in with ADMIN_EMAIL / ADMIN_PASSWORD from .env)" -ForegroundColor Cyan
  Write-Host "n8n editor: http://localhost:$n8nPort   (ADMIN_EMAIL / N8N_OWNER_PASSWORD from .env)" -ForegroundColor Cyan
  Write-Host "Verify everything with: powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1"
  exit 0
} else {
  Write-Host "Something did not come up. Inspect with: docker compose logs --tail=100 api worker n8n" -ForegroundColor Red
  exit 1
}
