"""JARVIS Brain 0.6.2: quotes of removed sources are never served, by any reader, even while no start-up check could
remove them (spec 11; findings 4-7; 19.9).

A history answer (supervisor.recall), a request summary that copies it and reference notes quote archived messages
and approved items. When the source is withheld, the start-up check removes the quotes (quotes.refresh_quotes), but
it can be skipped: while an import holds the import lock (0.6.2 then refreshes the quotes anyway, finding 4) or a
task holds the quote lock (it skips; the worker retries). Every reader therefore applies the same rule at read time
(app/api/work.py task_views, app/api/supervisor.py request_view: quotes.dead_sources, strip_dead, strip_summary).
A summary line 0.6.0's unlocked summary copied after the withhold had already removed it from the task is found by
its shape (QUOTE_LINE) and removed at start-up, by a rescan and at read time (finding 7); a digest line of another
agent's result that happens to say "(source: ...)" stays. Every role, the n8n service token and every agent path
are swept. Synthetic data only.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update

from app.agents.base import AgentContext
from app.bootstrap import bootstrap, check_withheld_copies
from app.brain import importer as imp
from app.brain import quotes, review, store
from app.brain.archive import recompute_session, scrub_before
from app.brain.notes import reference_notes
from app.brain.policy import POLICIES, PersonScope
from app.brain.quotes import REMOVED
from app.config import get_settings
from app.db import session_scope, sessionmaker
from app.llm.base import LLMError, LLMProvider
from app.models import ArchiveMessage, ArchiveSession, SupervisorRequest, Task
from app.supervisor import agent as sup_agent
from app.supervisor.agent import RequestParams, SupervisorAgent
from app.supervisor.recall import RecallParams, recall_history

from .. import brain_jarvis as bj
from .test_brain_agents import allow_ai_context
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture

pytestmark = pytest.mark.usefixtures("clean_db")

T0 = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
ROLES = ("viewer", "operator", "operator+", "admin", "admin+")


class CapturingModel(LLMProvider):
    """A fake AI model: planning fails (the rule-based planner plans), every text prompt is recorded."""
    name, available = "capture-model", True

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def json(self, *, purpose, system, prompt, schema, max_tokens=16000):
        self.prompts.append(system + "\n" + prompt)
        raise LLMError("planning by the model is off in this test")

    async def text(self, *, purpose, system, prompt, max_tokens=16000):
        self.prompts.append(system + "\n" + prompt)
        return "Draft written by the fake model."


def hits(blob: str, marks: tuple[str, ...]) -> list[str]:
    low = blob.lower()
    return sorted(m for m in marks if m.lower() in low)


async def plant(key: str, mark: str) -> dict[str, int]:
    """A general conversation: a request, an answer quoting `mark` (labelled an accomplishment, so recall quotes it)
    and a closing line."""
    async with session_scope() as s:
        sess = ArchiveSession(source_key="jarvis:session:%s" % key, source_session_id=key, title="Website check",
                              staff="jarvis", agent="supervisor", sensitivity="general", started_at=T0)
        s.add(sess)
        await s.flush()
        rows = [ArchiveMessage(session_id=sess.id, source_key="jarvis:message:%s:%d" % (key, p),
                               source_message_id=str(p), position=p, role=r, content=c,
                               at=T0 + timedelta(minutes=p), labels=lab)
                for p, r, c, lab in ((0, "user", "Please renew the certificate.", []),
                                     (1, "assistant", "The %s certificate was renewed and the site is up again."
                                      % mark, ["accomplishment"]),
                                     (2, "assistant", "Closing the ticket now.", []))]
        s.add_all(rows)
        await s.flush()
        await recompute_session(s, sess.id)
        return {"session": sess.id, "message": rows[1].id}


async def promote_approved(client, message_id: int, title: str) -> int:
    await as_(client, "operator")
    r = await client.post("/api/brain/messages/%d/promote" % message_id, json={"kind": "project", "title": title,
                                                                                 "agent": ""})
    assert r.status_code == 201, r.text
    item = r.json()["item"]["id"]
    await as_(client, "admin")
    assert (await client.post("/api/brain/items/%d/review" % item, json={"decision": "approve"})).status_code == 200
    return item


async def quote_everything(client, make_worker, model: LLMProvider | None = None) -> int:
    """A history request (recall answer, summary) and a blog draft (reference notes, a <memory> block for the
    model). Returns the request id."""
    async with session_scope() as s:
        allowed = await store.ai_context_allowed(s)
    if not allowed:
        await allow_ai_context()
    await as_(client, "operator")
    r = await client.post("/api/supervisor/requests", json={"message": "What did JARVIS accomplish?"})
    assert r.status_code == 202, r.text
    r2 = await client.post("/api/agents/seo_content/tasks",
                           json={"capability": "draft_blog_post", "params": {"topic": "certificate renewed plan"}})
    assert r2.status_code == 201, r2.text
    await make_worker(**({"llm": model} if model is not None else {})).run_until_idle()
    return r.json()["request"]["id"]


async def sweep(client, service_headers, marks: tuple[str, ...]) -> dict[str, list[str]]:
    """{who: [routes whose body holds a mark]} for every task and request route, as every role and the n8n service
    token. (An admin still lists a 0.6.0 copy that holds text on the Brain page until it is emptied, by design: it is
    the copy, not a quote, and test_brain_withhold_copies covers it.)"""
    async with session_scope() as s:
        tasks = list((await s.scalars(text("SELECT id FROM tasks ORDER BY id"))).all())
        requests = list((await s.scalars(text("SELECT id FROM supervisor_requests ORDER BY id"))).all())
    pages = ["/api/tasks", "/api/tasks?limit=200", "/api/supervisor/requests"]
    pages += ["/api/tasks/%d" % t for t in tasks] + ["/api/supervisor/requests/%d" % r for r in requests]
    found: dict[str, list[str]] = {}
    for who in ROLES:
        await as_(client, who)
        for page in pages:
            r = await client.get(page)
            if hits(r.text, marks):
                found.setdefault(who, []).append(page)
    client.cookies.clear()
    for page in pages:
        r = await client.get(page, headers=service_headers)
        if hits(r.text, marks):
            found.setdefault("service", []).append(page)
    return found


async def agent_paths(marks: tuple[str, ...], model: CapturingModel | None = None) -> dict[str, list[str]]:
    """Every agent path, directly: relevant, prompt_block (AI context on), history, reference notes, agents_view,
    the Supervisor's recall; and every prompt the model was sent."""
    out: dict[str, list[str]] = {}
    async with session_scope() as s:
        for agent in POLICIES:
            memory = store.AgentMemory(s, agent)
            for word in list(marks) + ["certificate renewed"]:
                blob = json.dumps([[i.title, i.body, i.tags] for i in await memory.relevant(word, limit=50)])
                blob += await memory.prompt_block(word, limit=20)
                found, _ = await memory.history(word, limit=50)
                blob += json.dumps([[h.message.content, h.excerpt] for h in found])
                blob += json.dumps(await reference_notes(memory, {"topic": word, "title": word}))
                if hits(blob, marks):
                    out.setdefault("agent:%s" % agent, []).extend(hits(blob, marks))
        for scope in (PersonScope("viewer", False), PersonScope("operator", True), PersonScope("admin", True)):
            if hits(json.dumps(await review.agents_view(s, scope)), marks):
                out["agents_view:%s" % scope.role] = list(marks)
        ctx = AgentContext(session=s, settings=get_settings(), http=None, llm=None, integrations=None,  # type: ignore[arg-type]
                           agent_name="supervisor", memory=store.AgentMemory(s, "supervisor"))
        for question in ["What did JARVIS accomplish?"] + ["What did JARVIS accomplish with %s?" % m for m in marks]:
            result = await recall_history(ctx, RecallParams(question=question, limit=30))
            if hits(json.dumps(result.model_dump(mode="json")), marks):
                out.setdefault("recall", []).extend(marks)
    for i, prompt in enumerate((model.prompts if model else [])):
        if hits(prompt, marks):
            out["model prompt %d" % i] = hits(prompt, marks)
    return out


