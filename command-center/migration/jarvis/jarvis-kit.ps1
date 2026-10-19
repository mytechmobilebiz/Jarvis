# JARVIS migration kit: a read-only inventory and an encrypted backup of the JARVIS installation, for planning
# its migration into the Infinite AI Command Center. Nothing here changes JARVIS, the Command Center or any other
# program: the work runs in a temporary Docker container with no network, the JARVIS folders mounted read-only,
# and only a new output folder writable. JARVIS can stay open. See README.md.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\jarvis-kit.ps1 -Action Backup
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\jarvis-kit.ps1 -Action VerifyBackup -Archive <file.ccjb>
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\jarvis-kit.ps1 -Action Discover
param(
  [ValidateSet("Discover", "Backup", "VerifyBackup")]
  [string]$Action = "Discover",
  [string]$ProfileDir = "",
  [string]$ContentDir = "",
  [string]$BackupDb = "C:\InfiniteAI\JarvisMigration\backups\jarvis-state-20261019-105625.db",
  [string]$OutRoot = "C:\InfiniteAI\JarvisMigration",
  [string]$Archive = "",
  [string]$ExtractTo = "",
  [int]$Sample = 500,
  [switch]$Full,
  [switch]$Check,
  [switch]$ListOtherProfiles,
  [string]$Image = "infinite-command-center/backend:local"
)
$ErrorActionPreference = "Stop"
$kit = $PSScriptRoot
$sep = [IO.Path]::DirectorySeparatorChar

function Stop-Kit([string]$message) {
  Write-Host "NOT RUN: $message" -ForegroundColor Red
  Write-Host "Nothing was changed."
  exit 2
}

