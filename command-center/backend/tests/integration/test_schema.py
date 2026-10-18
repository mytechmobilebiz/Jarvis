"""The ORM mappings must match the tables the SQL migrations create."""

from __future__ import annotations

from sqlalchemy import text

from app.models import Base


async def test_orm_matches_database(migrated):
    async with migrated.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'")
            )
        ).all()
    db_columns: dict[str, set[str]] = {}
    for table, column in rows:
        db_columns.setdefault(table, set()).add(column)
    for table in Base.metadata.sorted_tables:
        assert table.name in db_columns, f"table {table.name} missing from migrations"
        orm = {c.name for c in table.columns}
        assert orm == db_columns[table.name], f"{table.name}: orm-only {orm - db_columns[table.name]}, db-only {db_columns[table.name] - orm}"


async def test_migrations_are_idempotent(migrated):
    from app.db import run_migrations

    assert await run_migrations() == []


async def test_upgrade_to_official_agents_moves_existing_records(settings):
    """0.2.0 data (old agent names) lands on the seven official agents after migration 0004."""
    import asyncpg

    from app.db import migration_files

    base = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    admin = await asyncpg.connect(base)
    try:
        await admin.execute('DROP DATABASE IF EXISTS "cc_test_upgrade"')
        await admin.execute('CREATE DATABASE "cc_test_upgrade"')
    finally:
        await admin.close()
    conn = await asyncpg.connect(base.rsplit("/", 1)[0] + "/cc_test_upgrade")
    try:
        files = {p.stem: p for p in migration_files()}
        for version in ("0001_core", "0002_settings_and_secrets", "0003_agent_records"):
            await conn.execute(files[version].read_text())
        await conn.execute("""
            INSERT INTO tasks (id, agent, capability, params, risk, status) VALUES
              (1, 'wordpress_monitor', 'check_all_sites', '{}', 'read', 'succeeded'),
              (2, 'sales', 'send_proposal', '{"lead_id": 1}', 'external', 'awaiting_approval'),
              (3, 'support', 'triage_ticket', '{"ticket_id": 1}', 'draft', 'queued'),
              (4, 'content', 'publish_draft', '{"artifact_id": 1, "destination": "wordpress", "site_id": 1}', 'external', 'awaiting_approval'),
              (5, 'content', 'publish_draft', '{"artifact_id": 2, "destination": "facebook"}', 'external', 'awaiting_approval'),
              (6, 'content', 'draft_social_posts', '{"topic": "x"}', 'draft', 'succeeded'),
              (7, 'seo', 'audit_page', '{"url": "https://a.test"}', 'read', 'succeeded'),
              (8, 'ads', 'change_budget', '{}', 'financial', 'rejected'),
              (9, 'credit_repair', 'compliance_review', '{}', 'read', 'succeeded');
            INSERT INTO approvals (task_id, agent, capability, risk, summary) VALUES
              (2, 'sales', 'send_proposal', 'external', 's'), (4, 'content', 'publish_draft', 'external', 's'),
              (5, 'content', 'publish_draft', 'external', 's');
            INSERT INTO schedules (name, agent, capability, cron) VALUES ('wordpress-uptime-sweep', 'wordpress_monitor', 'check_all_sites', '*/15 * * * *');
            INSERT INTO artifacts (agent, kind, title) VALUES ('content', 'blog_draft', 'b'), ('content', 'social_drafts', 's'), ('sales', 'proposal', 'p');
            INSERT INTO outbox (agent, integration, action, approved_by) VALUES ('content', 'social', 'publish_post', 'a'),
              ('content', 'wordpress_publish', 'publish_post', 'a'), ('ads', 'ads', 'set_daily_budget', 'a');
            INSERT INTO events (kind, source, title) VALUES ('site.down', 'wordpress_monitor', 'x');
            INSERT INTO agent_state (name, enabled) VALUES ('sales', TRUE), ('support', FALSE), ('content', FALSE), ('seo', TRUE), ('wordpress_monitor', TRUE);
        """)
        await conn.execute(files["0004_official_agents"].read_text())

        tasks = {r["id"]: r["agent"] for r in await conn.fetch("SELECT id, agent FROM tasks")}
        assert tasks == {1: "monitoring", 2: "communications", 3: "communications", 4: "seo_content", 5: "social_marketing",
                         6: "social_marketing", 7: "seo_content", 8: "social_marketing", 9: "credit_repair"}
        approvals = {r["task_id"]: r["agent"] for r in await conn.fetch("SELECT task_id, agent FROM approvals")}
        assert approvals == {2: "communications", 4: "seo_content", 5: "social_marketing"}
        assert await conn.fetchval("SELECT agent FROM schedules") == "monitoring"
        assert {r["kind"]: r["agent"] for r in await conn.fetch("SELECT kind, agent FROM artifacts")} == {
            "blog_draft": "seo_content", "social_drafts": "social_marketing", "proposal": "communications"}
        assert {r["integration"]: r["agent"] for r in await conn.fetch("SELECT integration, agent FROM outbox")} == {
            "social": "social_marketing", "wordpress_publish": "seo_content", "ads": "social_marketing"}
        assert await conn.fetchval("SELECT source FROM events") == "monitoring"
        # Switched-off agents stay off in whatever they were folded into.
        state = {r["name"]: r["enabled"] for r in await conn.fetch("SELECT name, enabled FROM agent_state")}
        assert state == {"communications": False, "seo_content": False, "social_marketing": False, "monitoring": True}
    finally:
        await conn.close()
        admin = await asyncpg.connect(base)
        await admin.execute('DROP DATABASE IF EXISTS "cc_test_upgrade"')
        await admin.close()
