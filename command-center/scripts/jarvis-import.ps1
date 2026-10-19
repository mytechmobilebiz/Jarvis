# Imports JARVIS's knowledge and history into the Command Center's memory (docs/JARVIS_BRAIN_SPEC.md 6.6), first into
# the test copy (brain-test.ps1), later into the live installation. The import itself is "python -m app.cli
# brain-import" from the image the running api uses, in a one-off container on the private network only, with the
# JARVIS database backup and the restored profile mounted read-only (docker-compose.brain-import.yml).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target Test -Mode Plan -All `
#     -BackupDb C:\InfiniteAI\JarvisMigration\backups\jarvis-state-<time>.db -Restored C:\InfiniteAI\JarvisMigration\restored
#   ... -Mode Import -Sample 5        then review, then -Sample <about 10 % of the sessions>, then review, then -All
#   ... -Mode Verify -Run <id>        -Mode Status [-Run <id>]        -Mode Rollback -Run <id> [-DiscardReviewed]
#   ... -Mode ExportDecisions (test copy)     -Mode ApplyDecisions -Decisions <file> -Run <id> (live installation)
#   ... -Mode Resume -Run <id>        -Mode Rescan
#
# -Target Test uses the test copy of THIS folder (project cc-brain-test, .env.brain-test). -Target Production must be
# run from the live installation's folder; it asks for typed confirmations and, before an import, writes a verified
# dump of the command_center database. The JARVIS files are only read; nothing is created next to them. Every path
# reaches Docker Compose through an environment variable (BRAIN_IMPORT_*), never inside a -v string.
# Exit codes: 0 done; 1 failed or verification failed; 2 not run (refused before any change).
param(
  [string]$Target = "",
  [string]$Mode = "",
  [string]$BackupDb = "",
  [string]$Restored = "",
  [int]$Sample = 0,
  [int]$FilesSample = 0,
  [switch]$All,
  [switch]$NoFiles,
  [string]$Since = "",
  [string]$Until = "",
  [int]$Run = 0,
  [string]$Discovery = "",
  [string]$Reports = "C:\InfiniteAI\JarvisMigration\import-reports",
  [switch]$DiscardReviewed,
  [string]$Decisions = "",
  [string]$Label = "",
  [string]$Actor = ""
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

$onWindows = $true
if ($PSVersionTable.PSEdition -eq "Core" -and -not $IsWindows) { $onWindows = $false }

# --- Parameters ---------------------------------------------------------------------------------------------------
$targets = @("Test", "Production")
$modes = @("Plan", "Import", "Resume", "Verify", "Rollback", "Status", "ExportDecisions", "ApplyDecisions", "Rescan")
if ($targets -notcontains $Target) { Stop-Brain "give -Target Test or -Target Production" }
if ($modes -notcontains $Mode) { Stop-Brain ("give -Mode " + ($modes -join ", ")) }
$Target = $targets | Where-Object { $_ -eq $Target }
$Mode = $modes | Where-Object { $_ -eq $Mode }
$readsSource = @("Plan", "Import", "Resume", "Verify") -contains $Mode
if (@("Resume", "Verify", "Rollback", "ApplyDecisions") -contains $Mode -and $Run -le 0) { Stop-Brain "-Mode $Mode needs -Run <import run number> (see -Mode Status)" }
if ($Run -lt 0) { Stop-Brain "-Run must be a positive number" }
foreach ($date in @($Since, $Until)) {
  if ($date -and $date -notmatch '^\d{4}-\d{2}-\d{2}$') { Stop-Brain "dates are written as YYYY-MM-DD (got '$date')" }
}
if ($Label -and $Label -notmatch '^[A-Za-z0-9 ._:/-]{1,80}$') { Stop-Brain "-Label may hold letters, digits, spaces and . _ : / - (at most 80)" }
if ($Actor -and $Actor -notmatch '^[A-Za-z0-9._%+@-]{1,200}$') { Stop-Brain "-Actor is the email address of a Command Center admin" }
$selection = @()
if ($Mode -eq "Plan" -or $Mode -eq "Import") {
  $kinds = 0
  if ($Sample -gt 0) { $kinds++; $selection += @("--sample", "$Sample") }
  if ($All) { $kinds++; $selection += "--all" }
  if ($Since -or $Until) {
    $kinds++
    if ($Since) { $selection += @("--since", $Since) }
    if ($Until) { $selection += @("--until", $Until) }
  }
  if ($kinds -ne 1) { Stop-Brain "choose what to import with exactly one of -Sample N, -All, or -Since/-Until" }
  if ($FilesSample -gt 0) {
    if ($Sample -le 0) { Stop-Brain "-FilesSample goes with -Sample" }
    $selection += @("--files-sample", "$FilesSample")
  }
  if ($NoFiles) { $selection += "--no-files" }
} elseif ($Sample -or $FilesSample -or $All -or $Since -or $Until -or $NoFiles) {
  Stop-Brain "-Sample, -FilesSample, -All, -Since, -Until and -NoFiles go with -Mode Plan or Import"
}
if ($Mode -eq "ApplyDecisions" -and -not $Decisions) { Stop-Brain "-Mode ApplyDecisions needs -Decisions <decisions file exported from the test copy>" }

# --- Checks before anything runs ----------------------------------------------------------------------------------
Assert-Docker
$null = Assert-ComposeVersion
foreach ($needed in @("docker-compose.yml", "docker-compose.brain-import.yml", "docker-compose.brain-test.yml")) {
  if (-not (Test-Path -LiteralPath (Join-Path $root $needed) -PathType Leaf)) { Stop-Brain "$needed is missing in $root; extract the whole package again" }
}
$hermes = ""
if ($env:LOCALAPPDATA) { $hermes = Get-FullPath (Join-Path $env:LOCALAPPDATA "hermes") }
$Reports = Get-FullPath $Reports
if ($hermes -and (Test-Inside $Reports $hermes)) { Stop-Brain "the reports folder $Reports is inside JARVIS's folders ($hermes); choose a folder outside them" }

$profileDir = ""
$contentDir = ""
if ($readsSource) {
  if (-not $BackupDb) { Stop-Brain "-Mode $Mode needs -BackupDb <the JARVIS database backup (.db) outside JARVIS's folders>" }
  if (-not $Restored) { Stop-Brain "-Mode $Mode needs -Restored <the folder jarvis-kit.ps1 -Action VerifyBackup -ExtractTo restored>" }
  $BackupDb = Get-FullPath $BackupDb
  $Restored = Get-FullPath $Restored
  if (-not (Test-Path -LiteralPath $BackupDb -PathType Leaf)) { Stop-Brain "database backup not found: $BackupDb" }
  if (-not (Test-Path -LiteralPath $Restored -PathType Container)) { Stop-Brain "folder not found: $Restored" }
  $profileDir = Join-Path $Restored "profile"
  if (-not (Test-Path -LiteralPath $profileDir -PathType Container)) { Stop-Brain "folder not found: $profileDir (-Restored must hold profile\ and, if it was backed up, content\)" }
  $contentCandidate = Join-Path $Restored "content"
  if (Test-Path -LiteralPath $contentCandidate -PathType Container) { $contentDir = $contentCandidate }
  if ($hermes) {
    foreach ($path in @($BackupDb, $Restored, $profileDir, $contentDir)) {
      if ($path -and (Test-Inside $path $hermes)) {
        Stop-Brain "$path is inside JARVIS's own folders ($hermes); use the verified backup copy and the restored folder outside them"
      }
    }
  }
  foreach ($suffix in @("-wal", "-journal")) {
    $side = $BackupDb + $suffix
    if ((Test-Path -LiteralPath $side -PathType Leaf) -and (Get-Item -LiteralPath $side).Length -gt 0) {
      Stop-Brain "$side is not empty: the backup was copied while JARVIS was writing. Make the backup again with JARVIS closed"
    }
  }
  if ($Discovery) {
    $Discovery = Get-FullPath $Discovery
    if (-not (Test-Path -LiteralPath $Discovery -PathType Leaf)) { Stop-Brain "discovery summary not found: $Discovery" }
  }
  # Free space: three times the database, on the system drive and on the drive holding Docker's data.
  $need = 3 * (Get-Item -LiteralPath $BackupDb).Length
  $places = @()
  if ($onWindows) {
    if ($env:SystemDrive) { $places += ($env:SystemDrive + "\") }
    $dockerData = ""
    foreach ($name in @("settings-store.json", "settings.json")) {
      if ($dockerData -or -not $env:APPDATA) { continue }
      $file = Join-Path (Join-Path $env:APPDATA "Docker") $name
      if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { continue }
      try { $settings = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json } catch { continue }
      foreach ($p in $settings.PSObject.Properties) {
        if ($p.Name -match '^(customWslDistroDir|dataFolder|diskImageLocation)$' -and [string]$p.Value) { $dockerData = [string]$p.Value; break }
      }
    }
    if (-not $dockerData -and $env:LOCALAPPDATA) { $dockerData = Join-Path $env:LOCALAPPDATA "Docker" }
    if ($dockerData) { $places += $dockerData }
  } else {
    $places += "/"
    $r = Invoke-Docker @("info", "--format", "{{.DockerRootDir}}")
    if ($r.Code -eq 0 -and $r.Out.Count -gt 0 -and (Test-Path -LiteralPath $r.Out[0].Trim())) { $places += $r.Out[0].Trim() }
  }
  foreach ($place in $places) {
    try {
      $drive = New-Object IO.DriveInfo ([IO.Path]::GetPathRoot((Get-FullPath $place)))
      $free = $drive.AvailableFreeSpace
    } catch {
      Write-Host "Free space on the drive of $place could not be read." -ForegroundColor Yellow
      continue
    }
    if ($free -lt $need) {
      Stop-Brain ("only {0:N1} GB free on {1} (for {2}); the import needs three times the database: {3:N1} GB" -f ($free / 1GB), $drive.Name, $place, ($need / 1GB))
    }
  }
}
if ($Mode -eq "ApplyDecisions") {
  $Decisions = Get-FullPath $Decisions
  if (-not (Test-Path -LiteralPath $Decisions -PathType Leaf)) { Stop-Brain "decisions file not found: $Decisions" }
}

# The installation the import goes into, and the image its api runs.
if ($Target -eq "Test") {
  $null = Assert-NotLiveFolder $root
  $liveFolder = Get-LiveFolder
  $envFile = Join-Path $root ".env.brain-test"
  if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) { Stop-Brain "this folder has no test copy (.env.brain-test); start one with .\scripts\brain-test.ps1 -Action Start" }
  Assert-TestSecretsDiffer $envFile $liveFolder
  $testEnv = Read-EnvFile $envFile
  $port = 0
  if (-not [int]::TryParse([string]$testEnv["BRAIN_TEST_PORT"], [ref]$port)) { Stop-Brain "BRAIN_TEST_PORT in .env.brain-test is not a port number" }
  $problems = @(Get-TestConfigProblems (Get-TestConfig (Get-TestCompose $root)) $port)
  if ($problems.Count -gt 0) { Stop-Brain ("the test copy's configuration is not isolated from the live installation: " + ($problems -join "; ")) }
  $project = $BrainTestProject
  $compose = Get-TestCompose $root -Import
} else {
  $liveFolder = Get-LiveFolder
  if (-not $liveFolder) { Stop-Brain "the live installation is not running; start it first (.\scripts\start.ps1 -NoBuild in its folder)" }
  if (-not (Test-SameFolder $liveFolder $root)) {
    Stop-Brain "-Target Production runs from the live installation's folder ($liveFolder), not from $root"
  }
  $envFile = Join-Path $root ".env"
  if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) { Stop-Brain "the live installation's .env was not found in $root" }
  $project = $BrainLiveProject
  $compose = @("compose", "-p", $BrainLiveProject, "-f", (Join-Path $root "docker-compose.yml"),
    "-f", (Join-Path $root "docker-compose.brain-import.yml"), "--env-file", $envFile)
}
$apiIds = @(Get-ProjectContainers $project "api" -Running)
if ($apiIds.Count -eq 0) { Stop-Brain "the api of $project is not running; start it first" }
$api = Get-ContainerInfo $apiIds[0]
if (-not $api -or -not $api.Image.StartsWith("sha256:")) { Stop-Brain "could not read the image of the running api of $project" }
$pgIds = @(Get-ProjectContainers $project "postgres" -Running)
if ($pgIds.Count -eq 0) { Stop-Brain "the database of $project is not running" }

# The paths and the image go to Compose as environment variables only. Old values from an earlier run are cleared.
$importVars = @("BRAIN_IMPORT_IMAGE", "BRAIN_IMPORT_DB", "BRAIN_IMPORT_PROFILE", "BRAIN_IMPORT_CONTENT", "BRAIN_IMPORT_REPORTS")
foreach ($name in $importVars) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
function Clear-ImportVars { foreach ($name in $importVars) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue } }
# The importer runs the code the running api runs. Compose is given the image NAME the api was created from (with
# Docker's containerd image store an image cannot be run by its ID), after checking that this name still points to
# the exact image the api is running: if it was rebuilt since, the importer would run other code.
$imageRef = [string]$api.ImageRef
if (-not $imageRef -or $imageRef.StartsWith("sha256:")) { Stop-Brain "could not read the image name of the running api of $project" }
$refId = ""
$r = Invoke-Docker @("image", "inspect", "--format", "{{.Id}}", $imageRef)
if ($r.Code -eq 0 -and $r.Out.Count -gt 0) { $refId = $r.Out[0].Trim() }
if ($refId -ne $api.Image) {
  Stop-Brain ("the image $imageRef was rebuilt after the api of $project started (the api runs " + $api.Image.Substring(0, 19) +
    "); restart that installation first, so the importer runs the same code as the api")
}
$env:BRAIN_IMPORT_IMAGE = $imageRef
$reportsExisted = Test-Path -LiteralPath $Reports
if ($reportsExisted -and -not (Test-Path -LiteralPath $Reports -PathType Container)) { Clear-ImportVars; Stop-Brain "$Reports is a file, not a folder" }
# Compose must find the reports folder (it never creates a missing one); checks below still run before anything else.
$env:BRAIN_IMPORT_REPORTS = $Reports
$service = "brain-importer-nosource"
if ($readsSource) {
  $service = "brain-importer"
  $env:BRAIN_IMPORT_DB = $BackupDb
  $env:BRAIN_IMPORT_PROFILE = $profileDir
  if ($contentDir) { $env:BRAIN_IMPORT_CONTENT = $contentDir }
}

