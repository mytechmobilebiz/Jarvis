# Imports JARVIS's knowledge and history into the Command Center's memory (docs/JARVIS_BRAIN_SPEC.md 6.6), first into
# the test copy (brain-test.ps1), later into the live installation. The import itself is "python -m app.cli
# brain-import" from the image the running api uses, in a one-off container on the private network only, with the
# JARVIS database backup and the restored profile mounted read-only (docker-compose.brain-import.yml).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target Test -Mode Plan -All `
#     -BackupDb C:\InfiniteAI\JarvisMigration\backups\jarvis-state-<time>.db -Restored C:\InfiniteAI\JarvisMigration\restored
#   ... -Mode Import -Sample 5        then review, then -Sample <about 10 % of the sessions>, then review, then -All
#   ... -Mode Verify -Run <id>        -Mode Status [-Run <id>]        -Mode Rollback -Run <id> [-DiscardReviewed]
#   ... -Mode Settle (test copy, 0.6.2: after withholding, finish the decisions on the newest backup imported; review
#       what it holds), then -Mode ExportDecisions (refused until every withhold is finished and reviewed), then on
#       live, with the very same backup file:
#       -Mode Import -All -Decisions <file> -Actor <admin email>   (0.6.2: the withholds, releases and purges apply
#       first, then the import replays the decisions taken on that backup; a version 5 file)
#   ... -Mode ApplyDecisions -Decisions <file> -Run <id> -Actor <admin email>   (after an import: version 3 and 4 files)
#   ... -Mode Resume -Run <id>        -Mode Rescan        -Mode Repair (0.6.2: withhold what is the same message as a
#       withheld one now, without waiting for the next start or import)
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
$modes = @("Plan", "Import", "Settle", "Resume", "Verify", "Rollback", "Status", "ExportDecisions", "ApplyDecisions",
  "Rescan", "Repair")
