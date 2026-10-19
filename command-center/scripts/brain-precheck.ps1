# Read-only pre-check of the live Command Center before (and after) the JARVIS Brain test (spec 6.4).
# It changes nothing in the live installation: it only reads Docker's lists and labels, runs two read-only commands
# in the running containers (the API's version, and counts from the database in a read-only transaction), and
# writes its report to a new file outside the live folder.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\brain-precheck.ps1 [-OutDir C:\InfiniteAI\JarvisMigration]
#
# Run it before the test and again after it: the two reports must show the same fingerprint and counts.
# Exit codes: 0 report written and the live installation answers; 1 report written, but something is not healthy;
# 2 not run (nothing was changed or written).
param(
  [string]$OutDir = "C:\InfiniteAI\JarvisMigration"
)
$ErrorActionPreference = "Stop"
$common = Join-Path $PSScriptRoot "brain-common.ps1"
if (-not (Test-Path -LiteralPath $common -PathType Leaf)) {
  Write-Host "NOT RUN: scripts\brain-common.ps1 is missing next to this script; extract the whole package again." -ForegroundColor Red
  Write-Host "Nothing was changed."
  exit 2
}
. $common

# --- Checks before anything runs -----------------------------------------------------------------------------------
Assert-Docker
$OutDir = Get-FullPath $OutDir
$liveFolder = Get-LiveFolder
if ($liveFolder -and (Test-Inside $OutDir $liveFolder)) {
  Stop-Brain "the report folder $OutDir is inside the live installation's folder; choose a folder outside it"
}

# --- Read ----------------------------------------------------------------------------------------------------------
$report = New-Object System.Collections.Generic.List[string]
$problems = 0
function Add-Line([string]$text) { $report.Add($text); Write-Host $text }
function Add-Problem([string]$text) { $script:problems++; $report.Add("PROBLEM: $text"); Write-Host "PROBLEM: $text" -ForegroundColor Red }

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
Add-Line "JARVIS Brain pre-check of the live installation (project $BrainLiveProject), $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz'))"
Add-Line "Read-only: nothing in the live installation was changed by this check."
Add-Line ""

$fingerprint = @(Get-LiveFingerprint)
Add-Line "[fingerprint]"
foreach ($line in $fingerprint) { Add-Line "  $line" }
if ($fingerprint.Count -eq 0) { Add-Line "  (nothing)" }
Add-Line ""

Add-Line "[live folder]"
if ($liveFolder) {
  Add-Line "  folder: $liveFolder"
  $versionFile = Join-Path $liveFolder "VERSION"
  if (Test-Path -LiteralPath $versionFile -PathType Leaf) {
    Add-Line ("  VERSION file: " + (Get-Content -LiteralPath $versionFile -TotalCount 1).Trim())
  } else {
    Add-Line "  VERSION file: not readable from here"
  }
} else {
  Add-Problem "the live installation's api container is not running, so its folder is unknown"
}
Add-Line ""

Add-Line "[containers]"
$containers = @()
foreach ($id in @(Get-ProjectContainers $BrainLiveProject "")) {
  $info = Get-ContainerInfo $id
  if ($info) { $containers += $info }
}
foreach ($c in ($containers | Sort-Object Name)) {
  $health = if ($c.Health) { $c.Health } else { "no healthcheck" }
  Add-Line ("  {0}: {1}, {2}" -f $c.Name, $c.Status, $health)
}
if ($containers.Count -eq 0) { Add-Problem "no container of $BrainLiveProject exists" }
function Find-Service([string]$service) {
  return ($containers | Where-Object { (Get-Label $_ "com.docker.compose.service") -eq $service -and
    (Get-Label $_ "com.docker.compose.oneoff") -ne "True" } | Select-Object -First 1)
}
foreach ($service in @("postgres", "n8n", "api", "dashboard")) {
  $c = Find-Service $service
  if (-not $c) { Add-Problem "no $service container"; continue }
  if ($c.Status -ne "running") { Add-Problem "$service is $($c.Status)" }
  elseif ($c.Health -and $c.Health -ne "healthy") { Add-Problem "$service healthcheck: $($c.Health)" }
  elseif ($service -eq "postgres" -or $service -eq "n8n") { Add-Line "  $service healthcheck passes" }
}
$worker = Find-Service "worker"
if (-not $worker -or $worker.Status -ne "running") { Add-Problem "the worker is not running" }
Add-Line ""

Add-Line "[application]"
$api = Find-Service "api"
if ($api -and $api.Status -eq "running") {
  $r = Invoke-Docker @("exec", $api.Id, "python", "-c", "import app; print(app.__version__)")
  if ($r.Code -eq 0 -and $r.Out.Count -gt 0) { Add-Line ("  API version: " + $r.Out[-1].Trim()) } else { Add-Problem "the API did not report its version" }
}
$pg = Find-Service "postgres"
if ($pg -and $pg.Status -eq "running") {
  # A read-only transaction: the database itself refuses any change from this session.
  $sql = "SELECT (SELECT string_agg(version, ',' ORDER BY version) FROM schema_migrations), " +
    "(SELECT count(*) FROM agent_state WHERE NOT enabled), " +
    "(SELECT count(*) FROM schedules WHERE enabled), (SELECT count(*) FROM schedules), " +
    "(SELECT count(*) FROM sites), (SELECT count(*) FROM approvals WHERE status = 'pending'), (SELECT count(*) FROM users)"
  $r = Invoke-Docker @("exec", "-e", "PGOPTIONS=-c default_transaction_read_only=on", $pg.Id, "psql", "-U", "postgres",
    "-d", "command_center", "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-c", $sql)
  $row = @($r.Out | Where-Object { $_ -match '\|' } | Select-Object -Last 1)
  if ($r.Code -eq 0 -and $row.Count -eq 1) {
    $v = $row[0].Trim().Split('|')
    $migrations = $v[0].Split(',')
    Add-Line ("  schema_migrations: " + ($migrations -join ' '))
    $brain = if ($migrations -contains "0006_brain") { "present" } else { "not present (the live installation is older than 0.6.0)" }
    Add-Line "  0006_brain: $brain"
    Add-Line "  agents switched off: $($v[1]) (agents without a saved state are on)"
    Add-Line "  schedules enabled: $($v[2]) of $($v[3])"
    Add-Line "  sites: $($v[4])"
    Add-Line "  pending approvals: $($v[5])"
    Add-Line "  users: $($v[6])"
  } else {
    Add-Problem ("the database counts could not be read: " + (($r.Err | Select-Object -First 2) -join " "))
  }
}
Add-Line ""
$verdict = if ($problems -eq 0) { "RESULT: the live installation answers; nothing was changed." } else { "RESULT: $problems problem(s) above; nothing was changed." }
Add-Line $verdict

# --- Write the report (the only file this script writes) ------------------------------------------------------------
if (-not (Test-Path -LiteralPath $OutDir)) { $null = New-Item -ItemType Directory -Path $OutDir }
$file = Join-Path $OutDir "precheck-$stamp.txt"
[IO.File]::WriteAllText($file, (($report -join "`r`n") + "`r`n"), (New-Object System.Text.ASCIIEncoding))
Write-Host ""
Write-Host "Report written: $file" -ForegroundColor Green
if ($problems -eq 0) { exit 0 } else { exit 1 }
