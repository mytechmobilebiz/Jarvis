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