async def as_060_import_restricted(session_id: int) -> None:
    """What 0.6.0's import did to a conversation that became restricted: rows withheld, rollback copies blanked, the
    session recomputed; no copy emptied and no quote removed (importer.py at 0.6.0, lines 1205-1211)."""
    async with session_scope() as s:
        await s.execute(update(ArchiveMessage).where(ArchiveMessage.session_id == session_id).values(
            content="", tool_calls=[], labels=[], withheld=True, sensitivity="restricted", truncated=False,
            flags={"withheld": "restricted data in this conversation"}))
        await scrub_before(s, archive_session_id=session_id)
        await recompute_session(s, session_id)


async def stored(mark: str) -> list[str]:
    """Where the mark is still stored outside the archive (task results and request summaries)."""
    return [c for c in await bj.nowhere(mark) if c.startswith(("tasks.", "supervisor_requests."))]


# --- The four readers' probes: 0.6.0 quotes while the start-up check is skipped --------------------------------------


@pytest.mark.parametrize("busy", ["import lock", "quote lock"])
@pytest.mark.parametrize("copies", [False, True], ids=["no copy", "approved copy"])
async def test_060_quotes_while_the_startup_check_is_skipped(client, make_worker, service_headers, busy, copies,
                                                             caplog):
    """Readers 1 (finding 6) and finding 4: a 0.6.0 database kept quotes of a conversation its import restricted; the
    start-up check runs while an import holds the import lock (0.6.2 refreshes the quotes anyway) or while a task
    holds the quote lock (skipped: nothing is removed). No reader, role, token or agent shows them either way, and
    status counts what is left."""
    caplog.set_level(logging.INFO)
    mark, title_mark = "zqrdmsg62", "zqrdtitle62"
    ids = await plant("rd1", mark)
    if copies:
        await promote_approved(client, ids["message"], "Copy %s" % title_mark)
    model = CapturingModel()
    request_id = await quote_everything(client, make_worker, model)
    marks = (mark, title_mark) if copies else (mark,)
    before = await sweep(client, service_headers, marks)
    assert before.get("operator") and before.get("admin"), "the answer quotes the marks before"
    await as_060_import_restricted(ids["session"])
    locks = await imp.LockConnection.open()
    try:
        if busy == "import lock":
            assert await locks.try_lock(imp.GLOBAL_LOCK)
        else:
            await locks.conn.execute("SELECT pg_advisory_lock_shared($1::int, -1)", imp.LOCK_KEY)
        complete = await check_withheld_copies()
        assert complete is False
        st = await imp.status()
        found = await sweep(client, service_headers, marks)
        agents = await agent_paths(marks)
        left = await stored(mark)
    finally:
        await locks.close()
    if busy == "import lock":
        assert "quotes refreshed" in caplog.text and left == []  # finding 4: quotes go even while an import runs
    else:
        assert "withheld-copies check skipped" in caplog.text
        assert left and st["quotes_of_removed_sources"] > 0  # still stored, and counted
    assert found == {}, found  # the read-time guard: nobody is served them
    assert agents == {}, agents
    # The next start that runs removes what is left; the request's own page agrees.
    assert await check_withheld_copies() is True
    assert await stored(mark) == [] and (await imp.status())["quotes_of_removed_sources"] == 0
    await as_(client, "operator")
    summary = (await client.get("/api/supervisor/requests/%d" % request_id)).json()["request"]["summary"]
    assert REMOVED in summary and mark not in summary


