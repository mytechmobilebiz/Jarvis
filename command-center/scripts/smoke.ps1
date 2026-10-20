# Verifies a running Command Center end to end on this PC and prints PASS/FAIL.
# It signs in, checks that the running API and dashboard are the version of
# this folder, checks the worker, sends a test event through n8n and waits for
# n8n's acknowledgement, and runs one supervisor request. Web research is reported for information only
# (off, mock, or the search service's state): it fails only when the research API does not answer. It prints no
# secrets.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1

$root = Split-Path -Parent $PSScriptRoot
$envValues = @{}
foreach ($line in Get-Content (Join-Path $root ".env")) { if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $envValues[$Matches[1]] = $Matches[2].Trim() } }
$port = if ($envValues["DASHBOARD_PORT"]) { $envValues["DASHBOARD_PORT"] } else { "8090" }
$n8nPort = if ($envValues["N8N_PORT"]) { $envValues["N8N_PORT"] } else { "5680" }
$base = "http://127.0.0.1:$port"
$failures = 0
$headers = @{ "X-Requested-With" = "command-center" }
# One session object for every call, so the sign-in cookie is reused.
$web = New-Object Microsoft.PowerShell.Commands.WebRequestSession

function Check($name, [scriptblock]$test) {
  try {
    $detail = & $test
    Write-Host "PASS  $name  $detail" -ForegroundColor Green
  } catch {
    Write-Host "FAIL  $name  $($_.Exception.Message)" -ForegroundColor Red
    $script:failures++
  }
}

function Wait-Until([scriptblock]$probe, [int]$seconds, $what) {
  $deadline = (Get-Date).AddSeconds($seconds)
  while ((Get-Date) -lt $deadline) {
    $value = & $probe
    if ($value) { return $value }
    Start-Sleep -Seconds 2
  }
  throw "timed out after $seconds s waiting for $what"
}

Check "API health" { (Invoke-RestMethod "$base/api/health").status }
Check "n8n readiness" { (Invoke-WebRequest "http://127.0.0.1:$n8nPort/healthz/readiness" -UseBasicParsing).StatusCode }
Check "sign in" {
  $body = @{ email = $envValues["ADMIN_EMAIL"]; password = $envValues["ADMIN_PASSWORD"] } | ConvertTo-Json
  $r = Invoke-RestMethod "$base/api/auth/login" -Method Post -ContentType "application/json" -Body $body -WebSession $web
  "as $($r.user.email) ($($r.user.role))"
}
Check "versions match this folder" {
  $expected = (Get-Content (Join-Path $root "VERSION") -TotalCount 1).Trim()
  $apiVersion = (Invoke-RestMethod "$base/api/system/status" -WebSession $web).version
  $dashboardVersion = (Invoke-RestMethod "$base/version.json").version
  if ($apiVersion -ne $expected -or $dashboardVersion -ne $expected) {
    throw "this folder is $expected, but the API reports $apiVersion and the dashboard $dashboardVersion. Run install.ps1 from this folder again (it rebuilds from fresh copies of the files); if this stays, send the output of status.ps1"
  }
  "API $apiVersion, dashboard $dashboardVersion"
}
Check "worker running" {
  $s = Invoke-RestMethod "$base/api/system/status" -WebSession $web
  $alive = @($s.workers | Where-Object { $_.alive })
  if (-not $alive) { throw "no worker has reported in the last 2 minutes" }
  "planner: $($s.llm.provider)"
}
Check "event round trip through n8n" {
  $e = (Invoke-RestMethod "$base/api/integrations/n8n/test" -Method Post -ContentType "application/json" -Body "{}" -Headers $headers -WebSession $web).event
  $acked = Wait-Until { $x = (Invoke-RestMethod "$base/api/events/$($e.id)" -WebSession $web).event; if ($x.n8n_ack_at) { $x } } 120 "n8n acknowledgement"
  "event $($e.id) acknowledged by n8n execution $($acked.n8n_execution_id)"
}
Check "supervisor request" {
  $body = @{ message = "Is the system healthy?" } | ConvertTo-Json
  $req = (Invoke-RestMethod "$base/api/supervisor/requests" -Method Post -ContentType "application/json" -Body $body -Headers $headers -WebSession $web).request
  $done = Wait-Until { $x = (Invoke-RestMethod "$base/api/supervisor/requests/$($req.id)" -WebSession $web).request; if ($x.status -in @("completed", "failed")) { $x } } 120 "the supervisor"
  if ($done.status -ne "completed") { throw "request ended as $($done.status): $($done.error)" }
  "request $($req.id) completed ($($done.planner))"
}
Check "web research (information only)" {
  $h = Invoke-RestMethod "$base/api/research/health" -WebSession $web
  if ($h.mode -eq "off") { "off" } elseif ($h.mode -eq "mock") { "mock (nothing leaves this PC)" }
  else { "live, search service " + ([string]$h.searxng.state).Replace("_", " ") }
}

Write-Host ""
if ($failures -eq 0) { Write-Host "SMOKE TEST PASSED" -ForegroundColor Green; exit 0 } else { Write-Host "SMOKE TEST FAILED ($failures check(s))" -ForegroundColor Red; exit 1 }
