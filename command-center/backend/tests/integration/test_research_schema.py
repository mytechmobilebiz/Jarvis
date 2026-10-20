"""0008_research.sql and the Credit Repair index migration must not break restoring a backup taken before 0.7.0
(scripts/restore.ps1's pg_restore flags), and going back to 0.6.x must leave the new tables unused. An upgrade from
0.6.1 applies exactly the two research migrations and changes no row the upgrade guide fingerprints."""

from __future__ import annotations

import os
import shutil
import subprocess

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import bootstrap, db
from app.agents.registry import all_agents
from app.credit_repair import blind_index
from app.credit_repair import store as cr_store

pytestmark = pytest.mark.skipif(not shutil.which("docker"), reason="needs docker for pg_dump/pg_restore")

CONTAINER = "cc-test-pg"
# This run's own scratch databases and dumps (cc_test_070b -> cc_test_070b_up061, cc_test_070b_rsr): two runs on the
# same test server never drop or restore each other's.
RUN = os.environ["TEST_DATABASE_URL"].rsplit("/", 1)[1]
UPGRADE = RUN + "_up061"
UPGRADE_CR = UPGRADE + "_cr"
# The upgrade guide's step D, the data lines (the n8n lines are in n8n's own database).
FINGERPRINT = """
SELECT 'users', md5(string_agg(concat_ws(',',id,email,role,password_hash,can_access_sensitive,is_active),'|' ORDER BY id)) FROM users
UNION ALL SELECT 'secret_values', md5(string_agg(concat_ws(',',name,ciphertext,hint),'|' ORDER BY name)) FROM secret_values
UNION ALL SELECT 'app_settings', md5(string_agg(concat_ws(',',key,value::text),'|' ORDER BY key)) FROM app_settings
UNION ALL SELECT 'agent_state', string_agg(name||'='||enabled,',' ORDER BY name) FROM agent_state
UNION ALL SELECT 'sites', md5(string_agg(concat_ws(',',id,name,url,client,enabled,environment),'|' ORDER BY id)) FROM sites
UNION ALL SELECT 'leads', md5(string_agg(concat_ws(',',id,company,email,score,status),'|' ORDER BY id)) FROM leads
UNION ALL SELECT 'schedules', md5(string_agg(concat_ws(',',id,name,agent,capability,cron,enabled),'|' ORDER BY id)) FROM schedules
UNION ALL SELECT 'schedules_enabled', count(*)::text FROM schedules WHERE enabled
"""
# The schedules a 0.6.1 installation has (bootstrap.DEFAULT_SCHEDULES and the WordPress security report), one of them
# changed by the owner: 0.7.0 adds none (research is never scheduled).
SCHEDULES_061 = (("wordpress-uptime-sweep", "monitoring", "check_all_sites", "*/30 * * * *", True),
                 ("wordpress-security-scan", "wordpress_security", "security_scan_all", "17 3 * * *", False),
                 ("supervisor-health-review", "supervisor", "health_review", "7 * * * *", True),
                 ("wordpress-security-report", "wordpress_security", "security_report", "43 6 * * 1", True))


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
    name, dump = RUN + "_rsr", f"/tmp/{RUN}_pre_research.dump"
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
    name, dump = RUN + "_rsr_cr", f"/tmp/{RUN}_pre_research_cr.dump"
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


async def _fingerprint(conn: asyncpg.Connection) -> list[tuple[str, str | None]]:
    return [tuple(r) for r in await conn.fetch(FINGERPRINT)]


