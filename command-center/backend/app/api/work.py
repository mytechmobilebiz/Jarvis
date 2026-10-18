"""Agents, tasks and approvals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, licensing
from .. import tasks as task_service
from ..agents.registry import all_agents, get_agent
from ..config import Settings, get_settings
from ..models import AgentState, Approval, Task, TaskLog, User
from .deps import Principal, client_ip, get_db, require_operator, require_user, to_dict

router = APIRouter(prefix="/api", tags=["work"])


class AgentPatch(BaseModel):
    enabled: bool


class RunBody(BaseModel):
    capability: str
    params: dict[str, Any] = Field(default_factory=dict)


class DecisionBody(BaseModel):
    approve: bool
    note: str = Field("", max_length=2000)
    # For production changes: the target's name, typed by an admin.
    confirm: str = Field("", max_length=200)


async def require_sensitive_access(session: AsyncSession, principal: Principal, agent_name: str) -> None:
    """Sensitive agents (Credit Repair) need the per-user sensitive-data permission."""
    agent = get_agent(agent_name)
    if agent is None or not agent.sensitive:
        return
    user = await session.get(User, principal.user_id) if principal.user_id else None
    if user is None or not user.can_access_sensitive:
        raise HTTPException(403, "this agent handles protected personal data; your account needs the sensitive-data permission")


def task_view(task: Task) -> dict[str, Any]:
    return to_dict(task)


@router.get("/agents")
async def list_agents(
    _: Principal = Depends(require_user), session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)
) -> dict:
    states = {s.name: s for s in (await session.scalars(select(AgentState))).all()}
    since = datetime.now(UTC) - timedelta(hours=24)
    stats: dict[str, dict[str, int]] = {}
    for agent, status, n in (
        await session.execute(
            select(Task.agent, Task.status, func.count()).where(Task.created_at >= since).group_by(Task.agent, Task.status)
        )
    ).all():
        stats.setdefault(agent, {})[status] = n
    last_runs = dict(
        (await session.execute(select(Task.agent, func.max(Task.finished_at)).group_by(Task.agent))).all()
    )
    ent = await licensing.current_entitlements(session, settings)
    out = []
    for name, agent in all_agents().items():
        d = agent.describe()
        state = states.get(name)
        d["enabled"] = True if state is None else state.enabled
        d["entitled"] = ent.allows_agent(name)
        d["stats_24h"] = stats.get(name, {})
        last = last_runs.get(name)
        d["last_run_at"] = last.isoformat() if last else None
        out.append(d)
    return {"agents": out}


@router.patch("/agents/{name}")
async def patch_agent(
    name: str,
    body: AgentPatch,
    request: Request,
    principal: Principal = Depends(require_operator),
    session: AsyncSession = Depends(get_db),
) -> dict:
    if get_agent(name) is None:
        raise HTTPException(404, "unknown agent")
    if name == "supervisor" and not body.enabled:
        raise HTTPException(400, "the supervisor cannot be disabled")
    state = await session.get(AgentState, name)
    if state is None:
        state = AgentState(name=name)
        session.add(state)
    state.enabled = body.enabled
    state.updated_by = principal.name
    state.updated_at = datetime.now(UTC)
    await audit.record(session, principal.name, "agent.enabled" if body.enabled else "agent.disabled", "agent", name, ip=client_ip(request))
    await session.commit()
    return {"name": name, "enabled": body.enabled}


@router.post("/agents/{name}/tasks", status_code=201)
async def run_capability(
    name: str,
    body: RunBody,
    request: Request,
    principal: Principal = Depends(require_operator),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    if name == "supervisor" and body.capability in task_service.SUPERVISOR_INTERNAL:
        raise HTTPException(400, "internal supervisor capability")
    await require_sensitive_access(session, principal, name)
    task = await task_service.create_task(
        session,
        settings,
        agent=name,
        capability=body.capability,
        params=body.params,
        source="dashboard",
        created_by=principal.name,
    )
    await audit.record(
        session, principal.name, "task.created", "task", task.id,
        {"agent": name, "capability": body.capability, "status": task.status}, ip=client_ip(request),
    )
    await session.commit()
    return {"task": task_view(task)}


@router.get("/tasks")
async def list_tasks(
    _: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_db),
    status: str | None = None,
    agent: str | None = None,
    request_id: int | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    q = select(Task)
    if status:
        q = q.where(Task.status.in_(status.split(",")))
    if agent:
        q = q.where(Task.agent == agent)
    if request_id is not None:
        q = q.where(Task.request_id == request_id)
    total = await session.scalar(select(func.count()).select_from(q.subquery()))
    rows = (await session.scalars(q.order_by(Task.id.desc()).limit(limit).offset(offset))).all()
    return {"tasks": [task_view(t) for t in rows], "total": total}


@router.get("/tasks/{task_id}")
async def get_task(task_id: int, _: Principal = Depends(require_user), session: AsyncSession = Depends(get_db)) -> dict:
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "no such task")
    logs = (await session.scalars(select(TaskLog).where(TaskLog.task_id == task_id).order_by(TaskLog.id))).all()
    approval = await session.scalar(select(Approval).where(Approval.task_id == task_id))
    return {
        "task": task_view(task),
        "logs": [to_dict(entry) for entry in logs],
        "approval": to_dict(approval) if approval else None,
    }


async def _locked_task(session: AsyncSession, task_id: int) -> Task:
    task = await session.get(Task, task_id, with_for_update=True)
    if task is None:
        raise HTTPException(404, "no such task")
    return task


@router.post("/tasks/{task_id}/cancel")
async def cancel(
    task_id: int,
    request: Request,
    principal: Principal = Depends(require_operator),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    task = await task_service.cancel_task(session, settings, await _locked_task(session, task_id), principal.name)
    await audit.record(session, principal.name, "task.cancelled", "task", task_id, ip=client_ip(request))
    await session.commit()
    return {"task": task_view(task)}


@router.post("/tasks/{task_id}/retry", status_code=201)
async def retry(
    task_id: int,
    request: Request,
    principal: Principal = Depends(require_operator),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    task = await task_service.retry_task(session, settings, await _locked_task(session, task_id), principal.name)
    await audit.record(session, principal.name, "task.retried", "task", task_id, {"new_task_id": task.id}, ip=client_ip(request))
    await session.commit()
    return {"task": task_view(task)}


@router.get("/approvals")
async def list_approvals(
    _: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_db),
    status: str | None = "pending",
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    q = select(Approval)
    if status and status != "all":
        q = q.where(Approval.status == status)
    rows = (await session.scalars(q.order_by(Approval.id.desc()).limit(limit))).all()
    return {"approvals": [to_dict(a) for a in rows]}


@router.post("/approvals/{approval_id}/decision")
async def decide(
    approval_id: int,
    body: DecisionBody,
    request: Request,
    principal: Principal = Depends(require_operator),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    approval = await session.get(Approval, approval_id, with_for_update=True)
    if approval is None:
        raise HTTPException(404, "no such approval")
    await require_sensitive_access(session, principal, approval.agent)
    task = await task_service.decide_approval(
        session, settings, approval, approve=body.approve, actor=principal.name, note=body.note,
        actor_role=principal.role, confirm=body.confirm,
    )
    await audit.record(
        session, principal.name, "approval.approved" if body.approve else "approval.rejected", "approval", approval_id,
        {"task_id": task.id, "agent": task.agent, "capability": task.capability, "note": body.note}, ip=client_ip(request),
    )
    await session.commit()
    return {"approval": to_dict(approval), "task": task_view(task)}
