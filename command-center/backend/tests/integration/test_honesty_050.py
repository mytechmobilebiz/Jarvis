"""0.5.0 real-data honesty: offline sweeps, uptime coverage, site counts, open issues, mock delivery (C2-01/02/07/09/10/22)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select

from app.agents.monitoring import connectivity
from app.agents.monitoring.checks import SiteChecker
from app.agents.monitoring.findings import DEEP_STATUS_CODES, open_findings
from app.credit_repair import store
from app.db import session_scope
from app.models import Event, Site, SiteCheck

from ..fakes import FakeWeb, static_site, wordpress_site
from .test_agents import approve_all, grant_sensitive, run_task

HOSTS = ("alpha.example.test", "beta.example.test")


def serve(web: FakeWeb) -> None:
    for host in HOSTS:
        web.add(host, static_site())


def go_offline(web: FakeWeb) -> None:
    """The PC's connection drops: no name resolves, the control hosts included (FakeWeb raises a DNS error)."""
    web.sites.clear()


async def add_sites() -> list[int]:
    async with session_scope() as s:  # added directly: registering would queue baseline scans
        sites = [Site(name=h.split(".")[0].title(), url=f"http://{h}") for h in HOSTS]
        s.add_all(sites)
        await s.flush()
        return [site.id for site in sites]


async def count_checks() -> int:
    async with session_scope() as s:
        return await s.scalar(select(func.count()).select_from(SiteCheck))


async def events_of(admin, *kinds: str) -> list[dict]:
    return [e for e in (await admin.get("/api/events", params={"limit": 500})).json()["events"] if e["kind"] in kinds]


async def sweep(admin, make_worker) -> dict:
    task = await run_task(admin, make_worker, "monitoring", "check_all_sites", {"deep": False})
    assert task["status"] == "succeeded", task["error"]
    return task


# --------------------------------------------------------------- C2-22 offline


async def test_an_offline_pc_records_no_downtime_and_warns_once(admin, web: FakeWeb, make_worker):
    serve(web)
    await add_sites()
    await sweep(admin, make_worker)
    assert await count_checks() == 2
    status = (await admin.get("/api/system/status")).json()
    assert status["connectivity"]["state"] == "online" and status["connectivity"]["last_ok_at"]

    go_offline(web)
    for _ in range(2):  # two sweeps during one outage
        task = await sweep(admin, make_worker)
        assert task["summary"].startswith("The Command Center could not reach the internet: 2 of 2 site(s) were not checked")
        assert {r["status"] for r in task["result"]["data"]["sites"]} == {"not checked"}
        assert task["result"]["data"]["offline"] is True and task["result"]["data"]["counts"]["down"] == 0
        assert [f["code"] for f in task["result"]["findings"]] == ["command_center_offline"]
    assert await count_checks() == 2  # nothing stored: not 'down', not anything
    sites = (await admin.get("/api/sites")).json()["sites"]
    assert {s["last_status"] for s in sites} == {"up"}  # the last real observation stands
    assert all(s["uptime"]["24h"] == 100.0 and s["uptime"]["n_24h"] == 1 for s in sites)
    assert await events_of(admin, "site.down", "site.degraded") == []
    [offline] = await events_of(admin, connectivity.EVENT_OFFLINE)
    assert offline["severity"] == "warning" and offline["title"].startswith("Command Center lost internet")
    status = (await admin.get("/api/system/status")).json()
    assert status["connectivity"]["state"] == "offline" and "does not resolve" in status["connectivity"]["last_error"]

    web.__init__()  # the connection is back
    serve(web)
    task = await sweep(admin, make_worker)
    assert task["summary"].startswith("Checked 2 site(s): 2 up")
    assert await count_checks() == 4
    [online] = await events_of(admin, connectivity.EVENT_ONLINE)
    assert online["severity"] == "info" and "back online" in online["title"]
    assert online["forward_status"] != "skipped"  # n8n hears the all-clear as well as the warning
    assert len(await events_of(admin, connectivity.EVENT_OFFLINE)) == 1
    assert (await admin.get("/api/system/status")).json()["connectivity"]["state"] == "online"


