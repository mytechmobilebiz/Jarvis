# Backs up the databases (Command Center, n8n, Credit Repair) to .\backups\<timestamp>\.
# Stored credentials and Credit Repair fields are encrypted with keys from .env
# (N8N_ENCRYPTION_KEY, CC_SECRETS_KEY, CREDIT_REPAIR_DATA_KEY): keep a copy of
# .env somewhere safe as well, or those parts of a backup cannot be decrypted.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup.ps1
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$target = Join-Path $root "backups\$stamp"
New-Item -ItemType Directory -Force -Path $target | Out-Null
$databases = @("command_center", "n8n")
# The Credit Repair database exists from version 0.2.0 on.
$hasCreditRepair = docker compose exec -T postgres psql -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname = 'credit_repair'"
if ($LASTEXITCODE -ne 0) { Write-Host "the database is not reachable; is the stack running?" -ForegroundColor Red; exit 1 }
if ("$hasCreditRepair".Trim() -eq "1") { $databases += "credit_repair" }
foreach ($db in $databases) {
  # Dump inside the container and copy the file out: piping binary output
  # through PowerShell would corrupt it.
  docker compose exec -T postgres pg_dump -U postgres -Fc -f "/tmp/$db.dump" $db
  if ($LASTEXITCODE -ne 0) { Write-Host "pg_dump of $db failed; is the stack running?" -ForegroundColor Red; exit 1 }
  docker compose cp "postgres:/tmp/$db.dump" (Join-Path $target "$db.dump")
  if ($LASTEXITCODE -ne 0) { Write-Host "copying the $db dump out failed" -ForegroundColor Red; exit 1 }
  docker compose exec -T postgres rm -f "/tmp/$db.dump"
}
Get-ChildItem $target | Format-Table Name, Length
Write-Host "Backup written to $target. Restore with: .\scripts\restore.ps1 -BackupFolder `"$target`""
exit 0