function Get-FullPath([string]$path) {
  # Relative to the PowerShell location, as the user typed it.
  try {
    $resolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($path)
    return [IO.Path]::GetFullPath($resolved).TrimEnd('\', '/')
  } catch {
    Stop-Kit "the path $path cannot be used ($($_.Exception.GetType().Name))"
  }
}

function Test-Native([scriptblock]$command) {
  # Windows PowerShell 5.1 turns a native program's redirected error output into a terminating error under "Stop".
  $ErrorActionPreference = "Continue"
  $null = & $command 2>$null
  return ($LASTEXITCODE -eq 0)
}

function Test-Inside([string]$path, [string]$folder) {
  $p = (Get-FullPath $path) + $sep
  $f = (Get-FullPath $folder) + $sep
  return $p.StartsWith($f, [StringComparison]::OrdinalIgnoreCase)
}

function Read-Passphrase([bool]$confirm) {
  $prompt = "Backup passphrase (at least 12 characters; without it the backup cannot be opened)"
  $values = @()
  foreach ($round in @(1, 2)) {
    if ($round -eq 2 -and -not $confirm) { break }
    $secure = Read-Host -AsSecureString $(if ($round -eq 1) { $prompt } else { "Type the passphrase again" })
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $values += [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
  }
  if ($confirm -and $values[0] -cne $values[1]) { Stop-Kit "the two passphrases differ" }
  if ($values[0].Length -lt 12 -or $values[0] -notmatch '^[\x20-\x7E]+$') {
    Stop-Kit "the passphrase must have at least 12 characters: letters, digits, spaces and the symbols on a US keyboard"
  }
  return $values[0]
}

# --- Checks before anything runs ---------------------------------------------------------------------------------
# JARVIS's folders for this Windows user, unless given. Every action refuses to write inside them, and inside the
# Hermes folder that holds them (also the default one, in case -ProfileDir points elsewhere).
if (-not $ProfileDir -and $env:LOCALAPPDATA) { $ProfileDir = Join-Path $env:LOCALAPPDATA "hermes\profiles\jarvis" }
if (-not $ContentDir -and $env:LOCALAPPDATA) { $ContentDir = Join-Path $env:LOCALAPPDATA "hermes\jarvis-content" }
$protected = @()
if ($ProfileDir) {
  $ProfileDir = Get-FullPath $ProfileDir
  $protected += $ProfileDir
  # The Hermes folder, when the profile sits where Hermes keeps profiles (<hermes>\profiles\<name>).
  $parent = Split-Path -Parent $ProfileDir
  if ($parent -and (Split-Path -Leaf $parent) -eq "profiles") {
    $grandparent = Split-Path -Parent $parent
    if ($grandparent) { $protected += $grandparent }
  }
}
if ($ContentDir) {
  $ContentDir = Get-FullPath $ContentDir
  $protected += $ContentDir
}
if ($env:LOCALAPPDATA) { $protected += (Get-FullPath (Join-Path $env:LOCALAPPDATA "hermes")) }
# Never a drive root (C:\ would make everything "inside").
$protected = @($protected | Where-Object { $_ -and ($_.TrimEnd('\', '/') -ne ([IO.Path]::GetPathRoot($_)).TrimEnd('\', '/')) })
if ($Action -ne "VerifyBackup") {
  $OutRoot = Get-FullPath $OutRoot
  foreach ($folder in $protected) {
    if (Test-Inside $OutRoot $folder) { Stop-Kit "the output folder $OutRoot is inside the JARVIS folders; choose a folder outside them" }
  }
  foreach ($dir in @($ProfileDir, $ContentDir)) {
    if (-not $dir -or -not (Test-Path -LiteralPath $dir -PathType Container)) { Stop-Kit "folder not found: $dir" }
  }
}
if ($Action -eq "Discover" -and $BackupDb) {
  if (-not (Test-Path -LiteralPath $BackupDb -PathType Leaf)) { Stop-Kit "database backup not found: $BackupDb" }
  $BackupDb = Get-FullPath $BackupDb
  foreach ($folder in $protected) {
    if (Test-Inside $BackupDb $folder) { Stop-Kit "$BackupDb is inside the JARVIS folders ($folder); use the verified backup copy outside them" }
  }
}
if ($Action -eq "VerifyBackup") {
  if (-not $Archive -or -not (Test-Path -LiteralPath $Archive -PathType Leaf)) { Stop-Kit "give the backup with -Archive <file.ccjb>" }
  $Archive = Get-FullPath $Archive
  if ($ExtractTo) {
    $ExtractTo = Get-FullPath $ExtractTo
    foreach ($folder in $protected) {
      if (Test-Inside $ExtractTo $folder) { Stop-Kit "restore only outside the JARVIS folders; $ExtractTo is inside $folder" }
    }
    if ((Test-Path -LiteralPath $ExtractTo) -and (Get-ChildItem -LiteralPath $ExtractTo -Force | Select-Object -First 1)) {
      Stop-Kit "extract only into a new or empty folder; $ExtractTo is not empty"
    }
  }
}
foreach ($file in @("jarvis_kit.py", "templates.json")) {
  if (-not (Test-Path -LiteralPath (Join-Path $kit $file))) { Stop-Kit "$file is missing next to this script; extract the whole kit" }
}
if (-not (Test-Native { docker version --format "{{.Server.Version}}" })) {
  Stop-Kit "Docker is not running; start Docker Desktop and try again"
}
if (-not (Test-Native { docker image inspect $Image })) {
  Stop-Kit "the Docker image $Image is not on this PC (the Command Center's install.ps1 builds it)"
}

# --- The container: no network, read-only system, JARVIS folders read-only, one writable output folder ----------
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$docker = @("run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
  "--security-opt", "no-new-privileges", "--pids-limit", "256", "--memory", "2g",
  "--tmpfs", "/tmp:rw,size=256m", "--workdir", "/tmp", "--entrypoint", "python",
  "-v", "${kit}:/kit:ro")
if ($IsLinux -or $IsMacOS) {
  # Outside Windows, folders created here belong to this user: the container runs as the same user.
  $docker += @("--user", ((id -u) + ":" + (id -g)))
}
$passphrase = $null
switch ($Action) {
  "Discover" {
    $out = Join-Path $OutRoot "discovery-$stamp"
    $docker += @("-v", "${ProfileDir}:/src/profile:ro", "-v", "${ContentDir}:/src/content:ro", "-v", "${out}:/out")
    $run = @("discover", "--profile", "/src/profile", "--content", "/src/content", "--out", "/out", "--sample", "$Sample")
    if ($BackupDb) {
      $docker += @("-v", ((Split-Path -Parent $BackupDb) + ":/src/backup:ro"))
      $run += @("--db", ("/src/backup/" + (Split-Path -Leaf $BackupDb)))
    }
    if ($Full) { $run += "--full" }
    if ($Check) { $run += "--check" }
  }
  "Backup" {
    $passphrase = Read-Passphrase $true
    $out = Join-Path $OutRoot "backups\profile-backup-$stamp"
    $docker += @("-i", "-v", "${ProfileDir}:/src/profile:ro", "-v", "${ContentDir}:/src/content:ro", "-v", "${out}:/out")
    $run = @("backup", "--profile", "/src/profile", "--content", "/src/content", "--out", "/out")
  }
  "VerifyBackup" {
    $passphrase = Read-Passphrase $false
    $out = $null
    $docker += @("-i", "-v", ((Split-Path -Parent $Archive) + ":/archive:ro"))
    $run = @("verify", "--archive", ("/archive/" + (Split-Path -Leaf $Archive)))
    if ($ExtractTo) {
      $extractExisted = Test-Path -LiteralPath $ExtractTo
      $null = New-Item -ItemType Directory -Force -Path $ExtractTo
      $docker += @("-v", ($ExtractTo + ":/extract"))
      $run += @("--extract", "/extract")
    }
  }
}
if ($out) { $null = New-Item -ItemType Directory -Path $out }
$docker += @($Image, "-I", "-B", "/kit/jarvis_kit.py") + $run

# The kit writes progress and errors to stderr. Under "Stop", Windows PowerShell 5.1 would turn that into a
# terminating error whenever output is captured (ISE, *> log.txt), so the result is judged by the exit code alone.
$ErrorActionPreference = "Continue"
if ($passphrase) {
  $passphrase | & docker @docker
} else {
  & docker @docker
}
$code = $LASTEXITCODE
$ErrorActionPreference = "Stop"
$passphrase = $null

if ($code -ne 0) {
  # Nothing unfinished is left behind: an unverified archive and an empty restore folder are removed.
  if ($Action -eq "Backup" -and $out -and (Test-Path -LiteralPath $out)) {
    Get-ChildItem -LiteralPath $out -Filter "*.partial" | Remove-Item -Force
    if (-not (Get-ChildItem -LiteralPath $out -Force | Select-Object -First 1)) { Remove-Item -LiteralPath $out }
  }
  if ($Action -eq "VerifyBackup" -and $ExtractTo -and (Test-Path -LiteralPath $ExtractTo)) {
    # The kit's own staging folder, if Docker stopped it mid-restore; then the folder itself if the kit created it.
    Get-ChildItem -LiteralPath $ExtractTo -Directory -Force | Where-Object { $_.Name -match '^\.incomplete-[0-9a-f]{8}$' } |
      Remove-Item -Recurse -Force
    if (-not $extractExisted -and -not (Get-ChildItem -LiteralPath $ExtractTo -Force | Select-Object -First 1)) {
      Remove-Item -LiteralPath $ExtractTo
    }
  }
  if ($code -eq 137 -or $code -eq 143) {
    Write-Host "Docker stopped the kit before it finished (Docker Desktop closed or out of memory). Run the command again." -ForegroundColor Red
  } else {
    Write-Host ("The kit stopped (exit code {0}); see the message above." -f $code) -ForegroundColor Red
  }
  Write-Host "JARVIS was not changed."
  exit $code
}
if ($Action -eq "Discover" -and $ListOtherProfiles) {
  # Only the names of the other Hermes profiles next to JARVIS's, and whether each has its own database.
  $lines = Get-ChildItem -LiteralPath (Split-Path -Parent $ProfileDir) -Directory | ForEach-Object {
    $db = Join-Path $_.FullName "state.db"
    if (Test-Path -LiteralPath $db) { "{0}: state.db {1:N1} MB" -f $_.Name, ((Get-Item -LiteralPath $db).Length / 1MB) }
    else { "{0}: no state.db" -f $_.Name }
  }
  Set-Content -LiteralPath (Join-Path $out "other-profiles.txt") -Value $lines -Encoding ASCII
}
switch ($Action) {
  "Discover" {
    Write-Host ""
    Write-Host "Reports in $out" -ForegroundColor Green
    Write-Host "  discovery-summary.md    counts and structure only; send this one if you want it reviewed"
    Write-Host "  discovery-summary.json  the same, for the importer"
    Write-Host "  discovery-detail.json   adds file names (which can name customers): keep it on this PC"
  }
  "Backup" {
    $made = Get-ChildItem -LiteralPath $out -Filter "*.ccjb" | Select-Object -First 1
    Write-Host ""
    Write-Host "Encrypted backup: $($made.FullName)" -ForegroundColor Green
    Write-Host "Keep the passphrase somewhere safe (not next to the backup). backup-report.json counts what was left out;"
    Write-Host "backup-files.json lists every file, which were redacted and which could not be scanned (keep it on this PC)."
    Write-Host "To check that it opens:"
    Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File .\jarvis-kit.ps1 -Action VerifyBackup -Archive `"$($made.FullName)`""
  }
  "VerifyBackup" { Write-Host "Backup verified." -ForegroundColor Green }
}
exit 0