async def test_a_dead_site_is_down_when_the_control_check_passes(admin, web: FakeWeb, make_worker):
    web.add("alpha.example.test", static_site())  # beta does not resolve; api.wordpress.org (a control host) answers
    await add_sites()
    task = await sweep(admin, make_worker)
    assert "1 up, 0 degraded, 1 down" in task["summary"]
    [down] = await events_of(admin, "site.down")
    assert down["title"].startswith("Beta is DOWN") and "does not resolve" in down["title"]
    assert await events_of(admin, connectivity.EVENT_OFFLINE) == []
    assert any(r.url.host == "api.wordpress.org" for r in web.requests)  # the control check ran


async def test_a_site_that_answers_with_an_error_needs_no_control_check(admin, web: FakeWeb, make_worker):
    web.add("alpha.example.test", static_site(status=502))
    web.add("beta.example.test", static_site())
    await add_sites()
    task = await sweep(admin, make_worker)
    assert "1 down" in task["summary"]
    assert not any(r.url.host == "api.wordpress.org" for r in web.requests)


async def test_checking_one_site_or_a_url_while_offline_says_not_checked(admin, web: FakeWeb, make_worker):
    site_id = (await add_sites())[0]
    go_offline(web)
    task = await run_task(admin, make_worker, "monitoring", "check_site", {"site_id": site_id, "deep": False})
    assert task["status"] == "failed" and connectivity.OFFLINE_REASON in task["error"]
    assert await count_checks() == 0
    task = await run_task(admin, make_worker, "monitoring", "check_url", {"url": "gamma.example.test", "deep": False})
    assert task["status"] == "succeeded" and "was not checked" in task["summary"] and "DOWN" not in task["summary"]


# ------------------------------------------------ incidents and coverage (C2-07)


async def _history(site_id: int, rows: list[tuple[timedelta, str]]) -> None:
    now = datetime.now(UTC)
    async with session_scope() as s:
        for ago, status in rows:
            findings = [{"code": "fetch_failed", "severity": "critical", "title": "Site refuses connections"}] if status == "down" else []
            s.add(SiteCheck(site_id=site_id, checked_at=now - ago, status=status, response_ms=150, findings=findings))


async def test_incidents_stop_where_the_checks_stopped(admin):
    site_id = (await add_sites())[0]
    m = lambda minutes: timedelta(minutes=minutes)  # noqa: E731
    await _history(site_id, [
        (m(600), "up"), (m(585), "up"), (m(570), "down"), (m(555), "down"),  # down, then the PC slept for 5 hours
        (m(255), "down"), (m(240), "up"),                                     # still down when checks resumed
        (m(225), "up"), (m(210), "up"), (m(195), "down"),                     # down at the last check, 3 h ago
    ])
    view = (await admin.get(f"/api/sites/{site_id}")).json()["site"]
    first, second, third = view["incidents_30d"]
    assert (first["end_reason"], first["minutes"]) == ("not_checked", 15)  # seen down for 15 min, then nobody looked
    assert (second["end_reason"], second["minutes"]) == ("recovered", 15)
    assert third["end_reason"] == "not_checked" and third["minutes"] == 0 and third["end"] == third["start"]
    assert first["reason"] == "Site refuses connections"


async def test_uptime_figures_carry_their_coverage(admin):
    site_id = (await add_sites())[0]
    h = lambda hours: timedelta(hours=hours, minutes=5)  # noqa: E731
    await _history(site_id, [(h(1), "up"), (h(2), "down"), (h(30), "up"), (h(24 * 10), "up")])
    uptime = next(s for s in (await admin.get("/api/sites")).json()["sites"] if s["id"] == site_id)["uptime"]
    assert (uptime["n_24h"], uptime["n_7d"], uptime["n_30d"], uptime["checks_30d"]) == (2, 3, 4, 4)
    assert (uptime["24h"], uptime["7d"], uptime["30d"]) == (50.0, round(200 / 3, 2), 75.0)
    assert (uptime["hours_checked_24h"], uptime["hours_checked_7d"], uptime["hours_checked_30d"]) == (2, 3, 4)
    assert uptime["first_check_at"]


