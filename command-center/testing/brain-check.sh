#!/usr/bin/env bash
# End-to-end check of the JARVIS Brain operations layer (docs/JARVIS_BRAIN_SPEC.md 6.7; development only, testing/ is
# not in the package): scripts/brain-precheck.ps1, brain-test.ps1 and jarvis-import.ps1 in PowerShell 7 (Docker image
# mcr.microsoft.com/powershell), a copy of command-center without a .env as the test folder, and the synthetic JARVIS
# installation of migration/jarvis/tests/make_fixture.py.
#
#   testing/brain-check.sh [--require-importer]
#
# The Compose project "infinite-command-center" on this machine plays the owner's live installation: this check never
# stops, recreates, rebuilds, retags or changes it, and fails if its fingerprint (images, containers with their start
# times, volumes, networks) changes at any point. cc-test-pg and decoy-* containers are watched the same way. The
# test copy is the project cc-brain-test; everything it creates is removed at the end (also after a failure).
#
# Now: every .ps1 parses (ASCII only); brain-precheck.ps1; refusals before anything runs; brain-test.ps1 Start (build
# with the test copy's own image names, health, versions against VERSION, instance_label and the cookie name); the
# app role's terse PostgreSQL error log (no DETAIL);
# configuration and running-container assertions; the worker without internet; the hardened importer container and
# a missing bind source; jarvis-import.ps1 refusals and its -Target Test wiring; the pre-import dump function; the
# kit-made inputs; restart persistence and Stop/Start; COMPOSE_* in the environment; -SeedFromBackup; Remove; the
# pre-check again. Steps that need "python -m app.cli brain-import" and /api/brain (spec 6.7 steps 5, 6, 9 and the
# planted-value check of 11) run only when the test copy's image has them; otherwise they are reported as SKIP (with
# --require-importer, as FAIL). Exit status: 0 all ran steps passed, 1 a step failed.
#
# Environment: REGISTRY and EXTRA_CA_BUNDLE as for docker compose (passed to the scripts' builds); PWSH_IMAGE;
# KEEP_WORK=1 keeps the work folder (logs, reports) for inspection; BRAIN_UI=1 (with BRAIN_UI_PYTHON, a Python with
# Playwright) adds the Brain page in a browser after the import (testing/brain-ui-check.py);
# PSSCRIPTANALYZER=<folder with the module> adds its Windows PowerShell 5.1 compatibility rules. The test copy's port
# is 8190.
set -euo pipefail
CC=$(cd "$(dirname "$0")/.." && pwd)
REQUIRE_IMPORTER=0
[ "${1:-}" = "--require-importer" ] && REQUIRE_IMPORTER=1
PWSH_IMAGE=${PWSH_IMAGE:-mcr.microsoft.com/powershell:7.4-ubuntu-22.04}
PLUGINS=${DOCKER_CLI_PLUGINS:-/usr/libexec/docker/cli-plugins}
PORT=8190
LIVE_PROJECT=infinite-command-center
TEST_PROJECT=cc-brain-test
WORK=$(mktemp -d "${TMPDIR:-/tmp}/brain-check.XXXXXX")
TEST="$WORK/C_InfiniteAI/v0.6.0-brain-test/InfiniteAI-CommandCenter"
MIG="$WORK/C_InfiniteAI/JarvisMigration"
LOGS="$WORK/logs"
mkdir -p "$TEST" "$MIG" "$LOGS"
FAILED=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILED=1; }
skip() { if [ "$REQUIRE_IMPORTER" = 1 ]; then fail "$* (needs brain-import; --require-importer)"; else echo "SKIP  $* (needs brain-import and /api/brain: runs after the merge)"; fi; }

# ------------------------------------------------------------------------------------------------ cleanup (always)
cleanup() {
  local code=$?
  # Only what carries the cc-brain-test project label, and the test copy's own images.
  local ids
  ids=$(docker ps -aq --filter "label=com.docker.compose.project=$TEST_PROJECT")
  [ -n "$ids" ] && docker rm -f $ids > /dev/null 2>&1 || true
  ids=$(docker volume ls -q --filter "label=com.docker.compose.project=$TEST_PROJECT")
  [ -n "$ids" ] && docker volume rm $ids > /dev/null 2>&1 || true
  ids=$(docker network ls -q --filter "label=com.docker.compose.project=$TEST_PROJECT")
  [ -n "$ids" ] && docker network rm $ids > /dev/null 2>&1 || true
  for image in infinite-command-center-brain-test/backend:local infinite-command-center-brain-test/dashboard:local; do
    docker image inspect "$image" > /dev/null 2>&1 && docker image rm "$image" > /dev/null 2>&1 || true
  done
  # Files written by root inside containers: removed through a container, then the folder.
  if [ "${KEEP_WORK:-0}" = 1 ]; then echo "work folder kept: $WORK"; else
    docker run --rm -v "$WORK:/w" --entrypoint sh "$PWSH_IMAGE" -c 'rm -rf /w/* /w/.[!.]*' > /dev/null 2>&1 || true
    rm -rf "$WORK"
  fi
  exit $code
}
trap cleanup EXIT

# ------------------------------------------------------------------------------------------------ the live installation
fingerprint() {  # the scripts' fingerprint (6.4) plus networks, and the containers other engineers use
  {
    for image in infinite-command-center/backend:local infinite-command-center/dashboard:local; do
      echo "image $image $(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null || echo '(none)')"
    done
    local ids
    ids=$(docker ps -aq --no-trunc --filter "label=com.docker.compose.project=$LIVE_PROJECT")
    [ -n "$ids" ] && docker inspect --format 'container {{.Name}} {{.Id}} {{.Image}} {{.State.StartedAt}}' $ids
    docker volume ls -q --filter "label=com.docker.compose.project=$LIVE_PROJECT" | sed 's/^/volume /'
    docker network ls -q --no-trunc --filter "label=com.docker.compose.project=$LIVE_PROJECT" | sed 's/^/network /'
    ids=$(docker ps -aq --no-trunc --filter name=^cc-test-pg$ --filter name=^decoy-)
    [ -n "$ids" ] && docker inspect --format 'other {{.Name}} {{.Id}} {{.Image}} {{.State.StartedAt}}' $ids
  } | LC_ALL=C sort
}
FP0=$(fingerprint)
echo "$FP0" > "$LOGS/fingerprint-start.txt"
check_fp() {  # after every step
  local now
  now=$(fingerprint)
  if [ "$now" = "$FP0" ]; then pass "live installation unchanged after: $1"; else
    fail "LIVE INSTALLATION CHANGED after: $1"
    diff <(echo "$FP0") <(echo "$now") || true
  fi
}
LIVE=$(docker ps -q --filter "label=com.docker.compose.project=$LIVE_PROJECT" --filter label=com.docker.compose.service=api \
  | head -1 | xargs -r docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')
[ -n "$LIVE" ] || { echo "No running $LIVE_PROJECT api: this check needs the stand-in live installation."; exit 1; }
echo "live installation: $LIVE"

# ------------------------------------------------------------------------------------------------ the test folder
# What git tracks or would track (like the package), without .env: the owner's new, separate folder.
(cd "$CC" && git ls-files --cached --others --exclude-standard -z .) | python3 -c '
import os, shutil, sys
src, dst = sys.argv[1], sys.argv[2]
for f in sys.stdin.buffer.read().decode().split("\0"):
    if f and os.path.isfile(os.path.join(src, f)):
        os.makedirs(os.path.dirname(os.path.join(dst, f)), exist_ok=True)
        shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
' "$CC" "$TEST"
rm -f "$TEST/.env" "$TEST/.env.brain-test"
python3 "$CC/migration/jarvis/tests/make_fixture.py" "$WORK/inst" > "$WORK/fixture.json"
FIX_PROFILE=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['profile'])" "$WORK/fixture.json")
FIX_CONTENT=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['content'])" "$WORK/fixture.json")
FIX_DB=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['db'])" "$WORK/fixture.json")
VERSION=$(cat "$TEST/VERSION")

