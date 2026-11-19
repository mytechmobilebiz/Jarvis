"""The 0.6.2 oracle harness (plan 5.1): Oracle A (A1-A5) and Oracle B (B1-B7) over seeded Hermes histories.

Seeds are fixed and recorded: ORACLE_SEED_BASE (default 6200) .. + ORACLE_SEEDS (default 200; the release gate runs
at least 2000), plus every seed that found a bug in review round 3, replayed exactly (RV3_BUG_SEEDS,
rv3_replays.json; ORACLE_RV3_SEEDS=0 leaves them out). Each seed draws its own Hermes generation (uids or not, donor
timestamps or not, the _compressed_from marker or not, the uid insert trigger or not), then 16-40 operations split
between the test copy's history and later backups on live.

ORACLE_OUT=<file> appends one JSON line per seed (problems, notes, the Hermes log). ORACLE_LENIENT=1 notes a missing
Command Center entry point instead of failing on it. A docker restore round trip (pg_dump / pg_restore in cc-test-pg)
runs every ORACLE_RESTORE_EVERY-th seed (default 25) when docker is reachable; REQUIRE_DOCKER_TESTS=1 makes a missing
docker a failure. Synthetic data only.

Option C (rebuild R5): the oracle checks O1-O11 too (oracle.py), in two arms: ORACLE_LIVE_HISTORY=0/1 (live imported the
test copy's earlier backups before the decisions file, or not) and ORACLE_SECOND_WITHHOLD=0/1 (a second withhold on the
test copy after its review); unset, each arm is drawn per seed. RECORDED_SEEDS (ORACLE_RECORDED=0 leaves them out) are
the seeds of the fresh R5 runs (13000-13899, second-withhold arm, both live-history arms) recorded as regressions and as
coverage of option C's shapes; each runs in the second-withhold arm and in the live-history arm the environment names
(the release gate runs both). Old seed numbers do not reproduce old scenarios (the simulator gained operations)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from app.security import CSRF_HEADER, CSRF_VALUE

from . import scenario

pytestmark = pytest.mark.usefixtures("clean_db")

SEED_BASE = int(os.environ.get("ORACLE_SEED_BASE", "6200"))
N_SEEDS = int(os.environ.get("ORACLE_SEEDS", "200"))
# Every seed that found a bug in review round 3 (rv3/resurrection fuzz1/2/3.txt, fuzzp1/2.txt), replayed exactly:
# rv3_replay.py ports that driver's Hermes operations and rv3_replays.json holds what its admin choices consumed.
RV3_BUG_SEEDS = (1, 9, 16, 18, 22, 28, 33, 44, 45, 47, 49, 51, 56, 59, 66, 73, 84, 86, 94, 95, 107, 119, 121, 124, 125,
                 143, 152, 164, 167, 176, 218, 232, 247, 248, 252, 253, 261, 271, 272, 279, 298, 309, 321, 322, 333,
                 342, 344, 349)
SEEDS = list(range(SEED_BASE, SEED_BASE + N_SEEDS))
# rebuild R5: the recorded seeds (progress/R5.md says what each one covers or found)
RECORDED_SEEDS: tuple[int, ...] = ()
if os.environ.get("ORACLE_RECORDED", "1") != "1":
    RECORDED_SEEDS = ()
if os.environ.get("ORACLE_RV3_SEEDS", "1") != "1":
    RV3_BUG_SEEDS = ()
OWN_DB = os.environ["TEST_DATABASE_URL"].rsplit("/", 1)[1]


@pytest.fixture
async def client(app_client):
    app_client.headers[CSRF_HEADER] = CSRF_VALUE
    return app_client


class _Restore:
    """restore.ps1's database half: pg_dump now, pg_restore later with its flags (connections closed around it)."""

    def __init__(self, engine, container: str) -> None:
        self.engine, self.container = engine, container

    def _pg(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", "exec", self.container, *args], capture_output=True, text=True)

    def take(self, name: str) -> str:
        path = "/tmp/%s_oracle_%s.dump" % (OWN_DB, name)
        done = self._pg("pg_dump", "-U", "cc", "-Fc", "-f", path, OWN_DB)
        assert done.returncode == 0, done.stderr
        return path

    async def restore(self, path: str) -> None:
        from app import db

        await self.engine.dispose()
        done = self._pg("pg_restore", "-U", "cc", "-d", OWN_DB, "--clean", "--if-exists", "--single-transaction",
                        "--exit-on-error", path)
        await self.engine.dispose()
        self._pg("rm", "-f", path)
        assert done.returncode == 0, done.stderr
        await db.run_migrations()  # the next start


@pytest.fixture
def restore(migrated):
    required = os.environ.get("REQUIRE_DOCKER_TESTS") == "1"
    ok = shutil.which("docker") and subprocess.run(["docker", "exec", "cc-test-pg", "true"],
                                                   capture_output=True).returncode == 0
    if not ok:
        if required:
            pytest.fail("docker and the cc-test-pg container are needed for the restore round trips")
        return None
    return _Restore(migrated, "cc-test-pg")


def _record(seed, run) -> None:
    out = os.environ.get("ORACLE_OUT")
    if out:
        with open(out, "a") as f:
            f.write(json.dumps({"seed": seed, "problems": run.problems, "notes": run.notes,
                                "withholds": [(w.sid, w.row, w.text[:60]) for w in run.withholds],
                                "log": run.sim.log}) + "\n")


@pytest.mark.parametrize("seed", SEEDS)
async def test_oracle(seed, tmp_path, client, restore):
    run = await scenario.run_seed(seed, tmp_path, client, restore)
    _record(seed, run)
    assert not run.problems, "\n".join(run.problems + ["--- notes"] + run.notes + ["--- Hermes log"] + run.sim.log)


@pytest.mark.parametrize("seed", RV3_BUG_SEEDS)
async def test_rv3_bug_seed_replayed_exactly(seed, tmp_path, client):
    """Every seed that found a bug in round 3, its exact Hermes history and admin actions (rv3_replays.json)."""
    run = await scenario.run_replay(seed, tmp_path, client)
    _record(seed, run)
    assert not run.problems, "\n".join(run.problems + ["--- notes"] + run.notes + ["--- Hermes log"] + run.sim.log)


@pytest.mark.parametrize("seed", RECORDED_SEEDS)
async def test_oracle_recorded(seed, tmp_path, client, restore, monkeypatch):
    """A recorded seed of the R5 runs, in the second-withhold arm (the live-history arm from the environment)."""
    if os.environ.get("ORACLE_SECOND_WITHHOLD") not in ("0", "1"):
        monkeypatch.setenv("ORACLE_SECOND_WITHHOLD", "1")
    run = await scenario.run_seed(seed, tmp_path, client, restore)
    _record(seed, run)
    assert not run.problems, "\n".join(run.problems + ["--- notes"] + run.notes + ["--- Hermes log"] + run.sim.log)
