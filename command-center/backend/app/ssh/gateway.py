"""What agents use to talk to servers: approval check, audit trail, then the gate.

Every request is written to server_commands in its own transaction, before it
is sent and again when the answer arrives, so the audit trail survives even if
the task that asked for it fails. Operations that change anything are refused
unless the task carries a human approval, whatever the capability declared.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .. import events
from ..db import session_scope
from ..integrations import ApprovalRequired
from ..models import Server, ServerCommand
from . import keys
from .client import GateAnswer, GateConnection, GateTimeout, HostKeyMismatch, SSHError
from .ops import OPS, build_command

if TYPE_CHECKING:
    from ..agents.base import AgentContext
    from ..config import Settings

RESULT_LIMIT = 60_000  # characters of JSON kept in the audit row


class GateRefused(SSHError):
    """The gate (on the server) refused the request."""


class GateFailed(SSHError):
    """The operation ran on the server and failed."""


def _clip(result: Any) -> Any:
    text = json.dumps(result, default=str)
    if len(text) <= RESULT_LIMIT:
        return result
    return {"truncated": True, "preview": text[:RESULT_LIMIT]}


class SSHGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def open(self, ctx: AgentContext, server: Server) -> GateSession:
        return GateSession(self, ctx, server)


class GateSession:
    def __init__(self, gateway: SSHGateway, ctx: AgentContext, server: Server) -> None:
        self.gateway, self.ctx, self.server = gateway, ctx, server
        self.settings = gateway.settings
        self._conn: GateConnection | None = None
        self.sent_change = False  # a change operation has been sent to the server in this session

    async def __aenter__(self) -> GateSession:
        server = self.server
        if not server.enabled:
            raise SSHError(f"server {server.name} is disabled")
        async with session_scope() as s:
            client_key = await keys.load_private_key(s, self.settings)
        conn = GateConnection(host=server.host, port=server.port, username=server.username, host_key=server.host_key,
                              client_key=client_key, settings=self.settings)
        try:
            await conn.__aenter__()
        except HostKeyMismatch as exc:
            async with session_scope() as s:
                await events.emit(s, self.settings, kind="server.host_key_mismatch", severity="critical", source=self.ctx_agent,
                                  title=f"{server.name}: host key does not match the pinned key; connection refused",
                                  detail={"server_id": server.id, "host": server.host, "error": str(exc)}, task_id=self.ctx.task_id)
            raise
        self._conn = conn
        async with session_scope() as s:
            row = await s.get(Server, server.id)
            if row is not None:
                row.last_seen_at = datetime.now(UTC)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._conn is not None:
            await self._conn.__aexit__(*exc)

    @property
    def ctx_agent(self) -> str:
        return getattr(self.ctx, "agent_name", "") or "server_management"

    async def run(self, op: str, **args: str) -> Any:
        spec = OPS.get(op)
        if spec is None:
            raise GateRefused(f"unknown operation {op}")
        if spec.kind == "change" and not self.ctx.approved_by:
            raise ApprovalRequired(f"{op} changes the server and needs a human approval first")
        command = build_command(op, args)
        assert self._conn is not None, "use 'async with gateway.open(...)'"
        timeout = self.settings.ssh_slow_command_timeout_seconds if spec.slow else self.settings.ssh_command_timeout_seconds
        left = self.ctx.time_left()
        if spec.kind == "change" and left is not None and left < timeout:
            # Better not to start a change than to lose track of it halfway.
            raise SSHError(f"not enough time left in this task to run {op} safely ({left:.0f}s of {timeout}s); nothing was sent")
        async with session_scope() as s:
            row = ServerCommand(server_id=self.server.id, task_id=self.ctx.task_id, op=op, args=args, kind=spec.kind,
                                requested_by=self.ctx.requested_by, approved_by=self.ctx.approved_by)
            s.add(row)
            await s.flush()
            audit_id = row.id
        if spec.kind == "change":
            self.sent_change = True
        try:
            answer = await self._conn.request(command, timeout)
        except GateTimeout as exc:
            await self._unknown(audit_id, op, spec.kind, str(exc))
            raise
        except SSHError as exc:
            await self._finish(audit_id, GateAnswer(ok=False, refused=False, exit_status=None, error=str(exc)))
            raise
        except BaseException:
            # Cancelled (the task's time ran out) while the server was working: record it anyway.
            await asyncio.shield(self._unknown(audit_id, op, spec.kind, "cancelled before the server answered"))
            raise
        if answer.payload.get("timed_out"):
            await self._unknown(audit_id, op, spec.kind, answer.error, answer)
            raise GateFailed(f"{op} timed out on {self.server.name}: {answer.error}")
        await self._finish(audit_id, answer)
        self.ctx.log(f"{self.server.name}: {op} {args or ''} -> " + ("ok" if answer.ok else f"{'refused' if answer.refused else 'failed'}: {answer.error}"),
                     "info" if answer.ok else "warning")
        if answer.refused:
            raise GateRefused(f"{self.server.name} refused {op}: {answer.error}")
        if not answer.ok:
            raise GateFailed(f"{op} failed on {self.server.name}: {answer.error}")
        return answer.result

    async def _unknown(self, audit_id: int, op: str, kind: str, reason: str, answer: GateAnswer | None = None) -> None:
        """The outcome is not known: it may have finished, or be partly done."""
        error = f"outcome unknown: {reason}"
        await self._finish(audit_id, GateAnswer(ok=False, refused=False, exit_status=answer.exit_status if answer else None, error=error,
                                                output_sha256=answer.output_sha256 if answer else "",
                                                duration_ms=answer.duration_ms if answer else 0))
        self.ctx.log(f"{self.server.name}: {op} -> {error}", "warning")
        if kind == "change":
            async with session_scope() as s:
                await events.emit(s, self.settings, kind="server.change_outcome_unknown", severity="critical", source=self.ctx_agent,
                                  title=f"{self.server.name}: {op} may not have finished ({reason}); check the server",
                                  detail={"server_id": self.server.id, "op": op, "audit_id": audit_id}, task_id=self.ctx.task_id)

    async def _finish(self, audit_id: int, answer: GateAnswer) -> None:
        async with session_scope() as s:
            row = await s.get(ServerCommand, audit_id)
            assert row is not None
            row.finished_at = datetime.now(UTC)
            row.duration_ms = answer.duration_ms
            row.exit_status = answer.exit_status
            row.ok = answer.ok
            row.refused = answer.refused
            row.result = _clip(answer.result) if answer.ok else None
            row.error = answer.error[:2000]
            row.output_sha256 = answer.output_sha256
