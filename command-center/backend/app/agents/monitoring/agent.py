"""Website & Infrastructure Monitoring: read-only uptime, TLS, redirect and exposure checks, and server reachability."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, field_validator
from sqlalchemy import select

from ... import events, uptime
from ...models import Site, SiteCheck
from ..base import AgentContext, AgentResult, ArtifactSpec, BaseAgent, EventSpec, Finding, Intent, NoParams, Params, Risk, capability
from ..http import BlockedTarget, open_public_connection
from . import connectivity, findings as open_issues
from .checks import SiteChecker, SiteReport

_LATEST_CACHE: dict[str, tuple[float, str | None]] = {}
_LATEST_TTL = 6 * 3600
# Each site gets its own deadline (a multiple of MONITOR_TIMEOUT_SECONDS; a deep
# check makes about twenty requests), so one slow site cannot use up a sweep.
SITE_DEADLINE_FACTOR = {False: 6, True: 15}
SWEEP_TIMEOUT_SECONDS = 1800
PARTIAL_CHECKS_SEEN = 50  # see _reported_since


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


class UptimeParams(Params):
    site_id: int | None = Field(None, description="One site; leave out for every site")


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


class MonitoringAgent(BaseAgent):
    name = "monitoring"
    title = "Website & Infrastructure Monitoring"
    description = (
        "Read-only monitoring of client websites and their servers: uptime and uptime history, response time, TLS "
        "certificate validity and expiry, suspicious redirects, WordPress version and publicly exposed risks, and "
        "network reachability of servers (DNS, ports, TLS). Never logs in and never changes a site or server. A site that "
        "does not answer at all is recorded as down only after a control check shows the Command Center itself is online."
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
                "uptime_report",
                (r"\buptime\b.{0,20}\b(report|history|percent|%|stats?)\b", r"\b(downtime|outages?|incidents?)\b"),
                lambda text, urls: None if urls else {},
                weight=2,
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
                _, writer = await asyncio.wait_for(open_public_connection(host, port, ctx.http.allowed_private), timeout=5)
                writer.close()
                ports[port] = "open"
            except (OSError, TimeoutError, BlockedTarget):
                ports[port] = "closed or filtered"
                severity = "critical" if port in (80, 443) else "warning"
                findings.append(Finding(code="port_unreachable", severity=severity, title=f"{host}:{port} did not accept a connection"))
        tls = None
        if ports.get(443) == "open":
            info = await probe_tls(host, 443, ctx.settings.monitor_timeout_seconds, allowed_private=ctx.http.allowed_private)
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
        timeout_seconds=SWEEP_TIMEOUT_SECONDS,
    )
    async def check_site(self, ctx: AgentContext, params: CheckSiteParams) -> AgentResult:
        site = await ctx.session.get(Site, params.site_id)
        if site is None:
            raise ValueError(f"no monitored site with id {params.site_id}")
        # Recorded, with its events emitted, as soon as it is done (see check_sites).
        checked, failed = await check_sites(ctx, [site], deep=params.deep)
        if failed:
            raise RuntimeError(f"the check of {site.name} failed: {failed[0][1]}")
        report = checked[0][1]
        return AgentResult(summary=report.summary(), data={"site_id": site.id, "report": _report_data(report)}, findings=report.findings)

    @capability(
        risk=Risk.READ,
        description="Check any URL or domain now. Uses the monitored site's history if the URL is registered.",
        params=CheckUrlParams,
        examples=("Is example.com up?", "Check https://example.com"),
        timeout_seconds=SWEEP_TIMEOUT_SECONDS,
    )
    async def check_url(self, ctx: AgentContext, params: CheckUrlParams) -> AgentResult:
        site = await ctx.session.scalar(select(Site).where(Site.url == params.url))
        if site is not None:
            return await self.check_site(ctx, CheckSiteParams(site_id=site.id, deep=params.deep))
        latest = await latest_wordpress_version(ctx) if params.deep else None
        deadline = ctx.settings.monitor_timeout_seconds * SITE_DEADLINE_FACTOR[params.deep]
        report = SiteReport(url=params.url)
        try:
            await asyncio.wait_for(make_checker(ctx, latest).check(params.url, deep=params.deep, report=report), deadline)
        except TimeoutError:
            _cut_short(report, deadline)
        if connectivity.is_network_failure(report):
            guard = connectivity.ConnectivityGuard(ctx.http, ctx.settings)
            if not await guard.online(failed_at=time.monotonic()):
                ctx.log(f"{params.url}: {_first(report)}; {connectivity.OFFLINE_REASON}", "warning")
                return AgentResult(
                    summary=f"{params.url} was not checked: the Command Center could not reach the internet "
                            "(its control check of well-known hosts failed too).",
                    data={"report": _report_data(report), "offline": True, "control_error": guard.error},
                    findings=[Finding(code="command_center_offline", severity="warning",
                                      title="The Command Center could not reach the internet; the site was not checked")],
                )
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
        timeout_seconds=SWEEP_TIMEOUT_SECONDS,
    )
    async def check_all_sites(self, ctx: AgentContext, params: CheckAllParams) -> AgentResult:
        sites = (await ctx.session.scalars(select(Site).where(Site.enabled.is_(True)).order_by(Site.id))).all()
        if not sites:
            return AgentResult(summary="No sites are being monitored yet. Add one on the Websites page.")
        checked, failed = await check_sites(ctx, sites, deep=params.deep)  # recorded, and their events emitted, as they finish
        rows: list[dict[str, Any]] = [
            {"site_id": site.id, "name": site.name, "url": site.url, "status": report.status, "summary": report.summary()}
            for site, report in checked
        ]
        rows += [{"site_id": site.id, "name": site.name, "url": site.url, "status": "not checked", "summary": error}
                 for site, error in failed]
        offline = [site for site, error in failed if error == connectivity.OFFLINE_REASON]
        failed = [(site, error) for site, error in failed if error != connectivity.OFFLINE_REASON]
        counts = {s: sum(1 for _, r in checked if r.status == s) for s in ("up", "degraded", "down")}
        summary = f"Checked {len(sites)} site(s): {counts['up']} up, {counts['degraded']} degraded, {counts['down']} down."
        if offline:
            summary = (f"The Command Center could not reach the internet: {len(offline)} of {len(sites)} site(s) were not checked "
                       "(not recorded as down).")
            if checked:
                summary += f" Checked {len(checked)}: {counts['up']} up, {counts['degraded']} degraded, {counts['down']} down."
        if failed:
            summary += f" {len(failed)} could not be checked."
        problems = [r for r in rows if r["status"] != "up" and r["summary"] != connectivity.OFFLINE_REASON]
        if problems:
            summary += " Problems: " + "; ".join(f"{r['name']} ({r['status']})" for r in problems[:5])
        findings = [f for _, r in checked for f in r.findings if f.severity != "info"]
        findings += [Finding(code="check_failed", severity="warning", title=f"{site.name} could not be checked: {error}"[:300])
                     for site, error in failed]
        if offline:
            findings.append(Finding(code="command_center_offline", severity="warning",
                                    title=f"The Command Center could not reach the internet; {len(offline)} site(s) not checked",
                                    detail="Its control check of well-known hosts failed too, so these failures were not "
                                           "recorded as website downtime."))
        return AgentResult(summary=summary, data={"counts": {**counts, "not_checked": len(offline) + len(failed)}, "sites": rows,
                                                  "offline": bool(offline)}, findings=findings)

    @capability(
        risk=Risk.READ,
        description="Summarise the latest known status and open issues of every monitored site (no new requests).",
        params=NoParams,
        examples=("Give me a website overview", "Which sites have problems?"),
    )
    async def sites_overview(self, ctx: AgentContext, params: NoParams) -> AgentResult:
        sites = (await ctx.session.scalars(select(Site).order_by(Site.id))).all()
        if not sites:
            return AgentResult(summary="No sites are being monitored yet.")
        # Open issues from the latest quick and the latest deep check (findings.py), not the latest check alone.
        found = await open_issues.open_findings(ctx.session, [s.id for s in sites])
        rows = []
        for site in sites:
            check = await ctx.session.get(SiteCheck, site.last_check_id) if site.last_check_id else None
            rows.append(
                {
                    "site_id": site.id,
                    "name": site.name,
                    "url": site.url,
                    "enabled": site.enabled,
                    "status": site.last_status,
                    "last_checked_at": site.last_checked_at.isoformat() if site.last_checked_at else None,
                    "wp_version": check.wp_version if check else "",
                    "issues": [f["title"] for f in found[site.id]["open_findings"]],
                    "issues_sources": found[site.id]["open_findings_sources"],
                }
            )
        monitored = [r for r in rows if r["enabled"]]
        paused = len(rows) - len(monitored)

        def line(r: dict[str, Any]) -> str:
            status = r["status"] if r["last_checked_at"] else "not checked yet"
            return f"{r['name']}: {status}" + (f" ({len(r['issues'])} issue(s))" if r["issues"] else "")

        summary = f"{len(monitored)} monitored site(s)" + (f", {paused} paused" if paused else "") + ". "
        summary += "; ".join(line(r) for r in monitored[:10])
        return AgentResult(summary=summary.strip(), data={"sites": rows, "monitored": len(monitored), "paused": paused})


    @capability(
        risk=Risk.READ,
        description="Uptime report: uptime over 24 hours, 7 and 30 days, response time, and downtime incidents (from stored checks).",
        params=UptimeParams,
        examples=("Give me the uptime report",),
    )
    async def uptime_report(self, ctx: AgentContext, params: UptimeParams) -> AgentResult:
        query = select(Site).order_by(Site.name)
        if params.site_id is not None:
            query = query.where(Site.id == params.site_id)
        sites = (await ctx.session.scalars(query)).all()
        if not sites:
            return AgentResult(summary="No matching sites.")
        stats = await uptime.uptime_stats(ctx.session)
        lines = ["# Uptime report", "",
                 "_Based on the checks that ran (normally every 15 minutes); a degraded site counts as up. Time the Command "
                 "Center was off, asleep or without internet is not checked, so it counts neither as up nor as down: "
                 "each figure says how many checks and clock hours it covers._", "",
                 "| Site | 24 h | 7 days | 30 days | Avg response (24 h) | Incidents (30 days) |", "|---|---|---|---|---|---|"]
        rows = []
        for site in sites:
            s = stats.get(site.id) or {}
            incidents = await uptime.incidents(ctx.session, site.id)
            rows.append({"site_id": site.id, "name": site.name, **s, "incidents": incidents})
            pct = [_uptime_cell(s, k, hours) for k, hours in (("24h", 24), ("7d", 168), ("30d", 720))]
            avg = f"{s['avg_response_ms_24h']} ms" if s.get("avg_response_ms_24h") is not None else "n/a"
            lines.append(f"| {site.name}{'' if site.enabled else ' (paused)'} | {pct[0]} | {pct[1]} | {pct[2]} | {avg} | {len(incidents)} |")
        for row in rows:
            if row["incidents"]:
                lines += ["", f"## {row['name']}: downtime", ""]
                lines += [f"- {i['start'][:16].replace('T', ' ')} UTC, " + (
                    f"at least {i['minutes']} min (not checked after {i['end'][11:16]} UTC)" if i.get("end_reason") == "not_checked"
                    else f"{i['minutes']} min") + f": {i['reason']}" for i in row["incidents"][-10:]]
        worst = min(rows, key=lambda r: r.get("30d") if r.get("30d") is not None else 101)
        summary = f"Uptime for {len(rows)} site(s); lowest 30-day uptime: {worst['name']} " + (
            f"{worst['30d']:g}%" if worst.get("30d") is not None else "(no checks yet)")
        return AgentResult(summary=summary, data={"sites": rows},
                           artifacts=[ArtifactSpec(kind="uptime_report", title="Uptime report", content="\n".join(lines))])


def _uptime_cell(stats: dict[str, Any], window: str, hours: int) -> str:
    if stats.get(window) is None:
        return "n/a"
    return f"{stats[window]:g}% ({stats[f'n_{window}']} checks, {stats[f'hours_checked_{window}']} of {hours} h)"


# Findings that change a site's status (down or degraded); an alert headline names one of these first.
STATUS_CAUSES = frozenset({
    "fetch_failed", "page_problem", "http_error", "page_notice", "slow_response", "tls_expired", "tls_invalid",
    "redirect_offsite", "conditional_redirect", "cloaked_spam", "cloaked_injection", "js_redirect_offsite",
    "meta_refresh_offsite", "hidden_iframe", "hidden_spam", "check_timeout",
})


def _first(report: SiteReport) -> str:
    """The finding behind the site's status, else the most serious one, for an alert headline."""
    serious = [f for f in report.findings if f.severity != "info"]
    if not serious:
        return report.status
    return min(serious, key=lambda f: (f.code not in STATUS_CAUSES, f.severity != "critical")).title


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


