# Read-only pre-check of the live Command Center before (and after) the JARVIS Brain test (spec 6.4).
# It changes nothing in the live installation: it only reads Docker's lists and labels, runs two read-only commands
# in the running containers (the API's version, and counts from the database in a read-only transaction), and
# writes its report to a new file outside the live folder.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\brain-precheck.ps1 [-OutDir C:\InfiniteAI\JarvisMigration]
#
# Run it before the test and again after it. The image IDs, container IDs and names, volume names and applied
# migrations must be the same; start times change after a restart, and counts change with normal use.
# Exit codes: 0 report written and the live installation answers; 1 report written, but something is not healthy;
# 2 not run (nothing was changed or written).
param(
  [string]$OutDir = "C:\InfiniteAI\JarvisMigration"
)
$ErrorActionPreference = "Stop"
$common = Join-Path $PSScriptRoot "brain-common.ps1"
if (-not (Test-Path -LiteralPath $common -PathType Leaf)) {
  Write-Host "NOT RUN: scripts\brain-common.ps1 is missing next to this script; extract the whole package again." -ForegroundColor Red
  Write-Host "Nothing was changed."
  exit 2
}
. $common

# --- Checks before anything runs -----------------------------------------------------------------------------------
Assert-Docker
$OutDir = Get-FullPath $OutDir
$liveFolder = Get-LiveFolder
if ($liveFolder -and (Test-Inside $OutDir $liveFolder)) {
  Stop-Brain "the report folder $OutDir is inside the live installation's folder; choose a folder outside it"
}

# --- Read ----------------------------------------------------------------------------------------------------------
$report = New-Object System.Collections.Generic.List[string]
$problems = 0
function Add-Line([string]$text) { $report.Add($text); Write-Host $text }
function Add-Problem([string]$text) { $script:problems++; $report.Add("PROBLEM: $text"); Write-Host "PROBLEM: $text" -ForegroundColor Red }

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
Add-Line "JARVIS Brain pre-check of the live installation (project $BrainLiveProject), $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz'))"
Add-Line "Read-only: nothing in the live installation was changed by this check."
Add-Line ""

$fingerprint = @(Get-LiveFingerprint)
Add-Line "[fingerprint]"
foreach ($line in $fingerprint) { Add-Line "  $line" }
if ($fingerprint.Count -eq 0) { Add-Line "  (nothing)" }
Add-Line ""

Add-Line "[live folder]"
if ($liveFolder) {
  Add-Line "  folder: $liveFolder"
  $versionFile = Join-Path $liveFolder "VERSION"
  if (Test-Path -LiteralPath $versionFile -PathType Leaf) {
    Add-Line ("  VERSION file: " + (Get-Content -LiteralPath $versionFile -TotalCount 1).Trim())
  } else {
    Add-Line "  VERSION file: not readable from here"
  }
} else {
  Add-Problem "the live installation's api container is not running, so its folder is unknown"
}
Add-Line ""

Add-Line "[containers]"
$containers = @()
foreach ($id in @(Get-ProjectContainers $BrainLiveProject "")) {
  $info = Get-ContainerInfo $id
  if ($info) { $containers += $info }
}
foreach ($c in ($containers | Sort-Object Name)) {
  $health = if ($c.Health) { $c.Health } else { "no healthcheck" }
  Add-Line ("  {0}: {1}, {2}" -f $c.Name, $c.Status, $health)
}
if ($containers.Count -eq 0) { Add-Problem "no container of $BrainLiveProject exists" }
function Find-Service([string]$service) {
  return ($containers | Where-Object { (Get-Label $_ "com.docker.compose.service") -eq $service -and
    (Get-Label $_ "com.docker.compose.oneoff") -ne "True" } | Select-Object -First 1)
}
foreach ($service in @("postgres", "n8n", "api", "dashboard")) {
  $c = Find-Service $service
  if (-not $c) { Add-Problem "no $service container"; continue }
  if ($c.Status -ne "running") { Add-Problem "$service is $($c.Status)" }
  elseif ($c.Health -and $c.Health -ne "healthy") { Add-Problem "$service healthcheck: $($c.Health)" }
  elseif ($service -eq "postgres" -or $service -eq "n8n") { Add-Line "  $service healthcheck passes" }
}
$worker = Find-Service "worker"
if (-not $worker -or $worker.Status -ne "running") { Add-Problem "the worker is not running" }
Add-Line ""

