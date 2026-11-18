#!/usr/bin/env bash
# Rebuild environment for the 0.6.2 option C rebuild (idempotent). Run after any container restart.
# Test database: cc-test-pg on 127.0.0.1:55432 (user cc, db cc_test; test-only credentials, no real data).
set -u
R=/home/user/rebuild
mkdir -p $R
if ! docker info >/dev/null 2>&1; then nohup dockerd > $R/dockerd.log 2>&1 & for i in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done; fi
docker start cc-test-pg >/dev/null 2>&1 || docker run -d --name cc-test-pg -e POSTGRES_USER=cc -e POSTGRES_PASSWORD=cc \
  -e POSTGRES_DB=cc_test -p 127.0.0.1:55432:5432 mirror.gcr.io/library/postgres:18.6-alpine >/dev/null
for i in $(seq 1 30); do docker exec cc-test-pg pg_isready -U cc >/dev/null 2>&1 && break; sleep 1; done
docker exec cc-test-pg psql -U cc -d cc_test -tc "select 1 from pg_database where datname='cc_test_cr'" | grep -q 1 || \
  docker exec cc-test-pg psql -U cc -d cc_test -c 'create database cc_test_cr' >/dev/null
[ -x $R/venv/bin/python ] || { python3 -m venv $R/venv && $R/venv/bin/pip install -q -r "$(dirname "$0")/../../backend/requirements-dev.txt"; }
HERMES_COMMIT=b1a6350602383fdbed9b0bd0eb15d86eaa335da8   # last NousResearch/hermes-agent commit on 2026-11-10
if [ ! -d $R/hermes-src/h-$HERMES_COMMIT ]; then
  mkdir -p $R/hermes-src; [ -d $R/hermes-src/repo ] || git clone -q https://github.com/NousResearch/hermes-agent $R/hermes-src/repo
  git -C $R/hermes-src/repo worktree add -f -q $R/hermes-src/h-$HERMES_COMMIT $HERMES_COMMIT
fi
echo "export HERMES_SRC=$R/hermes-src/h-$HERMES_COMMIT PY=$R/venv/bin/python"