@pytest.mark.parametrize("holder", ["importer container", "running recall"])
async def test_060_quotes_when_both_starts_are_skipped_then_worker_ticks(client, make_worker, service_headers, holder,
                                                                         caplog):
    """The skeptic's variant: the api's and the worker's full start-ups both run while another process holds a lock;
    the worker then ticks. Operators never read the quote; status said so."""
    caplog.set_level(logging.INFO)
    mark = "zqrdboth62"
    ids = await plant("rd2", mark)
    await quote_everything(client, make_worker)
    await as_060_import_restricted(ids["session"])
    locks = await imp.LockConnection.open()
    try:
        if holder == "importer container":
            assert await locks.try_lock(imp.GLOBAL_LOCK)
        else:
            await locks.conn.execute("SELECT pg_advisory_lock_shared($1::int, -1)", imp.LOCK_KEY)
        for _ in range(2):  # api, worker
            assert (await bootstrap(get_settings()))["withheld_check_complete"] is False
        st = await imp.status()
        seen = await sweep(client, service_headers, (mark,))
    finally:
        await locks.close()
    assert seen == {}, seen
    if holder == "running recall":
        assert st["quotes_of_removed_sources"] > 0
    # The worker whose start-up check was skipped runs it again from its tick once nothing holds a lock.
    await make_worker(withheld_check_done=False).run_until_idle()
    assert await stored(mark) == []


