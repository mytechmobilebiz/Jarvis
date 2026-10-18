"""Delivering pending events to the configured workflow engine, with backoff."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db import session_scope
from ..models import Event, IntegrationStatus

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
BASE_DELAY_SECONDS = 5


async def record_integration(name: str, ok: bool, error: str = "", detail: dict | None = None) -> None:
    """Upsert, so concurrent workers recording the same integration never collide."""
    now = datetime.now(UTC)
    values: dict = {"name": name, "updated_at": now}
    if ok:
        values["last_ok_at"] = now
    else:
        values["last_error_at"] = now
        values["last_error"] = error[:500]
    if detail is not None:
        values["detail"] = detail
    stmt = pg_insert(IntegrationStatus).values(**values)
    stmt = stmt.on_conflict_do_update(index_elements=[IntegrationStatus.name], set_={k: v for k, v in values.items() if k != "name"})
    async with session_scope() as session:
        await session.execute(stmt)


async def forward_pending(engine, limit: int = 20) -> int:
    now = datetime.now(UTC)
    sent = 0
    errors: list[str] = []
    async with session_scope() as session:
        pending = (
            await session.scalars(
                select(Event)
                .where(Event.forward_status == "pending", Event.forward_next_at <= now)
                .order_by(Event.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for event in pending:
            if not engine.delivers:
                event.forward_status = "skipped"
                event.forward_error = "no workflow engine configured"
                continue
            try:
                await engine.deliver(event)
            except Exception as exc:  # noqa: BLE001 - any delivery failure is retried
                event.forward_attempts += 1
                event.forward_error = f"{exc.__class__.__name__}: {exc}"[:500]
                if event.forward_attempts >= MAX_ATTEMPTS:
                    event.forward_status = "failed"
                else:
                    delay = BASE_DELAY_SECONDS * 2 ** (event.forward_attempts - 1)
                    event.forward_next_at = now + timedelta(seconds=delay)
                errors.append(event.forward_error)
                continue
            event.forward_status = "sent"
            event.forwarded_at = now
            event.forward_error = ""
            sent += 1
    if sent:
        await record_integration(engine.name, True)
    if errors:
        await record_integration(engine.name, False, f"event delivery failed: {errors[-1]}")
    return sent
