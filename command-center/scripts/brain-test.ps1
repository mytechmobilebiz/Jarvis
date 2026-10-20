# The JARVIS Brain test copy: a second, separate Command Center on this PC for trying the JARVIS import before the
# live installation gets it (docs/JARVIS_BRAIN_SPEC.md 6.3). Run it from the NEW version's own folder (one with no
# .env), never from the live installation's folder.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\brain-test.ps1 -Action Start
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\brain-test.ps1 -Action Start -SeedFromBackup <folder of a backup.ps1 run>
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\brain-test.ps1 -Action Status
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\brain-test.ps1 -Action Stop
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\brain-test.ps1 -Action Remove
#
# The test copy is the Docker Compose project cc-brain-test: its own containers, database volume, network and images
# (infinite-command-center-brain-test/...), its own .env.brain-test with fresh secrets, the dashboard on
# http://127.0.0.1:8190, no n8n, and a worker without internet. Every Compose call is
#   docker compose -p cc-brain-test -f docker-compose.yml -f docker-compose.brain-test.yml --env-file .env.brain-test
# Before and after every action the live installation's fingerprint (brain-precheck.ps1) is printed, and the script
# fails loudly if it changed. Exit codes: 0 done; 1 failed; 2 not run (a check refused; nothing was changed).
param(
  [string]$Action = "",
  [string]$SeedFromBackup = ""
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$common = Join-Path $PSScriptRoot "brain-common.ps1"
if (-not (Test-Path -LiteralPath $common -PathType Leaf)) {
  Write-Host "NOT RUN: scripts\brain-common.ps1 is missing next to this script; extract the whole package again." -ForegroundColor Red
  Write-Host "Nothing was changed."
  exit 2
}
. $common

$actions = @("Start", "Status", "Stop", "Remove")
if ($actions -notcontains $Action) { Stop-Brain "give -Action Start, Status, Stop or Remove" }
$Action = $actions | Where-Object { $_ -eq $Action }
if ($SeedFromBackup -and $Action -ne "Start") { Stop-Brain "-SeedFromBackup goes with -Action Start only" }
$envFile = Join-Path $root ".env.brain-test"

function New-Secret([int]$bytes) {
  $buffer = New-Object byte[] $bytes
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buffer)
  return ([Convert]::ToBase64String($buffer)).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Write-TestEnv([string]$liveFolder) {
  # Fresh secrets, never the live installation's. From the live .env only the time zone and the build settings (a
  # registry mirror, a proxy's CA bundle: not secrets) are taken, so the test copy builds where the live one builds.
  $tz = "America/New_York"
  $build = @()
  if ($liveFolder) {
    $live = Read-EnvFile (Join-Path $liveFolder ".env")
    if ($live["TZ"] -and $live["TZ"] -match '^[A-Za-z_]+(/[A-Za-z0-9_+-]+)*$') { $tz = $live["TZ"] }
    $registry = [string]$live["REGISTRY"]
    if ($registry -and $registry -match '^[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._-]+)*$') { $build += "REGISTRY=$registry" }
    $ca = [string]$live["EXTRA_CA_BUNDLE"]
    if ($ca) {
      if (-not [IO.Path]::IsPathRooted($ca)) { $ca = Join-Path $liveFolder $ca }
      if (Test-Path -LiteralPath $ca -PathType Leaf) {
        # Copied into this folder: newer Docker builds refuse a build file outside the project folder.
        Copy-Item -LiteralPath $ca -Destination (Join-Path $root "brain-test-ca.pem") -Force
        $build += "EXTRA_CA_BUNDLE=./brain-test-ca.pem"
      }
    }
  }
  $stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
  $lines = @(
    "# JARVIS Brain test copy, written by scripts\brain-test.ps1 on $stamp. Fresh secrets, not the live installation's.",
    "# Private: never commit or share it. Delete it after brain-test.ps1 -Action Remove.",
    "ENVIRONMENT=production",
    "ADMIN_EMAIL=admin@brain-test.local",
    "ADMIN_PASSWORD=$(New-Secret 18)",
    "POSTGRES_PASSWORD=$(New-Secret 24)",
    "CC_DB_PASSWORD=$(New-Secret 24)",
    "N8N_DB_PASSWORD=$(New-Secret 24)",
    "CREDIT_REPAIR_DB_PASSWORD=$(New-Secret 24)",
    "CC_SERVICE_TOKEN=$(New-Secret 32)",
    "CC_SECRETS_KEY=$(New-Secret 32)",
    "CREDIT_REPAIR_DATA_KEY=$(New-Secret 32)",
    "N8N_WEBHOOK_SECRET=$(New-Secret 32)",
    "N8N_ENCRYPTION_KEY=$(New-Secret 32)",
    "N8N_OWNER_PASSWORD=$(New-Secret 18)A1",
    "COOKIE_SECURE=false",
    "TZ=$tz",
    "# The test copy's dashboard: http://127.0.0.1:<BRAIN_TEST_PORT>",
    "BRAIN_TEST_PORT=8190"
  ) + $build
  # UTF-8 without BOM and LF line endings, which Docker Compose reads cleanly. Never .env.
  [IO.File]::WriteAllText($envFile, (($lines -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding $false))
  Write-Host "Wrote $envFile (fresh secrets for the test copy)."
}

function Wait-Healthy([int]$port, [int]$seconds) {
  $deadline = (Get-Date).AddSeconds($seconds)
  while ((Get-Date) -lt $deadline) {
    try {
      $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/health" -UseBasicParsing -TimeoutSec 5
      if ($r.StatusCode -eq 200) { return $true }
    } catch { }
    Start-Sleep -Seconds 3
  }
  return $false
}

function Test-TestCopy([string[]]$compose, [int]$port, [hashtable]$testEnv) {
  # The running copy is this folder's version and is the test copy. Returns a list of problems.
  $problems = @()
  $expected = (Get-Content -LiteralPath (Join-Path $root "VERSION") -TotalCount 1).Trim()
  $r = Invoke-Docker ($compose + @("exec", "-T", "api", "python", "-c", "import app; print(app.__version__)"))
  $apiVersion = if ($r.Code -eq 0 -and $r.Out.Count -gt 0) { $r.Out[-1].Trim() } else { "(no answer)" }
  if ($apiVersion -ne $expected) { $problems += "the API reports $apiVersion, this folder is $expected" }
  try { $dashboardVersion = (Invoke-RestMethod "http://127.0.0.1:$port/version.json" -TimeoutSec 10).version } catch { $dashboardVersion = "(no answer)" }
  if ($dashboardVersion -ne $expected) { $problems += "the dashboard reports $dashboardVersion, this folder is $expected" }
  Write-Host "Versions: folder $expected, API $apiVersion, dashboard $dashboardVersion"
  # Signed in as the test copy's own admin: /api/system/status, the banner label and the cookie name.
  $web = New-Object Microsoft.PowerShell.Commands.WebRequestSession
  $body = @{ email = $testEnv["ADMIN_EMAIL"]; password = $testEnv["ADMIN_PASSWORD"] } | ConvertTo-Json
  $signedIn = $false
  try {
    $null = Invoke-RestMethod "http://127.0.0.1:$port/api/auth/login" -Method Post -ContentType "application/json" -Body $body -WebSession $web -TimeoutSec 20
    $signedIn = $true
  } catch {
    Write-Host "The test copy's own admin cannot sign in (after a seed, the copied accounts are the ones to use)." -ForegroundColor Yellow
  }
  if ($signedIn) {
    $names = @($web.Cookies.GetCookies((New-Object Uri "http://127.0.0.1:$port/")) | ForEach-Object { $_.Name })
    if ($names -notcontains $BrainTestCookie -or $names -contains "cc_session") {
      $problems += "the sign-in cookie is [$($names -join ', ')], expected $BrainTestCookie"
    }
    try {
      $status = Invoke-RestMethod "http://127.0.0.1:$port/api/system/status" -WebSession $web -TimeoutSec 20
      if ($status.version -ne $expected) { $problems += "/api/system/status reports $($status.version), this folder is $expected" }
      if ($status.instance_label -ne $BrainTestLabel) { $problems += "/api/system/status has instance_label '$($status.instance_label)'" }
      Write-Host "/api/system/status: version $($status.version), instance_label $($status.instance_label), cookie $($names -join ', ')"
    } catch {
      $problems += "/api/system/status did not answer ($($_.Exception.GetType().Name))"
    }
  }
  return $problems
}

function Invoke-Seed([string[]]$compose, [string]$folder) {
  # 1. postgres and db-init only; 2. a new, empty command_center database; 3. the backup restored into it;
  # 4. in one transaction, everything that could act is switched off. The application is started afterwards.
  $dump = Join-Path $folder "command_center.dump"
  Write-Host "Seeding the test copy from $dump"
  $null = Invoke-DockerLive ($compose + @("stop", "api", "worker", "dashboard"))
  if ((Invoke-DockerLive ($compose + @("up", "-d", "--wait", "postgres"))) -ne 0) { return "PostgreSQL did not start" }
  if ((Invoke-DockerLive ($compose + @("run", "--rm", "--no-deps", "-T", "db-init"))) -ne 0) { return "db-init failed" }
  $psql = @("exec", "-T", "postgres", "psql", "-U", "postgres", "-X", "-q", "-v", "ON_ERROR_STOP=1")
  $r = Invoke-Docker ($compose + $psql + @("-d", "postgres",
    "-c", "DROP DATABASE IF EXISTS command_center WITH (FORCE)",
    "-c", "CREATE DATABASE command_center OWNER command_center",
    "-c", "REVOKE ALL ON DATABASE command_center FROM PUBLIC"))
  if ($r.Code -ne 0) { return "could not recreate the command_center database: $($r.Err -join ' ')" }
  $r = Invoke-Docker ($compose + @("cp", $dump, "postgres:/tmp/brain-test-seed.dump"))
  if ($r.Code -ne 0) { return "could not copy the backup into the test database container" }
  $r = Invoke-Docker ($compose + @("exec", "-T", "postgres", "pg_restore", "-U", "postgres", "-d", "command_center",
    "--single-transaction", "--exit-on-error", "/tmp/brain-test-seed.dump"))
  $restored = $r.Code
  $why = ($r.Err | Select-Object -First 3) -join " "
  $null = Invoke-Docker ($compose + @("exec", "-T", "postgres", "rm", "-f", "/tmp/brain-test-seed.dump"))
  if ($restored -ne 0) { return "pg_restore failed: $why" }
  $sql = "BEGIN; " +
    "UPDATE schedules SET enabled = false, next_run_at = NULL, updated_at = now() WHERE enabled; " +
    "UPDATE approvals SET status = 'rejected', decided_by = 'test copy', decided_at = now(), decision_note = 'test copy' WHERE status = 'pending'; " +
    "UPDATE tasks SET status = 'cancelled', finished_at = now(), updated_at = now(), locked_by = '', error = 'cancelled: test copy' WHERE status IN ('queued', 'running', 'awaiting_approval'); " +
    "UPDATE supervisor_requests SET status = 'failed', error = 'test copy', updated_at = now() WHERE status IN ('planning', 'running', 'awaiting_approval'); " +
    "DELETE FROM sessions; " +
    "DELETE FROM secret_values; " +
    "UPDATE events SET forward_status = 'skipped' WHERE forward_status = 'pending'; " +
    "DELETE FROM app_settings WHERE key = 'research_mode'; " +
    "COMMIT;"
  $r = Invoke-Docker ($compose + $psql + @("-d", "command_center", "-c", $sql))
  if ($r.Code -ne 0) { return "the seeded data could not be switched off: $($r.Err -join ' ')" }
  $sql = "SELECT (SELECT count(*) FROM schedules WHERE enabled) + (SELECT count(*) FROM approvals WHERE status = 'pending') + " +
    "(SELECT count(*) FROM tasks WHERE status IN ('queued', 'running', 'awaiting_approval')) + (SELECT count(*) FROM sessions) + " +
    "(SELECT count(*) FROM secret_values) + (SELECT count(*) FROM events WHERE forward_status = 'pending') + " +
    "(SELECT count(*) FROM app_settings WHERE key = 'research_mode')"
  $r = Invoke-Docker ($compose + $psql + @("-d", "command_center", "-t", "-A", "-c", $sql))
  $left = (@($r.Out | Where-Object { $_.Trim() }) | Select-Object -Last 1)
  if ($r.Code -ne 0 -or "$left".Trim() -ne "0") { return "after the seed, something that could act is still on ($left)" }
  Write-Host "Seeded: schedules disabled, open tasks cancelled, pending approvals rejected, sessions and stored secrets deleted, pending events skipped, web research off." -ForegroundColor Green
  return ""
}

# --- Checks before anything runs ----------------------------------------------------------------------------------
Assert-Docker
$null = Assert-ComposeVersion
$liveFolder = Assert-NotLiveFolder $root
foreach ($needed in @("docker-compose.yml", "docker-compose.brain-test.yml", "VERSION", "scripts\prepare-build.ps1")) {
  if (-not (Test-Path -LiteralPath (Join-Path $root $needed))) { Stop-Brain "$needed is missing in $root; extract the whole package again" }
}
$seedFolder = ""
if ($SeedFromBackup) {
  $seedFolder = Get-FullPath $SeedFromBackup
  if (-not (Test-Path -LiteralPath (Join-Path $seedFolder "command_center.dump") -PathType Leaf)) {
    Stop-Brain "no command_center.dump in $seedFolder; give the folder of a backup.ps1 run"
  }
}
$before = @(Get-LiveFingerprint)
Show-Fingerprint "live installation before ($Action)" $before
$haveEnv = Test-Path -LiteralPath $envFile -PathType Leaf
if (-not $haveEnv -and $Action -ne "Start") { Stop-Brain "there is no test copy for this folder (.env.brain-test is missing); start one with -Action Start" }
$port = 8190
if ($haveEnv -and -not [int]::TryParse([string](Read-EnvFile $envFile)["BRAIN_TEST_PORT"], [ref]$port)) { $port = 0 }
if ($port -lt 1024 -or $port -gt 65535) { Stop-Brain "BRAIN_TEST_PORT in .env.brain-test must be a port number from 1024 to 65535" }
if ($Action -eq "Start" -and -not (Test-PortFree $port)) {
  # In use is fine only when it is this test copy's own dashboard.
  if (@(Get-ProjectContainers $BrainTestProject "dashboard" -Running).Count -eq 0) {
    Stop-Brain "port $port is in use by another program; set BRAIN_TEST_PORT in .env.brain-test to a free port (or close that program)"
  }
}
if (-not $haveEnv) { Write-TestEnv $liveFolder }
Assert-TestSecretsDiffer $envFile $liveFolder
$testEnv = Read-EnvFile $envFile
$compose = Get-TestCompose $root
$config = Get-TestConfig $compose
$problems = @(Get-TestConfigProblems $config $port)
if ($problems.Count -gt 0) { Stop-Brain ("the test copy's configuration is not isolated from the live installation: " + ($problems -join "; ")) }
Write-Host "Test copy configuration checked: project $BrainTestProject, own images, only 127.0.0.1:$port, worker on the private network only, no n8n."

# --- The action ---------------------------------------------------------------------------------------------------
$code = 0
try {
  switch ($Action) {
    "Start" {
      if ($seedFolder) {
        $volume = Invoke-Docker @("volume", "ls", "-q", "--filter", "label=com.docker.compose.project=$BrainTestProject", "--filter", "label=com.docker.compose.volume=pgdata")
        if (@($volume.Out | Where-Object { $_.Trim() }).Count -gt 0) {
          Confirm-Typed "REPLACE TEST DATA" "The test copy already has a database. Seeding replaces everything in it (imports and review decisions included)."
        }
      }
      $global:LASTEXITCODE = 1
      & (Join-Path $PSScriptRoot "prepare-build.ps1")
      if ($LASTEXITCODE -ne 0 -or -not $env:CC_RELEASE_VERSION) {
        Remove-Item Env:CC_RELEASE_VERSION -ErrorAction SilentlyContinue
        Write-Host "Nothing was started (see the message above)." -ForegroundColor Red
        $code = 1
        break
      }
      if ($seedFolder) {
        # Build first, so a failed build never leaves a half-seeded copy.
        $built = Invoke-DockerLive ($compose + @("build", "api", "dashboard"))
        if ($built -ne 0) { $code = 1; Write-Host "The build failed (see above); the test copy was not changed." -ForegroundColor Red; break }
        $failure = Invoke-Seed $compose $seedFolder
        if ($failure) { $code = 1; Write-Host "SEED FAILED: $failure. Run -Action Start -SeedFromBackup again, or -Action Remove." -ForegroundColor Red; break }
      }
      # --force-recreate: with Docker's containerd image store, a rebuilt image does not always make Compose replace
      # a stopped container, which would then keep running the previous code. The test copy's data is in volumes.
      $up = Invoke-DockerLive ($compose + @("up", "-d", "--build", "--force-recreate"))
      Remove-Item Env:CC_RELEASE_VERSION -ErrorAction SilentlyContinue
      if ($up -ne 0) { $code = 1; Write-Host "docker compose up failed (exit $up)." -ForegroundColor Red; break }
      Write-Host "Waiting for the test copy to answer..."
      if (-not (Wait-Healthy $port 240)) {
        $code = 1
        Write-Host "The test copy did not answer on http://127.0.0.1:$port within 240 s." -ForegroundColor Red
        break
      }
      $found = @(Test-TestCopy $compose $port $testEnv)
      if ($found.Count -gt 0) {
        $code = 1
        foreach ($p in $found) { Write-Host "CHECK FAILED: $p" -ForegroundColor Red }
        break
      }
      Write-Host ""
      Write-Host "Test copy running: http://127.0.0.1:$port" -ForegroundColor Cyan
      if ($seedFolder) {
        Write-Host "Sign in with the accounts of the copied installation (their passwords are the live ones)." -ForegroundColor Cyan
      } else {
        Write-Host "Sign in: $($testEnv['ADMIN_EMAIL']), password: ADMIN_PASSWORD in $envFile" -ForegroundColor Cyan
      }
      Write-Host "The dashboard labels every page '$BrainTestLabel'. The live installation stays at its own address."
    }
    "Status" {
      $null = Invoke-DockerLive ($compose + @("ps", "--all"))
      if (Wait-Healthy $port 5) {
        $found = @(Test-TestCopy $compose $port $testEnv)
        foreach ($p in $found) { Write-Host "CHECK FAILED: $p" -ForegroundColor Red }
        if ($found.Count -gt 0) { $code = 1 } else { Write-Host "Test copy running: http://127.0.0.1:$port" -ForegroundColor Cyan }
      } else {
        Write-Host "The test copy is not answering on http://127.0.0.1:$port (stopped?)." -ForegroundColor Yellow
      }
    }
    "Stop" {
      $code = Invoke-DockerLive ($compose + @("stop"))
      if ($code -eq 0) { Write-Host "Test copy stopped; its data is kept. Start it again with -Action Start." -ForegroundColor Green }
    }
    "Remove" {
      Confirm-Typed "REMOVE TEST COPY" "This deletes the test copy: its containers, its database (imports and review decisions included) and its images. The live installation is not touched."
      # "down --volumes" misses an anonymous volume that Compose handed on to a recreated container (db-init's empty
      # one, in a copy started before 0.7.0; Start always recreates), and it carries no project label: named first.
      $anonymous = @(Get-AnonymousVolumes $BrainTestProject)
      $code = Invoke-DockerLive ($compose + @("down", "--volumes", "--remove-orphans"))
      if ($code -eq 0) {
        $present = @((Invoke-Docker @("volume", "ls", "-q")).Out | ForEach-Object { $_.Trim() })
        foreach ($volume in @($anonymous | Where-Object { $present -contains $_ })) {
          $r = Invoke-Docker @("volume", "rm", $volume)
          if ($r.Code -eq 0) { Write-Host "removed volume $volume (anonymous, of the test copy's containers)" }
        }
        foreach ($image in $BrainTestImages) {
          $r = Invoke-Docker @("image", "inspect", $image)
          if ($r.Code -eq 0) {
            $r = Invoke-Docker @("image", "rm", $image)
            if ($r.Code -eq 0) { Write-Host "removed image $image" } else { Write-Host "could not remove image $image" -ForegroundColor Yellow; $code = 1 }
          }
        }
        $left = @(Get-ProjectContainers $BrainTestProject "")
        $volumes = Invoke-Docker @("volume", "ls", "-q", "--filter", "label=com.docker.compose.project=$BrainTestProject")
        $leftVolumes = @($volumes.Out | Where-Object { $_.Trim() })
        $present = @((Invoke-Docker @("volume", "ls", "-q")).Out | ForEach-Object { $_.Trim() })
        $leftVolumes += @($anonymous | Where-Object { $present -contains $_ })
        if ($left.Count -gt 0 -or $leftVolumes.Count -gt 0) {
          Write-Host "Left behind: $($left.Count) container(s), $($leftVolumes.Count) volume(s) of $BrainTestProject" -ForegroundColor Yellow
          $code = 1
        } else {
          Write-Host "Test copy removed." -ForegroundColor Green
          Write-Host "Delete $envFile, and the restored JARVIS files (C:\InfiniteAI\JarvisMigration\restored), when you no longer need them."
        }
      }
    }
  }
} catch {
  $code = 1
  Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red
}

# Only for this run's build: a version left in an interactive window would be checked against another folder's build.
Remove-Item Env:CC_RELEASE_VERSION -ErrorAction SilentlyContinue
$after = @(Get-LiveFingerprint)
Show-Fingerprint "live installation after ($Action)" $after
Assert-SameFingerprint $before $after
exit $code