# ------------------------------------------------------------------------------------------------ PowerShell runner
PS_ENV=()   # extra "-e NAME=VALUE" for the next psh call
PS_PRE=""   # PowerShell run before the script (stubs)
psh() {  # answer for Read-Host, folder to run in, script, then its parameters; prints the output and "exit=<code>"
  local answer=$1 dir=$2 script=$3; shift 3
  # Typed confirmations (and the kit's passphrase) come from a Read-Host function of the same name: no console here.
  local a="'${answer//\'/\'\'}'"
  local stub="function Read-Host { param([switch]\$AsSecureString, [Parameter(ValueFromRemainingArguments)]\$Rest) if (\$AsSecureString) { ConvertTo-SecureString $a -AsPlainText -Force } else { $a } }"
  local quoted=""
  for a in "$@"; do
    if [[ "$a" =~ ^-[A-Za-z]+$ ]]; then quoted+=" $a"; else quoted+=" '${a//\'/\'\'}'"; fi
  done
  local mounts=(-v /usr/bin/docker:/usr/bin/docker:ro -v "$PLUGINS:/usr/libexec/docker/cli-plugins:ro"
                -v /var/run/docker.sock:/var/run/docker.sock -v "$WORK:$WORK" -v "$LIVE:$LIVE:ro")
  [ -n "${EXTRA_CA_BUNDLE:-}" ] && mounts+=(-v "$EXTRA_CA_BUNDLE:$EXTRA_CA_BUNDLE:ro")
  local envs=()
  [ -n "${REGISTRY:-}" ] && envs+=(-e "REGISTRY=$REGISTRY")
  [ -n "${EXTRA_CA_BUNDLE:-}" ] && envs+=(-e "EXTRA_CA_BUNDLE=$EXTRA_CA_BUNDLE")
  docker run --rm --network host "${mounts[@]}" "${envs[@]}" "${PS_ENV[@]}" -w "$dir" "$PWSH_IMAGE" pwsh -NoProfile \
    -Command "$stub; $PS_PRE; & '$script'$quoted; \"exit=\$LASTEXITCODE\"" 2>&1
}
exit_of() { sed -n 's/^exit=\([0-9]*\)\r\{0,1\}$/\1/p' <<<"$1" | tail -1; }
run_step() {  # name, then psh arguments; output in $LOGS/<name>.log and in $OUT
  local name=$1; shift
  OUT=$(psh "$@" || true)
  echo "$OUT" > "$LOGS/$name.log"
  CODE=$(exit_of "$OUT")
}
show() { echo "----- $1 (last lines) -----"; tail -25 "$LOGS/$1.log"; }

BT="$TEST/scripts/brain-test.ps1"
JI="$TEST/scripts/jarvis-import.ps1"
PC="$TEST/scripts/brain-precheck.ps1"
compose_test() {  # the scripts' Compose call for the test copy, from bash (for checks only)
  docker compose -p "$TEST_PROJECT" -f "$TEST/docker-compose.yml" -f "$TEST/docker-compose.brain-test.yml" \
    --env-file "$TEST/.env.brain-test" "$@"
}
cid() { docker ps -q --filter "label=com.docker.compose.project=$TEST_PROJECT" --filter "label=com.docker.compose.service=$1" | head -1; }
tpsql() { docker exec -i "$(cid postgres)" psql -U postgres -d command_center -X -q -t -A -v ON_ERROR_STOP=1 "$@"; }
envval() { sed -n "s/^$1=//p" "$TEST/.env.brain-test" | tail -1; }
wait_health() {
  for _ in $(seq 1 80); do
    curl -fsS "http://127.0.0.1:$PORT/api/health" > /dev/null 2>&1 && return 0
    sleep 3
  done
  return 1
}
test_left() {  # containers, volumes, networks and images of the test copy still present
  echo "$(docker ps -aq --filter "label=com.docker.compose.project=$TEST_PROJECT" | wc -l)" \
       "$(docker volume ls -q --filter "label=com.docker.compose.project=$TEST_PROJECT" | wc -l)" \
       "$(docker network ls -q --filter "label=com.docker.compose.project=$TEST_PROJECT" | wc -l)" \
       "$(docker images -q --filter 'reference=infinite-command-center-brain-test/*' | wc -l)"
}
login() {  # signs in to the test copy as its own admin; cookie jar in $WORK/jar
  python3 - "$(envval ADMIN_EMAIL)" "$(envval ADMIN_PASSWORD)" > "$WORK/login.json" <<'EOF'
import json, sys
print(json.dumps({"email": sys.argv[1], "password": sys.argv[2]}))
EOF
  rm -f "$WORK/jar"
  curl -sS -c "$WORK/jar" -o "$WORK/login.out" -w '%{http_code}' -H 'content-type: application/json' \
    --data-binary @"$WORK/login.json" "http://127.0.0.1:$PORT/api/auth/login"
}
api_get() { curl -sS -b "$WORK/jar" "http://127.0.0.1:$PORT$1"; }
api_post() { curl -sS -b "$WORK/jar" -H 'content-type: application/json' -H 'x-requested-with: command-center' --data-binary "$2" "http://127.0.0.1:$PORT$1"; }
jq_py() { python3 -c "import json, sys; d = json.load(sys.stdin); print($1)"; }

# ================================================================================================ steps (now)

step_parse() {  # every PowerShell script of the package parses; ASCII only (Windows PowerShell 5.1 reads ANSI)
  cat > "$WORK/parse.ps1" <<'EOF'
$bad = 0; $n = 0
foreach ($f in (Get-ChildItem -Path $args[0] -Recurse -Filter *.ps1 | Where-Object { $_.FullName -notmatch '[\\/]testing[\\/]' })) {
  $n++; $tokens = $null; $errors = $null
  $null = [System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$tokens, [ref]$errors)
  foreach ($e in $errors) { $bad++; "PARSE ERROR {0}:{1} {2}" -f $f.Name, $e.Extent.StartLineNumber, $e.Message }
  if (@([IO.File]::ReadAllBytes($f.FullName) | Where-Object { $_ -gt 127 }).Count) { $bad++; "NOT ASCII " + $f.Name }
  "parsed " + $f.FullName.Substring($args[0].Length + 1)
}
"scripts=$n problems=$bad"
EOF
  local out
  out=$(docker run --rm -v "$TEST:$TEST:ro" -v "$WORK:$WORK:ro" "$PWSH_IMAGE" pwsh -NoProfile -File "$WORK/parse.ps1" "$TEST" 2>&1)
  echo "$out" > "$LOGS/parse.log"
  [[ "$out" == *"problems=0"* ]] && pass "PowerShell parser: $(grep -o 'scripts=[0-9]*' <<<"$out") .ps1 files of the package parse, all ASCII" \
    || { fail "parse"; cat "$LOGS/parse.log"; }
  # Optional: PSScriptAnalyzer's Windows PowerShell 5.1 compatibility rules, when a copy of the module is given
  # (PSSCRIPTANALYZER=<folder holding PSScriptAnalyzer/<version>/PSScriptAnalyzer.psd1>; nothing is downloaded here).
  if [ -n "${PSSCRIPTANALYZER:-}" ]; then
    cat > "$WORK/psa.ps1" <<'EOF'
Import-Module (Get-ChildItem /psmod -Recurse -Filter PSScriptAnalyzer.psd1 | Select-Object -First 1).FullName
$p = "win-48_x64_10.0.17763.0_5.1.17763.316_x64_4.0.30319.42000_framework"
$settings = @{ IncludeRules = @("PSUseCompatibleSyntax", "PSUseCompatibleCommands", "PSUseCompatibleTypes"); Rules = @{
  PSUseCompatibleSyntax = @{ Enable = $true; TargetVersions = @("5.1") }
  PSUseCompatibleCommands = @{ Enable = $true; TargetProfiles = @($p) }
  PSUseCompatibleTypes = @{ Enable = $true; TargetProfiles = @($p) } } }
$files = @(Get-ChildItem -Path (Join-Path $args[0] "scripts") -Filter *.ps1) + @(Get-ChildItem -Path (Join-Path $args[0] "migration/jarvis") -Filter *.ps1)
$found = @($files | ForEach-Object { Invoke-ScriptAnalyzer -Path $_.FullName -Settings $settings })
foreach ($x in $found) { "{0}:{1} {2}" -f $x.ScriptName, $x.Line, $x.Message }
"files=$($files.Count) findings=$($found.Count)"
EOF
    out=$(docker run --rm -v "$PSSCRIPTANALYZER:/psmod:ro" -v "$TEST:$TEST:ro" -v "$WORK:$WORK:ro" "$PWSH_IMAGE" pwsh -NoProfile \
      -File "$WORK/psa.ps1" "$TEST" 2>&1)
    echo "$out" > "$LOGS/psscriptanalyzer.log"
    [[ "$out" == *"findings=0"* ]] && pass "PSScriptAnalyzer: $(grep -o 'files=[0-9]*' <<<"$out") scripts, no Windows PowerShell 5.1 syntax, command or type incompatibility" \
      || { fail "PSScriptAnalyzer 5.1 compatibility"; tail -20 "$LOGS/psscriptanalyzer.log"; }
  fi
}

