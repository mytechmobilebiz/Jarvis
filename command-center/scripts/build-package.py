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
import re
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
TOP = "InfiniteAI-CommandCenter"

DEV_ONLY = (
    "backend/tests/",
    "backend/pytest.ini",
    "backend/requirements-dev.txt",
    "dashboard/src/test/",
    "e2e/",
    "testing/",
    "docker-compose.testing.yml",
    "scripts/build-package.py",
    "scripts/init-env.sh",
    "scripts/generate-sbom.py",
    "releases/",
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
    problems = []
    for f in files:
        name = PurePosixPath(f).name
        if name in FORBIDDEN_NAMES or name.endswith(FORBIDDEN_SUFFIXES) or (name.startswith(".env") and name != ".env.example"):
            problems.append(f"{f}: looks like a credential or data file")
            continue
        data = (ROOT / f).read_bytes()
        for value in secrets:
            if value.encode() in data:
                problems.append(f"{f}: contains a value from the local .env")
                break
    if problems:
        sys.exit("refusing to build the package:\n  " + "\n  ".join(problems))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "releases"))
    args = parser.parse_args()
    version = (ROOT / "VERSION").read_text().strip()
    files = candidate_files()
    check(files)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{TOP}-{version}-windows.zip"
    manifest = [f"# {TOP} {version} - built {datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}", "# sha256  path"]
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f in files:
            data = (ROOT / f).read_bytes()
            manifest.append(f"{hashlib.sha256(data).hexdigest()}  {f}")
            info = zipfile.ZipInfo(f"{TOP}/{f}", date_time=(2026, 1, 1, 0, 0, 0))
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
