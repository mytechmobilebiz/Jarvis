#!/usr/bin/env python3
"""Builds releases/jarvis-migration-kit-<version>.zip and its .sha256 (development only).

The zip holds one folder, jarvis-migration-kit, with the four files the owner needs; Explorer's Extract All to
C:\\InfiniteAI\\JarvisMigration gives C:\\InfiniteAI\\JarvisMigration\\jarvis-migration-kit.

    python make_kit.py [--out DIR]
"""

import argparse
import hashlib
import os
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from jarvis_kit import KIT_VERSION  # noqa: E402

FILES = ("jarvis-kit.ps1", "jarvis_kit.py", "templates.json", "README.md")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.join(HERE, "..", "..", "releases"))
    args = parser.parse_args()
    name = "jarvis-migration-kit-%s.zip" % KIT_VERSION
    path = os.path.join(os.path.abspath(args.out), name)
    stamp = time.gmtime()[:6]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for file in FILES:
            with open(os.path.join(HERE, file), "rb") as f:
                data = f.read()
            if file.endswith(".ps1"):
                # Windows line endings, as the Command Center's own scripts.
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            z.writestr(zipfile.ZipInfo("jarvis-migration-kit/" + file, date_time=stamp), data)
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    with open(path + ".sha256", "w", encoding="ascii") as f:
        f.write("%s  %s\n" % (digest, name))
    print("%s\nsha256 %s" % (path, digest))


if __name__ == "__main__":
    main()
