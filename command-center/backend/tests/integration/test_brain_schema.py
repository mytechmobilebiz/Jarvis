"""0006_brain.sql and 0007_brain_withhold.sql on every database they meet: a fresh one, an upgrade from 0.5.0 or 0.6.0,
and the restore of a backup taken before 0.6.0 or by 0.6.0 (scripts/restore.ps1's pg_restore flags).

The two restore tests need docker (pg_dump and pg_restore in the cc-test-pg container) and are skipped without it;
REQUIRE_DOCKER_TESTS=1 (the release run) makes a missing docker or container a failure instead."""

from __future__ import annotations

import os
import shutil
import subprocess

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app import db

REQUIRED = os.environ.get("REQUIRE_DOCKER_TESTS") == "1"
needs_docker = pytest.mark.skipif(not shutil.which("docker") and not REQUIRED,
                                  reason="needs docker for pg_dump/pg_restore")
# This run's own scratch database and dump files (cc_test_v061 -> cc_test_v061_restore): two runs on the same test
# server, or another build's tests, never drop or restore each other's.
SCRATCH = os.environ["TEST_DATABASE_URL"].rsplit("/", 1)[1] + "_restore"
PRE_BRAIN_DUMP = "/tmp/%s_pre_brain.dump" % SCRATCH
DUMP_060 = "/tmp/%s_060.dump" % SCRATCH
OLD_VALUES = ("created", "imported", "edited", "approved", "rejected", "archived", "reopened", "deleted", "restored",
              "redacted", "reverted")
CHANGE_CHECKS = ("SELECT c.conname, pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_attribute a ON "
                 "a.attrelid = c.conrelid AND a.attname = 'change' WHERE c.conrelid = 'memory_item_versions'::regclass "
                 "AND c.contype = 'c' AND c.conkey = ARRAY[a.attnum]")


def _pg(container: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", "exec", container, *args], capture_output=True, text=True)


def _container() -> str:
    container = "cc-test-pg"
    if not shutil.which("docker") or _pg(container, "true").returncode != 0:
        (pytest.fail if REQUIRED else pytest.skip)("the test PostgreSQL container is not reachable with docker exec")
    return container


def _base(settings) -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)[0]


async def _fresh(settings) -> str:
    """An empty scratch database; its URL."""
    admin = await asyncpg.connect(_base(settings) + "/postgres")
    try:
        await admin.execute("DROP DATABASE IF EXISTS %s" % SCRATCH)
        await admin.execute("CREATE DATABASE %s" % SCRATCH)
    finally:
        await admin.close()
    return _base(settings) + "/" + SCRATCH


async def _drop(settings) -> None:
    admin = await asyncpg.connect(_base(settings) + "/postgres")
    try:
        await admin.execute("DROP DATABASE IF EXISTS %s" % SCRATCH)
    finally:
        await admin.close()


@pytest.fixture
async def scratch(settings):
    """An empty scratch database (its URL), dropped afterwards, also when the test fails."""
    url = await _fresh(settings)
    try:
        yield url
    finally:
        await _drop(settings)


@pytest.fixture
def dumps():
    """The dump files a test writes in the test PostgreSQL container, removed afterwards."""
    written: list[tuple[str, str]] = []
    yield written
    for container, path in written:
        _pg(container, "rm", "-f", path)


INDEXES_0007 = ["archive_sessions_source_session_idx", "tasks_quotes_idx"]


async def _indexes_0007(conn: asyncpg.Connection) -> list[str]:
    return sorted(r["indexname"] for r in await conn.fetch(
        "SELECT indexname FROM pg_indexes WHERE indexname = ANY($1::text[])", INDEXES_0007))


async def _install(url: str, before: str) -> None:
    """The migrations a release older than `before` applied, recorded in schema_migrations as the runner does."""
    conn = await asyncpg.connect(url)
    try:
        await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, "
                           "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        for path in db.migration_files():
            if path.stem < before:
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", path.stem)
    finally:
        await conn.close()


