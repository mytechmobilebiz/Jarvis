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
from ..brain import quotes
from ..config import Settings, get_settings
from ..models import AgentState, Approval, Task, TaskLog, User
from .deps import OPERATOR_ROLES, Principal, client_ip, get_db, require_operator, require_user, to_dict

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


# Parts of a task result that only operators and admins see: answers quoted from JARVIS's history (supervisor.recall)
# and reference notes from memory (titles of memory items a viewer may not be allowed to read).
OPERATOR_ONLY_RESULT_DATA = ("answer", "memory_notes")


def task_view(task: Task, principal: Principal | None = None) -> dict[str, Any]:
    """A task as the API returns it. For a viewer (any role that does not operate), the operator-only parts of the
    result are left out; callers that already require an operator pass no principal. Routes serve tasks through
    task_views(), which also leaves out quotes of removed JARVIS Brain sources."""
    out = to_dict(task)
    if principal is not None and principal.role not in OPERATOR_ROLES:
        result = out.get("result")
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, dict) and any(key in data for key in OPERATOR_ONLY_RESULT_DATA):
            out["result"] = {**result, "data": {k: v for k, v in data.items() if k not in OPERATOR_ONLY_RESULT_DATA}}
    return out


async def task_views(session: AsyncSession, tasks: list[Task], principal: Principal | None = None
                     ) -> list[dict[str, Any]]:
    """task_view() of each task, without the answer lines that quote a withheld, deleted or emptied JARVIS Brain
    source and without reference notes naming such an item (quotes.dead_sources, one lookup per page), and with each
    quote of a conversation whose title an admin hid naming it without that title (quotes.hidden_titles): the
    read-time guard, for quotes a start-up check has not changed yet (skipped while a task held the quote lock, or a
    database 0.6.0 left behind). The stored result is not changed."""
    datas = [d for t in tasks if isinstance(d := (t.result or {}).get("data"), dict)]
    dead_m, dead_i = await quotes.dead_sources(session, datas) if datas else (set(), set())
    quoted = {s["message"] for d in datas for s in d.get("sources") or []
              if isinstance(s, dict) and isinstance(s.get("message"), int)}
    hidden = await quotes.hidden_titles(session, quoted) if quoted else {}
    out = []
    for t in tasks:
        view = task_view(t, principal)
        result = view.get("result")
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, dict) and (dead_m or dead_i or hidden):
            view["result"] = {**result, "data": quotes.strip_dead(data, dead_m, dead_i, hidden)}
        out.append(view)
    return out


async def task_one(session: AsyncSession, task: Task, principal: Principal | None = None) -> dict[str, Any]:
    return (await task_views(session, [task], principal))[0]


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
    # Work in progress right now, however old the task (stats_24h only covers tasks created in the last 24 h).
    active: dict[str, dict[str, int]] = {}
    for agent, status, n in (
        await session.execute(
            select(Task.agent, Task.status, func.count())
            .where(Task.status.in_(("queued", "running", "awaiting_approval")))
            .group_by(Task.agent, Task.status)
        )
    ).all():
        active.setdefault(agent, {})[status] = n
    ent = await licensing.current_entitlements(session, settings)
    out = []
    for name, agent in all_agents().items():
        d = agent.describe()
        state = states.get(name)
        d["enabled"] = True if state is None else state.enabled
        d["entitled"] = ent.allows_agent(name)
        d["stats_24h"] = stats.get(name, {})
        d["active"] = {k: active.get(name, {}).get(k, 0) for k in ("queued", "running", "awaiting_approval")}
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
    settings: Settings = Depends(get_settings),
) -> dict:
    if get_agent(name) is None:
        raise HTTPException(404, "unknown agent")
    if name == "supervisor" and not body.enabled:
        raise HTTPException(400, "the supervisor cannot be disabled")
    state = await session.get(AgentState, name, with_for_update=True)
    if state is None:
        state = AgentState(name=name)
        session.add(state)
    state.enabled = body.enabled
    state.updated_by = principal.name
    state.updated_at = datetime.now(UTC)
    cancelled: list[int] = []
    if not body.enabled:
        # Switching an agent off also withdraws its changes that have not started, approved or not (tasks already
        # running finish; Servers -> Disable stops every SSH session at once).
        cancelled = await task_service.cancel_pending_changes(session, settings, name, principal.name, "the agent was switched off")
        for task_id in cancelled:
            await audit.record(session, principal.name, "task.cancelled", "task", task_id, {"reason": "agent switched off"},
                               ip=client_ip(request))
    await audit.record(session, principal.name, "agent.enabled" if body.enabled else "agent.disabled", "agent", name,
                       {"cancelled_tasks": cancelled} if cancelled else None, ip=client_ip(request))
    await session.commit()
    return {"name": name, "enabled": body.enabled, "cancelled_tasks": cancelled}


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
    return {"task": await task_one(session, task)}


@router.get("/tasks")
async def list_tasks(
    principal: Principal = Depends(require_user),
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
    return {"tasks": await task_views(session, list(rows), principal), "total": total}


@router.get("/tasks/{task_id}")
async def get_task(task_id: int, principal: Principal = Depends(require_user), session: AsyncSession = Depends(get_db)) -> dict:
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "no such task")
    logs = (await session.scalars(select(TaskLog).where(TaskLog.task_id == task_id).order_by(TaskLog.id))).all()
    approval = await session.scalar(select(Approval).where(Approval.task_id == task_id))
    return {
        "task": await task_one(session, task, principal),
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
    task = await _locked_task(session, task_id)
    await require_sensitive_access(session, principal, task.agent)
    task = await task_service.cancel_task(session, settings, task, principal.name)
    await audit.record(session, principal.name, "task.cancelled", "task", task_id, ip=client_ip(request))
    await session.commit()
    return {"task": await task_one(session, task)}


@router.post("/tasks/{task_id}/retry", status_code=201)
async def retry(
    task_id: int,
    request: Request,
    principal: Principal = Depends(require_operator),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    task = await _locked_task(session, task_id)
    await require_sensitive_access(session, principal, task.agent)
    task = await task_service.retry_task(session, settings, task, principal.name)
    await audit.record(session, principal.name, "task.retried", "task", task_id, {"new_task_id": task.id}, ip=client_ip(request))
    await session.commit()
    return {"task": await task_one(session, task)}


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
    return {"approval": to_dict(approval), "task": await task_one(session, task)}