step_precheck() {  # 2. brain-precheck.ps1, read-only; the report is compared again at the end
  run_step precheck-1 "" "$TEST" "$PC" -OutDir "$MIG"
  REPORT1=$(ls "$MIG"/precheck-*.txt 2>/dev/null | head -1 || true)
  if [ "$CODE" = 0 ] && [ -n "$REPORT1" ] && grep -q "^\[fingerprint\]" "$REPORT1" && grep -q "0006_brain:" "$REPORT1" \
     && grep -q "folder: $LIVE" "$REPORT1"; then
    pass "brain-precheck.ps1 wrote its report (fingerprint, live folder, migrations, counts)"
  else fail "brain-precheck.ps1 (exit $CODE)"; show precheck-1; fi
  # The report's fingerprint is the one this check computes independently.
  if diff <(tr -d '\r' < "$REPORT1" | sed -n '/^\[fingerprint\]/,/^$/p' | sed -n 's/^  //p' | LC_ALL=C sort) \
          <(echo "$FP0" | grep -E '^(image|container|volume) ' | sed 's#^container /#container #' | LC_ALL=C sort) > /dev/null; then
    pass "the precheck fingerprint matches an independent reading"
  else fail "the precheck fingerprint differs from an independent reading"; fi
  check_fp "precheck"
}

step_refusals_before_start() {  # refusals that must leave nothing behind (no .env.brain-test, no container)
  local before after
  # A folder with a .env is (a copy of) the live installation.
  local bad="$WORK/with-env/InfiniteAI-CommandCenter"; mkdir -p "$bad"; cp -r "$TEST/." "$bad/"; echo "ADMIN_EMAIL=x@y.z" > "$bad/.env"
  run_step refuse-env "" "$bad" "$bad/scripts/brain-test.ps1" -Action Start
  [[ "$CODE" = 2 && "$OUT" == *"NOT RUN"*".env"* && ! -e "$bad/.env.brain-test" && "$(test_left)" == "0 0 0 0" ]] \
    && pass "a folder with a .env is refused before anything (no .env.brain-test, nothing created)" || { fail "folder with .env"; show refuse-env; }
  # The live folder itself, found through the container label (checked with the same function the scripts use).
  local same
  same=$(docker run --rm -v "$TEST:$TEST:ro" "$PWSH_IMAGE" pwsh -NoProfile -Command \
    ". '$TEST/scripts/brain-common.ps1'; (Test-SameFolder 'C:\\InfiniteAI\\InfiniteAI-CommandCenter' 'c:/infiniteai/InfiniteAI-CommandCenter/'), (Test-SameFolder 'C:\\InfiniteAI\\A' 'C:\\InfiniteAI\\AB'), (Test-Inside 'C:\\Users\\o\\AppData\\Local\\hermes\\profiles\\jarvis\\state.db' 'C:\\Users\\o\\AppData\\Local\\Hermes'), (Test-Inside 'C:\\Users\\o\\AppData\\Local\\hermes-old\\x' 'C:\\Users\\o\\AppData\\Local\\hermes')" 2>&1 | tr -d '\r' | tr '\n' ' ')
  [ "$same" = "True False True False " ] && pass "folder comparison: case and trailing separators ignored, prefixes not confused" \
    || fail "folder comparison gave: $same"
  # Docker Compose older than 2.24.4 (a docker function stands in for the version), and a Docker Desktop version string.
  PS_PRE="function docker { if (\$args[0] -eq 'compose' -and \$args[1] -eq 'version') { '2.24.3'; \$global:LASTEXITCODE = 0; return }; & /usr/bin/docker @args }" \
    run_step refuse-compose "" "$TEST" "$BT" -Action Start
  [[ "$CODE" = 2 && "$OUT" == *"older than 2.24.4"* && ! -e "$TEST/.env.brain-test" && "$(test_left)" == "0 0 0 0" ]] \
    && pass "Docker Compose 2.24.3 is refused before anything" || { fail "old compose"; show refuse-compose; }
  PS_PRE="function docker { if (\$args[0] -eq 'compose' -and \$args[1] -eq 'version') { '2.24.4-desktop.1'; \$global:LASTEXITCODE = 0; return }; & /usr/bin/docker @args }" \
    run_step desktop-version "" "$TEST" "$BT" -Action Status
  [[ "$CODE" = 2 && "$OUT" != *"older than"* && "$OUT" == *".env.brain-test is missing"* ]] \
    && pass "Docker Desktop's '2.24.4-desktop.1' passes the version check (Status then refuses: no test copy yet)" || { fail "desktop version"; show desktop-version; }
  # Parameters.
  run_step refuse-action "" "$TEST" "$BT" -Action Destroy
  [[ "$CODE" = 2 ]] && pass "an unknown action is refused" || { fail "unknown action"; show refuse-action; }
  mkdir -p "$WORK/not-a-backup"
  run_step refuse-seed "" "$TEST" "$BT" -Action Start -SeedFromBackup "$WORK/not-a-backup"
  [[ "$CODE" = 2 && "$OUT" == *"no command_center.dump"* && ! -e "$TEST/.env.brain-test" ]] \
    && pass "-SeedFromBackup without command_center.dump is refused before anything" || { fail "seed folder"; show refuse-seed; }
  # The test copy's port taken by another program: refused before .env.brain-test is written.
  python3 -m http.server "$PORT" --bind 127.0.0.1 > /dev/null 2>&1 &
  local server=$!
  sleep 1
  run_step refuse-port "" "$TEST" "$BT" -Action Start
  kill "$server" 2>/dev/null || true; wait "$server" 2>/dev/null || true
  [[ "$CODE" = 2 && "$OUT" == *"port $PORT is in use"* && ! -e "$TEST/.env.brain-test" && "$(test_left)" == "0 0 0 0" ]] \
    && pass "port $PORT in use by another program: refused before anything" || { fail "port in use"; show refuse-port; }
  # A test secret equal to the live one (made-up files here; the function the scripts use).
  mkdir -p "$WORK/fake-live"
  local shared other
  shared=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))'); other=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
  printf 'CC_DB_PASSWORD=%s\nTZ=UTC\nCOOKIE_SECURE=false\n' "$shared" > "$WORK/fake-live/.env"
  printf 'CC_DB_PASSWORD=%s\nADMIN_PASSWORD=%s\nTZ=UTC\nCOOKIE_SECURE=false\n' "$shared" "$other" > "$WORK/fake-test.env"
  local secret_out
  secret_out=$(docker run --rm -v "$WORK:$WORK:ro" -v "$TEST:$TEST:ro" "$PWSH_IMAGE" pwsh -NoProfile -Command \
    ". '$TEST/scripts/brain-common.ps1'; Assert-TestSecretsDiffer '$WORK/fake-test.env' '$WORK/fake-live'; 'not refused'" 2>&1; echo "exit=$?")
  [[ "$secret_out" == *"NOT RUN"*"(CC_DB_PASSWORD)"* && "$secret_out" == *"exit=2"* && "$secret_out" != *"not refused"* ]] \
    && pass "a test secret equal to the live one is refused (only secret keys count: equal TZ and COOKIE_SECURE do not)" \
    || fail "shared secret: $secret_out"
  check_fp "refusals before start"
}

step_start() {  # 3. brain-test.ps1 -Action Start
  run_step start "" "$TEST" "$BT" -Action Start
  if [[ "$CODE" = 0 && "$OUT" == *"Live installation fingerprint unchanged"* && "$OUT" == *"Test copy running: http://127.0.0.1:$PORT"* ]]; then
    pass "brain-test.ps1 -Action Start (built, healthy, versions checked, fingerprint unchanged)"
  else fail "Start (exit $CODE)"; show start; return 1; fi
  [ -f "$TEST/.env.brain-test" ] && [ ! -e "$TEST/.env" ] && pass "secrets written to .env.brain-test only, never .env" || fail ".env.brain-test / .env"
  # Fresh secrets, none equal to the live .env's.
  local same
  same=$(python3 - "$TEST/.env.brain-test" "$LIVE/.env" <<'EOF'
import re, sys
def read(p):
    out = {}
    for line in open(p):
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if m: out[m.group(1)] = m.group(2).strip()
    return out
t, l = read(sys.argv[1]), read(sys.argv[2])
print(" ".join(k for k, v in t.items() if v and l.get(k) == v and re.search(r"(PASSWORD|TOKEN|SECRET|_KEY)$", k)))
EOF
)
  [ -z "$same" ] && pass "no secret of the test copy equals the live installation's" || fail "shared secrets: $same"
  # Distinct image tags, and the live tags untouched (the fingerprint covers their IDs).
  local api_image
  api_image=$(docker inspect --format '{{.Config.Image}}' "$(cid api)")
  [[ "$api_image" == "infinite-command-center-brain-test/backend:local" ]] && docker image inspect infinite-command-center-brain-test/dashboard:local > /dev/null \
    && pass "built as infinite-command-center-brain-test/{backend,dashboard}:local" || fail "test images: $api_image"
  check_fp "start"
}

