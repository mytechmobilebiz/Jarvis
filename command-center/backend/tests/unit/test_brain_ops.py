"""The JARVIS Brain operations layer (docs/JARVIS_BRAIN_SPEC.md section 6): the test copy's Compose overlay, the
importer container, the Windows scripts and the package. The end-to-end run is testing/brain-check.sh; these are the
static rules it relies on, cheap enough for every test run."""

from __future__ import annotations

import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ("brain-common.ps1", "brain-precheck.ps1", "brain-test.ps1", "jarvis-import.ps1")
CHECKED = SCRIPTS + ("restore.ps1",)  # restore.ps1 carries the JARVIS Brain's removals across a restore (0.6.2)


class ComposeLoader(yaml.SafeLoader):
    """Reads Compose's !override and !reset tags (Compose 2.24.4) as plain values, marked."""


def _tagged(tag):
    def construct(loader, node):
        if isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node)
        elif isinstance(node, yaml.MappingNode):
            value = loader.construct_mapping(node)
        else:
            value = loader.construct_scalar(node)
        return {tag: value}
    return construct


ComposeLoader.add_constructor("!override", _tagged("!override"))
ComposeLoader.add_constructor("!reset", _tagged("!reset"))


def compose(name: str) -> dict:
    return yaml.load((ROOT / name).read_text(), Loader=ComposeLoader)


def script(name: str) -> str:
    return (ROOT / "scripts" / name).read_text().replace("\r\n", "\n")  # CRLF on a Windows checkout


def test_the_test_copy_overlay_isolates_it_from_the_live_installation():
    base = compose("docker-compose.yml")
    overlay = compose("docker-compose.brain-test.yml")
    assert overlay["name"] == "cc-brain-test"
    services = overlay["services"]
    assert set(services) == set(base["services"])  # every service is covered
    for name, service in services.items():
        assert service["restart"] == "no", name
    for name in ("api", "worker"):
        assert services[name]["image"] == "infinite-command-center-brain-test/backend:local"
    assert services["dashboard"]["image"] == "infinite-command-center-brain-test/dashboard:local"
    assert services["worker"]["pull_policy"] == "never"
    assert services["worker"]["networks"] == {"!override": ["internal"]}
    assert base["networks"]["internal"]["internal"] is True  # the private network has no route out
    assert services["dashboard"]["ports"] == {"!override": ["127.0.0.1:${BRAIN_TEST_PORT:-8190}:8080"]}
    assert services["n8n"]["profiles"] == ["never"]
    expected = {"WORKFLOW_ENGINE": "none", "LLM_PROVIDER": "rule_based", "ANTHROPIC_API_KEY": "", "N8N_EDITOR_URL": "",
                "CC_INSTANCE_LABEL": "cc-brain-test", "SESSION_COOKIE_NAME": "cc_session_brain_test"}
    for name in ("api", "worker"):
        assert services[name]["environment"] == expected, name


def test_the_importer_container_is_hardened():
    overlay = compose("docker-compose.brain-import.yml")
    services = overlay["services"]
    assert set(services) == {"brain-importer", "brain-importer-nosource"}
    for name, svc in services.items():
        assert svc["image"].startswith("${BRAIN_IMPORT_IMAGE:?"), name
        assert svc["pull_policy"] == "never" and svc["profiles"] == ["brain-import"] and svc["restart"] == "no"
        assert svc["entrypoint"] == ["python", "-m", "app.cli", "brain-import"]
        assert svc["networks"] == ["internal"]
        assert set(svc["environment"]) == {"DATABASE_URL", "TZ", "ENVIRONMENT"}
        assert "${CC_DB_PASSWORD:?}@postgres:5432/command_center" in svc["environment"]["DATABASE_URL"]
        assert svc["read_only"] is True and svc["tmpfs"] == ["/tmp"] and svc["cap_drop"] == ["ALL"]
        assert svc["security_opt"] == ["no-new-privileges:true"]
        assert svc["mem_limit"] == "3g" and svc["pids_limit"] == 256
        assert svc["labels"] == {"cc.brain-import": "1"}
        for mount in svc["volumes"]:
            assert mount["type"] == "bind" and mount["bind"] == {"create_host_path": False}, mount
            assert mount["source"].startswith("${BRAIN_IMPORT_"), mount  # paths come from the script's variables
            assert mount["read_only"] is (mount["target"] != "/reports"), mount
    targets = {m["target"]: m["source"] for m in services["brain-importer"]["volumes"]}
    assert set(targets) == {"/jarvis/state.db", "/jarvis/profile", "/jarvis/content", "/reports"}
    assert targets["/jarvis/state.db"].startswith("${BRAIN_IMPORT_DB")
    assert targets["/jarvis/profile"].startswith("${BRAIN_IMPORT_PROFILE")
    assert targets["/jarvis/content"].startswith("${BRAIN_IMPORT_CONTENT")
    assert [m["target"] for m in services["brain-importer-nosource"]["volumes"]] == ["/reports"]


