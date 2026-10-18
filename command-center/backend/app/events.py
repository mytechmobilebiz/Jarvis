"""Recording events. The worker forwards pending ones to n8n (app/worker/forwarder.py)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .models import Event

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
# Forwarded to n8n whatever their severity: the owner wants to hear the all-clear.
ALWAYS_FORWARD = frozenset({"site.recovered"})


def event_payload(event: Event) -> dict[str, Any]:
    """What a workflow engine receives for an event (no secrets, no personal data)."""
    return {
        "event_id": event.id,
        "kind": event.kind,
        "severity": event.severity,
        "source": event.source,
        "title": event.title,
        "detail": event.detail,
        "at": event.at.isoformat() if event.at else None,
        "task_id": event.task_id,
    }


async def emit(
    session: AsyncSession,
    settings: Settings,
    *,
    kind: str,
    title: str,
    source: str,
    severity: str = "info",
    detail: dict[str, Any] | None = None,
    task_id: int | None = None,
) -> Event:
    forward = kind in ALWAYS_FORWARD or SEVERITY_RANK[severity] >= SEVERITY_RANK[settings.n8n_forward_min_severity]
    event = Event(
        kind=kind,
        title=title,
        source=source,
        severity=severity,
        detail=detail or {},
        task_id=task_id,
        forward_status="pending" if forward else "skipped",
    )
    session.add(event)
    await session.flush()
    return event