step_config_assertions() {  # 4. the merged configuration and the running containers
  compose_test config --format json > "$WORK/config.json"
  local problems
  problems=$(python3 - "$WORK/config.json" "$PORT" <<'EOF'
import json, sys
c = json.load(open(sys.argv[1])); port = sys.argv[2]; p = []
s = c["services"]
if c.get("name") != "cc-brain-test": p.append("name %r" % c.get("name"))
if "n8n" in s: p.append("n8n present")
for n in ("api", "worker", "dashboard"):
    if not s[n]["image"].startswith("infinite-command-center-brain-test/"): p.append("%s image %s" % (n, s[n]["image"]))
if s["worker"].get("pull_policy") != "never": p.append("worker pull_policy")
if list(s["worker"]["networks"]) != ["internal"]: p.append("worker networks %s" % list(s["worker"]["networks"]))
ports = [(n, q.get("host_ip"), str(q.get("published")), q.get("target")) for n, v in s.items() for q in v.get("ports") or []]
if ports != [("dashboard", "127.0.0.1", port, 8080)]: p.append("ports %s" % ports)
for n, v in s.items():
    if v.get("restart") != "no": p.append("%s restart %s" % (n, v.get("restart")))
want = {"WORKFLOW_ENGINE": "none", "LLM_PROVIDER": "rule_based", "ANTHROPIC_API_KEY": "", "N8N_EDITOR_URL": "",
        "CC_INSTANCE_LABEL": "cc-brain-test", "SESSION_COOKIE_NAME": "cc_session_brain_test"}
for n in ("api", "worker"):
    for k, v in want.items():
        if s[n]["environment"].get(k) != v: p.append("%s %s" % (n, k))
print("; ".join(p))
EOF
)
  [ -z "$problems" ] && pass "merged config: name, images, port, worker network, no n8n, restart no, environment" || fail "config: $problems"
  # The running containers agree with it.
  local runtime
  runtime=$(for id in $(docker ps -aq --filter "label=com.docker.compose.project=$TEST_PROJECT"); do
    docker inspect --format '{{.Name}} {{.HostConfig.RestartPolicy.Name}} {{range $k, $v := .NetworkSettings.Networks}}{{$k}},{{end}} {{range $p, $b := .NetworkSettings.Ports}}{{range $b}}{{.HostIp}}:{{.HostPort}},{{end}}{{end}}' "$id"
  done | LC_ALL=C sort)
  echo "$runtime" > "$LOGS/runtime.txt"
  local internal
  internal=$(docker network inspect --format '{{.Internal}}' "${TEST_PROJECT}_internal")
  if grep -q "^/cc-brain-test-worker-1 no cc-brain-test_internal, $" <<<"$runtime" && [ "$internal" = true ] \
     && [ "$(grep -c '127.0.0.1:' <<<"$runtime")" = 1 ] && grep -q "^/cc-brain-test-dashboard-1 no .* 127.0.0.1:$PORT,$" <<<"$runtime" \
     && ! grep -qv ' no ' <<<"$runtime" && ! grep -q n8n <<<"$runtime"; then
    pass "running containers: worker only on cc-brain-test_internal (internal network), only 127.0.0.1:$PORT published, restart no, no n8n"
  else fail "running containers: $(tr '\n' ';' <<<"$runtime") internal=$internal"; fi
  check_fp "config assertions"
}

step_api_basics() {  # version against VERSION, banner field, cookie name
  local code status
  code=$(login)
  if [ "$code" = 200 ] && grep -q $'\tcc_session_brain_test\t' "$WORK/jar" && ! grep -q $'\tcc_session\t' "$WORK/jar"; then
    pass "sign-in on 127.0.0.1:$PORT sets the cookie cc_session_brain_test (not cc_session)"
  else fail "sign-in: HTTP $code"; fi
  status=$(api_get /api/system/status)
  local got
  got=$(jq_py '"%s|%s|%s" % (d.get("version"), d.get("instance_label"), "instance_label" in d)' <<<"$status")
  [ "$got" = "$VERSION|cc-brain-test|True" ] && pass "/api/system/status: version $VERSION (= VERSION), instance_label cc-brain-test" \
    || fail "/api/system/status gave $got"
  local dash
  dash=$(curl -sS "http://127.0.0.1:$PORT/version.json" | jq_py 'd["version"]')
  [ "$dash" = "$VERSION" ] && pass "dashboard version.json: $VERSION" || fail "dashboard version $dash"
  # The same token under the live installation's cookie name is not accepted.
  local token code2
  token=$(awk -F'\t' '$6 == "cc_session_brain_test" {print $7}' "$WORK/jar" | tail -1)
  code2=$(curl -sS -o /dev/null -w '%{http_code}' -H "cookie: cc_session=$token" "http://127.0.0.1:$PORT/api/auth/me")
  [ "$code2" = 401 ] && pass "a cc_session cookie is not accepted by the test copy" || fail "cc_session accepted: $code2"
  check_fp "API basics"
}

step_db_error_verbosity() {  # 0.6.1: the app role's errors reach the PostgreSQL log without the row's values
  local pg verbosity params since logs
  pg=$(cid postgres)
  verbosity=$(docker exec "$pg" psql -U command_center -d command_center -X -q -t -A -c "SHOW log_error_verbosity" 2>&1 || true)
  params=$(docker exec "$pg" psql -U command_center -d command_center -X -q -t -A -c "SHOW log_parameter_max_length_on_error" 2>&1 || true)
  since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  # A row the CHECK on memory_item_versions.change refuses, sent as the app sends it: from the api container, as the
  # app's role, the planted title a bind parameter (a literal in the statement would be logged with the statement).
  docker exec -i -e PROBE=BRAINCHECK-ROW-VALUE "$(cid api)" python - > "$LOGS/db-verbosity.log" 2>&1 <<'PY' || true
import asyncio, os

import asyncpg


async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        await conn.execute("INSERT INTO memory_item_versions (item_id, version, change, kind, title, sensitivity, "
                           "status) VALUES (0, 0, 'brain-check-bogus', 'knowledge', $1, 'general', 'pending_review')",
                           os.environ["PROBE"])
        print("NOT refused")
    except asyncpg.CheckViolationError:
        print("refused by the CHECK")
    finally:
        await conn.close()


asyncio.run(main())
PY
  sleep 1
  logs=$(docker logs --since "$since" "$pg" 2>&1 || true)
  echo "$logs" >> "$LOGS/db-verbosity.log"
  if [[ "$verbosity" = terse && "$params" = 0 && "$logs" == *"violates check constraint"* \
        && "$logs" != *"Failing row contains"* && "$logs" != *"BRAINCHECK-ROW-VALUE"* ]]; then
    pass "command_center logs at log_error_verbosity=terse with bind parameters: a refused row's values never reach the PostgreSQL log"
  else fail "database error logging: verbosity '$verbosity', parameters on error '$params'; see $LOGS/db-verbosity.log"; fi
  check_fp "database error verbosity"
}