Add-Line "[application]"
$api = Find-Service "api"
if ($api -and $api.Status -eq "running") {
  $r = Invoke-Docker @("exec", $api.Id, "python", "-c", "import app; print(app.__version__)")
  if ($r.Code -eq 0 -and $r.Out.Count -gt 0) { Add-Line ("  API version: " + $r.Out[-1].Trim()) } else { Add-Problem "the API did not report its version" }
}
$pg = Find-Service "postgres"
# Memory items promoted from a message that is withheld now, which still hold text: 0.6.0's withholds left them, and
# 0.6.1 and later empty them when they start (app/brain/archive.py withheld_copies() applies the same rules;
# tests/integration/test_brain_withhold_copies.py runs this query against both).
$withheldCopiesSql = @'
SELECT count(*) FROM memory_items i WHERE i.origin = 'promoted'
AND (NOT i.flags ? 'source_withheld' OR i.title <> 'Withheld: the archived message it was promoted from was withheld'
  OR i.body <> '' OR i.tags::text <> '[]'
  OR EXISTS (SELECT 1 FROM memory_item_versions v WHERE v.item_id = i.id AND (v.body <> '' OR v.tags::text <> '[]'
    OR v.title NOT IN ('[redacted]', 'Withheld: the archived message it was promoted from was withheld'))))
AND (i.flags ? 'source_withheld'
  OR EXISTS (SELECT 1 FROM archive_messages m WHERE m.withheld AND m.id = (i.flags ->> 'message_id')::bigint)
  OR EXISTS (SELECT 1 FROM archive_messages m WHERE m.withheld AND m.source_key = i.flags ->> 'message_source_key')
  OR EXISTS (SELECT 1 FROM brain_tombstones t WHERE t.reason = 'withheld by an admin'
    AND t.source_key = i.flags ->> 'message_source_key')
  OR (NOT i.flags ? 'message_source_key'
    AND NOT EXISTS (SELECT 1 FROM archive_messages m WHERE m.id = (i.flags ->> 'message_id')::bigint)
    AND EXISTS (SELECT 1 FROM archive_messages m JOIN archive_sessions s ON s.id = m.session_id WHERE m.withheld
      AND i.source_ref IN ('JARVIS session ' || s.source_session_id || ' ' || chr(183) || ' message ' || m.position,
        'JARVIS session ' || s.source_session_id || ' ' || chr(183) || ' message ' || m.position || ' (source removed)')))
  OR (NOT i.flags ? 'message_source_key'
    AND NOT EXISTS (SELECT 1 FROM archive_messages m WHERE m.id = (i.flags ->> 'message_id')::bigint)
    AND EXISTS (SELECT 1 FROM import_records r WHERE r.record_type = 'message' AND r.target_table = 'archive_messages'
      AND r.target_id = (i.flags ->> 'message_id')::bigint AND r.action IN ('imported', 'withheld', 'updated')
      AND (EXISTS (SELECT 1 FROM brain_tombstones t WHERE t.reason = 'withheld by an admin' AND t.source_key = r.source_key)
        OR EXISTS (SELECT 1 FROM archive_messages m WHERE m.withheld AND m.source_key = r.source_key))))
  OR EXISTS (SELECT 1 FROM archive_sessions s WHERE s.sensitivity = 'restricted'
    AND s.id = (i.flags ->> 'archive_session_id')::bigint))
