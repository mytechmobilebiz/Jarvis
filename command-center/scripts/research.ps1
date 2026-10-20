# Adds or removes web research's private search service (SearXNG, docker-compose.research.yml) on this PC, or shows
# its state. Whether agents may search (off, mock or live) is a separate switch: an admin sets it in the dashboard's
# Research Center, and it stays off until then. See docs\RESEARCH_GATEWAY.md.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\research.ps1 -Status
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\research.ps1 -Enable
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\research.ps1 -Disable
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\research.ps1 -RotateSecret
#
# -Enable adds docker-compose.research.yml to COMPOSE_FILE in .env, -Disable removes it again (entries separated by
# ';'). Every other entry (such as the remote-access tunnel) and every other line of .env stay as they are; .env is
# copied to .env.before-research-<time> first. When .env has no SEARXNG_SECRET (an upgraded installation: install and
# upgrade never add it), -Enable creates it (32 random bytes, as init-env.ps1 does for a new .env) in that same write.
# Both then run start.ps1 -NoBuild: the worker is recreated once (it joins or leaves the private "research" network),
# PostgreSQL, n8n, the API and the dashboard keep running.
# -Disable also removes the SearXNG container, its cache volume and its two networks (the image is kept).
# -RotateSecret gives SEARXNG_SECRET a new random value and recreates only the SearXNG container.
# None of them runs while the JARVIS Brain test copy or a JARVIS import is running, or changes a read-only .env. .env
# is replaced in one step (written next to it, then swapped in): a write that fails leaves it as it was and stops the
# script before anything else runs. No secret is ever printed.
# Exit codes: 0 done; 1 failed; 2 not run (a check refused; nothing was changed).
param([switch]$Enable, [switch]$Disable, [switch]$Status, [switch]$RotateSecret)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$envFile = Join-Path $root ".env"
$tunnelSubnet = "172.29.37.0/28"  # docker-compose.tunnel.yml's fixed subnet

function Stop-Research([string]$message) {
  Write-Host "NOT RUN: $message" -ForegroundColor Red
  Write-Host "Nothing was changed."
  exit 2
}

$helper = Join-Path $PSScriptRoot "compose-env.ps1"
if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) { Stop-Research "scripts\compose-env.ps1 is missing next to this script; extract the whole package again" }
. $helper

$chosen = @(@($Enable.IsPresent, $Disable.IsPresent, $Status.IsPresent, $RotateSecret.IsPresent) | Where-Object { $_ })
if ($chosen.Count -ne 1) { Stop-Research "give exactly one of -Enable, -Disable, -Status or -RotateSecret" }
if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) { Stop-Research "this folder has no .env; run it in the Command Center's installation folder" }

function New-Secret([int]$bytes) {
  $buffer = New-Object byte[] $bytes
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buffer)
  return ([Convert]::ToBase64String($buffer)).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Get-ComposeFileLines {
  # The lines of .env that set COMPOSE_FILE (Compose would use the last one).
  return @(Get-Content -LiteralPath $envFile | Where-Object { $_ -match '^\s*(export\s+)?COMPOSE_FILE\s*=' })
}

function Get-SecretLines {
  # The lines of .env that set SEARXNG_SECRET.
  return @(Get-Content -LiteralPath $envFile | Where-Object { $_ -match '^\s*(export\s+)?SEARXNG_SECRET\s*=' })
}

function Assert-EnvWritable {
  # Before anything changes: a read-only .env (its attribute on Windows, its permissions elsewhere) is not replaced.
  if ((Get-Item -LiteralPath $envFile -Force).IsReadOnly) { Stop-Research ".env is read-only; make it writable (or run this as its owner) and try again" }
}

