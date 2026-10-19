#!/usr/bin/env bash
# Behaviour test of scripts/prepare-build.ps1 in PowerShell 7 (development only; testing/ is not in the package).
# Each case extracts the package (keeping the zip's timestamps, as Windows Explorer does) and runs the script.
#
#   testing/prepare-build-check.sh PACKAGE.zip
set -euo pipefail
ZIP=$(realpath "$1")
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
PWSH_IMAGE=${PWSH_IMAGE:-mcr.microsoft.com/powershell:7.4-ubuntu-22.04}
FAILED=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILED=1; }

setup() {  # case folder (may contain subfolders, e.g. dist/x); prints the installation folder
  python3 - "$ZIP" "$WORK/$1" <<'EOF'
import os, sys, time, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    for info in z.infolist():
        path = z.extract(info, sys.argv[2])
        if not info.is_dir():
            t = time.mktime(info.date_time + (0, 0, -1))
            os.utime(path, (t, t))
EOF
  echo "$WORK/$1/InfiniteAI-CommandCenter"
}

run() {  # installation folder; prints the script's output, then "exit=<code> env=<CC_RELEASE_VERSION>"
  docker run --rm -v "$WORK:$WORK" -w "$1" "$PWSH_IMAGE" pwsh -NoProfile -Command \
    '& ./scripts/prepare-build.ps1; "exit=$LASTEXITCODE env=$env:CC_RELEASE_VERSION"' 2>&1
}

mtime() { stat -c %Y "$1"; }
refreshed() { sed -n 's/.*refreshed (\([0-9]*\) files).*/\1/p' <<<"$1" | head -1; }

F=$(setup normal); V=$(cat "$F/VERSION"); before=$(mtime "$F/backend/app/__init__.py")
out=$(run "$F")
n=$(refreshed "$out")
[[ "$out" == *"exit=0 env=$V"* && ${n:-0} -gt 100 && $(mtime "$F/backend/app/__init__.py") -gt $before ]] \
  && pass "normal folder: files refreshed, CC_RELEASE_VERSION=$V" || fail "normal folder: $out"

F=$(setup dist/inside)  # a parent folder named like a skipped one must not hide the files
out=$(run "$F")
n=$(refreshed "$out")
[[ "$out" == *"exit=0 env=$V"* && ${n:-0} -gt 100 ]] \
  && pass "under a folder named dist: files still refreshed" || fail "under a folder named dist: $out"

F=$(setup readonly); chmod 444 "$F/backend/app/__init__.py"; before=$(mtime "$F/backend/app/__init__.py")
out=$(run "$F")
[[ "$out" == *"exit=0 env=$V"* && $(mtime "$F/backend/app/__init__.py") -gt $before && $(( $(stat -c '0%a' "$F/backend/app/__init__.py") & 0200 )) -eq 0 ]] \
  && pass "read-only file: refreshed and still read-only" || fail "read-only file: $out"

F=$(setup mixed); sed -i "s/__version__ = \"$V\"/__version__ = \"0.0.1\"/" "$F/backend/app/__init__.py"
touch -d "2026-01-01" "$F/dashboard/package.json"; before=$(mtime "$F/dashboard/package.json")
out=$(run "$F")
[[ "$out" == *"this folder mixes releases"* && "$out" == *"exit=1 env="* && "$out" != *"env=$V"* && $(mtime "$F/dashboard/package.json") -eq $before ]] \
  && pass "mixed folder: refused before touching anything, no version passed on" || fail "mixed folder: $out"

F=$(setup noversion); rm "$F/VERSION"
out=$(run "$F")
[[ "$out" == *"Could not prepare the build folders"* && "$out" == *"exit=1 env="* && "$out" != *"env=$V"* ]] \
  && pass "missing VERSION file: refused" || fail "missing VERSION file: $out"

exit $FAILED
