"""The JARVIS Brain and the agents (spec section 4): ctx.memory, reference notes, memory in drafting prompts,
supervisor.recall, local-only summaries and what viewers see."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.agents.base import AgentContext
from app.agents.registry import all_agents
from app.brain import store
from app.brain.notes import host_of, reference_notes
from app.config import get_settings
from app.db import session_scope
from app.models import AppSetting, ArchiveMessage, ArchiveSession, Artifact, MemoryItem, SupervisorRequest, Task, User
from app.security import hash_password
from app.supervisor.planner import RulePlanner
from app.supervisor.recall import (
    FALLBACK_NOTE,
    MAX_LINES,
    NO_TOPIC_NOTE,
    NOTHING_IMPORTED,
    RecallParams,
    find_period,
    recall_history,
)

from ..conftest import ADMIN
from ..fakes import FakeLLM, static_site

pytestmark = pytest.mark.usefixtures("clean_db")

T0 = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
# Words planted in rows that recall must never quote (customer, restricted, Credit Repair, unreviewed).
NEVER = ("WALRUS", "OTTER", "KESTREL", "PELICAN", "ROBIN", "HERON", "jane@", "[email]", "[phone]")
SSL_QUESTION = "what problems did JARVIS have with SSL"


# --- Seeding (rows inserted directly, as the importer would leave them) --------------------------------------------


def _session(key: str, *, title: str = "", staff: str = "jarvis", agent: str = "supervisor", sensitivity: str = "general",
             started: datetime | None = None) -> ArchiveSession:
    return ArchiveSession(source_key=f"jarvis:session:{key}", source_session_id=key, title=title, staff=staff, agent=agent,
                          sensitivity=sensitivity, started_at=started or T0)


def _message(sess: ArchiveSession, position: int, content: str, labels: list[str], *, role: str = "assistant",
             at: datetime | None = None, sensitivity: str = "general", withheld: bool = False) -> ArchiveMessage:
    return ArchiveMessage(session_id=sess.id, source_key=f"jarvis:message:{sess.source_session_id}:{position}",
                          source_message_id=str(position), position=position, role=role, content=content,
                          labels=labels, at=at or sess.started_at, sensitivity=sensitivity, withheld=withheld)


async def _item(s, kind: str, title: str, body: str, *, agent: str = "", approve: bool = True) -> MemoryItem:
    item = await store.create_item(s, kind=kind, title=title, body=body, agent=agent, actor="admin@test.local")
    if approve:
        await store.review_item(s, item, decision="approve", actor="admin@test.local")
    return item


async def seed_history() -> dict[str, int]:
    """Two general sessions (Jarvis's site work, Scout's SEO sweep), plus rows recall must never read."""
    async with session_scope() as s:
        site = _session("s1", title="Website check for acme", started=T0)
        seo = _session("s2", staff="scout", agent="seo_content", started=T0 + timedelta(days=3))
        customer = _session("s3", title="Refund for a client", staff="sarah", agent="communications", sensitivity="customer")
        restricted = _session("s4", sensitivity="restricted")
        credit = _session("s5", title="Credit portal", agent="credit_repair")
        s.add_all([site, seo, customer, restricted, credit])
        await s.flush()
        s.add_all([
            _message(site, 3, "The SSL certificate on acme.example.test expired; I renewed it with AutoSSL and the site is up "
                              "again.", ["problem", "accomplishment"], at=T0 + timedelta(hours=1)),
            _message(site, 5, "From now on, never refund a client without asking me first.", ["decision"], role="user",
                     at=T0 + timedelta(hours=2)),
            _message(site, 7, "Called [phone] about the SSL renewal OTTER", ["problem"], sensitivity="customer",
                     at=T0 + timedelta(hours=3)),
            _message(seo, 1, "I published the keyword brief for home care websites.", ["accomplishment"],
                     at=T0 + timedelta(days=3)),
            _message(seo, 2, "Error 500 from the keyword tool; the sweep stopped.", ["problem"], role="tool",
                     at=T0 + timedelta(days=3, hours=1)),
            _message(customer, 0, "SSL issue for the client at [email]; refund approved WALRUS", ["problem", "decision"],
                     sensitivity="customer"),
            _message(restricted, 0, "", [], withheld=True, sensitivity="restricted"),
            _message(credit, 0, "SSL problem in the credit portal HERON", ["problem"]),
        ])
        await s.flush()
        problem = await _item(s, "problem", "AutoSSL failed on acme", "SSL renewal failed twice on acme.example.test.")
        refund = await _item(s, "business", "Refund policy", "Refunds need the owner's approval; none after 30 days.")
        await _item(s, "knowledge", "SSL note for a client", "Client jane@example.org asked about SSL KESTREL")
        await _item(s, "problem", "SSL draft note PELICAN", "Unreviewed SSL note.", approve=False)
        await _item(s, "problem", "SSL for the credit portal ROBIN", "SSL ROBIN.", agent="credit_repair")
        return {"problem_item": problem.id, "refund_item": refund.id}


async def ask(question: str, **kw):
    """supervisor.recall, called as the worker would (the Supervisor's memory view)."""
    async with session_scope() as s:
        ctx = AgentContext(session=s, settings=get_settings(), http=None, llm=None, integrations=None,  # type: ignore[arg-type]
                           agent_name="supervisor", memory=store.AgentMemory(s, "supervisor"))
        return await recall_history(ctx, RecallParams(question=question, **kw))


async def run_task(client, make_worker, agent, capability, params, **worker_kw):
    r = await client.post(f"/api/agents/{agent}/tasks", json={"capability": capability, "params": params})
    assert r.status_code == 201, r.text
    await make_worker(**worker_kw).run_until_idle()
    return (await client.get(f"/api/tasks/{r.json()['task']['id']}")).json()


async def ask_supervisor(client, make_worker, message, **worker_kw):
    r = await client.post("/api/supervisor/requests", json={"message": message})
    assert r.status_code == 202, r.text
    await make_worker(**worker_kw).run_until_idle()
    return (await client.get(f"/api/supervisor/requests/{r.json()['request']['id']}")).json()


async def sign_in_as(client, email: str, role: str) -> None:
    async with session_scope() as s:
        if await s.scalar(select(User).where(User.email == email)) is None:
            s.add(User(email=email, password_hash=hash_password(f"{role}-password-123"), role=role))
    await client.post("/api/auth/logout")
    r = await client.post("/api/auth/login", json={"email": email, "password": f"{role}-password-123"})
    assert r.status_code == 200, r.text


async def allow_ai_context() -> None:
    async with session_scope() as s:
        s.add(AppSetting(key=store.AI_CONTEXT_KEY, value="approved_general"))


def spy_prompt_block(monkeypatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    original = store.AgentMemory.prompt_block

    async def spy(self, query, **kw):
        calls.append((self.agent, query))
        return await original(self, query, **kw)

    monkeypatch.setattr(store.AgentMemory, "prompt_block", spy)
    return calls


# --- Routing and registry ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("question", ["What did JARVIS accomplish last month?", "what problems did JARVIS have with SSL",
                                      "what decisions were made about refunds"])