if ($targets -notcontains $Target) { Stop-Brain "give -Target Test or -Target Production" }
if ($modes -notcontains $Mode) { Stop-Brain ("give -Mode " + ($modes -join ", ")) }
$Target = $targets | Where-Object { $_ -eq $Target }
$Mode = $modes | Where-Object { $_ -eq $Mode }
$readsSource = @("Plan", "Import", "Settle", "Resume", "Verify") -contains $Mode
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
if (@("ApplyDecisions", "Import") -contains $Mode -and $Decisions -and -not $Actor) { Stop-Brain "-Mode $Mode -Decisions needs -Actor <your admin email> (recorded as the reviewer of each decision)" }
if ($Decisions -and @("ApplyDecisions", "Import") -notcontains $Mode) { Stop-Brain "-Decisions goes with -Mode Import (version 5 files) or -Mode ApplyDecisions (older files)" }

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
# The importer can write to the reports folder: it must not hold JARVIS, the Command Center or a whole drive.
if ($hermes -and (Test-Inside $hermes $Reports)) { Stop-Brain "the reports folder $Reports contains JARVIS's folders ($hermes); choose a dedicated folder for the reports" }
if ($Reports.TrimEnd('\', '/') -eq ([IO.Path]::GetPathRoot($Reports)).TrimEnd('\', '/')) { Stop-Brain "the reports folder cannot be a whole drive ($Reports); choose a dedicated folder" }
if ((Test-Inside $root $Reports) -or (Test-Inside $Reports $root)) { Stop-Brain "the reports folder $Reports must be outside the Command Center's folder ($root) and must not contain it" }

$profileDir = ""
$contentDir = ""
if ($readsSource) {
  if (-not $BackupDb) { Stop-Brain "-Mode $Mode needs -BackupDb <the JARVIS database backup (.db) outside JARVIS's folders>" }
  if (-not $Restored) { Stop-Brain "-Mode $Mode needs -Restored <the folder jarvis-kit.ps1 -Action VerifyBackup -ExtractTo restored>" }
  $BackupDb = Get-FullPath $BackupDb
  $Restored = Get-FullPath $Restored
  if (-not (Test-Path -LiteralPath $BackupDb -PathType Leaf)) { Stop-Brain "database backup not found: $BackupDb" }
  foreach ($path in @($BackupDb, $Restored)) {
    if (Test-Inside $path $Reports) { Stop-Brain "$path is inside the reports folder $Reports, which the importer can write to; keep the backup and the restored files outside it" }
  }
  if (Test-Inside $Reports $Restored) { Stop-Brain "the reports folder $Reports is inside the restored files ($Restored); choose a folder outside them" }
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
$decisionsVersion = 0
$decisionsData = $null
if ($Decisions) {
  $Decisions = Get-FullPath $Decisions
  if (-not (Test-Path -LiteralPath $Decisions -PathType Leaf)) { Stop-Brain "decisions file not found: $Decisions" }
  try { $decisionsData = Get-Content -LiteralPath $Decisions -Raw | ConvertFrom-Json } catch { Stop-Brain "the decisions file could not be read as JSON: $Decisions" }
  if ([string]$decisionsData.format -ne "jarvis-brain-decisions") { Stop-Brain "$Decisions is not a decisions file exported by -Mode ExportDecisions" }
  $decisionsVersion = [int]$decisionsData.version
  # 0.6.2 plan 3.3: a version 5 file goes with the import itself (its withholds apply before any row is written, so no
  # withheld text is readable at any moment); applying it after an import is for older (version 3 and 4) files only.
  if ($Mode -eq "ApplyDecisions" -and $decisionsVersion -ge 5) {
    Stop-Brain "this decisions file is version ${decisionsVersion}: give it to the import itself (-Mode Import -All -Decisions <file> -Actor <admin email>, with the very backup file the test copy imported), not after it"
  }
  if ($Mode -eq "Import" -and $decisionsVersion -lt 5) {
    Stop-Brain "-Mode Import -Decisions needs a version 5 decisions file (this one is version $decisionsVersion); apply an older file after the import with -Mode ApplyDecisions"
  }
  if ($Mode -eq "Import") {
    # The decisions are replayed only on the very backup file they were taken on (its SHA-256) by the same importer
    # version; any other file is imported as a later backup for the conversations that hold a withheld message
    # (new rows held for review): allowed, and said here first.
    $backupSha = (Get-FileHash -LiteralPath $BackupDb -Algorithm SHA256).Hash.ToLowerInvariant()
    $decided = @($decisionsData.backups | ForEach-Object { [string]$_.sha256 })
    if ($decided -notcontains $backupSha) {
      Write-Host "WARNING: $BackupDb (SHA-256 $backupSha) is not the backup the decisions in $Decisions were taken on: conversations with a withheld message are imported as a later backup (their new rows are held for review). Import the very file the test copy imported to replay its decisions exactly." -ForegroundColor Yellow
    } else {
      Write-Host "The decisions were taken on this very backup file (SHA-256 $backupSha): the import replays them." -ForegroundColor Green
    }
  }
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
# Docker's containerd image store an image cannot be run by its ID, and image IDs read from a container and from a
# name are not comparable). What is compared is the code itself: the Command Center and importer versions the running
# api reports (read-only docker exec) must equal those of the image the name points to now.
$imageRef = [string]$api.ImageRef
if (-not $imageRef -or $imageRef.StartsWith("sha256:")) { Stop-Brain "could not read the image name of the running api of $project" }
$versionCode = "import app, app.brain; print(app.__version__ + ' ' + app.brain.IMPORTER_VERSION)"
$running = Invoke-Docker @("exec", $api.Id, "python", "-c", $versionCode)
$named = Invoke-Docker @("run", "--rm", "--network", "none", "--read-only", "--entrypoint", "python", $imageRef, "-c", $versionCode)
$runningVersion = ""; $namedVersion = ""
if ($running.Code -eq 0 -and $running.Out.Count -gt 0) { $runningVersion = ([string]$running.Out[-1]).Trim() }
if ($named.Code -eq 0 -and $named.Out.Count -gt 0) { $namedVersion = ([string]$named.Out[-1]).Trim() }
if (-not $runningVersion) { Stop-Brain "the running api of $project has no JARVIS Brain importer (it is older than 0.6.0)" }
if ($runningVersion -ne $namedVersion) {
  Stop-Brain ("the image $imageRef holds other code ($namedVersion) than the running api of $project ($runningVersion);" +
    " restart that installation first, so the importer runs the same code as the api")
}
if ($decisionsData -and $Mode -eq "Import") {
  $importerHere = ($runningVersion -split ' ')[-1]
  if ([string]$decisionsData.importer_version -ne $importerHere) {
    Write-Host "WARNING: the decisions file was written by importer $($decisionsData.importer_version), this installation runs importer ${importerHere}: conversations with a withheld message are imported as a later backup (their new rows are held for review)." -ForegroundColor Yellow
  }
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
  if ($migrations -notcontains "0006_brain") { Clear-ImportVars; Stop-Brain "the live database has no 0006_brain migration: install 0.6.2 on the live installation first and let it start once" }
  if ($migrations -notcontains "0007_brain_withhold") { Clear-ImportVars; Stop-Brain "the live database has no 0007_brain_withhold migration: install 0.6.2 on the live installation first and let it start once" }
  if ($migrations -notcontains "0008_brain_withhold_identity") { Clear-ImportVars; Stop-Brain "the live database has no 0008_brain_withhold_identity migration: install 0.6.2 on the live installation first and let it start once" }
  if (-not (Test-Path -LiteralPath $Reports)) { $null = New-Item -ItemType Directory -Path $Reports }
  $saved = $service
  $service = "brain-importer-nosource"
  $r = Invoke-Importer @("status") -Capture
  $service = $saved
  $printed = ($r.Out -join "`n")
  # brain-import status prints "importer <version>; JARVIS Brain tables installed" (tests/unit/test_brain_ops.py runs this
  # pattern against the CLI's own output).
  if ($r.Code -ne 0 -or $printed -notmatch '(?m)^importer (\d+\.\d+\.\d+); JARVIS Brain tables installed\b') {
    Clear-ImportVars
    Stop-Brain "the running api's image has no working brain-import (status exit $($r.Code)); install 0.6.2 on the live installation first"
  }
  $importerVersion = $Matches[1]
  if ([version]$importerVersion -lt [version]"1.1.0") {
    Clear-ImportVars
    Stop-Brain "the running api's importer is ${importerVersion}: install 0.6.2 on the live installation first (importer 1.1.0 or later keeps withheld messages withheld whatever JARVIS does to them)"
  }
  # 0.6.1 empties, at start-up, the memory items promoted from withheld messages that 0.6.0 left with their text; the
  # status line says how many are left (tests/unit/test_brain_ops.py runs this pattern against the CLI's own output).
  # Only what brings JARVIS text in (Import, Resume) waits for 0: Status shows the count, Rescan is a remedy, and
  # Verify, Rollback and ApplyDecisions add no text.
  $withheldCopies = "unknown (no 0.6.1 importer)"
  if ($printed -match '(?m)^migration 0007_brain_withhold present; withheld copies with text: (\d+)\b') { $withheldCopies = $Matches[1] }
  if (@("Import", "Settle", "Resume") -contains $Mode -and $withheldCopies -ne "0") {
    Clear-ImportVars
    Stop-Brain "the live installation still has promoted memory items whose source message is withheld, or no 0.6.1 importer: install 0.6.2 on the live installation first and let it start once (the start-up check is skipped while an import, rollback or rescan runs: if brain-import status still shows withheld copies with text above 0, restart the live installation, or run .\scripts\jarvis-import.ps1 -Target Production -Mode Rescan)"
  }
  # 0.6.2: the withhold identity (tests/unit/test_brain_ops.py runs this pattern against the CLI's own output). An import
  # repairs before it imports, so readable rows matching a withhold, holds, quotes and guard counts are shown, not
  # required; the repair itself must have run once (the start-up check, or -Mode Repair), and purge fingerprints must
  # all be readable with this database password.
  $identity = $null
  if ($printed -match '(?m)^migration 0008_brain_withhold_identity present; identity repair (done|not done); readable rows matching a withhold: (\d+); held for review: (\d+); withheld rows with a text fingerprint: (\d+); rows blanked by the database guard: (\d+); quotes of removed sources: (\d+); purge fingerprints made with another key: (\d+)\b') {
    $identity = @{ Repair = $Matches[1]; Readable = $Matches[2]; Held = $Matches[3]; Fingerprints = $Matches[4]; Guard = $Matches[5]; Quotes = $Matches[6]; OtherKey = $Matches[7] }
  }
  if (@("Import", "Settle", "Resume") -contains $Mode) {
    if (-not $identity) {
      Clear-ImportVars
      Stop-Brain "brain-import status shows no withhold identity line: install 0.6.2 on the live installation first and let it start once"
    }
    if ($identity.Repair -ne "done") {
      Clear-ImportVars
      Stop-Brain "the identity repair has not run on the live installation yet: let it start once (the start-up check runs it), or run .\scripts\jarvis-import.ps1 -Target Production -Mode Repair"
    }
    if ($identity.OtherKey -ne "0") {
      Clear-ImportVars
      Stop-Brain "$($identity.OtherKey) purged-item fingerprint(s) were made with another database password: restore that password, or in this folder run: docker compose exec -T api python -m app.cli brain-import fingerprints --forget-other-key --yes (the purges themselves stay, by their keys)"
    }
  }
  Write-Host "Live installation: importer $importerVersion, migrations 0006_brain, 0007_brain_withhold and 0008_brain_withhold_identity present, withheld copies with text: $withheldCopies, importer image = running api code ($runningVersion)."
  if ($identity) {
    Write-Host "Withhold identity: repair $($identity.Repair); readable rows matching a withhold: $($identity.Readable) (an import repairs them first); held for review: $($identity.Held); withheld rows with a text fingerprint: $($identity.Fingerprints); rows blanked by the database guard: $($identity.Guard); quotes of removed sources: $($identity.Quotes)."
  }
}

# --- Run ----------------------------------------------------------------------------------------------------------
$code = 0
try {
  # Typed confirmations for the live installation, before anything is written.
  if ($Target -eq "Production") {
    switch ($Mode) {
      "Import" {
        $withDecisions = ""
        if ($Decisions) { $withDecisions = " The decisions in $Decisions (withholds, releases and purges) are applied first, then replayed by the import." }
        Confirm-Typed "IMPORT INTO PRODUCTION" ("This imports JARVIS's data into the LIVE Command Center. A dump of its command_center database is taken first; undo is -Mode Rollback." + $withDecisions)
      }
      "Settle" { Confirm-Typed "FINISH DECISIONS IN PRODUCTION" "This finishes, in the LIVE Command Center, the withholds taken on this JARVIS backup: their conversations are imported again from it, and every related row is held for review." }
      "Resume" { Confirm-Typed "IMPORT INTO PRODUCTION" "This continues import run $Run in the LIVE Command Center." }
      "Rollback" { Confirm-Typed "ROLL BACK PRODUCTION" "This removes everything import run $Run added to the LIVE Command Center." }
      "Rescan" { Confirm-Typed "RESCAN PRODUCTION" "This re-cleans every stored JARVIS text in the LIVE Command Center with the current detectors." }
      "Repair" { Confirm-Typed "REPAIR PRODUCTION" "This withholds, in the LIVE Command Center, every stored row that is the same JARVIS message as a withheld one." }
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
    "Settle" {
      # 0.6.2 plan 4.1: the conversations of the backup only (the profile is mounted, never read)
      $cli = @("settle", "--backup", "/jarvis/state.db", "--report", $report)
      if ($Actor) { $cli += @("--actor", $Actor) }
    }
    "Resume" { $cli = @("resume", "--run", "$Run") + $source + @("--report", $report) }
    "Verify" { $cli = @("verify", "--run", "$Run") + $source + @("--report", $report) }
    "Rollback" {
      $cli = @("rollback", "--run", "$Run", "--yes")
      if ($DiscardReviewed) { $cli += "--discard-reviewed" }
      if ($Actor) { $cli += @("--actor", $Actor) }
    }
    "Status" { $cli = @("status"); if ($Run -gt 0) { $cli += @("--run", "$Run") } }
    "Rescan" { $cli = @("rescan", "--yes") }
    "Repair" { $cli = @("repair", "--yes") }
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
  if ($Decisions -and $Mode -eq "Import") { $cli += @("--decisions", (Get-InReports $Decisions "decisions")) }

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