@pytest.mark.parametrize("name", CHECKED)
def test_the_scripts_are_ascii_and_never_set_compose_variables_or_write_env(name):
    text = script(name)
    assert text.isascii()
    assert not re.search(r"(?i)\$env:COMPOSE_|Env:COMPOSE_|COMPOSE_[A-Z_]+\s*=", text)
    # No path inside a -v string: Docker is never given a mount on the command line (psql's -v sets a variable).
    assert not re.search(r'"(-v|--volume|--mount)",\s*\$', text) and '"--volume"' not in text and '"--mount"' not in text
    for call in re.findall(r"WriteAllText\((\$\w+)", text):
        assert call in ("$envFile", "$file", "$markFile"), call
    if "WriteAllText($envFile" in text:
        assert '$envFile = Join-Path $root ".env.brain-test"' in text
    if "WriteAllText($markFile" in text:  # restore.ps1: the database mark's SQL, next to the backups (review 3, P1)
        assert '$markFile = Join-Path $backups "restore-mark-$stamp.sql"' in text


@pytest.mark.parametrize("name", CHECKED)
def test_no_empty_character_argument(name):
    # ('', '/') throws at run time ("String cannot be of zero length") although the script parses: a backslash lost
    # on the way into the file once made every jarvis-import.ps1 run stop there.
    assert not re.search(r"\(''\s*,", script(name))