def test_the_rule_planner_routes_history_questions_to_recall(question):
    agents = {n: a for n, a in all_agents().items() if not a.sensitive}
    plan = RulePlanner().plan(question, agents)
    assert [(s.agent, s.capability, s.params) for s in plan.steps] == [
        ("supervisor", "recall", {"question": question, "limit": 8})]


def test_the_registry_is_unchanged_but_for_the_recall_capability():
    from app.api.stats import CATEGORY_OF

    counts = {name: len(agent.capabilities()) for name, agent in all_agents().items()}
    assert counts == {"supervisor": 5, "wordpress_security": 13, "seo_content": 4, "social_marketing": 4,
                      "server_management": 5, "communications": 8, "monitoring": 6, "credit_repair": 3}
    supervisor = all_agents()["supervisor"].capabilities()
    assert list(supervisor) == ["plan_request", "summarize_request", "daily_briefing", "health_review", "recall"]
    assert supervisor["recall"].risk.value == "read" and not supervisor["recall"].risk.requires_approval
    assert CATEGORY_OF[("supervisor", "recall")] == "reports"
    assert "credit_repair" not in supervisor["recall"].description.lower().replace(" ", "_")


def test_periods_in_a_question():
    today = date(2026, 10, 19)  # a Monday
    cases = {
        "What did JARVIS accomplish last month?": (date(2026, 9, 1), date(2026, 9, 30)),
        "problems this month": (date(2026, 10, 1), today),
        "anything in the past 30 days": (date(2026, 9, 20), today),
        "decisions last week": (date(2026, 10, 12), date(2026, 10, 18)),
        "what happened yesterday": (date(2026, 10, 18), date(2026, 10, 18)),
        "what did Scout do in March": (date(2026, 3, 1), date(2026, 3, 31)),
        "what did Scout do in December": (date(2025, 12, 1), date(2025, 12, 31)),
        "problems in February 2024": (date(2024, 2, 1), date(2024, 2, 29)),
        "decisions in 2025": (date(2025, 1, 1), date(2025, 12, 31)),
        "problems last year": (date(2025, 1, 1), date(2025, 12, 31)),
    }
    for question, (start, end) in cases.items():
        period = find_period(question, today)
        assert period is not None and (period.start, period.end) == (start, end), question
    assert find_period("what problems did JARVIS have in the past with SSL", today) is None
    assert find_period("what problems may have happened", today) is None


