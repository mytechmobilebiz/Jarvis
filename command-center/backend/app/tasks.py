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

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import and_, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import events, licensing
from .agents.base import Capability, Params
from .agents.registry import all_agents, get_agent
from .config import Settings
from .models import AgentState, Approval, Schedule, SupervisorRequest, Task

if TYPE_CHECKING:
    from .agents.base import AgentContext, BaseAgent

TERMINAL = frozenset({"succeeded", "failed", "rejected", "cancelled"})
# Supervisor bookkeeping tasks: they plan and summarise a request rather than
# being part of its work, so they do not count toward its completion.
SUPERVISOR_INTERNAL = frozenset({"plan_request", "summarize_request"})
# The Research Gateway's connectivity test (app/research): only an admin queues it, from POST /api/research/test,
# as a task with this source. Direct runs, both planners and retries are refused (create_task checks the source).
RESEARCH_TEST = ("supervisor", "research_connectivity_test")
RESEARCH_TEST_SOURCE = "research_test"
# Capabilities nobody runs directly and no planner offers: the Supervisor's own bookkeeping and the connectivity test.
INTERNAL = frozenset({*(("supervisor", c) for c in SUPERVISOR_INTERNAL), RESEARCH_TEST})
# The phrases on which the Supervisor's rules plan a web search (app/supervisor/agent.py): a request using one asked
# for a search, whatever was planned.
WEB_SEARCH_PHRASE = r"\b(?:search\s+the\s+web\s+for|web\s+search\s*:|research\s+the\s+web\s+for)"
WEB_SEARCH_ASKED = re.compile(WEB_SEARCH_PHRASE, re.IGNORECASE)


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


def is_internal(agent_name: str, capability_name: str) -> bool:
    return (agent_name, capability_name) in INTERNAL


def uses_research(agent_name: str, capability_name: str) -> bool:
    """Whether this capability searches the web (it declares ResearchUse); False for unknown ones."""
    agent = get_agent(agent_name)
    cap = agent.capabilities().get(capability_name) if agent is not None else None
    return cap is not None and cap.research is not None


def research_capabilities() -> frozenset[tuple[str, str]]:
    """(agent, capability) of every capability that searches the web."""
    return frozenset((a.name, c.name) for a in all_agents().values() for c in a.capabilities().values()
                     if c.research is not None)


def _research_step(step: Any) -> bool:
    return isinstance(step, dict) and uses_research(str(step.get("agent")), str(step.get("capability")))


def hide_research_steps(plan: Any) -> Any:
    """A Supervisor plan with its web-search steps' parameters and rationale left out (the query, and Claude's words
    about it): as a viewer sees it, and as a purge leaves it stored."""
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
        return plan
    steps = [{**step, "params": {}, "rationale": ""} if _research_step(step) else step for step in plan["steps"]]
    return {**plan, "steps": steps}


def plan_mentions_research(plan: Any) -> bool:
    """Whether a stored Supervisor plan has a web-search step, or a note about one it dropped."""
    if not isinstance(plan, dict):
        return False
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    if any(_research_step(step) for step in steps):
        return True
    dropped = tuple(f"dropped {agent}.{capability}" for agent, capability in research_capabilities())
    notes = plan.get("notes") if isinstance(plan.get("notes"), list) else []
    return any(isinstance(note, str) and note.startswith(dropped) for note in notes)


def request_searches_web(message: str, plan: Any) -> bool:
    """Whether a Supervisor request asked for a web search (a web-search phrase) or had one planned or dropped. Its
    message, reply and summary then hold the query, or words of it: viewers read none of them (owner decision 13)."""
    return bool(WEB_SEARCH_ASKED.search(message or "")) or plan_mentions_research(plan)


def schedulable(agent_name: str, capability_name: str) -> str:
    """Why this capability may not run on a schedule; "" if it may. Research is never scheduled: recurring searches
    are not part of this release (and the gateway refuses a scheduled task whatever happens here)."""
    if uses_research(agent_name, capability_name):
        return f"{agent_name}.{capability_name} searches the web, and web research never runs on a schedule"
    return ""


