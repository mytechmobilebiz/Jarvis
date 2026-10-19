# Gets this folder ready for "docker compose build". install.ps1 and start.ps1 run it before they build.
#
# 1. Gives every file Docker builds from (backend\ and dashboard\) a fresh modification time. Docker keeps
#    a copy of the files it last built from (shared by every folder of this installation) and skips files
#    whose size and modification time look unchanged, so a new release could be built with old copies of
#    same-size files: the version numbers were, in every release up to 0.5.0, whether it was extracted
#    over the old folder or into a new one. Only the timestamps change, nothing else in the folder.
# 2. Sets CC_RELEASE_VERSION from the VERSION file for this PowerShell session. docker-compose.yml passes
#    it to both builds, which fail if the code they copied is a different version.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\prepare-build.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
try {
  $version = (Get-Content (Join-Path $root "VERSION") -TotalCount 1).Trim()
  if ($version -notmatch '^\d+\.\d+\.\d+$') { throw "the VERSION file says '$version'" }
  $now = [DateTime]::UtcNow
  $count = 0
  foreach ($dir in @("backend", "dashboard")) {
    Get-ChildItem -Path (Join-Path $root $dir) -Recurse -File -Force |
      Where-Object { $_.FullName -notmatch '[\\/](node_modules|dist|__pycache__)[\\/]' } |
      ForEach-Object { $_.LastWriteTimeUtc = $now; $count++ }
  }
} catch {
  Write-Host "Could not prepare the build folders: $($_.Exception.Message)" -ForegroundColor Red
  exit 1
}
$env:CC_RELEASE_VERSION = $version
Write-Host "Build folders refreshed ($count files); building version $version."
exit 0
