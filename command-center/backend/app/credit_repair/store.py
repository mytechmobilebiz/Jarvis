"""The isolated Credit Repair store: its own engine, models, encryption and access log.

Nothing else in the platform imports this module's engine or key. Callers
get decrypted values only through the functions here, and each of those
writes an access-log row naming who looked at or changed what.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import ARRAY, BigInteger, Date, DateTime, ForeignKey, Integer, Text, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..config import Settings

MIGRATIONS = Path(__file__).parent / "migrations"
LOCK_ID = 7_314_002_212


class NotConfigured(RuntimeError):
    pass


class CRBase(DeclarativeBase):
    pass


class CRClient(CRBase):
    __tablename__ = "cr_clients"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[str] = mapped_column(Text)
    reference: Mapped[str] = mapped_column(Text, unique=True)
    full_name_enc: Mapped[str] = mapped_column(Text)
    address_enc: Mapped[str] = mapped_column(Text, default="")
    email_enc: Mapped[str] = mapped_column(Text, default="")
    phone_enc: Mapped[str] = mapped_column(Text, default="")
    dob_enc: Mapped[str] = mapped_column(Text, default="")
    ssn_last4_enc: Mapped[str] = mapped_column(Text, default="")
    notes_enc: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="intake")
    contract_signed_on: Mapped[date | None] = mapped_column(Date)
    services_completed_on: Mapped[date | None] = mapped_column(Date)
    fees_collected_cents: Mapped[int] = mapped_column(Integer, default=0)


class CRItem(CRBase):
    __tablename__ = "cr_items"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    client_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("cr_clients.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    bureau: Mapped[str] = mapped_column(Text)
    creditor_enc: Mapped[str] = mapped_column(Text)
    account_last4_enc: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text)
    detail_enc: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="open")


class CRLetter(CRBase):
    __tablename__ = "cr_letters"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    client_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("cr_clients.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    bureau: Mapped[str] = mapped_column(Text)
    item_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))
    content_enc: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="draft")
    task_id: Mapped[int | None] = mapped_column(BigInteger)
    mailed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CRAccessLog(CRBase):
    __tablename__ = "cr_access_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    client_id: Mapped[int | None] = mapped_column(BigInteger)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


# ------------------------------------------------------------------ plumbing

_engines: dict[str, tuple[AsyncEngine, async_sessionmaker[AsyncSession]]] = {}


def configured(settings: Settings) -> bool:
    return bool(settings.credit_repair_database_url and settings.credit_repair_data_key and settings.credit_repair_data_key.get_secret_value())


def _maker(settings: Settings) -> async_sessionmaker[AsyncSession]:
    if not configured(settings):
        raise NotConfigured("the Credit Repair store is not configured (CREDIT_REPAIR_DATABASE_URL, CREDIT_REPAIR_DATA_KEY)")
    url = settings.credit_repair_database_url
    if url not in _engines:
        engine = create_async_engine(url, pool_size=3, pool_pre_ping=True)
        _engines[url] = (engine, async_sessionmaker(engine, expire_on_commit=False))
    return _engines[url][1]


@asynccontextmanager
async def session(settings: Settings) -> AsyncIterator[AsyncSession]:
    async with _maker(settings)() as s:
        try:
            yield s
            await s.commit()
        except BaseException:
            await s.rollback()
            raise


async def dispose() -> None:
    for engine, _ in _engines.values():
        await engine.dispose()
    _engines.clear()


async def migrate(settings: Settings) -> list[str]:
    _maker(settings)
    engine = _engines[settings.credit_repair_database_url][0]
    applied: list[str] = []
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": LOCK_ID})
        await conn.execute(text("CREATE TABLE IF NOT EXISTS cr_schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"))
        done = {r[0] for r in await conn.execute(text("SELECT version FROM cr_schema_migrations"))}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.stem in done:
                continue
            raw = await conn.get_raw_connection()
            await raw.driver_connection.execute(path.read_text(encoding="utf-8"))
            await conn.execute(text("INSERT INTO cr_schema_migrations (version) VALUES (:v)"), {"v": path.stem})
            applied.append(path.stem)
    return applied


class Cipher:
    def __init__(self, settings: Settings) -> None:
        raw = settings.credit_repair_data_key.get_secret_value() if settings.credit_repair_data_key else ""
        if not raw:
            raise NotConfigured("CREDIT_REPAIR_DATA_KEY is not set")
        self._f = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"credit-repair:" + raw.encode()).digest()))

    def enc(self, value: str) -> str:
        return self._f.encrypt(value.encode("utf-8")).decode("ascii") if value else ""

    def dec(self, token: str) -> str:
        return self._f.decrypt(token.encode("ascii")).decode("utf-8") if token else ""


async def log_access(s: AsyncSession, actor: str, action: str, client_id: int | None = None, **detail: Any) -> None:
    s.add(CRAccessLog(actor=actor, action=action, client_id=client_id, detail=detail))


# --------------------------------------------------------- client operations

def client_view(c: CRClient, cipher: Cipher, full: bool = False) -> dict[str, Any]:
    view = {
        "id": c.id,
        "reference": c.reference,
        "full_name": cipher.dec(c.full_name_enc),
        "status": c.status,
        "contract_signed_on": c.contract_signed_on.isoformat() if c.contract_signed_on else None,
        "services_completed_on": c.services_completed_on.isoformat() if c.services_completed_on else None,
        "fees_collected_cents": c.fees_collected_cents,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }
    if full:
        view.update(
            address=cipher.dec(c.address_enc), email=cipher.dec(c.email_enc), phone=cipher.dec(c.phone_enc),
            dob=cipher.dec(c.dob_enc), ssn_last4=cipher.dec(c.ssn_last4_enc), notes=cipher.dec(c.notes_enc),
        )
    return view


async def next_reference(s: AsyncSession) -> str:
    count = await s.scalar(select(func.count()).select_from(CRClient))
    return f"CR-{(count or 0) + 1:04d}"


def business_days_after(start: date, days: int) -> date:
    from datetime import timedelta

    current, added = start, 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() < 5:
            added += 1
    return current


def now() -> datetime:
    return datetime.now(UTC)
