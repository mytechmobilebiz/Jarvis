"""The Research Center's API (plan section 3.15): health, usage, history with citations, sources, blocked attempts,
the research settings, the per-agent policy, the connectivity test and purge.

Admins and operators only: viewers and the n8n service token get 403 on every route (owner decision 13). Settings,
policy, the connectivity test and purge are an admin's; switching research live and purging need a typed
confirmation. The lead or ticket that caused a refusal (blocked_by_ref) is shown only to people with the
sensitive-data permission, who may know who the Credit Repair clients are: for anyone else a lead's and a Credit
Repair client's name read alike (personal_record, no record). No route takes query text in its URL (uvicorn's access log records query strings), and audit entries
hold ids, codes and counts, never text (plan section 3.12).

Nothing here searches: the API is not on the research network. Searches, the connectivity test included, run in the
worker (app/research/gateway.py). Not in this build: DeepSeek, page fetching and promotion to the JARVIS Brain.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, licensing
from .. import tasks as task_service
from ..agents.monitoring import connectivity
from ..agents.registry import all_agents
from ..config import Settings, get_settings
from ..models import (
    AppSetting,
    IntegrationStatus,
    ResearchPolicy,
    ResearchQuery,
    ResearchResult,
    ResearchSource,
    ResearchUsageDaily,
    User,
)
from ..research import budget, health, policy, store
from ..research.gateway import FEATURE
from .deps import Principal, client_ip, get_db, require_admin, require_operator, to_dict
from .work import task_view

router = APIRouter(prefix="/api/research", tags=["research"])

LIVE_CONFIRM = "ENABLE LIVE RESEARCH"
PURGE_CONFIRM = "PURGE RESEARCH"
NOT_IN_BUILD = {
    "deepseek": "DeepSeek: not available in this build (pending review)",
    "fetch": "Page fetching: not available in this build",
}
REFUSED = ("blocked", "refused_policy")
PURGE_NOTE = ("Supervisor requests keep their message, reply and summary, as every request does (operators and admins "
              "see them; viewers do not, for a request with a web search). Container logs and earlier backups are not "
              "purged: recreate the search container to clear its log.")
MAX_RANGE_DAYS = 731
DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}")
# Columns of a query row the Research Center shows (no cache key; SearXNG costs nothing, so no money columns).
QUERY_HIDDEN = ("cache_key", "target_url", "billable", "unit_price_micro", "currency", "cost_micro", "fallback_of")
# Page fetching and promotion are not in this build: no fetched text, no promoted item.
SOURCE_HIDDEN = ("fetched_text", "content_sha256", "truncated", "promoted_item_id")


async def _entitled(session: AsyncSession, settings: Settings) -> bool:
    return (await licensing.current_entitlements(session, settings)).has(FEATURE)


async def _current(session: AsyncSession) -> tuple[policy.ResearchSettings, bool]:
    """(the settings, readable). Unreadable settings refuse every search; they read here as the defaults, mode off."""
    try:
        return await policy.load_settings(session), True
    except policy.SettingsUnreadable:
        return policy.ResearchSettings(), False


def _range(start: date | None, end: date | None, default_days: int = 30) -> tuple[date, date]:
    end = end or datetime.now(UTC).date()
    start = start or end - timedelta(days=default_days - 1)
    if start > end or (end - start).days >= MAX_RANGE_DAYS:
        raise HTTPException(400, f"'from' must be on or before 'to', at most {MAX_RANGE_DAYS} days apart")
    return start, end


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def sees_refs(session: AsyncSession, principal: Principal) -> bool:
    """Whether this person may see which lead or ticket caused a refusal: only with the sensitive-data permission (a
    Credit Repair match never has one, so for anyone else it would tell the stores apart)."""
    user = await session.get(User, principal.user_id) if principal.user_id is not None else None
    return bool(user and user.is_active and user.can_access_sensitive)


def query_view(row: ResearchQuery, *, refs: bool = False) -> dict[str, Any]:
    out = to_dict(row, exclude=QUERY_HIDDEN)
    if not refs:
        out["blocked_by_ref"] = None
    return out


# --- Health and usage ---------------------------------------------------------------------------------------------


def _engines_view(detail: dict[str, Any], now: datetime) -> dict[str, dict[str, Any]]:
    cooling = health.cooling(detail, now)
    out = {}
    for engine in health.ENGINES:
        state = (detail.get("engines") or {}).get(engine)
        state = state if isinstance(state, dict) else {}
        out[engine] = {"state": "suspended" if engine in cooling else state.get("state", "unknown"),
                       "reason": state.get("reason", "") if engine in cooling else "",
                       "until": _iso(cooling.get(engine)), "failures": int(state.get("failures", 0) or 0)}
    return out


@router.get("/health")
async def research_health(_: Principal = Depends(require_operator), session: AsyncSession = Depends(get_db),
                          settings: Settings = Depends(get_settings)) -> dict:
    now = datetime.now(UTC)
    values, readable = await _current(session)
    row = await session.get(IntegrationStatus, health.ROW)
    detail = dict(row.detail or {}) if row else {}
    internet = connectivity.connectivity_view(await session.get(IntegrationStatus, connectivity.CONNECTIVITY_ROW))
    mode = values.mode
    return {
        "mode": mode,
        "settings_readable": readable,
        "entitled": await _entitled(session, settings),
        "searxng": {
            "state": "mock" if mode == "mock" else health.state(detail, mode=mode, internet=internet["state"], now=now),
            "message": health.suspension_message(detail, now) if mode == "live" else "",
            "engines": _engines_view(detail, now),
            "enabled_engines": detail.get("enabled_engines", []),
            "version": detail.get("version", ""),
            "pinned": health.PINNED_TAG,
            "version_ok": detail.get("version_ok"),
            "consecutive_failures": int(detail.get("consecutive_failures", 0) or 0),
            "probed_at": detail.get("probed_at"),
            "last_ok_at": _iso(row.last_ok_at) if row else None,
            "last_error_at": _iso(row.last_error_at) if row else None,
            "last_error": (row.last_error or "") if row else "",  # a code
        },
        "internet": internet["state"],
        "deepseek": {"available": False, "note": NOT_IN_BUILD["deepseek"]},
        "fetch": {"available": False, "note": NOT_IN_BUILD["fetch"]},
    }


@router.get("/usage")
async def usage(principal: Principal = Depends(require_operator), session: AsyncSession = Depends(get_db),
                start: date | None = Query(None, alias="from"), end: date | None = Query(None, alias="to")) -> dict:
    start, end = _range(start, end)
    values, readable = await _current(session)
    provider = "mock" if values.mode == "mock" else "searxng"
    now = datetime.now(UTC)
    used, by_agent, by_person = await budget.counts(provider=provider, at=now)
    rules = {r.agent: r for r in (await session.scalars(select(ResearchPolicy))).all()}
    agents = [{"agent": name, "used": by_agent.get(name, 0),
               "cap": rules[name].search_daily_cap if name in rules else 0,
               "search_enabled": bool(rules[name].search_enabled) if name in rules else False}
              for name in all_agents() if name in rules or by_agent.get(name)]
    days = (await session.scalars(
        select(ResearchUsageDaily).where(ResearchUsageDaily.day >= start, ResearchUsageDaily.day <= end)
        .order_by(ResearchUsageDaily.day.desc(), ResearchUsageDaily.agent, ResearchUsageDaily.provider))).all()
    return {
        "mode": values.mode,
        "settings_readable": readable,
        "provider": provider,
        "today": {"day": now.date().isoformat(),
                  "global": {"used": used, "cap": values.global_daily_cap},
                  "person": {"who": principal.name, "used": by_person.get(principal.name, 0),
                             "cap": values.person_daily_cap},
                  "agents": agents},
        "from": start.isoformat(),
        "to": end.isoformat(),
        "days": [{**to_dict(d, exclude=("billable", "possibly_billable", "cost_micro", "currency")),
                  "day": d.day.isoformat()} for d in days],
    }


# --- History, citations, sources, blocked attempts ----------------------------------------------------------------


def _filtered(q, agent: str | None, status: str | None, provider: str | None, start: date | None, end: date | None):
    if agent:
        q = q.where(ResearchQuery.agent == agent)
    if status:
        q = q.where(ResearchQuery.status.in_(status.split(",")))
    if provider:
        q = q.where(ResearchQuery.provider == provider)
    if start:
        q = q.where(ResearchQuery.created_at >= datetime.combine(start, datetime.min.time(), UTC))
    if end:
        q = q.where(ResearchQuery.created_at < datetime.combine(end + timedelta(days=1), datetime.min.time(), UTC))
    return q


@router.get("/queries")
async def list_queries(
    principal: Principal = Depends(require_operator), session: AsyncSession = Depends(get_db),
    agent: str | None = Query(None, max_length=64), status: str | None = Query(None, max_length=200),
    provider: str | None = Query(None, max_length=40), start: date | None = Query(None, alias="from"),
    end: date | None = Query(None, alias="to"), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
) -> dict:
    q = _filtered(select(ResearchQuery), agent, status, provider, start, end)
    total = await session.scalar(select(func.count()).select_from(q.subquery()))
    rows = (await session.scalars(q.order_by(ResearchQuery.id.desc()).limit(limit).offset(offset))).all()
    refs = await sees_refs(session, principal)
    return {"queries": [query_view(r, refs=refs) for r in rows], "total": total}


@router.get("/queries/{query_id}")
async def get_query(query_id: int, principal: Principal = Depends(require_operator),
                    session: AsyncSession = Depends(get_db)) -> dict:
    row = await session.get(ResearchQuery, query_id)
    if row is None:
        raise HTTPException(404, "no such research query")
    retrieved = _iso(row.finished_at or row.created_at)
    found = (await session.execute(
        select(ResearchResult, ResearchSource)
        .outerjoin(ResearchSource, ResearchSource.id == ResearchResult.source_id)
        .where(ResearchResult.query_id == query_id).order_by(ResearchResult.rank))).all()
    citations = [{
        "citation": f"[{r.rank}]", "rank": r.rank, "result_id": r.id, "source_id": r.source_id,
        "url": src.url if src else None, "domain": src.domain if src else "", "title": src.title if src else "",
        "snippet": r.snippet, "type": r.type, "provider": row.provider, "engines": r.engines,
        "published_at": _iso(src.published_at) if src else None, "retrieved_at": retrieved,
        "provider_score": r.provider_score, "flags": r.flags, "source_purged": src is None,
    } for r, src in found]
    return {"query": query_view(row, refs=await sees_refs(session, principal)), "citations": citations}


@router.get("/sources")
async def list_sources(
    _: Principal = Depends(require_operator), session: AsyncSession = Depends(get_db),
    domain: str | None = Query(None, max_length=253), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
) -> dict:
    """Every distinct page the searches returned (screen 5): when it was first and last seen and how often cited."""
    q = select(ResearchSource)
    if domain:
        q = q.where(ResearchSource.domain == domain.strip().lower().rstrip("."))
    total = await session.scalar(select(func.count()).select_from(q.subquery()))
    cited = (select(ResearchResult.source_id, func.count().label("times"))
             .group_by(ResearchResult.source_id).subquery())
    rows = (await session.execute(
        q.add_columns(func.coalesce(cited.c.times, 0)).outerjoin(cited, cited.c.source_id == ResearchSource.id)
        .order_by(ResearchSource.last_retrieved_at.desc(), ResearchSource.id.desc()).limit(limit).offset(offset))).all()
    return {"sources": [{**to_dict(src, exclude=SOURCE_HIDDEN), "times_cited": times} for src, times in rows],
            "total": total}


@router.get("/sources/{source_id}")
async def get_source(source_id: int, _: Principal = Depends(require_operator),
                     session: AsyncSession = Depends(get_db)) -> dict:
    src = await session.get(ResearchSource, source_id)
    if src is None:
        raise HTTPException(404, "no such research source")
    times = await session.scalar(select(func.count()).select_from(ResearchResult)
                                 .where(ResearchResult.source_id == source_id))
    cited = (await session.execute(
        select(ResearchQuery.id, ResearchQuery.agent, ResearchQuery.created_at, ResearchResult.rank)
        .join(ResearchResult, ResearchResult.query_id == ResearchQuery.id)
        .where(ResearchResult.source_id == source_id).order_by(ResearchQuery.id.desc()).limit(50))).all()
    return {
        "source": to_dict(src, exclude=SOURCE_HIDDEN),
        "times_cited": times,
        "cited_by": [{"query_id": qid, "agent": agent, "created_at": _iso(at), "rank": rank}
                     for qid, agent, at, rank in cited],
    }


@router.get("/blocked")
async def blocked(
    principal: Principal = Depends(require_operator), session: AsyncSession = Depends(get_db),
    agent: str | None = Query(None, max_length=64), start: date | None = Query(None, alias="from"),
    end: date | None = Query(None, alias="to"), limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
) -> dict:
    """Refused attempts: when, which agent, the reason code and (for people with the sensitive-data permission) the
    lead or ticket that caused it. They hold no text."""
    q = _filtered(select(ResearchQuery).where(ResearchQuery.status.in_(REFUSED)), agent, None, None, start, end)
    total = await session.scalar(select(func.count()).select_from(q.subquery()))
    rows = (await session.scalars(q.order_by(ResearchQuery.id.desc()).limit(limit).offset(offset))).all()
    refs = await sees_refs(session, principal)
    return {"blocked": [{"id": r.id, "created_at": _iso(r.created_at), "agent": r.agent, "capability": r.capability,
                         "requested_by": r.requested_by, "trigger_source": r.trigger_source, "status": r.status,
                         "reason_code": r.reason_code, "blocked_by_ref": r.blocked_by_ref if refs else None,
                         "task_id": r.task_id, "request_id": r.request_id} for r in rows],
            "total": total}


# --- Settings (admin) ---------------------------------------------------------------------------------------------


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["off", "mock", "live"] | None = None
    confirm: str = Field("", max_length=100)  # LIVE_CONFIRM, typed, to switch live research on
    global_daily_cap: int | None = Field(None, ge=0, le=policy.MAX_CAP)
    person_daily_cap: int | None = Field(None, ge=0, le=policy.MAX_CAP)
    sources_days: int | None = Field(None, ge=1, le=3650)
    logs_days: int | None = Field(None, ge=1, le=3650)
    usage_days: int | None = Field(None, ge=1, le=3650)
    owner_terms: list[str] | None = Field(None, max_length=policy.MAX_OWNER_TERMS)

    @field_validator("owner_terms")
    @classmethod
    def _terms(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        terms = [" ".join(t.split()) for t in value]
        if not all(0 < len(t) <= 200 for t in terms):
            raise ValueError("each business name is 1-200 characters")
        return list(dict.fromkeys(terms))


SETTING_FIELDS = ("global_daily_cap", "person_daily_cap", "sources_days", "logs_days", "usage_days", "owner_terms")


def settings_view(values: policy.ResearchSettings, readable: bool, entitled: bool) -> dict[str, Any]:
    return {
        "mode": values.mode, "readable": readable, "entitled": entitled,
        **{name: getattr(values, name) for name in SETTING_FIELDS}, "owner_terms": list(values.owner_terms),
        "live_confirm": LIVE_CONFIRM, "provider": "searxng", "engines": list(health.ENGINES),
        "deepseek": NOT_IN_BUILD["deepseek"], "fetch": NOT_IN_BUILD["fetch"],
    }


async def _write(session: AsyncSession, key: str, value: Any, actor: str) -> None:
    row = await session.get(AppSetting, key, with_for_update=True)
    if row is None:
        session.add(AppSetting(key=key, value=value, updated_by=actor))
    else:
        row.value, row.updated_by, row.updated_at = value, actor, datetime.now(UTC)


@router.get("/settings")
async def get_research_settings(_: Principal = Depends(require_admin), session: AsyncSession = Depends(get_db),
                                settings: Settings = Depends(get_settings)) -> dict:
    values, readable = await _current(session)
    return {"settings": settings_view(values, readable, await _entitled(session, settings))}


@router.patch("/settings")
async def patch_research_settings(body: SettingsPatch, request: Request, principal: Principal = Depends(require_admin),
                                  session: AsyncSession = Depends(get_db),
                                  settings: Settings = Depends(get_settings)) -> dict:
    values, readable = await _current(session)
    changed = body.model_dump(exclude_none=True, exclude={"confirm", "mode"})
    if body.mode is None and not changed:
        raise HTTPException(400, "nothing to change")
    entitled = await _entitled(session, settings)
    if body.mode in ("mock", "live") and not entitled:
        raise HTTPException(403, "web research is not included in this edition")
    if body.mode == "live" and values.mode != "live" and body.confirm.strip() != LIVE_CONFIRM:
        raise HTTPException(400, f"type '{LIVE_CONFIRM}' to switch live web research on: searches then go to "
                            "DuckDuckGo, Brave and Wikipedia from this PC's internet address")
    if changed or not readable:
        stored = {name: getattr(values, name) for name in SETTING_FIELDS}
        stored["owner_terms"] = list(values.owner_terms)
        stored.update(changed)
        await _write(session, policy.SETTINGS_KEY, stored, principal.name)
    if changed:
        detail: dict[str, Any] = {k: v for k, v in changed.items() if k != "owner_terms"}
        if "owner_terms" in changed:
            detail["owner_terms"] = len(changed["owner_terms"])  # a count: the names are text
        await audit.record(session, principal.name, "research.settings_changed", "research", "settings",
                           {"fields": sorted(changed), **detail}, ip=client_ip(request))
    if body.mode is not None and (body.mode != values.mode or not readable):
        await _write(session, policy.MODE_KEY, body.mode, principal.name)
        await audit.record(session, principal.name, "research.mode_changed", "research", "mode",
                           {"from": values.mode if readable else "unreadable", "to": body.mode}, ip=client_ip(request))
    await session.commit()
    values, readable = await _current(session)
    return {"settings": settings_view(values, readable, entitled)}


# --- Per-agent policy (admin) -------------------------------------------------------------------------------------


class PolicyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search_enabled: bool | None = None
    search_daily_cap: int | None = Field(None, ge=0, le=policy.MAX_CAP)
    data_classes: list[Literal["G", "S", "O", "C"]] | None = Field(None, max_length=4)
    blocked_domains: list[str] | None = Field(None, max_length=100)

    @field_validator("blocked_domains")
    @classmethod
    def _domains(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        domains = [d.strip().lower().rstrip(".") for d in value]
        if not all(len(d) <= 253 and DOMAIN.fullmatch(d) for d in domains):
            raise ValueError("each blocked domain is a plain domain name, e.g. example.com")
        return list(dict.fromkeys(domains))


def policy_view(name: str, row: ResearchPolicy | None) -> dict[str, Any]:
    agent = all_agents()[name]
    return {
        "agent": name, "title": agent.title, "exists": row is not None,
        "never": name == policy.SENSITIVE_AGENT,  # Credit Repair: refused in code, whatever its row says
        "search_enabled": bool(row.search_enabled) if row else False,
        "search_daily_cap": row.search_daily_cap if row else 0,
        "data_classes": list(row.data_classes or []) if row else [],
        "blocked_domains": list(row.blocked_domains or []) if row else [],
        "updated_by": row.updated_by if row else "", "updated_at": _iso(row.updated_at) if row else None,
        "capabilities": [c.name for c in agent.capabilities().values() if c.research is not None
                         and not task_service.is_internal(name, c.name)],
    }


def _agent(name: str) -> str:
    if name not in all_agents():
        raise HTTPException(404, "unknown agent")
    return name


@router.get("/policy")
async def list_policies(_: Principal = Depends(require_admin), session: AsyncSession = Depends(get_db)) -> dict:
    rows = {r.agent: r for r in (await session.scalars(select(ResearchPolicy))).all()}
    return {"policies": [policy_view(name, rows.get(name)) for name in all_agents()]}


@router.get("/policy/{agent}")
async def get_policy(agent: str, _: Principal = Depends(require_admin), session: AsyncSession = Depends(get_db)) -> dict:
    return {"policy": policy_view(_agent(agent), await session.get(ResearchPolicy, agent))}


@router.patch("/policy/{agent}")
async def patch_policy(agent: str, body: PolicyPatch, request: Request, principal: Principal = Depends(require_admin),
                       session: AsyncSession = Depends(get_db)) -> dict:
    _agent(agent)
    if agent == policy.SENSITIVE_AGENT:
        raise HTTPException(400, "Credit Repair never searches the web (enforced in code)")
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(400, "nothing to change")
    row = await session.get(ResearchPolicy, agent, with_for_update=True)
    if row is None:
        row = ResearchPolicy(agent=agent, search_enabled=False, search_daily_cap=0, data_classes=[], blocked_domains=[])
        session.add(row)
    for key, value in changes.items():
        setattr(row, key, sorted(set(value)) if key == "data_classes" else value)
    row.updated_by, row.updated_at = principal.name, datetime.now(UTC)
    detail: dict[str, Any] = {"agent": agent, "fields": sorted(changes)}
    detail.update({k: changes[k] for k in ("search_enabled", "search_daily_cap", "data_classes") if k in changes})
    if "blocked_domains" in changes:
        detail["blocked_domains"] = len(changes["blocked_domains"])  # a count: the domains are text
    await audit.record(session, principal.name, "research.policy_changed", "research_policy", agent, detail,
                       ip=client_ip(request))
    await session.commit()
    return {"policy": policy_view(agent, await session.get(ResearchPolicy, agent))}


# --- Connectivity test and purge (admin) --------------------------------------------------------------------------


@router.post("/test", status_code=201)
async def connectivity_test(request: Request, principal: Principal = Depends(require_admin),
                            session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    """Queues the internal connectivity-test task: one fixed search, run by the worker like any other (screened,
    counted, logged). The only way to start it; direct runs, both planners and retries refuse it."""
    values, readable = await _current(session)
    if not readable:
        raise HTTPException(400, "the research settings cannot be read: save them again first")
    if values.mode == "off":
        raise HTTPException(400, "web research is switched off: switch it to mock or live first")
    if not await _entitled(session, settings):
        raise HTTPException(403, "web research is not included in this edition")
    agent, capability = task_service.RESEARCH_TEST
    task = await task_service.create_task(session, settings, agent=agent, capability=capability, params={},
                                          source=task_service.RESEARCH_TEST_SOURCE, created_by=principal.name,
                                          priority=20)
    await audit.record(session, principal.name, "research.test_requested", "task", task.id,
                       {"task_id": task.id, "mode": values.mode}, ip=client_ip(request))
    await session.commit()
    return {"task": task_view(task)}


class PurgeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    older_than_days: int = Field(ge=0, le=3650)
    include_usage: bool = False
    confirm: str = Field("", max_length=100)  # PURGE_CONFIRM, typed


@router.post("/purge")
async def purge(body: PurgeBody, request: Request, principal: Principal = Depends(require_admin),
                session: AsyncSession = Depends(get_db)) -> dict:
    """Removes research records older than the given days: attempts and their results, pages, cached answers, the
    parameters and citations of research tasks, the query and rationale of web-search steps in stored Supervisor
    plans, and the daily totals when asked. Supervisor requests keep their message, reply and summary, as every request
    does (nothing purges requests); container logs and earlier backups keep what they hold."""
    if body.confirm.strip() != PURGE_CONFIRM:
        raise HTTPException(400, f"type '{PURGE_CONFIRM}' to purge research records")
    before = datetime.now(UTC) - timedelta(days=body.older_than_days)
    counts = await store.purge_older(before, include_usage=body.include_usage)
    counts["tasks"] = await store.purge_task_data(before, task_service.research_capabilities())
    counts["plans"] = await store.purge_plan_queries(before, task_service.research_capabilities())
    await audit.record(session, principal.name, "research.purged", "research", "records",
                       {"older_than_days": body.older_than_days, "include_usage": body.include_usage, **counts},
                       ip=client_ip(request))
    await session.commit()
    return {"purged": counts,
            "note": PURGE_NOTE}