function Write-EnvLines([scriptblock]$change, [string[]]$append = @()) {
  # Copies .env to .env.before-research-<time>, then replaces it in one step: the new text is written to
  # .env.research-tmp and swapped in, so .env is never left half-written. $change gets each line and returns the line
  # to keep, or $null to drop it; the lines of $append, if any, come last. Other lines stay as they are; LF line
  # endings, UTF-8 without BOM. If anything fails (a file locked by antivirus or sync software, a full disk, no
  # permission), .env is as it was and the script stops here (exit 1): start.ps1 never runs on a .env it did not write.
  $stamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
  $copy = Join-Path $root ".env.before-research-$stamp"
  $tmp = "$envFile.research-tmp"
  $written = ""
  try {
    Copy-Item -LiteralPath $envFile -Destination $copy -Force -ErrorAction Stop
    $text = [IO.File]::ReadAllText($envFile)
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($line in ($text -split "`r?`n")) { $lines.Add($line) }
    # A final line break leaves one empty piece at the end: not a line of its own.
    if ($text.EndsWith("`n") -and $lines.Count -gt 0) { $lines.RemoveAt($lines.Count - 1) }
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
      $kept = & $change $line
      if ($null -ne $kept) { $out.Add([string]$kept) }
    }
    foreach ($extra in $append) { if ($extra) { $out.Add($extra) } }
    $written = ($out -join "`n") + "`n"
    [IO.File]::WriteAllText($tmp, $written, (New-Object System.Text.UTF8Encoding $false))
    # No backup file (the copy above is the backup). PowerShell would pass $null as "", which File.Replace refuses.
    [IO.File]::Replace($tmp, $envFile, [System.Management.Automation.Language.NullString]::Value)
  } catch {
    $why = $_.Exception
    if ($why.InnerException) { $why = $why.InnerException }
    if (Test-Path -LiteralPath $tmp -PathType Leaf) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
    Write-Host "FAILED: .env could not be written ($($why.GetType().Name)); it is unchanged and nothing else was done." -ForegroundColor Red
    Write-Host "Close any program that holds .env open, check the free disk space and the file's permissions, then run this again."
    exit 1
  }
  if ([IO.File]::ReadAllText($envFile) -cne $written) {
    Write-Host "FAILED: .env does not hold what was just written; nothing else was done. The previous copy is $copy." -ForegroundColor Red
    exit 1
  }
  Write-Host "Updated .env (the previous copy is $copy)."
}

function Set-ComposeFile([string[]]$entries, [string]$newSecret = "") {
  # COMPOSE_FILE becomes these entries; only docker-compose.yml (Compose's default) removes the line. With $newSecret,
  # the same write also sets SEARXNG_SECRET: on its (empty) line, or as a new last line.
  $value = ($entries -join ";")
  $dropLine = ($entries.Count -eq 0) -or ($entries.Count -eq 1 -and $entries[0] -eq "docker-compose.yml")
  $hasLine = (Get-ComposeFileLines).Count -gt 0
  $append = @()
  if (-not $hasLine -and -not $dropLine) { $append += "COMPOSE_FILE=$value" }
  if ($newSecret -and (Get-SecretLines).Count -eq 0) { $append += "SEARXNG_SECRET=$newSecret" }
  if (-not $hasLine -and -not $newSecret -and $append.Count -eq 0) { return }
  Write-EnvLines ({
    param($line)
    if ($newSecret -and $line -match '^\s*(export\s+)?SEARXNG_SECRET\s*=') { return "SEARXNG_SECRET=$newSecret" }
    if ($line -notmatch '^\s*(export\s+)?COMPOSE_FILE\s*=') { return $line }
    if ($dropLine) { return $null }
    return "COMPOSE_FILE=$value"
  }.GetNewClosure()) $append
}

function Set-Secret([string]$newSecret) {
  # SEARXNG_SECRET gets this value in one write: on its line, or as a new last line. Nothing else changes.
  $append = @()
  if ((Get-SecretLines).Count -eq 0) { $append += "SEARXNG_SECRET=$newSecret" }
  Write-EnvLines ({
    param($line)
    if ($line -match '^\s*(export\s+)?SEARXNG_SECRET\s*=') { return "SEARXNG_SECRET=$newSecret" }
    return $line
  }.GetNewClosure()) $append
}