async def test_upgrade_from_0_6_1_applies_0008_only_and_changes_no_existing_row(settings, monkeypatch):
    if _pg("true").returncode != 0:
        pytest.skip("the test PostgreSQL container is not reachable with docker exec")
    base = settings.database_url.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)[0]
    await _fresh(base, UPGRADE)
    await _fresh(base, UPGRADE_CR)
    engine = create_async_engine(f"{base}/{UPGRADE}".replace("postgresql://", "postgresql+asyncpg://", 1))
    cr_settings = settings.model_copy(update={"credit_repair_database_url": f"{base}/{UPGRADE_CR}".replace(
        "postgresql://", "postgresql+asyncpg://", 1)})
    try:
        conn = await asyncpg.connect(f"{base}/{UPGRADE}")
        try:  # 0.6.1: 0001-0007 applied and recorded, with data
            await conn.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, "
                               "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            for path in db.migration_files():
                if path.stem < "0008":
                    await conn.execute(path.read_text(encoding="utf-8"))
                    await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", path.stem)
            await conn.execute("INSERT INTO users (email, password_hash, role, can_access_sensitive) VALUES "
                               "('owner@example.com', 'hash-1', 'admin', TRUE), ('op@example.com', 'hash-2', "
                               "'operator', FALSE)")
            await conn.execute("""INSERT INTO app_settings (key, value) VALUES
                                  ('credit_repair_enabled', '{"enabled": true}'),
                                  ('customer_settings', '{"business_name": "Example Marketing"}')""")
            await conn.execute("INSERT INTO secret_values (name, ciphertext, hint) VALUES "
                               "('wp_app_password', 'c1', 'h')")
            await conn.execute("INSERT INTO agent_state (name, enabled) VALUES ('seo_content', FALSE), "
                               "('communications', FALSE)")
            await conn.execute("INSERT INTO sites (name, url, client) VALUES ('Site One', 'https://one.example', 'C1')")
            await conn.execute("INSERT INTO leads (name, company, email) VALUES ('Sacha Coe', 'Coe LLC', "
                               "'sacha@coe.example')")
            for name, agent, capability, cron, enabled in SCHEDULES_061:
                await conn.execute("INSERT INTO schedules (name, agent, capability, cron, enabled) VALUES "
                                   "($1, $2, $3, $4, $5)", name, agent, capability, cron, enabled)
            before = await _fingerprint(conn)
        finally:
            await conn.close()
        cr = await asyncpg.connect(f"{base}/{UPGRADE_CR}")
        try:  # the Credit Repair database of 0.6.1: 0001, one client
            await cr.execute("CREATE TABLE cr_schema_migrations (version TEXT PRIMARY KEY, "
                             "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            first = sorted(cr_store.MIGRATIONS.glob("*.sql"))[0]
            await cr.execute(first.read_text(encoding="utf-8"))
            await cr.execute("INSERT INTO cr_schema_migrations (version) VALUES ($1)", first.stem)
            await cr.execute("INSERT INTO cr_clients (created_by, reference, full_name_enc) VALUES "
                             "('t', 'CR-0001', $1)", cr_store.Cipher(cr_settings).enc("Lee Older Client"))
            clients = await cr.fetchval("SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM cr_clients t")
        finally:
            await cr.close()

        # The first start of 0.7.0 (app/bootstrap.py): migrations, the admin, the Credit Repair store, the schedules.
        assert await db.run_migrations(engine) == ["0008_research"]
        monkeypatch.setattr(db, "_engine", engine)
        monkeypatch.setattr(db, "_sessionmaker", async_sessionmaker(engine, expire_on_commit=False))
        assert await bootstrap.ensure_admin(settings) is False
        assert await cr_store.migrate(cr_settings) == ["0002_research_index"]
        await bootstrap.ensure_schedules([s for a in all_agents().values()
                                          for s in getattr(a, "default_schedules", [])])
        assert await db.run_migrations(engine) == [] and await cr_store.migrate(cr_settings) == []  # the next start

        conn = await asyncpg.connect(f"{base}/{UPGRADE}")
        try:
            assert await _fingerprint(conn) == before
            assert await conn.fetchval("SELECT count(*) FROM research_policy") == 8
            assert await conn.fetchval("SELECT count(*) FROM app_settings WHERE key = 'research_mode'") == 0  # off
            assert await conn.fetchval("SELECT count(*) FROM research_queries") == 0
        finally:
            await conn.close()
        cr = await asyncpg.connect(f"{base}/{UPGRADE_CR}")
        try:
            assert await cr.fetchval("SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM cr_clients t") == clients
            # Even with research off, each start decrypts every client for the match index, and says so.
            assert [tuple(r) for r in await cr.fetch("SELECT actor, action FROM cr_access_log ORDER BY id")] == [
                ("system:research-index", "research_index_rebuild")] * 2
            assert await cr.fetchval("SELECT version FROM cr_research_index_meta") == blind_index.INDEX_VERSION
        finally:
            await cr.close()
    finally:
        await engine.dispose()
        cr_engine = cr_store._engines.pop(cr_settings.credit_repair_database_url, None)
        if cr_engine:
            await cr_engine[0].dispose()
        await _drop(base, UPGRADE)
        await _drop(base, UPGRADE_CR)
