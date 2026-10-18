"""End-to-end flows through the API, the worker and the database (external services faked)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app import tasks as task_service
from app.config import Settings
from app.db import session_scope
from app.models import Artifact, Event, Schedule, Site, SiteCheck, Task
from app.worker import scheduler

from ..fakes import FakeLLM, static_site, wordpress_site
from .conftest import GateTestAgent


async def add_site(admin, name, url):
    r = await admin.post("/api/sites", json={"name": name, "url": url})
    assert r.status_code == 201, r.text
    return r.json()["site"]


async def ask(admin, message):
    r = await admin.post("/api/supervisor/requests", json={"message": message})
    assert r.status_code == 202, r.text
    return r.json()["request"]["id"]


async def test_supervisor_checks_all_sites(admin, web, make_worker):
    web.add("acme.example.test", wordpress_site("https://acme.example.test"))
    web.add("down.example.test", static_site("<h1>Error establishing a database connection</h1>", status=500))
    acme = await add_site(admin, "Acme", "https://acme.example.test")
    down = await add_site(admin, "Broken", "https://down.example.test")

    req_id = await ask(admin, "Check all websites please")
    await make_worker().run_until_idle()

    detail = (await admin.get(f"/api/supervisor/requests/{req_id}")).json()
    req = detail["request"]
    assert req["status"] == "completed", req
    assert req["planner"] == "rule-based"
    assert req["summarizer"] == "template"
    assert "CRITICAL" in req["summary"] and "Broken" in req["summary"]
    actions = [(t["agent"], t["capability"], t["status"]) for t in detail["tasks"]]
    assert ("monitoring", "check_all_sites", "succeeded") in actions
    assert ("supervisor", "summarize_request", "succeeded") in actions

    sites = {s["id"]: s for s in (await admin.get("/api/sites")).json()["sites"]}
    assert sites[acme["id"]]["last_status"] == "up"
    assert sites[down["id"]]["last_status"] == "down"
    assert sites[acme["id"]]["last_check"]["is_wordpress"] is True

    events = (await admin.get("/api/events")).json()["events"]
    kinds = {(e["kind"], e["severity"]) for e in events}
    assert ("site.down", "critical") in kinds
    # every request the agent made was a GET
    assert web.methods() == {"GET"}


async def test_site_down_event_is_forwarded_to_n8n_and_acknowledged(admin, web, n8n, make_worker, service_headers):
    web.add("down.example.test", static_site("bad gateway", status=502))
    site = await add_site(admin, "Broken", "https://down.example.test")
    await admin.post(f"/api/sites/{site['id']}/check", json={"deep": False})
    await make_worker().run_until_idle()

    assert len(n8n.received) == 1
    sent = n8n.received[0]
    assert sent["headers"]["x-cc-webhook-secret"] == "test-webhook-secret"
    assert sent["body"]["kind"] == "site.down"
    event_id = sent["body"]["event_id"]
    event = (await admin.get(f"/api/events/{event_id}")).json()["event"]
    assert event["forward_status"] == "sent"

    r = await admin.post("/api/integrations/n8n/ack", json={"event_id": event_id})
    assert r.status_code == 403  # people cannot fake an n8n acknowledgement
    r = await admin.post("/api/integrations/n8n/ack", json={"event_id": event_id, "execution_id": "42"}, headers=service_headers)
    assert r.status_code == 200
    event = (await admin.get(f"/api/events/{event_id}")).json()["event"]
    assert event["n8n_ack_at"] and event["n8n_execution_id"] == "42"


async def test_recovery_is_reported_once(admin, web, make_worker):
    state = {"ok": False}

    def flaky(request):
        return static_site()(request) if state["ok"] else static_site("down", status=500)(request)

    web.add("flaky.example.test", flaky)
    site = await add_site(admin, "Flaky", "https://flaky.example.test")
    worker = make_worker()
    for ok in (False, False, True, True):
        state["ok"] = ok
        await admin.post(f"/api/sites/{site['id']}/check", json={"deep": False})
        await worker.run_until_idle()
    kinds = [e["kind"] for e in (await admin.get("/api/events")).json()["events"]]
    assert kinds.count("site.down") == 1
    assert kinds.count("site.recovered") == 1


async def test_new_findings_alert_only_once(admin, web, make_worker):
    web.add("acme.example.test", wordpress_site("https://acme.example.test"))
    site = await add_site(admin, "Acme", "https://acme.example.test")
    worker = make_worker()
    for _ in range(2):
        await admin.post(f"/api/sites/{site['id']}/check", json={"deep": True})
        await worker.run_until_idle()
    events = [e for e in (await admin.get("/api/events")).json()["events"] if e["kind"] == "site.finding"]
    assert len(events) == 1
    assert events[0]["severity"] == "critical"  # the exposed debug.log
    async with session_scope() as s:
        checks = (await s.scalars(select(SiteCheck))).all()
    assert len(checks) == 3  # the baseline scan queued at registration, then two checks


async def test_n8n_outage_backs_off_and_records_error(admin, web, n8n, make_worker):
    n8n.fail = True
    web.add("down.example.test", static_site("x", status=500))
    site = await add_site(admin, "Broken", "https://down.example.test")
    await admin.post(f"/api/sites/{site['id']}/check", json={"deep": False})
    await make_worker().run_until_idle()
    async with session_scope() as s:
        event = await s.scalar(select(Event).where(Event.kind == "site.down"))
    assert event.forward_status == "pending"
    assert event.forward_attempts == 1
    assert event.forward_next_at > datetime.now(UTC)
    status = (await admin.get("/api/system/status")).json()
    assert "event delivery failed" in status["n8n"]["last_error"]


async def test_approval_gate(admin, gate_agent, make_worker):
    r = await admin.post("/api/agents/gate_test/tasks", json={"capability": "publish", "params": {"title": "Hello"}})
    task = r.json()["task"]
    assert task["status"] == "awaiting_approval"
    worker = make_worker()
    await worker.run_until_idle()
    assert (await admin.get(f"/api/tasks/{task['id']}")).json()["task"]["status"] == "awaiting_approval"
    assert GateTestAgent.seen_approvers == []

    approvals = (await admin.get("/api/approvals")).json()["approvals"]
    assert len(approvals) == 1 and approvals[0]["risk"] == "external"
    r = await admin.post(f"/api/approvals/{approvals[0]['id']}/decision", json={"approve": True, "note": "fine"})
    assert r.status_code == 200
    await worker.run_until_idle()
    done = (await admin.get(f"/api/tasks/{task['id']}")).json()
    assert done["task"]["status"] == "succeeded"
    assert GateTestAgent.seen_approvers == ["admin@test.local"]
    # a decision cannot be made twice
    r = await admin.post(f"/api/approvals/{approvals[0]['id']}/decision", json={"approve": False})
    assert r.status_code == 400


async def test_rejection_completes_supervisor_request(admin, gate_agent, make_worker):
    llm = FakeLLM(
        json_responses=[{"reply": "Needs approval.", "steps": [{"action": "gate_test.publish", "params_json": "{\"title\": \"x\"}", "rationale": "asked"}]}],
        text_response="The publish step was rejected.",
    )
    req_id = await ask(admin, "publish x")
    worker = make_worker(llm=llm)
    await worker.run_until_idle()
    assert (await admin.get(f"/api/supervisor/requests/{req_id}")).json()["request"]["status"] == "awaiting_approval"
    approval = (await admin.get("/api/approvals")).json()["approvals"][0]
    await admin.post(f"/api/approvals/{approval['id']}/decision", json={"approve": False, "note": "not now"})
    await worker.run_until_idle()
    detail = (await admin.get(f"/api/supervisor/requests/{req_id}")).json()
    assert detail["request"]["status"] == "completed"
    publish = next(t for t in detail["tasks"] if t["capability"] == "publish")
    assert publish["status"] == "rejected" and "not now" in publish["error"]
    assert '"status": "rejected"' in llm.calls[-1]["prompt"]
    assert GateTestAgent.seen_approvers == []


async def test_supervisor_request_waits_for_approval(admin, gate_agent, make_worker, settings):
    llm = FakeLLM(
        json_responses=[{"reply": "Will publish after approval.", "steps": [{"action": "gate_test.publish", "params_json": "{\"title\": \"T\"}", "rationale": "asked"}]}],
        text_response="Published T after approval.",
    )
    req_id = await ask(admin, "publish T")
    worker = make_worker(llm=llm)
    await worker.run_until_idle()
    req = (await admin.get(f"/api/supervisor/requests/{req_id}")).json()["request"]
    assert req["status"] == "awaiting_approval"
    assert req["planner"] == "fake:test-model"
    approval = (await admin.get("/api/approvals")).json()["approvals"][0]
    await admin.post(f"/api/approvals/{approval['id']}/decision", json={"approve": True})
    await worker.run_until_idle()
    req = (await admin.get(f"/api/supervisor/requests/{req_id}")).json()["request"]
    assert req["status"] == "completed"
    assert req["summary"] == "Published T after approval."
    assert req["summarizer"] == "fake:test-model"
    # the summariser is told agent output is data
    assert "<results>" in llm.calls[-1]["prompt"]


async def test_cancel_awaiting_task(admin, gate_agent):
    task = (await admin.post("/api/agents/gate_test/tasks", json={"capability": "publish", "params": {"title": "x"}})).json()["task"]
    r = await admin.post(f"/api/tasks/{task['id']}/cancel")
    assert r.json()["task"]["status"] == "cancelled"
    approvals = (await admin.get("/api/approvals?status=all")).json()["approvals"]
    assert approvals[0]["status"] == "rejected"
    assert (await admin.post(f"/api/tasks/{task['id']}/cancel")).status_code == 400


async def test_failing_task_is_recorded_and_retryable(admin, gate_agent, make_worker):
    task = (await admin.post("/api/agents/gate_test/tasks", json={"capability": "explode", "params": {}})).json()["task"]
    await make_worker().run_until_idle()
    detail = (await admin.get(f"/api/tasks/{task['id']}")).json()
    assert detail["task"]["status"] == "failed"
    assert "kaboom" in detail["task"]["error"]
    assert [entry["message"] for entry in detail["logs"]][0] == "about to fail"
    kinds = [e["kind"] for e in (await admin.get("/api/events")).json()["events"]]
    assert "task.failed" in kinds
    r = await admin.post(f"/api/tasks/{task['id']}/retry")
    assert r.status_code == 201 and r.json()["task"]["status"] == "queued"


async def test_task_timeout(admin, gate_agent, make_worker, settings):
    fast = Settings(**{**settings.model_dump(), "task_timeout_seconds": 1})
    task = (await admin.post("/api/agents/gate_test/tasks", json={"capability": "slow", "params": {}})).json()["task"]
    await make_worker(settings=fast).run_until_idle()
    detail = (await admin.get(f"/api/tasks/{task['id']}")).json()["task"]
    assert detail["status"] == "failed" and "timed out" in detail["error"]


async def test_stale_running_task_is_reaped(admin, settings, make_worker):
    async with session_scope() as s:
        t = await task_service.create_task(s, settings, agent="monitoring", capability="sites_overview")
        await s.flush()
        await s.execute(
            update(Task)
            .where(Task.id == t.id)
            .values(status="running", locked_by="ghost", heartbeat_at=datetime.now(UTC) - timedelta(hours=1))
        )
        tid = t.id
    await make_worker().run_until_idle()
    task = (await admin.get(f"/api/tasks/{tid}")).json()["task"]
    assert task["status"] == "failed" and "ghost" in task["error"]


async def test_scheduler_fires_and_does_not_pile_up(clean_db, settings):
    async with session_scope() as s:
        s.add(Schedule(name="every-minute", agent="monitoring", capability="sites_overview", params={}, cron="* * * * *"))
    assert await scheduler.enqueue_due(settings) == 0  # first pass only schedules the next slot
    future = datetime.now(UTC) + timedelta(minutes=2)
    assert await scheduler.enqueue_due(settings, now=future) == 1
    assert await scheduler.enqueue_due(settings, now=future + timedelta(minutes=2)) == 0  # previous still queued
    async with session_scope() as s:
        tasks = (await s.scalars(select(Task).where(Task.source == "schedule"))).all()
    assert len(tasks) == 1 and tasks[0].created_by == "schedule:every-minute"


async def test_daily_briefing_and_health_review(admin, web, make_worker):
    web.add("down.example.test", static_site("x", status=500))
    await add_site(admin, "Broken", "https://down.example.test")
    worker = make_worker()
    await worker.run_until_idle()
    req_id = await ask(admin, "check all websites")
    await worker.run_until_idle()
    req_id = await ask(admin, "Give me the daily briefing")
    await worker.run_until_idle()
    req = (await admin.get(f"/api/supervisor/requests/{req_id}")).json()["request"]
    assert req["status"] == "completed"
    assert "1 site(s) with problems" in req["summary"]
    async with session_scope() as s:
        briefing = await s.scalar(select(Artifact).where(Artifact.kind == "briefing"))
    assert "Broken** is down" in briefing.content
    artifacts = (await admin.get("/api/artifacts")).json()["artifacts"]
    assert artifacts and "content" not in artifacts[0]
    assert "Broken" in (await admin.get(f"/api/artifacts/{briefing.id}")).json()["artifact"]["content"]

    req_id = await ask(admin, "Is the system healthy?")
    await worker.run_until_idle()
    tasks = (await admin.get(f"/api/supervisor/requests/{req_id}")).json()["tasks"]
    review = next(t for t in tasks if t["capability"] == "health_review")
    codes = {f["code"] for f in review["result"]["findings"]}
    assert "llm_not_configured" in codes
    assert "no_worker" not in codes


async def test_unmatched_request_completes_with_help(admin, make_worker):
    req_id = await ask(admin, "bake a cake")
    await make_worker().run_until_idle()
    req = (await admin.get(f"/api/supervisor/requests/{req_id}")).json()["request"]
    assert req["status"] == "completed"
    assert "could not match" in req["summary"]


async def test_concurrent_workers_never_double_run(admin, web, make_worker, settings):
    web.add("acme.example.test", static_site())
    site = await add_site(admin, "Acme", "https://acme.example.test")
    for _ in range(12):
        await admin.post(f"/api/sites/{site['id']}/check", json={"deep": False})
    w1, w2 = make_worker(), make_worker()
    w2.worker_id = "test-worker-2"
    await asyncio.gather(w1.run_until_idle(), w2.run_until_idle())
    async with session_scope() as s:
        tasks = (await s.scalars(select(Task).where(Task.capability == "check_site"))).all()
        checks = (await s.scalars(select(SiteCheck))).all()
    assert len(tasks) == 12 and all(t.status == "succeeded" and t.attempts == 1 for t in tasks)
    assert len(checks) == 13  # the twelve, plus the baseline scan queued at registration


async def test_check_url_for_unregistered_site_is_not_stored(admin, web, make_worker):
    web.add("other.example.test", static_site())
    r = await admin.post("/api/agents/monitoring/tasks", json={"capability": "check_url", "params": {"url": "other.example.test", "deep": False}})
    await make_worker().run_until_idle()
    task = (await admin.get(f"/api/tasks/{r.json()['task']['id']}")).json()["task"]
    assert task["status"] == "succeeded" and "not stored" in task["summary"]
    async with session_scope() as s:
        assert (await s.scalars(select(Site))).all() == []
