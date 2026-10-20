"""The research records (plan sections 3.12 and 3.14): every attempt, its results, the pages they cite, daily usage.

Every function here runs in its own short transaction (session_scope), never in the task's: a task that fails or
times out cannot erase what it searched, and a refused attempt is recorded too. The worker writes them; the API only
reads them.

A blocked attempt (status "blocked") and one refused by the policy before its text was screened ("refused_policy")
keep the reason code and, for a lead or ticket match, that record's id: never the text, never a hash of it, never its
options. log_attempt() drops them whatever the caller passes, and a CHECK in 0008_research.sql holds it in the
database too.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, cast, delete, func, literal, literal_column, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db import session_scope
from ..models import (
    ResearchCache,
    ResearchQuery,
    ResearchReservation,
    ResearchResult,
    ResearchSource,
    ResearchUsageDaily,
    SupervisorRequest,
    Task,
)
from .normalize import url_sha256
from .policy import ResearchSettings

WITHOUT_TEXT = ("blocked", "refused_policy")
REFUSED = ("blocked", "refused_policy", "refused_budget", "refused_rate")
SUCCEEDED = ("ok", "partial", "empty")
USAGE_COLUMNS = ("reserved", "sent", "billable", "possibly_billable", "cache_hits", "blocked", "failed")


@dataclass(frozen=True)
class Attempt:
    """Who asked and how, as the gateway resolved it from the database."""

    agent: str
    capability: str = ""
    task_id: int | None = None
    request_id: int | None = None
    requested_by: str = ""
    trigger_source: str = "unknown"
    kind: str = "search"


def now() -> datetime:
    return datetime.now(UTC)


async def add_usage(session: Any, *, day: date, provider: str, agent: str, kind: str = "search", **counts: int) -> None:
    """Adds to the day's totals (in the caller's transaction)."""
    values = {k: v for k, v in counts.items() if k in USAGE_COLUMNS and v}
    if not values:
        return
    stmt = pg_insert(ResearchUsageDaily).values(day=day, provider=provider or "none", agent=agent, kind=kind, **values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[ResearchUsageDaily.day, ResearchUsageDaily.provider, ResearchUsageDaily.agent,
                        ResearchUsageDaily.kind],
        set_={k: getattr(ResearchUsageDaily, k) + stmt.excluded[k] for k in values})
    await session.execute(stmt)


async def log_attempt(attempt: Attempt, *, status: str, reason: str, mode: str, provider: str = "", ref: str = "",
                      query_text: str | None = None, cache_key: str | None = None,
                      options: dict[str, Any] | None = None, at: datetime | None = None) -> int:
    """A finished attempt without results (refused, or failed before the provider answered). Returns its id."""
    at = at or now()
    if status in WITHOUT_TEXT:
        query_text, cache_key, options = None, None, {}
    async with session_scope() as session:
        row = ResearchQuery(
            created_at=at, finished_at=at, task_id=attempt.task_id, request_id=attempt.request_id, agent=attempt.agent,
            capability=attempt.capability, requested_by=attempt.requested_by, trigger_source=attempt.trigger_source,
            kind=attempt.kind, query_text=query_text, options=options or {}, provider=provider, mode=mode,
            status=status, reason_code=reason, cache_key=cache_key,
            blocked_by_ref=ref if status == "blocked" and ref else None)
        session.add(row)
        await session.flush()
        refused = status in REFUSED
        await add_usage(session, day=at.date(), provider=provider, agent=attempt.agent, kind=attempt.kind,
                        blocked=int(refused), failed=int(not refused))
        return row.id


async def open_query(attempt: Attempt, *, query_text: str, cache_key: str, options: dict[str, Any], provider: str,
                     mode: str, engines: list[str], flags: list[str], at: datetime | None = None) -> int:
    """The row of a screened search about to be sent (or answered from the cache). Returns its id."""
    at = at or now()
    async with session_scope() as session:
        row = ResearchQuery(
            created_at=at, task_id=attempt.task_id, request_id=attempt.request_id, agent=attempt.agent,
            capability=attempt.capability, requested_by=attempt.requested_by, trigger_source=attempt.trigger_source,
            kind=attempt.kind, query_text=query_text, options=options, provider=provider, mode=mode, status="running",
            cache_key=cache_key, engines=engines, flags=flags)
        session.add(row)
        await session.flush()
        return row.id


async def close_query(query_id: int, *, status: str, reason: str = "", http_status: int | None = None,
                      results: list[dict[str, Any]] | None = None, unresponsive: list[dict[str, Any]] | None = None,
                      provider_version: str = "", cached: bool = False, latency_ms: int | None = None,
                      sources_days: int = 30, at: datetime | None = None) -> list[dict[str, Any]]:
    """Stores the outcome: each page once in research_sources, the query's results in order, the totals.

    Returns the results with their result_id set.
    """
    at = at or now()
    results = [dict(r) for r in results or []]
    expires = at + timedelta(days=sources_days)
    async with session_scope() as session:
        query = await session.get(ResearchQuery, query_id, with_for_update=True)
        if query is None:
            raise LookupError("the research query row is gone")
        for r in results:
            flags = {f: True for f in r["flags"]}
            stmt = pg_insert(ResearchSource).values(
                url=r["url"], url_sha256=url_sha256(r["url"]), domain=r["domain"], title=r["title"],
                published_at=datetime.fromisoformat(r["published_at"]) if r["published_at"] else None,
                first_retrieved_at=at, last_retrieved_at=at, provider=r["source"]["provider"], flags=flags,
                expires_at=expires)
            stmt = stmt.on_conflict_do_update(
                index_elements=[ResearchSource.url_sha256],
                set_={"last_retrieved_at": at, "expires_at": func.greatest(ResearchSource.expires_at, expires)})
            source_id = await session.scalar(stmt.returning(ResearchSource.id))
            row = ResearchResult(query_id=query_id, rank=r["rank"], source_id=source_id, snippet=r["snippet"],
                                 provider_score=r["provider_score"], authority=r["authority"],
                                 engines=r["source"]["engines"], type=r["type"], flags=r["flags"])
            session.add(row)
            await session.flush()
            r["result_id"] = row.id
        query.status, query.reason_code, query.http_status = status, reason, http_status
        query.result_count, query.cached, query.latency_ms = len(results), cached, latency_ms
        query.unresponsive = [[u["engine"], u["reason"]] for u in unresponsive or []]
        query.provider_version = provider_version
        query.finished_at = at
        await add_usage(session, day=query.created_at.date(), provider=query.provider, agent=query.agent,
                        kind=query.kind, cache_hits=int(cached), failed=int(status not in SUCCEEDED))
    return results


async def purge_expired(settings: ResearchSettings, at: datetime | None = None) -> dict[str, int]:
    """Retention (owner decision 14): pages after sources_days unless promoted, attempts after logs_days, daily totals
    after usage_days, expired cache entries, budget slots after two days. For the worker's housekeeping."""
    at = at or now()
    counts: dict[str, int] = {}
    async with session_scope() as session:
        statements = {
            "queries": delete(ResearchQuery).where(ResearchQuery.created_at < at - timedelta(days=settings.logs_days)),
            "sources": delete(ResearchSource).where(ResearchSource.expires_at < at,
                                                    ResearchSource.promoted_item_id.is_(None)),
            "usage": delete(ResearchUsageDaily).where(
                ResearchUsageDaily.day < (at - timedelta(days=settings.usage_days)).date()),
            "cache": delete(ResearchCache).where(ResearchCache.expires_at < at),
            "reservations": delete(ResearchReservation).where(ResearchReservation.created_at < at - timedelta(days=2)),
        }
        for name, stmt in statements.items():
            counts[name] = (await session.execute(stmt)).rowcount or 0
    return counts


