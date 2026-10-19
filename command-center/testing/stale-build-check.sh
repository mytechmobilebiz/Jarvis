#!/usr/bin/env bash
# Regression test for stale images built from an upgraded folder (fixed in 0.5.1). Needs Docker and the
# PowerShell image; development only (testing/ is not in the Windows package).
#
# Docker (BuildKit) keeps a copy of the build folder (shared by every folder of the project) and skips files
# whose size and modification time match that copy. Up to 0.5.0 every file in the package had the same timestamp, so a release extracted over the
# previous one (Windows Explorer keeps the zip's timestamps) kept same-size files from the first build: the
# API of a 0.5.0 install reported 0.2.0. This script checks the three defences:
#
#   1. Package timestamps: the old release, then the new package, extracted into one folder and built with a
#      plain "docker compose build" (no help from the scripts): both images must report the new version.
#   2. The build check: with Docker's copy made stale on purpose, a build given CC_RELEASE_VERSION must fail.
#   3. scripts/prepare-build.ps1 (run by install.ps1 and start.ps1): after it, the same build must succeed
#      with the new version in both images.
#
#   testing/stale-build-check.sh OLD-RELEASE.zip NEW-PACKAGE.zip
#
# It rebuilds the infinite-command-center/backend:local and dashboard:local images (run it where nothing
# depends on them). Set REGISTRY / EXTRA_CA_BUNDLE as for docker compose if the network needs them.
set -euo pipefail
OLD_ZIP=$(realpath "$1"); NEW_ZIP=$(realpath "$2")
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
PWSH_IMAGE=${PWSH_IMAGE:-mcr.microsoft.com/powershell:7.4-ubuntu-22.04}
FAILED=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILED=1; }

extract() {  # zip, destination: like Explorer's "Extract All" (overwrite, keep each file's zip timestamp)
  python3 - "$1" "$2" <<'EOF'
import os, sys, time, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    for info in z.infolist():
        path = z.extract(info, sys.argv[2])
        if not info.is_dir():
            t = time.mktime(info.date_time + (0, 0, -1))
            os.utime(path, (t, t))
EOF
}

dummy_env() {  # compose needs values for its required variables; nothing is started
  cat > "$1/.env" <<EOF
ADMIN_EMAIL=check@example.test
ADMIN_PASSWORD=stale-build-check-1
POSTGRES_PASSWORD=stale-build-check-2
CC_DB_PASSWORD=stale-build-check-3
N8N_DB_PASSWORD=stale-build-check-4
CREDIT_REPAIR_DB_PASSWORD=stale-build-check-5
CC_SERVICE_TOKEN=stale-build-check-6
CC_SECRETS_KEY=stale-build-check-7
CREDIT_REPAIR_DATA_KEY=stale-build-check-8
N8N_ENCRYPTION_KEY=stale-build-check-9
N8N_WEBHOOK_SECRET=stale-build-check-10
N8N_OWNER_PASSWORD=stale-build-check-11
${REGISTRY:+REGISTRY=$REGISTRY}
${EXTRA_CA_BUNDLE:+EXTRA_CA_BUNDLE=$EXTRA_CA_BUNDLE}
EOF
}

image_versions() {
  local api dash
  api=$(docker run --rm --entrypoint python infinite-command-center/backend:local -c "import app; print(app.__version__)")
  dash=$(docker run --rm --entrypoint cat infinite-command-center/dashboard:local /usr/share/nginx/html/version.json \
         | python3 -c "import json, sys; print(json.load(sys.stdin)['version'])")
  echo "$api $dash"
}

build() {  # folder, then extra environment for compose (e.g. CC_RELEASE_VERSION=...)
  local dir=$1; shift
  (cd "$dir" && env "$@" docker compose build api dashboard > "$WORK/build.log" 2>&1)
}

build_one() {  # folder, service, then extra environment
  local dir=$1 service=$2; shift 2
  (cd "$dir" && env "$@" docker compose build "$service" > "$WORK/build.log" 2>&1)
}

# ---------------------------------------------------------------- 1. package timestamps
A="$WORK/upgraded"; mkdir -p "$A"
extract "$OLD_ZIP" "$A"; F="$A/InfiniteAI-CommandCenter"; OLD=$(cat "$F/VERSION"); dummy_env "$F"
build "$F" || { cat "$WORK/build.log"; exit 1; }
extract "$NEW_ZIP" "$A"; NEW=$(cat "$F/VERSION"); dummy_env "$F"
echo "== $OLD, then $NEW extracted over it in $F"
build "$F" || { tail -30 "$WORK/build.log"; exit 1; }
got=$(image_versions)
[ "$got" = "$NEW $NEW" ] && pass "plain docker compose build after extracting over $OLD: API and dashboard report $NEW" \
                        || fail "plain docker compose build after extracting over $OLD: API and dashboard report $got, expected $NEW $NEW"

