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
  if ($line -match "version = '0006_brain'") { "1"; return }
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


def run_restore(tmp_path: Path, scenario: str, *switches: str) -> tuple[str, list[str], int]:
    work = tmp_path / ("w-" + scenario + "-" + "-".join(switches))
    proj = work / "InfiniteAI-CommandCenter"
    (proj / "scripts").mkdir(parents=True)
    (proj / "backups" / "old").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "restore.ps1", proj / "scripts" / "restore.ps1")
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
    """R23, R31: export (helper) < stop < pg_restore < restore-carry apply < decisions-apply (helper) < start.ps1."""
    out, calls, code = run_restore(tmp_path, "works")
    order = [at(calls, r"^backup\.ps1$"), at(calls, r"restore-carry export"),
             at(calls, r"decisions-export --removals-only"), at(calls, r"^compose stop api worker dashboard n8n"),
             at(calls, r"pg_restore -U postgres"), at(calls, r"restore-carry apply --in /tmp/access\.json --yes"),
             at(calls, r"decisions-apply --removals-only --in /tmp/removals\.json --yes"), at(calls, r"^start\.ps1$")]
    assert -1 not in order and order == sorted(order), (order, calls)
    assert all(not re.search(r"compose (exec|cp) .*\bapi\b", c) for c in calls), calls  # never the api service
    assert at(calls, r"^compose run -d --no-deps --name cc-restore-helper-") >= 0
    rms = [n for n, c in enumerate(calls) if c.startswith("rm -f cc-restore-helper-")]
    runs = [n for n, c in enumerate(calls) if c.startswith("compose run -d --no-deps --name cc-restore-helper-")]
    assert len(runs) == len(rms) == 2 and runs[0] < rms[0] < order[3] < runs[1] < order[6] < rms[1] < order[7], calls
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
    stops before anything is stopped or restored, and names the override."""
    out, calls, code = run_restore(tmp_path, scenario)
    assert code == 1 and at(calls, r"compose stop") == -1 and at(calls, r"pg_restore") == -1, calls
    assert "nothing was changed" in out and "-SkipBrainRemovals" in out
    assert "Restore complete" not in out


def test_the_overrides_say_what_is_lost_and_restore_anyway(tmp_path):
    """R24: with -SkipBrainRemovals and -KeepRestoredAccess the restore runs and says what it gives up."""
    out, calls, code = run_restore(tmp_path, "nohelper", "SkipBrainRemovals", "KeepRestoredAccess")
    assert code == 0 and at(calls, r"pg_restore") >= 0 and at(calls, r"^start\.ps1$") >= 0, (code, calls)
    assert "BRAIN REMOVALS NOT KEPT (-SkipBrainRemovals)" in out and "ACCESS NOT KEPT (-KeepRestoredAccess)" in out
    assert "3 withheld messages and copies, 1 purged items with a source key" in out
    assert at(calls, r"decisions-apply") == -1