PURGED = {"purged": True}  # a research task's result data after its retention ended


async def purge_task_data(before: datetime, capabilities: Iterable[tuple[str, str]]) -> int:
    """Research tasks finished before `before`: their parameters (the query) and result data (the citations) are
    removed; the fixed summary line stays. For the retention purge and an admin's purge. Own transaction."""
    pairs = sorted(capabilities)
    if not pairs:
        return 0
    async with session_scope() as session:
        result = await session.execute(
            update(Task)
            .where(tuple_(Task.agent, Task.capability).in_(pairs), Task.finished_at < before,
                   or_(Task.params != literal({}, JSONB),
                       and_(Task.result.is_not(None), Task.result["data"] != literal(PURGED, JSONB))))
            .values(params={}, result=func.jsonb_set(Task.result, literal_column("'{data}'"), literal(PURGED, JSONB)),
                    updated_at=func.now())
            .execution_options(synchronize_session=False))
        return result.rowcount or 0


# A plan step of one of the research capabilities that still holds parameters or a rationale.
_KEPT_STEP = '$.steps[*] ? (@.capability == $capability && (exists(@.params.*) || @.rationale != ""))'


def _keeps_query(column: Any, prefix: str, capabilities: list[str]) -> Any:
    path = cast(literal(prefix + _KEPT_STEP[1:]), JSONPATH)
    return or_(*(func.jsonb_path_exists(column, path, literal({"capability": c}, JSONB)) for c in capabilities))