def test_hosts_for_reference_notes():
    assert host_of("https://www.Acme.example.test:8443/x") == "acme.example.test"
    assert host_of("acme.example.test") == "acme.example.test"
    assert host_of("localhost") == "" and host_of("") == "" and host_of("not a url at all") == ""


# --- supervisor.recall ------------------------------------------------------------------------------------------------


async def test_recall_answers_from_the_archive_and_approved_items_with_sources_and_coverage():
    ids = await seed_history()
    result = await ask(SSL_QUESTION)
    answer = result.data["answer"]
    text = "\n".join(answer)
    assert result.data["local_only"] is True and len(answer) <= MAX_LINES
    assert answer[0] == "### Problems (2 found)"  # the question asks about problems: that section comes first
    assert any("approved memory (problem)" in line and f"item #{ids['problem_item']}" in line for line in answer)
    assert any(line.startswith("- 2026-03-02 · jarvis · \"The SSL certificate on acme.example.test expired")
               and line.endswith("(source: Website check for acme · message 3)") for line in answer)
    assert "### Accomplishments" in text and "### Decisions (0 found)" in text and "- Nothing found." in text
    assert "Searched 2 imported JARVIS session(s) from 2026-03-02 to 2026-03-05" in text
    assert "matched by keywords" in text and "inferred" in text
    for word in NEVER:
        assert word not in text, word
    assert result.data["counts"] == {"accomplishment": 1, "problem": 1, "decision": 0, "items": 1, "shown": 2,
                                     "sessions_searched": 2}
    assert result.data["coverage"]["sessions"] == 2 and result.data["coverage"]["matched_topic_words"] is True
    # The summary holds counts only: no quoted text.
    assert result.summary.startswith("JARVIS history: 1 accomplishment, 1 problem and 0 decision message(s)")
    assert "SSL" not in result.summary and "acme" not in result.summary
    assert result.artifacts == [] and result.events == []

    refunds = await ask("what decisions were made about refunds")
    lines = refunds.data["answer"]
    assert lines[0] == "### Decisions (2 found)"
    assert any("never refund a client without asking me first" in line and "message 5" in line for line in lines)
    assert any("approved memory (business)" in line and "Refund policy" in line for line in lines)
    for word in NEVER:
        assert word not in "\n".join(lines), word


async def test_recall_never_reads_customer_restricted_or_credit_repair_rows():
    await seed_history()
    for question in ("SSL", "refund", "What did JARVIS do previously?", "what problems did JARVIS have with the client portal"):
        result = await ask(question, limit=30)
        text = "\n".join(result.data["answer"]) + result.summary + json.dumps(result.data)
        for word in NEVER:
            assert word not in text, (question, word)
        assert result.data["coverage"]["sessions"] == 2  # customer, restricted and Credit Repair sessions are not searched