# ---------------------------------------------------------------------- shared
# Used by this agent and by the WordPress Security & Repair agent.


def make_checker(ctx: AgentContext, latest: str | None) -> SiteChecker:
    return SiteChecker(ctx.http, ctx.settings, latest_wp_version=latest, retry_delay=ctx.settings.monitor_retry_delay_seconds)


def _same_kind(site_id: int, deep: bool) -> tuple[Any, ...]:
    # Filtered in the database: the nightly deep check is usually far behind dozens of quick ones.
    deep_check = SiteCheck.details.contains({"deep": True})
    return SiteCheck.site_id == site_id, deep_check if deep else ~deep_check


async def previous_check(ctx: AgentContext, site_id: int, deep: bool) -> SiteCheck | None:
    """The latest deep or quick check that was not cut short.

    A check cut short lacks whatever it did not reach, so it is never the one a
    new check is compared with.
    """
    query = select(SiteCheck).where(*_same_kind(site_id, deep), ~SiteCheck.details.contains({"partial": True}))
    return await ctx.session.scalar(query.order_by(SiteCheck.checked_at.desc()).limit(1))


async def _reported_since(ctx: AgentContext, site_id: int, deep: bool, previous: SiteCheck | None) -> list[dict[str, Any]]:
    """The findings of the checks cut short since previous: already reported, so not new either.

    Each may stop at a different point, so all of them count, up to PARTIAL_CHECKS_SEEN
    (a site whose every check is cut short would otherwise mean reading them all).
    """
    query = select(SiteCheck.findings).where(*_same_kind(site_id, deep), SiteCheck.details.contains({"partial": True}))
    if previous is not None:
        query = query.where(SiteCheck.checked_at > previous.checked_at)
    rows = await ctx.session.scalars(query.order_by(SiteCheck.checked_at.desc()).limit(PARTIAL_CHECKS_SEEN))
    return [f for findings in rows for f in findings or []]


