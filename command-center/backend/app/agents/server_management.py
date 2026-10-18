"""VPS & Server Management (CWP/SSH): restricted, allowlisted, audited server operations.

Every operation goes through app.ssh: the server's cc-gate (forced command of
a non-root account) accepts only allowlisted operations, the host key is
pinned, every command is audited, and anything that changes a server needs an
approval (an admin's typed confirmation for production).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Server
from .base import AgentContext, AgentResult, BaseAgent, Finding, Params, Risk, capability


class ServerParams(Params):
    server_id: int = Field(description="ID of a registered server (Servers page)")


class ServiceParams(Params):
    server_id: int = Field(description="ID of a registered server")
    service: str = Field(pattern=r"^[a-z0-9][a-z0-9@._-]{0,63}$", description="A service listed in the server's cc-gate configuration")


async def load_server(ctx: AgentContext, server_id: int) -> Server:
    server = await ctx.session.get(Server, server_id)
    if server is None:
        raise ValueError(f"no server with id {server_id}")
    if ctx.ssh is None:
        raise RuntimeError("SSH is not available in this process")
    return server


async def server_approval_context(session: AsyncSession, server_id: int | None) -> dict[str, Any]:
    server = await session.get(Server, server_id) if server_id else None
    if server is None:
        return {}
    return {"target": f"server {server.name}", "environment": server.environment,
            "production": server.environment == "production", "confirm": server.name}


class ServerManagementAgent(BaseAgent):
    name = "server_management"
    title = "VPS & Server Management (CWP/SSH)"
    description = (
        "Server diagnostics and maintenance over SSH with a restricted non-root account. Only allowlisted operations can "
        "run (through cc-gate on the server); the host key is pinned; every command is audited; changes need approval, "
        "and production changes need an admin's typed confirmation."
    )
    integrations = ("ssh",)

    async def approval_context(self, session: AsyncSession, capability: str, params: dict[str, Any]) -> dict[str, Any]:
        return await server_approval_context(session, params.get("server_id"))

    @capability(risk=Risk.READ, description="Connect through cc-gate and report the gate version, account, configured sites and services.",
                params=ServerParams, examples=("Test the connection to server 1",))
    async def test_connection(self, ctx: AgentContext, params: ServerParams) -> AgentResult:
        server = await load_server(ctx, params.server_id)
        async with ctx.ssh.open(ctx, server) as gate:
            hello = await gate.run("hello")
        findings = []
        if int(hello.get("uid", -1)) == 0:
            findings.append(Finding(code="gate_runs_as_root", severity="critical", title="cc-gate reports uid 0; use an unprivileged account"))
        if hello.get("allow_changes"):
            findings.append(Finding(code="changes_allowed", severity="info",
                                    title="The server allows approved changes (allow_changes is true in its cc-gate configuration)"))
        server.last_hello = {**hello, "at": datetime.now(UTC).isoformat()}
        sites = ", ".join(hello.get("sites", {})) or "none"
        return AgentResult(
            summary=f"{server.name}: cc-gate {hello.get('gate_version')} as {hello.get('user')} (uid {hello.get('uid')}) on "
                    f"{hello.get('hostname')}; sites: {sites}; changes {'allowed' if hello.get('allow_changes') else 'switched off'}.",
            data={"hello": hello}, findings=findings,
        )

    @capability(risk=Risk.READ, description="Uptime, load, memory and disk space of a server.", params=ServerParams,
                examples=("How is server 1 doing?",))
    async def server_overview(self, ctx: AgentContext, params: ServerParams) -> AgentResult:
        server = await load_server(ctx, params.server_id)
        async with ctx.ssh.open(ctx, server) as gate:
            info = await gate.run("system_overview")
        findings = []
        for disk in info.get("disks", []):
            pct = disk.get("used_pct", 0)
            if pct >= 90:
                findings.append(Finding(code="disk_full", severity="critical", title=f"{disk['mount']} is {pct}% full ({disk['free_gb']} GB free)"))
            elif pct >= 80:
                findings.append(Finding(code="disk_filling", severity="warning", title=f"{disk['mount']} is {pct}% full ({disk['free_gb']} GB free)"))
        mem = info.get("memory_mb") or {}
        if mem.get("total") and mem.get("available") is not None and mem["available"] < 0.1 * mem["total"]:
            findings.append(Finding(code="memory_low", severity="warning", title=f"Only {mem['available']} MB of {mem['total']} MB memory available"))
        cpus = info.get("cpus") or 1
        load = (info.get("load") or [0])[0]
        if load > 2 * cpus:
            findings.append(Finding(code="load_high", severity="warning", title=f"Load {load} on {cpus} CPU(s)"))
        days = int(info.get("uptime_seconds", 0)) // 86400
        disks = "; ".join(f"{d['mount']} {d['used_pct']}%" for d in info.get("disks", []))
        return AgentResult(summary=f"{server.name}: up {days} day(s), load {load}, memory {mem.get('available')}/{mem.get('total')} MB free, disks {disks}",
                           data=info, findings=findings)

    @capability(risk=Risk.READ, description="Check every service listed in the server's cc-gate configuration.", params=ServerParams,
                examples=("Are the services on server 1 running?",))
    async def check_services(self, ctx: AgentContext, params: ServerParams) -> AgentResult:
        server = await load_server(ctx, params.server_id)
        states: dict[str, str] = {}
        async with ctx.ssh.open(ctx, server) as gate:
            hello = await gate.run("hello")
            for service in hello.get("services", []):
                states[service] = (await gate.run("service_status", service=service)).get("state", "unknown")
        # Services that are not installed report "inactive"/"unknown"; only "failed" is certainly a problem.
        findings = [Finding(code="service_failed", severity="critical", title=f"{name} has failed") for name, s in states.items() if s == "failed"]
        running = [n for n, s in states.items() if s == "active"]
        return AgentResult(summary=f"{server.name}: running: {', '.join(running) or 'none'}" + (f"; failed: {', '.join(f.title.split()[0] for f in findings)}" if findings else ""),
                           data={"services": states}, findings=findings)

    @capability(risk=Risk.CHANGE, description="Restart a service the server's cc-gate configuration lists as restartable (approval required).",
                params=ServiceParams)
    async def restart_service(self, ctx: AgentContext, params: ServiceParams) -> AgentResult:
        server = await load_server(ctx, params.server_id)
        async with ctx.ssh.open(ctx, server) as gate:
            await gate.run("service_restart", service=params.service)
            state = (await gate.run("service_status", service=params.service)).get("state", "unknown")
        return AgentResult(summary=f"{server.name}: restarted {params.service} (now {state}); approved by {ctx.approved_by}",
                           data={"service": params.service, "state": state},
                           findings=[] if state == "active" else [Finding(code="service_not_active", severity="critical",
                                                                         title=f"{params.service} is {state} after the restart")])
