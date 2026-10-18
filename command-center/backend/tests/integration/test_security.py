"""Site registration, uptime figures, security scans and security reports (HTTP side, read-only)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select

from app.agents.monitoring import agent as monitoring
from app.agents.monitoring.checks import SiteChecker
from app.db import session_scope
from app.models import Artifact, AuditLog, Event, Site, SiteCheck, User
from app.security import hash_password

from ..conftest import ADMIN
from ..fakes import WP_HOME, FakeWeb, static_site, wordpress_site
from .test_agents import run_task


async def register(admin, name: str, url: str) -> dict:
    r = await admin.post("/api/sites", json={"name": name, "url": url})
    assert r.status_code == 201, r.text
    return r.json()


async def test_registration_queues_a_baseline_security_scan(admin, web: FakeWeb, make_worker):
    web.add("acme.example.test", wordpress_site("https://acme.example.test"))
    created = await register(admin, "Acme", "acme.example.test")
    assert created["baseline_task"]["agent"] == "wordpress_security" and created["baseline_task"]["capability"] == "security_scan"
    await make_worker().run_until_idle()
    site = (await admin.get(f"/api/sites/{created['site']['id']}")).json()["site"]
    assert site["last_check"]["details"]["deep"] is True
    assert site["uptime"]["24h"] == 100.0 and site["uptime"]["checks_30d"] == 1
    assert site["incidents_30d"] == []
    assert web.methods() == {"GET"}


async def test_new_script_domain_after_the_baseline_is_flagged(admin, web: FakeWeb, make_worker):
    state = {"html": WP_HOME}
    site_handler = lambda r: wordpress_site("https://acme.example.test", home_html=state["html"])(r)  # noqa: E731
    web.add("acme.example.test", site_handler)
    site = (await register(admin, "Acme", "acme.example.test"))["site"]
    await make_worker().run_until_idle()  # baseline
    state["html"] = WP_HOME.replace("</head>", '<script src="https://stats.new-tracker.example/t.js"></script></head>')
    task = await run_task(admin, make_worker, "wordpress_security", "security_scan", {"site_id": site["id"]})
    codes = {f["code"]: f["severity"] for f in task["result"]["findings"]}
    assert codes["new_external_script"] == "warning"


async def test_uptime_figures_and_incidents(admin, web: FakeWeb, make_worker):
    web.add("acme.example.test", wordpress_site("https://acme.example.test"))
    site = (await register(admin, "Acme", "acme.example.test"))["site"]
    await make_worker().run_until_idle()
    now = datetime.now(UTC)
    async with session_scope() as s:
        # A one-hour outage two days ago, then recovery: 96 checks over 24 hours of history.
        for i in range(96):
            at = now - timedelta(days=2) + timedelta(minutes=15 * i)
            status = "down" if 8 <= i < 12 else "up"
            s.add(SiteCheck(site_id=site["id"], checked_at=at, status=status, response_ms=200,
                            findings=[{"code": "http_error", "severity": "critical", "title": "HTTP 502"}] if status == "down" else []))
    view = (await admin.get(f"/api/sites/{site['id']}")).json()["site"]
    assert view["uptime"]["24h"] == 100.0
    assert view["uptime"]["7d"] == round(100 * 93 / 97, 2)
    [incident] = view["incidents_30d"]
    assert incident["minutes"] == 60 and incident["reason"] == "HTTP 502"
    report = await run_task(admin, make_worker, "monitoring", "uptime_report", {})
    assert report["status"] == "succeeded" and "Acme" in report["summary"]


async def test_security_report_for_a_hacked_site(admin, web: FakeWeb, make_worker):
    def hacked(request: httpx.Request) -> httpx.Response:
        if "Googlebot" in request.headers.get("user-agent", ""):
            return httpx.Response(301, headers={"location": "https://pharma-spam.example/"})
        return wordpress_site("https://hacked.example.test")(request)

    web.add("hacked.example.test", hacked)
    web.add("clean.example.test", wordpress_site("https://clean.example.test", exposed=False))
    hacked_site = (await register(admin, "Hacked", "hacked.example.test"))["site"]
    await register(admin, "Clean", "clean.example.test")
    await make_worker().run_until_idle()

    events = (await admin.get("/api/events")).json()["events"]
    assert any(e["kind"] == "site.degraded" and "Googlebot" in e["title"] for e in events)

    task = await run_task(admin, make_worker, "wordpress_security", "security_report", {})
    assert task["status"] == "succeeded" and task["risk"] == "draft"
    assert "1 at risk (Hacked)" in task["summary"]
    async with session_scope() as s:
        artifact = await s.scalar(select(Artifact).where(Artifact.kind == "security_report"))
    content = artifact.content
    assert "| Hacked | AT RISK |" in content and "| Clean |" in content
    assert "Visitors identifying as Googlebot: **redirected to https://pharma-spam.example/**" in content
    assert "repair_reinstall_core" in content  # the matching repair is named; it still needs approval
    assert "nothing was changed on any site" in content
    single = await run_task(admin, make_worker, "wordpress_security", "security_report", {"site_id": hacked_site["id"]})
    assert single["summary"].startswith("Security report - Hacked")
    assert "pharma-spam.example" not in {r.url.host for r in web.requests}
    assert web.methods() == {"GET"}


async def test_only_an_admin_registers_a_staging_or_test_site(app_client, admin):
    async with session_scope() as s:
        s.add(User(email="op@test.local", password_hash=hash_password("operator-password"), role="operator"))
    await app_client.post("/api/auth/logout")
    await app_client.post("/api/auth/login", json={"email": "op@test.local", "password": "operator-password"})
    r = await app_client.post("/api/sites", json={"name": "Stage", "url": "stage.example.test", "environment": "staging"})
    assert r.status_code == 403 and "admin" in r.text
    assert (await app_client.post("/api/sites", json={"name": "Prod", "url": "prod.example.test"})).status_code == 201
    await app_client.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    r = await app_client.post("/api/sites", json={"name": "Stage", "url": "stage.example.test", "environment": "staging"})
    assert r.status_code == 201 and r.json()["site"]["environment"] == "staging"
    async with session_scope() as s:
        entries = (await s.scalars(select(AuditLog).where(AuditLog.action == "site.created").order_by(AuditLog.id))).all()
    assert [(e.actor, e.detail["environment"]) for e in entries] == [("op@test.local", "production"), (ADMIN[0], "staging")]


async def test_deep_scan_is_compared_with_the_previous_deep_scan(admin, web: FakeWeb, make_worker):
    web.add("acme.example.test", wordpress_site("https://acme.example.test"))
    site = (await register(admin, "Acme", "acme.example.test"))["site"]
    await make_worker().run_until_idle()  # the baseline deep scan reports the exposures once
    async with session_scope() as s:
        baseline = await s.scalar(select(SiteCheck).where(SiteCheck.site_id == site["id"]))
        for i in range(25):  # then a day of quick checks
            s.add(SiteCheck(site_id=site["id"], checked_at=baseline.checked_at + timedelta(microseconds=i + 1), status="up",
                            details={"deep": False}))
    task = await run_task(admin, make_worker, "monitoring", "check_site", {"site_id": site["id"], "deep": True})
    assert task["status"] == "succeeded"
    async with session_scope() as s:
        alerts = await s.scalar(select(func.count()).select_from(Event).where(Event.kind == "site.finding"))
    assert alerts == 1  # nothing new since the baseline, so no second alert


async def _add_sites(*names: str) -> None:
    async with session_scope() as s:  # added directly: registering would queue baseline scans
        for name in names:
            s.add(Site(name=name.title(), url=f"http://{name}.example.test"))


async def _hang(request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(60)
    raise AssertionError("should have been cut short")


async def _statuses(admin) -> dict[str, str]:
    return {s["name"]: s["last_status"] for s in (await admin.get("/api/sites")).json()["sites"]}


async def test_sweep_gives_each_site_its_own_deadline_and_survives_a_broken_check(admin, web: FakeWeb, make_worker, settings,
                                                                                  monkeypatch):
    real_check = SiteChecker.check

    async def check(self, url, **kwargs):
        if "broken" in url:
            raise ValueError("Invalid IPv6 URL")
        return await real_check(self, url, **kwargs)

    monkeypatch.setattr(SiteChecker, "check", check)
    monkeypatch.setitem(monitoring.SITE_DEADLINE_FACTOR, False, 1)
    web.add("ok.example.test", static_site())
    web.add("hang.example.test", _hang)
    await _add_sites("ok", "hang", "broken")
    task = await run_task(admin, make_worker, "monitoring", "check_all_sites", {"deep": False},
                          settings=settings.model_copy(update={"monitor_timeout_seconds": 0.5}))
    assert task["status"] == "succeeded", task["error"]
    assert "1 up, 0 degraded, 1 down. 1 could not be checked." in task["summary"]
    found = {f["code"]: f for f in task["result"]["findings"]}
    assert found["check_timeout"]["severity"] == "critical" and "0.5 seconds" in found["check_timeout"]["title"]
    assert found["check_failed"]["title"] == "Broken could not be checked: ValueError: Invalid IPv6 URL"
    assert await _statuses(admin) == {"Ok": "up", "Hang": "down", "Broken": "unknown"}
    events = (await admin.get("/api/events")).json()["events"]
    assert [e["title"] for e in events if e["kind"] == "site.down"] == ["Hang is DOWN: Site could not be checked within 0.5 seconds"]


async def test_sweep_keeps_the_sites_it_finished_when_the_task_times_out(admin, web: FakeWeb, make_worker, monkeypatch):
    cap = monitoring.MonitoringAgent.capabilities()["check_all_sites"]
    assert cap.timeout_seconds == monitoring.SWEEP_TIMEOUT_SECONDS
    monkeypatch.setitem(monitoring.MonitoringAgent._capabilities, "check_all_sites", replace(cap, timeout_seconds=1))
    web.add("ok.example.test", static_site())
    web.add("hang.example.test", _hang)
    await _add_sites("ok", "hang")
    task = await run_task(admin, make_worker, "monitoring", "check_all_sites", {"deep": False})
    assert "timed out after 1s" in task["error"]
    assert await _statuses(admin) == {"Ok": "up", "Hang": "unknown"}
