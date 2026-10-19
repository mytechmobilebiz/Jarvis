# Gets this folder ready for "docker compose build". install.ps1 and start.ps1 run it before they build.
#
# 1. Checks that the folder holds one release: VERSION, backend\app\__init__.py and dashboard\package.json
#    must name the same version (a partly extracted folder, or one mixing two releases, would not).
# 2. Gives every file Docker builds from (backend\ and dashboard\) a fresh modification time. Docker keeps
#    a copy of the files it last built from (shared by every folder of this installation) and skips files
#    whose size and modification time look unchanged, so a new release could be built with old copies of
#    same-size files: the version numbers were, in every release up to 0.5.0, whether it was extracted
#    over the old folder or into a new one. Only the timestamps change, nothing else in the folder.
# 3. Sets CC_RELEASE_VERSION from the VERSION file for this PowerShell session. docker-compose.yml passes
#    it to both builds, which fail if the code they copied is a different version. The callers remove it
#    again after their docker compose command.
#
# Run on its own it only refreshes the timestamps: the version it sets lives in that PowerShell process only.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
try {
  $version = (Get-Content -LiteralPath (Join-Path $root "VERSION") -TotalCount 1).Trim()
  if ($version -notmatch '^\d+\.\d+\.\d+$') { throw "the VERSION file says '$version'" }
  $init = Select-String -LiteralPath (Join-Path $root "backend\app\__init__.py") -Pattern '^__version__ = "([^"]+)"' | Select-Object -First 1
  $backendVersion = if ($init) { $init.Matches[0].Groups[1].Value } else { "(none)" }
  $dashboardVersion = (Get-Content -LiteralPath (Join-Path $root "dashboard\package.json") -Raw | ConvertFrom-Json).version
  if ($backendVersion -ne $version -or $dashboardVersion -ne $version) {
    throw "this folder mixes releases: VERSION says $version, backend\app\__init__.py $backendVersion and dashboard\package.json $dashboardVersion. Extract the zip again into a new, empty folder, copy .env there and run install.ps1 from it"
  }
  $files = foreach ($dir in @("backend", "dashboard")) {
    $base = Join-Path $root $dir
    # Skip what Docker ignores anyway (see the .dockerignore files), judged inside the build folder only.
    Get-ChildItem -LiteralPath $base -Recurse -File -Force |
      Where-Object { $_.FullName.Substring($base.Length) -notmatch '[\\/](node_modules|dist|__pycache__)[\\/]' }
  }
} catch {
  Write-Host "Could not prepare the build folders: $($_.Exception.Message)" -ForegroundColor Red
  exit 1
}
$env:CC_RELEASE_VERSION = $version
$now = [DateTime]::UtcNow
$count = 0
$skipped = @()
foreach ($file in $files) {
  try {
    # Windows PowerShell 5.1 cannot set the time of a read-only file: lift the attribute for the moment.
    $readOnly = $file.IsReadOnly
    if ($readOnly) { $file.IsReadOnly = $false }
    $file.LastWriteTimeUtc = $now
    if ($readOnly) { $file.IsReadOnly = $true }
    $count++
  } catch {
    $skipped += "$($file.FullName) ($($_.Exception.Message))"
  }
}
if ($count -eq 0) {
  $why = if ($skipped.Count) { " First: $($skipped[0])" } else { "" }
  Write-Host "Could not prepare the build folders: no file in backend\ or dashboard\ got a fresh timestamp.$why" -ForegroundColor Red
  exit 1
}
if ($skipped.Count) {
  # The build still checks the version, so a stale version file is caught; other files are very unlikely to matter.
  Write-Host "$($skipped.Count) file(s) could not get a fresh timestamp (in use by another program?), first: $($skipped[0])" -ForegroundColor Yellow
}
Write-Host "Build folders refreshed ($count files) for version $version."
exit 0
