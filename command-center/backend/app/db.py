"""Database engine, sessions and the SQL migration runner."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
# Arbitrary constant so concurrent API/worker start-ups apply migrations once.
MIGRATION_LOCK_ID = 7_314_002_211

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            pool_pre_ping=True,
        )
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A session that commits on success and rolls back on error."""
    async with sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


async def run_migrations(engine: AsyncEngine | None = None) -> list[str]:
    """Apply pending migrations in filename order. Returns the versions applied."""
    engine = engine or get_engine()
    applied: list[str] = []
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": MIGRATION_LOCK_ID})
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
        )
        done = {row[0] for row in await conn.execute(text("SELECT version FROM schema_migrations"))}
        for path in migration_files():
            version = path.stem
            if version in done:
                continue
            log.info("applying migration %s", version)
            # asyncpg's own execute() uses the simple query protocol, which runs a
            # file of several statements; SQLAlchemy would prepare it and fail.
            raw = await conn.get_raw_connection()
            await raw.driver_connection.execute(path.read_text(encoding="utf-8"))
            await conn.execute(text("INSERT INTO schema_migrations (version) VALUES (:v)"), {"v": version})
            applied.append(version)
    return applied
