# Shared by brain-precheck.ps1, brain-test.ps1 and jarvis-import.ps1 (dot-sourced; it runs nothing by itself).
# Windows PowerShell 5.1 and PowerShell 7. Every check that refuses calls Stop-Brain before anything has changed.
#
# The live installation is the Compose project "infinite-command-center". These helpers only read it: image IDs,
# container IDs and start times, volume names and labels. The JARVIS Brain test copy is the project "cc-brain-test".

$BrainLiveProject = "infinite-command-center"
$BrainTestProject = "cc-brain-test"
$BrainTestImagePrefix = "infinite-command-center-brain-test/"
$BrainLiveImages = @("infinite-command-center/backend:local", "infinite-command-center/dashboard:local")
$BrainTestImages = @("infinite-command-center-brain-test/backend:local", "infinite-command-center-brain-test/dashboard:local")
$BrainMinCompose = New-Object Version 2, 24, 4
$BrainTestCookie = "cc_session_brain_test"
$BrainTestLabel = "cc-brain-test"

# Docker prints UTF-8. Windows PowerShell 5.1 reads a program's output in the console's code page unless told
# otherwise, which would garble a folder name with accents in a label. (No console, as in the ISE: left as it is.)
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }

function Stop-Brain([string]$message) {
  Write-Host "NOT RUN: $message" -ForegroundColor Red
  Write-Host "Nothing was changed."
  exit 2
}

function Invoke-Native([string]$exe, [string[]]$arguments) {
  # Runs a program and returns its exit code, standard output lines and error lines. Windows PowerShell 5.1 turns a
  # native program's redirected error output into error records (terminating under "Stop"), so they are sorted here.
  $ErrorActionPreference = "Continue"
  $out = New-Object System.Collections.Generic.List[string]
  $err = New-Object System.Collections.Generic.List[string]
  if ($null -eq $arguments) { $arguments = @() }
  & $exe @arguments 2>&1 | ForEach-Object {
    if ($_ -is [System.Management.Automation.ErrorRecord]) { $err.Add($_.ToString()) } else { $out.Add([string]$_) }
  }
  $code = $LASTEXITCODE
  return New-Object PSObject -Property @{ Code = $code; Out = $out.ToArray(); Err = $err.ToArray() }
}

function Invoke-Docker([string[]]$arguments) { return Invoke-Native "docker" $arguments }

function Invoke-DockerLive([string[]]$arguments) {
  # For long commands (build, up, down): the output goes straight to the console; only the exit code comes back.
  $ErrorActionPreference = "Continue"
  & docker @arguments | Out-Host
  return $LASTEXITCODE
}

function Get-FullPath([string]$path) {
  # Relative to the PowerShell location, as the user typed it.
  try {
    $resolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($path)
    $full = [IO.Path]::GetFullPath($resolved)
    $root = [IO.Path]::GetPathRoot($full)
    if ($full.Length -gt $root.Length) { $full = $full.TrimEnd('\', '/') }
    return $full
  } catch {
    Stop-Brain "the path $path cannot be used ($($_.Exception.GetType().Name))"
  }
}

function ConvertTo-PathKey([string]$path) {
  # Comparable form of a path: one separator, no trailing separator, case ignored by the callers.
  if (-not $path) { return "" }
  return $path.Replace('/', '\').TrimEnd('\')
}

function Test-SameFolder([string]$a, [string]$b) {
  if (-not $a -or -not $b) { return $false }
  return [string]::Equals((ConvertTo-PathKey $a), (ConvertTo-PathKey $b), [StringComparison]::OrdinalIgnoreCase)
}

function Test-Inside([string]$path, [string]$folder) {
  # True when $path is $folder or anything below it.
  if (-not $path -or -not $folder) { return $false }
  $p = (ConvertTo-PathKey $path) + '\'
  $f = (ConvertTo-PathKey $folder) + '\'
  return $p.StartsWith($f, [StringComparison]::OrdinalIgnoreCase)
}

function Read-EnvFile([string]$path) {
  $values = @{}
  if ($path -and (Test-Path -LiteralPath $path -PathType Leaf)) {
    foreach ($line in Get-Content -LiteralPath $path) {
      if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        $value = $Matches[2].Trim()
        if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
          $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$Matches[1]] = $value
      }
    }
  }
  return $values
}

function Test-SecretKey([string]$key) {
  # The keys whose values are secrets (as scripts/build-package.py judges them).
  return ($key -match '(PASSWORD|TOKEN|SECRET|_KEY)$')
}

function Assert-Docker {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Stop-Brain "the docker command was not found; install or start Docker Desktop" }
  $r = Invoke-Docker @("version", "--format", "{{.Server.Version}}")
  if ($r.Code -ne 0) { Stop-Brain "Docker is not running; start Docker Desktop and try again" }
}