function Get-SearxngContainers([string]$project) {
  return @(docker ps -a -q --no-trunc --filter "label=com.docker.compose.project=$project" --filter "label=cc.research=searxng" 2>$null |
    ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Get-ResearchLeftovers([string]$project) {
  # The research networks and the cache volume of this project (names as Docker reports them).
  $networks = @()
  foreach ($key in @("research", "research_egress")) {
    $networks += @(docker network ls -q --filter "label=com.docker.compose.project=$project" --filter "label=com.docker.compose.network=$key" 2>$null |
      ForEach-Object { $_.Trim() } | Where-Object { $_ })
  }
  $volumes = @(docker volume ls -q --filter "label=com.docker.compose.project=$project" --filter "label=com.docker.compose.volume=searxng_cache" 2>$null |
    ForEach-Object { $_.Trim() } | Where-Object { $_ })
  return New-Object PSObject -Property @{ Networks = $networks; Volumes = $volumes }
}

function Get-PinnedImage {
  # The image docker-compose.research.yml pins, with this .env's registry.
  $file = Join-Path $root $ResearchComposeFile
  if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { return "" }
  $line = Get-Content -LiteralPath $file | Where-Object { $_ -match '^\s*image:\s*\$\{REGISTRY:-docker\.io\}/(\S+)\s*$' } | Select-Object -First 1
  if (-not $line) { return "" }
  $null = $line -match '^\s*image:\s*\$\{REGISTRY:-docker\.io\}/(\S+)\s*$'
  $registry = (Read-ComposeEnvFile $envFile)["REGISTRY"]
  if (-not $registry) { $registry = "docker.io" }
  return "$registry/$($Matches[1])"
}

function Get-Ipv4Range([string]$cidr) {
  # First and last address of an IPv4 subnet as numbers, or $null (IPv6 or not a subnet).
  if ($cidr -notmatch '^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/(\d{1,2})$') { return $null }
  $bits = [int]$Matches[2]
  if ($bits -gt 32) { return $null }
  $number = [int64]0
  foreach ($part in $Matches[1].Split(".")) { $number = $number * 256 + [int64]$part }
  $size = [int64][math]::Pow(2, 32 - $bits)
  $first = $number - ($number % $size)
  return @($first, ($first + $size - 1))
}

function Show-TunnelOverlap([string]$project) {
  # Remote access needs 172.29.37.0/28 free; a network that took it (the research networks come from Docker's
  # default address pools) would make the tunnel fail later with "Pool overlaps". A warning only.
  $tunnel = Get-Ipv4Range $tunnelSubnet
  $ids = @(docker network ls -q 2>$null | ForEach-Object { $_.Trim() } | Where-Object { $_ })
  if ($ids.Count -eq 0) { return }
  foreach ($line in @(docker network inspect --format '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}' $ids 2>$null)) {
    $parts = @("$line".Trim().Split(" ") | Where-Object { $_ })
    if ($parts.Count -lt 2 -or $parts[0] -eq "$($project)_tunnel") { continue }
    foreach ($subnet in $parts[1..($parts.Count - 1)]) {
      $range = Get-Ipv4Range $subnet
      if ($range -and $range[0] -le $tunnel[1] -and $tunnel[0] -le $range[1]) {
        Write-Host ("Note: the Docker network $($parts[0]) uses $subnet, which overlaps $tunnelSubnet, the fixed subnet of " +
          "remote access (docker-compose.tunnel.yml). Remote access would not start until one of them changes; see " +
          "docs\REMOTE_ACCESS_PLAN.md.") -ForegroundColor Yellow
      }
    }
  }
}

function Assert-NothingElseRunning {
  # Enabling and disabling recreate the live worker and create or remove networks: a JARVIS test copy or import run
  # would then report "LIVE INSTALLATION CHANGED".
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Stop-Research "the docker command was not found; start Docker Desktop" }
  $null = docker version --format '{{.Server.Version}}' 2>$null
  if ($LASTEXITCODE -ne 0) { Stop-Research "Docker is not running; start Docker Desktop and try again" }
  $testCopy = @(docker ps -q --filter "label=com.docker.compose.project=cc-brain-test" 2>$null | Where-Object { $_ })
  if ($testCopy.Count -gt 0) { Stop-Research "the JARVIS Brain test copy is running; stop it first (brain-test.ps1 -Action Stop)" }
  $import = @(docker ps -q --filter "label=cc.brain-import" 2>$null | Where-Object { $_ })
  if ($import.Count -gt 0) { Stop-Research "a JARVIS import is running; wait until it has finished" }
  if ((Get-ComposeFileLines).Count -gt 1) { Stop-Research "COMPOSE_FILE is set more than once in .env; keep one line and run this again" }
}

function Show-Status {
  $values = Read-ComposeEnvFile $envFile
  $entries = @(Split-ComposeFiles $values["COMPOSE_FILE"])
  $listed = @($entries | Where-Object { Test-ResearchEntry $_ }).Count -gt 0
  $secret = [string]$values["SEARXNG_SECRET"]
  Write-Host "=== Web research: search service (SearXNG) ==="
  if ($listed) { Write-Host "Switched on in .env: COMPOSE_FILE=$($entries -join ';')" -ForegroundColor Green }
  else { Write-Host "Not switched on (docker-compose.research.yml is not in COMPOSE_FILE in .env)." }
  if ($secret.Length -ge $ResearchSecretMin) { Write-Host "SEARXNG_SECRET: set ($($secret.Length) characters)" }
  elseif ($secret) { Write-Host "SEARXNG_SECRET: too short ($($secret.Length) characters, at least $ResearchSecretMin): run .\scripts\research.ps1 -RotateSecret, then -Enable" -ForegroundColor Yellow }
  elseif ($listed) { Write-Host "SEARXNG_SECRET: missing: run .\scripts\research.ps1 -Enable" -ForegroundColor Yellow }
  else { Write-Host "SEARXNG_SECRET: not set yet (research.ps1 -Enable creates it)" }
  foreach ($file in @($ResearchComposeFile, "research\searxng\settings.yml", "research\searxng\limiter.toml")) {
    if (Test-Path -LiteralPath (Join-Path $root $file) -PathType Leaf) { Write-Host "$($file): present" } else { Write-Host "$($file): MISSING" -ForegroundColor Yellow }
  }
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Write-Host "Docker: the docker command was not found"; return }
  $null = docker version --format '{{.Server.Version}}' 2>$null
  if ($LASTEXITCODE -ne 0) { Write-Host "Docker: not running"; return }
  $null = Use-ComposeEnv $root
  $project = Get-ComposeProjectName
  $image = Get-PinnedImage
  if ($image) {
    $null = docker image inspect $image 2>$null
    if ($LASTEXITCODE -eq 0) { Write-Host "Image: $image (on this PC)" } else { Write-Host "Image: $image (not on this PC yet; start.ps1 pulls it once research is switched on)" }
  }
  $ids = Get-SearxngContainers $project
  if ($ids.Count -eq 0) { Write-Host "Container: none" }
  foreach ($id in $ids) {
    $state = (docker inspect --format '{{.Name}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}} started {{.State.StartedAt}}' $id 2>$null | Out-String).Trim().TrimStart('/')
    Write-Host "Container: $state"
  }
  $left = Get-ResearchLeftovers $project
  Write-Host "Networks: $(if ($left.Networks.Count) { @(docker network inspect --format '{{.Name}}' $left.Networks 2>$null) -join ', ' } else { 'none' })"
  Write-Host "Cache volume: $(if ($left.Volumes.Count) { $left.Volumes -join ', ' } else { 'none' })"
  if (-not $listed -and ($ids.Count -or $left.Networks.Count -or $left.Volumes.Count)) {
    Write-Host "Left over from earlier: run research.ps1 -Disable to remove them." -ForegroundColor Yellow
  }
  Write-Host "Whether agents may search (off, mock or live) is set by an admin in the dashboard: Research Center, Policy and settings."
}

