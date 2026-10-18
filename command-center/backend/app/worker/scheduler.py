"""Cron schedules: enqueue a task when a schedule comes due."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from croniter import croniter
from sqlalchemy import or_, select

from .. import tasks as task_service
from ..config import Settings
from ..db import session_scope
from ..models import Schedule, Task

log = logging.getLogger(__name__)


def next_fire(cron: str, after: datetime) -> datetime:
    return croniter(cron, after).get_next(datetime).astimezone(UTC)


def valid_cron(cron: str) -> bool:
    return croniter.is_valid(cron)


async def enqueue_due(settings: Settings, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    fired = 0
    async with session_scope() as session:
        due = (
            await session.scalars(
                select(Schedule)
                .where(Schedule.enabled.is_(True), or_(Schedule.next_run_at.is_(None), Schedule.next_run_at <= now))
                .with_for_update(skip_locked=True)
            )
        ).all()
        for schedule in due:
            if schedule.next_run_at is None:
                # Newly created or re-enabled: wait for the first real slot.
                schedule.next_run_at = next_fire(schedule.cron, now)
                continue
            busy = await session.scalar(
                select(Task.id)
                .where(Task.schedule_id == schedule.id, Task.status.in_(("queued", "running", "awaiting_approval")))
                .limit(1)
            )
            if busy is None:
                try:
                    await task_service.create_task(
                        session,
                        settings,
                        agent=schedule.agent,
                        capability=schedule.capability,
                        params=schedule.params,
                        source="schedule",
                        created_by=f"schedule:{schedule.name}",
                        schedule_id=schedule.id,
                        priority=150,
                    )
                    fired += 1
                except task_service.TaskError as exc:
                    log.warning("schedule %s could not create its task: %s", schedule.name, exc)
            else:
                log.info("schedule %s skipped: previous run still in progress", schedule.name)
            schedule.last_run_at = now
            schedule.next_run_at = next_fire(schedule.cron, now)
    return fired