async def test_uptime_report_states_checks_and_hours(admin, make_worker):
    site_id = (await add_sites())[0]
    await _history(site_id, [(timedelta(hours=1), "up"), (timedelta(hours=2), "down")])
    task = await run_task(admin, make_worker, "monitoring", "uptime_report", {"site_id": site_id})
    assert task["status"] == "succeeded", task["error"]
    async with session_scope() as s:
        from app.models import Artifact

        content = (await s.scalar(select(Artifact).where(Artifact.kind == "uptime_report"))).content
    assert "| Alpha | 50% (2 checks, 2 of 24 h) |" in content
    assert "counts neither as up nor as down" in content


# ------------------------------------------------------------ C2-09 site counts


async def test_status_counts_separate_monitored_and_paused_sites(admin):
    async with session_scope() as s:
        s.add_all([
            Site(name="A", url="http://a.example.test", last_status="up"),
            Site(name="B", url="http://b.example.test", last_status="down"),
            Site(name="C", url="http://c.example.test"),  # never checked: 'unknown'
            Site(name="D", url="http://d.example.test", last_status="down", enabled=False),
        ])
    counts = (await admin.get("/api/system/status")).json()["counts"]
    assert counts["sites"] == {"up": 1, "down": 2, "unknown": 1}  # unchanged: every site
    assert counts["sites_enabled"] == {"up": 1, "down": 1, "unknown": 1}
    assert counts["sites_paused"] == 1


async def test_sites_overview_names_paused_and_unchecked_sites(admin, make_worker):
    async with session_scope() as s:
        s.add_all([Site(name="Live", url="http://live.example.test", last_status="up", last_checked_at=datetime.now(UTC)),
                   Site(name="New", url="http://new.example.test"),
                   Site(name="Old", url="http://old.example.test", enabled=False, last_status="down")])
    task = await run_task(admin, make_worker, "monitoring", "sites_overview", {})
    assert task["summary"] == "2 monitored site(s), 1 paused. Live: up; New: not checked yet"
    assert task["result"]["data"]["paused"] == 1


# ----------------------------------------------------------- C2-10 open issues


async def test_open_findings_keep_the_night_scan_after_a_quick_sweep(admin, make_worker):
    site_id = (await add_sites())[0]
    now = datetime.now(UTC)
    async with session_scope() as s:
        s.add(SiteCheck(site_id=site_id, checked_at=now - timedelta(minutes=40), status="degraded", details={"deep": True},
                        findings=[{"code": "cloaked_spam", "severity": "critical", "title": "Spam shown only to Googlebot"},
                                  {"code": "slow_response", "severity": "warning", "title": "Home page took 4000 ms"}]))
        s.add(SiteCheck(site_id=site_id, checked_at=now - timedelta(minutes=5), status="up", details={"deep": False},
                        findings=[{"code": "missing_hsts", "severity": "info", "title": "No HSTS"}]))
    async with session_scope() as s:
        found = (await open_findings(s, [site_id, 999]))
    assert [f["title"] for f in found[site_id]["open_findings"]] == ["Spam shown only to Googlebot"]
    sources = found[site_id]["open_findings_sources"]
    assert sources["quick"] and sources["deep"] and sources["deep"] < sources["quick"]
    assert found[999] == {"open_findings": [], "open_findings_sources": {"quick": None, "deep": None}}
    overview = await run_task(admin, make_worker, "monitoring", "sites_overview", {})
    row = overview["result"]["data"]["sites"][0]
    assert row["issues"] == ["Spam shown only to Googlebot"] and row["issues_sources"] == sources


