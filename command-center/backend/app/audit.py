"""Append-only audit trail of who changed what."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuditLog


async def record(
    session: AsyncSession,
    actor: str,
    action: str,
    target_type: str = "",
    target_id: str | int = "",
    detail: dict[str, Any] | None = None,
    ip: str = "",
) -> None:
    session.add(
        AuditLog(
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            detail=detail or {},
            ip=ip,
        )
    )