'@
if ($pg -and $pg.Status -eq "running") {
  # A read-only transaction: the database itself refuses any change from this session.
  $sql = "SELECT (SELECT string_agg(version, ',' ORDER BY version) FROM schema_migrations), " +
    "(SELECT count(*) FROM agent_state WHERE NOT enabled), " +
    "(SELECT count(*) FROM schedules WHERE enabled), (SELECT count(*) FROM schedules), " +
    "(SELECT count(*) FROM sites), (SELECT count(*) FROM approvals WHERE status = 'pending'), (SELECT count(*) FROM users)"
  $r = Invoke-Docker @("exec", "-e", "PGOPTIONS=-c default_transaction_read_only=on", $pg.Id, "psql", "-U", "postgres",
    "-d", "command_center", "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-c", $sql)
  $row = @($r.Out | Where-Object { $_ -match '\|' } | Select-Object -Last 1)
  if ($r.Code -eq 0 -and $row.Count -eq 1) {
    $v = $row[0].Trim().Split('|')
    $migrations = $v[0].Split(',')
    Add-Line ("  schema_migrations: " + ($migrations -join ' '))
    $brain = if ($migrations -contains "0006_brain") { "present" } else { "not present (the live installation is older than 0.6.0)" }
    Add-Line "  0006_brain: $brain"
    $withhold = if ($migrations -contains "0007_brain_withhold") { "present" } else { "not present (the live installation is older than 0.6.1)" }
    Add-Line "  0007_brain_withhold: $withhold"
    if ($migrations -contains "0006_brain") {
      $c = Invoke-Docker @("exec", "-e", "PGOPTIONS=-c default_transaction_read_only=on", $pg.Id, "psql", "-U", "postgres",
        "-d", "command_center", "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-c", $withheldCopiesSql.Replace("`r", " ").Replace("`n", " "))
      $n = @($c.Out | ForEach-Object { $_.Trim() } | Where-Object { $_ -match '^\d+$' } | Select-Object -Last 1)
      if ($c.Code -eq 0 -and $n.Count -eq 1) {
        $after = if ($n[0] -ne "0") { " (0.6.1 and later empty them when they start; a production import refuses until this is 0)" } else { "" }
        Add-Line ("  promoted memory items whose source message is withheld, still holding text: " + $n[0] + $after)
      } else {
        Add-Problem ("the promoted memory items of withheld messages could not be counted: " + (($c.Err | Select-Object -First 2) -join " "))
      }
    }
    # 0.6.2: what the withhold identity repair would still change (read-only: brain_withhold_pending() is STABLE and
    # only counts; 0008_brain_withhold_identity.sql).
    if ($migrations -contains "0008_brain_withhold_identity") {
      Add-Line "  0008_brain_withhold_identity: present"
      $p = Invoke-Docker @("exec", "-e", "PGOPTIONS=-c default_transaction_read_only=on", $pg.Id, "psql", "-U", "postgres",
        "-d", "command_center", "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-c", "SELECT brain_withhold_pending()")
      $j = @($p.Out | ForEach-Object { $_.Trim() } | Where-Object { $_ -match '^\{' } | Select-Object -Last 1)
      if ($p.Code -eq 0 -and $j.Count -eq 1) {
        Add-Line ("  withhold identity (counts only): " + $j[0])
        try { $pending = $j[0] | ConvertFrom-Json } catch { $pending = $null }
        if ($pending) {
          # The live installation's next start (or jarvis-import.ps1 -Mode Repair) withholds the readable rows; an
          # import repairs before it imports. Held rows wait for an admin (JARVIS Brain page, Held for review).
          $readable = [int]$pending.readable_by_key + [int]$pending.readable_by_jarvis_row + [int]$pending.readable_by_fold +
            [int]$pending.readable_in_restricted_sessions
          $repair = if ($pending.repair_done -eq $true) { "done" } else { "not done (the next start runs it)" }
          Add-Line ("  readable rows matching a withhold: $readable; held for review: $([int]$pending.held_for_review); " +
            "identity repair $repair")
          # 0.6.2 plan 1.8, 9.4: decisions not finished on their backup (Settle), Settle's holds waiting for review,
          # rows a later backup held, conversations an admin stopped importing
          Add-Line ("  withhold decisions not finished (Settle): $([int]$pending.unsettled_decisions); found when finishing " +
            "decisions, waiting for review: $([int]$pending.unreviewed_settle_holds); new in a later backup, held: " +
            "$([int]$pending.quarantined_rows); conversations not imported (stopped by an admin): $([int]$pending.frozen_sessions)")
          if ($pending.legacy_lock -eq $true) {
            # 0.6.2 plan 8.1: data that 0.6.0 or 0.6.1 imported is hidden from every reader until it is imported again
            Add-Line ("  legacy JARVIS Brain data (Command Center 0.6.0 or 0.6.1): LOCKED, hidden from every reader, " +
              "imports refused; reset it with .\scripts\jarvis-import.ps1 -Target Production -Mode LegacyReset")
          }
        }
      } else {
        Add-Problem ("the withhold identity could not be read: " + (($p.Err | Select-Object -First 2) -join " "))
      }
    } else {
      Add-Line "  0008_brain_withhold_identity: not present (0.6.2 not installed)"
    }
    Add-Line "  agents switched off: $($v[1]) (agents without a saved state are on)"
    Add-Line "  schedules enabled: $($v[2]) of $($v[3])"
    Add-Line "  sites: $($v[4])"
    Add-Line "  pending approvals: $($v[5])"
    Add-Line "  users: $($v[6])"
  } else {
    Add-Problem ("the database counts could not be read: " + (($r.Err | Select-Object -First 2) -join " "))
  }
}
Add-Line ""
$verdict = if ($problems -eq 0) { "RESULT: the live installation answers; nothing was changed." } else { "RESULT: $problems problem(s) above; nothing was changed." }
Add-Line $verdict

# --- Write the report (the only file this script writes) ------------------------------------------------------------
if (-not (Test-Path -LiteralPath $OutDir)) { $null = New-Item -ItemType Directory -Path $OutDir }
$file = Join-Path $OutDir "precheck-$stamp.txt"
[IO.File]::WriteAllText($file, (($report -join "`r`n") + "`r`n"), (New-Object System.Text.ASCIIEncoding))
Write-Host ""
Write-Host "Report written: $file" -ForegroundColor Green
if ($problems -eq 0) { exit 0 } else { exit 1 }