function Get-ImportConfigProblems($config) {
  # The importer container as Compose will create it (spec 6.5).
  $problems = @()
  if ([string]$config.name -ne $project) { $problems += "the project is '$($config.name)', not $project" }
  $svc = $null
  if ($config.services -and $config.services.PSObject.Properties[$service]) { $svc = $config.services.$service }
  if (-not $svc) { return @("the service $service is missing") }
  if ([string]$svc.image -ne $imageRef) { $problems += "its image is '$($svc.image)', not the running api's $imageRef" }
  $nets = @(); if ($svc.networks) { $nets = @($svc.networks.PSObject.Properties | ForEach-Object { $_.Name }) }
  if ($nets.Count -ne 1 -or $nets[0] -ne "internal") { $problems += "its networks are [$($nets -join ', ')]" }
  if ($svc.read_only -ne $true) { $problems += "its file system is not read-only" }
  if (@($svc.cap_drop) -notcontains "ALL") { $problems += "it keeps Linux capabilities" }
  if (@($svc.security_opt) -notcontains "no-new-privileges:true") { $problems += "no-new-privileges is not set" }
  if ([string]$svc.restart -ne "no") { $problems += "it restarts by itself" }
  $keys = @(); if ($svc.environment) { $keys = @($svc.environment.PSObject.Properties | ForEach-Object { $_.Name } | Sort-Object) }
  if (($keys -join ",") -ne "DATABASE_URL,ENVIRONMENT,TZ") { $problems += "its environment is [$($keys -join ', ')]" }
  if (-not $svc.labels -or [string]$svc.labels.PSObject.Properties["cc.brain-import"].Value -ne "1") { $problems += "the label cc.brain-import=1 is missing" }
  $writable = @()
  foreach ($v in @($svc.volumes)) {
    if ($null -eq $v) { continue }
    if ($v.type -ne "bind" -or -not $v.bind -or $v.bind.create_host_path -ne $false) { $problems += "the mount $($v.target) could create a missing host folder" }
    if ($v.read_only -ne $true) { $writable += [string]$v.target }
  }
  if (($writable -join ",") -ne "/reports") { $problems += "writable mounts are [$($writable -join ', ')], expected only /reports" }
  return $problems
}

