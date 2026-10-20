"""The Research Gateway's operations layer (docs/RESEARCH_GATEWAY_PLAN.md section 2): the SearXNG overlay, its
read-only settings, SEARXNG_SECRET, the Windows scripts that switch it on and keep the core safe without it, the
package and the SBOM. The running checks are testing/research-check.sh (plan 7.4); these are the static rules it
relies on, cheap enough for every test run."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

from app.research import health

ROOT = Path(__file__).resolve().parents[3]
IMAGE = re.compile(r"\$\{REGISTRY:-docker\.io\}/searxng/searxng:(?P<tag>[0-9.]+-[0-9a-f]{9})@sha256:[0-9a-f]{64}")
COMPOSE_SCRIPTS = ("install.ps1", "start.ps1", "stop.ps1", "status.ps1", "backup.ps1", "restore.ps1", "uninstall.ps1")


def compose(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text())


def script(name: str) -> str:
    return (ROOT / "scripts" / name).read_text().replace("\r\n", "\n")  # CRLF on a Windows checkout


def packager():
    spec = importlib.util.spec_from_file_location("build_package", ROOT / "scripts" / "build-package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_the_overlay_runs_searxng_hardened_private_and_pinned():
    overlay = compose("docker-compose.research.yml")
    assert set(overlay["services"]) == {"searxng", "worker"}
    sx = overlay["services"]["searxng"]
    match = IMAGE.fullmatch(sx["image"])
    assert match and match["tag"] == health.PINNED_TAG  # the gateway's version check names the same image
    assert sx["user"] == "977:977" and sx["read_only"] is True and sx["cap_drop"] == ["ALL"]
    assert sx["security_opt"] == ["no-new-privileges:true"] and sx["restart"] == "unless-stopped"
    assert "ports" not in sx and "expose" not in sx  # nothing on this PC or the network can open it
    assert sx["networks"] == ["research", "research_egress"]
    env = sx["environment"]
    assert env["SEARXNG_SECRET"].startswith("${SEARXNG_SECRET:?")  # an empty key would be accepted
    assert env["GRANIAN_HOST"] == "0.0.0.0" and env["GRANIAN_LOG_ACCESS_ENABLED"] == "false"
    assert not {"SEARXNG_LIMITER", "SEARXNG_PUBLIC_INSTANCE", "SEARXNG_IMAGE_PROXY", "SEARXNG_METHOD"} & set(env)
    config, cache = sx["volumes"]
    assert config == {"type": "bind", "source": "./research/searxng", "target": "/etc/searxng", "read_only": True,
                      "bind": {"create_host_path": False}}  # the whole folder, read-only
    assert cache == "searxng_cache:/var/cache/searxng"
    assert sx["tmpfs"] == ["/tmp:size=64m"] and sx["mem_limit"] == "512m" and sx["pids_limit"] == 256
    assert sx["labels"] == {"cc.research": "searxng"}
    assert overlay["services"]["worker"] == {"networks": {"research": {}}}  # adds one network, keeps the others
    assert overlay["networks"] == {"research": {"internal": True}, "research_egress": {}}


def test_no_other_service_joins_the_research_network_and_the_api_never_sees_the_secret():
    base = compose("docker-compose.yml")
    for name, service in base["services"].items():
        assert "research" not in (service.get("networks") or []), name
    assert "SEARXNG" not in (ROOT / "docker-compose.yml").read_text()  # not in x-api-env: only searxng gets it
    for name in ("docker-compose.brain-test.yml", "docker-compose.brain-import.yml", "docker-compose.tunnel.yml"):
        assert "searxng" not in (ROOT / name).read_text().lower(), name


def test_the_settings_are_json_only_private_and_match_the_gateway():
    settings = yaml.safe_load((ROOT / "research/searxng/settings.yml").read_text())
    assert tuple(settings["use_default_settings"]["engines"]["keep_only"]) == health.ENGINES
    assert settings["search"]["formats"] == ["json"]
    assert settings["search"]["autocomplete"] == "" and settings["search"]["favicon_resolver"] == ""
    assert settings["server"] == {"limiter": False, "public_instance": False, "image_proxy": False, "method": "POST"}
    assert "secret_key" not in settings["server"]  # SEARXNG_SECRET only, from .env
    locked = settings["preferences"]["lock"]
    assert {"image_proxy", "safesearch", "autocomplete", "favicon_resolver", "method"} <= set(locked)
    assert "categories" not in locked  # locking it would make SearXNG ignore the engines= field
    assert settings["general"]["enable_metrics"] is False and settings["general"]["debug"] is False
    assert settings["plugins"] == {} and settings["valkey"] == {"url": False}
    assert settings["outgoing"]["retries"] == 0
    # The gateway's cool-downs copy SearXNG's own suspensions (app/research/health.py).
    suspended = settings["search"]["suspended_times"]
    for reason, key in (("access_denied", "SearxEngineAccessDenied"), ("captcha", "SearxEngineCaptcha"),
                        ("too_many_requests", "SearxEngineTooManyRequests"), ("cf_captcha", "cf_SearxEngineCaptcha"),
                        ("cf_access_denied", "cf_SearxEngineAccessDenied"), ("recaptcha", "recaptcha_SearxEngineCaptcha")):
        assert health.pause_seconds(reason) == suspended[key], reason
    assert health.pause_seconds("timeout") == settings["search"]["ban_time_on_fail"]
    limiter = (ROOT / "research/searxng/limiter.toml").read_text().splitlines()
    assert limiter and all(line.startswith("#") for line in limiter)


def test_the_reviewed_copies_are_not_kept_twice():
    folder = ROOT / "docs" / "research-gateway"
    assert sorted(p.name for p in folder.iterdir()) == ["README.md"]
    assert "docker-compose.research.yml" in (folder / "README.md").read_text()


def test_searxng_secret_is_generated_like_every_other_secret_and_added_on_upgrade():
    ps1 = script("init-env.ps1")
    assert "SEARXNG_SECRET=$(New-Secret 32)" in ps1  # 32 random bytes, base64url: 43 characters
    assert '"SEARXNG_SECRET" = 32' in ps1[ps1.index("$upgradeKeys"):]
    sh = (ROOT / "scripts" / "init-env.sh").read_text()
    assert "SEARXNG_SECRET=$(secret 32)" in sh and "SEARXNG_SECRET:32" in sh
    preflight = script("preflight.ps1")
    addable = next(line for line in preflight.splitlines() if line.strip().startswith("$addable"))
    assert '"SEARXNG_SECRET"' in addable
    assert "SEARXNG_SECRET" in (ROOT / ".env.example").read_text()


@pytest.mark.parametrize("name", ["compose-env.ps1", "research.ps1", *COMPOSE_SCRIPTS])
def test_scripts_are_ascii_and_windows_powershell_5_1(name):
    text = script(name)
    assert text.isascii()  # Windows PowerShell 5.1 reads a file without a byte order mark in the ANSI code page
    code = "\n".join(re.sub(r"'[^'\n]*'", "''", line) for line in text.splitlines() if not line.lstrip().startswith("#"))
    for pattern in (r"\?\?", r"\?\.", r"\s&&\s", r"\s\|\|\s", r"-Parallel\b", r"ConvertFrom-Json\s+-AsHashtable"):
        assert not re.search(pattern, code), (name, pattern)  # PowerShell 6/7 only
    # Windows PowerShell 5.1 passes a double quote inside a native argument mangled: Go templates use single quotes.
    for fmt in re.findall(r"--format\s+(\S+)", text):
        assert '"' not in fmt and (fmt.startswith("'") or "{{" not in fmt), (name, fmt)


@pytest.mark.parametrize("name", COMPOSE_SCRIPTS)
def test_every_compose_script_decides_the_compose_files_before_its_first_compose_call(name):
    text = script(name)
    helper = text.index('. (Join-Path $')
    assert "compose-env.ps1" in text[helper:helper + 80]
    first = min(i for i in (text.find("docker compose"), text.find('@("compose"')) if i >= 0)
    assert helper < text.index("Use-ComposeEnv $root") < first, name


def test_the_helper_leaves_research_out_for_its_own_process_only():
    helper = script("compose-env.ps1")
    assert "$ResearchSecretMin = 32" in helper
    assert "$env:COMPOSE_FILE = $kept -join \";\"" in helper
    assert "WriteAllText" not in helper and "Set-Content" not in helper  # .env is never changed here
    for line in helper.splitlines():
        if "Write-Host" in line:
            assert "$secret" not in line[line.index("Write-Host"):], line


def test_start_starts_searxng_last_and_never_fails_because_of_it():
    start = script("start.ps1")
    assert '$_ -ne "searxng"' in start  # the core is started without it
    core_up = start.index("& docker @composeArgs")
    assert start.index("docker compose up -d --no-deps searxng") > core_up
    assert start.index("docker image inspect $image") < start.index("docker compose pull searxng")  # pulled only when missing
    after = start[start.index("$why = Start-Searxng"):start.index("function Wait-Url")]
    assert "exit" not in after  # a warning, never a failure


def test_research_ps1_switches_only_its_own_entry_and_never_prints_a_secret():
    text = script("research.ps1")
    assert "param([switch]$Enable, [switch]$Disable, [switch]$Status, [switch]$RotateSecret)" in text
    assert 'Split(";")' in script("compose-env.ps1") and '-join ";"' in text  # ';' as on Windows
    assert ".env.before-research-" in text  # .env is copied before every change
    assert "New-Object System.Text.UTF8Encoding $false" in text  # UTF-8 without BOM
    assert "label=com.docker.compose.project=cc-brain-test" in text and "label=cc.brain-import" in text
    assert text.index("function Assert-NothingElseRunning") < text.index("Assert-NothingElseRunning\n$values")
    assert '& (Join-Path $PSScriptRoot "start.ps1") -NoBuild' in text
    for line in text.splitlines():
        if "Write-Host" in line:
            assert not re.search(r"\$(secret|new)\b(?!\.Length)", line[line.index("Write-Host"):]), line
    enable = text[text.index("if ($Enable) {"):text.index("if ($Disable) {")]
    # Compose must accept the new list before .env changes.
    assert enable.index("docker compose config --services") < enable.index("Set-ComposeFile $wanted")


def test_install_refuses_a_development_build_unless_the_sandbox_allows_it():
    install = script("install.ps1")
    check = install.index("$version -match '-dev$' -and $env:CC_ALLOW_DEV_BUILD -ne \"1\"")
    assert check < install.index('Step "1/5')
    assert "a development build, not for installation" in install


def test_uninstall_removes_the_research_networks_and_cache_but_keeps_the_image():
    text = script("uninstall.ps1")
    for needle in ("com.docker.compose.network=$key", '@("research", "research_egress")',
                   "com.docker.compose.volume=searxng_cache", "docker image rm $($searxngImages"):
        assert needle in text, needle
    assert "docker image rm $image" in text  # only this project's own builds are removed


def test_the_brain_test_copy_switches_research_off_and_tolerates_a_searxng_restart():
    seed = script("brain-test.ps1")
    assert "DELETE FROM app_settings WHERE key = 'research_mode'" in seed
    assert "(SELECT count(*) FROM app_settings WHERE key = 'research_mode')" in seed
    common = script("brain-common.ps1")
    fingerprint = common[common.index("function Get-LiveFingerprint"):common.index("function Show-Fingerprint")]
    assert 'Get-ProjectContainers $BrainLiveProject "searxng"' in fingerprint
    assert "{{.Name}} {{.Id}} {{.Image}} {{.State.StartedAt}}" in fingerprint  # ID and image still compared


def test_the_package_ships_the_research_files_and_the_sbom_names_searxng(tmp_path):
    bp = packager()
    for f in ("docker-compose.research.yml", "research/searxng/settings.yml", "research/searxng/limiter.toml",
              "scripts/compose-env.ps1", "scripts/research.ps1"):
        assert f in bp.REQUIRED, f
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "build-package.py"), "--out", str(tmp_path)],
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    [package] = tmp_path.glob("*.zip")
    with zipfile.ZipFile(package) as z:
        names = set(z.namelist())
        research = z.read("InfiniteAI-CommandCenter/scripts/research.ps1")
    assert "InfiniteAI-CommandCenter/research/searxng/settings.yml" in names
    assert not any("/docs/research-gateway/searxng" in n for n in names)
    assert b"\r\n" in research and b"\n" not in research.replace(b"\r\n", b"")
    sbom = (ROOT / "SBOM.md").read_text()
    assert f"| searxng/searxng | {health.PINNED_TAG} | AGPL-3.0-or-later | optional |" in sbom


def test_a_development_build_is_never_packaged_as_a_release(monkeypatch, tmp_path):
    bp = packager()
    monkeypatch.setattr(bp, "check_versions", lambda root=bp.ROOT: "9.9.0-dev")
    monkeypatch.setattr(sys, "argv", ["build-package.py"])
    with pytest.raises(SystemExit, match="development build, not a release"):
        bp.main()
    built = []
    monkeypatch.setattr(bp, "candidate_files", lambda: built.append(1) or [])
    monkeypatch.setattr(bp, "check", lambda files: None)
    monkeypatch.setattr(sys, "argv", ["build-package.py", "--out", str(tmp_path)])
    bp.main()  # elsewhere (a test folder) it may
    assert built and list(tmp_path.glob("*-9.9.0-dev-windows.zip"))
