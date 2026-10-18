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
Only one repair or server-side check runs per site at a time. If a repair
fails or cannot confirm its result after it started changing the site, the
site is checked over HTTP and a critical repair.failed event says what
happened and where the backup is.

What WP-CLI reports about a compromised site can be influenced by that site's
own code (wp-config.php, must-use plugins and drop-ins run whenever WordPress
loads); core checksum verification runs before WordPress loads.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from pydantic import Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import events
from ..db import session_scope
from ..models import Server, Site
from ..ssh.client import GateTimeout
from ..ssh.gateway import GateFailed, GateRefused
from .base import AgentContext, AgentResult, EventSpec, Finding, Params, Risk, capability
from .monitoring.agent import make_checker

# Seconds for a whole repair or diagnosis: room for several slow server operations
# (backup, download, verification) one after the other.
REPAIR_TIMEOUT = 3000
STATUS_RANK = {"up": 0, "degraded": 1, "down": 2}


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


async def lock_site(ctx: AgentContext, site: Site) -> None:
    """One repair or server-side check per site at a time; held until the task ends (waits for the other one)."""
    await ctx.session.execute(text("SELECT pg_advisory_xact_lock(hashtext('cc-site-server-work'), :id)"), {"id": site.id})


async def site_approval_context(session: AsyncSession, site_id: int | None, *, lock: bool = False) -> dict[str, Any]:
    """What an approval for a change on this site is about (see BaseAgent.approval_context).

    "binding" pins the exact target: if the site is re-linked, renamed or its
    server changes after the request, the approval no longer matches and the
    change is refused (tasks.decide_approval, and again by the worker).
    """
    if site_id is None:
        return {}
    site = await session.get(Site, site_id, with_for_update=lock)
    if site is None:
        raise ValueError(f"no monitored site with id {site_id}")
    server = await session.get(Server, site.server_id, with_for_update=lock) if site.server_id else None
    production = site.environment == "production" or (server is not None and server.environment == "production")
    return {
        "target": f"site {site.name} ({site.url})" + (f" on server {server.name}" if server else ""),
        "environment": "production" if production else site.environment,
        "production": production,
        "confirm": site.name,
        "binding": {"site_id": site.id, "name": site.name, "url": site.url, "server_site_key": site.server_site_key or "",
                    "server": server_binding(server)},
    }


def server_binding(server: Server | None) -> dict[str, Any] | None:
    if server is None:
        return None
    return {"server_id": server.id, "name": server.name, "host": server.host, "port": server.port, "username": server.username}


DIAGNOSIS_PARTS = {
    "core_version": ("wp_core_version", lambda r: r.get("version")),
    "site_urls": ("wp_site_urls", lambda r: r),
    "plugins": ("wp_plugin_list", lambda r: r.get("plugins", [])),
    "themes": ("wp_theme_list", lambda r: r.get("themes", [])),
    "admins": ("wp_admins", lambda r: r.get("admins", [])),
    "super_admins": ("wp_admins", lambda r: r.get("super_admins", [])),
    "maintenance": ("wp_maintenance_status", lambda r: r),
    "debug_log": ("wp_debug_log_status", lambda r: r),
    "core_verify": ("wp_core_verify", lambda r: r),
}
# Kept from the previous diagnosis when they cannot be read, so "new since last time" still compares with something.
BASELINE_PARTS = ("admins", "super_admins", "plugins")
HIDDEN_CODE = ("must-use", "dropin")  # loaded even with --skip-plugins