async def test_recall_falls_back_to_the_newest_when_the_topic_words_match_nothing():
    await seed_history()
    result = await ask("what problems did JARVIS have with kubernetes")
    answer = result.data["answer"]
    assert answer[0] == FALLBACK_NOTE and answer[1].startswith("### Problems")
    assert "Error 500 from the keyword tool" in answer[2]  # the newest problem first
    assert result.data["coverage"]["newest_shown"] is True and result.data["coverage"]["matched_topic_words"] is False
    plain = await ask("What did JARVIS accomplish?")
    assert plain.data["answer"][0] == NO_TOPIC_NOTE and plain.data["answer"][1].startswith("### Accomplishments")
    assert "published the keyword brief" in plain.data["answer"][2]


async def test_recall_limits_to_the_period_asked_for():
    now = datetime.now(UTC)
    first_of_month = now.replace(day=1, hour=12, minute=0, second=0, microsecond=0)
    in_last_month = first_of_month - timedelta(days=10)
    long_ago = first_of_month - timedelta(days=120)
    async with session_scope() as s:
        sess = _session("p1", title="Monthly work", started=long_ago)
        s.add(sess)
        await s.flush()
        s.add_all([_message(sess, 0, "I published the October landing page.", ["accomplishment"], at=in_last_month),
                   _message(sess, 1, "I deployed the old theme fix.", ["accomplishment"], at=long_ago)])
    result = await ask("What did JARVIS accomplish last month?")
    text = "\n".join(result.data["answer"])
    assert "October landing page" in text and "old theme fix" not in text
    assert "Period asked:" in text and result.data["coverage"]["period"]["timezone"] == "America/New_York"


async def test_recall_says_when_nothing_has_been_imported():
    result = await ask(SSL_QUESTION)
    assert result.data["answer"] == [NOTHING_IMPORTED]
    assert result.summary == "JARVIS history: nothing imported yet (0 sessions, 0 approved items)."


async def test_history_answers_never_reach_an_ai_model(admin, make_worker):
    await seed_history()
    llm = FakeLLM(json_responses=[{"reply": "Looking at JARVIS's history.", "steps": [
        {"action": "supervisor.recall", "params_json": json.dumps({"question": SSL_QUESTION}), "rationale": "history"},
        {"action": "supervisor.health_review", "params_json": "{}", "rationale": "and the platform"}]}],
        text_response="MODEL SUMMARY")
    detail = await ask_supervisor(admin, make_worker, SSL_QUESTION, llm=llm)
    req = detail["request"]
    assert req["status"] == "completed" and req["summarizer"] == "template" and req["plan"]["local_only"] is True
    assert "MODEL SUMMARY" not in req["summary"]
    assert "### Problems (2 found)" in req["summary"] and "Website check for acme · message 3" in req["summary"]
    assert [c["purpose"] for c in llm.calls] == ["plan"]  # the plan only: no summary call
    assert all("expired" not in c["prompt"] and "AutoSSL" not in c["prompt"] for c in llm.calls)
    summarize = next(t for t in detail["tasks"] if t["capability"] == "summarize_request")
    digests = json.dumps(summarize["result"]["data"]["digests"])
    assert '"answer"' not in digests and "expired" not in digests  # _task_digest never carries the answer
    recall = next(t for t in detail["tasks"] if t["capability"] == "recall")
    assert recall["status"] == "succeeded" and recall["result"]["data"]["local_only"] is True
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(Artifact).where(Artifact.task_id == recall["id"])) == 0


async def test_requests_without_history_answers_are_summarised_as_before(admin, make_worker):
    llm = FakeLLM(json_responses=[{"reply": "Checking.", "steps": [
        {"action": "supervisor.health_review", "params_json": "{}", "rationale": "asked"}]}], text_response="MODEL SUMMARY")
    req = (await ask_supervisor(admin, make_worker, "Is the system healthy?", llm=llm))["request"]
    assert req["summary"] == "MODEL SUMMARY" and "local_only" not in req["plan"]


