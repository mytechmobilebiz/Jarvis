#!/usr/bin/env bash
# End-to-end check of migration/jarvis/jarvis-kit.ps1 (development only; testing/ is not in the package): the
# PowerShell wrapper in PowerShell 7, the kit container from the Command Center's backend image, and a synthetic
# JARVIS installation (migration/jarvis/tests/make_fixture.py) with planted secrets and personal data.
#
#   testing/jarvis-kit-check.sh            (needs Docker and infinite-command-center/backend:local)
set -euo pipefail
KIT=$(cd "$(dirname "$0")/../migration/jarvis" && pwd)
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
PWSH_IMAGE=${PWSH_IMAGE:-mcr.microsoft.com/powershell:7.4-ubuntu-22.04}
PASS="correct horse battery 42"
FAILED=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILED=1; }

python3 "$KIT/tests/make_fixture.py" "$WORK/inst" > /dev/null
PROFILE=$WORK/inst/hermes/profiles/jarvis; CONTENT=$WORK/inst/hermes/jarvis-content
DB=$WORK/inst/backups/jarvis-state-test.db
state() { (cd "$WORK/inst" && find . -printf '%p %s %T@\n' | sort | sha256sum); }
BEFORE=$(state)

kit() {  # passphrase ("" for none), then the script's parameters; prints the output and "exit=<code>"
  local phrase=$1; shift
  # Read-Host cannot read a secure string from redirected input: a function of the same name answers instead.
  local stub="function Read-Host { param([switch]\$AsSecureString, [Parameter(ValueFromRemainingArguments)]\$Rest) ConvertTo-SecureString '$phrase' -AsPlainText -Force }"
  local quoted=""
  for a in "$@"; do  # parameter names as they are, values quoted
    if [[ "$a" =~ ^-[A-Za-z]+$ ]]; then quoted+=" $a"; else quoted+=" '${a//\'/\'\'}'"; fi
  done
  docker run --rm -v /usr/bin/docker:/usr/bin/docker:ro -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$WORK:$WORK" -v "$KIT:$KIT:ro" -w "$WORK" "$PWSH_IMAGE" pwsh -NoProfile -Command \
    "$stub; & '$KIT/jarvis-kit.ps1'$quoted; \"exit=\$LASTEXITCODE\"" 2>&1
}

out=$(kit "$PASS" -Action Backup -ProfileDir "$PROFILE" -ContentDir "$CONTENT" -OutRoot "$WORK/out")
archive=$(find "$WORK/out/backups" -name '*.ccjb' 2>/dev/null | head -1 || true)
[[ "$out" == *"exit=0"* && -n "$archive" && -f "$archive.sha256" ]] && pass "backup written and verified" \
  || fail "backup: $out"

out=$(kit "$PASS" -Action VerifyBackup -Archive "$archive" -ExtractTo "$WORK/restored" -ProfileDir "$PROFILE" \
      -ContentDir "$CONTENT")
leaks=$(python3 - "$KIT/tests" "$WORK/restored" <<'EOF'
import os, sys
sys.path.insert(0, sys.argv[1])
from make_fixture import NOT_IN_BACKUP, PLANTED
data = b"".join(open(os.path.join(d, f), "rb").read() for d, _, fs in os.walk(sys.argv[2]) for f in fs)
print(" ".join(k for k in NOT_IN_BACKUP if PLANTED[k].encode() in data or PLANTED[k].encode("utf-16-le") in data))
EOF
)
[[ "$out" == *"exit=0"* && -f "$WORK/restored/profile/SOUL.md" && ! -e "$WORK/restored/profile/.env" \
   && ! -e "$WORK/restored/profile/auth.json" && ! -e "$WORK/restored/profile/state.db" \
   && ! -e "$WORK/restored/profile/platforms/whatsapp/session/creds.json" && -z "$leaks" ]] \
  && pass "backup restores without secret files or secret values" || fail "verify and extract: $out $leaks"

out=$(kit "$PASS" -Action VerifyBackup -Archive "$archive" -ExtractTo "$PROFILE/restored" -ProfileDir "$PROFILE" \
      -ContentDir "$CONTENT")
[[ "$out" == *"NOT RUN"* && "$out" == *"exit=2"* && ! -e "$PROFILE/restored" ]] \
  && pass "a restore into the JARVIS folders is refused before anything is created" || fail "extract inside: $out"

