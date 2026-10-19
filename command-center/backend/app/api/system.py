"""Health, platform status, events, artifacts, schedules and the audit log."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __version__, audit, customer_settings, licensing
from ..agents.monitoring import connectivity
from ..config import Settings, get_settings
from ..models import (
    Approval,
    Artifact,
    AuditLog,
    Event,
    IntegrationStatus,
    LLMUsage,
    Schedule,
    Site,
    Task,
    WorkerHeartbeat,
)
from ..worker.scheduler import next_fire, valid_cron
from .deps import Principal, client_ip, get_db, require_admin, require_operator, require_user, to_dict

router = APIRouter(prefix="/api", tags=["system"])


class ArtifactPatch(BaseModel):
    status: str = Field(pattern="^(draft|approved|rejected|archived)$")


class SchedulePatch(BaseModel):
    enabled: bool | None = None
    cron: str | None = Field(None, max_length=100)


@router.get("/health")
async def health(session: AsyncSession = Depends(get_db)) -> dict:
    """Unauthenticated liveness probe for Docker; reveals nothing beyond up/down."""
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}


@router.get("/system/status")
async def system_status(
    _: Principal = Depends(require_user), session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)
) -> dict:
    now = datetime.now(UTC)
    workers = (await session.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_seen_at.desc()))).all()
    site_counts = dict((await session.execute(select(Site.last_status, func.count()).group_by(Site.last_status))).all())
    # The same by status for the sites being monitored, and paused ones apart: a paused site's last status is not current.
    enabled_counts = dict((await session.execute(
        select(Site.last_status, func.count()).where(Site.enabled.is_(True)).group_by(Site.last_status))).all())
    paused = await session.scalar(select(func.count()).select_from(Site).where(Site.enabled.is_(False)))
    task_counts = dict(
        (
            await session.execute(
                select(Task.status, func.count()).where(Task.created_at >= now - timedelta(hours=24)).group_by(Task.status)
            )
        ).all()
    )
    queued = await session.scalar(select(func.count()).select_from(Task).where(Task.status == "queued"))
    running = await session.scalar(select(func.count()).select_from(Task).where(Task.status == "running"))
    pending = await session.scalar(select(func.count()).select_from(Approval).where(Approval.status == "pending"))
    event_counts = dict(
        (
            await session.execute(
                select(Event.severity, func.count()).where(Event.at >= now - timedelta(hours=24)).group_by(Event.severity)
            )
        ).all()
    )
    drafts = await session.scalar(select(func.count()).select_from(Artifact).where(Artifact.status == "draft"))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    llm_today = await session.scalar(select(func.count()).select_from(LLMUsage).where(LLMUsage.at >= start))
    # When the model last answered or failed, so "available" can mean verified rather than merely configured.
    llm_last_ok = await session.scalar(select(func.max(LLMUsage.at)).where(LLMUsage.ok.is_(True)))
    llm_last_error = (
        await session.execute(select(LLMUsage.at, LLMUsage.error).where(LLMUsage.ok.is_(False)).order_by(LLMUsage.at.desc()).limit(1))
    ).first()
    statuses = {r.name: r for r in (await session.scalars(select(IntegrationStatus))).all()}
    integrations = {name: to_dict(r) for name, r in statuses.items()}
    ai, _key = await customer_settings.resolve_ai(session, settings)
    ent = await licensing.current_entitlements(session, settings)
    return {
        "app": settings.app_name,
        "version": __version__,
        "environment": settings.environment,
        "time": now.isoformat(),
        "database": {"ok": True},
        "workers": [
            {**to_dict(w), "alive": now - w.last_seen_at < timedelta(minutes=2)} for w in workers[:10]
        ],
        "llm": {
            "configured": ai.provider == "anthropic",
            "provider": "anthropic" if ai.provider == "anthropic" else "rule-based",
            "model": ai.model if ai.provider == "anthropic" else None,
            "requests_today": llm_today,
            "daily_cap": ai.daily_cap,
            "reason": ai.reason,
            "last_ok_at": llm_last_ok.isoformat() if llm_last_ok else None,
            "last_error_at": llm_last_error[0].isoformat() if llm_last_error else None,
            "last_error": (llm_last_error[1] or "")[:300] if llm_last_error else "",
        },
        "workflow_engine": settings.workflow_engine,
        "edition": {"code": ent.edition.code, "title": ent.edition.title, "source": ent.source},
        "n8n": integrations.get(settings.workflow_engine),
        "n8n_editor_url": settings.n8n_editor_url
        if settings.workflow_engine == "n8n" and settings.n8n_editor_url.startswith(("http://", "https://"))
        else None,
        # This PC's own internet connection, as the website monitoring last found it (agents/monitoring/connectivity.py).
        "connectivity": connectivity.connectivity_view(statuses.get(connectivity.CONNECTIVITY_ROW)),
        "counts": {
            "sites": site_counts,
            "sites_enabled": enabled_counts,
            "sites_paused": paused or 0,
            "tasks_24h": task_counts,
            "tasks_queued": queued,
            "tasks_running": running,
            "approvals_pending": pending,
            "events_24h": event_counts,
            "drafts": drafts,
        },
    }


@router.get("/events")
async def list_events(
    _: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_db),
    severity: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    q = select(Event)
    if severity:
        q = q.where(Event.severity.in_(severity.split(",")))
    rows = (await session.scalars(q.order_by(Event.id.desc()).limit(limit))).all()
    return {"events": [to_dict(e) for e in rows]}


@router.get("/events/{event_id}")
async def get_event(event_id: int, _: Principal = Depends(require_user), session: AsyncSession = Depends(get_db)) -> dict:
    event = await session.get(Event, event_id)
    if event is None:
        raise HTTPException(404, "no such event")
    return {"event": to_dict(event)}


@router.get("/artifacts")
async def list_artifacts(
    _: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_db),
    kind: str | None = None,
    agent: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    q = select(Artifact)
    if kind:
        q = q.where(Artifact.kind == kind)
    if agent:
        q = q.where(Artifact.agent == agent)
    if status:
        q = q.where(Artifact.status == status)
    rows = (await session.scalars(q.order_by(Artifact.id.desc()).limit(limit))).all()
    return {"artifacts": [to_dict(a, exclude=("content",)) for a in rows]}


@router.get("/artifacts/{artifact_id}")
async def get_artifact(artifact_id: int, _: Principal = Depends(require_user), session: AsyncSession = Depends(get_db)) -> dict:
    artifact = await session.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(404, "no such artifact")
    return {"artifact": to_dict(artifact)}


@router.patch("/artifacts/{artifact_id}")
async def patch_artifact(
    artifact_id: int, body: ArtifactPatch, request: Request,
    principal: Principal = Depends(require_operator), session: AsyncSession = Depends(get_db),
) -> dict:
    """Marks a draft as reviewed. Approving a draft here does not publish or send it."""
    artifact = await session.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(404, "no such artifact")
    artifact.status = body.status
    artifact.updated_at = datetime.now(UTC)
    await audit.record(session, principal.name, f"artifact.{body.status}", "artifact", artifact.id, ip=client_ip(request))
    await session.commit()
    return {"artifact": to_dict(artifact, exclude=("content",))}


@router.get("/schedules")
async def list_schedules(_: Principal = Depends(require_user), session: AsyncSession = Depends(get_db)) -> dict:
    rows = (await session.scalars(select(Schedule).order_by(Schedule.name))).all()
    return {"schedules": [to_dict(s) for s in rows]}


@router.patch("/schedules/{schedule_id}")
async def patch_schedule(
    schedule_id: int, body: SchedulePatch, request: Request,
    principal: Principal = Depends(require_operator), session: AsyncSession = Depends(get_db),
) -> dict:
    schedule = await session.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(404, "no such schedule")
    changes = body.model_dump(exclude_none=True)
    if body.cron is not None:
        if not valid_cron(body.cron):
            raise HTTPException(400, f"invalid cron expression: {body.cron!r}")
        schedule.cron = body.cron
        schedule.next_run_at = next_fire(body.cron, datetime.now(UTC))
    if body.enabled is not None:
        schedule.enabled = body.enabled
        if body.enabled:
            schedule.next_run_at = next_fire(schedule.cron, datetime.now(UTC))
    schedule.updated_at = datetime.now(UTC)
    await audit.record(session, principal.name, "schedule.updated", "schedule", schedule.id, changes, ip=client_ip(request))
    await session.commit()
    return {"schedule": to_dict(schedule)}


@router.get("/audit")
async def audit_log(
    _: Principal = Depends(require_admin), session: AsyncSession = Depends(get_db), limit: int = Query(100, ge=1, le=1000)
) -> dict:
    rows = (await session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))).all()
    return {"entries": [to_dict(r) for r in rows]}