# --- Memory in drafting prompts ------------------------------------------------------------------------------------


async def test_drafting_uses_memory_only_when_the_owner_allows_it(admin, make_worker, monkeypatch):
    calls = spy_prompt_block(monkeypatch)
    async with session_scope() as s:
        await _item(s, "knowledge", "Page speed checklist", "Compress images and cache pages for page speed.")
        await _item(s, "knowledge", "Page speed complaint", "jane@example.org says the page speed is bad.")
        await _item(s, "knowledge", "Page speed draft idea", "Not reviewed yet.", approve=False)
    topic = {"topic": "page speed for small businesses"}
    llm = FakeLLM(text_response="# Page speed\n\nDraft.")
    off = await run_task(admin, make_worker, "seo_content", "draft_blog_post", topic, llm=llm)
    assert off["task"]["status"] == "succeeded"
    assert "<memory>" not in llm.calls[-1]["prompt"] and store.MEMORY_NOTE not in llm.calls[-1]["system"]
    assert not any("memory item" in entry["message"] for entry in off["logs"])

    await allow_ai_context()
    on = await run_task(admin, make_worker, "seo_content", "draft_blog_post", topic, llm=llm)
    prompt, system = llm.calls[-1]["prompt"], llm.calls[-1]["system"]
    assert prompt.startswith("<memory>") and "Page speed checklist" in prompt and prompt.endswith(
        "Topic: page speed for small businesses\nAudience: small business owners\nLength: about 800 words")
    assert "complaint" not in prompt and "[email]" not in prompt and "draft idea" not in prompt
    assert system.endswith(store.MEMORY_NOTE)
    assert any(entry["message"] == "used 1 memory item(s) in the prompt" for entry in on["logs"])
    assert calls[-1] == ("seo_content", "page speed for small businesses")

    # No model: nothing is looked up and the template is used.
    before = len(calls)
    plain = await run_task(admin, make_worker, "seo_content", "draft_blog_post", topic)
    assert "(template)" in plain["task"]["summary"] and len(calls) == before


async def test_social_posts_and_keyword_briefs_query_memory_with_the_topic(admin, make_worker, monkeypatch):
    calls = spy_prompt_block(monkeypatch)
    await allow_ai_context()
    llm = FakeLLM(text_response="## facebook #1\nDraft.")
    await run_task(admin, make_worker, "social_marketing", "draft_social_posts", {"topic": "spring website refresh"}, llm=llm)
    brief = {"topic": "home care websites", "location": "Atlanta"}
    await run_task(admin, make_worker, "seo_content", "keyword_brief", brief, llm=llm)
    assert calls == [("social_marketing", "spring website refresh"), ("seo_content", "home care websites")]


async def test_support_replies_query_memory_with_the_category_only(admin, make_worker, monkeypatch):
    calls = spy_prompt_block(monkeypatch)
    async with session_scope() as s:
        await _item(s, "business", "Billing replies", "For billing questions, the accounts team replies within a day.")
        await _item(s, "knowledge", "Zanzibar orders", "ZANZIBAR orders ship from the second warehouse.")
    await allow_ai_context()
    r = await admin.post("/api/support/tickets", json={
        "customer_name": "Dana Client", "customer_email": "dana@client.example", "subject": "Invoice question",
        "body": "Please check invoice ZANZIBAR-77, I was charged twice."})
    assert r.status_code == 201, r.text
    llm = FakeLLM(text_response="Hi Dana,\n\nWe are checking.\n\nInfinite Marketing Support")
    await make_worker(llm=llm).run_until_idle()
    assert calls == [("communications", "billing")]
    prompt = llm.calls[-1]["prompt"]
    assert prompt.startswith("<memory>") and "Billing replies" in prompt and "warehouse" not in prompt


# --- Reference notes -------------------------------------------------------------------------------------------------