step_worker_offline() {  # 7. the worker cannot reach the internet
  local probe="import socket; socket.create_connection(('1.1.1.1', 443), 3)"
  if docker exec "$(cid worker)" python -c "$probe" > "$LOGS/worker-socket.log" 2>&1; then
    fail "the worker reached 1.1.1.1:443"
  else pass "the worker cannot open a connection to 1.1.1.1:443 ($(tail -1 "$LOGS/worker-socket.log" | cut -c1-80))"; fi
  if docker exec "$(cid worker)" python -c "import socket; socket.getaddrinfo('pypi.org', 443)" > /dev/null 2>&1; then
    fail "the worker can resolve public names"
  else pass "the worker cannot resolve public names"; fi
  # Control: the same probe from a container on Docker's ordinary network. If it fails too, this sandbox has no direct
  # route out and only the network check above (internal network) proves the point.
  if docker run --rm --network bridge --entrypoint python infinite-command-center-brain-test/backend:local -c "$probe" > /dev/null 2>&1; then
    pass "control: an ordinary container here does reach 1.1.1.1:443, so the worker's failure is its network"
  else echo "NOTE  control: an ordinary container here cannot reach 1.1.1.1:443 either (sandbox egress is proxied); the internal network check is the proof"; fi
  check_fp "worker offline"
}

step_importer_container() {  # 6.5 at run time, without the importer: mounts, environment, network, rights
  local api_image probe rc
  api_image=$(docker inspect --format '{{.Config.Image}}' "$(cid api)")  # the name: an ID cannot be run (containerd store)
  docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$api_image" | cut -d= -f1 | grep . > "$WORK/image-env.txt"
  mkdir -p "$WORK/imp-reports" && chmod 0777 "$WORK/imp-reports"  # the image's user (uid 10001) writes here on Linux
  mkdir -p "$WORK/imp-src/profile" "$WORK/imp-src/content" && echo "x" > "$WORK/imp-src/profile/SOUL.md" && cp "$FIX_DB" "$WORK/imp-src/state.db"
  local db_sum
  db_sum=$(sha256sum < "$WORK/imp-src/state.db")
  probe=$(cat <<'EOF'
import json, os, socket
def can_create(folder):
    path = os.path.join(folder, ".probe")
    try:
        with open(path, "w") as f:
            f.write("x")
        os.remove(path)
        return True
    except OSError:
        return False
def can_open_for_writing(path):  # never truncates: append mode, nothing written
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_APPEND))
        return True
    except OSError:
        return False
def connect(host, port):
    try:
        socket.create_connection((host, port), 3).close()
        return True
    except OSError:
        return False
status = open("/proc/self/status").read()
print(json.dumps({
    "env": sorted(os.environ), "root_rw": can_create("/app"), "tmp_rw": can_create("/tmp"),
    "reports_rw": can_create("/reports"), "db_rw": can_open_for_writing("/jarvis/state.db"),
    "profile_rw": can_create("/jarvis/profile"), "content_rw": can_create("/jarvis/content"),
    "db_read": open("/jarvis/state.db", "rb").read(15) == b"SQLite format 3",
    "cap_eff": status.split("CapEff:")[1].split()[0], "nnp": status.split("NoNewPrivs:")[1].split()[0],
    "db_reachable": connect("postgres", 5432), "internet": connect("1.1.1.1", 443)}, sort_keys=True))
EOF
)
  BRAIN_IMPORT_IMAGE=$api_image BRAIN_IMPORT_DB="$WORK/imp-src/state.db" BRAIN_IMPORT_PROFILE="$WORK/imp-src/profile" \
    BRAIN_IMPORT_CONTENT="$WORK/imp-src/content" BRAIN_IMPORT_REPORTS="$WORK/imp-reports" docker compose -p "$TEST_PROJECT" \
    -f "$TEST/docker-compose.yml" -f "$TEST/docker-compose.brain-test.yml" -f "$TEST/docker-compose.brain-import.yml" \
    --env-file "$TEST/.env.brain-test" --profile brain-import run --rm --no-deps -T --entrypoint python brain-importer -c "$probe" \
    2> "$LOGS/importer-probe.err" | tail -1 > "$LOGS/importer-probe.json" || true
  local verdict
  verdict=$(python3 - "$LOGS/importer-probe.json" "$WORK/image-env.txt" <<'EOF'
import json, sys
try:
    r = json.load(open(sys.argv[1]))
except ValueError:
    print("no answer from the probe"); sys.exit()
image_env = set(open(sys.argv[2]).read().split()) | {"HOME", "HOSTNAME"}
r["env"] = sorted(set(r["env"]) - image_env)
want = {"env": ["DATABASE_URL", "ENVIRONMENT", "TZ"], "root_rw": False, "tmp_rw": True, "reports_rw": True, "db_rw": False,
        "profile_rw": False, "content_rw": False, "db_read": True, "cap_eff": "0000000000000000", "nnp": "1",
        "db_reachable": True, "internet": False}
print("; ".join("%s=%r" % (k, r.get(k)) for k, v in want.items() if r.get(k) != v))
EOF
)
  if [ -z "$verdict" ] && [ "$(sha256sum < "$WORK/imp-src/state.db")" = "$db_sum" ]; then
    pass "importer container: only DATABASE_URL/TZ/ENVIRONMENT added, read-only system, source read-only, /tmp and /reports writable, no capabilities, no-new-privileges, database reachable, no internet"
  else fail "importer container: $verdict"; tail -5 "$LOGS/importer-probe.err"; fi
  # A missing source path makes Compose stop; nothing is created in its place.
  if BRAIN_IMPORT_IMAGE=$api_image BRAIN_IMPORT_DB="$WORK/imp-src/state.db" BRAIN_IMPORT_PROFILE="$WORK/imp-src/no-such-profile" \
     BRAIN_IMPORT_REPORTS="$WORK/imp-reports" docker compose -p "$TEST_PROJECT" -f "$TEST/docker-compose.yml" \
     -f "$TEST/docker-compose.brain-test.yml" -f "$TEST/docker-compose.brain-import.yml" --env-file "$TEST/.env.brain-test" \
     --profile brain-import run --rm --no-deps -T brain-importer status > "$LOGS/importer-missing.log" 2>&1; then rc=0; else rc=$?; fi
  [ "$rc" != 0 ] && [ ! -e "$WORK/imp-src/no-such-profile" ] && grep -q "does not exist" "$LOGS/importer-missing.log" \
    && pass "a missing bind source makes Compose fail; no folder is created in its place" || fail "missing bind source (exit $rc)"
  [ -z "$(docker ps -aq --filter label=cc.brain-import=1)" ] && pass "no importer container is left behind" || fail "importer containers left"
  check_fp "importer container"
}

step_import_refusals() {  # jarvis-import.ps1 checks that refuse before anything runs
  mkdir -p "$MIG/restored/profile" "$MIG/restored/content" "$MIG/backups"
  cp -r "$FIX_PROFILE/." "$MIG/restored/profile/"; cp -r "$FIX_CONTENT/." "$MIG/restored/content/"
  rm -f "$MIG/restored/profile/.env" "$MIG/restored/profile/auth.json" "$MIG/restored/profile/state.db"*
  cp "$FIX_DB" "$MIG/backups/jarvis-state.db"
  local before n=0
  before=$(find "$WORK/inst" -printf '%p %s %T@\n' | LC_ALL=C sort | sha256sum)
  refuse() {  # name, expected text, then parameters (a reports folder that must never get created is added)
    local name=$1 text=$2; shift 2
    local reports=(-Reports "$MIG/import-reports")
    [[ " $* " == *" -Reports "* ]] && reports=()
    run_step "refuse-$name" "" "$TEST" "$JI" "$@" "${reports[@]}"
    if [[ "$CODE" = 2 && "$OUT" == *"NOT RUN"* && "$OUT" == *"$text"* ]]; then n=$((n + 1)); else fail "jarvis-import refusal $name"; show "refuse-$name"; fi
  }
  refuse no-target "-Target" -Mode Plan -All
  refuse no-selection "exactly one" -Target Test -Mode Plan -BackupDb "$MIG/backups/jarvis-state.db" -Restored "$MIG/restored"
  refuse two-selections "exactly one" -Target Test -Mode Plan -All -Sample 2 -BackupDb "$MIG/backups/jarvis-state.db" -Restored "$MIG/restored"
  refuse no-run "-Run" -Target Test -Mode Verify -BackupDb "$MIG/backups/jarvis-state.db" -Restored "$MIG/restored"
  refuse missing-db "not found" -Target Test -Mode Plan -All -BackupDb "$MIG/backups/nope.db" -Restored "$MIG/restored"
  refuse missing-restored "not found" -Target Test -Mode Plan -All -BackupDb "$MIG/backups/jarvis-state.db" -Restored "$MIG/nope"
  cp "$FIX_DB" "$MIG/backups/wal.db"; echo "pending" > "$MIG/backups/wal.db-wal"
  refuse wal "-wal is not empty" -Target Test -Mode Plan -All -BackupDb "$MIG/backups/wal.db" -Restored "$MIG/restored"
  cp "$FIX_DB" "$MIG/backups/journal.db"; echo "pending" > "$MIG/backups/journal.db-journal"
  refuse journal "-journal is not empty" -Target Test -Mode Plan -All -BackupDb "$MIG/backups/journal.db" -Restored "$MIG/restored"
  # JARVIS's own folders (LOCALAPPDATA\hermes): the fixture's live database and profile.
  PS_ENV=(-e "LOCALAPPDATA=$WORK/inst")
  refuse inside-hermes "inside JARVIS's own folders" -Target Test -Mode Plan -All -BackupDb "$FIX_PROFILE/state.db" -Restored "$MIG/restored"
  refuse reports-in-hermes "inside JARVIS's folders" -Target Test -Mode Status -Reports "$WORK/inst/hermes/reports"
  PS_ENV=()
  # Not enough free space: a sparse "database" of 8 TB (nothing is read from it).
  truncate -s 8T "$MIG/backups/huge.db"
  refuse free-space "three times the database" -Target Test -Mode Plan -All -BackupDb "$MIG/backups/huge.db" -Restored "$MIG/restored"
  rm -f "$MIG/backups/huge.db"
  # The live installation is only a target from its own folder.
  refuse production-elsewhere "runs from the live installation's folder" -Target Production -Mode Status
  [ "$n" = 12 ] && pass "jarvis-import.ps1 refused 12 bad requests before anything ran (exit 2)"
  rm -f "$MIG/backups/wal.db"* "$MIG/backups/journal.db"*
  [ "$(find "$WORK/inst" -printf '%p %s %T@\n' | LC_ALL=C sort | sha256sum)" = "$before" ] \
    && [ -z "$(docker ps -aq --filter label=cc.brain-import=1)" ] && [ ! -e "$MIG/import-reports" ] && [ ! -e "$WORK/inst/hermes/reports" ] \
    && pass "the refusals changed nothing in the JARVIS fixture and created nothing (no reports folder, no container)" \
    || fail "a refusal left something behind"
  check_fp "jarvis-import refusals"
}