@pytest.mark.parametrize("name", CHECKED)
def test_no_variable_followed_by_a_colon(name):
    # "... is $version: install ..." does not parse ("Variable reference is not valid"): PowerShell reads $name: as a
    # drive or scope (as in $env:TEMP). ${version}: is meant. A 0.6.2 refusal once stopped jarvis-import.ps1 from
    # parsing at all (testing/brain-check.sh parses every script; this catches it on every test run).
    assert not re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*:(?=[\s\"'])", script(name))


def test_refusals_come_before_anything_and_say_nothing_changed():
    common = script("brain-common.ps1")
    stop = common[common.index("function Stop-Brain"):common.index("function Invoke-Native")]
    assert "exit 2" in stop and "Nothing was changed." in stop
    assert "New-Object Version 2, 24, 4" in common and r"'(\d+)\.(\d+)\.(\d+)'" in common  # version by regex
    for name in ("brain-test.ps1", "jarvis-import.ps1"):
        # The script's own top-level steps, without its function definitions (which run only when called).
        text = re.sub(r"(?ms)^function .*?^}\n", "", script(name))
        checks = text.index("# --- Checks before anything runs")
        text = text[:checks] + re.sub(r"(?m)^\s*#.*$", "", text[checks:])  # comments say what is done, not do it
        before = text.index("$before = @(Get-LiveFingerprint)")
        changes = [m.start() for m in re.finditer(
            r"Invoke-DockerLive|Invoke-Importer |New-Item|Copy-Item|Write-TestEnv|Join-Path \$PSScriptRoot \"prepare-build|Invoke-Seed ",
            text)]
        assert changes and checks < text.index("Assert-ComposeVersion") < before < min(changes), name
        assert text.index("Assert-SameFingerprint $before $after") > max(changes), name


def test_typed_confirmations():
    assert 'Confirm-Typed "REMOVE TEST COPY"' in script("brain-test.ps1")
    jarvis = script("jarvis-import.ps1")
    assert jarvis.count('Confirm-Typed "IMPORT INTO PRODUCTION"') == 2  # Import and Resume
    assert 'Confirm-Typed "ROLL BACK PRODUCTION"' in jarvis
    assert "-cne $phrase" in script("brain-common.ps1")  # exact, case-sensitive


def test_the_test_copy_is_built_like_install_does_it():
    text = script("brain-test.ps1")
    call = text.index('& (Join-Path $PSScriptRoot "prepare-build.ps1")')
    up = '@("up", "-d", "--build", "--force-recreate")'  # a rebuilt image is not always picked up without it
    assert call < text.index("-not $env:CC_RELEASE_VERSION") < text.index(up)
    assert text.index(up) < text.rindex("Remove-Item Env:CC_RELEASE_VERSION")
    common = script("brain-common.ps1")
    assert '"compose", "-p", $BrainTestProject' in common and '"--env-file", (Join-Path $root ".env.brain-test")' in common


def test_the_package_ships_the_operations_files_and_not_the_sandbox_check(tmp_path):
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "build-package.py"), "--out", str(tmp_path)],
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    [package] = tmp_path.glob("*.zip")
    with zipfile.ZipFile(package) as z:
        names = set(z.namelist())
        brain_test = z.read("InfiniteAI-CommandCenter/scripts/brain-test.ps1")
    for f in ("docker-compose.brain-test.yml", "docker-compose.brain-import.yml", *(f"scripts/{s}" for s in SCRIPTS)):
        assert f"InfiniteAI-CommandCenter/{f}" in names, f
    assert not any("/testing/" in n for n in names)
    assert b"\r\n" in brain_test and b"\n" not in brain_test.replace(b"\r\n", b"")


@pytest.mark.parametrize("left", [0, 3, None])
def test_a_production_import_waits_until_no_promoted_item_keeps_withheld_text(monkeypatch, capsys, left):
    """jarvis-import.ps1 reads brain-import status' 0.6.1 line: 0007_brain_withhold present, and no promoted item
    whose message is withheld still holding text (left=None: an importer older than 0.6.1, which has no such line)."""
    from app import cli
    from app.brain import importer

    async def status(run=None):
        out = {"importer_version": importer.IMPORTER_VERSION, "installed": True, "runs": []}
        if left is not None:
            out.update(migration_0007_brain_withhold=True, withheld_copies_with_text=left)
        return out

    monkeypatch.setattr(importer, "status", status)
    assert cli.main(["cli", "brain-import", "status"]) == 0
    printed = capsys.readouterr().out
    if left is None:
        printed = "\n".join(line for line in printed.splitlines() if not line.startswith("migration 0007"))
    text = script("jarvis-import.ps1")
    pattern = re.search(r"if \(\$printed -match '(\(\?m\)\^migration [^']+)'\) \{ \$withheldCopies = \$Matches\[1\] \}",
                        text).group(1)
    found = re.search(pattern, printed)
    assert (found.group(1) if found else None) == (None if left is None else str(left)), printed
    assert '$migrations -notcontains "0007_brain_withhold"' in text and "install 0.6.2 on the live installation" in text
    assert "install 0.6.0" not in text and "install 0.6.1" not in text  # 0.6.0 and 0.6.1 are NO-GO: 0.6.2 is asked for