async def _migrate(url: str) -> list[str]:
    engine = create_async_engine(url.replace("postgresql://", "postgresql+asyncpg://", 1))
    try:
        return await db.run_migrations(engine)
    finally:
        await engine.dispose()


def test_the_scratch_database_and_dumps_are_this_runs_own():
    """Never a name another build's tests or the release runs use (cc_brain_restore, /tmp/pre_brain.dump)."""
    own = os.environ["TEST_DATABASE_URL"].rsplit("/", 1)[1]
    assert SCRATCH == own + "_restore" and own in PRE_BRAIN_DUMP and own in DUMP_060


async def _version_row(conn: asyncpg.Connection, change: str) -> None:
    item = await conn.fetchval("INSERT INTO memory_items (kind, title, content_hash) VALUES ('knowledge', 't', 'h') "
                               "RETURNING id")
    await conn.execute("INSERT INTO memory_item_versions (item_id, version, change, kind, title, sensitivity, status) "
                       "VALUES ($1, 1, $2, 'knowledge', 't', 'general', 'pending_review')", item, change)


@needs_docker
async def test_a_pre_brain_backup_restores_into_a_brain_database(scratch, dumps):
    container = _container()
    url = scratch
    dumps.append((container, PRE_BRAIN_DUMP))
    conn = await asyncpg.connect(url)
    try:
        for path in db.migration_files():
            if path.stem < "0006":
                await conn.execute(path.read_text(encoding="utf-8"))
        await conn.execute("INSERT INTO schedules (name, agent, capability, cron) VALUES ('s', 'monitoring', 'check_all_sites', '*/5 * * * *')")
    finally:
        await conn.close()
    dump = _pg(container, "pg_dump", "-U", "cc", "-Fc", "-f", PRE_BRAIN_DUMP, SCRATCH)
    assert dump.returncode == 0, dump.stderr
    conn = await asyncpg.connect(url)
    try:
        await conn.execute((db.MIGRATIONS_DIR / "0006_brain.sql").read_text(encoding="utf-8"))
        await conn.execute((db.MIGRATIONS_DIR / "0007_brain_withhold.sql").read_text(encoding="utf-8"))
        await conn.execute("INSERT INTO workflow_drafts (source_key, name, compatibility, schedule_id) VALUES ('k', 'n', 'mapped', 1)")
    finally:
        await conn.close()
    restore = _pg(container, "pg_restore", "-U", "cc", "-d", SCRATCH, "--clean", "--if-exists",
                  "--single-transaction", "--exit-on-error", PRE_BRAIN_DUMP)
    assert restore.returncode == 0, restore.stderr
    conn = await asyncpg.connect(url)
    try:
        assert await conn.fetchval("SELECT count(*) FROM schedules") == 1
        # The brain tables survive the restore; running 0006 and 0007 again is harmless.
        await conn.execute((db.MIGRATIONS_DIR / "0006_brain.sql").read_text(encoding="utf-8"))
        await conn.execute((db.MIGRATIONS_DIR / "0007_brain_withhold.sql").read_text(encoding="utf-8"))
        assert await conn.fetchval("SELECT count(*) FROM workflow_drafts") == 1
        await _version_row(conn, "quarantined")
    finally:
        await conn.close()


async def test_0007_replaces_the_change_check_whatever_its_name(scratch):
    url = scratch
    await _install(url, "0007")
    conn = await asyncpg.connect(url)
    try:
        await conn.execute("ALTER TABLE memory_item_versions RENAME CONSTRAINT memory_item_versions_change_check "
                           "TO some_other_name")
        await conn.execute((db.MIGRATIONS_DIR / "0007_brain_withhold.sql").read_text(encoding="utf-8"))
        checks = await conn.fetch(CHANGE_CHECKS)
        assert [c["conname"] for c in checks] == ["memory_item_versions_change_check"]
        assert "'quarantined'" in checks[0]["pg_get_constraintdef"]
    finally:
        await conn.close()


