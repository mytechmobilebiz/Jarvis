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

from .. import events
from .. import tasks as task_service
from ..agents.base import AgentContext, AgentResult
from ..agents.http import ReadOnlyHttpClient
from ..agents.registry import get_agent
from ..config import Settings
from ..db import session_scope, sessionmaker
from ..integrations import IntegrationHub, build_hub
from ..llm import LLMProvider, runtime_provider
from ..models import Approval, Artifact, ServerCommand, Task, TaskLog, WorkerHeartbeat
from ..workflow import WorkflowEngine, build_engine
from . import forwarder, scheduler

log = logging.getLogger(__name__)

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
                refused = ""
                if approved_by:
                    # The approval covers the target as it was then; refuse if it was re-linked or changed since.
                    try:
                        await task_service.current_target(session, approval, params)
                    except task_service.TargetChanged as exc:
                        refused = str(exc)
                llm = self._fixed_llm or await runtime_provider(session, self.settings)
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
                    ctx = AgentContext(
                        session=session,
                        settings=self.settings,
                        http=http,
                        llm=llm,
                        integrations=self.hub,
                        task_id=task_id,
                        request_id=request_id,
                        approved_by=approved_by,
                        logs=logs,
                        ssh=self.ssh,
                        requested_by=requested_by,
                        agent_name=agent_name,
                        deadline=asyncio.get_running_loop().time() + timeout,
                    )
                    try:
                        result = await asyncio.wait_for(agent.run(ctx, cap_name, params), timeout=timeout)
                        await self._complete(session, task_id, agent_name, result, logs)
                        await session.commit()
                    except BaseException:
                        await session.rollback()
                        raise
            except TimeoutError:
                await self._fail(task_id, f"timed out after {timeout}s", logs)
            except Exception as exc:  # noqa: BLE001 - every failure is recorded on the task
                log.warning("task %s (%s.%s) failed: %s", task_id, agent_name, cap_name, exc)
                logs.append(("error", "".join(traceback.format_exception_only(exc)).strip()))
                await self._fail(task_id, f"{exc.__class__.__name__}: {exc}", logs)
            finally:
                beat.cancel()
                await http.aclose()

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
