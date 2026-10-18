"""Delivering pending events to the n8n events webhook, with backoff."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ..db import session_scope
from ..integrations.n8n import N8nClient
from ..models import Event, IntegrationStatus

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
BASE_DELAY_SECONDS = 5


async def record_integration(name: str, ok: bool, error: str = "", detail: dict | None = None) -> None:
    now = datetime.now(UTC)
    async with session_scope() as session:
        row = await session.get(IntegrationStatus, name, with_for_update=True)
        if row is None:
            row = IntegrationStatus(name=name, detail={})
            session.add(row)
        if ok:
            row.last_ok_at = now
        else:
            row.last_error_at = now
            row.last_error = error[:500]
        if detail is not None:
            row.detail = detail
        row.updated_at = now


async def forward_pending(n8n: N8nClient, limit: int = 20) -> int:
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
            try:
                await n8n.send_event(event)
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
        await record_integration("n8n", True)
    if errors:
        await record_integration("n8n", False, f"event delivery failed: {errors[-1]}")
    return sent
