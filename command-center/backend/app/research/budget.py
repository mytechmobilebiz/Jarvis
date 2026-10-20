"""Daily caps and spacing for research searches (plan section 3.10, owner decision 4).

reserve() takes one slot before a search is sent, in its own short transaction that is committed before the call:
no lock is held across HTTP, and a task that fails or times out afterwards cannot undo the count. The transaction
takes a PostgreSQL advisory lock first, so reservations made at the same moment, in one worker or several, are
counted one after the other and never exceed a cap.

A slot counts against the caps of its UTC day while it is "sent", or "reserved" and younger than STALE_AFTER: a worker
that crashed mid-call frees its slot after ten minutes. Caps: the global cap (50 a day at first), the agent's cap from
its policy, the person's cap (30): every Supervisor-planned step counts against the person who typed the request.
A cap of 0 refuses. Spacing: a token bucket per provider (SearXNG: one search every 3 seconds, bursts of 3), which
protects the owner's home IP from upstream blocks.

Cache hits take no slot.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db import session_scope
from ..models import ResearchRate, ResearchReservation
from .store import add_usage

LOCK_NAMESPACE = 7_316  # pg_advisory_xact_lock(namespace, 0): research budget
STALE_AFTER = timedelta(minutes=10)


@dataclass(frozen=True)
class Spacing:
    interval: float  # seconds per token
    burst: int


SEARXNG_SPACING = Spacing(interval=3.0, burst=3)


@dataclass(frozen=True)
class Caps:
    global_daily: int
    agent_daily: int
    person_daily: int


class BudgetRefused(Exception):
    """status: refused_budget or refused_rate; code: which cap; retry_after: seconds, for spacing."""

    def __init__(self, status: str, code: str, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.status, self.code, self.retry_after = status, code, retry_after


def _counting(at: datetime, provider: str, kind: str):
    return and_(ResearchReservation.day == at.date(), ResearchReservation.provider == provider,
                ResearchReservation.kind == kind,
                or_(ResearchReservation.status == "sent",
                    and_(ResearchReservation.status == "reserved", ResearchReservation.created_at > at - STALE_AFTER)))


async def reserve(*, provider: str, agent: str, person: str, caps: Caps, spacing: Spacing = SEARXNG_SPACING,
                  kind: str = "search", at: datetime | None = None) -> int:
    """A slot for one search; its id. Raises BudgetRefused when a cap or the spacing does not allow it."""
    at = at or datetime.now(UTC)
    if 0 in (caps.global_daily, caps.agent_daily, caps.person_daily):
        raise BudgetRefused("refused_budget", "cap_zero")
    async with session_scope() as session:
        await session.execute(text("SELECT pg_advisory_xact_lock(:ns, 0)"), {"ns": LOCK_NAMESPACE})
        used, by_agent, by_person = (await session.execute(
            select(func.count(),
                   func.count().filter(ResearchReservation.agent == agent),
                   func.count().filter(ResearchReservation.person == person))
            .where(_counting(at, provider, kind)))).one()
        if used >= caps.global_daily:
            raise BudgetRefused("refused_budget", "global_cap")
        if by_agent >= caps.agent_daily:
            raise BudgetRefused("refused_budget", "agent_cap")
        if by_person >= caps.person_daily:
            raise BudgetRefused("refused_budget", "person_cap")

        await session.execute(pg_insert(ResearchRate).values(provider=provider, burst_tokens=float(spacing.burst))
                              .on_conflict_do_nothing(index_elements=[ResearchRate.provider]))
        rate = await session.scalar(select(ResearchRate).where(ResearchRate.provider == provider)
                                    .with_for_update().execution_options(populate_existing=True))
        assert rate is not None
        tokens = float(spacing.burst)
        if rate.last_sent_at is not None:
            elapsed = max((at - rate.last_sent_at).total_seconds(), 0.0)
            tokens = min(float(spacing.burst), rate.burst_tokens + elapsed / spacing.interval)
        if tokens < 1.0:
            raise BudgetRefused("refused_rate", "spacing", math.ceil((1.0 - tokens) * spacing.interval))
        rate.burst_tokens, rate.last_sent_at = tokens - 1.0, at

        slot = ResearchReservation(created_at=at, day=at.date(), provider=provider, agent=agent, person=person,
                                   kind=kind, status="reserved")
        session.add(slot)
        await session.flush()
        await add_usage(session, day=at.date(), provider=provider, agent=agent, kind=kind, reserved=1)
        return slot.id


async def counts(*, provider: str, kind: str = "search", at: datetime | None = None
                 ) -> tuple[int, dict[str, int], dict[str, int]]:
    """Today's slots that count against the caps: (in all, per agent, per person), for the usage screen."""
    at = at or datetime.now(UTC)
    async with session_scope() as session:
        rows = (await session.execute(
            select(ResearchReservation.agent, ResearchReservation.person, func.count())
            .where(_counting(at, provider, kind))
            .group_by(ResearchReservation.agent, ResearchReservation.person))).all()
    by_agent: dict[str, int] = {}
    by_person: dict[str, int] = {}
    for agent, person, n in rows:
        by_agent[agent] = by_agent.get(agent, 0) + n
        by_person[person] = by_person.get(person, 0) + n
    return sum(by_agent.values()), by_agent, by_person


async def finish(slot_id: int, *, sent: bool, query_id: int | None = None, at: datetime | None = None) -> None:
    """The search was sent (the slot keeps counting) or not (the slot is released). Own transaction."""
    at = at or datetime.now(UTC)
    async with session_scope() as session:
        slot = await session.get(ResearchReservation, slot_id, with_for_update=True)
        if slot is None or slot.status != "reserved":
            return
        slot.status, slot.query_id, slot.finished_at = ("sent" if sent else "released"), query_id, at
        if sent:
            await add_usage(session, day=slot.day, provider=slot.provider, agent=slot.agent, kind=slot.kind, sent=1)


async def expire_stale(at: datetime | None = None) -> int:
    """Marks the slots of crashed searches as expired (they stopped counting at STALE_AFTER already)."""
    at = at or datetime.now(UTC)
    async with session_scope() as session:
        result = await session.execute(update(ResearchReservation)
                                       .where(ResearchReservation.status == "reserved",
                                              ResearchReservation.created_at <= at - STALE_AFTER)
                                       .values(status="expired", finished_at=at))
        return result.rowcount or 0