python3 - "$archive" "$WORK/damaged.ccjb" <<'EOF'
import sys
raw = bytearray(open(sys.argv[1], "rb").read()); raw[len(raw) * 3 // 4] ^= 1; open(sys.argv[2], "wb").write(raw)
EOF
out=$(kit "$PASS" -Action VerifyBackup -Archive "$WORK/damaged.ccjb" -ExtractTo "$WORK/restored-damaged")
[[ "$out" != *"exit=0"* && ! -e "$WORK/restored-damaged" ]] \
  && pass "a damaged archive restores nothing and leaves no folder behind" || fail "damaged restore: $out"

out=$(kit "a wrong passphrase!" -Action VerifyBackup -Archive "$archive")
[[ "$out" == *"wrong passphrase"* && "$out" != *"exit=0"* ]] && pass "a wrong passphrase is refused" \
  || fail "wrong passphrase: $out"

# A relative output folder, as typed in PowerShell, is resolved from the current folder.
out=$(kit "" -Action Discover -ProfileDir "$PROFILE" -ContentDir "$CONTENT" -BackupDb "$DB" -OutRoot "rel-out" \
      -Full -Check -ListOtherProfiles)
report=$(find "$WORK/rel-out" -maxdepth 1 -name 'discovery-*' -type d 2>/dev/null | head -1 || true)
if [[ "$out" == *"exit=0"* && -f "$report/discovery-summary.md" && -f "$report/discovery-detail.json" \
      && "$(cat "$report/other-profiles.txt")" == "jarvis: state.db"* ]]; then
  pass "discovery reports written"
else
  fail "discover: $out"
fi
leaks=$(python3 - "$KIT/tests" "$report" <<'EOF'
import os, sys
sys.path.insert(0, sys.argv[1])
import json
from make_fixture import DETAIL_ONLY, PLANTED
found = []
for name in ("discovery-summary.md", "discovery-summary.json", "discovery-detail.json"):
    text = open(os.path.join(sys.argv[2], name), encoding="utf-8").read()
    if name == "discovery-detail.json":  # the owner's own report may name credential-file keys and own file names
        detail = json.loads(text)
        for f in detail["files"]:
            f.pop("keys", None)
        text = json.dumps(detail)
    for key, value in PLANTED.items():
        if value in text and not (name == "discovery-detail.json" and key in DETAIL_ONLY):
            found.append("%s in %s" % (key, name))
print("; ".join(found))
EOF
)
[ -z "$leaks" ] && pass "no planted secret, personal data, content or reasoning in the reports" || fail "leaked: $leaks"
grep -q "quick_check: ok" "$report/discovery-summary.md" && pass "database analysed read-only (quick_check ok)" \
  || fail "no quick_check in the summary"

# A copy of the profile outside Hermes's folders (as restored for the import) can be inventoried, with the output
# next to it: only a profile under <hermes>/profiles makes its Hermes folder protected.
mkdir -p "$WORK/copy" && cp -r "$PROFILE" "$WORK/copy/profile" && cp -r "$CONTENT" "$WORK/copy/content"
out=$(kit "" -Action Discover -ProfileDir "$WORK/copy/profile" -ContentDir "$WORK/copy/content" -BackupDb "$DB" \
      -OutRoot "$WORK/copy-out")
[[ "$out" == *"exit=0"* ]] && pass "a profile copy outside Hermes's folders can be inventoried" || fail "copy: $out"

for case in inside-hermes live-db no-image; do
  before=$(find "$WORK" -maxdepth 2 | sort | sha256sum)
  case $case in
    inside-hermes) out=$(kit "" -Action Discover -ProfileDir "$PROFILE" -ContentDir "$CONTENT" -BackupDb "$DB" \
                         -OutRoot "$WORK/inst/hermes/reports") ;;
    live-db) out=$(kit "" -Action Discover -ProfileDir "$PROFILE" -ContentDir "$CONTENT" -BackupDb "$PROFILE/state.db" \
                   -OutRoot "$WORK/out2") ;;
    no-image) out=$(kit "" -Action Discover -ProfileDir "$PROFILE" -ContentDir "$CONTENT" -BackupDb "$DB" \
                    -OutRoot "$WORK/out3" -Image "no-such-image:0") ;;
  esac
  [[ "$out" == *"NOT RUN"* && "$out" == *"exit=2"* && "$(find "$WORK" -maxdepth 2 | sort | sha256sum)" == "$before" ]] \
    && pass "refused before anything ran: $case" || fail "$case: $out"
done

[ "$(state)" = "$BEFORE" ] && pass "the JARVIS folders and the database backup are unchanged" \
  || fail "the installation changed"
exit $FAILED
