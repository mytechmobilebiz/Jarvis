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

from .. import tasks as task_service
from ..models import Server
from .base import AgentContext, AgentResult, BaseAgent, Finding, Params, Risk, capability
from .wp_server import server_binding, server_label


MIN_GATE_VERSION = (1, 1, 0)  # runs WP-CLI through cc-wp


def _version(value: Any) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(value).split("."))
    except ValueError:
        return (0,)


def overview_summary(name: str, info: dict[str, Any]) -> str:
    """One line from the gate's system_overview; a value the gate did not report says so instead of reading as 0 (C2-13)."""
    seconds = info.get("uptime_seconds")
    up = f"up {int(seconds) // 86400} day(s)" if isinstance(seconds, (int, float)) else "uptime not reported"
    load = (info.get("load") or [None])[0]
    mem = info.get("memory_mb") or {}
    memory = (f"memory {mem['available']}/{mem['total']} MB free" if mem.get("available") is not None and mem.get("total")
              else "memory not reported")
    disks = "; ".join(f"{d.get('mount', '?')} {d.get('used_pct', '?')}%" for d in info.get("disks") or []) or "not reported"
    return f"{name}: {up}, load {load if isinstance(load, (int, float)) else 'not reported'}, {memory}, disks {disks}"


class ServerParams(Params):
    server_id: int = Field(description="ID of a registered server (Servers page)")


class ServiceParams(Params):
    server_id: int = Field(description="ID of a registered server")
    service: str = Field(pattern=r"^[a-z0-9][a-z0-9@._-]{0,63}$", description="A service listed in the server's cc-gate configuration")


async def load_server(ctx: AgentContext, server_id: int, *, refresh: bool = False) -> Server:
    """refresh=True reads the row again rather than from this session's identity map."""
    server = await ctx.session.get(Server, server_id, populate_existing=refresh)
    if server is None:
        raise ValueError(f"no server with id {server_id}")
    if ctx.ssh is None:
        raise RuntimeError("SSH is not available in this process")
    return server


async def server_approval_context(session: AsyncSession, server_id: int | None, *, lock: bool = False) -> dict[str, Any]:
    if server_id is None:
        return {}
    server = await session.get(Server, server_id, with_for_update=lock)
    if server is None:
        raise ValueError(f"no server with id {server_id}")
    # The binding includes the pinned host key: after a re-pin another machine may answer, so earlier approvals stop applying.
    return {"target": f"server {server_label(server)}", "environment": server.environment,
            "production": server.environment == "production", "confirm": server.name,
            "binding": {"server": server_binding(server)}}


class ServerManagementAgent(BaseAgent):
    name = "server_management"
    title = "VPS & Server Management (CWP/SSH)"
    description = (
        "Server diagnostics and maintenance over SSH with a restricted non-root account. Only allowlisted operations can "
        "run (through cc-gate on the server); the host key is pinned; every command is audited; changes need approval, "
        "and production changes need an admin's typed confirmation."
    )
    integrations = ("ssh",)

    async def approval_context(self, session: AsyncSession, capability: str, params: dict[str, Any], *,
                               lock: bool = False) -> dict[str, Any]:
        return await server_approval_context(session, params.get("server_id"), lock=lock)

    @capability(risk=Risk.READ, description="Connect through cc-gate and report the gate version, account, configured sites and services.",
                params=ServerParams, examples=("Test the connection to server 1",))
    async def test_connection(self, ctx: AgentContext, params: ServerParams) -> AgentResult:
        server = await load_server(ctx, params.server_id)
        async with ctx.ssh.open(ctx, server) as gate:
            hello = await gate.run("hello")
        findings = []
        if int(hello.get("uid", -1)) == 0:
            findings.append(Finding(code="gate_runs_as_root", severity="critical", title="cc-gate reports uid 0; use an unprivileged account"))
        if _version(hello.get("gate_version")) < MIN_GATE_VERSION:
            findings.append(Finding(code="gate_outdated", severity="warning",
                                    title=f"cc-gate {hello.get('gate_version')} on this server is older than "
                                          f"{'.'.join(map(str, MIN_GATE_VERSION))}; run install-gate.sh from this version's server folder"))
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
        load = (info.get("load") or [None])[0]
        if isinstance(load, (int, float)) and load > 2 * cpus:
            findings.append(Finding(code="load_high", severity="warning", title=f"Load {load} on {cpus} CPU(s)"))
        return AgentResult(summary=overview_summary(server.name, info), data=info, findings=findings)

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
                params=ServiceParams, timeout_seconds=360)
    async def restart_service(self, ctx: AgentContext, params: ServiceParams) -> AgentResult:
        server = await load_server(ctx, params.server_id, refresh=True)
        # Right before the change, in this task's own session: still the server that was approved (from the row just read).
        task_service.check_target(ctx.approved_target or {}, await server_approval_context(ctx.session, server.id))
        async with ctx.ssh.open(ctx, server) as gate:
            await gate.run("service_restart", service=params.service)
            state = (await gate.run("service_status", service=params.service)).get("state", "unknown")
        return AgentResult(summary=f"{server.name}: restarted {params.service} (now {state}); approved by {ctx.approved_by}",
                           data={"service": params.service, "state": state},
                           findings=[] if state == "active" else [Finding(code="service_not_active", severity="critical",
                                                                         title=f"{params.service} is {state} after the restart")])
