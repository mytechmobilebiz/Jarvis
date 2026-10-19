"""0006_brain.sql must not break restoring a backup taken before 0.6.0 (scripts/restore.ps1's pg_restore flags)."""

from __future__ import annotations

import shutil
import subprocess

import asyncpg
import pytest

from app import db

pytestmark = pytest.mark.skipif(not shutil.which("docker"), reason="needs docker for pg_dump/pg_restore")


def _pg(container: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", "exec", container, *args], capture_output=True, text=True)


async def test_a_pre_brain_backup_restores_into_a_brain_database(settings):
    container = "cc-test-pg"
    if _pg(container, "true").returncode != 0:
        pytest.skip("the test PostgreSQL container is not reachable with docker exec")
    base = settings.database_url.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)[0]
    admin = await asyncpg.connect(base + "/postgres")
    try:
        await admin.execute("DROP DATABASE IF EXISTS cc_brain_restore")
        await admin.execute("CREATE DATABASE cc_brain_restore")
    finally:
        await admin.close()
    conn = await asyncpg.connect(base + "/cc_brain_restore")
    try:
        for path in db.migration_files():
            if path.stem < "0006":
                await conn.execute(path.read_text(encoding="utf-8"))
        await conn.execute("INSERT INTO schedules (name, agent, capability, cron) VALUES ('s', 'monitoring', 'check_all_sites', '*/5 * * * *')")
    finally:
        await conn.close()
    dump = _pg(container, "pg_dump", "-U", "cc", "-Fc", "-f", "/tmp/pre_brain.dump", "cc_brain_restore")
    assert dump.returncode == 0, dump.stderr
    conn = await asyncpg.connect(base + "/cc_brain_restore")
    try:
        await conn.execute((db.MIGRATIONS_DIR / "0006_brain.sql").read_text(encoding="utf-8"))
        await conn.execute("INSERT INTO workflow_drafts (source_key, name, compatibility, schedule_id) VALUES ('k', 'n', 'mapped', 1)")
    finally:
        await conn.close()
    restore = _pg(container, "pg_restore", "-U", "cc", "-d", "cc_brain_restore", "--clean", "--if-exists",
                  "--single-transaction", "--exit-on-error", "/tmp/pre_brain.dump")
    assert restore.returncode == 0, restore.stderr
    conn = await asyncpg.connect(base + "/cc_brain_restore")
    try:
        assert await conn.fetchval("SELECT count(*) FROM schedules") == 1
        # The brain tables survive the restore; running 0006 again is harmless.
        await conn.execute((db.MIGRATIONS_DIR / "0006_brain.sql").read_text(encoding="utf-8"))
        assert await conn.fetchval("SELECT count(*) FROM workflow_drafts") == 1
    finally:
        await conn.close()
    admin = await asyncpg.connect(base + "/postgres")
    try:
        await admin.execute("DROP DATABASE IF EXISTS cc_brain_restore")
    finally:
        await admin.close()