def _compare_scripts(report: SiteReport, previous: SiteCheck | None) -> None:
    """Flag script domains that were not on the page at the previous check (the first check is the baseline)."""
    now = set(report.details.get("external_scripts") or [])
    if previous is None or "external_scripts" not in previous.details:
        return
    new = sorted(now - set(previous.details.get("external_scripts") or []))
    if new:
        report.add("new_external_script", "warning", "New third-party script domain(s) on the home page: " + ", ".join(new[:5]),
                   "Expected after adding a plugin or tracking code; otherwise check for injected scripts.")


async def record_check(ctx: AgentContext, site: Site, deep: bool, report: SiteReport) -> list[EventSpec] | None:
    """Store a check result on the site's history and return the state-change events; None if the site is gone."""
    # Locked and re-read: two checks of the same site may finish at the same time,
    # and only the first may report the status change.
    fresh = await ctx.session.scalar(
        select(Site).where(Site.id == site.id).with_for_update().execution_options(populate_existing=True)
    )
    if fresh is None:
        ctx.log(f"{site.name} was deleted during the check; result not recorded", "warning")
        return None
    previous_status = site.last_status
    previous = await previous_check(ctx, site.id, deep)
    _compare_scripts(report, previous)
    seen = [*(previous.findings if previous else []), *await _reported_since(ctx, site.id, deep, previous)]
    # A quick check (or a deep one cut short before it got there) does not compare what search visitors get: the
    # cloaking the latest deep check that made the comparison found stays until another such deep check clears it.
    ran_cloaking = "variants" in (report.details.get("redirect_check") or {})
    carried = [] if ran_cloaking else open_issues.carried_causes(
        await open_issues.latest_deep_check(ctx.session, site.id, compared=True))
    status = open_issues.effective_status(report.status, carried)
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
    site.last_status = status  # the check row keeps what this check itself saw
    site.last_checked_at = check.checked_at
    site.last_check_id = check.id
    return change_events(site, previous_status, seen, report, carried=carried)


