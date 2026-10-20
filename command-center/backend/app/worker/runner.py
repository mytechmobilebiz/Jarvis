"""The worker: claims queued tasks, runs agent capabilities, records results."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import traceback
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from .. import events
from .. import tasks as task_service
from ..agents.base import AgentContext, AgentResult
from ..agents.http import ReadOnlyHttpClient
from ..agents.registry import get_agent
from ..agents.wp_server import SiteBusy
from ..brain.notes import reference_notes
from ..brain.policy import POLICIES, SENSITIVE_AGENT
from ..brain.quotes import QuotesBusy, lock_quotes
from ..brain.store import AgentMemory
from ..config import Settings
from ..db import describe_error, session_scope, sessionmaker
from ..integrations import IntegrationHub, build_hub
from ..llm import LLMProvider, runtime_provider
from ..models import Approval, Artifact, ServerCommand, Task, TaskLog, WorkerHeartbeat
from ..workflow import WorkflowEngine, build_engine
from . import forwarder, scheduler

log = logging.getLogger(__name__)

BUSY_RETRY_SECONDS = 30  # a task whose site is busy (SiteBusy), or that must quote (QuotesBusy), is tried again later
# Capabilities that copy JARVIS Brain text into their result (a history answer): they hold the quote lock shared from
# before their first read until they commit, so a withhold, purge or redaction finds what they wrote
# (app/brain/quotes.py). Never waited for: while a removal holds or waits for it, the task goes back to the queue, as
# for a busy site. summarize_request takes it itself, and only when it copies such an answer (never around a call to
# an AI model).
QUOTING = {("supervisor", "recall")}

CLAIM_SQL = text(
    """
    UPDATE tasks SET status = 'running', started_at = now(), heartbeat_at = now(), updated_at = now(),
           locked_by = :worker, attempts = attempts + 1
    WHERE id = (
        SELECT id FROM tasks
        WHERE status = 'queued' AND run_after <= now()
        ORDER BY priority, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id
    """
)


def _now() -> datetime:
    return datetime.now(UTC)


class Worker:
    def __init__(
        self,
        settings: Settings,
        *,
        llm: LLMProvider | None = None,
        hub: IntegrationHub | None = None,
        engine: WorkflowEngine | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        worker_id: str | None = None,
        withheld_check_done: bool = True,
    ) -> None:
        self.settings = settings
        # A fixed provider (tests); otherwise resolved per task from the
        # customer's AI settings, so a key entered in the dashboard applies at once.
        self._fixed_llm = llm
        self.hub = hub or build_hub(settings)
        self.engine = engine or build_engine(settings)
        self.http_transport = http_transport
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self._slots = asyncio.Semaphore(settings.worker_concurrency)
        self._running: set[asyncio.Task[None]] = set()
        self._stop = asyncio.Event()
        self._last_probe = 0.0
        # The start-up's withheld check (bootstrap.check_withheld_copies) skipped or failed: retried from tick() at
        # most once a minute until it runs completely. It never waits for a lock, so ticks are never delayed.
        self._withheld_check_done = withheld_check_done
        self._last_withheld_check: float | None = None
        from ..ssh.gateway import SSHGateway

        self.ssh = SSHGateway(settings)

    # ----------------------------------------------------------------- loop

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        log.info("worker %s started (concurrency %d, workflow engine: %s)", self.worker_id, self.settings.worker_concurrency, self.engine.name)
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive a bad tick
                log.exception("worker tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.worker_poll_seconds)
            except TimeoutError:
                pass
        if self._running:
            await asyncio.gather(*self._running, return_exceptions=True)
        log.info("worker %s stopped", self.worker_id)

    async def tick(self) -> int:
        await self.heartbeat()
        await self.retry_withheld_check()
        await scheduler.enqueue_due(self.settings)
        await self.reap_stale()
        claimed = 0
        while len(self._running) < self.settings.worker_concurrency:
            task_id = await self.claim()
            if task_id is None:
                break
            claimed += 1
            job = asyncio.create_task(self.execute(task_id))
            self._running.add(job)
            job.add_done_callback(self._running.discard)
        await forwarder.forward_pending(self.engine)
        await self.probe_engine()
        return claimed

    async def retry_withheld_check(self) -> None:
        if self._withheld_check_done:
            return
        loop = asyncio.get_running_loop()
        if self._last_withheld_check is not None and loop.time() - self._last_withheld_check < 60:
            return
        self._last_withheld_check = loop.time()
        from ..bootstrap import check_withheld_copies

        self._withheld_check_done = await check_withheld_copies()

    async def run_until_idle(self, max_rounds: int = 100) -> None:
        """Process everything currently runnable (used by tests and one-shot runs)."""
        for _ in range(max_rounds):
            claimed = await self.tick()
            if self._running:
                await asyncio.gather(*list(self._running), return_exceptions=True)
            if claimed == 0 and not self._running:
                return
        raise RuntimeError("worker did not become idle")

    # ------------------------------------------------------------ bookkeeping

    async def heartbeat(self) -> None:
        async with session_scope() as session:
            hb = await session.get(WorkerHeartbeat, self.worker_id)
            info = {"running": len(self._running), "engine": self.engine.name, "concurrency": self.settings.worker_concurrency}
            if hb is None:
                session.add(WorkerHeartbeat(worker_id=self.worker_id, info=info))
            else:
                hb.last_seen_at = _now()
                hb.info = info

    async def probe_engine(self) -> None:
        loop = asyncio.get_running_loop()
        if not self.engine.delivers or loop.time() - self._last_probe < 60:
            return
        self._last_probe = loop.time()
        try:
            health = await self.engine.health()
        except Exception as exc:  # noqa: BLE001
            await forwarder.record_integration(self.engine.name, False, f"health check failed: {exc}")
            return
        if health.get("reachable") is None:
            return
        if health["reachable"]:
            await forwarder.record_integration(self.engine.name, True, detail=health)
        else:
            await forwarder.record_integration(
                self.engine.name, False, f"health check returned HTTP {health.get('status_code')}", detail=health
            )

    async def reap_stale(self) -> None:
        """Fail tasks whose worker vanished mid-run (container restart, crash)."""
        cutoff = _now() - timedelta(minutes=self.settings.stale_task_minutes)
        async with session_scope() as session:
            stale = (
                await session.scalars(
                    select(Task).where(Task.status == "running", Task.heartbeat_at < cutoff).with_for_update(skip_locked=True)
                )
            ).all()
            for task in stale:
                task.status = "failed"
                task.error = f"worker {task.locked_by or '?'} stopped responding while running this task"
                task.finished_at = _now()
                task.updated_at = _now()
                # Server commands it left unanswered may have finished, or be half done.
                open_commands = (await session.scalars(
                    select(ServerCommand).where(ServerCommand.task_id == task.id, ServerCommand.finished_at.is_(None))
                )).all()
                for row in open_commands:
                    row.finished_at, row.ok, row.error = _now(), False, "outcome unknown: the worker stopped while this was running"
                changes = [row.op for row in open_commands if row.kind == "change"]
                change_task = task.risk not in ("read", "draft")
                await events.emit(
                    session, self.settings, kind="task.failed", severity="critical" if change_task else "warning", source=task.agent,
                    title=f"Task {task.id} ({task.agent}.{task.capability}) was interrupted: the worker stopped"
                          + (f"; {', '.join(changes)} may not have finished - check the server" if changes else ""),
                    detail={"task_id": task.id, "unfinished_server_commands": [row.id for row in open_commands]}, task_id=task.id,
                )
                await task_service.on_task_terminal(session, self.settings, task)

    async def claim(self) -> int | None:
        async with session_scope() as session:
            return await session.scalar(CLAIM_SQL, {"worker": self.worker_id})

    # -------------------------------------------------------------- execution

    async def execute(self, task_id: int) -> None:
        async with self._slots:
            async with session_scope() as session:
                task = await session.get(Task, task_id)
                assert task is not None
                agent_name, cap_name, params = task.agent, task.capability, dict(task.params)
                request_id = task.request_id
                requested_by = task.created_by
                approval = await session.scalar(select(Approval).where(Approval.task_id == task_id))
                approved_by = approval.decided_by if approval and approval.status == "approved" else ""
                approved_target = dict(approval.details or {}) if approved_by else None
                # Switched off (or out of the edition) after the task was created or approved: it does not run.
                refused = await task_service.agent_unavailable(session, self.settings, agent_name)
                switched_off = bool(refused) and not await task_service.agent_enabled(session, agent_name)
                if approved_by and not refused:
                    # The approval covers the target as it was then; refuse if it was re-linked or changed since.
                    try:
                        await task_service.current_target(session, approval, params)
                    except task_service.TargetChanged as exc:
                        refused = str(exc)
                llm = self._fixed_llm or await runtime_provider(session, self.settings)
            if switched_off:
                await self._withdraw(task_id, refused)
                return
            if refused:
                await self._fail(task_id, f"refused: {refused}", [("error", refused)])
                return

            http = ReadOnlyHttpClient(self.settings, transport=self.http_transport)
            logs: list[tuple[str, str]] = []
            beat = asyncio.create_task(self._heartbeat(task_id))
            timeout = self.settings.task_timeout_seconds
            try:
                agent = get_agent(agent_name)
                if agent is None:
                    raise RuntimeError(f"agent '{agent_name}' is not installed")
                cap = agent.capabilities().get(cap_name)
                if cap is not None and cap.timeout_seconds:
                    timeout = cap.timeout_seconds
                async with sessionmaker()() as session:
                    # Read-only memory through the agent's access policy (none for agents without one).
                    memory = AgentMemory(session, agent_name) if agent_name in POLICIES else None
                    ctx = AgentContext(
                        session=session,
                        settings=self.settings,
                        http=http,
                        llm=llm,
                        integrations=self.hub,
                        task_id=task_id,
                        request_id=request_id,
                        approved_by=approved_by,
                        approved_target=approved_target,
                        logs=logs,
                        ssh=self.ssh,
                        requested_by=requested_by,
                        agent_name=agent_name,
                        deadline=asyncio.get_running_loop().time() + timeout,
                        memory=memory,
                    )
                    try:
                        if (agent_name, cap_name) in QUOTING and not await lock_quotes(session, shared=True,
                                                                                       wait=False):
                            raise QuotesBusy("a JARVIS Brain removal is running")
                        result = await asyncio.wait_for(agent.run(ctx, cap_name, params), timeout=timeout)
                        if memory is not None and agent_name != SENSITIVE_AGENT:  # never for Credit Repair
                            await self._add_memory_notes(session, task_id, memory, params, result)
                        await self._complete(session, task_id, agent_name, result, logs)
                        await session.commit()
                    except BaseException:
                        await session.rollback()
                        raise
            except (SiteBusy, QuotesBusy) as exc:
                await self._requeue(task_id, str(exc), logs)
            except TimeoutError:
                await self._fail(task_id, f"timed out after {timeout}s", logs)
            except DBAPIError as exc:  # its text carries the row's values (PostgreSQL's DETAIL): classes only
                described = describe_error(exc)
                log.warning("task %s (%s.%s) failed: %s", task_id, agent_name, cap_name, described)
                logs.append(("error", described))
                await self._fail(task_id, described, logs)
            except Exception as exc:  # noqa: BLE001 - every failure is recorded on the task
                log.warning("task %s (%s.%s) failed: %s", task_id, agent_name, cap_name, exc)
                logs.append(("error", "".join(traceback.format_exception_only(exc)).strip()))
                await self._fail(task_id, f"{exc.__class__.__name__}: {exc}", logs)
            finally:
                beat.cancel()
                await http.aclose()

    async def _add_memory_notes(self, session: Any, task_id: int, memory: AgentMemory, params: dict[str, Any],
                                result: AgentResult) -> None:
        """Adds result.data.memory_notes: approved, general memory items about the task's site or topic (app/brain/notes.py).

        Local and read-only: the capability has already run, and its summary and output stay as they are. A failure
        here never fails the task: the lookup runs in a SAVEPOINT and is skipped with a warning.
        """
        # The capability's own changes are flushed first, as _complete would, so their errors surface unchanged.
        await session.flush()
        # The quote lock, shared until this task commits (a removal then finds the notes); never waited for: while a
        # removal holds it, the notes are left out.
        if not await lock_quotes(session, shared=True, wait=False):
            log.info("reference notes for task %s skipped: a JARVIS Brain removal is running", task_id)
            return
        try:
            async with session.begin_nested():
                notes = await reference_notes(memory, params)
        except Exception as exc:  # noqa: BLE001 - reference notes are optional
            log.warning("reference notes for task %s skipped (%s)", task_id, exc.__class__.__name__)
            return
        if notes:
            result.data = {**result.data, "memory_notes": notes}

    async def _heartbeat(self, task_id: int) -> None:
        """Keep a long task's heartbeat fresh so the stale-task reaper leaves it alone."""
        while True:
            await asyncio.sleep(60)
            try:
                async with session_scope() as session:
                    await session.execute(update(Task).where(Task.id == task_id, Task.status == "running").values(heartbeat_at=_now()))
            except Exception as exc:  # noqa: BLE001 - a missed beat is retried next minute
                log.warning("heartbeat for task %s failed: %s", task_id, exc)

    async def _complete(self, session: Any, task_id: int, agent_name: str, result: AgentResult, logs: list[tuple[str, str]]) -> None:
        for spec in result.artifacts:
            session.add(
                Artifact(
                    task_id=task_id,
                    agent=agent_name,
                    kind=spec.kind,
                    title=spec.title,
                    content=spec.content,
                    content_type=spec.content_type,
                    data=spec.data,
                )
            )
        for spec in result.events:
            await events.emit(
                session,
                self.settings,
                kind=spec.kind,
                severity=spec.severity,
                title=spec.title,
                source=agent_name,
                detail=spec.detail,
                task_id=task_id,
            )
        task = await session.get(Task, task_id, with_for_update=True)
        task.status = "succeeded"
        task.summary = result.summary[:2000]
        task.result = result.model_dump(mode="json", exclude={"artifacts", "events"})
        task.finished_at = _now()
        task.updated_at = _now()
        task.error = ""
        for level, message in logs:
            session.add(TaskLog(task_id=task_id, level=level, message=message[:4000]))
        await session.flush()
        await task_service.on_task_terminal(session, self.settings, task)

    async def _requeue(self, task_id: int, reason: str, logs: list[tuple[str, str]]) -> None:
        """Its site is busy with other server-side work, or a JARVIS Brain removal holds the quote lock: back in the
        queue (not an attempt, not a failure), freeing the slot."""
        log.info("task %s waits: %s", task_id, reason)
        async with session_scope() as session:
            task = await session.get(Task, task_id, with_for_update=True)
            for level, message in [*logs, ("info", f"{reason}; trying again in {BUSY_RETRY_SECONDS}s")]:
                session.add(TaskLog(task_id=task_id, level=level, message=message[:4000]))
            task.status = "queued"
            task.run_after = _now() + timedelta(seconds=BUSY_RETRY_SECONDS)
            task.attempts = max(task.attempts - 1, 0)
            task.locked_by = ""
            task.started_at = None
            task.updated_at = _now()

    async def _withdraw(self, task_id: int, reason: str) -> None:
        """Its agent was switched off before it started: cancelled, as switching off withdraws pending changes.

        Nothing ran, so this is no failure (no task.failed event, and not in the agent's failure counts).
        """
        async with session_scope() as session:
            task = await session.get(Task, task_id, with_for_update=True)
            session.add(TaskLog(task_id=task_id, level="info", message=f"not run: {reason}"))
            task.status = "cancelled"
            task.error = f"cancelled: {reason}"
            task.finished_at = _now()
            task.updated_at = _now()
            await task_service.on_task_terminal(session, self.settings, task)

    async def _fail(self, task_id: int, error: str, logs: list[tuple[str, str]]) -> None:
        async with session_scope() as session:
            task = await session.get(Task, task_id, with_for_update=True)
            for level, message in logs:
                session.add(TaskLog(task_id=task_id, level=level, message=message[:4000]))
            if task.attempts < task.max_attempts:
                task.status = "queued"
                task.run_after = _now() + timedelta(seconds=30 * task.attempts)
                task.error = error[:2000]
                task.updated_at = _now()
                return
            task.status = "failed"
            task.error = error[:2000]
            task.finished_at = _now()
            task.updated_at = _now()
            await events.emit(
                session,
                self.settings,
                kind="task.failed",
                severity="warning",
                title=f"Task {task.id} {task.agent}.{task.capability} failed: {error[:200]}",
                source=task.agent,
                detail={"task_id": task.id},
                task_id=task.id,
            )
            await task_service.on_task_terminal(session, self.settings, task)
