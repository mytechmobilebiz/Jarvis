"""scripts/restore.ps1 itself, run under PowerShell (review 1, findings R23, R24, R27, R31).

The script runs in a copy of the project folder with a stand-in "docker" function that records every call and answers
like the CLI would; backup.ps1 and start.ps1 are stand-ins too. Nothing is stopped, restored or started. It runs in the
mcr.microsoft.com/powershell image (as testing/brain-check.sh step_restore_reapply does), or a local pwsh.

What it asserts: the removals and the users' access are saved before anything is stopped (with a helper container of
the api image, so a stopped api does not matter); a failure to save them stops the restore before any change unless
the owner passes the override that names what is lost; after pg_restore they are applied again BEFORE start.ps1; a
failure to apply them again leaves the services stopped and ends with exit 1; the user changes the restore reverts
are listed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
IMAGE = os.environ.get("PWSH_IMAGE", "mcr.microsoft.com/powershell:7.4-ubuntu-22.04")
REQUIRED = os.environ.get("REQUIRE_DOCKER_TESTS") == "1"

STUB = r'''
param([string]$Dir, [string]$Scenario, [string]$Switches)
$global:Calls = New-Object System.Collections.Generic.List[string]
$global:Tries = 0
$global:Scenario = $Scenario
function global:Start-Sleep { param([int]$Seconds) $global:Calls.Add("sleep $Seconds") }
function global:docker {
  $line = ($args | ForEach-Object { "$_" }) -join " "
  $global:Calls.Add($line); $global:LASTEXITCODE = 0
  # The installed application (review 3, P2): its containers' folder and its api image's version.
  if ($line -eq "compose ps -a -q") { "c0ffee01"; "c0ffee02"; return }
  if ($line -match '^inspect --format \{\{json \.Config\.Labels\}\} (c0ffee0\d)$') {
    $from = $Dir
    if ($global:Scenario -eq "otherfolder" -and $Matches[1] -eq "c0ffee02") { $from = "C:\InfiniteAI\v0.5.0\InfiniteAI-CommandCenter" }
    @{ "com.docker.compose.project" = "infinite-command-center"; "com.docker.compose.oneoff" = "False";
       "com.docker.compose.project.working_dir" = $from } | ConvertTo-Json -Compress
    return
  }
  if ($line -match '^compose run --rm --no-deps -T api python -c import app; print\(app\.__version__\)$') {
    if ($global:Scenario -eq "oldimage") { "0.2.0"; return }
    if ($global:Scenario -eq "noimage") { "no such image"; $global:LASTEXITCODE = 1; return }
    (Get-Content -LiteralPath (Join-Path $Dir "VERSION") -TotalCount 1).Trim(); return
  }
  # The database's restore mark (review 3, P1): the SQL file restore.ps1 puts into the postgres container.
  if ($line -match '^compose cp (\S+restore-mark-\S+\.sql) postgres:/tmp/restore-mark\.sql$') {
    $global:Calls.Add("mark-sql: " + (Get-Content -Raw -LiteralPath $Matches[1]).Trim()); return
  }
  if ($line -match "version = '0006_brain'") { "1"; return }
  if ($line -match "FROM pg_locks") { "0"; return }  # no import, rollback or rescan running (review 2, S19)
  if ($line -match "FILTER \(WHERE reason") { "3 withheld messages and copies, 1 purged items with a source key"; return }
  if ($line -match '^compose run -d --no-deps --name (\S+) api ') {
    if ($global:Scenario -eq "nohelper") { "no such image"; $global:LASTEXITCODE = 1 }
    return
  }
  if ($line -match 'restore-carry export --out') { "access and audit exported: users 4, audit entries 10"; return }
  if ($line -match 'decisions-export --removals-only') {
    if ($global:Scenario -eq "noexport") { "failed: OperationalError"; $global:LASTEXITCODE = 1; return }
    "removals exported (withheld messages and purged items, keys and JARVIS ids only): 3; releases: 1; purges by item id: 2; purge fingerprints: 1"; return
  }
  if ($line -match '^cp cc-restore-helper-\S+:(\S+) (.+)$') { Set-Content -LiteralPath $Matches[2] -Value '{}'; return }
  if ($line -match 'restore-carry apply --in') {
    if ($global:Scenario -eq "accessfails") { "failed: OperationalError"; $global:LASTEXITCODE = 1; return }
    "audit entries made after the backup, put back: 2"
    "access changes made after the backup, applied again: 1"
    "  kept: operator@test.local: deactivated"
    "user changes the restore reverts (redo them on the Users page if still wanted): 1"
    "  reverted: viewer@test.local: role raised to operator after the backup; the restore puts back viewer"
    "sign-in sessions ended: 3 (everyone signs in again)"
    return
  }
  if ($line -match 'decisions-apply --removals-only') {
    $global:Tries++
    if ($global:Scenario -eq "fails") { "failed: OperationalError"; $global:LASTEXITCODE = 1; return }
    if ($global:Tries -lt 3) { "refused: an import, rollback or rescan is running: try again when it has finished"; $global:LASTEXITCODE = 2; return }
    "removals applied: already done 2, purged 1, withheld 2"
    if ($global:Scenario -eq "partial") { "  purge not applied (purged before the restore, refused here: x): purged items 2" }
    return
  }
}
$flags = @{}
foreach ($s in ($Switches -split ',')) { if ($s) { $flags[$s] = $true } }
& (Join-Path $Dir "scripts/restore.ps1") -BackupFolder (Join-Path $Dir "backups/old") -Yes @flags
$code = $LASTEXITCODE
"--- calls"; $global:Calls; "exit=$code"
'''


def _runner(tmp: Path) -> list[str]:
    docker = shutil.which("docker")
    if docker and subprocess.run([docker, "image", "inspect", IMAGE], capture_output=True).returncode == 0:
        return [docker, "run", "--rm", "-v", "%s:%s" % (tmp, tmp), "-w", str(tmp), IMAGE, "pwsh"]
    pwsh = shutil.which("pwsh")
    if pwsh:
        return [pwsh]
    (pytest.fail if REQUIRED else pytest.skip)("needs PowerShell: the %s image or a local pwsh" % IMAGE)
    return []


def run_restore(tmp_path: Path, scenario: str, *switches: str, pending: bool = False) -> tuple[str, list[str], int]:
    work = tmp_path / ("w-" + scenario + "-" + "-".join(switches))
    proj = work / "InfiniteAI-CommandCenter"
    (proj / "scripts").mkdir(parents=True)
    (proj / "backups" / "old").mkdir(parents=True)
    if pending:  # an earlier restore did not finish: its marker names the two files it saved
        saved = {k: str(proj / "backups" / ("%s-before-restore-1.json" % k)) for k in ("access", "removals")}
        for path in saved.values():
            Path(path).write_text("{}")
        (proj / "backups" / "restore-pending.json").write_text(json.dumps({**saved, "backup": "old", "at": "now"}))
    shutil.copy(ROOT / "scripts" / "restore.ps1", proj / "scripts" / "restore.ps1")
    shutil.copy(ROOT / "VERSION", proj / "VERSION")
    (proj / "scripts" / "backup.ps1").write_text("$global:Calls.Add('backup.ps1')\r\nexit 0\r\n")
    (proj / "scripts" / "start.ps1").write_text("param([switch]$NoBuild)\r\n$global:Calls.Add('start.ps1')\r\nexit 0\r\n")
    (proj / "backups" / "old" / "command_center.dump").write_bytes(b"")
    stub = work / "stub.ps1"
    stub.write_text(STUB)
    done = subprocess.run([*_runner(work), "-NoProfile", "-File", str(stub), str(proj), scenario, ",".join(switches)],
                          capture_output=True, text=True, timeout=300)
    out = done.stdout + done.stderr
    assert "--- calls" in done.stdout, out
    tail = done.stdout.split("--- calls", 1)[1].strip().splitlines()
    assert tail and re.fullmatch(r"exit=\d+", tail[-1]), out
    return out, tail[:-1], int(tail[-1][5:])


def at(calls: list[str], pattern: str) -> int:
    for n, line in enumerate(calls):
        if re.search(pattern, line):
            return n
    return -1


def test_the_removals_and_access_are_saved_first_and_applied_again_before_anything_starts(tmp_path):
    """R23, R31, review 2 S19: stop < export (helper: the removals, then the access and the audit log, which then
    holds the export's own audit entry) < pg_restore < restore-carry apply < decisions-apply (helper) < start.ps1.
    Stopped before the export: nothing decided between the export and the stop is lost."""
    out, calls, code = run_restore(tmp_path, "works")
    order = [at(calls, r"^backup\.ps1$"), at(calls, r"^compose stop api worker dashboard n8n"),
             at(calls, r"decisions-export --removals-only"), at(calls, r"restore-carry export"),
             at(calls, r"pg_restore -U postgres"), at(calls, r"restore-carry apply --in /tmp/access\.json --yes"),
             at(calls, r"decisions-apply --removals-only --in /tmp/removals\.json --yes"), at(calls, r"^start\.ps1$")]
    assert -1 not in order and order == sorted(order), (order, calls)
    assert all(not re.search(r"compose (exec|cp) .*\bapi\b", c) for c in calls), calls  # never the api service
    assert at(calls, r"^compose run -d --no-deps --name cc-restore-helper-") >= 0
    rms = [n for n, c in enumerate(calls) if c.startswith("rm -f cc-restore-helper-")]
    runs = [n for n, c in enumerate(calls) if c.startswith("compose run -d --no-deps --name cc-restore-helper-")]
    assert len(runs) == len(rms) == 2 and order[1] < runs[0] < order[2] < rms[0] < order[4] < runs[1] < order[6] \
        < rms[1] < order[7], calls
    assert "were applied again (3)" in out and code == 0, out
    assert sum(1 for c in calls if c == "sleep 10") == 2  # tried again while something held the lock
    backups = os.listdir(tmp_path / "w-works-" / "InfiniteAI-CommandCenter" / "backups")
    assert any(b.startswith("removals-before-restore-") for b in backups)
    assert any(b.startswith("access-before-restore-") for b in backups)
    # R27: the access changes it applied again and the ones it reverts are listed, after the start
    assert "operator@test.local: deactivated" in out
    assert re.search(r"WARNING: the restore reverts these user changes.*\n.*viewer@test\.local: role raised", out)
    assert "everyone signs in again" in out


@pytest.mark.parametrize("scenario", ["fails", "partial", "accessfails"])
def test_a_failed_reapply_leaves_the_services_stopped_and_ends_with_exit_1(tmp_path, scenario):
    """R23, R31: the restored data is never served before the removals and the access narrowing are applied again;
    the commands to apply them are printed."""
    out, calls, code = run_restore(tmp_path, scenario)
    assert code == 1 and at(calls, r"^start\.ps1$") == -1, (code, calls)
    assert "NOT applied again" in out and "services were NOT started" in out
    assert "docker exec cc-restore-helper python -m app.cli brain-import decisions-apply --removals-only" in out
    assert "restore-carry apply --in /tmp/access.json --yes" in out
    assert "Restore complete" not in out


@pytest.mark.parametrize("scenario", ["nohelper", "noexport"])
def test_nothing_is_changed_when_the_removals_cannot_be_saved(tmp_path, scenario):
    """R24: the api being down no longer matters (a helper container exports); when even that fails, the restore
    stops before anything is restored, and names the override. Since review 2 (S19) the application is stopped
    before the export, so it is started again (start.ps1) before the restore stops."""
    out, calls, code = run_restore(tmp_path, scenario)
    assert code == 1 and at(calls, r"pg_restore") == -1, calls
    assert 0 <= at(calls, r"^compose stop") < at(calls, r"^start\.ps1$"), calls
    assert "nothing was changed" in out and "-SkipBrainRemovals" in out
    assert "Restore complete" not in out


def test_the_overrides_say_what_is_lost_and_restore_anyway(tmp_path):
    """R24: with -SkipBrainRemovals and -KeepRestoredAccess the restore runs and says what it gives up."""
    out, calls, code = run_restore(tmp_path, "nohelper", "SkipBrainRemovals", "KeepRestoredAccess")
    assert code == 0 and at(calls, r"pg_restore") >= 0 and at(calls, r"^start\.ps1$") >= 0, (code, calls)
    assert "BRAIN REMOVALS NOT KEPT (-SkipBrainRemovals)" in out and "ACCESS NOT KEPT (-KeepRestoredAccess)" in out
    assert "3 withheld messages and copies, 1 purged items with a source key" in out
    assert at(calls, r"decisions-apply") == -1


def test_the_databases_mark_is_written_by_the_pg_restore_command_itself(tmp_path):
    """Review 3, P1: the mark that keeps the api and the worker from starting (brain_maintenance
    restore_reapply_pending) is written by the same command in the database container as the pg_restore of
    command_center, right after it commits. A closed window or Ctrl+C kills this script and the docker client, not that
    command: pg_restore goes on and commits, and the mark follows it, so a plain 'docker compose up' (or a Docker
    restart) never serves the restored data without it. Its SQL goes into the container as a file first, before
    pg_restore; the local copy is not kept. (tests/integration/test_brain_review2_062.py kills a real one.)"""
    out, calls, code = run_restore(tmp_path, "works")
    copy = at(calls, r"^compose cp \S+[\\/]backups[\\/]restore-mark-\d{8}-\d{6}\.sql postgres:/tmp/restore-mark\.sql$")
    chain = at(calls, r"^compose exec -T postgres sh -c pg_restore -U postgres -d command_center --clean --if-exists "
                      r"--single-transaction --exit-on-error /tmp/restore-command_center\.dump \|\| exit 1; psql -U "
                      r"postgres -d command_center -X -q -1 -v ON_ERROR_STOP=1 -f /tmp/restore-mark\.sql \|\| exit 97$")
    order = [at(calls, r"^compose stop "), at(calls, r"restore-carry export"), copy, chain,
             at(calls, r"restore-carry apply --in"), at(calls, r"decisions-apply --removals-only --in"),
             at(calls, r"^start\.ps1$")]
    assert -1 not in order and order == sorted(order), (order, calls)
    sql = calls[copy + 1]
    assert sql.startswith("mark-sql: SET client_min_messages = warning; CREATE TABLE IF NOT EXISTS "
                          "brain_maintenance ("), sql
    assert re.search(r"INSERT INTO brain_maintenance \(name, detail\) VALUES \('restore_reapply_pending', "
                     r"jsonb_build_object\('access', true, 'removals', true, 'access_file', "
                     r"'access-before-restore-\d{8}-\d{6}\.json', 'removals_file', "
                     r"'removals-before-restore-\d{8}-\d{6}\.json', 'backup', 'old', 'at', now\(\)\)\) "
                     r"ON CONFLICT \(name\) DO UPDATE SET detail = EXCLUDED\.detail, done_at = now\(\);$", sql), sql
    assert not any("INSERT INTO brain_maintenance" in c for c in calls if not c.startswith("mark-sql: ")), calls
    assert at(calls, r"^compose exec -T postgres rm -f /tmp/restore-mark\.sql$") > chain
    backups = os.listdir(tmp_path / "w-works-" / "InfiniteAI-CommandCenter" / "backups")
    assert not [b for b in backups if b.startswith("restore-mark-")], backups
    assert code == 0 and "Restore complete" in out


@pytest.mark.parametrize("scenario", ["oldimage", "otherfolder", "noimage"])
@pytest.mark.parametrize("pending", [False, True])
def test_a_restore_with_another_versions_application_changes_nothing(tmp_path, scenario, pending):
    """Review 3, P2: every folder tags its images the same, so this folder's restore.ps1 could run another release's
    code (its helper, start.ps1 -NoBuild), and containers created from another folder belong to that folder's restore.
    Before anything is changed (no safety backup, no stop, no export, no pg_restore, and not even an earlier restore's
    files applied), the restore stops when the api image's version is not this folder's VERSION, when a container was
    created from another folder (its compose working_dir label), or when the version cannot be read."""
    out, calls, code = run_restore(tmp_path, scenario, pending=pending)
    assert code == 1 and "Restore complete" not in out, (code, out)
    for pattern in (r"^backup\.ps1$", r"^compose stop", r"pg_restore", r"decisions-export", r"restore-carry",
                    r"decisions-apply", r"^compose run -d", r"^start\.ps1$"):
        assert at(calls, pattern) == -1, (pattern, calls)
    version = (ROOT / "VERSION").read_text().strip()
    said = {"oldimage": "the installed application is version 0.2.0 (its api image), but this folder (",
            "otherfolder": "the application's containers were created from another folder "
                           "(C:\\InfiniteAI\\v0.5.0\\InfiniteAI-CommandCenter), not from this one (",
            "noimage": "could not read the installed application's version"}[scenario]
    assert said in out and "othing was changed" in out, out
    if scenario == "oldimage":
        assert ") is version %s." % version in out, out
    proj = tmp_path / ("w-%s-" % scenario) / "InfiniteAI-CommandCenter"
    assert (proj / "backups" / "restore-pending.json").exists() is pending  # an earlier restore's marker stays