step_import_wiring() {  # -Target Test end to end through the script, with the importer's own answer
  # Without the importer CLI in the image, brain-import answers "unknown command" (exit 2): the script's checks, the
  # importer container and the fingerprint bracket still run, which is what this step checks now.
  run_step import-status "" "$TEST" "$JI" -Target Test -Mode Status -Reports "$MIG/import-reports"
  if [[ "$OUT" == *"Live installation fingerprint unchanged"* && "$OUT" == *"brain-import status"* ]]; then
    pass "jarvis-import.ps1 -Target Test -Mode Status ran the importer container in $TEST_PROJECT (importer exit $CODE)"
  else fail "jarvis-import Status wiring (exit $CODE)"; show import-status; fi
  # The importer CLI (brain-import, spec 2.1) and the brain API (app/api/brain.py) are in the image (after the merge).
  if docker run --rm --network none --entrypoint python "$(docker inspect --format '{{.Image}}' "$(cid api)")" -c \
       "import importlib.util as u, inspect, sys, app.cli as c; sys.exit(0 if (u.find_spec('app.brain.cli') or 'brain-import' in inspect.getsource(c)) and u.find_spec('app.api.brain') else 1)" \
       > /dev/null 2>&1; then
    HAVE_IMPORTER=1
  else HAVE_IMPORTER=0; fi
  check_fp "jarvis-import wiring"
}

step_pre_import_dump() {  # the dump jarvis-import.ps1 takes before a live import, here from the test copy's database
  local pg out sha
  pg=$(cid postgres)
  out=$(docker run --rm -v /usr/bin/docker:/usr/bin/docker:ro -v /var/run/docker.sock:/var/run/docker.sock -v "$WORK:$WORK" \
    -v "$TEST:$TEST:ro" "$PWSH_IMAGE" pwsh -NoProfile -Command \
    ". '$TEST/scripts/brain-common.ps1'; \$d = New-DatabaseDump '$pg' '$MIG/pre-import' 'cc-pre-import-check.dump'; 'sha=' + \$d.Sha256; 'tables=' + \$d.Tables" 2>&1 || true)
  echo "$out" > "$LOGS/pre-import-dump.log"
  sha=$(sha256sum "$MIG/pre-import/cc-pre-import-check.dump" 2>/dev/null | cut -d' ' -f1)
  if [[ -n "$sha" && "$out" == *"sha=$sha"* && "$out" =~ tables=[1-9][0-9]* ]] \
     && ! docker exec "$pg" test -e /tmp/cc-pre-import-check.dump \
     && docker exec -i "$pg" pg_restore -l < "$MIG/pre-import/cc-pre-import-check.dump" | grep -c ' TABLE DATA public schema_migrations ' > /dev/null; then
    pass "pre-import dump (New-DatabaseDump): command_center only, pg_restore -l lists it, SHA-256 matches, no copy left in the container"
  else fail "pre-import dump: $out"; fi
  out=$(docker run --rm -v /usr/bin/docker:/usr/bin/docker:ro -v /var/run/docker.sock:/var/run/docker.sock -v "$WORK:$WORK" \
    -v "$TEST:$TEST:ro" "$PWSH_IMAGE" pwsh -NoProfile -Command \
    ". '$TEST/scripts/brain-common.ps1'; try { \$null = New-DatabaseDump 'no-such-container' '$MIG/pre-import' 'bad.dump'; 'no error' } catch { 'threw: ' + \$_.Exception.Message }" 2>&1 || true)
  [[ "$out" == *"threw: pg_dump failed"* && ! -e "$MIG/pre-import/bad.dump" ]] && pass "a failed dump stops before any import (no file)" || fail "failed dump: $out"
  check_fp "pre-import dump"
}

step_persistence() {  # 8. restart of api, worker and postgres, and Stop then Start: the data stays
  MARK="brain-check-$(date +%s)-$RANDOM"
  tpsql -c "INSERT INTO audit_log (actor, action, target_type, target_id) VALUES ('brain-check', 'brain_check.marker', 'test', '$MARK')" || true
  compose_test restart postgres api worker > "$LOGS/restart.log" 2>&1 || true
  wait_health || true
  local got
  got=$(tpsql -c "SELECT target_id FROM audit_log WHERE action = 'brain_check.marker'" || true)
  [ "$got" = "$MARK" ] && pass "a row inserted with psql survives a restart of postgres, api and worker" || fail "after restart: '$got'"
  local envsum
  envsum=$(sha256sum < "$TEST/.env.brain-test")
  run_step stop "" "$TEST" "$BT" -Action Stop
  [[ "$CODE" = 0 && -z "$(docker ps -q --filter "label=com.docker.compose.project=$TEST_PROJECT")" ]] \
    && pass "brain-test.ps1 -Action Stop stops the test copy" || { fail "Stop"; show stop; }
  run_step restart "" "$TEST" "$BT" -Action Start
  got=$(tpsql -c "SELECT target_id FROM audit_log WHERE action = 'brain_check.marker'" 2>/dev/null || true)
  [[ "$CODE" = 0 && "$got" = "$MARK" && "$(sha256sum < "$TEST/.env.brain-test")" = "$envsum" ]] \
    && pass "Stop then Start keeps the data and the same .env.brain-test" || { fail "Stop/Start persistence"; show restart; }
  check_fp "restart persistence"
}

step_status_env_override() {  # COMPOSE_* in the owner's environment cannot point the scripts at the live project
  PS_ENV=(-e COMPOSE_PROJECT_NAME=$LIVE_PROJECT -e "COMPOSE_FILE=$LIVE/docker-compose.yml")
  run_step status-env "" "$TEST" "$BT" -Action Status
  PS_ENV=()
  # The compose ps table (lines starting with a container name) lists the test copy's containers only.
  [[ "$CODE" = 0 ]] && grep -q '^cc-brain-test-api-1 ' <<<"$OUT" && ! grep -q '^infinite-command-center-' <<<"$OUT" \
    && pass "with COMPOSE_PROJECT_NAME/COMPOSE_FILE pointing at the live installation, Status still shows only cc-brain-test" \
    || { fail "Status with COMPOSE_* set"; show status-env; }
  PS_ENV=(-e COMPOSE_PROFILES=never)
  run_step profiles-env "" "$TEST" "$BT" -Action Status
  PS_ENV=()
  [[ "$CODE" = 2 && "$OUT" == *"n8n is part of it"* ]] && pass "COMPOSE_PROFILES that would bring n8n in is refused" || { fail "COMPOSE_PROFILES"; show profiles-env; }
  # A tampered overlay (worker back on the internet) is refused before anything runs.
  local bad="$WORK/tampered/InfiniteAI-CommandCenter"; mkdir -p "$bad"; cp -r "$TEST/." "$bad/"
  sed -i 's/networks: !override \[internal\]/networks: [internal]/' "$bad/docker-compose.brain-test.yml"
  run_step tampered "" "$bad" "$bad/scripts/brain-test.ps1" -Action Status
  [[ "$CODE" = 2 && "$OUT" == *"worker's networks"* ]] && pass "an overlay that would give the worker internet is refused" || { fail "tampered overlay"; show tampered; }
  check_fp "COMPOSE_* environment"
}