@pytest.mark.parametrize("mode", ["Import", "Resume", "Verify", "Rollback", "Status", "Rescan", "ApplyDecisions"])
def test_only_an_import_or_a_resume_into_production_waits_for_the_withheld_copies(mode):
    """While promoted items of withheld messages still hold text, Status shows how many, Rescan (the remedy the
    message names) empties them, and Verify, Rollback (the undo of an import) and ApplyDecisions, which bring no
    JARVIS text in, still run; only Import and Resume are refused. Both migration checks apply to every mode."""
    text = script("jarvis-import.ps1")
    production = text[text.index('if ($Target -eq "Production") {'):text.index("# --- Run ---")]
    [gate] = re.findall(r'if \(@\(([^)]*)\) -contains \$Mode -and \$withheldCopies -ne "0"\) \{', production)
    gated = set(re.findall(r'"(\w+)"', gate))
    assert (mode in gated) == (mode in ("Import", "Resume"))
    for migration in ("0006_brain", "0007_brain_withhold"):
        assert '$migrations -notcontains "%s"' % migration in production
    assert "-Target Production -Mode Rescan" in production and '"Rescan" { $cli = @("rescan", "--yes") }' in text


@pytest.mark.parametrize("installed", [True, False])
def test_the_production_feature_check_matches_what_brain_import_status_prints(monkeypatch, capsys, installed):
    from app import cli
    from app.brain import importer

    async def status(run=None):
        return {"importer_version": importer.IMPORTER_VERSION, "installed": installed, "runs": []}

    monkeypatch.setattr(importer, "status", status)
    assert cli.main(["cli", "brain-import", "status"]) == 0
    printed = capsys.readouterr().out
    pattern = re.search(r"\$printed -notmatch '([^']+)'", script("jarvis-import.ps1")).group(1)
    found = re.search(pattern, printed)
    assert (found.group(1) if found else None) == (importer.IMPORTER_VERSION if installed else None), printed


@pytest.mark.parametrize("repair", ["done", "not done"])
def test_the_0_6_2_gate_reads_the_withhold_identity_line_brain_import_status_prints(monkeypatch, capsys, repair):
    """jarvis-import.ps1 reads brain-import status' 0.6.2 line: 0008_brain_withhold_identity present, whether the
    identity repair ran, and the counts it shows (or requires: the repair done and no purge fingerprint made with
    another key, for Import and Resume)."""
    from app import cli
    from app.brain import importer

    async def status(run=None):
        return {"importer_version": importer.IMPORTER_VERSION, "installed": True, "runs": [],
                "migration_0007_brain_withhold": True, "withheld_copies_with_text": 0,
                "migration_0008_brain_withhold_identity": True, "identity_repair_done": repair == "done",
                "readable_rows_matching_a_withhold": 2, "held_for_review": 3,
                "withheld_rows_with_a_text_fingerprint": 4, "rows_blanked_by_the_database_guard": 5, "quotes_of_removed_sources": 6,
                "purge_fingerprints_made_with_another_key": 7}

    monkeypatch.setattr(importer, "status", status)
    assert cli.main(["cli", "brain-import", "status"]) == 0
    printed = capsys.readouterr().out
    text = script("jarvis-import.ps1")
    pattern = re.search(r"if \(\$printed -match '(\(\?m\)\^migration 0008_brain_withhold_identity [^']+)'\) \{",
                        text).group(1)
    found = re.search(pattern, printed)
    assert found is not None and found.groups() == (repair, "2", "3", "4", "5", "6", "7"), printed
    # The 0.6.1 line is still printed and read as before.
    assert re.search(r"(?m)^migration 0007_brain_withhold present; withheld copies with text: 0\b", printed)
    production = text[text.index('if ($Target -eq "Production") {'):text.index("# --- Run ---")]
    assert '$migrations -notcontains "0008_brain_withhold_identity"' in production
    assert '[version]$importerVersion -lt [version]"1.1.0"' in production
    assert tuple(int(p) for p in importer.IMPORTER_VERSION.split(".")) >= (1, 1, 0)
    assert '$identity.Repair -ne "done"' in production and '$identity.OtherKey -ne "0"' in production


def test_repair_mode_runs_brain_import_repair_without_the_jarvis_sources():
    text = script("jarvis-import.ps1")
    modes = re.search(r"\$modes = @\(([^)]*)\)", text).group(1)
    assert '"Repair"' in modes
    reads = re.search(r"\$readsSource = @\(([^)]*)\) -contains \$Mode", text).group(1)
    assert '"Repair"' not in reads  # brain-importer-nosource: no JARVIS file is mounted
    assert '"Repair" { $cli = @("repair", "--yes") }' in text
    assert 'Confirm-Typed "REPAIR PRODUCTION"' in text
    precheck = script("brain-precheck.ps1")
    assert '"SELECT brain_withhold_pending()"' in precheck and "0.6.2 not installed" in precheck
    assert "default_transaction_read_only=on" in precheck