def diagnosis_findings(diag: dict[str, Any], previous: dict[str, Any] | None) -> list[Finding]:
    out: list[Finding] = []
    errors = diag.get("errors") or {}
    if errors:
        out.append(Finding(code="server_check_incomplete", severity="warning",
                           title="Server-side check incomplete; could not check: " + ", ".join(sorted(errors)),
                           detail="; ".join(f"{k}: {v}" for k, v in sorted(errors.items()))[:1000]))
    urls = diag.get("site_urls") or {}
    if urls and not urls.get("matches"):
        expected = urls.get("expected")
        if urls.get("expected_siteurl") and urls.get("expected_siteurl") != expected:
            expected = f"{expected} and {urls.get('expected_siteurl')}"
        out.append(Finding(code="site_url_mismatch", severity="critical",
                           title=f"WordPress address options point elsewhere (home {urls.get('home')}, siteurl {urls.get('siteurl')}; "
                                 f"expected {expected})",
                           detail="Changed home/siteurl options are a classic redirect hack. Repair: repair_restore_site_url. "
                                  "If WordPress lives in its own folder, set that site's \"siteurl\" in the server's cc-gate configuration."))
    verify = diag.get("core_verify") or {}
    if verify.get("verified") is False:
        changed, extra = verify.get("files") or [], verify.get("extra_files") or []
        out.append(Finding(code="core_modified", severity="critical",
                           title=f"WordPress core does not match the official checksums ({len(changed)} changed or missing, "
                                 f"{len(extra)} unexpected file(s))",
                           detail="Modified or extra core files usually mean malware. Repair: repair_reinstall_core (replaces changed "
                                  "files; unexpected files must be checked and removed by hand). " + "; ".join((changed + extra)[:10])[:900]))
    elif verify.get("verified") is None and verify:
        out.append(Finding(code="core_verify_unavailable", severity="info", title="Core checksums could not be checked",
                           detail=str(verify.get("detail", ""))[:300]))
    plugins = diag.get("plugins") or []
    updates = [p for p in plugins if p.get("update") == "available"]
    if updates:
        out.append(Finding(code="plugin_update_available", severity="warning",
                           title="Plugin updates available: " + ", ".join(f"{p['name']} {p.get('version')} → {p.get('update_version')}" for p in updates[:6]),
                           detail="Repair: repair_update_plugin (one plugin per approval)."))
    if diag.get("maintenance", {}).get("active"):
        out.append(Finding(code="maintenance_on", severity="warning", title="WordPress maintenance mode is on",
                           detail="Left on after an interrupted update? Repair: repair_maintenance_mode with on=false."))
    debug = diag.get("debug_log") or {}
    if debug.get("exists") and debug.get("type", "file") != "file":
        out.append(Finding(code="debug_log_not_a_file", severity="warning", title=f"wp-content/debug.log is a {debug.get('type')}, not a file",
                           detail="A link there can expose another file (e.g. wp-config.php) at /wp-content/debug.log. Check it on the server."))
    elif debug.get("exists"):
        out.append(Finding(code="debug_log_present", severity="warning", title=f"wp-content/debug.log exists ({debug.get('bytes', 0)} bytes)",
                           detail="It can leak paths and errors if served publicly. Repair: repair_quarantine_debug_log."))
    if previous:
        def logins(d: dict[str, Any]) -> set[str]:
            return {a.get("user_login") for a in d.get("admins") or []} | set(d.get("super_admins") or [])

        new_admins = logins(diag) - logins(previous)
        if new_admins and "admins" not in errors:
            out.append(Finding(code="new_admin_account", severity="critical",
                               title="New administrator account(s) since the last check: " + ", ".join(sorted(new_admins)),
                               detail="Check with the site owner; attackers often add an admin account."))
        code = {p.get("name") for p in plugins if p.get("status") in HIDDEN_CODE}
        before = {p.get("name") for p in previous.get("plugins") or [] if p.get("status") in HIDDEN_CODE}
        if code - before and "plugins" not in errors:
            out.append(Finding(code="new_mu_plugin", severity="critical",
                               title="New must-use plugin or drop-in since the last check: " + ", ".join(sorted(code - before)),
                               detail="They run on every request and cannot be switched off from wp-admin; malware likes to hide there."))
    return out


