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

# A development build (VERSION ends in -dev) is for the sandbox's tests only, never for a PC.
$version = if (Test-Path (Join-Path $root "VERSION")) { (Get-Content (Join-Path $root "VERSION") -TotalCount 1).Trim() } else { "" }
if ($version -match '-dev$' -and $env:CC_ALLOW_DEV_BUILD -ne "1") {
  Fail "this folder holds version $version, a development build, not for installation. Nothing was changed"
}
# Before each script below: a script that cannot run at all (a parse error, a missing file) sets no exit code
# of its own, so this 1 stays and counts as a failure. Each script ends with exit 0 when it succeeds.

Step "1/5 Checking prerequisites"
$global:LASTEXITCODE = 1
& (Join-Path $scripts "preflight.ps1")
if ($LASTEXITCODE -ne 0) { Fail "fix the preflight problems above, then run install.ps1 again" }

Step "2/5 Configuration"
if (Test-Path (Join-Path $root ".env")) {
  Write-Host "Keeping the existing .env (keys are added only to a .env from before 0.2.0; existing values are never changed)."
  $global:LASTEXITCODE = 1
  & (Join-Path $scripts "init-env.ps1") -AddMissing
  if ($LASTEXITCODE -ne 0) { Fail "could not update .env" }
} else {
  # Data of an earlier installation is still on this PC: a new .env would bring new passwords and encryption keys that
  # do not fit it (the services would not start, and stored secrets could not be read). Its own .env must come along.
  $existing = (docker volume ls -q --filter "name=infinite-command-center_pgdata" 2>$null | Out-String).Trim()
  if ($existing) {
    Fail ("this folder has no .env, but this PC already has Command Center data (Docker volume $existing). Copy the .env " +
          "of your current installation into $root first: it holds the passwords and encryption keys of that data. Nothing was changed")
  }
  if (-not $AdminEmail) { $AdminEmail = Read-Host "Email address for the first admin account" }
  if ($AdminEmail -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$') { Fail "'$AdminEmail' is not an email address" }
  $global:LASTEXITCODE = 1
  & (Join-Path $scripts "init-env.ps1") -AdminEmail $AdminEmail
  if ($LASTEXITCODE -ne 0) { Fail "could not write .env" }
}
. (Join-Path $scripts "compose-env.ps1")
$null = Use-ComposeEnv $root
docker compose config --quiet
if ($LASTEXITCODE -ne 0) { Fail "docker compose rejected the configuration (see the message above)" }

Step "3/5 Building and validating the application image"
$global:LASTEXITCODE = 1
& (Join-Path $scripts "prepare-build.ps1")
if ($LASTEXITCODE -ne 0 -or -not $env:CC_RELEASE_VERSION) { Fail "could not prepare the build folders (see the message above); nothing was built or changed" }
docker compose build
$built = $LASTEXITCODE
Remove-Item Env:CC_RELEASE_VERSION -ErrorAction SilentlyContinue
if ($built -ne 0) {
  Fail ("the image build failed (see the messages above); the running system and its data were not changed. If a message says " +
        "'out-of-date copy', run install.ps1 again; otherwise check the internet connection and Docker Desktop")
}
docker compose run --rm --no-deps api python -m app.cli check-config
if ($LASTEXITCODE -ne 0) { Fail "configuration validation failed; correct .env and re-run" }

Step "4/5 Starting (first start initialises the databases)"
$global:LASTEXITCODE = 1
& (Join-Path $scripts "start.ps1") -NoBuild
if ($LASTEXITCODE -ne 0) { Fail "the services did not start; see: docker compose logs --tail=100 api worker n8n" }
docker compose exec -T api python -m app.cli db-status
if ($LASTEXITCODE -ne 0) { Fail "the database was not initialised" }

if ($SkipSmokeTest) {
  Step "5/5 Smoke test skipped"
} else {
  Step "5/5 Smoke test"
  $global:LASTEXITCODE = 1
  & (Join-Path $scripts "smoke.ps1")
  if ($LASTEXITCODE -ne 0) { Fail "the smoke test failed; the system is running but not verified" }
}

Write-Host "`nInstalled. Sign-in details are in $(Join-Path $root ".env") (ADMIN_EMAIL / ADMIN_PASSWORD)." -ForegroundColor Green
Write-Host "Keep .env private and back up N8N_ENCRYPTION_KEY."
Write-Host "If the dashboard was open in a browser during the update, reload the page: it still runs the previous version's code." -ForegroundColor Yellow