def _cut_short(report: SiteReport, seconds: float) -> None:
    """A check that ran out of time: down if the home page never answered, else what it found so far."""
    report.details["partial"] = True  # see previous_check
    if report.http_status is None:
        report.degrade("down")
        report.add("check_timeout", "critical", f"Site could not be checked within {seconds:g} seconds")
    else:
        report.add("check_timeout", "warning", f"The check did not finish within {seconds:g} seconds; results are partial")


async def check_sites(
    ctx: AgentContext, sites: Sequence[Site], *, deep: bool, record: bool = True
) -> tuple[list[tuple[Site, SiteReport]], list[tuple[Site, str]]]:
    """Check sites four at a time, each within its own deadline.

    With record, each result is stored, its state-change events are emitted and
    the session is committed as soon as that site is done, so a later timeout
    cannot discard it; the events are therefore not returned. Returns the
    checked sites with their reports, and the sites whose check failed with an
    error (nothing is recorded for those), both in the order given.

    With record, a site whose home page never answered is recorded only after
    a control check shows the Command Center itself is online (see
    connectivity.py); otherwise it fails with connectivity.OFFLINE_REASON and
    nothing is recorded for it.
    """
    latest = await latest_wordpress_version(ctx) if deep else None
    checker = make_checker(ctx, latest)
    deadline = ctx.settings.monitor_timeout_seconds * SITE_DEADLINE_FACTOR[deep]
    sem, db_lock = asyncio.Semaphore(4), asyncio.Lock()
    results: dict[int, SiteReport | str] = {}
    guard = connectivity.ConnectivityGuard(ctx.http, ctx.settings)
    source = ctx.agent_name or "monitoring"

    async def run(i: int, site: Site) -> None:
        async with sem:
            report = SiteReport(url=site.url)
            try:
                await asyncio.wait_for(checker.check(site.url, deep=deep, report=report), deadline)
            except TimeoutError:
                _cut_short(report, deadline)
            except Exception as exc:  # noqa: BLE001 - one broken check must not stop the others
                results[i] = f"{exc.__class__.__name__}: {exc}"
                ctx.log(f"{site.name}: check failed: {results[i]}", "error")
                return
        if record and connectivity.is_network_failure(report):
            if not await guard.online(failed_at=time.monotonic()):
                results[i] = connectivity.OFFLINE_REASON
                ctx.log(f"{site.name}: {_first(report)}; {connectivity.OFFLINE_REASON}", "warning")
                async with db_lock:
                    await connectivity.record_state(ctx.session, ctx.settings, online=False, error=guard.error,
                                                    source=source, task_id=ctx.task_id)
                    await ctx.session.commit()
                return
        elif report.http_status is not None:
            guard.saw_answer()
        if record:
            async with db_lock:  # one session, so one site at a time
                specs = await record_check(ctx, site, deep, report)
                if specs is None:
                    results[i] = "site was deleted during the check"
                    return
                for spec in specs:
                    await events.emit(ctx.session, ctx.settings, kind=spec.kind, severity=spec.severity, title=spec.title,
                                      source=source, detail=spec.detail, task_id=ctx.task_id)
                await ctx.session.commit()
        results[i] = report
        ctx.log(report.summary())

    # Least recently checked first: a sweep cut short by its task timeout leaves
    # out the sites it did not reach, and the next sweep starts with them.
    order = sorted(range(len(sites)),
                   key=lambda i: (sites[i].last_checked_at is not None, sites[i].last_checked_at or 0, sites[i].id))
    try:
        async with asyncio.TaskGroup() as group:  # a database error stops the remaining checks
            for i in order:
                group.create_task(run(i, sites[i]))
    except ExceptionGroup as exc:
        raise exc.exceptions[0] from None
    if record and guard.state is not None:
        # The latest evidence of the sweep: back online (one event if it was offline), or still offline.
        await connectivity.record_state(ctx.session, ctx.settings, online=guard.state == "online", error=guard.error,
                                        source=source, task_id=ctx.task_id)
        await ctx.session.commit()
    checked = [(site, r) for i, site in enumerate(sites) if isinstance(r := results.get(i), SiteReport)]
    failed = [(site, r) for i, site in enumerate(sites) if isinstance(r := results.get(i), str)]
    return checked, failed