async def test_reference_notes_follow_each_agents_policy(admin, make_worker, web):
    web.add("acme.example.test", static_site("<html><head><title>Acme</title></head><body>Hello</body></html>"))
    site = (await admin.post("/api/sites", json={"name": "Acme", "url": "https://acme.example.test"})).json()["site"]
    await make_worker().run_until_idle()  # the first check of a new site, so both reports below see the same checks
    before = await run_task(admin, make_worker, "monitoring", "uptime_report", {"site_id": site["id"]})
    assert "memory_notes" not in before["task"]["result"]["data"]  # nothing in memory yet: nothing added

    async with session_scope() as s:
        shared = await _item(s, "knowledge", "Hosting for acme.example.test", "acme.example.test runs on the CWP server.")
        own = await _item(s, "agent_note", "Monitoring acme.example.test", "acme.example.test is slow on Mondays.",
                          agent="monitoring")
        seo = await _item(s, "agent_note", "SEO for acme.example.test", "Target local keywords on acme.example.test.",
                          agent="seo_content")
        await _item(s, "knowledge", "Contact for acme.example.test", "Owner jane@example.org manages acme.example.test.")
        await _item(s, "knowledge", "Draft about acme.example.test", "Unreviewed.", approve=False)
        await _item(s, "knowledge", "Credit note acme.example.test", "acme.example.test", agent="credit_repair")

    after = await run_task(admin, make_worker, "monitoring", "uptime_report", {"site_id": site["id"]})
    notes = after["task"]["result"]["data"]["memory_notes"]
    assert {n["id"] for n in notes} == {shared.id, own.id}
    assert all(set(n) == {"id", "kind", "title"} for n in notes)
    # The capability's own output is unchanged.
    assert after["task"]["summary"] == before["task"]["summary"]
    assert {k: v for k, v in after["task"]["result"]["data"].items() if k != "memory_notes"} == before["task"]["result"]["data"]

    url_check = await run_task(admin, make_worker, "monitoring", "check_url", {"url": "https://www.acme.example.test"})
    assert {n["id"] for n in url_check["task"]["result"]["data"]["memory_notes"]} == {shared.id, own.id}
    blog = await run_task(admin, make_worker, "seo_content", "draft_blog_post", {"topic": "acme.example.test redesign"})
    assert {n["id"] for n in blog["task"]["result"]["data"]["memory_notes"]} == {shared.id, seo.id}
    other = await run_task(admin, make_worker, "monitoring", "check_url", {"url": "https://other.example.test"})
    assert "memory_notes" not in other["task"]["result"]["data"]

    async with session_scope() as s:
        credit = store.AgentMemory(s, "credit_repair")
        assert await reference_notes(credit, {"topic": "acme.example.test"}) == []
        supervisor = await reference_notes(store.AgentMemory(s, "supervisor"), {"url": "acme.example.test"})
        assert {n["id"] for n in supervisor} == {shared.id, own.id, seo.id}  # never customer, pending or Credit Repair


async def test_customer_text_never_selects_reference_notes(admin, make_worker):
    async with session_scope() as s:
        await _item(s, "knowledge", "Zanzibar orders", "ZANZIBAR orders ship from the second warehouse.")
    r = await admin.post("/api/support/tickets", json={"customer_name": "Dana", "customer_email": "dana@client.example",
                                                       "subject": "Zanzibar", "body": "Where is my Zanzibar order?"})
    assert r.status_code == 201
    await make_worker().run_until_idle()
    async with session_scope() as s:
        tasks = (await s.scalars(select(Task).where(Task.agent == "communications"))).all()
    assert tasks and all(t.status == "succeeded" and "memory_notes" not in t.result["data"] for t in tasks)


async def test_a_failing_reference_note_lookup_never_fails_the_task(admin, make_worker, monkeypatch):
    from sqlalchemy import text

    from app.worker import runner

    async def broken(memory, params, **kw):
        await memory.session.execute(text("SELECT 1 / 0"))  # a database error inside the SAVEPOINT

    monkeypatch.setattr(runner, "reference_notes", broken)
    done = await run_task(admin, make_worker, "seo_content", "draft_blog_post", {"topic": "page speed"})
    assert done["task"]["status"] == "succeeded" and "memory_notes" not in done["task"]["result"]["data"]
    async with session_scope() as s:  # the capability's own output was still saved
        assert await s.scalar(select(func.count()).select_from(Artifact).where(Artifact.task_id == done["task"]["id"])) == 1


