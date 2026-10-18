# Read-only readiness check for running the Command Center on this PC.
# Changes nothing: no installs, no settings, no containers started or stopped.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\preflight.ps1

$root = Split-Path -Parent $PSScriptRoot
$problems = 0
function Ok($msg) { Write-Host "  [ok]   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  [warn] $msg" -ForegroundColor Yellow }
function Bad($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red; $script:problems++ }

function Read-DotEnv($path) {
  $values = @{}
  if (Test-Path $path) {
    foreach ($line in Get-Content $path) {
      if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $values[$Matches[1]] = $Matches[2].Trim() }
    }
  }
  return $values
}

$envValues = Read-DotEnv (Join-Path $root ".env")
$dashboardPort = if ($envValues["DASHBOARD_PORT"]) { [int]$envValues["DASHBOARD_PORT"] } else { 8090 }
$n8nPort = if ($envValues["N8N_PORT"]) { [int]$envValues["N8N_PORT"] } else { 5680 }

Write-Host "=== Infinite AI Command Center: preflight ==="
Write-Host "Project folder: $root"

Write-Host "`nDocker"
if (Get-Command docker -ErrorAction SilentlyContinue) {
  $server = docker info --format '{{.ServerVersion}}' 2>$null
  if ($LASTEXITCODE -eq 0 -and $server) { Ok "Docker engine $server is running" } else { Bad "Docker CLI found but the engine is not reachable: start Docker Desktop" }
  $composeVersion = docker compose version --short 2>$null
  if ($LASTEXITCODE -eq 0) { Ok "Docker Compose $composeVersion" } else { Bad "docker compose is not available" }
  $osType = docker info --format '{{.OSType}}' 2>$null
  if ($osType -eq "linux") { Ok "Linux containers (WSL 2 backend)" } elseif ($osType) { Bad "Docker is in $osType container mode; switch to Linux containers" }
} else {
  Bad "Docker CLI not found: install Docker Desktop"
}

Write-Host "`nWSL"
if (Get-Command wsl.exe -ErrorAction SilentlyContinue) {
  $wsl = (wsl.exe --status 2>$null) -join " "
  if ($wsl -match "2") { Ok "WSL present (default version 2 expected)" } else { Warn "Could not confirm WSL 2; Docker Desktop settings should show the WSL 2 engine" }
} else { Warn "wsl.exe not found" }

Write-Host "`nPorts (published on 127.0.0.1 only)"
function Test-PortFree([int]$port) {
  $listener = New-Object System.Net.Sockets.TcpListener ([System.Net.IPAddress]::Loopback), $port
  try { $listener.Start(); $listener.Stop(); return $true } catch { return $false }
}
foreach ($port in @($dashboardPort, $n8nPort)) {
  if (Test-PortFree $port) { Ok "Port $port is free"; continue }
  $ours = docker ps --filter "label=com.docker.compose.project=infinite-command-center" --format '{{.Ports}}' 2>$null | Select-String ":$port->"
  if ($ours) { Ok "Port $port is used by this Command Center (already running)"; continue }
  $owner = ""
  if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
    $conn = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) { $owner = " by " + (Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue).ProcessName + " (PID $($conn.OwningProcess))" }
  }
  Bad "Port $port is already in use$owner; set DASHBOARD_PORT / N8N_PORT in .env to a free port"
}

Write-Host "`nOther Docker projects (left untouched)"
$projects = docker compose ls --all --format json 2>$null | ConvertFrom-Json
foreach ($p in $projects) {
  if ($p.Name -eq "infinite-command-center") { Ok "this project: $($p.Status)" } else { Write-Host "  [info] $($p.Name): $($p.Status)" }
}

Write-Host "`nDisk"
$drive = (Get-Item $root).PSDrive
$freeGb = [math]::Round($drive.Free / 1GB, 1)
if ($freeGb -ge 10) { Ok "$freeGb GB free on $($drive.Name):" } else { Warn "only $freeGb GB free on $($drive.Name): (images and data need about 5 GB)" }

Write-Host "`nConfiguration"
if (Test-Path (Join-Path $root ".env")) {
  $required = "ADMIN_EMAIL","ADMIN_PASSWORD","POSTGRES_PASSWORD","CC_DB_PASSWORD","N8N_DB_PASSWORD","CC_SERVICE_TOKEN","CC_SECRETS_KEY","N8N_WEBHOOK_SECRET","N8N_ENCRYPTION_KEY","N8N_OWNER_PASSWORD"
  $missing = $required | Where-Object { -not $envValues[$_] }
  if ($missing) { Bad ".env is missing: $($missing -join ', ')" } else { Ok ".env has every required secret" }
  if ($envValues["ANTHROPIC_API_KEY"]) { Ok "ANTHROPIC_API_KEY set: the supervisor will use Claude (metered)" } else { Write-Host "  [info] ANTHROPIC_API_KEY empty: the supervisor plans with deterministic rules" }
} else {
  Warn ".env not found yet: run .\scripts\init-env.ps1"
}

Write-Host ""
if ($problems -eq 0) { Write-Host "Preflight passed." -ForegroundColor Green; exit 0 } else { Write-Host "$problems problem(s) to fix first." -ForegroundColor Red; exit 1 }