# --- Readers 2: a summary line whose task line was already removed (finding 7) -------------------------------------


async def test_summary_line_already_removed_from_its_task_goes_at_startup_rescan_and_read_time(client, make_worker,
                                                                                               service_headers):
    """What 0.6.0's race left (hand-made): the recall task's line REMOVED (source None), the request's summary still
    quoting it. Read time, the start-up check and a rescan each remove it; status counts it before."""
    mark = "zqrdorphan62"
    ids = await plant("rd3", mark)
    request_id = await quote_everything(client, make_worker)
    async with session_scope() as s:
        recall = await s.scalar(select(Task).where(Task.capability == "recall"))
        data = dict(recall.result["data"])
        quoted = [line for line, src in zip(data["answer"], data["sources"]) if src == {"message": ids["message"]}]
        request = await s.get(SupervisorRequest, request_id)
        assert quoted and mark in quoted[0] and quoted[0] in request.summary
        data["answer"] = [REMOVED if src == {"message": ids["message"]} else line
                          for line, src in zip(data["answer"], data["sources"])]
        data["sources"] = [None if src == {"message": ids["message"]} else src for src in data["sources"]]
        recall.result = {**recall.result, "data": data}
        await s.execute(update(ArchiveMessage).where(ArchiveMessage.id == ids["message"]).values(
            content="", tool_calls=[], labels=[], withheld=True))
    assert (await imp.status())["quotes_of_removed_sources"] == 1
    assert await sweep(client, service_headers, (mark,)) == {}  # read time
    assert await stored(mark) == ["supervisor_requests.summary"]
    out = await imp.rescan(apply=True, actor="admin@test.local")  # a rescan finds it too
    assert out["answers_changed"] >= 1 and await stored(mark) == []
    assert (await imp.status())["quotes_of_removed_sources"] == 0


