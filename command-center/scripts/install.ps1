# One-step installer for a Windows PC with Docker Desktop (WSL 2 engine).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 -AdminEmail you@yourdomain.com
#
# Steps: prerequisite checks -> .env with fresh secrets (only if missing) ->
# configuration validation inside the application image -> start (database
# roles, databases and migrations are created on first start) -> smoke test.
# Re-running it is safe: an existing .env and existing data are kept.
param(
  [string]$AdminEmail = "",
  [switch]$SkipSmokeTest
)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$scripts = Join-Path $root "scripts"
function Step($text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }
function Fail($text) { Write-Host "INSTALL STOPPED: $text" -ForegroundColor Red; exit 1 }

Step "1/5 Checking prerequisites"
& (Join-Path $scripts "preflight.ps1")
if ($LASTEXITCODE -ne 0) { Fail "fix the preflight problems above, then run install.ps1 again" }

Step "2/5 Configuration"
if (Test-Path (Join-Path $root ".env")) {
  Write-Host "Keeping the existing .env."
} else {
  if (-not $AdminEmail) { $AdminEmail = Read-Host "Email address for the first admin account" }
  if ($AdminEmail -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$') { Fail "'$AdminEmail' is not an email address" }
  & (Join-Path $scripts "init-env.ps1") -AdminEmail $AdminEmail
  if ($LASTEXITCODE -ne 0) { Fail "could not write .env" }
}
docker compose config --quiet
if ($LASTEXITCODE -ne 0) { Fail "docker compose rejected the configuration (see the message above)" }

Step "3/5 Building and validating the application image"
docker compose build
if ($LASTEXITCODE -ne 0) { Fail "the image build failed; check the internet connection and Docker Desktop" }
docker compose run --rm --no-deps api python -m app.cli check-config
if ($LASTEXITCODE -ne 0) { Fail "configuration validation failed; correct .env and re-run" }

Step "4/5 Starting (first start initialises the databases)"
& (Join-Path $scripts "start.ps1") -NoBuild
if ($LASTEXITCODE -ne 0) { Fail "the services did not start; see: docker compose logs --tail=100 api worker n8n" }
docker compose exec -T api python -m app.cli db-status
if ($LASTEXITCODE -ne 0) { Fail "the database was not initialised" }

if ($SkipSmokeTest) {
  Step "5/5 Smoke test skipped"
} else {
  Step "5/5 Smoke test"
  & (Join-Path $scripts "smoke.ps1")
  if ($LASTEXITCODE -ne 0) { Fail "the smoke test failed; the system is running but not verified" }
}

Write-Host "`nInstalled. Sign-in details are in $root\.env (ADMIN_EMAIL / ADMIN_PASSWORD)." -ForegroundColor Green
Write-Host "Keep .env private and back up N8N_ENCRYPTION_KEY."
