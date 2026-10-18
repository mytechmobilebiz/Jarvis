"""Server-side WordPress work for the WordPress Security & Repair agent: WP-CLI diagnostics and repairs.

Everything runs through the server's cc-gate (see app/ssh and server/cc-gate):
a restricted, non-root account that can only run allowlisted operations, as
the site's own system user. Diagnostics are read-only. Every repair:

  1. checks the current state, and makes no change when nothing is wrong;
  2. backs up the database into the server's backup folder (or stops);
  3. makes the one change it was approved for;
  4. verifies the result on the server and over HTTP;
  5. reports what changed, and raises a critical event if the site went down,
     naming the repair that undoes it (which needs approval in turn).

Repairs are "change" risk: always held for a human approval; for production
sites (or sites on production servers), an admin must type the site's name.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Server, Site
from .base import AgentContext, AgentResult, EventSpec, Finding, Params, Risk, capability
from .monitoring.agent import make_checker

REPAIR_TIMEOUT = 900  # seconds; core and plugin downloads can be slow


class SiteParams(Params):
    site_id: int = Field(description="ID of a monitored site that is linked to a server")


class PluginParams(Params):
    site_id: int = Field(description="ID of a monitored site that is linked to a server")
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,99}$", description="The plugin's folder name, e.g. akismet")


class MaintenanceParams(Params):
    site_id: int = Field(description="ID of a monitored site that is linked to a server")
    on: bool = Field(description="true to switch maintenance mode on, false to switch it off")


async def linked(ctx: AgentContext, site_id: int) -> tuple[Site, Server]:
    site = await ctx.session.get(Site, site_id)
    if site is None:
        raise ValueError(f"no monitored site with id {site_id}")
    if not site.server_id or not site.server_site_key:
        raise ValueError(f"{site.name} is not linked to a server yet (Websites page: server and site key)")
    server = await ctx.session.get(Server, site.server_id)
    if server is None:
        raise ValueError(f"the server linked to {site.name} no longer exists")
    if ctx.ssh is None:
        raise RuntimeError("SSH is not available in this process")
    return site, server


async def site_approval_context(session: AsyncSession, site_id: int | None) -> dict[str, Any]:
    site = await session.get(Site, site_id) if site_id else None
    if site is None:
        return {}
    server = await session.get(Server, site.server_id) if site.server_id else None
    production = site.environment == "production" or (server is not None and server.environment == "production")
    return {
        "target": f"site {site.name} ({site.url})" + (f" on server {server.name}" if server else ""),
        "environment": "production" if production else site.environment,
        "production": production,
        "confirm": site.name,
    }


def diagnosis_findings(diag: dict[str, Any], previous: dict[str, Any] | None) -> list[Finding]:
    out: list[Finding] = []
    urls = diag.get("site_urls") or {}
    if urls and not urls.get("matches"):
        out.append(Finding(code="site_url_mismatch", severity="critical",
                           title=f"WordPress address options point elsewhere (home {urls.get('home')}, siteurl {urls.get('siteurl')}; "
                                 f"expected {urls.get('expected')})",
                           detail="Changed home/siteurl options are a classic redirect hack. Repair: repair_restore_site_url."))
    verify = diag.get("core_verify") or {}
    if verify.get("verified") is False:
        out.append(Finding(code="core_modified", severity="critical",
                           title=f"{len(verify.get('files') or [])} WordPress core file(s) do not match the official checksums",
                           detail="Modified core files usually mean malware. Repair: repair_reinstall_core."))
    elif verify.get("verified") is None and verify:
        out.append(Finding(code="core_verify_unavailable", severity="info", title="Core checksums could not be checked",
                           detail=str(verify.get("detail", ""))[:300]))
    updates = [p for p in diag.get("plugins") or [] if p.get("update") == "available"]
    if updates:
        out.append(Finding(code="plugin_update_available", severity="warning",
                           title="Plugin updates available: " + ", ".join(f"{p['name']} {p.get('version')} → {p.get('update_version')}" for p in updates[:6]),
                           detail="Repair: repair_update_plugin (one plugin per approval)."))
    if diag.get("maintenance", {}).get("active"):
        out.append(Finding(code="maintenance_on", severity="warning", title="WordPress maintenance mode is on",
                           detail="Left on after an interrupted update? Repair: repair_maintenance_mode with on=false."))
    debug = diag.get("debug_log") or {}
    if debug.get("exists"):
        out.append(Finding(code="debug_log_present", severity="warning", title=f"wp-content/debug.log exists ({debug.get('bytes', 0)} bytes)",
                           detail="It can leak paths and errors if served publicly. Repair: repair_quarantine_debug_log."))
    admins = {a.get("user_login") for a in diag.get("admins") or []}
    before = {a.get("user_login") for a in (previous or {}).get("admins") or []}
    if previous and admins - before:
        out.append(Finding(code="new_admin_account", severity="critical",
                           title="New administrator account(s) since the last check: " + ", ".join(sorted(admins - before)),
                           detail="Check with the site owner; attackers often add an admin account."))
    return out


class ServerSideCapabilities:
    """WP-CLI diagnostics and repairs; mixed into the WordPress Security & Repair agent."""

    name: str

    async def _verify_http(self, ctx: AgentContext, site: Site) -> tuple[str, list[Finding], list[EventSpec]]:
        report = await make_checker(ctx, None).check(site.url, deep=False)
        findings: list[Finding] = []
        events: list[EventSpec] = []
        if report.status == "down":
            findings.append(Finding(code="site_down_after_repair", severity="critical", title=f"{site.name} does not load after the repair: "
                                    + "; ".join(f.title for f in report.findings if f.severity == "critical")[:300]))
        return report.status, findings, events

    async def _diagnose(self, ctx: AgentContext, site: Site, server: Server) -> dict[str, Any]:
        key = site.server_site_key
        async with ctx.ssh.open(ctx, server) as gate:
            diag: dict[str, Any] = {
                "core_version": (await gate.run("wp_core_version", site=key)).get("version"),
                "site_urls": await gate.run("wp_site_urls", site=key),
                "plugins": (await gate.run("wp_plugin_list", site=key)).get("plugins", []),
                "themes": (await gate.run("wp_theme_list", site=key)).get("themes", []),
                "admins": (await gate.run("wp_admins", site=key)).get("admins", []),
                "maintenance": await gate.run("wp_maintenance_status", site=key),
                "debug_log": await gate.run("wp_debug_log_status", site=key),
                "core_verify": await gate.run("wp_core_verify", site=key),
            }
        diag["at"] = datetime.now(UTC).isoformat()
        diag["server"] = server.name
        return diag

    @capability(risk=Risk.READ, description="Server-side diagnosis through WP-CLI (read-only): core checksums, home/siteurl, plugins and "
                "updates, themes, administrator accounts, maintenance mode, debug.log.", params=SiteParams, timeout_seconds=REPAIR_TIMEOUT,
                examples=("Diagnose site 3 on its server",))
    async def wp_diagnose(self, ctx: AgentContext, params: SiteParams) -> AgentResult:
        site, server = await linked(ctx, params.site_id)
        previous = dict(site.last_diagnosis or {})
        diag = await self._diagnose(ctx, site, server)
        findings = diagnosis_findings(diag, previous or None)
        diag["findings"] = [f.model_dump() for f in findings]
        site.last_diagnosis, site.last_diagnosis_at = diag, datetime.now(UTC)
        serious = [f for f in findings if f.severity != "info"]
        events = []
        if serious:
            worst = "critical" if any(f.severity == "critical" for f in serious) else "warning"
            events.append(EventSpec(kind="site.server_finding", severity=worst,
                                    title=f"{site.name} (server check): " + "; ".join(f.title for f in serious[:3]),
                                    detail={"site_id": site.id, "codes": [f.code for f in serious]}))
        summary = (f"{site.name} on {server.name}: WordPress {diag['core_version']}, {len(diag['plugins'])} plugin(s), "
                   f"{len(diag['admins'])} admin(s); " + ("; ".join(f.title for f in serious) if serious else "no problems found"))
        return AgentResult(summary=summary, data={"site_id": site.id, "diagnosis": diag}, findings=findings, events=events)

    async def _repair(self, ctx: AgentContext, site_id: int, title: str, steps, *, backup: bool = True) -> AgentResult:
        """Common shape: backup, change (steps), HTTP verification, report."""
        site, server = await linked(ctx, site_id)
        key = site.server_site_key
        notes: list[str] = []
        async with ctx.ssh.open(ctx, server) as gate:
            outcome = await steps(gate, key, notes, dry=True)
            if outcome == "nothing to do":
                return AgentResult(summary=f"{site.name}: {title} not needed ({'; '.join(notes) or 'already correct'}); nothing was changed.",
                                   data={"site_id": site.id, "changed": False})
            if backup:
                exported = await gate.run("wp_db_export", site=key)
                notes.insert(0, f"database backup: {exported.get('file')}")
            await steps(gate, key, notes, dry=False)
        status, findings, events = await self._verify_http(ctx, site)
        notes.append(f"site responds: {status}")
        ok = not findings
        events.append(EventSpec(
            kind="repair.completed" if ok else "repair.site_down",
            severity="info" if ok else "critical",
            title=f"{site.name}: {title} " + ("completed" if ok else "completed, but the site does not load"),
            detail={"site_id": site.id, "server": server.name, "approved_by": ctx.approved_by, "notes": notes},
        ))
        return AgentResult(summary=f"{site.name}: {title} done (approved by {ctx.approved_by}). " + "; ".join(notes),
                           data={"site_id": site.id, "changed": True, "notes": notes}, findings=findings, events=events)

    @capability(risk=Risk.CHANGE, description="Set the WordPress home and siteurl options back to the site's URL configured on the server "
                "(fixes the common redirect hack). Database backup first.", params=SiteParams, timeout_seconds=REPAIR_TIMEOUT)
    async def repair_restore_site_url(self, ctx: AgentContext, params: SiteParams) -> AgentResult:
        async def steps(gate, key, notes, dry):
            urls = await gate.run("wp_site_urls", site=key)
            if dry:
                if urls.get("matches"):
                    notes.append(f"home and siteurl already {urls.get('expected')}")
                    return "nothing to do"
                notes.append(f"was home {urls.get('home')}, siteurl {urls.get('siteurl')}")
                return "change"
            await gate.run("wp_set_site_urls", site=key)
            after = await gate.run("wp_site_urls", site=key)
            if not after.get("matches"):
                raise RuntimeError(f"home/siteurl still do not match after the change: {after}")
            notes.append(f"now {after.get('expected')}")
            return "done"

        return await self._repair(ctx, params.site_id, "restore home/siteurl", steps)

    @capability(risk=Risk.CHANGE, description="Deactivate one plugin (e.g. a suspicious or broken one). Database backup first; undo with "
                "repair_activate_plugin.", params=PluginParams, timeout_seconds=REPAIR_TIMEOUT)
    async def repair_deactivate_plugin(self, ctx: AgentContext, params: PluginParams) -> AgentResult:
        async def steps(gate, key, notes, dry):
            plugins = {p["name"]: p for p in (await gate.run("wp_plugin_list", site=key)).get("plugins", [])}
            plugin = plugins.get(params.slug)
            if plugin is None:
                raise ValueError(f"plugin {params.slug} is not installed")
            if dry:
                if plugin.get("status") != "active":
                    notes.append(f"{params.slug} is already {plugin.get('status')}")
                    return "nothing to do"
                return "change"
            await gate.run("wp_plugin_deactivate", site=key, slug=params.slug)
            after = {p["name"]: p for p in (await gate.run("wp_plugin_list", site=key)).get("plugins", [])}
            notes.append(f"{params.slug} is now {after.get(params.slug, {}).get('status')}")
            return "done"

        return await self._repair(ctx, params.site_id, f"deactivate plugin {params.slug}", steps)

    @capability(risk=Risk.CHANGE, description="Activate one installed plugin (undoes repair_deactivate_plugin). Database backup first.",
                params=PluginParams, timeout_seconds=REPAIR_TIMEOUT)
    async def repair_activate_plugin(self, ctx: AgentContext, params: PluginParams) -> AgentResult:
        async def steps(gate, key, notes, dry):
            plugins = {p["name"]: p for p in (await gate.run("wp_plugin_list", site=key)).get("plugins", [])}
            plugin = plugins.get(params.slug)
            if plugin is None:
                raise ValueError(f"plugin {params.slug} is not installed")
            if dry:
                if plugin.get("status") == "active":
                    notes.append(f"{params.slug} is already active")
                    return "nothing to do"
                return "change"
            await gate.run("wp_plugin_activate", site=key, slug=params.slug)
            notes.append(f"{params.slug} activated")
            return "done"

        return await self._repair(ctx, params.site_id, f"activate plugin {params.slug}", steps)

    @capability(risk=Risk.CHANGE, description="Update one plugin from WordPress.org. Database backup first.", params=PluginParams,
                timeout_seconds=REPAIR_TIMEOUT)
    async def repair_update_plugin(self, ctx: AgentContext, params: PluginParams) -> AgentResult:
        async def steps(gate, key, notes, dry):
            plugins = {p["name"]: p for p in (await gate.run("wp_plugin_list", site=key)).get("plugins", [])}
            plugin = plugins.get(params.slug)
            if plugin is None:
                raise ValueError(f"plugin {params.slug} is not installed")
            if dry:
                if plugin.get("update") != "available":
                    notes.append(f"{params.slug} {plugin.get('version')} is up to date")
                    return "nothing to do"
                notes.append(f"{params.slug} {plugin.get('version')} → {plugin.get('update_version')}")
                return "change"
            await gate.run("wp_plugin_update", site=key, slug=params.slug)
            after = {p["name"]: p for p in (await gate.run("wp_plugin_list", site=key)).get("plugins", [])}
            notes.append(f"now version {after.get(params.slug, {}).get('version')}")
            return "done"

        return await self._repair(ctx, params.site_id, f"update plugin {params.slug}", steps)

    @capability(risk=Risk.CHANGE, description="Reinstall one plugin's current version from WordPress.org (replaces tampered plugin files). "
                "Database backup first.", params=PluginParams, timeout_seconds=REPAIR_TIMEOUT)
    async def repair_reinstall_plugin(self, ctx: AgentContext, params: PluginParams) -> AgentResult:
        async def steps(gate, key, notes, dry):
            if dry:
                return "change"
            result = await gate.run("wp_plugin_reinstall", site=key, slug=params.slug)
            notes.append(f"reinstalled {params.slug} {result.get('version')}")
            return "done"

        return await self._repair(ctx, params.site_id, f"reinstall plugin {params.slug}", steps)

    @capability(risk=Risk.CHANGE, description="Reinstall the current WordPress version's core files from WordPress.org (replaces modified "
                "core files; wp-content untouched). Database backup first, checksums verified after.", params=SiteParams,
                timeout_seconds=REPAIR_TIMEOUT)
    async def repair_reinstall_core(self, ctx: AgentContext, params: SiteParams) -> AgentResult:
        async def steps(gate, key, notes, dry):
            if dry:
                return "change"
            result = await gate.run("wp_core_reinstall", site=key)
            verify = await gate.run("wp_core_verify", site=key)
            notes.append(f"reinstalled WordPress {result.get('version')}; checksums "
                         + {True: "verified", False: "STILL DO NOT MATCH", None: "could not be checked"}[verify.get("verified")])
            return "done"

        return await self._repair(ctx, params.site_id, "reinstall WordPress core", steps)

    @capability(risk=Risk.CHANGE, description="Switch WordPress maintenance mode on or off.", params=MaintenanceParams,
                timeout_seconds=REPAIR_TIMEOUT)
    async def repair_maintenance_mode(self, ctx: AgentContext, params: MaintenanceParams) -> AgentResult:
        async def steps(gate, key, notes, dry):
            active = (await gate.run("wp_maintenance_status", site=key)).get("active")
            if dry:
                if bool(active) == params.on:
                    notes.append(f"maintenance mode is already {'on' if active else 'off'}")
                    return "nothing to do"
                return "change"
            await gate.run("wp_maintenance", site=key, onoff="on" if params.on else "off")
            notes.append(f"maintenance mode {'on' if params.on else 'off'}")
            return "done"

        return await self._repair(ctx, params.site_id, f"maintenance mode {'on' if params.on else 'off'}", steps, backup=False)

    @capability(risk=Risk.CHANGE, description="Flush the object cache and rewrite rules.", params=SiteParams, timeout_seconds=REPAIR_TIMEOUT)
    async def repair_flush_caches(self, ctx: AgentContext, params: SiteParams) -> AgentResult:
        async def steps(gate, key, notes, dry):
            if dry:
                return "change"
            await gate.run("wp_cache_flush", site=key)
            await gate.run("wp_rewrite_flush", site=key)
            notes.append("object cache and rewrite rules flushed")
            return "done"

        return await self._repair(ctx, params.site_id, "flush caches", steps, backup=False)

    @capability(risk=Risk.CHANGE, description="Move wp-content/debug.log into the server's backup folder (nothing is deleted).",
                params=SiteParams, timeout_seconds=REPAIR_TIMEOUT)
    async def repair_quarantine_debug_log(self, ctx: AgentContext, params: SiteParams) -> AgentResult:
        async def steps(gate, key, notes, dry):
            if dry:
                status = await gate.run("wp_debug_log_status", site=key)
                if not status.get("exists"):
                    notes.append("there is no wp-content/debug.log")
                    return "nothing to do"
                return "change"
            moved = await gate.run("debug_log_quarantine", site=key)
            notes.append(f"debug.log moved to {moved.get('moved_to')}")
            return "done"

        return await self._repair(ctx, params.site_id, "quarantine debug.log", steps, backup=False)
