"""0008_research.sql and the Credit Repair index migration must not break restoring a backup taken before 0.7.0
(scripts/restore.ps1's pg_restore flags), and going back to 0.6.x must leave the new tables unused."""

from __future__ import annotations

import shutil
import subprocess

import asyncpg
import pytest

from app import db
from app.credit_repair import store as cr_store

pytestmark = pytest.mark.skipif(not shutil.which("docker"), reason="needs docker for pg_dump/pg_restore")

CONTAINER = "cc-test-pg"


def _pg(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", "exec", CONTAINER, *args], capture_output=True, text=True)


async def _fresh(base: str, name: str) -> None:
    admin = await asyncpg.connect(base + "/postgres")
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()


async def _drop(base: str, name: str) -> None:
    admin = await asyncpg.connect(base + "/postgres")
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        await admin.close()


def _restore(dump: str, name: str) -> None:
    restore = _pg("pg_restore", "-U", "cc", "-d", name, "--clean", "--if-exists", "--single-transaction",
                  "--exit-on-error", dump)
    assert restore.returncode == 0, restore.stderr


async def test_a_pre_research_backup_restores_into_a_research_database(settings):
    if _pg("true").returncode != 0:
        pytest.skip("the test PostgreSQL container is not reachable with docker exec")
    base = settings.database_url.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)[0]
    name, dump = "cc_rg_research_restore", "/tmp/cc_rg_pre_research.dump"
    await _fresh(base, name)
    try:
        conn = await asyncpg.connect(f"{base}/{name}")
        try:
            for path in db.migration_files():
                if path.stem < "0008":
                    await conn.execute(path.read_text(encoding="utf-8"))
            await conn.execute("INSERT INTO tasks (agent, capability, risk) VALUES ('seo_content', 'x', 'read')")
        finally:
            await conn.close()
        made = _pg("pg_dump", "-U", "cc", "-Fc", "-f", dump, name)
        assert made.returncode == 0, made.stderr
        conn = await asyncpg.connect(f"{base}/{name}")
        try:
            sql = (db.MIGRATIONS_DIR / "0008_research.sql").read_text(encoding="utf-8")
            await conn.execute(sql)
            task_id = await conn.fetchval("SELECT id FROM tasks")
            await conn.execute("INSERT INTO research_queries (task_id, agent, trigger_source, status, reason_code) "
                               "VALUES ($1, 'seo_content', 'dashboard', 'blocked', 'secret')", task_id)
        finally:
            await conn.close()
        _restore(dump, name)
        conn = await asyncpg.connect(f"{base}/{name}")
        try:
            # The older tables came back from the dump; the research tables kept their rows; 0008 runs again.
            assert await conn.fetchval("SELECT count(*) FROM tasks") == 1
            assert await conn.fetchval("SELECT count(*) FROM research_queries") == 1
            await conn.execute(sql)
            assert await conn.fetchval("SELECT count(*) FROM research_policy") == 8
        finally:
            await conn.close()
    finally:
        _pg("rm", "-f", dump)
        await _drop(base, name)


async def test_a_pre_research_credit_repair_backup_restores_over_the_index(settings):
    if _pg("true").returncode != 0:
        pytest.skip("the test PostgreSQL container is not reachable with docker exec")
    base = settings.database_url.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)[0]
    name, dump = "cc_rg_cr_restore", "/tmp/cc_rg_pre_research_cr.dump"
    files = sorted(cr_store.MIGRATIONS.glob("*.sql"))
    await _fresh(base, name)
    try:
        conn = await asyncpg.connect(f"{base}/{name}")
        try:
            await conn.execute(files[0].read_text(encoding="utf-8"))
            await conn.execute("INSERT INTO cr_clients (created_by, reference, full_name_enc) VALUES ('t', 'CR-0001', 'x')")
        finally:
            await conn.close()
        made = _pg("pg_dump", "-U", "cc", "-Fc", "-f", dump, name)
        assert made.returncode == 0, made.stderr
        conn = await asyncpg.connect(f"{base}/{name}")
        index_sql = (cr_store.MIGRATIONS / "0002_research_index.sql").read_text(encoding="utf-8")
        try:
            await conn.execute(index_sql)
            await conn.execute("INSERT INTO cr_research_index (client_id, kind, mac) VALUES (1, 'reference', $1)",
                               "0" * 64)
        finally:
            await conn.close()
        _restore(dump, name)
        conn = await asyncpg.connect(f"{base}/{name}")
        try:
            assert await conn.fetchval("SELECT count(*) FROM cr_clients") == 1
            await conn.execute(index_sql)  # twice: harmless
            assert await conn.fetchval("SELECT count(*) FROM cr_research_index") == 1
        finally:
            await conn.close()
    finally:
        _pg("rm", "-f", dump)
        await _drop(base, name)
