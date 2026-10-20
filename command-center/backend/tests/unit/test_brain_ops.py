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


@pytest.mark.parametrize("name", SCRIPTS)
def test_the_scripts_are_ascii_and_never_set_compose_variables_or_write_env(name):
    text = script(name)
    assert text.isascii()
    assert not re.search(r"(?i)\$env:COMPOSE_|Env:COMPOSE_|COMPOSE_[A-Z_]+\s*=", text)
    # No path inside a -v string: Docker is never given a mount on the command line (psql's -v sets a variable).
    assert not re.search(r'"(-v|--volume|--mount)",\s*\$', text) and '"--volume"' not in text and '"--mount"' not in text
    for call in re.findall(r"WriteAllText\((\$\w+)", text):
        assert call in ("$envFile", "$file"), call
    if "WriteAllText($envFile" in text:
        assert '$envFile = Join-Path $root ".env.brain-test"' in text


@pytest.mark.parametrize("name", SCRIPTS)
def test_no_empty_character_argument(name):
    # ('', '/') throws at run time ("String cannot be of zero length") although the script parses: a backslash lost
    # on the way into the file once made every jarvis-import.ps1 run stop there.
    assert not re.search(r"\(''\s*,", script(name))


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