async def test_the_sites_api_gives_the_open_findings_of_both_checks(admin):
    site_id = (await add_sites())[0]
    now = datetime.now(UTC)
    async with session_scope() as s:
        s.add(SiteCheck(site_id=site_id, checked_at=now - timedelta(minutes=40), status="degraded", details={"deep": True},
                        findings=[{"code": "cloaked_spam", "severity": "critical", "title": "Spam"}]))
        s.add(SiteCheck(site_id=site_id, checked_at=now - timedelta(minutes=5), status="up", details={"deep": False}, findings=[]))
    listed = next(s for s in (await admin.get("/api/sites")).json()["sites"] if s["id"] == site_id)
    assert [f["title"] for f in listed["open_findings"]] == ["Spam"] and listed["open_findings"][0]["source"] == "deep"
    assert listed["open_findings_sources"]["quick"] and listed["open_findings_sources"]["deep"]
    unchecked = next(s for s in (await admin.get("/api/sites")).json()["sites"] if s["id"] != site_id)
    assert unchecked["open_findings"] == [] and unchecked["open_findings_sources"] == {"quick": None, "deep": None}
    one = (await admin.get(f"/api/sites/{site_id}")).json()["site"]
    assert one["open_findings"] == listed["open_findings"]
    created = (await admin.post("/api/sites", json={"name": "Gamma", "url": "gamma.example.test"})).json()["site"]
    assert created["open_findings"] == [] and created["open_findings_sources"] == {"quick": None, "deep": None}


def _hacked(origin: str, state: dict):
    clean = wordpress_site(origin, version="6.8.3", exposed=False)

    def handle(request: httpx.Request) -> httpx.Response:
        if state["hacked"] and "Googlebot" in request.headers.get("user-agent", ""):
            return httpx.Response(301, headers={"location": "https://pharma-spam.example/"})
        return clean(request)

    return handle