async def test_0007_runs_twice_and_keeps_every_0_6_0_value(scratch):
    url = scratch
    await _install(url, "0007")
    conn = await asyncpg.connect(url)
    try:
        for change in OLD_VALUES:  # rows 0.6.0 wrote stay valid through the new CHECK
            await _version_row(conn, change)
        for _ in range(2):
            await conn.execute((db.MIGRATIONS_DIR / "0007_brain_withhold.sql").read_text(encoding="utf-8"))
        assert len(await conn.fetch(CHANGE_CHECKS)) == 1
        assert await _indexes_0007(conn) == INDEXES_0007
        await _version_row(conn, "quarantined")
        with pytest.raises(asyncpg.CheckViolationError):
            await _version_row(conn, "bogus")
        assert await conn.fetchval("SELECT count(*) FROM memory_item_versions") == len(OLD_VALUES) + 1
    finally:
        await conn.close()


async def test_upgrade_from_0_5_0_applies_0006_and_0007_together(scratch):
    url = scratch
    await _install(url, "0006")
    assert await _migrate(url) == ["0006_brain", "0007_brain_withhold", "0008_brain_withhold_identity"]
    assert await _migrate(url) == []
    conn = await asyncpg.connect(url)
    try:
        await _version_row(conn, "quarantined")
        assert await _indexes_0007(conn) == INDEXES_0007
    finally:
        await conn.close()


async def test_upgrade_from_0_6_0_applies_0007_only(scratch):
    url = scratch
    await _install(url, "0007")
    conn = await asyncpg.connect(url)
    try:
        with pytest.raises(asyncpg.CheckViolationError):  # the 0.6.0 defect
            await _version_row(conn, "quarantined")
    finally:
        await conn.close()
    assert await _migrate(url) == ["0007_brain_withhold", "0008_brain_withhold_identity"]
    conn = await asyncpg.connect(url)
    try:
        await _version_row(conn, "quarantined")
        assert await _indexes_0007(conn) == INDEXES_0007
    finally:
        await conn.close()


@needs_docker
async def test_a_0_6_0_backup_restored_over_0_6_1_gets_the_new_check_again(scratch, dumps):
    container = _container()
    url = scratch
    dumps.append((container, DUMP_060))
    await _install(url, "0007")  # 0.6.0
    dump = _pg(container, "pg_dump", "-U", "cc", "-Fc", "-f", DUMP_060, SCRATCH)
    assert dump.returncode == 0, dump.stderr
    assert await _migrate(url) == ["0007_brain_withhold", "0008_brain_withhold_identity"]  # 0.6.2 installed
    restore = _pg(container, "pg_restore", "-U", "cc", "-d", SCRATCH, "--clean", "--if-exists",
                  "--single-transaction", "--exit-on-error", DUMP_060)
    assert restore.returncode == 0, restore.stderr
    conn = await asyncpg.connect(url)
    try:
        checks = await conn.fetch(CHANGE_CHECKS)
        assert len(checks) == 1 and "'quarantined'" not in checks[0]["pg_get_constraintdef"]  # 0.6.0's CHECK is back
        assert not await conn.fetchval("SELECT count(*) FROM schema_migrations WHERE version = '0007_brain_withhold'")
    finally:
        await conn.close()
    assert await _migrate(url) == ["0007_brain_withhold", "0008_brain_withhold_identity"]  # the next start
    conn = await asyncpg.connect(url)
    try:
        checks = await conn.fetch(CHANGE_CHECKS)
        assert [c["conname"] for c in checks] == ["memory_item_versions_change_check"]
        assert "'quarantined'" in checks[0]["pg_get_constraintdef"]
        assert await _indexes_0007(conn) == INDEXES_0007  # the restore dropped them with their tables
    finally:
        await conn.close()