step_seed() {  # -SeedFromBackup with a backup of the test copy itself: everything that could act is switched off
  # Things that would act in a copy of a live installation; the worker must not get to them first (run_after and
  # forward_next_at lie in the future).
  tpsql -c "INSERT INTO schedules (name, agent, capability, cron, enabled) VALUES ('brain-check-on', 'monitoring', 'check_all_sites', '0 3 * * *', true);
            INSERT INTO tasks (agent, capability, risk, status, run_after) VALUES ('monitoring', 'check_all_sites', 'read', 'queued', now() + interval '1 day'),
              ('monitoring', 'check_all_sites', 'external', 'awaiting_approval', now() + interval '1 day');
            INSERT INTO approvals (task_id, agent, capability, risk, summary) SELECT max(id), 'monitoring', 'check_all_sites', 'external', 'brain-check' FROM tasks;
            INSERT INTO secret_values (name, ciphertext) VALUES ('brain_check_secret', 'not-a-real-ciphertext');
            INSERT INTO events (kind, source, title, forward_next_at) VALUES ('brain_check', 'brain-check', 'pending event', now() + interval '1 day');" > /dev/null || true
  login > /dev/null  # leaves a session row
  mkdir -p "$WORK/seed-backup"
  docker exec "$(cid postgres)" pg_dump -U postgres -Fc -f /tmp/seed.dump command_center || true
  docker cp "$(cid postgres):/tmp/seed.dump" "$WORK/seed-backup/command_center.dump" > /dev/null || true
  docker exec "$(cid postgres)" rm -f /tmp/seed.dump || true
  run_step seed-cancel "no" "$TEST" "$BT" -Action Start -SeedFromBackup "$WORK/seed-backup"
  [[ "$CODE" = 2 && "$OUT" == *"REPLACE TEST DATA"* && -n "$(cid api)" ]] && pass "seeding over existing test data needs the typed confirmation" \
    || { fail "seed confirmation"; show seed-cancel; }
  run_step seed "REPLACE TEST DATA" "$TEST" "$BT" -Action Start -SeedFromBackup "$WORK/seed-backup"
  local counts
  counts=$(tpsql -c "SELECT (SELECT count(*) FROM audit_log WHERE action = 'brain_check.marker'), (SELECT count(*) FROM schedules WHERE enabled),
    (SELECT count(*) FROM schedules), (SELECT count(*) FROM tasks WHERE status IN ('queued', 'running', 'awaiting_approval')),
    (SELECT count(*) FROM approvals WHERE status = 'pending'), (SELECT count(*) FROM approvals WHERE decision_note = 'test copy'),
    (SELECT count(*) FROM secret_values), (SELECT count(*) FROM events WHERE forward_status = 'pending')" 2>/dev/null || true)
  if [[ "$CODE" = 0 && "$counts" =~ ^1\|0\|[1-9][0-9]*\|0\|0\|1\|0\|0$ && "$OUT" == *"Seeded:"* ]]; then
    pass "Start -SeedFromBackup: data restored, schedules disabled, open tasks cancelled, approvals rejected ('test copy'), secrets deleted, events skipped"
  else fail "seed (exit $CODE, counts $counts)"; show seed; fi
  # The script's own sign-in check after the seed adds a session (PowerShell); the one made with curl before must be gone.
  local copied_sessions
  copied_sessions=$(tpsql -c "SELECT count(*) FROM sessions WHERE user_agent LIKE 'curl/%'" 2>/dev/null || echo "?")
  [ "$copied_sessions" = 0 ] && pass "sessions copied with the backup were deleted" || fail "sessions after the seed: $copied_sessions"
  check_fp "seed"
}

step_remove() {  # 10. Remove: typed confirmation, then nothing of cc-brain-test is left
  run_step remove-cancel "remove test copy" "$TEST" "$BT" -Action Remove
  [[ "$CODE" = 2 && -n "$(cid api)" ]] && pass "Remove without the exact words REMOVE TEST COPY changes nothing" || { fail "Remove confirmation"; show remove-cancel; }
  run_step remove "REMOVE TEST COPY" "$TEST" "$BT" -Action Remove
  [[ "$CODE" = 0 && "$(test_left)" == "0 0 0 0" && "$OUT" == *"Live installation fingerprint unchanged"* ]] \
    && pass "brain-test.ps1 -Action Remove: no container, volume, network or image of cc-brain-test is left" \
    || { fail "Remove (exit $CODE, left: $(test_left))"; show remove; }
  check_fp "remove"
}

step_precheck_again() {  # 11. the same report after the test: same fingerprint and counts
  sleep 1
  run_step precheck-2 "" "$TEST" "$PC" -OutDir "$MIG"
  local report2
  report2=$(ls "$MIG"/precheck-*.txt | LC_ALL=C sort | tail -1)
  if [ "$CODE" = 0 ] && [ "$report2" != "$REPORT1" ] && diff <(sed 1d "$REPORT1" | grep -v '^Report written') <(sed 1d "$report2" | grep -v '^Report written') > "$LOGS/precheck-diff.txt"; then
    pass "brain-precheck.ps1 after the test: the same fingerprint, health and counts as before"
  else fail "precheck before/after differ"; cat "$LOGS/precheck-diff.txt"; fi
  check_fp "precheck again"
}

# ================================================================================================ steps (after the merge)
# These need "python -m app.cli brain-import" (spec 2.1) and /api/brain (spec 3) in the test copy's image.

restored_from_kit() {  # the owner's inputs, made with jarvis-kit.ps1 from the test image: Backup, VerifyBackup -ExtractTo, Discover
  local kit="$TEST/migration/jarvis/jarvis-kit.ps1" image=infinite-command-center-brain-test/backend:local
  run_step kit-backup "correct horse battery 42" "$TEST" "$kit" -Action Backup -ProfileDir "$FIX_PROFILE" -ContentDir "$FIX_CONTENT" -OutRoot "$MIG/kit" -Image "$image"
  local archive
  archive=$(find "$MIG/kit/backups" -name '*.ccjb' | head -1)
  rm -rf "$MIG/restored-kit"
  run_step kit-restore "correct horse battery 42" "$TEST" "$kit" -Action VerifyBackup -Archive "$archive" -ExtractTo "$MIG/restored-kit" -Image "$image"
  [[ "$CODE" = 0 && -d "$MIG/restored-kit/profile" ]] && pass "restored folder made with jarvis-kit.ps1 (Backup, VerifyBackup -ExtractTo)" || { fail "kit restore"; show kit-restore; }
  run_step kit-discover "" "$TEST" "$kit" -Action Discover -ProfileDir "$FIX_PROFILE" -ContentDir "$FIX_CONTENT" -BackupDb "$MIG/backups/jarvis-state.db" -OutRoot "$MIG/kit" -Image "$image"
  DISCOVERY=$(find "$MIG/kit" -name discovery-summary.json | head -1)
  [[ "$CODE" = 0 && -n "$DISCOVERY" ]] && pass "discovery-summary.json made with jarvis-kit.ps1 -Action Discover" || { fail "kit discover"; show kit-discover; }
  check_fp "kit inputs"
}

ji() { run_step "$1" "${2:-}" "$TEST" "$JI" -Target Test "${@:3}" -BackupDb "$MIG/backups/jarvis-state.db" -Restored "$MIG/restored-kit" -Reports "$MIG/import-reports"; }
latest_run() { docker exec "$(cid postgres)" psql -U postgres -d command_center -tAc "SELECT max(id) FROM import_runs" || true; }

step_import_staged() {  # 5. Plan, Import with sample 2, Verify, Import with all, Verify
  mkdir -p "$MIG/import-reports" && chmod 0777 "$MIG/import-reports"  # written by the image's user (uid 10001) on Linux
  chmod -R a+rX "$MIG/restored-kit" "$MIG/backups"  # read by the image's user on Linux (Docker Desktop maps this itself)
  ji import-plan "" -Mode Plan -All -Discovery "$DISCOVERY"
  [[ "$CODE" = 0 && "$(tpsql -c 'SELECT count(*) FROM import_runs')" = 0 ]] && pass "Plan (writes nothing to the database)" || { fail "Plan"; show import-plan; }
  ji import-sample "" -Mode Import -Sample 2 -Discovery "$DISCOVERY"
  RUN1=$(latest_run)
  [[ "$CODE" = 0 && -n "$RUN1" ]] && pass "Import -Sample 2 (run $RUN1)" || { fail "Import sample"; show import-sample; }
  ji verify-sample "" -Mode Verify -Run "$RUN1"
  [[ "$CODE" = 0 ]] && pass "Verify run $RUN1" || { fail "Verify sample"; show verify-sample; }
  ji import-all "" -Mode Import -All
  RUN2=$(latest_run)
  [[ "$CODE" = 0 && "$RUN2" != "$RUN1" ]] && pass "Import -All (run $RUN2)" || { fail "Import all"; show import-all; }
  ji verify-all "" -Mode Verify -Run "$RUN2"
  [[ "$CODE" = 0 ]] && pass "Verify run $RUN2" || { fail "Verify all"; show verify-all; }
  check_fp "staged import"
}

step_api_brain() {  # 6. sign-in with the test cookie; overview; search; sessions; a recall through the Supervisor
  [ "$(login)" = 200 ] || { fail "sign-in for the brain API"; return; }
  local overview
  overview=$(api_get /api/brain/overview)
  jq_py '"ok" if d["instance_label"] == "cc-brain-test" and d["archive"]["sessions"] > 0 and d["memory"]["total"] > 0 else d' <<<"$overview" | grep -qx ok \
    && pass "/api/brain/overview: label, sessions and memory items" || fail "overview: $(cut -c1-200 <<<"$overview")"
  api_get "/api/brain/search?q=widget&scope=all" | jq_py '"ok" if "memory" in d and "archive" in d else d' | grep -qx ok \
    && pass "/api/brain/search answers" || fail "search"
  api_get "/api/brain/sessions?limit=5" | jq_py '"ok" if d["total"] > 0 else d' | grep -qx ok && pass "/api/brain/sessions lists sessions" || fail "sessions"
  local req id status
  req=$(api_post /api/supervisor/requests '{"message": "What did JARVIS do about the website backups?"}')
  id=$(jq_py 'd["request"]["id"]' <<<"$req")
  for _ in $(seq 1 60); do
    status=$(api_get "/api/supervisor/requests/$id" | jq_py 'd["request"]["status"]')
    [[ "$status" = completed || "$status" = failed ]] && break
    sleep 2
  done
  [ "$status" = completed ] && pass "a recall through the Supervisor completed (worker answered)" || fail "supervisor recall: $status"
  check_fp "brain API"
}

step_restart_with_imports() {  # 8 (with imported data): restart, and the imported rows are still there
  local before after
  before=$(tpsql -c "SELECT (SELECT count(*) FROM memory_items), (SELECT count(*) FROM archive_messages)" || true)
  compose_test restart postgres api worker > /dev/null 2>&1 || true; wait_health || true
  after=$(tpsql -c "SELECT (SELECT count(*) FROM memory_items), (SELECT count(*) FROM archive_messages)" || true)
  [ "$before" = "$after" ] && pass "imported rows persist across a restart ($after)" || fail "imported rows $before -> $after"
}

step_no_planted_values() {  # 11. no planted secret or restricted value in the brain tables (pg_dump, grep)
  docker exec "$(cid postgres)" pg_dump -U postgres --data-only -t 'import_*' -t 'memory_*' -t 'archive_*' -t workflow_drafts \
    -t brain_tombstones command_center > "$WORK/brain-dump.sql" || true
  local found
  found=$(python3 - "$CC/migration/jarvis/tests" "$WORK/brain-dump.sql" <<'EOF'
import sys
sys.path.insert(0, sys.argv[1])
from make_fixture import DETAIL_ONLY, PLANTED
allowed = set(DETAIL_ONLY) | {"title", "message", "memory_text", "customer_name", "bad_utf8"}  # ordinary content, not secrets
data = open(sys.argv[2], encoding="utf-8", errors="replace").read()
print(" ".join(k for k, v in PLANTED.items() if k not in allowed and v in data))
EOF
)
  [ -s "$WORK/brain-dump.sql" ] && [ -z "$found" ] && pass "no planted secret or restricted value in the brain tables" || fail "planted values stored: $found"
}

step_ui_brain() {  # BRAIN_UI=1: the Brain page in a browser (testing/brain-ui-check.py), per role; approves and edits an item
  python3 - "$CC/migration/jarvis/tests" > "$WORK/planted.json" <<'EOF'
import json, sys
sys.path.insert(0, sys.argv[1])
from make_fixture import DETAIL_ONLY, PLANTED
allowed = set(DETAIL_ONLY) | {"title", "message", "memory_text", "customer_name", "bad_utf8"}  # ordinary content
print(json.dumps({k: v for k, v in PLANTED.items() if k not in allowed}))
EOF
  local code=0
  "${BRAIN_UI_PYTHON:-python3}" "$CC/testing/brain-ui-check.py" "http://127.0.0.1:$PORT" "$(envval ADMIN_EMAIL)" \
    "$(envval ADMIN_PASSWORD)" "$WORK/planted.json" > "$WORK/brain-ui.log" 2>&1 || code=$?
  sed -n 's/^\(PASS\|FAIL\)  /\1  Brain page: /p' "$WORK/brain-ui.log"
  grep -q '^FAIL' "$WORK/brain-ui.log" && FAILED=1
  [ "$code" = 0 ] || { tail -5 "$WORK/brain-ui.log"; fail "brain-ui-check.py exit $code"; }
  check_fp "Brain page in a browser"
}

step_rollback() {  # 9. Rollback (newest first), then zero rows
  local discard=()
  [ "${BRAIN_UI:-0}" = 1 ] && discard=(-DiscardReviewed)  # the browser check approved and edited an imported item
  ji rollback-all "" -Mode Rollback -Run "$RUN2" "${discard[@]}"
  local c2=$CODE
  ji rollback-sample "" -Mode Rollback -Run "$RUN1" "${discard[@]}"
  local rows
  rows=$(tpsql -c "SELECT (SELECT count(*) FROM memory_items) + (SELECT count(*) FROM memory_item_versions) + (SELECT count(*) FROM archive_sessions)
    + (SELECT count(*) FROM archive_messages) + (SELECT count(*) FROM memory_conflicts) + (SELECT count(*) FROM workflow_drafts)" || true)
  [[ "$c2" = 0 && "$CODE" = 0 && "$rows" = 0 ]] && pass "Rollback of both runs leaves zero brain rows" || fail "rollback (exit $c2/$CODE, rows $rows)"
  check_fp "rollback"
}

# ================================================================================================ run
step_parse
step_precheck
step_refusals_before_start
step_start || { echo "The test copy did not start; the remaining steps need it."; exit 1; }
step_config_assertions
step_api_basics
step_db_error_verbosity
step_worker_offline
step_importer_container
step_import_refusals
step_import_wiring
step_pre_import_dump
restored_from_kit  # the importer's inputs as the owner makes them (no importer needed yet)
step_persistence
step_status_env_override
if [ "${HAVE_IMPORTER:-0}" = 1 ]; then
  step_import_staged
  step_api_brain
  step_restart_with_imports
  step_no_planted_values
  [ "${BRAIN_UI:-0}" = 1 ] && step_ui_brain
  step_rollback
else
  for s in "5. jarvis-import -Target Test: Plan, Import -Sample 2, Verify, Import -All, Verify" \
           "6. brain API: overview, search, sessions, Supervisor recall" "8. imported rows persist across a restart" \
           "9. Rollback, then zero rows" "11. no planted value in the brain tables"; do skip "$s"; done
fi
step_seed
step_remove
step_precheck_again
[ "$(fingerprint)" = "$FP0" ] && pass "live installation (and cc-test-pg, decoy-*) unchanged from start to end" || fail "LIVE INSTALLATION CHANGED"
exit $FAILED
