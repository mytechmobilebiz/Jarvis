"""Research capabilities end to end on PostgreSQL (plan sections 3.13, 3.18 and 7.3): screened before the task exists,
run by the worker with the reduced context, a fixed summary line and data.citations, never on a schedule, never inside
an approved task, never for Credit Repair; viewers see none of it; nothing a page says creates a task, an approval, an
outbox entry, a server command, a schedule, a finding or an event, or reaches an AI prompt or n8n. Also the worker's
SearXNG probe (only while research is live) and its research housekeeping."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import pytest
from sqlalchemy import func, select, text

from app import tasks as task_service
from app.agents import registry
from app.agents.base import AgentContext, AgentResult, BaseAgent, NoParams, Risk, capability
from app.agents.research import SEARCH, ResearchParams
from app.bootstrap import ensure_schedules
from app.db import session_scope
from app.models import (
    AgentState,
    Approval,
    Event,
    IntegrationStatus,
    Lead,
    OutboxEntry,
    ResearchCache,
    ResearchQuery,
    ResearchReservation,
    Schedule,
    ServerCommand,
    SupervisorRequest,
    Task,
    WorkflowDraft,
)
from app.research import ResearchClient, budget, health, policy, screen
from app.research.gateway import Gateway
from app.worker import scheduler

from ..fakes import FakeLLM
from ..research_fakes import (
    ADMIN,
    OPERATOR,
    CountingProvider,
    FakeSearXNG,
    HostileProvider,
    add_people,
    as_,
    forbid_real_http,
    research_on,
    searxng_answer,
    web_result,
)

pytestmark = pytest.mark.usefixtures("clean_db")

RUNS = [
    ("supervisor", "web_search", {"query": "WordPress 6.9 release notes"}, "Web search"),
    ("seo_content", "research_topic", {"query": "home care marketing ideas"}, "Topic research"),
    ("social_marketing", "trend_research", {"query": "short video marketing"}, "Trend research"),
    ("wordpress_security", "check_advisories", {"software": "WordPress", "version": "6.9"}, "Advisory search"),
    ("server_management", "check_advisories", {"software": "OpenSSH", "version": "9.6p1"}, "Advisory search"),
]


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch):
    forbid_real_http(monkeypatch)


@pytest.fixture
async def client(app_client):
    await add_people()
    return app_client


LOOSE = budget.Spacing(interval=0.001, burst=1000)


def research_worker(make_worker, settings, provider=None, **kwargs):
    provider = provider or CountingProvider()
    return make_worker(research=Gateway(settings, providers={"mock": provider}, spacing=LOOSE), **kwargs), provider


async def counts() -> dict[str, int]:
    async with session_scope() as s:
        return {m.__tablename__: await s.scalar(select(func.count()).select_from(m))
                for m in (Task, Approval, OutboxEntry, ServerCommand, Schedule, Event)}


async def dump(*tables: str) -> str:
    """Every row of these tables as JSON text."""
    out = []
    async with session_scope() as s:
        for table in tables:
            out += [str(r[0]) for r in await s.execute(text(f"SELECT row_to_json(x) FROM {table} x"))]
    return "\n".join(out)


# --- Direct runs ------------------------------------------------------------------------------------------------------


async def test_a_direct_run_is_screened_before_any_task_exists(client):
    await as_(client, "operator")
    body = {"capability": "research_topic", "params": {"query": "content marketing ideas"}}
    r = await client.post("/api/agents/seo_content/tasks", json=body)
    assert r.status_code == 400 and "research_off" in r.json()["detail"]
    await research_on("mock")
    refusals = {
        "jane.doe@example.com pricing": "contact_detail",
        "call 4 0 4 5 5 5 0 1 9 9 today": "contact_detail",
        "equifax dispute letters": "credit_topic",
        "!g wordpress": "search_syntax",
        "x" * 129: "too_long",
    }
    for query, code in refusals.items():
        r = await client.post("/api/agents/seo_content/tasks", json={**body, "params": {"query": query}})
        assert r.status_code == 400 and f"({code})" in r.json()["detail"], (query, r.text)
        assert query not in r.json()["detail"]
    r = await client.post("/api/agents/credit_repair/tasks", json={"capability": "web_search", "params": {}})
    assert r.status_code in (400, 403)
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(Task)) == 0
        rows = (await s.scalars(select(ResearchQuery).order_by(ResearchQuery.id))).all()
    assert [(q.status, q.reason_code) for q in rows] == [("refused_policy", "research_off"),
                                                        *(("blocked", c) for c in refusals.values())]
    assert all(q.query_text is None and q.task_id is None and q.trigger_source == "dashboard"
               and q.requested_by == OPERATOR for q in rows)
    stored = await dump("tasks", "task_logs", "events", "audit_log", "research_queries", "research_usage_daily")
    for query in refusals:
        assert query not in stored
    assert "jane" not in stored and "4045550199" not in stored

    # An invalid parameter is refused without repeating the query either.
    r = await client.post("/api/agents/seo_content/tasks", json={**body, "params": {"query": "jane" * 70}})
    assert r.status_code == 400 and "jane" not in r.text


@pytest.mark.parametrize("agent, cap, params, label", RUNS)
async def test_each_research_capability_runs_read_only_with_a_fixed_line_and_citations(client, make_worker, settings,
                                                                                      agent, cap, params, label):
    await research_on("mock")
    await as_(client, "operator")
    r = await client.post(f"/api/agents/{agent}/tasks", json={"capability": cap, "params": params})
    assert r.status_code == 201, r.text
    task = r.json()["task"]
    assert (task["status"], task["risk"]) == ("queued", "read")  # no approval
    worker, mock = research_worker(make_worker, settings)
    await worker.run_until_idle()
    done = (await client.get(f"/api/tasks/{task['id']}")).json()
    result = done["task"]["result"]
    assert done["task"]["status"] == "succeeded" and done["approval"] is None
    assert re.fullmatch(rf"{label} #\d+: \d sources", done["task"]["summary"]), done["task"]["summary"]
    assert result["findings"] == [] and len(mock.calls) == 1
    first = result["data"]["citations"][0]
    assert first["citation"] == "[1]" and first["provider"] == "mock" and first["url"].startswith("https://")
    assert {"title", "domain", "engines", "published_at", "retrieved_at", "result_id"} <= set(first)
    assert result["data"]["research"]["status"] == "ok" and "query" not in result["data"]["research"]
    if cap == "check_advisories":
        assert mock.calls[0].query == f"{params['software']} {params['version']} security advisory"
        assert result["data"]["cves"] == []
    if cap == "trend_research":
        assert mock.calls[0].time_range == "month"
    async with session_scope() as s:
        row = await s.scalar(select(ResearchQuery).where(ResearchQuery.task_id == task["id"]))
        assert await s.scalar(select(func.count()).select_from(Event)) == 0
    assert (row.agent, row.capability, row.trigger_source, row.requested_by) == (agent, cap, "dashboard", OPERATOR)


async def test_a_live_run_goes_to_the_private_searxng_by_post_and_nowhere_else(client, make_worker, settings, web):
    await research_on("live")
    await as_(client, "operator")
    r = await client.post("/api/agents/wordpress_security/tasks",
                          json={"capability": "check_advisories", "params": {"software": "WordPress", "version": "6.9"}})
    task_id = r.json()["task"]["id"]
    fake = FakeSearXNG(searxng_answer([web_result(content="Fixes CVE-2026-11111 and cve-2026-22222.")]))
    worker = make_worker(research=Gateway(settings, transport=fake.transport, spacing=LOOSE))
    await worker.run_until_idle()
    (request,) = fake.requests
    assert (request.method, str(request.url)) == ("POST", "http://searxng:8080/search")
    form = parse_qs(request.content.decode(), keep_blank_values=True)
    assert form["q"] == ["WordPress 6.9 security advisory"] and form["engines"] == ["duckduckgo,brave,wikipedia"]
    assert set(form) == {"q", "format", "engines", "language", "time_range", "pageno"}
    assert web.requests == []  # the worker's ordinary HTTP client was not used
    done = (await client.get(f"/api/tasks/{task_id}")).json()["task"]
    assert re.fullmatch(r"Advisory search #\d+: 1 sources", done["summary"])
    assert done["result"]["data"]["cves"] == ["CVE-2026-11111", "CVE-2026-22222"]
    assert done["result"]["data"]["research"]["provider"] == "searxng"
    await worker.research.aclose()


async def test_web_text_never_becomes_a_finding_an_event_a_task_or_an_n8n_message(client, make_worker, settings, n8n):
    await research_on("mock")
    await as_(client, "admin")
    before = await counts()
    ids = []
    for agent, cap, params, _ in RUNS:
        r = await client.post(f"/api/agents/{agent}/tasks", json={"capability": cap, "params": params})
        ids.append(r.json()["task"]["id"])
    worker, hostile = research_worker(make_worker, settings, HostileProvider())
    await worker.run_until_idle()
    after = await counts()
    assert after == {**before, "tasks": before["tasks"] + len(RUNS)}  # only the research tasks themselves
    async with session_scope() as s:
        tasks = (await s.scalars(select(Task).where(Task.id.in_(ids)))).all()
    assert all(t.status == "succeeded" and t.result["findings"] == [] and t.summary.endswith(" 4 sources")
               for t in tasks), [t.summary for t in tasks]
    advisory = next(t for t in tasks if t.agent == "wordpress_security")
    assert advisory.result["data"]["cves"] == ["CVE-2025-99999", "CVE-2026-01001", "CVE-2026-02001", "CVE-2026-03001",
                                               "CVE-2026-04001", "CVE-2026-12345"]
    flags = {f for t in tasks for c in t.result["data"]["citations"] for f in c["flags"]}
    assert "instruction_like" in flags
    for t in tasks:
        assert "approve" not in t.summary.lower() and "hostile" not in t.summary
    assert n8n.received == [] and await dump("events", "outbox") == ""
    assert "jane.doe" not in await dump("tasks", "research_sources", "research_results")  # cleaned before storing


async def test_viewers_never_see_research_parameters_or_results(client, make_worker, settings):
    await research_on("mock")
    await as_(client, "operator")
    r = await client.post("/api/agents/seo_content/tasks",
                          json={"capability": "research_topic", "params": {"query": "home care marketing ideas"}})
    task_id = r.json()["task"]["id"]
    await research_worker(make_worker, settings)[0].run_until_idle()
    seen = (await client.get(f"/api/tasks/{task_id}")).json()["task"]
    assert seen["params"]["query"] == "home care marketing ideas" and seen["result"]["data"]["citations"]
    await as_(client, "viewer")
    for task in ((await client.get(f"/api/tasks/{task_id}")).json()["task"],
                 (await client.get("/api/tasks")).json()["tasks"][0]):
        assert task["params"] == {} and task["result"]["data"] == {}
        assert task["summary"].startswith("Topic research #")
    assert "home care" not in (await client.get("/api/tasks")).text
    # Other tasks keep what viewers saw before.
    async with session_scope() as s:
        s.add(Task(agent="monitoring", capability="check_url", params={"url": "https://a.test"}, risk="read",
                   status="succeeded", result={"summary": "x", "data": {"url": "https://a.test"}}))
    plain = (await client.get("/api/tasks", params={"agent": "monitoring"})).json()["tasks"][0]
    assert plain["params"] == {"url": "https://a.test"} and plain["result"]["data"] == {"url": "https://a.test"}


async def test_a_persons_retry_is_screened_again(client, make_worker, settings):
    await research_on("mock")
    await as_(client, "operator")
    r = await client.post("/api/agents/seo_content/tasks",
                          json={"capability": "research_topic", "params": {"query": "Brightwater Plumbing reviews"}})
    task_id = r.json()["task"]["id"]
    async with session_scope() as s:
        (await s.get(Task, task_id)).status = "failed"
    r = await client.post(f"/api/tasks/{task_id}/retry")
    assert r.status_code == 201 and r.json()["task"]["source"] == "retry"
    retry_id = r.json()["task"]["id"]
    async with session_scope() as s:
        await s.execute(text("UPDATE tasks SET status = 'cancelled' WHERE id = :id"), {"id": task_id})
        s.add(Lead(name="Robin Okafor", company="Brightwater Plumbing LLC"))
    await research_worker(make_worker, settings)[0].run_until_idle()
    async with session_scope() as s:
        retried = await s.get(Task, retry_id)
        row = await s.scalar(select(ResearchQuery).where(ResearchQuery.task_id == retry_id))
    # The lead was added after the retry was queued: the gateway's own screen refuses it when the task runs.
    assert retried.summary.endswith("refused: personal_record") and row.trigger_source == "retry"
    r = await client.post(f"/api/tasks/{task_id}/retry")
    assert r.status_code == 400 and "(personal_record)" in r.json()["detail"]


async def test_a_query_refused_when_its_task_runs_is_removed_from_the_task(client, make_worker, settings):
    await research_on("mock")
    await as_(client, "operator")
    r = await client.post("/api/agents/supervisor/tasks",
                          json={"capability": "web_search", "params": {"query": "Kim Lambert reviews"}})
    assert r.status_code == 201
    task_id = r.json()["task"]["id"]
    async with session_scope() as s:  # added between queueing and running
        s.add(Lead(name="Kim Lambert"))
    worker, provider = research_worker(make_worker, settings)
    await worker.run_until_idle()
    async with session_scope() as s:
        task = await s.get(Task, task_id)
        row = await s.scalar(select(ResearchQuery).where(ResearchQuery.task_id == task_id))
    assert provider.calls == [] and task.summary.endswith("refused: personal_record") and task.params == {}
    assert (row.status, row.reason_code, row.query_text) == ("blocked", "personal_record", None)
    assert "Lambert" not in (await client.get(f"/api/tasks/{task_id}")).text  # an operator's view as well
    assert "Lambert" not in await dump("tasks", "task_logs", "events", "research_queries")


@pytest.mark.parametrize("target, name, code", [(screen, "load_stores", "screen_error"),
                                                (policy, "search_policy", "policy_error")])
async def test_an_intake_that_fails_refuses_before_any_task_exists(client, monkeypatch, target, name, code):
    await research_on("mock")
    await as_(client, "operator")
    async with session_scope() as s:  # a failed research task, for a person's retry
        failed = Task(agent="seo_content", capability="research_topic", params={"query": "marketing ideas"},
                      risk="read", status="failed", source="dashboard", created_by=OPERATOR)
        s.add(failed)
        await s.flush()
        failed_id = failed.id

    async def broken(*args, **kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(target, name, broken)
    attempts = [("/api/agents/seo_content/tasks", {"capability": "research_topic", "params": {"query": "marketing ideas"}}),
                (f"/api/tasks/{failed_id}/retry", None)]
    for path, body in attempts:
        r = await client.post(path, json=body)
        assert r.status_code == 400 and f"({code})" in r.json()["detail"], (path, r.text)
        async with session_scope() as s:
            assert await s.scalar(select(func.count()).select_from(Task)) == 1  # the failed task only
            row = await s.scalar(select(ResearchQuery).order_by(ResearchQuery.id.desc()).limit(1))
        assert (row.reason_code, row.query_text, row.task_id) == (code, None, None)
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(ResearchQuery)) == 2


# --- Schedules --------------------------------------------------------------------------------------------------------


async def test_research_never_runs_on_a_schedule(client, settings):
    await research_on("mock")
    async with session_scope() as s:
        for kwargs in ({"source": "schedule"}, {"schedule_id": None, "source": "schedule", "created_by": "schedule:x"}):
            with pytest.raises(task_service.TaskError, match="never runs on a schedule"):
                await task_service.create_task(s, settings, agent="seo_content", capability="research_topic",
                                               params={"query": "marketing ideas"}, **kwargs)
        sched = Schedule(name="weekly-research", agent="seo_content", capability="research_topic",
                         params={"query": "marketing ideas"}, cron="0 9 * * 1", enabled=True,
                         next_run_at=datetime.now(UTC) - timedelta(minutes=1))
        draft = WorkflowDraft(source_key="jarvis:job:r1", name="weekly-research", agent="seo_content",
                              capability="research_topic", params={"query": "marketing ideas"}, compatibility="mapped",
                              cron="0 9 * * 1", status="reviewed")
        s.add_all([sched, draft])
        await s.flush()
        sched_id, draft_id = sched.id, draft.id
        with pytest.raises(task_service.TaskError, match="never runs on a schedule"):
            await task_service.create_task(s, settings, agent="seo_content", capability="research_topic",
                                           params={"query": "marketing ideas"}, schedule_id=sched_id)
    # The scheduler never queues one, whatever the row says.
    assert await scheduler.enqueue_due(settings) == 0
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(Task)) == 0
        assert (await s.get(Schedule, sched_id)).next_run_at > datetime.now(UTC)
    # No route makes or enables one.
    await as_(client, "admin")
    r = await client.patch(f"/api/schedules/{sched_id}", json={"enabled": True})
    assert r.status_code == 400 and "never runs on a schedule" in r.json()["detail"]
    assert (await client.patch(f"/api/schedules/{sched_id}", json={"enabled": False})).status_code == 200
    r = await client.post(f"/api/brain/workflows/{draft_id}/convert", json={"jarvis_job_is_off": True})
    assert r.status_code == 400 and "never runs on a schedule" in r.json()["detail"]
    # Nor does start-up.
    await ensure_schedules([{"name": "research-sweep", "agent": "supervisor", "capability": "web_search",
                             "params": {"query": "x"}, "cron": "0 * * * *"}])
    async with session_scope() as s:
        names = set((await s.scalars(select(Schedule.name))).all())
    assert "research-sweep" not in names and len(names) == 1 + 3  # the row above and the default schedules


# --- Isolation --------------------------------------------------------------------------------------------------------


class ProbeAgent(BaseAgent):
    """A stand-in with one research capability and one ordinary one, recording what each gets."""

    name = "research_probe"
    title = "Research probe"
    description = "test agent"
    seen: dict[str, dict] = {}

    @capability(risk=Risk.READ, research=SEARCH, description="searches", params=ResearchParams)
    async def look(self, ctx: AgentContext, params: ResearchParams) -> AgentResult:
        type(self).seen["look"] = {"ssh": ctx.ssh, "integrations": ctx.integrations, "llm": ctx.llm, "http": ctx.http,
                                   "memory": ctx.memory, "research": ctx.research}
        return AgentResult(summary="looked")

    @capability(risk=Risk.READ, research=SEARCH, description="breaks", params=ResearchParams)
    async def broken(self, ctx: AgentContext, params: ResearchParams) -> AgentResult:
        raise RuntimeError(f"could not search for {params.query}")

    @capability(risk=Risk.READ, description="plain", params=NoParams)
    async def plain(self, ctx: AgentContext, params: NoParams) -> AgentResult:
        type(self).seen["plain"] = {"ssh": ctx.ssh, "llm": ctx.llm, "http": ctx.http, "research": ctx.research}
        return AgentResult(summary="plain")


class FakeCreditRepair(ProbeAgent):
    name = "credit_repair"


@pytest.fixture
def probe_agents(monkeypatch):
    agents = dict(registry.all_agents())
    agents["research_probe"] = ProbeAgent()
    agents["credit_repair"] = FakeCreditRepair()
    ProbeAgent.seen = {}
    monkeypatch.setattr(registry, "_AGENTS", agents)
    return agents


async def queue(agent: str, cap: str, params: dict, **extra) -> int:
    async with session_scope() as s:
        task = Task(agent=agent, capability=cap, params=params, risk="read", status="queued", source="dashboard",
                    created_by=ADMIN, **extra)
        s.add(task)
        await s.flush()
        return task.id


async def test_a_research_capability_gets_the_reduced_context_and_credit_repair_no_client(probe_agents, make_worker,
                                                                                          settings):
    await research_on("mock")
    look_id = await queue("research_probe", "look", {"query": "x"})
    await queue("research_probe", "plain", {})
    await research_worker(make_worker, settings, llm=FakeLLM())[0].run_until_idle()
    look, plain = ProbeAgent.seen["look"], ProbeAgent.seen["plain"]
    assert {k: v for k, v in look.items() if k != "research"} == {
        "ssh": None, "integrations": None, "llm": None, "http": None, "memory": None}
    assert isinstance(look["research"], ResearchClient)
    assert (look["research"].agent, look["research"].task_id) == ("research_probe", look_id)
    assert plain["research"] is None and plain["ssh"] is not None and plain["llm"] is not None
    assert plain["http"] is not None

    ProbeAgent.seen = {}
    await queue("credit_repair", "look", {"query": "x"})
    await research_worker(make_worker, settings)[0].run_until_idle()
    assert ProbeAgent.seen["look"]["research"] is None and ProbeAgent.seen["look"]["ssh"] is None


async def test_a_failing_research_task_records_a_code_never_its_text(probe_agents, make_worker, settings, n8n, caplog):
    await research_on("mock")
    task_id = await queue("research_probe", "broken", {"query": "unusual canary words"})
    caplog.set_level("DEBUG")
    await research_worker(make_worker, settings)[0].run_until_idle()
    async with session_scope() as s:
        task = await s.get(Task, task_id)
    assert (task.status, task.error) == ("failed", "RuntimeError")
    assert "canary" not in await dump("task_logs", "events") and "canary" not in caplog.text
    assert "canary" not in json.dumps([m["body"] for m in n8n.received])


async def test_a_research_task_that_carries_an_approval_is_refused(make_worker, settings):
    await research_on("mock")
    task_id = await queue("seo_content", "research_topic", {"query": "marketing ideas"})
    async with session_scope() as s:
        s.add(Approval(task_id=task_id, agent="seo_content", capability="research_topic", risk="read", summary="x",
                       status="approved", decided_by=ADMIN))
    worker, mock = research_worker(make_worker, settings)
    await worker.run_until_idle()
    async with session_scope() as s:
        task = await s.get(Task, task_id)
    assert task.status == "succeeded" and task.summary.endswith("refused: task_has_approval") and mock.calls == []


async def test_switched_off_agents_and_policies_refuse_at_run_time(make_worker, settings):
    await research_on("mock")
    off = await queue("social_marketing", "trend_research", {"query": "short video marketing"})
    disabled = await queue("seo_content", "research_topic", {"query": "marketing ideas"})
    async with session_scope() as s:
        s.add(AgentState(name="social_marketing", enabled=False))
        await s.execute(text("UPDATE research_policy SET search_enabled = false WHERE agent = 'seo_content'"))
    worker, mock = research_worker(make_worker, settings)
    await worker.run_until_idle()
    async with session_scope() as s:
        assert (await s.get(Task, off)).status == "cancelled"  # switched off: withdrawn, never run
        assert (await s.get(Task, disabled)).summary.endswith("refused: search_disabled")
    assert mock.calls == []


# --- The worker's own research work -------------------------------------------------------------------------------------


async def test_the_worker_probes_searxng_only_while_research_is_live(make_worker, settings):
    fake = FakeSearXNG()
    worker = make_worker(research=Gateway(settings, transport=fake.transport))
    for mode in (None, "off", "mock"):
        if mode:
            await research_on(mode)
        worker._last_research_probe = 0.0
        await worker.tick()
    assert fake.probes == [] and fake.requests == []
    await research_on("live")
    worker._last_research_probe = 0.0
    await worker.tick()
    assert fake.probes == ["/healthz", "/config"]
    async with session_scope() as s:
        detail = (await s.get(IntegrationStatus, health.ROW)).detail
    assert detail["healthz"] == "ok" and detail["version_ok"] is True
    assert detail["enabled_engines"] == ["brave", "duckduckgo", "wikipedia"]
    await worker.tick()  # within the minute: no probe
    worker._last_research_probe = 0.0
    await worker.tick()  # the next minute: /healthz only (/config is hourly)
    assert fake.probes == ["/healthz", "/config", "/healthz"]
    fake.healthy = False
    worker._last_research_probe = 0.0
    await worker.tick()
    async with session_scope() as s:
        row = await s.get(IntegrationStatus, health.ROW)
    assert row.detail["healthz"] == "failed" and row.last_error == "unreachable"
    await research_on("off")
    worker._last_research_probe = 0.0
    await worker.tick()
    assert len(fake.probes) == 4 and fake.requests == []
    await worker.research.aclose()


async def test_the_worker_keeps_research_house_in_its_own_loop(make_worker, settings):
    await research_on("mock")
    old = datetime.now(UTC) - timedelta(minutes=20)
    async with session_scope() as s:
        s.add(ResearchQuery(agent="seo_content", trigger_source="dashboard", status="running", query_text="x y",
                            created_at=old))
        s.add(ResearchReservation(created_at=old, day=old.date(), provider="mock", agent="seo_content",
                                  status="reserved"))
        s.add(ResearchCache(cache_key="k", kind="search", provider="mock", response={}, created_at=old,
                            expires_at=old + timedelta(minutes=1)))
        s.add(Task(agent="seo_content", capability="research_topic", params={"query": "old words"}, risk="read",
                   status="succeeded", summary="Topic research #1: 3 sources",
                   result={"summary": "Topic research #1: 3 sources", "data": {"citations": [{"title": "t"}]}},
                   finished_at=datetime.now(UTC) - timedelta(days=91)))
        s.add(Task(agent="monitoring", capability="check_url", params={"url": "https://a.test"}, risk="read",
                   status="succeeded", result={"summary": "x", "data": {"url": "https://a.test"}},
                   finished_at=datetime.now(UTC) - timedelta(days=400)))
        # A Supervisor plan's copies of a query: the request's plan and its planning task's result.
        plan = {"planner": "fake", "reply": "Searching.", "notes": [], "steps": [
            {"agent": "supervisor", "capability": "web_search", "params": {"query": "old words"}, "rationale": "asked"},
            {"agent": "supervisor", "capability": "health_review", "params": {}, "rationale": "health"}]}
        for days in (91, 1):
            at = datetime.now(UTC) - timedelta(days=days)
            req = SupervisorRequest(message="search the web for old words", source="dashboard", status="completed",
                                    requested_by="admin@test.local", plan=plan, created_at=at)
            s.add(req)
            await s.flush()
            s.add(Task(agent="supervisor", capability="plan_request", params={"request_id": req.id}, risk="read",
                       status="succeeded", request_id=req.id, finished_at=at,
                       result={"summary": "Planned 2 task(s)", "data": {"plan": plan, "task_ids": []}}))
    worker = make_worker(research=Gateway(settings, providers={"mock": CountingProvider()}))
    await worker.tick()
    async with session_scope() as s:
        query = await s.scalar(select(ResearchQuery))
        slot = await s.scalar(select(ResearchReservation))
        tasks = {t.agent: t for t in (await s.scalars(select(Task))).all()}
        assert await s.scalar(select(func.count()).select_from(ResearchCache)) == 0
    assert (query.status, query.reason_code) == ("failed", "abandoned") and slot.status == "expired"
    research = tasks["seo_content"]
    assert research.params == {} and research.result["data"] == {"purged": True}
    assert research.result["summary"] == research.summary == "Topic research #1: 3 sources"
    assert tasks["monitoring"].params == {"url": "https://a.test"}  # other tasks are not research's to purge
    async with session_scope() as s:
        plans = [r.plan for r in (await s.scalars(select(SupervisorRequest).order_by(SupervisorRequest.id))).all()]
        plans += [t.result["data"]["plan"] for t in (await s.scalars(
            select(Task).where(Task.capability == "plan_request").order_by(Task.id))).all()]
    old_plan, new_plan, old_task_plan, new_task_plan = plans
    for purged in (old_plan, old_task_plan):  # after the log retention: no query and no rationale for the search
        assert purged["steps"] == [{**plan["steps"][0], "params": {}, "rationale": ""}, plan["steps"][1]]
    assert new_plan == new_task_plan == plan


async def test_housekeeping_purges_nothing_when_the_settings_cannot_be_read(make_worker, settings):
    await research_on("on")
    old = datetime.now(UTC) - timedelta(days=2)
    async with session_scope() as s:
        s.add(ResearchCache(cache_key="k", kind="search", provider="mock", response={}, created_at=old,
                            expires_at=old + timedelta(hours=1)))
    await make_worker(research=Gateway(settings, providers={"mock": CountingProvider()})).tick()
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(ResearchCache)) == 1


async def test_audit_and_logs_hold_no_query_text(client, make_worker, settings, caplog):
    await research_on("mock")
    await as_(client, "operator")
    caplog.set_level("DEBUG")
    await client.post("/api/agents/seo_content/tasks",
                      json={"capability": "research_topic", "params": {"query": "unusual canary marketing words"}})
    await client.post("/api/agents/seo_content/tasks",
                      json={"capability": "research_topic", "params": {"query": "canary jane.doe@example.com"}})
    await research_worker(make_worker, settings)[0].run_until_idle()
    assert "canary" not in caplog.text and "Example result" not in caplog.text
    assert "canary" not in json.dumps(await dump("audit_log", "task_logs", "events"))