async def agent_enabled(session: AsyncSession, agent_name: str) -> bool:
    state = await session.get(AgentState, agent_name)
    return True if state is None else state.enabled


async def agent_unavailable(session: AsyncSession, settings: Settings, agent_name: str) -> str:
    """Why this agent may not take on or run work now (switched off, or not in this edition); "" if it may."""
    if not await agent_enabled(session, agent_name):
        return f"the {agent_name} agent is switched off"
    entitlements = await licensing.current_entitlements(session, settings)
    if not entitlements.allows_agent(agent_name):
        return f"the {agent_name} agent is not included in the {entitlements.edition.title} edition"
    return ""


def validate_params(agent: str, capability: str, params: dict[str, Any] | None) -> Params:
    """The parameters as the capability's own model accepts them; TaskError otherwise. A research query is never
    repeated in the error (it may be what the screen would refuse)."""
    cap = resolve_capability(agent, capability)
    try:
        return cap.params_model.model_validate(params or {})
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_input=cap.research is None)
        raise TaskError(f"invalid parameters for {agent}.{capability}: {errors}") from exc


def describe_action(agent: str, capability: str, params: dict[str, Any]) -> str:
    shown = ", ".join(f"{k}={v!r}" for k, v in list(params.items())[:4])
    return f"{agent}.{capability}({shown})"


