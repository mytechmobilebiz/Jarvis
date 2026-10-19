"""Dashboard aggregates: task activity, per-agent work in progress, AI and n8n status fields, uptime coverage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.db import session_scope
from app.models import LLMUsage, Site, SiteCheck, Task


async def _task(agent: str, capability: str, status: str, finished: datetime | None, **kw) -> None:
    async with session_scope() as s:
        s.add(Task(agent=agent, capability=capability, risk="read", status=status, finished_at=finished, started_at=finished, **kw))


async def test_task_activity_buckets_categories_and_comparison(admin):
    now = datetime.now(UTC)
    await _task("monitoring", "check_all_sites", "succeeded", now - timedelta(minutes=5))
    await _task("monitoring", "check_all_sites", "failed", now - timedelta(minutes=6))
    await _task("seo_content", "audit_page", "succeeded", now - timedelta(hours=3))
    await _task("supervisor", "plan_request", "succeeded", now - timedelta(minutes=1))  # internal: never counted
    await _task("monitoring", "check_all_sites", "queued", None)  # not finished: not counted
    await _task("monitoring", "check_all_sites", "succeeded", now - timedelta(days=3))  # outside 24 h
    r = await admin.get("/api/stats/task-activity", params={"range": "24h", "tz": "UTC"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["bucket"] == "hour"
    assert [c["key"] for c in d["categories"]][:2] == ["website_scans", "seo"]
    assert d["totals"]["website_scans"] == 2 and d["totals"]["seo"] == 1
    assert sum(d["totals"].values()) == 3 and d["failed"] == 1
    assert 24 <= len(d["buckets"]) <= 25  # continuous axis, one per hour
    last = d["buckets"][-1]
    assert last["counts"]["website_scans"] == 2 and last["failed"] == 1
    assert all(sum(b["counts"].values()) == 0 for b in d["buckets"][:-4])
    week = (await admin.get("/api/stats/task-activity", params={"range": "7d", "tz": "UTC"})).json()
    assert week["bucket"] == "6 hours" and week["totals"]["website_scans"] == 3


async def test_task_activity_uses_the_callers_time_zone(admin):
    zone = ZoneInfo("Asia/Kolkata")  # +05:30: buckets start on the local hour, not the UTC hour
    d = (await admin.get("/api/stats/task-activity", params={"tz": "Asia/Kolkata"})).json()
    first = datetime.fromisoformat(d["buckets"][0]["start"])
    assert first.utcoffset() == timedelta(hours=5, minutes=30) and first.minute == 0
    local_midnight = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    await _task("monitoring", "check_site", "succeeded", local_midnight + timedelta(minutes=1))
    # Yesterday just after midnight counts towards "yesterday by the same time"; yesterday 23:59 does not.
    await _task("monitoring", "check_site", "succeeded", local_midnight - timedelta(days=1) + timedelta(seconds=30))
    await _task("monitoring", "check_site", "succeeded", local_midnight - timedelta(minutes=1))
    d = (await admin.get("/api/stats/task-activity", params={"tz": "Asia/Kolkata"})).json()
    assert d["today"]["succeeded"] == 1
    if datetime.now(zone) - local_midnight > timedelta(minutes=1):
        assert d["yesterday_same_time"]["succeeded"] == 1


async def test_task_activity_rejects_bad_input_and_needs_sign_in(admin, app_client):
    assert (await admin.get("/api/stats/task-activity", params={"tz": "Not/AZone"})).status_code == 400
    assert (await admin.get("/api/stats/task-activity", params={"range": "1y"})).status_code == 422
    app_client.cookies.clear()
    assert (await app_client.get("/api/stats/task-activity")).status_code == 401


async def test_agents_report_work_in_progress_regardless_of_age(admin):
    await _task("monitoring", "check_all_sites", "queued", None, created_at=datetime.now(UTC) - timedelta(days=2))
    await _task("monitoring", "check_site", "running", None)
    agents = {a["name"]: a for a in (await admin.get("/api/agents")).json()["agents"]}
    assert agents["monitoring"]["active"] == {"queued": 1, "running": 1, "awaiting_approval": 0}
    assert agents["monitoring"]["stats_24h"].get("queued", 0) == 0  # the old task is outside the 24 h window
    assert agents["seo_content"]["active"] == {"queued": 0, "running": 0, "awaiting_approval": 0}


async def test_status_reports_ai_calls_and_the_n8n_editor(admin):
    s = (await admin.get("/api/system/status")).json()
    assert s["llm"]["last_ok_at"] is None and s["llm"]["last_error_at"] is None
    assert s["n8n_editor_url"] == "http://localhost:5680"
    async with session_scope() as db:
        db.add(LLMUsage(provider="anthropic", model="m", purpose="plan", ok=True))
        db.add(LLMUsage(provider="anthropic", model="m", purpose="plan", ok=False, error="overloaded " * 100,
                        at=datetime.now(UTC) + timedelta(seconds=1)))
    s = (await admin.get("/api/system/status")).json()
    assert s["llm"]["last_ok_at"] and s["llm"]["last_error_at"]
    assert s["llm"]["last_error"].startswith("overloaded") and len(s["llm"]["last_error"]) <= 300


async def test_n8n_editor_link_only_for_http_urls(admin, settings, monkeypatch):
    monkeypatch.setattr(settings, "n8n_editor_url", "javascript:alert(1)")
    assert (await admin.get("/api/system/status")).json()["n8n_editor_url"] is None
    monkeypatch.setattr(settings, "n8n_editor_url", "http://localhost:5680")
    monkeypatch.setattr(settings, "workflow_engine", "none")
    assert (await admin.get("/api/system/status")).json()["n8n_editor_url"] is None


async def test_uptime_says_since_when(admin):
    async with session_scope() as s:
        site = Site(name="Young site", url="https://young.example")
        s.add(site)
        await s.flush()
        first = datetime.now(UTC) - timedelta(days=2)
        s.add(SiteCheck(site_id=site.id, status="up", checked_at=first))
        s.add(SiteCheck(site_id=site.id, status="down", checked_at=datetime.now(UTC) - timedelta(hours=1)))
    sites = (await admin.get("/api/sites")).json()["sites"]
    up = next(x for x in sites if x["name"] == "Young site")["uptime"]
    assert up["checks_30d"] == 2 and up["30d"] == 50.0
    assert datetime.fromisoformat(up["first_check_at"]) < datetime.now(UTC) - timedelta(days=1)
