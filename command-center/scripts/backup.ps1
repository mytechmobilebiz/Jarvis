# Backs up both databases (Command Center and n8n) to .\backups\<timestamp>\.
# n8n's stored credentials are encrypted with N8N_ENCRYPTION_KEY: keep a copy
# of .env somewhere safe as well, or the n8n backup cannot be decrypted.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup.ps1
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$target = Join-Path $root "backups\$stamp"
New-Item -ItemType Directory -Force -Path $target | Out-Null
foreach ($db in @("command_center", "n8n")) {
  # Dump inside the container and copy the file out: piping binary output
  # through PowerShell would corrupt it.
  docker compose exec -T postgres pg_dump -U postgres -Fc -f "/tmp/$db.dump" $db
  if ($LASTEXITCODE -ne 0) { Write-Host "pg_dump of $db failed; is the stack running?" -ForegroundColor Red; exit 1 }
  docker compose cp "postgres:/tmp/$db.dump" (Join-Path $target "$db.dump")
  if ($LASTEXITCODE -ne 0) { Write-Host "copying the $db dump out failed" -ForegroundColor Red; exit 1 }
  docker compose exec -T postgres rm -f "/tmp/$db.dump"
}
Get-ChildItem $target | Format-Table Name, Length
Write-Host "Backup written to $target. Test a restore before relying on it (docs/OPERATIONS.md)."
exit 0
