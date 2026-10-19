"""The release version is the same everywhere, and nothing can build or ship a release from stale copies of it.

Up to 0.5.0 every file in the Windows package had the same fixed timestamp. Docker (BuildKit) treats a file
it builds from as unchanged when its size and modification time match its copy from an earlier build (of any
folder of the project), so a new release kept same-size files from the first build: the API reported 0.2.0
from a 0.5.0 folder. These tests keep the three defences in place: per-build timestamps in the package, the version
check in both image builds, and the scripts that refresh the build folders and pass the version to the build.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import app

ROOT = Path(__file__).resolve().parents[3]
OLD_FIXED_STAMP = (2026, 1, 1, 0, 0, 0)


def packager():
    spec = importlib.util.spec_from_file_location("build_package", ROOT / "scripts" / "build-package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_every_version_constant_matches_the_version_file():
    version = (ROOT / "VERSION").read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    assert app.__version__ == version
    package = json.loads((ROOT / "dashboard" / "package.json").read_text())
    lock = json.loads((ROOT / "dashboard" / "package-lock.json").read_text())
    assert package["version"] == lock["version"] == lock["packages"][""]["version"] == version
    assert set(packager().version_constants().values()) == {version}


def test_the_packager_refuses_a_release_whose_version_constants_disagree(tmp_path):
    for name in ("VERSION", "backend/app/__init__.py", "dashboard/package.json", "dashboard/package-lock.json"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, tmp_path / name)
    bp = packager()
    assert bp.check_versions(tmp_path) == app.__version__
    init = tmp_path / "backend/app/__init__.py"
    init.write_text(init.read_text().replace(f'"{app.__version__}"', '"0.2.0"'))
    with pytest.raises(SystemExit, match=r"backend/app/__init__\.py: 0\.2\.0"):
        bp.check_versions(tmp_path)


def test_package_timestamps_change_with_every_build_and_are_never_in_the_future():
    bp = packager()
    now = datetime(2026, 10, 19, 9, 30, 15, tzinfo=UTC)
    stamp = bp.entry_time(now)
    assert stamp != OLD_FIXED_STAMP
    assert bp.entry_time(now + timedelta(minutes=5)) > stamp  # a later release is newer
    # Zip times carry no zone and are read as local time; even at UTC-12 this one is in the past.
    assert datetime(*stamp, tzinfo=UTC) + timedelta(hours=12) <= now


def test_the_built_package_carries_this_builds_timestamp_on_every_file(tmp_path):
    started = datetime.now(UTC)
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "build-package.py"), "--out", str(tmp_path)],
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    [package] = tmp_path.glob("*.zip")
    assert package.name == f"InfiniteAI-CommandCenter-{app.__version__}-windows.zip"
    with zipfile.ZipFile(package) as z:
        stamps = {i.date_time for i in z.infolist() if not i.filename.endswith("MANIFEST.txt")}
        init = z.read("InfiniteAI-CommandCenter/backend/app/__init__.py").decode()
    assert len(stamps) == 1 and OLD_FIXED_STAMP not in stamps
    [stamp] = stamps
    assert abs(datetime(*stamp, tzinfo=UTC) + timedelta(hours=14) - started) < timedelta(minutes=5)
    assert f'__version__ = "{app.__version__}"' in init


def test_both_image_builds_check_the_copied_code_against_the_release_version():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert compose.count("APP_VERSION: ${CC_RELEASE_VERSION:-}") == 2  # backend and dashboard
    backend = (ROOT / "backend" / "Dockerfile").read_text()
    assert backend.index("COPY app ./app") < backend.index("ARG APP_VERSION") < backend.index("app.__version__")
    dashboard = (ROOT / "dashboard" / "Dockerfile").read_text()
    assert dashboard.index("COPY . .") < dashboard.index("ARG APP_VERSION") < dashboard.index("require('./package.json').version")
    assert dashboard.index("require('./package.json').version") < dashboard.index("RUN npm run build")


def test_the_backend_build_check_fails_on_stale_code_and_passes_on_current(tmp_path):
    """Runs the backend Dockerfile's check itself (no Docker needed) against a package that says another version."""
    run = next(line for line in (ROOT / "backend" / "Dockerfile").read_text().splitlines() if "app.__version__" in line)
    script = re.search(r'python -c "(.+?)" "\$APP_VERSION"', run).group(1)  # type: ignore[union-attr]
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text('__version__ = "0.2.0"\n')
    stale = subprocess.run([sys.executable, "-c", script, "0.5.1"], cwd=tmp_path, capture_output=True, text=True)
    assert stale.returncode == 1 and "says 0.2.0, but this release is 0.5.1" in stale.stderr
    current = subprocess.run([sys.executable, "-c", script, "0.2.0"], cwd=tmp_path, capture_output=True, text=True)
    assert current.returncode == 0, current.stderr


def test_the_scripts_refresh_the_build_folders_and_pass_the_version_before_every_build():
    prepare = (ROOT / "scripts" / "prepare-build.ps1").read_text()
    assert "LastWriteTimeUtc" in prepare and '$env:CC_RELEASE_VERSION = $version' in prepare
    assert '@("backend", "dashboard")' in prepare
    install = (ROOT / "scripts" / "install.ps1").read_text()
    assert install.index('"prepare-build.ps1"') < install.index("docker compose build")
    start = (ROOT / "scripts" / "start.ps1").read_text()
    assert start.index('"prepare-build.ps1"') < start.index('@("compose", "up", "-d")')
    # The smoke test stays strict: both the API and the dashboard must report this folder's version.
    smoke = (ROOT / "scripts" / "smoke.ps1").read_text()
    assert "$apiVersion -ne $expected -or $dashboardVersion -ne $expected" in smoke
