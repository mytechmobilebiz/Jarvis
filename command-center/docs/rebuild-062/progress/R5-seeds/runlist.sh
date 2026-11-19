#!/usr/bin/env bash
# runlist.sh <label> <live_history> <test: oracle|recorded> <seeds...>
L=$1; H=$2; T=$3; shift 3
OUT=/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/r5/final; mkdir -p $OUT
eval "$(bash /home/user/Jarvis/.worktrees/brain-062C/command-center/docs/rebuild-062/env.sh)"
for d in $L ${L}_cr; do docker exec cc-test-pg psql -U cc -d cc_test -c "drop database if exists $d" >/dev/null 2>&1; docker exec cc-test-pg psql -U cc -d cc_test -c "create database $d" >/dev/null; done
cd /home/user/Jarvis/.worktrees/brain-062C/command-center/backend
K=""; for s in "$@"; do K="$K or [$s]"; done; K=${K# or }
if [ $T = oracle ]; then SEL="test_oracle["; EXTRA="ORACLE_SEED_BASE=$1 ORACLE_SEEDS=$(( ${@: -1} - $1 + 1 ))"; else SEL="test_oracle_recorded"; EXTRA="ORACLE_SEEDS=0"; fi
KEXPR=""; for s in "$@"; do KEXPR="$KEXPR or ${SEL%[}[$s]"; done
env TEST_DATABASE_URL=postgresql+asyncpg://cc:cc@127.0.0.1:55432/$L TEST_CREDIT_REPAIR_DATABASE_URL=postgresql+asyncpg://cc:cc@127.0.0.1:55432/${L}_cr \
 REQUIRE_HERMES_SRC=1 ORACLE_RV3_SEEDS=0 ORACLE_LIVE_HISTORY=$H ORACLE_SECOND_WITHHOLD=1 $EXTRA ORACLE_OUT=$OUT/$L.jsonl \
 timeout 20000 $PY -m pytest -p no:randomly -p no:cacheprovider -q --no-header -rf tests/property/test_oracle_properties.py -k "${KEXPR# or }" > $OUT/$L.txt 2>&1
tail -1 $OUT/$L.txt
for d in $L ${L}_cr; do docker exec cc-test-pg psql -U cc -d cc_test -c "drop database if exists $d" >/dev/null 2>&1; done