def test_settle_mode_runs_brain_import_settle_on_the_backup_only():
    """0.6.2 plan 4.1: jarvis-import.ps1 -Mode Settle reads the JARVIS backup (its conversations only) and runs
    brain-import settle --backup; on production it needs its own typed confirmation, and it waits for the withheld
    copies like an import (it imports conversations)."""
    text = script("jarvis-import.ps1")
    assert '"Settle"' in re.search(r"\$modes = @\(([^)]*)\)", text).group(1)
    assert '"Settle"' in re.search(r"\$readsSource = @\(([^)]*)\) -contains \$Mode", text).group(1)
    assert '$cli = @("settle", "--backup", "/jarvis/state.db", "--report", $report)' in text
    assert 'Confirm-Typed "FINISH DECISIONS IN PRODUCTION"' in text
    [gate] = re.findall(r'if \(@\(([^)]*)\) -contains \$Mode -and \$withheldCopies -ne "0"\) \{', text)
    assert '"Settle"' in gate


@pytest.mark.parametrize("result", [
    {"run_id": None, "status": "nothing to finish", "decisions": 0, "waiting": {"older-backup": 1}},
    {"run_id": 7, "decisions": 2, "waiting": {}, "report": {"settle": {"certain": 3, "held": 1}}}])
def test_settle_cli_takes_the_backup_and_says_what_it_finished(monkeypatch, capsys, result):
    """brain-import settle --backup FILE (plan 4.1): the backup's conversations only, no profile needed; it says what
    waits on another backup, what it finished and what comes next (review, then export)."""
    from app import cli
    from app.brain import importer

    calls = []

    async def settle(paths, *, actor, report_file=None):
        calls.append((paths.db, paths.profile, actor, report_file))
        return result

    monkeypatch.setattr(importer, "settle", settle)
    monkeypatch.setattr(cli, "_print_report", lambda report: None)
    assert cli.main(["cli", "brain-import", "settle"]) == 2  # --backup is required
    capsys.readouterr()
    assert cli.main(["cli", "brain-import", "settle", "--backup", "/jarvis/state.db", "--actor", "ops@test"]) == 0
    assert calls == [("/jarvis/state.db", "", "ops@test", None)]
    printed = capsys.readouterr().out
    if result["run_id"] is None:
        assert "nothing to finish" in printed and "waiting on another JARVIS backup (older-backup): 1" in printed
    else:
        assert "run 7 finished 2 decision(s)" in printed and "Held for review" in printed


def test_decisions_apply_needs_a_run_and_an_admin_unless_removals_only(monkeypatch, capsys, tmp_path):
    from app import cli
    from app.brain import importer

    calls = []

    async def apply(in_path, run_id, *, actor, apply, removals_only=False):
        calls.append((run_id, actor, apply, removals_only))
        return {"applied" if apply else "would_apply": 0, "not_applied": {}, "removals": {"withheld": 1},
                "removals_not_applied": {}, "releases": {}, "releases_not_applied": {}}

    monkeypatch.setattr(importer, "decisions_apply", apply)
    file = str(tmp_path / "removals.json")
    assert cli.main(["cli", "brain-import", "decisions-apply", "--in", file, "--yes"]) == 2
    assert "needs --run ID and --actor" in capsys.readouterr().out and calls == []
    assert cli.main(["cli", "brain-import", "decisions-apply", "--in", file, "--run", "3", "--yes"]) == 2
    assert cli.main(["cli", "brain-import", "decisions-apply", "--in", file, "--removals-only", "--yes"]) == 0
    assert calls == [(None, None, True, True)]
    printed = capsys.readouterr().out
    assert "decisions applied" not in printed and "removals applied: withheld 1" in printed


