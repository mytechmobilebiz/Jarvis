"""Creating tasks and moving them through their lifecycle.

This is where the approval policy is enforced: a task's status at creation is
decided here from its capability's declared risk, never by the caller.

    queued ──► running ──► succeeded | failed
       ▲
       │ approve
    awaiting_approval ──reject──► rejected
    (any non-terminal) ──cancel──► cancelled
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import and_, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import events, licensing
from .agents.base import Capability
from .agents.registry import get_agent
from .config import Settings
from .models import AgentState, Approval, SupervisorRequest, Task

TERMINAL = frozenset({"succeeded", "failed", "rejected", "cancelled"})
# Supervisor bookkeeping tasks: they plan and summarise a request rather than
# being part of its work, so they do not count toward its completion.
SUPERVISOR_INTERNAL = frozenset({"plan_request", "summarize_request"})


class TaskError(ValueError):
    pass


def now() -> datetime:
    return datetime.now(UTC)


def resolve_capability(agent_name: str, capability_name: str) -> Capability:
    agent = get_agent(agent_name)
    if agent is None:
        raise TaskError(f"unknown agent '{agent_name}'")
    cap = agent.capabilities().get(capability_name)
    if cap is None:
        raise TaskError(f"agent '{agent_name}' has no capability '{capability_name}'")
    return cap


async def agent_enabled(session: AsyncSession, agent_name: str) -> bool:
    state = await session.get(AgentState, agent_name)
    return True if state is None else state.enabled


def describe_action(agent: str, capability: str, params: dict[str, Any]) -> str:
    shown = ", ".join(f"{k}={v!r}" for k, v in list(params.items())[:4])
    return f"{agent}.{capability}({shown})"


async def create_task(
    session: AsyncSession,
    settings: Settings,
    *,
    agent: str,
    capability: str,
    params: dict[str, Any] | None = None,
    source: str = "api",
    created_by: str = "",
    request_id: int | None = None,
    schedule_id: int | None = None,
    priority: int = 100,
    summary: str = "",
) -> Task:
    cap = resolve_capability(agent, capability)
    try:
        validated = cap.params_model.model_validate(params or {})
    except ValidationError as exc:
        raise TaskError(f"invalid parameters for {agent}.{capability}: {exc.errors(include_url=False)}") from exc
    if not await agent_enabled(session, agent):
        raise TaskError(f"agent '{agent}' is disabled")
    entitlements = await licensing.current_entitlements(session, settings)
    if not entitlements.allows_agent(agent):
        raise TaskError(f"the {agent} agent is not included in the {entitlements.edition.title} edition")

    clean = validated.model_dump(mode="json")
    needs_approval = cap.risk.requires_approval
    task = Task(
        agent=agent,
        capability=capability,
        params=clean,
        risk=cap.risk.value,
        status="awaiting_approval" if needs_approval else "queued",
        source=source,
        created_by=created_by,
        request_id=request_id,
        schedule_id=schedule_id,
        priority=priority,
        summary=summary,
    )
    session.add(task)
    await session.flush()
    if needs_approval:
        extra = await get_agent(agent).approval_context(session, capability, clean)  # type: ignore[union-attr]
        action = summary or describe_action(agent, capability, clean)
        if extra.get("target"):
            action += f" on {extra['target']}" + (" (PRODUCTION)" if extra.get("production") else "")
        session.add(
            Approval(
                task_id=task.id,
                agent=agent,
                capability=capability,
                risk=cap.risk.value,
                summary=action,
                details={"params": clean, "capability_description": cap.description, **extra},
            )
        )
        await events.emit(
            session,
            settings,
            kind="approval.requested",
            severity="warning",
            source=agent,
            title=f"Approval needed ({cap.risk.value}): {action}",
            detail={"task_id": task.id, "agent": agent, "capability": capability},
            task_id=task.id,
        )
    return task


class ConfirmationRequired(TaskError):
    pass


async def decide_approval(
    session: AsyncSession, settings: Settings, approval: Approval, *, approve: bool, actor: str, note: str = "",
    actor_role: str = "", confirm: str = "",
) -> Task:
    if approval.status != "pending":
        raise TaskError(f"approval {approval.id} was already {approval.status}")
    details = approval.details or {}
    if approve and details.get("production"):
        if actor_role != "admin":
            raise ConfirmationRequired("changes to production need an admin's approval")
        if confirm.strip() != str(details.get("confirm", "")):
            raise ConfirmationRequired(f"type '{details.get('confirm', '')}' to confirm this production change")
    task = await session.get(Task, approval.task_id, with_for_update=True)
    assert task is not None
    if task.status != "awaiting_approval":
        raise TaskError(f"task {task.id} is {task.status}, not awaiting approval")
    approval.status = "approved" if approve else "rejected"
    approval.decided_by = actor
    approval.decided_at = now()
    approval.decision_note = note
    task.updated_at = now()
    if approve:
        task.status = "queued"
    else:
        task.status = "rejected"
        task.finished_at = now()
        task.error = f"rejected by {actor}" + (f": {note}" if note else "")
        await on_task_terminal(session, settings, task)
    return task


async def cancel_task(session: AsyncSession, settings: Settings, task: Task, actor: str) -> Task:
    if task.status in TERMINAL or task.status == "running":
        raise TaskError(f"task {task.id} is {task.status} and cannot be cancelled")
    if task.status == "awaiting_approval":
        approval = await session.scalar(select(Approval).where(Approval.task_id == task.id))
        if approval and approval.status == "pending":
            approval.status = "rejected"
            approval.decided_by = actor
            approval.decided_at = now()
            approval.decision_note = "task cancelled"
    task.status = "cancelled"
    task.finished_at = now()
    task.updated_at = now()
    task.error = f"cancelled by {actor}"
    await on_task_terminal(session, settings, task)
    return task


async def retry_task(session: AsyncSession, settings: Settings, task: Task, actor: str) -> Task:
    if task.status not in ("failed", "cancelled"):
        raise TaskError(f"only failed or cancelled tasks can be retried (task {task.id} is {task.status})")
    return await create_task(
        session,
        settings,
        agent=task.agent,
        capability=task.capability,
        params=task.params,
        source="retry",
        created_by=actor,
        request_id=None,
        priority=task.priority,
        summary=task.summary,
    )


async def on_task_terminal(session: AsyncSession, settings: Settings, task: Task) -> None:
    """Called whenever a task reaches a terminal state; advances its supervisor request."""
    if task.request_id is None or (task.agent == "supervisor" and task.capability in SUPERVISOR_INTERNAL):
        return
    await advance_request(session, settings, task.request_id)


async def advance_request(session: AsyncSession, settings: Settings, request_id: int) -> None:
    request = await session.get(SupervisorRequest, request_id, with_for_update=True)
    if request is None or request.status in ("completed", "failed", "planning"):
        return
    work = (
        await session.scalars(
            select(Task).where(
                Task.request_id == request_id,
                not_(and_(Task.agent == "supervisor", Task.capability.in_(SUPERVISOR_INTERNAL))),
            )
        )
    ).all()
    statuses = {t.status for t in work}
    if any(s in ("queued", "running") for s in statuses):
        request.status = "running"
    elif "awaiting_approval" in statuses:
        request.status = "awaiting_approval"
    else:
        already = await session.scalar(
            select(Task.id).where(
                Task.request_id == request_id,
                Task.agent == "supervisor",
                Task.capability == "summarize_request",
            )
        )
        if already is None:
            request.status = "running"
            await create_task(
                session,
                settings,
                agent="supervisor",
                capability="summarize_request",
                params={"request_id": request_id},
                source="supervisor",
                created_by="supervisor",
                request_id=request_id,
                priority=50,
            )
    request.updated_at = now()
