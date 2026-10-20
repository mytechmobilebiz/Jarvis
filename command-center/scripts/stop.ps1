# Stops the Command Center containers. Data (PostgreSQL, n8n) is kept.
# This script never runs `down -v`, which would delete the data volumes.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop.ps1
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
. (Join-Path $PSScriptRoot "compose-env.ps1")
$null = Use-ComposeEnv $root
docker compose stop
docker compose ps