# ---------------------------------------------------------------- 2 and 3. a stale Docker copy, made on purpose
B="$WORK/stale"; mkdir -p "$B"
extract "$NEW_ZIP" "$B"; G="$B/InfiniteAI-CommandCenter"; dummy_env "$G"
FILES=(backend/app/__init__.py dashboard/package.json dashboard/package-lock.json)
# A timestamp no earlier build used: Docker's copy is shared by every folder of the project, so a common
# time could match files from step 1 instead of the marker files below.
STAMP="@$(( $(date +%s) - 86400 * 30 - RANDOM ))"
MARK=$(echo "$NEW" | tr '0-8' '9')  # same length, so the files keep their size
ESC=${NEW//./\\.}
for f in "${FILES[@]}"; do cp "$G/$f" "$WORK/$(basename "$f").real"; done
sed -i "s/^__version__ = \"$ESC\"$/__version__ = \"$MARK\"/" "$G/backend/app/__init__.py"
sed -i "0,/\"version\": \"$ESC\"/s//\"version\": \"$MARK\"/" "$G/dashboard/package.json"
for _ in 1 2; do sed -i "0,/\"version\": \"$ESC\"/s//\"version\": \"$MARK\"/" "$G/dashboard/package-lock.json"; done  # top and packages[""]
for f in "${FILES[@]}"; do touch -d "$STAMP" "$G/$f"; done
build "$G" || { tail -30 "$WORK/build.log"; exit 1; }
[ "$(image_versions)" = "$MARK $MARK" ] || { echo "setup failed: could not build the marker version $MARK"; exit 1; }
for f in "${FILES[@]}"; do cp "$WORK/$(basename "$f").real" "$G/$f"; touch -d "$STAMP" "$G/$f"; done
build "$G" || { tail -30 "$WORK/build.log"; exit 1; }
got=$(image_versions)
if [ "$got" != "$MARK $MARK" ]; then
  echo "note: this Docker does not keep same-size, same-time files (images report $got); checks 2 and 3 are not meaningful here"
  exit $FAILED
fi
echo "== reproduced: the folder says $NEW, a plain build still reports $got"
for service in api dashboard; do
  if build_one "$G" "$service" CC_RELEASE_VERSION="$NEW"; then
    fail "$service: a build with CC_RELEASE_VERSION=$NEW from a stale Docker copy succeeded"
  elif msg=$(grep -oE "[a-z_/.]+ in this build says [0-9.]+, but this release is [0-9.]+" "$WORK/build.log" | head -1); [ -n "$msg" ]; then
    pass "$service: the build check stopped the stale build ($msg)"
  else
    tail -20 "$WORK/build.log"; fail "$service: the build failed, but not with the build check's message"
  fi
done
# prepare-build.ps1 runs in PowerShell; the build then runs from this shell, the same Docker client as the
# stale builds above (buildx keys its copy of a folder by the folder and the client's ID in ~/.docker).
got_env=$(docker run --rm -v "$WORK:$WORK" -w "$G" "$PWSH_IMAGE" pwsh -NoProfile -Command \
  '& ./scripts/prepare-build.ps1 | Out-Host; if ($LASTEXITCODE -ne 0) { exit 1 }; "CC_RELEASE_VERSION=$env:CC_RELEASE_VERSION"' | tail -1) || true
[ "$got_env" = "CC_RELEASE_VERSION=$NEW" ] && pass "prepare-build.ps1 sets $got_env for the build" \
                                         || fail "prepare-build.ps1: expected CC_RELEASE_VERSION=$NEW, got '$got_env'"
if build "$G" CC_RELEASE_VERSION="$NEW"; then
  got=$(image_versions)
  [ "$got" = "$NEW $NEW" ] && pass "after prepare-build.ps1 the checked build is current: API and dashboard report $NEW" \
                          || fail "after prepare-build.ps1: API and dashboard report $got, expected $NEW $NEW"
else
  tail -20 "$WORK/build.log"; fail "after prepare-build.ps1 the checked build still failed"
fi
exit $FAILED