async def test_agents_without_a_policy_get_no_memory(admin, make_worker, monkeypatch):
    from app.agents import registry
    from app.agents.base import AgentResult, BaseAgent, Params, Risk, capability

    seen: list[object] = []

    class ProbeAgent(BaseAgent):
        name = "memory_probe"
        title = "Probe"
        description = "test agent"

        @capability(risk=Risk.READ, description="records ctx.memory", params=Params)
        async def probe(self, ctx, params):
            seen.append(ctx.memory)
            return AgentResult(summary="probed")

    agents = dict(registry.all_agents())
    agents["memory_probe"] = ProbeAgent()
    monkeypatch.setattr(registry, "_AGENTS", agents)
    done = await run_task(admin, make_worker, "memory_probe", "probe", {})
    assert done["task"]["status"] == "succeeded" and seen == [None]


# --- What viewers see --------------------------------------------------------------------------------------------------


async def test_viewers_get_neither_history_answers_nor_reference_notes(admin, app_client, make_worker, web):
    await seed_history()
    site = (await admin.post("/api/sites", json={"name": "Acme", "url": "https://acme.example.test"})).json()["site"]
    async with session_scope() as s:
        await _item(s, "knowledge", "Hosting for acme.example.test", "acme.example.test runs on the CWP server.")
    checked = await run_task(admin, make_worker, "monitoring", "uptime_report", {"site_id": site["id"]})
    assert checked["task"]["result"]["data"]["memory_notes"]
    detail = await ask_supervisor(admin, make_worker, SSL_QUESTION)
    req_id = detail["request"]["id"]
    recall_id = next(t["id"] for t in detail["tasks"] if t["capability"] == "recall")
    assert "### Problems" in detail["request"]["summary"]

    for role in ("viewer", "operator", "admin"):
        if role == "admin":
            await app_client.post("/api/auth/logout")
            await app_client.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
        else:
            await sign_in_as(app_client, f"{role}@test.local", role)
        sees = role != "viewer"
        tasks = {t["id"]: t for t in (await app_client.get("/api/tasks")).json()["tasks"]}
        one = (await app_client.get(f"/api/tasks/{recall_id}")).json()["task"]
        note_task = (await app_client.get(f"/api/tasks/{checked['task']['id']}")).json()["task"]
        listed = next(r for r in (await app_client.get("/api/supervisor/requests")).json()["requests"] if r["id"] == req_id)
        got = (await app_client.get(f"/api/supervisor/requests/{req_id}")).json()
        for data in (tasks[recall_id]["result"]["data"], one["result"]["data"],
                     next(t for t in got["tasks"] if t["id"] == recall_id)["result"]["data"]):
            assert ("answer" in data) is sees and data["local_only"] is True and "counts" in data
        for data in (tasks[checked["task"]["id"]]["result"]["data"], note_task["result"]["data"]):
            assert ("memory_notes" in data) is sees and "sites" in data
        for summary in (listed["summary"], got["request"]["summary"]):
            if sees:
                assert "### Problems" in summary
            else:
                assert summary == "Answer about JARVIS's history (visible to operators and admins)"
        if not sees:
            text = json.dumps([tasks, one, listed, got])
            assert "renewed it with AutoSSL" not in text and "Hosting for acme" not in text


async def test_a_viewer_sees_ordinary_request_summaries(admin, app_client, make_worker):
    detail = await ask_supervisor(admin, make_worker, "Is the system healthy?")
    await sign_in_as(app_client, "viewer@test.local", "viewer")
    got = (await app_client.get(f"/api/supervisor/requests/{detail['request']['id']}")).json()
    assert got["request"]["summary"] == detail["request"]["summary"] and "visible to operators" not in got["request"]["summary"]
    async with session_scope() as s:
        assert (await s.get(SupervisorRequest, detail["request"]["id"])).plan.get("local_only") is None
