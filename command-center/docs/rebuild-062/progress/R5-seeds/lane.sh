#!/usr/bin/env bash
# One lane of fresh oracle seeds (rebuild R5): lane.sh <label> <live_history 0|1> <base> <count> [second_withhold, default 1]
# Writes $OUT/<label>.jsonl (one line per seed) and $OUT/<label>.txt (pytest's output). Own database <label>, dropped after.
set -u
L=$1; H=$2; B=$3; N=$4; SW=${5:-1}
OUT=${OUT:-/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/r5/seeds}
mkdir -p $OUT
eval "$(bash /home/user/Jarvis/.worktrees/brain-062C/command-center/docs/rebuild-062/env.sh)"
for d in $L ${L}_cr; do docker exec cc-test-pg psql -U cc -d cc_test -c "drop database if exists $d" >/dev/null 2>&1; docker exec cc-test-pg psql -U cc -d cc_test -c "create database $d" >/dev/null; done
cd /home/user/Jarvis/.worktrees/brain-062C/command-center/backend
TEST_DATABASE_URL=postgresql+asyncpg://cc:cc@127.0.0.1:55432/$L TEST_CREDIT_REPAIR_DATABASE_URL=postgresql+asyncpg://cc:cc@127.0.0.1:55432/${L}_cr \
REQUIRE_HERMES_SRC=1 ORACLE_SEED_BASE=$B ORACLE_SEEDS=$N ORACLE_RV3_SEEDS=0 ORACLE_LIVE_HISTORY=$H ORACLE_SECOND_WITHHOLD=$SW \
ORACLE_RECORDED=0 ORACLE_OUT=$OUT/$L.jsonl timeout 36000 $PY -m pytest -p no:randomly -p no:cacheprovider -q --no-header \
  tests/property/test_oracle_properties.py -k "test_oracle[" > $OUT/$L.txt 2>&1
tail -1 $OUT/$L.txt
for d in $L ${L}_cr; do docker exec cc-test-pg psql -U cc -d cc_test -c "drop database if exists $d" >/dev/null 2>&1; done