@pytest.mark.parametrize("removals", [{}, {"withheld": 2, "held for review": 1, "purged": 3, "already done": 4,
                                          "kept for later imports": 1}])
@pytest.mark.parametrize("not_applied", [{}, {"removals_not_applied": {"refused here": [2]}},
                                         {"purged_items_not_applied": {"purged before the restore, refused here: x": [1]}},
                                         {"releases_not_applied": {"not a release": [3]}}])
def test_restore_reads_what_decisions_apply_and_export_print(monkeypatch, capsys, tmp_path, removals, not_applied):
    """restore.ps1 counts what was applied again from decisions-apply's removals line (what the restored backup
    already held is "already done", not counted), and treats any "not applied" line (a removal, a purge by item id or a
    release it could not apply again) as a failure, the same way PowerShell does: these are its own patterns."""
    from app import cli
    from app.brain import importer

    async def apply(in_path, run_id, *, actor, apply, removals_only=False):
        return {"applied": 0, "not_applied": {}, "removals": removals, "removals_not_applied": {},
                "releases": {"released": 1}, "releases_not_applied": {}, "purged_items_not_applied": {},
                "purge_fingerprints": {"carried": 1}, **not_applied}

    async def export(out_path, *, actor="", removals_only=False):
        return {"removals": 5, "releases": 1, "removals_only": True, "purged_items": 2, "purge_fingerprints": 1}

    monkeypatch.setattr(importer, "decisions_apply", apply)
    monkeypatch.setattr(importer, "decisions_export", export)
    assert cli.main(["cli", "brain-import", "decisions-apply", "--in", str(tmp_path / "f"), "--removals-only",
                     "--yes"]) == 0
    printed = capsys.readouterr().out
    text = script("restore.ps1")
    line = re.search(r"\$text -match '(\(\?m\)\^removals applied: [^']+)'\) \{", text).group(1)
    part = re.search(r"\$part -match '([^']+)' -and \$Matches\[1\] -ne \"already done\"", text).group(1)
    failed = re.search(r"\$text -match '([^']+not applied)'\) \{", text).group(1)
    assert bool(re.search(failed.replace("(?m)", ""), printed, re.M)) is bool(not_applied), printed
    found = re.search(line, printed)
    again = 0
    if found:
        for p in found.group(1).split(", "):
            m = re.match(part, p)
            if m and m.group(1) != "already done":
                again += int(m.group(2))
    assert again == sum(n for k, n in removals.items() if k != "already done")
    assert found is not None and "releases applied: released 1" in printed  # "removals applied: none" counts 0
    assert "purge fingerprints: carried 1" in printed
    assert cli.main(["cli", "brain-import", "decisions-export", "--out", str(tmp_path / "r"), "--removals-only"]) == 0
    assert "removals exported" in capsys.readouterr().out