function Assert-ComposeVersion {
  # Docker Desktop adds suffixes ("2.29.1-desktop.1"); only the three numbers count.
  $r = Invoke-Docker @("compose", "version", "--short")
  if ($r.Code -ne 0) { Stop-Brain "docker compose is not available; update Docker Desktop" }
  $text = ($r.Out -join " ").Trim()
  if ($text -notmatch '(\d+)\.(\d+)\.(\d+)') { Stop-Brain "could not read the Docker Compose version from '$text'" }
  $found = New-Object Version ([int]$Matches[1]), ([int]$Matches[2]), ([int]$Matches[3])
  if ($found -lt $BrainMinCompose) {
    Stop-Brain "Docker Compose $found is older than $BrainMinCompose, which this needs; update Docker Desktop"
  }
  return $found
}

function Get-ProjectContainers([string]$project, [string]$service, [switch]$Running) {
  $arguments = @("ps", "-q", "--no-trunc", "--filter", "label=com.docker.compose.project=$project")
  if (-not $Running) { $arguments = @("ps", "-a", "-q", "--no-trunc", "--filter", "label=com.docker.compose.project=$project") }
  if ($service) { $arguments += @("--filter", "label=com.docker.compose.service=$service") }
  $r = Invoke-Docker $arguments
  if ($r.Code -ne 0) { throw "docker ps failed" }
  return @($r.Out | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Get-ContainerInfo([string]$id) {
  # Name, image ID, the image name it was created from, labels, state and health of one container, or $null.
  $r = Invoke-Docker @("inspect", "--format", "{{json .}}", $id)
  if ($r.Code -ne 0 -or -not $r.Out) { return $null }
  try { $c = ($r.Out -join "`n") | ConvertFrom-Json } catch { return $null }
  $health = ""
  if ($c.State.Health) { $health = [string]$c.State.Health.Status }
  return New-Object PSObject -Property @{
    Id = [string]$c.Id; Name = ([string]$c.Name).TrimStart('/'); Image = [string]$c.Image; Labels = $c.Config.Labels
    ImageRef = [string]$c.Config.Image
    Status = [string]$c.State.Status; Health = $health; StartedAt = [string]$c.State.StartedAt
  }
}

function Get-Label($info, [string]$name) {
  if (-not $info -or -not $info.Labels) { return "" }
  $p = $info.Labels.PSObject.Properties[$name]
  if ($p) { return [string]$p.Value }
  return ""
}

function Get-LiveApi {
  # The running api container of the live installation, or $null.
  $ids = @(Get-ProjectContainers $BrainLiveProject "api" -Running)
  if ($ids.Count -eq 0) { return $null }
  return Get-ContainerInfo $ids[0]
}

function Get-LiveFolder {
  # The folder the live installation's containers were created from (Compose's working_dir label), or "".
  $api = Get-LiveApi
  if (-not $api) { return "" }
  return Get-Label $api "com.docker.compose.project.working_dir"
}

function Get-LiveFingerprint {
  # Image IDs of the live images; name, ID, image ID and start time of every container of the live project; its
  # volume names. Read-only. A container that disappears between listing and reading is read again.
  # Web research's search service (SearXNG, when switched on) may restart by itself: its start time is left out, its
  # container ID, image ID and configuration are not (a changed SearXNG is still a change).
  for ($attempt = 1; $attempt -le 3; $attempt++) {
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($image in $BrainLiveImages) {
      $r = Invoke-Docker @("image", "inspect", "--format", "{{.Id}}", $image)
      $id = "(none)"
      if ($r.Code -eq 0 -and $r.Out.Count -gt 0) { $id = $r.Out[0].Trim() }
      $lines.Add("image $image $id")
    }
    $ids = @(Get-ProjectContainers $BrainLiveProject "")
    $searxng = @(Get-ProjectContainers $BrainLiveProject "searxng")
    $complete = $true
    if ($ids.Count -gt 0) {
      $r = Invoke-Docker (@("inspect", "--format", "{{.Name}} {{.Id}} {{.Image}} {{.State.StartedAt}}") + $ids)
      if ($r.Code -ne 0) { $complete = $false }
      foreach ($line in $r.Out) {
        $line = $line.Trim()
        if (-not $line) { continue }
        $parts = $line.Split(" ")
        if ($parts.Count -eq 4 -and $searxng -contains $parts[1]) {
          $line = "{0} {1} {2} (start time not compared: web research's search service)" -f $parts[0], $parts[1], $parts[2]
          Write-Host "Fingerprint: the start time of $($parts[0].TrimStart('/')) is left out (it may restart by itself)."
        }
        $lines.Add("container " + $line.TrimStart('/'))
      }
    }
    $r = Invoke-Docker @("volume", "ls", "-q", "--filter", "label=com.docker.compose.project=$BrainLiveProject")
    if ($r.Code -ne 0) { throw "docker volume ls failed" }
    foreach ($v in $r.Out) { if ($v.Trim()) { $lines.Add("volume " + $v.Trim()) } }
    if ($complete) { return @($lines | Sort-Object) }
    Start-Sleep -Seconds 2
  }
  throw "the live installation's containers could not be read"
}

function Show-Fingerprint([string]$title, [string[]]$lines) {
  Write-Host "--- $title ---"
  foreach ($line in $lines) { Write-Host "  $line" }
  if ($lines.Count -eq 0) { Write-Host "  (nothing)" }
}

function Assert-SameFingerprint([string[]]$before, [string[]]$after) {
  if (($before -join "`n") -ceq ($after -join "`n")) {
    Write-Host "Live installation fingerprint unchanged ($($after.Count) entries)." -ForegroundColor Green
    return
  }
  Write-Host ""
  Write-Host "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" -ForegroundColor Red
  Write-Host "!!  THE LIVE INSTALLATION (infinite-command-center) CHANGED DURING THIS RUN  !!" -ForegroundColor Red
  Write-Host "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" -ForegroundColor Red
  foreach ($line in $before) { if ($after -cnotcontains $line) { Write-Host "  before only: $line" -ForegroundColor Red } }
  foreach ($line in $after) { if ($before -cnotcontains $line) { Write-Host "  after only:  $line" -ForegroundColor Red } }
  Write-Host "Check it with .\scripts\status.ps1 in the live folder, and send this output." -ForegroundColor Red
  exit 1
}

function Confirm-Typed([string]$phrase, [string]$what) {
  Write-Host $what -ForegroundColor Yellow
  $answer = Read-Host "Type $phrase to continue"
  if ([string]$answer -cne $phrase) {
    Write-Host "Cancelled: that was not $phrase." -ForegroundColor Red
    Write-Host "Nothing was changed."
    exit 2
  }
}

function Test-PortFree([int]$port) {
  $listener = New-Object System.Net.Sockets.TcpListener ([System.Net.IPAddress]::Loopback), $port
  try { $listener.Start(); $listener.Stop(); return $true } catch { return $false }
}

function Get-TestConfig([string[]]$compose) {
  # The merged configuration of the test copy as Compose sees it (values from .env.brain-test included: kept in
  # memory, never printed).
  $r = Invoke-Docker ($compose + @("config", "--format", "json"))
  if ($r.Code -ne 0) {
    $why = ($r.Err | Where-Object { $_ -notmatch 'level=warn' } | Select-Object -First 3) -join " "
    Stop-Brain "docker compose rejected the test copy's configuration: $why"
  }
  $text = $r.Out -join "`n"
  $start = $text.IndexOf('{')
  if ($start -lt 0) { Stop-Brain "docker compose printed no configuration" }
  try { return ($text.Substring($start) | ConvertFrom-Json) } catch { Stop-Brain "could not read the configuration docker compose printed" }
}

function Get-TestConfigProblems($config, [int]$port) {
  # Everything that keeps the test copy apart from the live installation (spec 6.2 and 6.3).
  $problems = @()
  if ([string]$config.name -ne $BrainTestProject) { $problems += "the project name is '$($config.name)', not $BrainTestProject" }
  $services = @()
  if ($config.services) { $services = @($config.services.PSObject.Properties | ForEach-Object { $_.Name }) }
  foreach ($needed in @("postgres", "db-init", "api", "worker", "dashboard")) {
    if ($services -notcontains $needed) { $problems += "the service $needed is missing" }
  }
  if ($services -contains "n8n") { $problems += "n8n is part of it" }
  $published = @()
  foreach ($name in $services) {
    $svc = $config.services.$name
    $image = [string]$svc.image
    if ($image.StartsWith("infinite-command-center/")) { $problems += "$name uses the live image $image" }
    if (($svc.build -or @("api", "worker", "dashboard") -contains $name) -and -not $image.StartsWith($BrainTestImagePrefix)) {
      $problems += "$name uses the image '$image' (not under $BrainTestImagePrefix)"
    }
    if ([string]$svc.restart -ne "no") { $problems += "$name restarts by itself (restart: $($svc.restart))" }
    foreach ($p in @($svc.ports)) {
      if ($null -ne $p) { $published += ("{0}:{1}:{2}:{3}" -f $name, $p.host_ip, $p.published, $p.target) }
    }
  }
  $expected = "dashboard:127.0.0.1:$($port):8080"
  if ($published.Count -ne 1 -or $published[0] -ne $expected) {
    $problems += "published ports are [$($published -join ', ')], expected only 127.0.0.1:$port on the dashboard"
  }
  if ($services -contains "worker") {
    $nets = @()
    if ($config.services.worker.networks) { $nets = @($config.services.worker.networks.PSObject.Properties | ForEach-Object { $_.Name }) }
    if ($nets.Count -ne 1 -or $nets[0] -ne "internal") { $problems += "the worker's networks are [$($nets -join ', ')], not only internal" }
  }
  $wanted = @{ WORKFLOW_ENGINE = "none"; LLM_PROVIDER = "rule_based"; ANTHROPIC_API_KEY = ""; N8N_EDITOR_URL = "";
               CC_INSTANCE_LABEL = $BrainTestLabel; SESSION_COOKIE_NAME = $BrainTestCookie }
  foreach ($name in @("api", "worker")) {
    if ($services -notcontains $name) { continue }
    $vars = $config.services.$name.environment
    foreach ($key in $wanted.Keys) {
      $value = $null
      if ($vars -and $vars.PSObject.Properties[$key]) { $value = [string]$vars.PSObject.Properties[$key].Value }
      if ($null -eq $value -or $value -cne $wanted[$key]) { $problems += "$name has $key set otherwise" }
    }
  }
  return $problems
}

function Get-TestCompose([string]$root, [switch]$Import) {
  # The one way this package calls Compose for the test copy. No COMPOSE_* variable is ever set.
  $arguments = @("compose", "-p", $BrainTestProject, "-f", (Join-Path $root "docker-compose.yml"),
    "-f", (Join-Path $root "docker-compose.brain-test.yml"))
  if ($Import) { $arguments += @("-f", (Join-Path $root "docker-compose.brain-import.yml")) }
  $arguments += @("--env-file", (Join-Path $root ".env.brain-test"))
  return $arguments
}

function Assert-NotLiveFolder([string]$root) {
  # The test copy never runs from the live installation's folder.
  if (Test-Path -LiteralPath (Join-Path $root ".env")) {
    Stop-Brain "this folder has a .env, so it is (or was copied from) the live installation: $root. Extract the new version into a new, empty folder (for example C:\InfiniteAI\v<version>-brain-test\InfiniteAI-CommandCenter, with the version you test) and run this from there"
  }
  $live = Get-LiveFolder
  if ($live -and (Test-SameFolder $live $root)) {
    Stop-Brain "this is the live installation's folder ($live); run the test copy from its own folder"
  }
  return $live
}

function Assert-TestSecretsDiffer([string]$testEnv, [string]$liveFolder) {
  # No secret of the test copy may equal the live installation's (that would let one open the other's data).
  if (-not $liveFolder) {
    Write-Host "The live installation is not running, so its .env could not be found to compare secrets with." -ForegroundColor Yellow
    return
  }
  $liveEnvPath = Join-Path $liveFolder ".env"
  if (-not (Test-Path -LiteralPath $liveEnvPath -PathType Leaf)) {
    Write-Host "The live installation's .env was not found at $liveEnvPath; secrets not compared." -ForegroundColor Yellow
    return
  }
  $live = Read-EnvFile $liveEnvPath
  $test = Read-EnvFile $testEnv
  $same = @()
  foreach ($key in $test.Keys) {
    if ((Test-SecretKey $key) -and $test[$key] -and $live.ContainsKey($key) -and $test[$key] -ceq $live[$key]) { $same += $key }
  }
  if ($same.Count -gt 0) {
    Stop-Brain ".env.brain-test shares secrets with the live installation's .env ($(($same | Sort-Object) -join ', ')); delete .env.brain-test and start again so fresh ones are made"
  }
  Write-Host "Test copy secrets differ from the live installation's ($($test.Keys.Count) keys compared by name)."
}

function New-DatabaseDump([string]$container, [string]$folder, [string]$name) {
  # pg_dump -Fc of the command_center database only, inside the given postgres container; checked with pg_restore -l
  # (it must list table data, schema_migrations included), copied out to $folder\$name and compared by SHA-256 with
  # the file in the container, which is removed whatever happens. Throws when any step fails.
  $inside = "/tmp/$name"
  if (-not (Test-Path -LiteralPath $folder)) { $null = New-Item -ItemType Directory -Path $folder }
  $file = Join-Path $folder $name
  try {
    $r = Invoke-Docker @("exec", $container, "pg_dump", "-U", "postgres", "-Fc", "-f", $inside, "command_center")
    if ($r.Code -ne 0) { throw "pg_dump failed: $(($r.Err | Select-Object -First 2) -join ' '); nothing was imported" }
    $listing = Invoke-Docker @("exec", $container, "pg_restore", "-l", $inside)
    $hash = Invoke-Docker @("exec", $container, "sha256sum", $inside)
    $copied = Invoke-Docker @("cp", ($container + ":" + $inside), $file)
  } finally {
    $null = Invoke-Docker @("exec", $container, "rm", "-f", $inside)
  }
  $tables = @($listing.Out | Where-Object { $_ -match ' TABLE DATA public ' })
  if ($listing.Code -ne 0 -or $tables.Count -eq 0 -or @($tables | Where-Object { $_ -match ' TABLE DATA public schema_migrations ' }).Count -ne 1) {
    throw "the dump did not pass pg_restore -l; nothing was imported"
  }
  if ($copied.Code -ne 0 -or -not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "the dump could not be copied to $file; nothing was imported" }
  $sha = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLower()
  $insideSha = ""
  if ($hash.Code -eq 0 -and $hash.Out.Count -gt 0) { $insideSha = ($hash.Out[0] -split '\s+')[0].ToLower() }
  if ($sha -ne $insideSha) { throw "the copied dump differs from the one in the container; nothing was imported" }
  return New-Object PSObject -Property @{ File = $file; Sha256 = $sha; Tables = $tables.Count }
}