async def test_summary_race_replayed_is_clean_after_the_startup_check_and_rescan(client, make_worker,
                                                                               service_headers, monkeypatch):
    """The real 0.6.0 interleaving with 0.6.2's own code: summarize_request without the quote lock (as in 0.6.0)
    reads the recall answer; an admin withholds the message and commits (the task's line becomes REMOVED, the summary
    is still empty); then the summary commits with the quote."""
    mark = "zqrdrace62"
    ids = await plant("rd4", mark)
    request_id = await quote_everything(client, make_worker)
    async with session_scope() as s:  # back to 'before summarize'
        req = await s.get(SupervisorRequest, request_id)
        req.summary, req.status, req.completed_at = "", "running", None
        req.plan = {k: v for k, v in (req.plan or {}).items() if k != "local_only"}

    async def no_lock(*a, **k):
        return True

    monkeypatch.setattr(sup_agent, "lock_quotes", no_lock)
    async with sessionmaker()() as a:
        ctx = AgentContext(session=a, settings=get_settings(), http=None, llm=None, integrations=None,  # type: ignore[arg-type]
                           agent_name="supervisor")
        await SupervisorAgent().summarize_request(ctx, RequestParams(request_id=request_id))
        await a.flush()
        await as_(client, "admin")
        r = await client.post("/api/brain/messages/%d/withhold" % ids["message"])
        assert r.status_code == 200, r.text
        await a.commit()
    monkeypatch.undo()
    assert await stored(mark) == ["supervisor_requests.summary"]  # the race left it
    assert await sweep(client, service_headers, (mark,)) == {}  # never served
    assert await check_withheld_copies() is True
    assert await stored(mark) == []
    await imp.rescan(apply=True, actor="admin@test.local")
    assert await stored(mark) == [] and await sweep(client, service_headers, (mark,)) == {}


async def test_other_agents_digest_lines_with_source_survive(client, make_worker):
    """Only recall's two quoting shapes are quotes: a digest line of another agent's result that says "(source: ...)",
    and a quoting line its task still holds, stay in the summary at start-up and at read time."""
    mark = "zqrddigest62"
    ids = await plant("rd5", mark)
    request_id = await quote_everything(client, make_worker)
    digest = "- seo_content.draft_blog_post: Draft written (source: the site's blog feed)"
    other = "- weekly-report: 3 checks passed (source: monitoring · uptime)"
    async with session_scope() as s:
        req = await s.get(SupervisorRequest, request_id)
        held = [line for line in req.summary.split("\n") if quotes.QUOTE_LINE.search(line)]
        assert held, req.summary
        req.summary = "\n".join([digest, other, req.summary])
    await check_withheld_copies()
    async with session_scope() as s:
        summary = (await s.get(SupervisorRequest, request_id)).summary
    assert digest in summary and other in summary and all(line in summary for line in held)
    await as_(client, "operator")
    shown = (await client.get("/api/supervisor/requests/%d" % request_id)).json()["request"]["summary"]
    assert digest in shown and other in shown and all(line in shown for line in held)
    # Once the quoted message is withheld, only its line goes.
    await as_(client, "admin")
    assert (await client.post("/api/brain/messages/%d/withhold" % ids["message"])).status_code == 200
    await as_(client, "operator")
    shown = (await client.get("/api/supervisor/requests/%d" % request_id)).json()["request"]["summary"]
    assert digest in shown and other in shown and mark not in shown and REMOVED in shown


async def test_read_time_guard_hides_notes_and_lines_of_an_item_promoted_from_a_withheld_message(
        client, make_worker, service_headers):
    """Reference notes and answer lines naming a promoted item whose message is withheld, before the item is emptied
    (a 0.6.0 leftover): withheld_source() makes the item dead at read time."""
    mark, title_mark = "zqrditem62", "zqrditemtitle62"
    ids = await plant("rd6", mark)
    copy = await promote_approved(client, ids["message"], "Copy %s" % title_mark)
    await quote_everything(client, make_worker)
    async with session_scope() as s:  # 0.6.0: the message withheld, its copy kept
        await s.execute(update(ArchiveMessage).where(ArchiveMessage.id == ids["message"]).values(
            content="", tool_calls=[], labels=[], withheld=True))
        assert await s.scalar(select(Task.id).where(text("result::text LIKE :m")), {"m": "%%%s%%" % title_mark})
    found = await sweep(client, service_headers, (mark, title_mark))
    assert found == {}, found
    assert await agent_paths((mark, title_mark)) == {}
    assert copy