async def requested_details(
    session: AsyncSession, *, source: str, created_by: str, request_id: int | None, schedule_id: int | None, retry_of: int | None,
) -> dict[str, Any]:
    """Who or what asked for a change, for the approval card: a person, the AI Supervisor for a request (and where that
    request came from), a schedule, or a retry of an earlier task."""
    request = await session.get(SupervisorRequest, request_id) if request_id is not None else None
    schedule = await session.get(Schedule, schedule_id) if schedule_id is not None else None
    return {
        "source": source,
        "by": created_by,
        "request_id": request_id,
        "request_source": request.source if request else None,
        "requested_by": request.requested_by if request else None,
        "message": request.message[:200] if request else None,
        "schedule": schedule.name if schedule else None,
        "retry_of": retry_of,
    }


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
    retry_of: int | None = None,
) -> Task:
    cap = resolve_capability(agent, capability)
    if cap.research is not None and (schedule_id is not None or source == "schedule"):
        raise TaskError(schedulable(agent, capability))
    if (agent, capability) == RESEARCH_TEST and source != RESEARCH_TEST_SOURCE:
        raise TaskError("the research connectivity test is started by an admin from the Research Center only")
    validated = validate_params(agent, capability, params)
    unavailable = await agent_unavailable(session, settings, agent)
    if unavailable:
        raise TaskError(unavailable)

    clean = validated.model_dump(mode="json")
    needs_approval = cap.risk.requires_approval
    extra: dict[str, Any] = {}
    if needs_approval:
        # Before the task exists: a target that does not exist must not leave a task without an approval behind.
        try:
            extra = await get_agent(agent).approval_context(session, capability, clean)  # type: ignore[union-attr]
        except ValueError as exc:
            raise TaskError(str(exc)) from exc
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
        action = summary or describe_action(agent, capability, clean)
        expected = extra.get("expected")
        if isinstance(expected, dict) and expected.get("summary"):
            action += f" ({expected['summary']})"
        if extra.get("target"):
            action += f" on {extra['target']}" + (" (PRODUCTION)" if extra.get("production") else "")
        requested = await requested_details(session, source=source, created_by=created_by, request_id=request_id,
                                            schedule_id=schedule_id, retry_of=retry_of)
        session.add(
            Approval(
                task_id=task.id,
                agent=agent,
                capability=capability,
                risk=cap.risk.value,
                summary=action,
                details={"params": clean, "capability_description": cap.description, **extra, "requested": requested},
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


class TargetChanged(TaskError):
    pass


async def current_target(session: AsyncSession, approval: Approval, params: dict[str, Any], *, lock: bool = False) -> dict[str, Any]:
    """The approval context as it is now; raises TargetChanged if it no longer matches the approved one."""
    agent = get_agent(approval.agent)
    try:
        fresh = await agent.approval_context(session, approval.capability, params, lock=lock) if agent else {}
    except ValueError as exc:
        raise TargetChanged(f"the target changed since this was requested: {exc}; reject it and request the change again") from exc
    check_target(approval.details or {}, fresh)
    return fresh


async def recheck_target(agent: BaseAgent, ctx: AgentContext, capability: str, params: dict[str, Any]) -> dict[str, Any]:
    """In the task's own session, right before the action is sent: the target still matches the approval.

    lock=True keeps the target's rows locked until the task's transaction ends, so they cannot change in between.
    """
    try:
        fresh = await agent.approval_context(ctx.session, capability, params, lock=True)
    except ValueError as exc:
        raise TargetChanged(f"the target changed since this was requested: {exc}; reject it and request the change again") from exc
    check_target(ctx.approved_target or {}, fresh)
    return fresh


def check_target(approved: dict[str, Any], fresh: dict[str, Any]) -> None:
    """Raises TargetChanged unless `fresh` (an approval context computed now) still describes the `approved` target.

    Also used by agents in their own session, right before a change is sent.
    """
    if fresh.get("binding") != approved.get("binding"):
        raise TargetChanged(f"the target changed since this was requested (now: {fresh.get('target') or 'none'}); "
                            "reject it and request the change again")
    if fresh.get("production") and not approved.get("production"):
        # Approved without an admin's typed confirmation, which production needs.
        raise TargetChanged(f"{fresh.get('target')} became a production target after this was requested; "
                            "reject it and request the change again")


async def decide_approval(
    session: AsyncSession, settings: Settings, approval: Approval, *, approve: bool, actor: str, note: str = "",
    actor_role: str = "", confirm: str = "",
) -> Task:
    if approval.status != "pending":
        raise TaskError(f"approval {approval.id} was already {approval.status}")
    details = approval.details or {}
    if approve:
        unavailable = await agent_unavailable(session, settings, approval.agent)
        if unavailable:
            raise TaskError(f"{unavailable}; switch it on again before approving its changes, or reject this one")
        task = await session.get(Task, approval.task_id)
        assert task is not None
        # Computed again now: the site or server may have moved to production, been
        # re-linked or renamed since the request was made.
        await current_target(session, approval, task.params, lock=True)
        if details.get("production"):
            if actor_role != "admin":
                raise ConfirmationRequired("changes to production need an admin's approval")
            expected = str(details.get("confirm", "")).strip()
            if not expected or confirm.strip() != expected:
                raise ConfirmationRequired(f"type '{expected}' to confirm this production change")
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


async def cancel_task(session: AsyncSession, settings: Settings, task: Task, actor: str, *, reason: str = "") -> Task:
    if task.status in TERMINAL or task.status == "running":
        raise TaskError(f"task {task.id} is {task.status} and cannot be cancelled")
    if task.status == "awaiting_approval":
        approval = await session.scalar(select(Approval).where(Approval.task_id == task.id))
        if approval and approval.status == "pending":
            approval.status = "rejected"
            approval.decided_by = actor
            approval.decided_at = now()
            approval.decision_note = f"task cancelled: {reason}" if reason else "task cancelled"
    task.status = "cancelled"
    task.finished_at = now()
    task.updated_at = now()
    task.error = f"cancelled by {actor}" + (f": {reason}" if reason else "")
    await on_task_terminal(session, settings, task)
    return task


async def cancel_pending_changes(session: AsyncSession, settings: Settings, agent: str, actor: str, reason: str) -> list[int]:
    """Cancels the agent's tasks that need an approval and have not started: waiting for one, or approved and queued.

    Used when the agent is switched off, so that neither a pending nor an already given approval still runs.
    """
    rows = (await session.scalars(
        select(Task).where(Task.agent == agent, Task.status.in_(("queued", "awaiting_approval")),
                           Task.risk.not_in(("read", "draft"))).order_by(Task.id).with_for_update()
    )).all()
    for task in rows:
        await cancel_task(session, settings, task, actor, reason=reason)
    return [task.id for task in rows]


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
        retry_of=task.id,
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
