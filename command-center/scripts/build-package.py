#!/usr/bin/env python3
"""Builds the clean Windows installation package (a .zip) from this folder.

    python scripts/build-package.py [--out DIR]

The package holds only files git tracks or would track (so anything ignored,
.env included, can never get in), minus development-only files. As a second
guard it refuses to build if any packaged file contains a value from the local
.env, or a file name that looks like a credential.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
TOP = "InfiniteAI-CommandCenter"

DEV_ONLY = (
    "backend/tests/",
    "backend/pytest.ini",
    "backend/requirements-dev.txt",
    "dashboard/src/test/",
    "e2e/",
    "migration/jarvis/tests/",
    "migration/jarvis/make_templates.py",
    "migration/jarvis/make_kit.py",
    "testing/",
    "docker-compose.testing.yml",
    "scripts/build-package.py",
    "scripts/init-env.sh",
    "scripts/generate-sbom.py",
    "releases/",
)
# The owner's operations files: the package is refused if one is missing (testing/ stays out, as above).
REQUIRED = (
    "VERSION",
    "docker-compose.yml",
    "docker-compose.brain-test.yml",
    "docker-compose.brain-import.yml",
    "scripts/install.ps1",
    "scripts/prepare-build.ps1",
    "scripts/brain-common.ps1",
    "scripts/brain-precheck.ps1",
    "scripts/brain-test.ps1",
    "scripts/jarvis-import.ps1",
    "migration/jarvis/jarvis-kit.ps1",
)
FORBIDDEN_NAMES = (".env", "id_rsa", "id_ed25519")
FORBIDDEN_SUFFIXES = (".key", ".p12", ".pfx", ".dump", ".sqlite", ".pyc")


def candidate_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "."],
        cwd=ROOT, capture_output=True, check=True,
    ).stdout.decode()
    files = sorted({f for f in out.split("\0") if f and (ROOT / f).is_file()})
    return [f for f in files if not any(f == d or f.startswith(d) for d in DEV_ONLY)]


def local_secret_values() -> list[str]:
    env = ROOT / ".env"
    if not env.exists():
        return []
    values = []
    for line in env.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = (part.strip() for part in line.split("=", 1))
            if re.search(r"(PASSWORD|TOKEN|SECRET|_KEY)$", key) and len(value) >= 8:
                values.append(value)
    return values


def check(files: list[str]) -> None:
    secrets = local_secret_values()
    problems = [f"{f}: missing from the package" for f in REQUIRED if f not in files]
    for f in files:
        name = PurePosixPath(f).name
        if name in FORBIDDEN_NAMES or name.endswith(FORBIDDEN_SUFFIXES) or (name.startswith(".env") and name != ".env.example"):
            problems.append(f"{f}: looks like a credential or data file")
            continue
        data = (ROOT / f).read_bytes()
        if f.endswith(".ps1") and not data.isascii():
            # Windows PowerShell 5.1 reads a file without a byte order mark in the ANSI code page.
            problems.append(f"{f}: PowerShell scripts must be plain ASCII")
        for value in secrets:
            if value.encode() in data:
                problems.append(f"{f}: contains a value from the local .env")
                break
    if problems:
        sys.exit("refusing to build the package:\n  " + "\n  ".join(problems))


def version_constants(root: Path = ROOT) -> dict[str, str]:
    """Every place the release version is written down; they must all agree."""
    init = (root / "backend/app/__init__.py").read_text()
    match = re.search(r'^__version__ = "([^"]+)"$', init, re.MULTILINE)
    package = json.loads((root / "dashboard/package.json").read_text())
    lock = json.loads((root / "dashboard/package-lock.json").read_text())
    sbom = json.loads((root / "sbom.cdx.json").read_text())
    sbom_title = re.match(r"^# Software bill of materials: Infinite AI Command Center (\S+)$", (root / "SBOM.md").read_text().splitlines()[0])
    return {
        "VERSION": (root / "VERSION").read_text().strip(),
        "backend/app/__init__.py": match.group(1) if match else "(no __version__ line)",
        "dashboard/package.json": package.get("version", "(none)"),
        "dashboard/package-lock.json": lock.get("version", "(none)"),
        'dashboard/package-lock.json packages[""]': lock.get("packages", {}).get("", {}).get("version", "(none)"),
        # Regenerated with scripts/generate-sbom.py; a stale one names another release.
        "sbom.cdx.json": sbom.get("metadata", {}).get("component", {}).get("version", "(none)"),
        "SBOM.md": sbom_title.group(1) if sbom_title else "(no title line)",
    }


def check_versions(root: Path = ROOT) -> str:
    """The release version, or exit if any version constant disagrees with the VERSION file."""
    found = version_constants(root)
    version = found["VERSION"]
    wrong = [f"{where}: {value}" for where, value in found.items() if value != version]
    if wrong:
        sys.exit(f"refusing to build the package: VERSION is {version}, but\n  " + "\n  ".join(wrong))
    return version


def entry_time(now: datetime) -> tuple[int, int, int, int, int, int]:
    """The timestamp every file gets in the zip: this build's time, so it differs from every earlier release.

    A fixed timestamp once made upgrades build stale images: Docker (BuildKit) treats a file it builds
    from as unchanged when its size and modification time match its copy from an earlier build (of any
    folder of the project), so a new release kept same-size files (the version numbers) from the first
    build. Zip times have no time zone and extractors read them as local time; 14 hours earlier is in the
    past in every zone.
    """
    t = (now - timedelta(hours=14)).astimezone(UTC)
    return (t.year, t.month, t.day, t.hour, t.minute, t.second - t.second % 2)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "releases"))
    args = parser.parse_args()
    version = check_versions()
    files = candidate_files()
    check(files)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{TOP}-{version}-windows.zip"
    now = datetime.now(UTC)
    stamp = entry_time(now)
    manifest = [f"# {TOP} {version} - built {now:%Y-%m-%dT%H:%M:%SZ}", "# sha256  path"]
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f in files:
            data = (ROOT / f).read_bytes()
            if f.endswith(".ps1"):  # as a Windows git checkout has them (.gitattributes)
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            manifest.append(f"{hashlib.sha256(data).hexdigest()}  {f}")
            info = zipfile.ZipInfo(f"{TOP}/{f}", date_time=stamp)
            info.external_attr = (0o755 if f.endswith(".sh") else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
        zf.writestr(f"{TOP}/MANIFEST.txt", "\n".join(manifest) + "\n")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (out_dir / f"{target.name}.sha256").write_text(f"{digest}  {target.name}\n")
    print(f"{target}  ({len(files)} files, {target.stat().st_size / 1024:.0f} KiB)")
    print(f"sha256 {digest}")


if __name__ == "__main__":
    main()