function Get-ImportConfig {
  $r = Invoke-Docker ($compose + @("--profile", "brain-import", "config", "--format", "json"))
  if ($r.Code -ne 0) { Clear-ImportVars; Stop-Brain ("docker compose rejected the importer configuration: " + (($r.Err | Select-Object -First 2) -join " ")) }
  $text = $r.Out -join "`n"
  try { return ($text.Substring($text.IndexOf('{')) | ConvertFrom-Json) } catch { Clear-ImportVars; Stop-Brain "could not read the importer configuration" }
}

$problems = @(Get-ImportConfigProblems (Get-ImportConfig))
if ($problems.Count -gt 0) { Clear-ImportVars; Stop-Brain ("the importer container is not as hardened as it must be: " + ($problems -join "; ")) }

function Get-AppliedMigrations {
  $r = Invoke-Docker @("exec", "-e", "PGOPTIONS=-c default_transaction_read_only=on", $pgIds[0], "psql", "-U", "postgres", "-d", "command_center",
    "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-c", "SELECT version FROM schema_migrations ORDER BY version")
  if ($r.Code -ne 0) { return @() }
  return @($r.Out | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Invoke-Importer([string[]]$cli, [switch]$Capture) {
  $arguments = $compose + @("--profile", "brain-import", "run", "--rm", "--no-deps", "-T", $service) + $cli
  if ($Capture) { return Invoke-Docker $arguments }
  return Invoke-DockerLive $arguments
}

function Get-InReports([string]$path, [string]$prefix) {
  # The container path (/reports/...) of a file: as it is when inside the reports folder, else a copy made there.
  if (-not (Test-Inside $path $Reports)) {
    $copy = Join-Path $Reports ("{0}-{1}{2}" -f $prefix, $stamp, [IO.Path]::GetExtension($path))
    Copy-Item -LiteralPath $path -Destination $copy
    $path = $copy
  }
  return "/reports/" + $path.Substring($Reports.Length).TrimStart('\', '/').Replace('\', '/')
}

# From here on containers may be created (a one-off importer at most): the live fingerprint brackets all of it.
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$before = @(Get-LiveFingerprint)
Show-Fingerprint "live installation before ($Target $Mode)" $before

if ($Target -eq "Production") {
  # Feature check: the running api's image has the importer and the database has the JARVIS Brain tables.
  $migrations = @(Get-AppliedMigrations)
  if ($migrations -notcontains "0006_brain") { Clear-ImportVars; Stop-Brain "the live database has no 0006_brain migration: install 0.6.0 on the live installation first" }
  if (-not (Test-Path -LiteralPath $Reports)) { $null = New-Item -ItemType Directory -Path $Reports }
  $saved = $service
  $service = "brain-importer-nosource"
  $r = Invoke-Importer @("status") -Capture
  $service = $saved
  $printed = ($r.Out -join "`n")
  if ($r.Code -ne 0 -or $printed -notmatch '(?i)importer[_ ]version\W{0,4}(\d+\.\d+\.\d+)') {
    Clear-ImportVars
    Stop-Brain "the running api's image has no working brain-import (status exit $($r.Code)); install 0.6.0 on the live installation first"
  }
  Write-Host "Live installation: importer $($Matches[1]), migration 0006_brain present, importer image = running api image ($($api.Image.Substring(0, 19)))."
}

# --- Run ----------------------------------------------------------------------------------------------------------
$code = 0
try {
  # Typed confirmations for the live installation, before anything is written.
  if ($Target -eq "Production") {
    switch ($Mode) {
      "Import" { Confirm-Typed "IMPORT INTO PRODUCTION" "This imports JARVIS's data into the LIVE Command Center. A dump of its command_center database is taken first; undo is -Mode Rollback." }
      "Resume" { Confirm-Typed "IMPORT INTO PRODUCTION" "This continues import run $Run in the LIVE Command Center." }
      "Rollback" { Confirm-Typed "ROLL BACK PRODUCTION" "This removes everything import run $Run added to the LIVE Command Center." }
      "Rescan" { Confirm-Typed "RESCAN PRODUCTION" "This re-cleans every stored JARVIS text in the LIVE Command Center with the current detectors." }
      "ApplyDecisions" { Confirm-Typed "APPLY DECISIONS TO PRODUCTION" "This applies the review decisions from the test copy to import run $Run in the LIVE Command Center." }
    }
  }

  if (-not (Test-Path -LiteralPath $Reports)) { $null = New-Item -ItemType Directory -Path $Reports }
  $source = @("--db", "/jarvis/state.db", "--profile", "/jarvis/profile")
  if ($contentDir) { $source += @("--content", "/jarvis/content") }
  $report = "/reports/" + $Mode.ToLower() + "-" + $Target.ToLower() + "-$stamp.json"
  $extra = @()
  if ($Label) { $extra += @("--label", $Label) }
  if ($Actor) { $extra += @("--actor", $Actor) }
  $cli = @()
  switch ($Mode) {
    "Plan" { $cli = @("plan") + $source + $selection + @("--report", $report) }
    "Import" { $cli = @("run") + $source + $selection + $extra + @("--report", $report) }
    "Resume" { $cli = @("resume", "--run", "$Run") + $source + @("--report", $report) }
    "Verify" { $cli = @("verify", "--run", "$Run") + $source + @("--report", $report) }
    "Rollback" {
      $cli = @("rollback", "--run", "$Run", "--yes")
      if ($DiscardReviewed) { $cli += "--discard-reviewed" }
      if ($Actor) { $cli += @("--actor", $Actor) }
    }
    "Status" { $cli = @("status"); if ($Run -gt 0) { $cli += @("--run", "$Run") } }
    "Rescan" { $cli = @("rescan", "--yes") }
    "ExportDecisions" {
      $report = "/reports/decisions-" + $Target.ToLower() + "-$stamp.json"
      $cli = @("decisions-export", "--out", $report)
    }
    "ApplyDecisions" {
      $cli = @("decisions-apply", "--in", (Get-InReports $Decisions "decisions"), "--run", "$Run", "--yes")
      if ($Actor) { $cli += @("--actor", $Actor) }
    }
  }
  if ($Discovery -and ($Mode -eq "Plan" -or $Mode -eq "Import")) { $cli += @("--discovery", (Get-InReports $Discovery "discovery-summary")) }

  if ($Target -eq "Production" -and $Mode -eq "Import") {
    # A dump of command_center only, from the live postgres container, checked with pg_restore -l, then copied out.
    Write-Host "Writing a dump of the live command_center database first..."
    $dump = New-DatabaseDump $pgIds[0] (Join-Path $Reports "pre-import") "cc-pre-import-$stamp.dump"
    Write-Host "Dump of the live database (pg_restore -l: $($dump.Tables) tables): $($dump.File)" -ForegroundColor Green
    Write-Host "SHA-256 $($dump.Sha256)"
    Write-Host "Undo an import with -Mode Rollback. Restoring this dump is disaster recovery only: it loses everything since it was taken."
    $cli += @("--pre-backup-sha256", $dump.Sha256)
  }

  Write-Host ("brain-import " + ($cli -join " ")) -ForegroundColor Cyan
  $code = Invoke-Importer $cli
  $hostReport = Join-Path $Reports ($report.Substring("/reports/".Length))
  if (Test-Path -LiteralPath $hostReport) { Write-Host "Report: $hostReport" }
  switch ($code) {
    0 { Write-Host "brain-import $Mode finished." -ForegroundColor Green }
    2 { Write-Host "brain-import refused $Mode before changing anything (see its message above)." -ForegroundColor Red }
    default { Write-Host "brain-import $Mode failed (exit $code); see its message above." -ForegroundColor Red }
  }
} catch {
  $code = 1
  Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red
} finally {
  Clear-ImportVars
}

$after = @(Get-LiveFingerprint)
Show-Fingerprint "live installation after ($Target $Mode)" $after
Assert-SameFingerprint $before $after
exit $code