if ($Status) { Show-Status; exit 0 }

Assert-NothingElseRunning
$values = Read-ComposeEnvFile $envFile
$secret = [string]$values["SEARXNG_SECRET"]
$entries = @(Split-ComposeFiles $values["COMPOSE_FILE"])
$listed = @($entries | Where-Object { Test-ResearchEntry $_ }).Count -gt 0
if ($env:COMPOSE_FILE -and -not ($env:CC_COMPOSE_FILE_SET -and $env:COMPOSE_FILE -ceq $env:CC_COMPOSE_FILE_SET)) {
  Stop-Research "COMPOSE_FILE is set in this PowerShell window, so .env would not be used; close the window and run this in a new one"
}

if ($RotateSecret) {
  $secretLines = (Get-SecretLines).Count
  if ($secretLines -eq 0) { Stop-Research "SEARXNG_SECRET is not set: research.ps1 -Enable creates it" }
  if ($secretLines -gt 1) { Stop-Research ".env sets SEARXNG_SECRET more than once; keep one line and run this again" }
  Assert-EnvWritable
  $wasShort = $secret.Length -lt $ResearchSecretMin  # the scripts then ran without the research file
  $new = New-Secret 32
  Set-Secret $new
  Remove-Variable new
  Write-Host "SEARXNG_SECRET has a new value (not shown)."
  $research = Use-ComposeEnv $root
  if (-not $research.Active) { Write-Host "The search service is not switched on: nothing else to do."; exit 0 }
  docker compose up -d --no-deps searxng
  if ($LASTEXITCODE -ne 0) { Write-Host "SearXNG could not be recreated with the new value; see the message above." -ForegroundColor Red; exit 1 }
  Write-Host "SearXNG was recreated with the new value." -ForegroundColor Green
  if ($wasShort) { Write-Host "The previous value was too short, so the worker may run without the search network: run .\scripts\research.ps1 -Enable to reconnect it." -ForegroundColor Yellow }
  exit 0
}