def test_restore_saves_first_and_applies_again_before_anything_starts():
    """spec 9.3 (review 1, R23, R24, R27): the removals and the users' access are saved with a helper container of the
    api image (the api need not run) before anything is stopped, kept next to the backups, and applied again after
    pg_restore and BEFORE start.ps1, in a helper again; a failure to save stops the restore before any change unless
    an override names what is lost; a failure to apply again leaves the services stopped and names the commands.
    tests/unit/test_restore_ps1.py runs the script."""
    text = script("restore.ps1")
    # Review 2: an unfinished restore is finished first (S18); the application is stopped BEFORE the export (S19),
    # after a probe that fails closed (S20) and a check that no import runs; the apply is one function (Invoke-Reapply)
    # used for both, and a marker names the saved files from before pg_restore until they are applied again.
    reapply = text[text.index("function Invoke-Reapply"):text.index("function Show-Reapply")]
    assert reapply.index("Invoke-Helper restore-carry apply --in /tmp/access.json --yes") < reapply.index(
        "Invoke-Helper brain-import decisions-apply --removals-only --in /tmp/removals.json --yes")
    order = ["if (Test-Path -LiteralPath $pendingFile -PathType Leaf)", "Invoke-Reapply $pendingAccess $pendingRemovals",
             '& (Join-Path $PSScriptRoot "backup.ps1")', "version = '0006_brain'", "FROM pg_locks",
             "docker compose stop api worker dashboard n8n",
             "Invoke-Helper brain-import decisions-export --removals-only --out $inside",
             'docker cp "${helper}:$inside" $target', "Invoke-Helper restore-carry export --out $inside",
             'docker cp "${helper}:$inside" $target', "if ($missing.Count -gt 0)",
             "Set-Content -LiteralPath $pendingFile -Value $marker",
             # review 3, P1: the database's mark is put into the container first, then written by the command that
             # runs pg_restore, once pg_restore has committed (a killed script never leaves the restore unmarked)
             "INSERT INTO brain_maintenance (name, detail) VALUES ('restore_reapply_pending'",
             'docker compose cp $markFile "postgres:/tmp/restore-mark.sql"',
             "pg_restore -U postgres -d command_center --clean --if-exists --single-transaction --exit-on-error "
             "/tmp/restore-command_center.dump || exit 1; psql -U postgres -d command_center -X -q -1 -v "
             "ON_ERROR_STOP=1 -f /tmp/restore-mark.sql || exit 97",
             "Invoke-Reapply $access $removals", "if ($script:problems.Count -gt 0)",
             "Remove-Item -LiteralPath $pendingFile -Force }", '& (Join-Path $PSScriptRoot "start.ps1") -NoBuild']
    at = 0
    for o in order:  # each one after the one before it
        assert o in text[at:], (o, order)
        at = text.index(o, at) + len(o)
    assert "docker compose run -d --no-deps --name $helper api" in text
    assert "--status running api" not in text and "compose exec -T api" not in text  # the api need not run
    assert '"removals-before-restore-$stamp.json"' in text and '"access-before-restore-$stamp.json"' in text
    assert "$backups = Join-Path $root \"backups\"" in text
    assert "[switch]$SkipBrainRemovals" in text and "[switch]$KeepRestoredAccess" in text
    assert "for ($try = 1; $try -le 4; $try++)" in text and "Start-Sleep -Seconds 10" in text
    assert "$text -notmatch 'is running'" in text and "$code -ne 2" in text
    assert text.count("Show-Reapply") == 3  # defined, and called when something was not applied again (twice)
    assert "-Mode Status" in text and "were applied again" in text
    assert '($failed -notcontains "command_center")' in text  # an unchanged database needs nothing applied again
    assert ".env" not in text.replace("Use the same .env the backup was taken with.", "")


def test_the_test_copy_seed_leaves_out_the_live_purge_fingerprints():
    """A seed from a live 0.6.2 backup would bring purge fingerprints keyed with the live database password; the
    test copy's imports would refuse them (identity.PurgeIndex). They are left out; the purges stay by key."""
    text = script("brain-test.ps1")
    seed = text[text.index("function Invoke-Seed"):text.index("# --- Checks before anything runs")]
    assert "IF to_regclass(''public.brain_purged_content'') IS NOT NULL THEN DELETE FROM brain_purged_content" in seed
    assert seed.index("brain_purged_content") < seed.index('"COMMIT;"')


def test_the_precheck_reads_keys_brain_withhold_pending_returns():
    """brain-precheck.ps1 turns brain_withhold_pending()'s JSON into words: every key it reads is one the function of
    migration 0008 builds (a renamed key would print 0 for ever)."""
    precheck = script("brain-precheck.ps1")
    read = set(re.findall(r"\$pending\.(\w+)", precheck))
    assert read == {"readable_by_key", "readable_by_jarvis_row", "readable_by_fold", "readable_in_restricted_sessions",
                    "held_for_review", "repair_done"}  # 0.6.2 step 6: no time-rule counter
    sql = (ROOT / "backend" / "app" / "migrations" / "0008_brain_withhold_identity.sql").read_text()
    pending = sql[sql.index("FUNCTION brain_withhold_pending()"):]
    built = set(re.findall(r"'(\w+)',", pending))
    assert read <= built, read - built
