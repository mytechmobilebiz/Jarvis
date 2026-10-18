"""WordPress Security & Repair: security scans and reports, WP-CLI diagnostics, and approval-based repairs.

Scans are read-only HTTP checks (the same checker the monitoring agent uses, in
deep mode). Server-side diagnostics and repairs go through the restricted SSH
gate (app/ssh) and are added to these capabilities when a site is linked to a
server.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from pydantic import Field
from sqlalchemy import select

from .. import __version__, uptime
from ..models import Site, SiteCheck
from .base import AgentContext, AgentResult, ArtifactSpec, BaseAgent, EventSpec, Finding, Intent, NoParams, Params, Risk, capability
from .monitoring.agent import latest_wordpress_version, make_checker, record_check
from .monitoring.checks import SiteReport
from .wp_server import ServerSideCapabilities, site_approval_context

# What to do about each finding, and which approval-based repair (if any) applies.
REMEDIATION: dict[str, tuple[str, str | None]] = {
    "redirect_offsite": ("If the site was not meant to move, treat it as compromised: check the home/siteurl options, "
                         ".htaccess and recently changed files.", "repair_restore_site_url"),
    "conditional_redirect": ("Malware that hides from the owner. Check .htaccess, wp-config.php, active plugins and the "
                             "theme's functions.php; verify the core files.", "repair_reinstall_core"),
    "cloaked_spam": ("Spam shown only to search engines (SEO spam hack). Verify core files, review active plugins and the "
                     "theme, then ask Google to re-crawl.", "repair_reinstall_core"),
    "js_redirect_offsite": ("Find and remove the injected script (theme files, widgets, posts or the database).", "repair_reinstall_core"),
    "meta_refresh_offsite": ("Find and remove the injected refresh tag (theme header, widgets or the database).", None),
    "hidden_iframe": ("Find and remove the injected iframe (theme files, widgets or the database).", None),
    "obfuscated_script": ("Identify which plugin or theme outputs the obfuscated code; remove it if nobody added it.", None),
    "hidden_spam": ("Remove the hidden spam links from posts, widgets and theme files.", None),
    "spam_terms": ("Expected for some businesses; otherwise look for injected spam.", None),
    "new_external_script": ("Confirm someone added the new script (plugin, analytics, chat widget).", None),
    "wp_outdated": ("Update WordPress after a backup.", None),
    "xmlrpc_enabled": ("Disable XML-RPC unless Jetpack or the mobile app needs it.", None),
    "user_enumeration": ("Hide the REST API user list from anonymous visitors (security plugin setting).", None),
    "readme_exposed": ("Delete or block readme.html.", None),
    "debug_log_exposed": ("Turn off WP_DEBUG_LOG and move the public debug.log out of the web root.", "repair_quarantine_debug_log"),
    "directory_listing": ("Turn off directory listing (Options -Indexes).", None),
    "no_https": ("Install a certificate (CWP AutoSSL / Let's Encrypt) and redirect HTTP to HTTPS.", None),
    "no_https_redirect": ("Redirect every http:// request to https://.", None),
    "tls_expired": ("Renew the certificate now.", None),
    "tls_expiring": ("Renew the certificate (check that AutoSSL renewal works).", None),
    "tls_invalid": ("Fix the certificate (wrong host name, self-signed or incomplete chain).", None),
    "tls_old_protocol": ("Disable TLS 1.0 and 1.1 on the web server.", None),
    "php_eol": ("Move the site to a supported PHP version (8.2 or later) after testing.", None),
    "redirect_downgrade": ("Make every redirect stay on https://.", None),
    "fetch_failed": ("Check the server, DNS and the domain's registration.", None),
    "page_problem": ("Check the hosting account, database and PHP error log.", None),
    "http_error": ("Check the server and PHP error logs.", None),
    # From the server-side (WP-CLI) diagnosis:
    "site_url_mismatch": ("Set home/siteurl back to the site's address.", "repair_restore_site_url"),
    "core_modified": ("Replace the modified core files with the official ones.", "repair_reinstall_core"),
    "plugin_update_available": ("Update the plugin after a backup.", "repair_update_plugin"),
    "maintenance_on": ("Switch maintenance mode off if no update is running.", "repair_maintenance_mode"),
    "debug_log_present": ("Move debug.log out of the web root and turn off WP_DEBUG_LOG.", "repair_quarantine_debug_log"),
    "new_admin_account": ("Confirm the new administrator with the site owner; remove it if unknown.", None),
    "site_down_after_repair": ("Undo the last repair (for a plugin: repair_activate_plugin) and check the error log.", None),
}
SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


class ScanParams(Params):
    site_id: int = Field(description="ID of a monitored site")


class ReportParams(Params):
    site_id: int | None = Field(None, description="One site; leave out for a report on every site")


def rating(findings: list[dict[str, Any]]) -> str:
    severities = {f.get("severity") for f in findings}
    if "critical" in severities:
        return "AT RISK"
    if "warning" in severities:
        return "NEEDS ATTENTION"
    return "GOOD"


def _fmt_pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:g}%"


class WordPressSecurityAgent(ServerSideCapabilities, BaseAgent):
    name = "wordpress_security"
    title = "WordPress Security & Repair"
    description = (
        "Security scans of client WordPress sites (exposures, suspicious redirects, cloaking, injected scripts and spam, TLS), "
        "security reports, server-side diagnostics through WP-CLI, and repairs that run only after approval, through a "
        "restricted server account and an allowlist of commands."
    )
    default_schedules = [
        {
            "name": "wordpress-security-report",
            "description": "Weekly security report for every site (Monday morning).",
            "agent": "wordpress_security",
            "capability": "security_report",
            "params": {},
            "cron": "43 6 * * 1",
        }
    ]

    async def approval_context(self, session: Any, capability: str, params: dict[str, Any]) -> dict[str, Any]:
        return await site_approval_context(session, params.get("site_id"))

    def intents(self) -> list[Intent]:
        return [
            Intent("security_report", (r"\bsecurity\b.{0,20}\breport\b", r"\breport\b.{0,20}\bsecurity\b"), lambda text, urls: {}, weight=3),
            Intent("security_scan_all", (r"\b(security|malware|hack(ed)?|redirects?)\b.{0,30}\b(scan|check)\b.{0,30}\b(all|every|each)\b",
                                         r"\b(scan|check)\b.{0,30}\b(all|every|each)\b.{0,30}\b(security|malware|hack(ed)?)\b"),
                   lambda text, urls: None if urls else {}, weight=2),
        ]

    # ------------------------------------------------------------------ scans

    async def _scan(self, ctx: AgentContext, sites: list[Site]) -> tuple[list[tuple[Site, SiteReport]], list[EventSpec]]:
        latest = await latest_wordpress_version(ctx)
        checker = make_checker(ctx, latest)
        sem = asyncio.Semaphore(4)

        async def run(site: Site) -> SiteReport:
            async with sem:
                return await checker.check(site.url, deep=True)

        reports = await asyncio.gather(*(run(s) for s in sites))
        events: list[EventSpec] = []
        for site, report in zip(sites, reports, strict=True):
            events.extend(await record_check(ctx, site, True, report))
            ctx.log(report.summary())
        return list(zip(sites, reports, strict=True)), events

    @capability(
        risk=Risk.READ,
        description="Security scan of one site: exposures, suspicious redirects and cloaking, injected scripts or spam, TLS "
        "(read-only GET requests).",
        params=ScanParams,
        examples=("Run a security scan on site 3",),
    )
    async def security_scan(self, ctx: AgentContext, params: ScanParams) -> AgentResult:
        site = await ctx.session.get(Site, params.site_id)
        if site is None:
            raise ValueError(f"no monitored site with id {params.site_id}")
        [(site, report)], events = await self._scan(ctx, [site])
        findings = list(report.findings)
        server_note = ""
        if site.server_id and site.server_site_key and ctx.ssh is not None:
            from .wp_server import SiteParams

            try:
                diag = await self.wp_diagnose(ctx, SiteParams(site_id=site.id))
                findings.extend(diag.findings)
                events.extend(diag.events)
                server_note = " Server-side check done."
            except Exception as exc:  # noqa: BLE001 - the HTTP scan stands on its own
                findings.append(Finding(code="server_check_failed", severity="warning", title=f"Server-side check failed: {exc}"[:300]))
        serious = [f for f in findings if f.severity != "info"]
        summary = f"{site.name}: {rating([f.model_dump() for f in findings])}, {len(serious)} issue(s). " + report.summary() + server_note
        return AgentResult(summary=summary, data={"site_id": site.id, "status": report.status}, findings=findings, events=events)

    @capability(
        risk=Risk.READ,
        description="Security scan of every enabled site (read-only GET requests). Runs nightly.",
        params=NoParams,
        examples=("Run a security scan on every site",),
    )
    async def security_scan_all(self, ctx: AgentContext, params: NoParams) -> AgentResult:
        sites = (await ctx.session.scalars(select(Site).where(Site.enabled.is_(True)).order_by(Site.id))).all()
        if not sites:
            return AgentResult(summary="No sites are being monitored yet. Add one on the Websites page.")
        pairs, events = await self._scan(ctx, list(sites))
        ratings = {s.name: rating([f.model_dump() for f in r.findings]) for s, r in pairs}
        at_risk = [n for n, v in ratings.items() if v == "AT RISK"]
        summary = f"Security scan of {len(sites)} site(s): {len(at_risk)} at risk" + (f" ({', '.join(at_risk[:5])})" if at_risk else "") + "."
        findings = [f for _, r in pairs for f in r.findings if f.severity != "info"]
        return AgentResult(summary=summary, data={"ratings": ratings}, findings=findings, events=events)

    # ---------------------------------------------------------------- reports

    async def _latest_check(self, ctx: AgentContext, site: Site) -> SiteCheck | None:
        rows = (
            await ctx.session.scalars(
                select(SiteCheck).where(SiteCheck.site_id == site.id).order_by(SiteCheck.checked_at.desc()).limit(50)
            )
        ).all()
        return next((r for r in rows if r.details.get("deep")), rows[0] if rows else None)

    async def _site_section(self, ctx: AgentContext, site: Site, stats: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
        check = await self._latest_check(ctx, site)
        findings = sorted(check.findings if check else [], key=lambda f: SEVERITY_ORDER.get(f.get("severity", "info"), 3))
        issues = [f for f in findings if f.get("severity") != "info"]
        server_issues = [f for f in (site.last_diagnosis or {}).get("findings") or [] if f.get("severity") != "info"]
        overall = rating(findings + server_issues) if check else "NOT SCANNED"
        lines = [f"## {site.name}", "", f"{site.url} · **{overall}**", ""]
        if check is None:
            lines += ["No checks yet. Run a security scan for this site.", ""]
            return "\n".join(lines), {"site_id": site.id, "rating": overall, "critical": 0, "warnings": 0}
        details = check.details or {}
        age_h = (datetime.now(UTC) - check.checked_at).total_seconds() / 3600
        tls = details.get("tls") or {}
        tls_text = (
            f"{check.tls_days_left} days left" + (f", expires {tls.get('expires_at', '')[:10]}" if tls.get("expires_at") else "")
            + (f", {check.tls_issuer}" if check.tls_issuer else "") + (f", {tls.get('protocol')}" if tls.get("protocol") else "")
            if check.tls_days_left is not None else "not served over HTTPS or not inspected"
        )
        wp = "not detected"
        if check.is_wordpress:
            wp = check.wp_version or "version hidden"
            if details.get("latest_wp_version"):
                wp += f" (current release {details['latest_wp_version']})"
        uptime_text = " · ".join(f"{k} {_fmt_pct((stats or {}).get(k))}" for k in ("24h", "7d", "30d"))
        lines += [
            "| | |", "|---|---|",
            f"| Status | {check.status}" + (f" (HTTP {check.http_status}, {check.response_ms} ms)" if check.http_status else "") + " |",
            f"| Uptime | {uptime_text} |",
            f"| TLS certificate | {tls_text} |",
            f"| WordPress | {wp} |",
            f"| Checked | {check.checked_at:%Y-%m-%d %H:%M} UTC ({'deep scan' if details.get('deep') else 'quick check'}) |",
            "",
        ]
        if age_h > 48 or not details.get("deep"):
            lines += ["_These results are not from a recent security scan; run one for current results._", ""]
        if issues:
            lines += [f"### Issues ({len(issues)})", "", "| Severity | Issue | What to do | Repair (needs approval) |", "|---|---|---|---|"]
            for f in issues:
                todo, repair = REMEDIATION.get(f.get("code", ""), ("Review this finding.", None))
                lines.append(f"| {f.get('severity')} | {f.get('title', '').replace('|', '/')} | {todo} | {repair or '-'} |")
            lines.append("")
        else:
            lines += ["No issues found.", ""]
        redirect = details.get("redirect_check") or {}
        if redirect:
            chain = redirect.get("chain") or []
            lines += ["### Redirects and page integrity", ""]
            if isinstance(chain, list) and len(chain) > 1:
                lines.append("- Redirect chain: " + " → ".join(str(u) for u in chain))
            else:
                lines.append("- No redirects for ordinary visitors.")
            for label, trace in (redirect.get("variants") or {}).items():
                target = trace.get("offsite_target")
                lines.append(f"- Visitors {label}: " + (f"**redirected to {target}**" if target else "same site as ordinary visitors"))
            scripts = details.get("external_scripts")
            if scripts is not None:
                lines.append("- Third-party script domains: " + (", ".join(scripts) if scripts else "none"))
            lines.append("")
        diag = site.last_diagnosis or {}
        if diag:
            server_findings = [f for f in diag.get("findings") or [] if f.get("severity") != "info"]
            verify = diag.get("core_verify") or {}
            lines += [f"### Server-side checks (WP-CLI on {diag.get('server', 'the server')}, {str(diag.get('at', ''))[:16].replace('T', ' ')} UTC)", "",
                      f"- WordPress {diag.get('core_version', '?')}; core files: "
                      + {True: "match the official checksums", False: "**modified**", None: "could not be checked"}.get(verify.get("verified"), "not checked"),
                      "- home/siteurl: " + ("correct" if (diag.get("site_urls") or {}).get("matches") else
                                             f"**{(diag.get('site_urls') or {}).get('home')}** (expected {(diag.get('site_urls') or {}).get('expected')})"),
                      f"- Plugins: {len(diag.get('plugins') or [])}, updates available: "
                      + (", ".join(p["name"] for p in diag.get("plugins") or [] if p.get("update") == "available") or "none"),
                      f"- Administrator accounts: {len(diag.get('admins') or [])}", ""]
            if server_findings:
                lines += ["| Severity | Issue | What to do | Repair (needs approval) |", "|---|---|---|---|"]
                for f in server_findings:
                    todo, repair = REMEDIATION.get(f.get("code", ""), ("Review this finding.", None))
                    lines.append(f"| {f.get('severity')} | {str(f.get('title', '')).replace('|', '/')} | {todo} | {repair or '-'} |")
                lines.append("")
        elif site.server_id:
            lines += ["_Linked to a server, but no server-side check yet: run wp_diagnose or a security scan._", ""]
        incidents = await uptime.incidents(ctx.session, site.id)
        if incidents:
            lines += ["### Downtime in the last 30 days", ""]
            for inc in incidents[-10:]:
                lines.append(f"- {inc['start'][:16].replace('T', ' ')} UTC, {inc['minutes']} min" + (" (ongoing)" if not inc["end"] else "")
                             + f": {inc['reason']}")
            lines.append("")
        summary = {
            "site_id": site.id, "rating": overall,
            "critical": sum(1 for f in issues + server_issues if f.get("severity") == "critical"),
            "warnings": sum(1 for f in issues + server_issues if f.get("severity") == "warning"),
        }
        return "\n".join(lines), summary

    @capability(
        risk=Risk.DRAFT,
        description="Write a security report (one site, or every site) from the latest scans, uptime history and incidents.",
        params=ReportParams,
        examples=("Write a security report for all sites",),
    )
    async def security_report(self, ctx: AgentContext, params: ReportParams) -> AgentResult:
        if params.site_id is not None:
            site = await ctx.session.get(Site, params.site_id)
            if site is None:
                raise ValueError(f"no monitored site with id {params.site_id}")
            sites = [site]
        else:
            sites = list((await ctx.session.scalars(select(Site).where(Site.enabled.is_(True)).order_by(Site.name))).all())
        if not sites:
            return AgentResult(summary="No sites are being monitored yet; no report written.")
        stats = await uptime.uptime_stats(ctx.session)
        sections, rows = [], []
        for site in sites:
            text, row = await self._site_section(ctx, site, stats.get(site.id))
            sections.append(text)
            rows.append({**row, "name": site.name})
        now = datetime.now(UTC)
        single = len(sites) == 1
        title = f"Security report - {sites[0].name}" if single else f"Security report - {len(sites)} sites"
        head = [f"# {title}", "", f"_Generated {now:%Y-%m-%d %H:%M} UTC by the Infinite AI Command Center {__version__}. "
                "Findings come from read-only checks; nothing was changed on any site._", ""]
        if not single:
            head += ["| Site | Rating | Critical | Warnings |", "|---|---|---|---|"]
            head += [f"| {r['name']} | {r['rating']} | {r['critical']} | {r['warnings']} |" for r in rows]
            head.append("")
        content = "\n".join(head + sections)
        at_risk = [r["name"] for r in rows if r["rating"] == "AT RISK"]
        summary = f"{title}: {len(at_risk)} at risk" + (f" ({', '.join(at_risk[:5])})" if at_risk else "") + "."
        return AgentResult(
            summary=summary,
            data={"sites": rows},
            artifacts=[ArtifactSpec(kind="security_report", title=title, content=content, data={"sites": rows})],
        )