def change_events(site: Site, previous_status: str, seen: list[dict[str, Any]], report: SiteReport,
                  carried: list[dict[str, Any]] | None = None) -> list[EventSpec]:
    """Status-change events, and one event for the findings whose code is not among the findings seen before.

    carried: the cloaking findings of the latest deep check that compared, which keep a quick check's site status degraded.
    """
    out: list[EventSpec] = []
    status = open_issues.effective_status(report.status, carried or [])
    detail = {"site_id": site.id, "site": site.name, "url": site.url, "status": status}
    if status != previous_status:
        if status == "down":
            out.append(EventSpec(kind="site.down", severity="critical", title=f"{site.name} is DOWN: {_first(report)}", detail=detail))
        elif status == "degraded":
            headline = _first(report) if report.status == "degraded" else (carried or [{}])[0].get("title", "degraded")
            out.append(EventSpec(kind="site.degraded", severity="warning", title=f"{site.name} is degraded: {headline}", detail=detail))
        elif previous_status in ("down", "degraded"):
            out.append(EventSpec(kind="site.recovered", severity="info", title=f"{site.name} has recovered", detail=detail))
    old_codes = {f.get("code") for f in seen}
    new = [f for f in report.findings if f.severity != "info" and f.code not in old_codes]
    # Availability problems are already covered by the status-change event.
    new = [f for f in new if f.code not in ("fetch_failed", "page_problem", "http_error", "page_notice", "slow_response",
                                            "check_timeout")]
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