class ServerSideCapabilities:
    """WP-CLI diagnostics and repairs; mixed into the WordPress Security & Repair agent."""

    name: str

    async def _http_status(self, ctx: AgentContext, site: Site) -> tuple[str, str]:
        """(status, the critical problems) from a quick HTTP check."""
        report = await make_checker(ctx, None).check(site.url, deep=False)
        return report.status, "; ".join(f.title for f in report.findings if f.severity == "critical")[:300]

    async def _diagnose(self, ctx: AgentContext, site: Site, server: Server, previous: dict[str, Any] | None = None) -> dict[str, Any]:
        """Each part on its own: a part the server cannot answer is listed under "errors", the rest still counts."""
        key = site.server_site_key
        diag: dict[str, Any] = {}
        errors: dict[str, str] = {}
        answers: dict[str, Any] = {}
        async with ctx.ssh.open(ctx, server) as gate:
            for part, (op, pick) in DIAGNOSIS_PARTS.items():
                if op not in answers:
                    try:
                        answers[op] = await gate.run(op, site=key)
                    except (GateFailed, GateRefused, GateTimeout) as exc:
                        answers[op] = exc
                if isinstance(answers[op], Exception):
                    errors[part] = str(answers[op])[:500]
                    continue
                try:
                    diag[part] = pick(answers[op])
                except (AttributeError, TypeError) as exc:  # an answer of the wrong shape
                    errors[part] = f"unexpected answer: {exc}"[:500]
        for part in BASELINE_PARTS:
            if part in errors and previous and part in previous:
                diag[part] = previous[part]
        diag.setdefault("plugins", [])
        diag.setdefault("admins", [])
        diag["errors"] = errors
        diag["at"] = datetime.now(UTC).isoformat()
        diag["server"] = server.name
        return diag

    @capability(risk=Risk.READ, description="Server-side diagnosis through WP-CLI (read-only): core checksums, home/siteurl, plugins and "
                "updates, themes, administrator accounts, maintenance mode, debug.log.", params=SiteParams, timeout_seconds=REPAIR_TIMEOUT,
                examples=("Diagnose site 3 on its server",))
    async def wp_diagnose(self, ctx: AgentContext, params: SiteParams) -> AgentResult:
        site, server = await linked(ctx, params.site_id)
        await lock_site(ctx, site)
        previous = dict(site.last_diagnosis or {})
        diag = await self._diagnose(ctx, site, server, previous or None)
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
        summary = (f"{site.name} on {server.name}: WordPress {diag.get('core_version') or '?'}, {len(diag['plugins'])} plugin(s), "
                   f"{len(diag['admins'])} admin(s); " + ("; ".join(f.title for f in serious) if serious else "no problems found"))
        return AgentResult(summary=summary, data={"site_id": site.id, "diagnosis": diag}, findings=findings, events=events)

    async def _repair(self, ctx: AgentContext, site_id: int, title: str, steps, *, backup: bool = True,
                      may_degrade: bool = False) -> AgentResult:
        """Common shape: check, backup, change (steps), confirm, HTTP verification, report.

        steps(gate, key, notes, dry) returns "nothing to do" or "change" when dry,
        and raises when the change did not take effect.
        """
        site, server = await linked(ctx, site_id)
        await lock_site(ctx, site)
        key = site.server_site_key
        notes: list[str] = []
        before, _ = await self._http_status(ctx, site)
        changing = False
        try:
            async with ctx.ssh.open(ctx, server) as gate:
                outcome = await steps(gate, key, notes, dry=True)
                if outcome == "nothing to do":
                    return AgentResult(summary=f"{site.name}: {title} not needed ({'; '.join(notes) or 'already correct'}); nothing was changed.",
                                       data={"site_id": site.id, "changed": False})
                if backup:
                    exported = await gate.run("wp_db_export", site=key)
                    notes.insert(0, f"database backup: {exported.get('file')}")
                changing = True
                await steps(gate, key, notes, dry=False)
        except BaseException as exc:
            if changing and not isinstance(exc, GateRefused):  # a refused request changed nothing
                await self._repair_failed(ctx, site, server, title, notes, exc)
            raise
        after, problems = await self._http_status(ctx, site)
        notes.append(f"site responds: {after}" + (f" (was {before})" if after != before else ""))
        findings: list[Finding] = []
        if STATUS_RANK.get(after, 2) > STATUS_RANK.get(before, 2) and not (may_degrade and after == "degraded"):
            findings.append(Finding(code="site_worse_after_repair", severity="critical",
                                    title=f"{site.name} is {after} after the repair (was {before})" + (f": {problems}" if problems else "")))
        ok = not findings
        result_events = [EventSpec(
            kind="repair.completed" if ok else "repair.site_down",
            severity="info" if ok else "critical",
            title=f"{site.name}: {title} " + ("completed" if ok else f"completed, but the site is now {after} (was {before})"),
            detail={"site_id": site.id, "server": server.name, "approved_by": ctx.approved_by, "notes": notes},
        )]
        return AgentResult(summary=f"{site.name}: {title} done (approved by {ctx.approved_by}). " + "; ".join(notes),
                           data={"site_id": site.id, "changed": True, "notes": notes}, findings=findings, events=result_events)

    async def _repair_failed(self, ctx: AgentContext, site: Site, server: Server, title: str, notes: list[str], exc: BaseException) -> None:
        """The change started but did not finish cleanly: look at the site and say so loudly (own transaction, survives the failure)."""
        status = "not checked"
        if isinstance(exc, Exception):
            try:
                async with asyncio.timeout(60):
                    status, problems = await self._http_status(ctx, site)
                    status += f" ({problems})" if problems else ""
            except Exception as check_exc:  # noqa: BLE001 - the report matters more than this check
                status = f"could not be checked ({check_exc.__class__.__name__})"
        error = f"{exc.__class__.__name__}: {exc}" if str(exc) else exc.__class__.__name__
        ctx.log(f"{title} failed after it started changing the site: {error}; site responds: {status}; " + "; ".join(notes), "error")

        async def emit() -> None:
            async with session_scope() as s:
                await events.emit(s, ctx.settings, kind="repair.failed", severity="critical", source=ctx.agent_name or "wordpress_security",
                                  title=f"{site.name}: {title} failed part-way; check the site (it responds: {status})",
                                  detail={"site_id": site.id, "server": server.name, "approved_by": ctx.approved_by, "error": error[:1000],
                                          "notes": notes, "site_status": status}, task_id=ctx.task_id)

        await asyncio.shield(emit())

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
            status = after.get(params.slug, {}).get("status")
            if status == "active":
                raise RuntimeError(f"{params.slug} is still active after deactivating it")
            notes.append(f"{params.slug} is now {status}")
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
            after = {p["name"]: p for p in (await gate.run("wp_plugin_list", site=key)).get("plugins", [])}
            if after.get(params.slug, {}).get("status") != "active":
                raise RuntimeError(f"{params.slug} is {after.get(params.slug, {}).get('status')} after activating it")
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
            after = {p["name"]: p for p in (await gate.run("wp_plugin_list", site=key)).get("plugins", [])}.get(params.slug)
            if after is None:
                raise RuntimeError(f"{params.slug} is no longer installed after the update")
            if after.get("version") == plugin.get("version") or after.get("update") == "available":
                raise RuntimeError(f"{params.slug} is still version {after.get('version')} after the update")
            notes.append(f"now version {after.get('version')}")
            return "done"

        return await self._repair(ctx, params.site_id, f"update plugin {params.slug}", steps)

    @capability(risk=Risk.CHANGE, description="Reinstall one plugin's current version from WordPress.org (replaces tampered plugin files). "
                "Database backup first.", params=PluginParams, timeout_seconds=REPAIR_TIMEOUT)
    async def repair_reinstall_plugin(self, ctx: AgentContext, params: PluginParams) -> AgentResult:
        async def steps(gate, key, notes, dry):
            if dry:
                plugins = {p["name"]: p for p in (await gate.run("wp_plugin_list", site=key)).get("plugins", [])}
                if params.slug not in plugins:
                    raise ValueError(f"plugin {params.slug} is not installed")
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
            if verify.get("verified") is False:
                left = (verify.get("files") or []) + (verify.get("extra_files") or [])
                raise RuntimeError(f"reinstalled WordPress {result.get('version')}, but core still does not match the checksums "
                                   "(files that should not exist are not removed by a reinstall; check and remove them by hand): "
                                   + "; ".join(left[:10]))
            notes.append(f"reinstalled WordPress {result.get('version')}; checksums "
                         + ("verified" if verify.get("verified") else "could not be checked"))
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
            if bool((await gate.run("wp_maintenance_status", site=key)).get("active")) != params.on:
                raise RuntimeError(f"maintenance mode is not {'on' if params.on else 'off'} after switching it")
            notes.append(f"maintenance mode {'on' if params.on else 'off'}")
            return "done"

        return await self._repair(ctx, params.site_id, f"maintenance mode {'on' if params.on else 'off'}", steps, backup=False,
                                  may_degrade=params.on)

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
            if (await gate.run("wp_debug_log_status", site=key)).get("exists"):
                notes.append("a new debug.log was written right away: WP_DEBUG_LOG is probably on in wp-config.php")
            return "done"

        return await self._repair(ctx, params.site_id, "quarantine debug.log", steps, backup=False)
