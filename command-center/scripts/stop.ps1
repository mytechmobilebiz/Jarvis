# Stops the Command Center containers. Data (PostgreSQL, n8n) is kept.
# This script never runs `down -v`, which would delete the data volumes.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop.ps1
Set-Location (Split-Path -Parent $PSScriptRoot)
docker compose stop
docker compose ps