if ($Enable) {
  foreach ($needed in @($ResearchComposeFile, "research\searxng\settings.yml")) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $needed) -PathType Leaf)) { Stop-Research "$needed is missing; extract the whole package again" }
  }
  if ((Get-SecretLines).Count -gt 1) { Stop-Research ".env sets SEARXNG_SECRET more than once; keep one line and run this again" }
  if ($secret -and $secret.Length -lt $ResearchSecretMin) {
    Stop-Research "SEARXNG_SECRET in .env is shorter than $ResearchSecretMin characters; give it a new value with .\scripts\research.ps1 -RotateSecret, then run -Enable again"
  }
  # Missing (an upgraded .env): created here, 32 random bytes as init-env.ps1 makes it, and written in the same write
  # as COMPOSE_FILE (after the same copy of .env). No stored data depends on it.
  $new = if ($secret) { "" } else { New-Secret 32 }
  if ($new -or -not $listed) { Assert-EnvWritable }
  if ($listed) {
    Write-Host "docker-compose.research.yml is already in COMPOSE_FILE; starting what is missing."
    if ($new) { Set-Secret $new }
  } else {
    if ($entries.Count -eq 0) { $entries = @("docker-compose.yml") }
    $wanted = @($entries) + @($ResearchComposeFile)
    # Checked before .env changes: Compose must accept the files together and list the search service. A secret
    # created above is given to this check only (a value set in this window wins over .env) and removed right after.
    $env:COMPOSE_FILE = $wanted -join ";"
    if ($new) { $env:SEARXNG_SECRET = $new }
    $services = @(docker compose config --services 2>$null)
    $checked = $LASTEXITCODE
    Remove-Item Env:COMPOSE_FILE, Env:SEARXNG_SECRET -ErrorAction SilentlyContinue
    if ($checked -ne 0 -or $services -notcontains "searxng" -or $services -notcontains "worker") {
      Stop-Research "docker compose did not accept COMPOSE_FILE=$($wanted -join ';'); run docker compose config to see why"
    }
    Set-ComposeFile $wanted $new
  }
  if ($new) { Write-Host "Added SEARXNG_SECRET to .env (not shown); no stored data depends on it." }
  Remove-Variable new
  Remove-Item Env:SEARXNG_SECRET -ErrorAction SilentlyContinue  # from here .env is the only source
  $global:LASTEXITCODE = 1  # stays 1 if the script cannot run at all
  & (Join-Path $PSScriptRoot "start.ps1") -NoBuild
  $started = $LASTEXITCODE -eq 0
  Show-TunnelOverlap (Get-ComposeProjectName)
  if (-not $started) { Write-Host "Research's search service is switched on in .env, but the start did not finish; see the messages above." -ForegroundColor Red; exit 1 }
  Write-Host ""
  Write-Host "The search service is switched on. Agents still cannot search: an admin chooses mock or live in the Research Center." -ForegroundColor Green
  exit 0
}

if ($Disable) {
  if ($listed) { Assert-EnvWritable }  # before the container goes
  $null = Use-ComposeEnv $root  # so Compose can read its files even without the secret
  $project = Get-ComposeProjectName
  $ids = Get-SearxngContainers $project
  if ($ids.Count -gt 0) {
    docker stop $ids | Out-Null
    docker rm -v $ids | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Host "The SearXNG container could not be removed; see the message above." -ForegroundColor Red; exit 1 }
    Write-Host "Removed the SearXNG container."
  }
  if ($listed) {
    Set-ComposeFile @($entries | Where-Object { -not (Test-ResearchEntry $_) })
    Remove-Item Env:COMPOSE_FILE, Env:CC_COMPOSE_FILE_SET -ErrorAction SilentlyContinue
  } else {
    Write-Host "docker-compose.research.yml is not in COMPOSE_FILE; removing what is left of it."
  }
  # The worker leaves the research network here (Compose recreates it without that network).
  $global:LASTEXITCODE = 1  # stays 1 if the script cannot run at all
  & (Join-Path $PSScriptRoot "start.ps1") -NoBuild
  $started = $LASTEXITCODE -eq 0
  $left = Get-ResearchLeftovers $project
  $failed = 0
  foreach ($volume in $left.Volumes) {
    docker volume rm $volume | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "Removed the volume $volume." } else { $failed++ }
  }
  foreach ($network in $left.Networks) {
    $name = (docker network inspect --format '{{.Name}}' $network 2>$null | Out-String).Trim()
    docker network rm $network | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "Removed the network $name." } else { $failed++ }
  }
  $image = Get-PinnedImage
  if ($image) { Write-Host "The SearXNG image is kept (about 383 MB). To remove it: docker image rm $image" }
  if (-not $started) { Write-Host "The search service is switched off in .env, but the start did not finish; see the messages above." -ForegroundColor Red; exit 1 }
  if ($failed -gt 0) { Write-Host "$failed research network(s) or volume(s) could not be removed; run research.ps1 -Disable again." -ForegroundColor Red; exit 1 }
  Write-Host ""
  Write-Host "The search service is switched off. Research records in the database are kept (purge them in the Research Center)." -ForegroundColor Green
  exit 0
}
