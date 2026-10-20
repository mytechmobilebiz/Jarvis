# Shared by the scripts that run docker compose in this folder (dot-sourced; it runs nothing by itself).
# Windows PowerShell 5.1 and PowerShell 7.
#
# COMPOSE_FILE in .env lists the Compose files in use, separated by ';' (docker-compose.research.yml while web
# research is switched on with scripts\research.ps1, docker-compose.tunnel.yml for remote access). Compose reads every
# listed file for every command, and the research file needs SEARXNG_SECRET: without it, even "docker compose ps" or
# backup.ps1 would fail. So when the research file is listed but the secret is missing or shorter than 32 characters,
# Use-ComposeEnv leaves that file out for this script's process only ($env:COMPOSE_FILE) and says so; .env is not
# changed. Everything else (the core services, the tunnel) runs as before, and the gateway reports SearXNG unreachable.

$ResearchComposeFile = "docker-compose.research.yml"
$ResearchSecretMin = 32
$ComposeDefaultProject = "infinite-command-center"

function Read-ComposeEnvFile([string]$path) {
  # KEY=value lines of .env, also written "export KEY=value" as Compose accepts them (a value in matching quotes
  # loses them), or an empty table. research.ps1 rewrites the same lines, so both read every line alike.
  $values = @{}
  if ($path -and (Test-Path -LiteralPath $path -PathType Leaf)) {
    foreach ($line in Get-Content -LiteralPath $path) {
      if ($line -match '^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
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

function Split-ComposeFiles([string]$value) {
  # The entries of a COMPOSE_FILE value, in order, without blanks.
  if (-not $value) { return @() }
  return @($value.Split(";") | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Test-ResearchEntry([string]$entry) {
  # docker-compose.research.yml, also written as .\docker-compose.research.yml or with a folder in front.
  return ($entry -match '(^|[\\/])docker-compose\.research\.yml$')
}

function Use-ComposeEnv([string]$root) {
  # Decides which Compose files this process uses. Returns Listed (the research file is in COMPOSE_FILE) and Active
  # (it is used): Listed but not Active means the secret is missing or too short. Prints a warning, never the secret.
  if ($env:COMPOSE_FILE -and $env:CC_COMPOSE_FILE_SET -and $env:COMPOSE_FILE -ceq $env:CC_COMPOSE_FILE_SET) {
    # Set by an earlier script in this same window: decide again from .env.
    Remove-Item Env:COMPOSE_FILE, Env:CC_COMPOSE_FILE_SET -ErrorAction SilentlyContinue
  }
  $values = Read-ComposeEnvFile (Join-Path $root ".env")
  $current = $values["COMPOSE_FILE"]
  if ($env:COMPOSE_FILE) { $current = $env:COMPOSE_FILE }  # a COMPOSE_FILE set in this window wins over .env, as in Compose
  $entries = @(Split-ComposeFiles $current)
  $listed = @($entries | Where-Object { Test-ResearchEntry $_ }).Count -gt 0
  $secret = [string]$values["SEARXNG_SECRET"]
  $active = $listed -and $secret.Length -ge $ResearchSecretMin
  if ($listed -and -not $active) {
    $kept = @($entries | Where-Object { -not (Test-ResearchEntry $_) })
    if ($kept.Count -eq 0) { $kept = @("docker-compose.yml") }
    $env:COMPOSE_FILE = $kept -join ";"
    $env:CC_COMPOSE_FILE_SET = $env:COMPOSE_FILE
    # -Enable adds a missing secret but refuses a short one; -RotateSecret replaces it, and -Enable then reconnects the
    # worker (started without the research network while the secret was unusable).
    $problem = "missing"
    $fix = ".\scripts\research.ps1 -Enable (it adds the secret)"
    if ($secret.Length -gt 0) {
      $problem = "shorter than $ResearchSecretMin characters"
      $fix = ".\scripts\research.ps1 -RotateSecret, then .\scripts\research.ps1 -Enable"
    }
    Write-Host ("Web research is switched on in .env (COMPOSE_FILE), but SEARXNG_SECRET is $($problem): this script " +
      "runs without the research search service (SearXNG). To fix it, run $fix; .env was not changed.") -ForegroundColor Yellow
  }
  return New-Object PSObject -Property @{ Listed = $listed; Active = $active }
}

function Get-ComposeProjectName {
  # The Compose project of this folder (its "name:", or COMPOSE_PROJECT_NAME), as docker compose resolves it.
  $text = (docker compose config --format json 2>$null | Out-String)
  $start = $text.IndexOf('{')
  if ($LASTEXITCODE -eq 0 -and $start -ge 0) {
    try {
      $name = [string]($text.Substring($start) | ConvertFrom-Json).name
      if ($name -match '^[a-z0-9][a-z0-9_-]*$') { return $name }
    } catch { }
  }
  return $ComposeDefaultProject
}