async def test_a_quick_sweep_does_not_clear_cloaking_or_announce_a_false_recovery(admin, web: FakeWeb, make_worker, monkeypatch):
    state = {"hacked": True}
    web.add("alpha.example.test", _hacked("http://alpha.example.test", state))
    web.add("beta.example.test", static_site())
    site_id = (await add_sites())[0]
    scan = await run_task(admin, make_worker, "wordpress_security", "security_scan", {"site_id": site_id})
    assert scan["status"] == "succeeded", scan["error"]
    statuses = lambda: admin.get("/api/sites")  # noqa: E731
    alpha = next(s for s in (await statuses()).json()["sites"] if s["id"] == site_id)
    assert alpha["last_status"] == "degraded"
    await sweep(admin, make_worker)  # quick: does not look at what Googlebot gets
    alpha = next(s for s in (await statuses()).json()["sites"] if s["id"] == site_id)
    assert alpha["last_status"] == "degraded" and alpha["last_check"]["status"] == "up"  # the row keeps what it saw
    assert await events_of(admin, "site.recovered") == []
    assert len(await events_of(admin, "site.degraded")) == 1

    state["hacked"] = False  # cleaned up
    real_cloaking = SiteChecker._assess_cloaking

    async def cut_short(self, *args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(SiteChecker, "_assess_cloaking", cut_short)  # a deep scan that ran out of time before comparing
    for _ in range(2):  # twice: the second must not take the first one, which compared nothing, as the latest word
        await run_task(admin, make_worker, "wordpress_security", "security_scan", {"site_id": site_id})
        alpha = next(s for s in (await statuses()).json()["sites"] if s["id"] == site_id)
        assert alpha["last_check"]["details"]["partial"] is True and alpha["last_status"] == "degraded"
    assert await events_of(admin, "site.recovered") == []

    await sweep(admin, make_worker)  # the next quick sweep: the cloaking stays, from the last scan that compared
    alpha = next(s for s in (await statuses()).json()["sites"] if s["id"] == site_id)
    assert alpha["last_status"] == "degraded" and alpha["last_check"]["status"] == "up"
    assert await events_of(admin, "site.recovered") == []
    open_codes = {f["code"]: f for f in alpha["open_findings"]}
    cloaking = [f for code, f in open_codes.items() if code in DEEP_STATUS_CODES]
    assert cloaking and all(f["checked_at"] < alpha["open_findings_sources"]["deep"] for f in cloaking)
    assert "check_timeout" in open_codes  # and the note that the latest scan was cut short

    monkeypatch.setattr(SiteChecker, "_assess_cloaking", real_cloaking)  # the next full deep scan clears it
    await run_task(admin, make_worker, "wordpress_security", "security_scan", {"site_id": site_id})
    alpha = next(s for s in (await statuses()).json()["sites"] if s["id"] == site_id)
    assert alpha["last_status"] == "up"
    assert len(await events_of(admin, "site.recovered")) == 1
    assert not any(f["code"] in DEEP_STATUS_CODES for f in alpha["open_findings"])


# ---------------------------------------------------- C2-01 / C2-02 delivery


async def test_leads_and_tickets_say_the_email_was_only_recorded(admin, make_worker):
    lead_id = (await admin.post("/api/leads", json={"name": "Lee", "company": "Lee Co", "email": "lee@lee.example",
                                                     "message": "New website please"})).json()["lead"]["id"]
    other = (await admin.post("/api/leads", json={"name": "Other", "email": "o@o.example"})).json()["lead"]
    assert other["delivery_mode"] is None and other["delivery_outbox_id"] is None
    ticket_id = (await admin.post("/api/support/tickets", json={"customer_name": "Dana", "customer_email": "dana@client.example",
                                                                 "subject": "Question", "body": "Hello"})).json()["ticket"]["id"]
    await make_worker().run_until_idle()
    await run_task(admin, make_worker, "communications", "draft_proposal", {"lead_id": lead_id})
    await run_task(admin, make_worker, "communications", "send_proposal", {"lead_id": lead_id})
    await run_task(admin, make_worker, "communications", "send_reply", {"ticket_id": ticket_id})
    assert await approve_all(admin) == 2
    await make_worker().run_until_idle()
    outbox = {e["recipient"]: e["id"] for e in (await admin.get("/api/outbox")).json()["entries"]}

    leads = {r["id"]: r for r in (await admin.get("/api/leads")).json()["leads"]}
    assert leads[lead_id]["status"] == "proposal_sent"  # stored data unchanged
    assert leads[lead_id]["delivery_mode"] == "mock" and leads[lead_id]["delivery_outbox_id"] == outbox["lee@lee.example"]
    assert leads[other["id"]]["delivery_mode"] is None
    assert (await admin.get(f"/api/leads/{lead_id}")).json()["lead"]["delivery_mode"] == "mock"
    [ticket] = (await admin.get("/api/support/tickets")).json()["tickets"]
    assert ticket["status"] == "replied" and ticket["delivery_mode"] == "mock"
    assert ticket["delivery_outbox_id"] == outbox["dana@client.example"]


async def test_credit_repair_letters_say_mock_mail_was_not_mailed(admin, settings):
    await grant_sensitive("admin@test.local")
    await admin.post("/api/credit-repair/enable", json={"acknowledge": True})
    cid = (await admin.post("/api/credit-repair/clients", json={"full_name": "Pat Example (synthetic)"})).json()["client"]["id"]
    for bureau in ("experian", "equifax"):
        await admin.post(f"/api/credit-repair/clients/{cid}/items", json={"bureau": bureau, "creditor": "Bank", "reason": "not_mine"})
    cipher = store.Cipher(settings)
    async with store.session(settings) as s:
        items = (await s.scalars(select(store.CRItem).order_by(store.CRItem.id))).all()
        items[0].status = "disputed"  # what a mock mailing run stores today; not rewritten
        s.add(store.CRLetter(client_id=cid, bureau="experian", item_ids=[items[0].id], content_enc=cipher.enc("x"),
                             status="mailed_mock", mailed_at=datetime.now(UTC)))
        s.add(store.CRLetter(client_id=cid, bureau="equifax", item_ids=[items[1].id], content_enc=cipher.enc("y")))
    view = (await admin.get(f"/api/credit-repair/clients/{cid}")).json()
    assert [(l["status"], l["mail_mode"]) for l in view["letters"]] == [("mailed_mock", "mock"), ("draft", None)]
    assert view["letters"][0]["mailed_at"]  # when the mailing was recorded; nothing was mailed
    assert [(i["status"], i["mail_mode"]) for i in view["items"]] == [("disputed", "mock"), ("open", None)]
    letter = (await admin.get(f"/api/credit-repair/letters/{view['letters'][0]['id']}")).json()["letter"]
    assert letter["mail_mode"] == "mock" and letter["mailed_at"]


async def test_old_down_rows_are_left_as_they_are(admin):
    """No migration: checks stored as 'down' before 0.5.0 are not rewritten."""
    site_id = (await add_sites())[0]
    await _history(site_id, [(timedelta(minutes=30), "down")])
    async with session_scope() as s:
        assert (await s.scalars(select(SiteCheck.status))).all() == ["down"]
        assert await s.scalar(select(func.count()).select_from(Event)) == 0


# ------------------------------------------- C2-09, C2-10, C2-12 in the AI Supervisor's own texts


async def test_the_daily_briefing_counts_monitored_sites_and_keeps_the_night_scan(admin, make_worker):
    alpha, beta = await add_sites()
    now = datetime.now(UTC)
    async with session_scope() as s:
        (await s.get(Site, beta)).enabled = False
        site = await s.get(Site, alpha)
        site.last_status, site.last_checked_at = "degraded", now - timedelta(minutes=5)
        s.add(SiteCheck(site_id=alpha, checked_at=now - timedelta(minutes=40), status="degraded", details={"deep": True},
                        findings=[{"code": "cloaked_spam", "severity": "critical", "title": "Spam shown only to Googlebot"}]))
        s.add(SiteCheck(site_id=alpha, checked_at=now - timedelta(minutes=5), status="degraded", details={"deep": False}, findings=[]))
    task = await run_task(admin, make_worker, "supervisor", "daily_briefing", {"hours": 24})
    assert task["status"] == "succeeded", task["error"]
    data = task["result"]["data"]
    assert data["sites"] == {"degraded": 1} and data["sites_paused"] == 1  # the paused site is not counted as monitored
    assert data["critical_site_issues"] == ["Alpha: Spam shown only to Googlebot"]  # from the deep scan, after a quick sweep
    async with session_scope() as s:
        from app.models import Artifact

        content = (await s.scalar(select(Artifact).where(Artifact.kind == "briefing"))).content
    assert "1 monitored, 1 paused: 0 up, 1 degraded, 0 down, 0 not checked yet." in content
    assert "- critical: Alpha: Spam shown only to Googlebot" in content


def test_request_summaries_say_when_results_are_sample_data():
    from app.models import Task
    from app.supervisor.agent import SUMMARY_SYSTEM, _task_digest, template_summary

    ads = Task(id=1, agent="social_marketing", capability="ads_report", status="succeeded", risk="read", summary="",
               error="", result={"data": {"sample_data": True}, "findings": []})
    scan = Task(id=2, agent="monitoring", capability="check_site", status="succeeded", risk="read", summary="Alpha: up",
                error="", result={"data": {}, "findings": []})
    digests = [_task_digest(ads, None), _task_digest(scan, None)]
    assert [d["sample_data"] for d in digests] == [True, False]
    lines = template_summary("ads?", digests).splitlines()
    assert "- social_marketing.ads_report: done (sample data, no ad account connected)" in lines
    assert "- monitoring.check_site: Alpha: up" in lines
    labelled = _task_digest(Task(id=3, agent="social_marketing", capability="ads_report", status="succeeded", risk="read",
                                 summary="Ads (sample data, 7 days): 3 campaigns", error="", result={"data": {"sample_data": True}}), None)
    assert template_summary("ads?", [labelled]).count("sample data") == 1  # not said twice
    assert "sample_data are synthetic" in SUMMARY_SYSTEM and "no ad account connected" in SUMMARY_SYSTEM
