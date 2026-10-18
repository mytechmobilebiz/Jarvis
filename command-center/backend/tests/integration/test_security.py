"""Site registration, uptime figures, security scans and security reports (HTTP side, read-only)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from app.db import session_scope
from app.models import Artifact, SiteCheck

from ..fakes import WP_HOME, FakeWeb, wordpress_site
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
