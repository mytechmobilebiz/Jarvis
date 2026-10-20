"""The Research Center's API (app/api/research.py) on PostgreSQL: every route's roles (viewers and the n8n service
token never get in), research off by default, mock and live switched on by an admin only and live only with the typed
words, the per-agent policy (Credit Repair refused), the connectivity test, purge, history and citations, blocked
attempts, usage and health; audit entries with ids, codes and counts only."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select

from app import licensing
from app.config import get_settings
from app.db import session_scope
from app.models import (
    AuditLog,
    IntegrationStatus,
    Lead,
    ResearchPolicy,
    ResearchQuery,
    ResearchReservation,
    ResearchSource,
    ResearchUsageDaily,
    Task,
    User,
)
from app.research import health
from app.research.gateway import Gateway

from ..research_fakes import (
    ADMIN,
    OPERATOR,
    CountingProvider,
    add_people,
    as_,
    forbid_real_http,
    research_on,
)

pytestmark = pytest.mark.usefixtures("clean_db")

LIVE = "ENABLE LIVE RESEARCH"


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch):
    forbid_real_http(monkeypatch)


@pytest.fixture
async def client(app_client):
    await add_people()
    return app_client


async def audit_entries(action: str) -> list[AuditLog]:
    async with session_scope() as s:
        return list((await s.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id))).all())


# --- Roles ----------------------------------------------------------------------------------------------------------


ROUTES = [
    # (method, route, path, body, least role)
    ("GET", "/api/research/health", "/api/research/health", None, "operator"),
    ("GET", "/api/research/usage", "/api/research/usage", None, "operator"),
    ("GET", "/api/research/queries", "/api/research/queries", None, "operator"),
    ("GET", "/api/research/queries/{query_id}", "/api/research/queries/999999", None, "operator"),
    ("GET", "/api/research/sources", "/api/research/sources", None, "operator"),
    ("GET", "/api/research/sources/{source_id}", "/api/research/sources/999999", None, "operator"),
    ("GET", "/api/research/blocked", "/api/research/blocked", None, "operator"),
    ("GET", "/api/research/settings", "/api/research/settings", None, "admin"),
    ("PATCH", "/api/research/settings", "/api/research/settings", {}, "admin"),
    ("GET", "/api/research/policy", "/api/research/policy", None, "admin"),
    ("GET", "/api/research/policy/{agent}", "/api/research/policy/seo_content", None, "admin"),
    ("PATCH", "/api/research/policy/{agent}", "/api/research/policy/seo_content", {}, "admin"),
    ("POST", "/api/research/test", "/api/research/test", {}, "admin"),
    ("POST", "/api/research/purge", "/api/research/purge", {"older_than_days": 1}, "admin"),
]
RANK = {"viewer": 0, "operator": 1, "admin": 2}


async def test_every_research_route_refuses_viewers_and_the_service_token(client, service_headers):
    # The list above covers every route the router has.
    paths = client._transport.app.openapi()["paths"]
    routes = {(m.upper(), path) for path, ops in paths.items() if path.startswith("/api/research") for m in ops}
    assert routes == {(method, route) for method, route, _, _, _ in ROUTES}

    for method, _, path, body, least in ROUTES:
        for who in ("viewer", "operator", "admin"):
            await as_(client, who)
            r = await client.request(method, path, json=body) if body is not None else await client.request(method, path)
            if RANK[who] < RANK[least]:
                assert r.status_code == 403, (method, path, who, r.status_code)
            else:
                assert r.status_code != 403 and r.status_code < 500, (method, path, who, r.status_code, r.text)
        client.cookies.clear()
        r = await client.request(method, path, json=body, headers=service_headers) if body is not None else \
            await client.request(method, path, headers=service_headers)
        assert r.status_code == 403, (method, path, "service", r.status_code)
        r = await client.request(method, path, json=body) if body is not None else await client.request(method, path)
        assert r.status_code == 401, (method, path, "signed out", r.status_code)


async def test_running_research_needs_an_operator_never_a_viewer_or_the_service_token(client, service_headers):
    await research_on("mock")
    body = {"capability": "research_topic", "params": {"query": "content marketing ideas"}}
    await as_(client, "viewer")
    assert (await client.post("/api/agents/seo_content/tasks", json=body)).status_code == 403
    client.cookies.clear()
    assert (await client.post("/api/agents/seo_content/tasks", json=body, headers=service_headers)).status_code == 403
    await as_(client, "operator")
    assert (await client.post("/api/agents/seo_content/tasks", json=body)).status_code == 201


# --- Settings ---------------------------------------------------------------------------------------------------------


async def test_research_is_off_until_an_admin_switches_it_and_live_needs_the_typed_words(client):
    await as_(client, "admin")
    shown = (await client.get("/api/research/settings")).json()["settings"]
    assert shown["mode"] == "off" and shown["readable"] and shown["global_daily_cap"] == 50
    assert shown["person_daily_cap"] == 30 and shown["engines"] == ["duckduckgo", "brave", "wikipedia"]
    assert "not available in this build" in shown["deepseek"] and "not available" in shown["fetch"]
    assert shown["live_confirm"] == LIVE

    await as_(client, "operator")
    assert (await client.patch("/api/research/settings", json={"mode": "mock"})).status_code == 403
    await as_(client, "admin")
    assert (await client.patch("/api/research/settings", json={"mode": "mock"})).json()["settings"]["mode"] == "mock"
    for confirm in ("", "enable live", "ENABLE LIVE"):
        r = await client.patch("/api/research/settings", json={"mode": "live", "confirm": confirm})
        assert r.status_code == 400 and LIVE in r.json()["detail"]
    r = await client.patch("/api/research/settings", json={"mode": "live", "confirm": LIVE})
    assert r.status_code == 200 and r.json()["settings"]["mode"] == "live"
    assert (await client.patch("/api/research/settings", json={"mode": "off"})).json()["settings"]["mode"] == "off"
    changes = [(e.detail["from"], e.detail["to"]) for e in await audit_entries("research.mode_changed")]
    assert changes == [("off", "mock"), ("mock", "live"), ("live", "off")]
    assert (await client.patch("/api/research/settings", json={"mode": "unknown"})).status_code == 422


async def test_settings_changes_are_audited_with_counts_never_the_owners_names(client):
    await as_(client, "admin")
    r = await client.patch("/api/research/settings", json={
        "global_daily_cap": 40, "person_daily_cap": 0, "owner_terms": ["Infinite Marketing", "  Infinite  Marketing "]})
    shown = r.json()["settings"]
    assert (shown["global_daily_cap"], shown["person_daily_cap"], shown["owner_terms"]) == (40, 0, ["Infinite Marketing"])
    (entry,) = await audit_entries("research.settings_changed")
    assert entry.detail == {"fields": ["global_daily_cap", "owner_terms", "person_daily_cap"], "global_daily_cap": 40,
                            "person_daily_cap": 0, "owner_terms": 1}
    assert "Infinite" not in json.dumps(entry.detail)
    for bad in ({"global_daily_cap": -1}, {"logs_days": 0}, {"owner_terms": [""]}, {"surprise": 1},
                {"owner_terms": ["x" * 201]}):
        assert (await client.patch("/api/research/settings", json=bad)).status_code == 422, bad
    assert (await client.patch("/api/research/settings", json={})).status_code == 400


async def test_unreadable_settings_show_as_off_and_saving_repairs_them(client):
    await research_on("on")  # not a mode: every search refuses
    await as_(client, "admin")
    shown = (await client.get("/api/research/settings")).json()["settings"]
    assert (shown["mode"], shown["readable"]) == ("off", False)
    assert (await client.post("/api/research/test", json={})).status_code == 400
    r = await client.patch("/api/research/settings", json={"mode": "mock"})
    assert r.json()["settings"]["mode"] == "mock" and r.json()["settings"]["readable"]
    assert (await audit_entries("research.mode_changed"))[-1].detail == {"from": "unreadable", "to": "mock"}


async def test_an_edition_without_web_research_cannot_switch_it_on(client, settings):
    app = client._transport.app
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(update={"edition": "personal"})
    licensing.clear_cache()
    try:
        await as_(client, "admin")
        r = await client.patch("/api/research/settings", json={"mode": "mock"})
        assert r.status_code == 403 and "edition" in r.json()["detail"]
        assert (await client.get("/api/research/settings")).json()["settings"]["entitled"] is False
    finally:
        app.dependency_overrides.clear()
        licensing.clear_cache()


# --- Policy -----------------------------------------------------------------------------------------------------------


async def test_the_agent_matrix_and_its_changes(client):
    await research_on("mock")
    await as_(client, "admin")
    policies = {p["agent"]: p for p in (await client.get("/api/research/policy")).json()["policies"]}
    assert (policies["seo_content"]["search_enabled"], policies["seo_content"]["search_daily_cap"],
            policies["seo_content"]["data_classes"]) == (True, 20, ["G", "O"])
    assert policies["supervisor"]["capabilities"] == ["web_search"]  # the connectivity test is internal
    assert policies["wordpress_security"]["data_classes"] == ["S"]
    assert policies["credit_repair"]["never"] and not policies["credit_repair"]["search_enabled"]
    assert policies["communications"]["search_enabled"] is False

    r = await client.patch("/api/research/policy/seo_content", json={
        "search_daily_cap": 5, "data_classes": ["C", "G", "O", "G"], "blocked_domains": ["Spam.Example.", "x.org"]})
    shown = r.json()["policy"]
    assert (shown["search_daily_cap"], shown["data_classes"], shown["blocked_domains"]) == (
        5, ["C", "G", "O"], ["spam.example", "x.org"])
    assert shown["updated_by"] == ADMIN
    (entry,) = await audit_entries("research.policy_changed")
    assert entry.target_id == "seo_content" and entry.detail["blocked_domains"] == 2
    assert "spam" not in json.dumps(entry.detail)

    r = await client.patch("/api/research/policy/credit_repair", json={"search_enabled": True, "search_daily_cap": 9})
    assert r.status_code == 400 and "Credit Repair" in r.json()["detail"]
    assert (await client.patch("/api/research/policy/ghost", json={"search_enabled": True})).status_code == 404
    assert (await client.get("/api/research/policy/ghost")).status_code == 404
    for bad in ({"data_classes": ["X"]}, {"blocked_domains": ["not a domain"]}, {"search_daily_cap": -2}):
        assert (await client.patch("/api/research/policy/seo_content", json=bad)).status_code == 422, bad
    # An agent without a row gets one on its first change (off until switched on).
    async with session_scope() as s:
        await s.execute(delete(ResearchPolicy).where(ResearchPolicy.agent == "monitoring"))
    shown = (await client.patch("/api/research/policy/monitoring", json={"search_daily_cap": 3})).json()["policy"]
    assert shown["exists"] and not shown["search_enabled"] and shown["search_daily_cap"] == 3


# --- The connectivity test ---------------------------------------------------------------------------------------------


async def test_the_connectivity_test_is_an_admins_and_starts_nowhere_else(client, make_worker, settings):
    await as_(client, "admin")
    r = await client.post("/api/research/test", json={})
    assert r.status_code == 400 and "switched off" in r.json()["detail"]
    await research_on("mock")
    r = await client.post("/api/research/test", json={})
    assert r.status_code == 201
    task = r.json()["task"]
    assert (task["agent"], task["capability"], task["source"], task["created_by"], task["status"]) == (
        "supervisor", "research_connectivity_test", "research_test", ADMIN, "queued")
    (entry,) = await audit_entries("research.test_requested")
    assert entry.detail == {"task_id": task["id"], "mode": "mock"}

    mock = CountingProvider()
    await make_worker(research=Gateway(settings, providers={"mock": mock})).run_until_idle()
    done = (await client.get(f"/api/tasks/{task['id']}")).json()["task"]
    assert done["status"] == "succeeded" and done["summary"].startswith("Connectivity test #")
    assert done["summary"].endswith(": 3 sources") and len(mock.calls) == 1
    async with session_scope() as s:
        row = await s.scalar(select(ResearchQuery).where(ResearchQuery.task_id == task["id"]))
    assert (row.trigger_source, row.requested_by, row.status) == ("test", ADMIN, "ok")
    # It always asks the provider: a second test is not answered from the cache.
    await client.post("/api/research/test", json={})
    await make_worker(research=Gateway(settings, providers={"mock": mock})).run_until_idle()
    assert len(mock.calls) == 2

    # Direct runs and retries never start it, whoever asks.
    for who in ("operator", "admin"):
        await as_(client, who)
        r = await client.post("/api/agents/supervisor/tasks", json={"capability": "research_connectivity_test",
                                                                   "params": {}})
        assert r.status_code == 400, who
    async with session_scope() as s:
        failed = Task(agent="supervisor", capability="research_connectivity_test", params={}, risk="read",
                      status="failed", source="research_test", created_by=ADMIN)
        s.add(failed)
        await s.flush()
        failed_id = failed.id
    r = await client.post(f"/api/tasks/{failed_id}/retry")
    assert r.status_code == 400 and "Research Center" in r.json()["detail"]


async def test_an_operator_cannot_start_the_connectivity_test_even_as_a_queued_task(client, make_worker, settings):
    await research_on("mock")
    async with session_scope() as s:  # a row as if some other path had queued it for an operator
        task = Task(agent="supervisor", capability="research_connectivity_test", params={}, risk="read",
                    status="queued", source="research_test", created_by=OPERATOR)
        s.add(task)
        await s.flush()
        task_id = task.id
    mock = CountingProvider()
    await make_worker(research=Gateway(settings, providers={"mock": mock})).run_until_idle()
    async with session_scope() as s:
        done = await s.get(Task, task_id)
    assert done.summary.endswith("refused: person_not_allowed") and mock.calls == []


# --- History, citations, blocked attempts, usage ----------------------------------------------------------------------


async def test_history_detail_sources_blocked_and_usage(client, make_worker, settings):
    await research_on("mock")
    async with session_scope() as s:
        lead = Lead(name="Sacha Coe", company="x")
        s.add(lead)
        await s.flush()
        lead_id = lead.id
    await as_(client, "operator")
    r = await client.post("/api/agents/seo_content/tasks",
                          json={"capability": "research_topic", "params": {"query": "content   marketing ideas"}})
    assert r.status_code == 201 and r.json()["task"]["params"]["query"] == "content marketing ideas"
    r = await client.post("/api/agents/seo_content/tasks",
                          json={"capability": "research_topic", "params": {"query": "Sacha Coe reviews"}})
    # The message never names the record: a Credit Repair client's name gets the same words (test_research_index.py).
    assert r.status_code == 400 and r.json()["detail"] == (
        "The search was refused (personal_record): it names a person or business from this business's own records. "
        "Nothing was searched or stored.")
    await make_worker(research=Gateway(settings, providers={"mock": CountingProvider()})).run_until_idle()

    history = (await client.get("/api/research/queries")).json()
    assert history["total"] == 2
    by_status = {q["status"]: q for q in history["queries"]}
    ok, refused = by_status["ok"], by_status["blocked"]
    assert (ok["status"], ok["query_text"], ok["requested_by"], ok["trigger_source"], ok["result_count"]) == (
        "ok", "content marketing ideas", OPERATOR, "dashboard", 4)
    assert "cache_key" not in ok and "cost_micro" not in ok
    assert (refused["status"], refused["query_text"], refused["blocked_by_ref"]) == ("blocked", None, None)
    assert (await client.get("/api/research/queries", params={"status": "blocked"})).json()["total"] == 1
    assert (await client.get("/api/research/queries", params={"agent": "supervisor"})).json()["total"] == 0

    detail = (await client.get(f"/api/research/queries/{ok['id']}")).json()
    assert [c["citation"] for c in detail["citations"]] == ["[1]", "[2]", "[3]", "[4]"]
    first = detail["citations"][0]
    assert first["url"].startswith("https://example.com/research/") and first["domain"] == "example.com"
    assert first["provider"] == "mock" and first["retrieved_at"] and not first["source_purged"]
    source = (await client.get(f"/api/research/sources/{first['source_id']}")).json()
    assert source["times_cited"] == 1 and source["cited_by"][0]["query_id"] == ok["id"]
    assert "fetched_text" not in source["source"] and "promoted_item_id" not in source["source"]
    listed = (await client.get("/api/research/sources")).json()
    assert listed["total"] == 4 and {s["times_cited"] for s in listed["sources"]} == {1}
    assert first["source_id"] in {s["id"] for s in listed["sources"]} and "fetched_text" not in listed["sources"][0]
    on_example = sum(s["domain"] == "example.com" for s in listed["sources"])
    assert (await client.get("/api/research/sources", params={"domain": "Example.com."})).json()["total"] == on_example
    assert (await client.get("/api/research/sources", params={"domain": "other.example"})).json()["total"] == 0
    assert (await client.get("/api/research/queries/999999")).status_code == 404

    blocked = (await client.get("/api/research/blocked")).json()["blocked"]
    assert [(b["reason_code"], b["blocked_by_ref"], b["agent"]) for b in blocked] == [
        ("personal_record", None, "seo_content")]
    assert set(blocked[0]) == {"id", "created_at", "agent", "capability", "requested_by", "trigger_source", "status",
                               "reason_code", "blocked_by_ref", "task_id", "request_id"}
    # Only people with the sensitive-data permission see which lead caused it (a planted lead shows up for them).
    await as_(client, "admin")
    assert (await client.get("/api/research/blocked")).json()["blocked"][0]["blocked_by_ref"] is None
    async with session_scope() as s:
        (await s.scalar(select(User).where(User.email == ADMIN))).can_access_sensitive = True
    assert (await client.get("/api/research/blocked")).json()["blocked"][0]["blocked_by_ref"] == f"lead:{lead_id}"
    listed = (await client.get("/api/research/queries", params={"status": "blocked"})).json()["queries"]
    assert [q["blocked_by_ref"] for q in listed] == [f"lead:{lead_id}"]
    shown = (await client.get(f"/api/research/queries/{refused['id']}")).json()["query"]
    assert shown["blocked_by_ref"] == f"lead:{lead_id}"
    await as_(client, "operator")

    usage = (await client.get("/api/research/usage")).json()
    assert usage["provider"] == "mock" and usage["today"]["global"] == {"used": 1, "cap": 50}
    assert usage["today"]["person"] == {"who": OPERATOR, "used": 1, "cap": 30}
    seo = next(a for a in usage["today"]["agents"] if a["agent"] == "seo_content")
    assert (seo["used"], seo["cap"]) == (1, 20)
    assert {(d["agent"], d["sent"], d["blocked"]) for d in usage["days"]} == {("seo_content", 1, 1)}
    today = datetime.now(UTC).date()
    assert (await client.get("/api/research/usage", params={"from": str(today), "to": str(today - timedelta(1))})
            ).status_code == 400


async def test_health_shows_the_mode_engines_and_what_this_build_lacks(client):
    await as_(client, "operator")
    shown = (await client.get("/api/research/health")).json()
    assert shown["mode"] == "off" and shown["searxng"]["state"] == "off"
    assert shown["deepseek"] == {"available": False, "note": "DeepSeek: not available in this build (pending review)"}
    assert shown["fetch"]["available"] is False
    await research_on("live")
    assert (await client.get("/api/research/health")).json()["searxng"]["state"] == "unreachable"
    now = datetime.now(UTC)
    await health.record_probe(healthz_ok=True, config={"version": "2026.10.4+d48c4b555", "engines": []}, now=now)
    await health.record_search(asked=list(health.ENGINES), unresponsive=[{"engine": "brave", "reason": "captcha"}],
                               answered=True, now=now)
    shown = (await client.get("/api/research/health")).json()["searxng"]
    assert shown["state"] == "degraded" and shown["version_ok"] is True
    assert shown["engines"]["brave"]["state"] == "suspended" and shown["engines"]["brave"]["until"]
    assert "Brave asked for a CAPTCHA (paused for 60 minutes)" in shown["message"]
    async with session_scope() as s:
        s.add(IntegrationStatus(name="internet", detail={"state": "offline"}))
    assert (await client.get("/api/research/health")).json()["searxng"]["state"] == "pc_offline"
    await research_on("mock")
    assert (await client.get("/api/research/health")).json()["searxng"]["state"] == "mock"


# --- Purge ------------------------------------------------------------------------------------------------------------


async def test_purge_needs_the_typed_words_and_removes_records_and_research_task_data(client, make_worker, settings):
    await research_on("mock")
    await as_(client, "operator")
    r = await client.post("/api/agents/seo_content/tasks",
                          json={"capability": "research_topic", "params": {"query": "content marketing ideas"}})
    task_id = r.json()["task"]["id"]
    await make_worker(research=Gateway(settings, providers={"mock": CountingProvider()})).run_until_idle()
    await as_(client, "admin")
    for confirm in ("", "purge research"):
        r = await client.post("/api/research/purge", json={"older_than_days": 0, "confirm": confirm})
        assert r.status_code == 400
    r = await client.post("/api/research/purge", json={"older_than_days": 0, "include_usage": True,
                                                       "confirm": "PURGE RESEARCH"})
    counts = r.json()["purged"]
    assert counts["queries"] == 1 and counts["sources"] == 4 and counts["cache"] == 1 and counts["tasks"] == 1
    assert "backups" in r.json()["note"]
    async with session_scope() as s:
        task = await s.get(Task, task_id)
        assert await s.scalar(select(func.count()).select_from(ResearchQuery)) == 0
        assert await s.scalar(select(func.count()).select_from(ResearchSource)) == 0
        # Today's budget slots stay: purging never resets the caps.
        assert await s.scalar(select(func.count()).select_from(ResearchReservation)) == 1
        assert await s.scalar(select(func.count()).select_from(ResearchUsageDaily)) == 1  # today's row stays
    assert task.params == {} and task.result["data"] == {"purged": True} and task.summary.startswith("Topic research #")
    (entry,) = await audit_entries("research.purged")
    assert entry.detail["queries"] == 1 and entry.detail["older_than_days"] == 0 and entry.detail["tasks"] == 1
    assert "content" not in json.dumps(entry.detail)