async def purge_plan_queries(before: datetime, capabilities: Iterable[tuple[str, str]]) -> int:
    """The other copies of a planned search's query: in Supervisor requests created before `before` and in their
    plan_request tasks' results, each web-search step of the stored plan loses its parameters and rationale (the same
    as a viewer sees it). The request's message, reply and summary stay, as for any request. For the retention purge
    and an admin's purge. Own transaction; returns how many plans changed."""
    return await _hide_plan_queries(capabilities, SupervisorRequest.created_at < before, Task.finished_at < before)


async def hide_plan_queries(request_id: int, capabilities: Iterable[tuple[str, str]]) -> int:
    """The same for one Supervisor request: the gateway refused one of its planned searches when it ran."""
    return await _hide_plan_queries(capabilities, SupervisorRequest.id == request_id, Task.request_id == request_id)


async def _hide_plan_queries(capabilities: Iterable[tuple[str, str]], requests_where: Any, tasks_where: Any) -> int:
    from ..tasks import hide_research_steps  # app.tasks loads the agents, and they load this package

    names = sorted({capability for _, capability in capabilities})
    if not names:
        return 0
    changed = 0
    async with session_scope() as session:
        requests = await session.scalars(select(SupervisorRequest).where(
            requests_where, _keeps_query(SupervisorRequest.plan, "$", names)))
        for request in requests.all():
            plan = hide_research_steps(request.plan)
            if plan != request.plan:
                request.plan, request.updated_at = plan, now()
                changed += 1
        tasks = await session.scalars(select(Task).where(
            Task.agent == "supervisor", Task.capability == "plan_request", tasks_where,
            _keeps_query(Task.result, "$.data.plan", names)))
        for task in tasks.all():
            data = task.result["data"]
            plan = hide_research_steps(data["plan"])
            if plan != data["plan"]:
                task.result, task.updated_at = {**task.result, "data": {**data, "plan": plan}}, now()
                changed += 1
    return changed


async def purge_older(before: datetime, *, include_usage: bool) -> dict[str, int]:
    """An admin's purge (POST /api/research/purge): attempts with their results, pages and cached answers from before
    `before`, and the daily totals when asked. Promoted pages stay. Budget slots stay: today's caps count them."""
    async with session_scope() as session:
        statements = {
            "queries": delete(ResearchQuery).where(ResearchQuery.created_at < before),
            "sources": delete(ResearchSource).where(ResearchSource.last_retrieved_at < before,
                                                    ResearchSource.promoted_item_id.is_(None)),
            "cache": delete(ResearchCache).where(ResearchCache.created_at < before),
        }
        if include_usage:
            statements["usage"] = delete(ResearchUsageDaily).where(ResearchUsageDaily.day < before.date())
        return {name: (await session.execute(stmt)).rowcount or 0 for name, stmt in statements.items()}


async def mark_abandoned(at: datetime | None = None, older_than: timedelta = timedelta(minutes=10)) -> int:
    """Rows left "running" by a worker that stopped mid-search, as failed (code abandoned)."""
    at = at or now()
    async with session_scope() as session:
        result = await session.execute(update(ResearchQuery)
                                       .where(ResearchQuery.status == "running",
                                              ResearchQuery.created_at < at - older_than)
                                       .values(status="failed", reason_code="abandoned", finished_at=at))
        return result.rowcount or 0


async def query_row(query_id: int) -> ResearchQuery | None:
    async with session_scope() as session:
        return await session.scalar(select(ResearchQuery).where(ResearchQuery.id == query_id))
