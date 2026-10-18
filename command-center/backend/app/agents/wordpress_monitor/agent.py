"""WordPress Monitor: read-only uptime, TLS and security-exposure monitoring."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, field_validator
from sqlalchemy import select

from ...models import Site, SiteCheck
from ..base import AgentContext, AgentResult, BaseAgent, EventSpec, Finding, Intent, NoParams, Params, Risk, capability
from .checks import SiteChecker, SiteReport

_LATEST_CACHE: dict[str, tuple[float, str | None]] = {}
_LATEST_TTL = 6 * 3600


def normalize_url(raw: str) -> str:
    raw = raw.strip()
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    parts = urlsplit(raw)
    if not parts.hostname:
        raise ValueError(f"not a URL: {raw!r}")
    netloc = parts.hostname.lower() + (f":{parts.port}" if parts.port else "")
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


class CheckSiteParams(Params):
    site_id: int = Field(description="ID of a monitored site")
    deep: bool = Field(True, description="Also run the WordPress exposure checks (a few extra GET requests)")


class CheckUrlParams(Params):
    url: str = Field(description="URL or domain to check, e.g. https://example.com")
    deep: bool = Field(True, description="Also run the WordPress exposure checks")

    @field_validator("url")
    @classmethod
    def _norm(cls, v: str) -> str:
        return normalize_url(v)


class ServerParams(Params):
    host: str = Field(max_length=253, description="Server hostname or public IP, e.g. server1.example.com")
    ports: list[int] = Field(default_factory=lambda: [80, 443], max_length=6,
                             description="TCP ports to test (default 80 and 443; keep the list short)")

    @field_validator("ports")
    @classmethod
    def _ports(cls, v: list[int]) -> list[int]:
        if any(not 1 <= p <= 65535 for p in v):
            raise ValueError("ports must be between 1 and 65535")
        return sorted(set(v))


class CheckAllParams(Params):
    deep: bool = Field(False, description="Run the exposure checks on every site (slower)")


async def latest_wordpress_version(ctx: AgentContext) -> str | None:
    if not ctx.settings.wp_core_version_check:
        return None
    url = ctx.settings.wp_version_api_url
    cached = _LATEST_CACHE.get(url)
    if cached and time.monotonic() - cached[0] < _LATEST_TTL:
        return cached[1]
    version: str | None = None
    try:
        result = await ctx.http.fetch(url, max_bytes=200_000)
        if result.status == 200:
            data = result.json()
            offers = data.get("offers") if isinstance(data, dict) else None
            if offers:
                version = str(offers[0].get("current") or "") or None
    except Exception as exc:  # noqa: BLE001 - the version check is optional
        ctx.log(f"could not fetch the latest WordPress version: {exc}", "warning")
    _LATEST_CACHE[url] = (time.monotonic(), version)
    return version


class WordPressMonitorAgent(BaseAgent):
    name = "wordpress_monitor"
    title = "WordPress & Hosting Monitor"
    description = (
        "Read-only monitoring of client websites and their servers: uptime, response time, TLS certificate expiry, "
        "WordPress version, publicly exposed risks (XML-RPC, user listing, debug.log, directory listing), and basic "
        "hosting reachability. Never logs in, never uses SSH or panel credentials, never changes a site or server."
    )

    def intents(self) -> list[Intent]:
        return [
            Intent(
                "check_url",
                (
                    r"\b(check|scan|monitor|test|inspect|look at|ping|verify)\b",
                    r"\bis\b.+\b(up|down|online|offline|working|broken)\b",
                    r"\b(uptime|ssl|tls|certificate|wordpress|wp)\b",
                ),
                lambda text, urls: [
                    {"url": u, "deep": True} for u in urls
                ]
                or None,
            ),
            Intent(
                "check_all_sites",
                (
                    r"\b(all|every|each)\b.{0,20}\b(web)?sites?\b",
                    r"\b(web)?sites?\b.{0,30}\b(status|health|up|down|check)\b",
                    r"\b(status|health) of\b.{0,20}\b(web)?sites?\b",
                ),
                lambda text, urls: None
                if urls
                else {"deep": bool(re.search(r"\b(deep|security|full|vulnerab|exposure)", text, re.I))},
            ),
            Intent(
                "sites_overview",
                (
                    r"\b(web)?sites?\b.{0,15}\b(overview|report|summary|list)\b",
                    r"\bwhich\b.{0,20}\b(web)?sites?\b",
                ),
                lambda text, urls: None if urls else {},
            ),
        ]

    # ------------------------------------------------------------------ helpers

    def _checker(self, ctx: AgentContext, latest: str | None) -> SiteChecker:
        return SiteChecker(ctx.http, ctx.settings, latest_wp_version=latest, retry_delay=ctx.settings.monitor_retry_delay_seconds)

    async def _previous_check(self, ctx: AgentContext, site_id: int, deep: bool) -> SiteCheck | None:
        rows = await ctx.session.scalars(
            select(SiteCheck).where(SiteCheck.site_id == site_id).order_by(SiteCheck.checked_at.desc()).limit(20)
        )
        for row in rows:
            if bool(row.details.get("deep")) == deep:
                return row
        return None

    async def _record(self, ctx: AgentContext, site: Site, deep: bool, report: SiteReport) -> list[EventSpec]:
        """Store a check result on the site's history and return the state-change events."""
        previous_status = site.last_status
        previous = await self._previous_check(ctx, site.id, deep)
        check = SiteCheck(
            site_id=site.id,
            task_id=ctx.task_id,
            status=report.status,
            http_status=report.http_status,
            response_ms=report.response_ms,
            final_url=report.final_url,
            tls_days_left=report.tls_days_left,
            tls_issuer=report.tls_issuer,
            is_wordpress=report.is_wordpress,
            wp_version=report.wp_version,
            findings=[f.model_dump() for f in report.findings],
            details=report.details,
        )
        ctx.session.add(check)
        await ctx.session.flush()
        site.last_status = report.status
        site.last_checked_at = check.checked_at
        site.last_check_id = check.id
        return self._change_events(site, previous_status, previous, report)

    @staticmethod
    def _change_events(site: Site, previous_status: str, previous: SiteCheck | None, report: SiteReport) -> list[EventSpec]:
        out: list[EventSpec] = []
        detail = {"site_id": site.id, "site": site.name, "url": site.url, "status": report.status}
        if report.status != previous_status:
            if report.status == "down":
                out.append(EventSpec(kind="site.down", severity="critical", title=f"{site.name} is DOWN: {_first(report)}", detail=detail))
            elif report.status == "degraded":
                out.append(EventSpec(kind="site.degraded", severity="warning", title=f"{site.name} is degraded: {_first(report)}", detail=detail))
            elif previous_status in ("down", "degraded"):
                out.append(EventSpec(kind="site.recovered", severity="info", title=f"{site.name} has recovered", detail=detail))
        old_codes = {f.get("code") for f in (previous.findings if previous else [])}
        new = [f for f in report.findings if f.severity != "info" and f.code not in old_codes]
        # Availability problems are already covered by the status-change event.
        new = [f for f in new if f.code not in ("fetch_failed", "page_problem", "http_error", "page_notice", "slow_response")]
        if new:
            worst = "critical" if any(f.severity == "critical" for f in new) else "warning"
            out.append(
                EventSpec(
                    kind="site.finding",
                    severity=worst,
                    title=f"{site.name}: {len(new)} new issue(s) - " + "; ".join(f.title for f in new[:3]),
                    detail={**detail, "findings": [f.model_dump() for f in new]},
                )
            )
        return out

    # ------------------------------------------------------------- capabilities

    @capability(
        risk=Risk.READ,
        description="Read-only hosting check of a server (e.g. a CWP server): DNS, a single TCP connect per listed port, "
        "and the TLS certificate on 443. Never logs in; no SSH, no panel credentials.",
        params=ServerParams,
        examples=("Check server1.example.com",),
    )
    async def check_server(self, ctx: AgentContext, params: ServerParams) -> AgentResult:
        from .checks import probe_tls

        host = params.host.strip().lower()
        await ctx.http.check_target(host)  # refuses private and internal addresses
        findings = []
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None)
            addresses = sorted({i[4][0] for i in infos})
        except OSError as exc:
            addresses = []
            findings.append(Finding(code="dns_failed", severity="critical", title=f"{host} does not resolve: {exc}"))
        ports: dict[int, str] = {}
        for port in params.ports if addresses else []:
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5)
                writer.close()
                ports[port] = "open"
            except (OSError, TimeoutError):
                ports[port] = "closed or filtered"
                severity = "critical" if port in (80, 443) else "warning"
                findings.append(Finding(code="port_unreachable", severity=severity, title=f"{host}:{port} did not accept a connection"))
        tls = None
        if ports.get(443) == "open":
            info = await probe_tls(host, 443, ctx.settings.monitor_timeout_seconds)
            tls = {"days_left": info.days_left, "issuer": info.issuer, "error": info.error}
            if info.error and not info.error.startswith("probe failed"):
                findings.append(Finding(code="tls_invalid", severity="critical", title=f"TLS certificate problem: {info.error}"))
            elif info.days_left is not None and info.days_left <= 14:
                findings.append(Finding(code="tls_expiring", severity="critical" if info.days_left <= 7 else "warning",
                                        title=f"TLS certificate expires in {info.days_left} days"))
        ok = not any(f.severity == "critical" for f in findings)
        summary = f"{host}: " + ("reachable" if ok else "PROBLEM") + f"; addresses {', '.join(addresses) or 'none'}; " + \
            ", ".join(f"{p} {state}" for p, state in ports.items())
        ctx.log(summary)
        return AgentResult(summary=summary, data={"host": host, "addresses": addresses, "ports": ports, "tls": tls}, findings=findings)

    @capability(
        risk=Risk.READ,
        description="Check one monitored site now and record the result.",
        params=CheckSiteParams,
        examples=("Check site 3",),
    )
    async def check_site(self, ctx: AgentContext, params: CheckSiteParams) -> AgentResult:
        site = await ctx.session.get(Site, params.site_id)
        if site is None:
            raise ValueError(f"no monitored site with id {params.site_id}")
        latest = await latest_wordpress_version(ctx) if params.deep else None
        report = await self._checker(ctx, latest).check(site.url, deep=params.deep)
        events = await self._record(ctx, site, params.deep, report)
        ctx.log(report.summary())
        return AgentResult(
            summary=report.summary(),
            data={"site_id": site.id, "report": _report_data(report)},
            findings=report.findings,
            events=events,
        )

    @capability(
        risk=Risk.READ,
        description="Check any URL or domain now. Uses the monitored site's history if the URL is registered.",
        params=CheckUrlParams,
        examples=("Is example.com up?", "Check https://example.com"),
    )
    async def check_url(self, ctx: AgentContext, params: CheckUrlParams) -> AgentResult:
        site = await ctx.session.scalar(select(Site).where(Site.url == params.url))
        if site is not None:
            return await self.check_site(ctx, CheckSiteParams(site_id=site.id, deep=params.deep))
        latest = await latest_wordpress_version(ctx) if params.deep else None
        report = await self._checker(ctx, latest).check(params.url, deep=params.deep)
        ctx.log(report.summary())
        return AgentResult(
            summary=report.summary() + " (not a monitored site; result not stored in site history)",
            data={"report": _report_data(report)},
            findings=report.findings,
        )

    @capability(
        risk=Risk.READ,
        description="Check every enabled monitored site and record the results.",
        params=CheckAllParams,
        examples=("Check all websites", "Run a security scan on every site"),
    )
    async def check_all_sites(self, ctx: AgentContext, params: CheckAllParams) -> AgentResult:
        sites = (await ctx.session.scalars(select(Site).where(Site.enabled.is_(True)).order_by(Site.id))).all()
        if not sites:
            return AgentResult(summary="No sites are being monitored yet. Add one on the Websites page.")
        latest = await latest_wordpress_version(ctx) if params.deep else None
        checker = self._checker(ctx, latest)
        sem = asyncio.Semaphore(4)

        async def run(site: Site) -> SiteReport:
            async with sem:
                return await checker.check(site.url, deep=params.deep)

        reports = await asyncio.gather(*(run(s) for s in sites))
        events: list[EventSpec] = []
        rows: list[dict[str, Any]] = []
        for site, report in zip(sites, reports, strict=True):
            events.extend(await self._record(ctx, site, params.deep, report))
            rows.append({"site_id": site.id, "name": site.name, "url": site.url, "status": report.status, "summary": report.summary()})
            ctx.log(report.summary())
        counts = {s: sum(1 for r in reports if r.status == s) for s in ("up", "degraded", "down")}
        summary = f"Checked {len(sites)} site(s): {counts['up']} up, {counts['degraded']} degraded, {counts['down']} down."
        problems = [r for r in rows if r["status"] != "up"]
        if problems:
            summary += " Problems: " + "; ".join(f"{r['name']} ({r['status']})" for r in problems[:5])
        findings = [f for r in reports for f in r.findings if f.severity != "info"]
        return AgentResult(summary=summary, data={"counts": counts, "sites": rows}, findings=findings, events=events)

    @capability(
        risk=Risk.READ,
        description="Summarise the latest known status and open issues of every monitored site (no new requests).",
        params=NoParams,
        examples=("Give me a website overview", "Which sites have problems?"),
    )
    async def sites_overview(self, ctx: AgentContext, params: NoParams) -> AgentResult:
        sites = (await ctx.session.scalars(select(Site).order_by(Site.id))).all()
        rows = []
        for site in sites:
            check = await ctx.session.get(SiteCheck, site.last_check_id) if site.last_check_id else None
            issues = [f for f in (check.findings if check else []) if f.get("severity") != "info"]
            rows.append(
                {
                    "site_id": site.id,
                    "name": site.name,
                    "url": site.url,
                    "enabled": site.enabled,
                    "status": site.last_status,
                    "last_checked_at": site.last_checked_at.isoformat() if site.last_checked_at else None,
                    "wp_version": check.wp_version if check else "",
                    "issues": [i["title"] for i in issues],
                }
            )
        if not rows:
            return AgentResult(summary="No sites are being monitored yet.")
        lines = [f"{r['name']}: {r['status']}" + (f" ({len(r['issues'])} issue(s))" if r["issues"] else "") for r in rows]
        return AgentResult(summary=f"{len(rows)} monitored site(s). " + "; ".join(lines[:10]), data={"sites": rows})


def _first(report: SiteReport) -> str:
    for f in report.findings:
        if f.severity != "info":
            return f.title
    return report.status


def _report_data(report: SiteReport) -> dict[str, Any]:
    return {
        "url": report.url,
        "status": report.status,
        "http_status": report.http_status,
        "response_ms": report.response_ms,
        "final_url": report.final_url,
        "tls_days_left": report.tls_days_left,
        "tls_issuer": report.tls_issuer,
        "is_wordpress": report.is_wordpress,
        "wp_version": report.wp_version,
        "details": report.details,
    }
