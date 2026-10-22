"""JARVIS Brain 0.6.1: withholding a message that has promoted copies, and purging them.

0.6.0 could not withhold a message once an item had been promoted from it (the version it wrote broke the CHECK on
memory_item_versions.change, and the whole withhold rolled back), and its import, rescan and rollback withholds never
reached the copies. Every rule here is checked with synthetic data: unique markers are planted in a message's text
and in its copy's title, tags, review note and edits, and nowhere() searches every text, JSONB, array and search
column of every table, and the log, for them. No real JARVIS data is used.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, event, func, or_, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from app import bootstrap as startup
from app import tasks as task_service
from app.bootstrap import check_withheld_copies
from app.brain import archive, policy, review, store
from app.brain import quotes as quotes_module
from app.brain import importer as imp
from app.brain.archive import WITHHELD_BY_ADMIN, recompute_session
from app.brain.notes import reference_notes
from app.brain.policy import PersonScope
from app.brain.quotes import REMOVED, lock_quotes
from app.db import session_scope, sessionmaker
from app.llm.base import LLMProvider, LLMUnavailable
from app.models import (
    ArchiveMessage,
    ArchiveSession,
    AuditLog,
    BrainTombstone,
    ImportRecord,
    ImportRun,
    MemoryConflict,
    MemoryItem,
    MemoryItemVersion,
    SupervisorRequest,
    Task,
    TaskLog,
    User,
)
from app.worker.runner import Worker

from .. import brain_jarvis as bj
from ..conftest import ADMIN
from .test_brain_agents import allow_ai_context, ask
from .test_brain_api import USERS, as_, client, resolve, statuses  # noqa: F401 - client is a fixture
from .test_brain_import import modify_copy, source_messages

pytestmark = pytest.mark.usefixtures("clean_db")

ROOT = Path(__file__).resolve().parents[3]
ALL = imp.Selection(all=True)
T0 = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
# Planted in the message's text, the copy's title, its tags, its review note and an admin's edit.
MSG, TITLE, TAG, NOTE, EDIT = "msgquokka71", "titlequokka72", "tagquokka73", "notequokka74", "editquokka76"
OPERATOR = USERS["operator"][0]
SAFE_DB_ERROR = "the database refused this change; nothing was saved"
NOT_READ = {"relevant": False, "prompt": False, "notes": False, "recall": False, "counted": False}


# --- Helpers -----------------------------------------------------------------------------------------------------


nowhere = bj.nowhere  # shared with the 0.6.2 identity tests (tests/brain_jarvis.py)


async def plant(key: str = "w1", *, labels: tuple[str, ...] = ("accomplishment",), content: str | None = None,
                redactions: dict[str, int] | None = None) -> dict[str, int]:
    """A general conversation: a request, the planted assistant message (MSG), and a closing line, so the session's
    summary (first user line, last assistant line) never quotes MSG."""
    async with session_scope() as s:
        sess = ArchiveSession(source_key="jarvis:session:%s" % key, source_session_id=key, title="Website check",
                              staff="jarvis", agent="supervisor", sensitivity="general", started_at=T0)
        s.add(sess)
        await s.flush()

        def row(position: int, role: str, body: str, **kw) -> ArchiveMessage:
            return ArchiveMessage(session_id=sess.id, source_key="jarvis:message:%s:%d" % (key, position),
                                  source_message_id=str(position), position=position, role=role, content=body,
                                  at=T0 + timedelta(minutes=position), **kw)

        rows = [row(0, "user", "Please renew the certificate."),
                row(1, "assistant", content or "The %s certificate was renewed and the site is up again." % MSG,
                    labels=list(labels), redactions=dict(redactions or {})),
                row(2, "assistant", "Closing the ticket now.")]
        s.add_all(rows)
        await s.flush()
        await recompute_session(s, sess.id)
        return {"session": sess.id, "message": rows[1].id, "first": rows[0].id, "key": rows[1].source_key}


async def promote(message_id: int, *, title: str = "Copy " + TITLE, kind: str = "project", agent: str = "") -> int:
    async with session_scope() as s:
        message = await s.get(ArchiveMessage, message_id)
        sess = await s.get(ArchiveSession, message.session_id)
        return (await review.promote_message(s, message, sess, kind=kind, title=title, agent=agent,
                                             actor=OPERATOR)).id


async def decide(item_id: int, decision: str, note: str = "") -> None:
    async with session_scope() as s:
        await store.review_item(s, await s.get(MemoryItem, item_id), decision=decision, actor=ADMIN[0], note=note)


async def withhold(client, message_id: int):
    await as_(client, "admin")
    return await client.post("/api/brain/messages/%d/withhold" % message_id)


async def withhold_service(message_id: int, cause: str = "admin") -> review.Withheld:
    async with session_scope() as s:
        return await review.withhold_message(s, await s.get(ArchiveMessage, message_id), actor=ADMIN[0], cause=cause)


async def item_state(item_id: int) -> tuple[MemoryItem | None, list[MemoryItemVersion]]:
    async with session_scope() as s:
        item = await s.get(MemoryItem, item_id)
        return item, await store.versions(s, item_id)


async def assert_emptied(item_id: int, *, status: str = "quarantined") -> MemoryItem:
    item, versions = await item_state(item_id)
    assert item is not None
    assert (item.title, item.body, item.tags, item.status) == (store.WITHHELD_COPY_TITLE, "", [], status)
    assert item.review_note == "" or status != "quarantined"  # a later reject or archive writes its own note
    assert item.flags["source_withheld"] is True
    assert all(store.blank_version(v) for v in versions), [(v.version, v.title) for v in versions]
    assert versions[0].change == "redacted" or status != "quarantined"
    return item


async def audits(action: str) -> list[AuditLog]:
    async with session_scope() as s:
        return list((await s.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id))).all())


async def agent_reads(item_id: int, word: str) -> dict[str, bool]:
    """Whether each agent path reads the item (AI context allowed by the caller)."""
    async with session_scope() as s:
        relevant = [i.id for i in await store.AgentMemory(s, "supervisor").relevant(word)]
        block = await store.AgentMemory(s, "seo_content").prompt_block(word)
        notes = [n["id"] for n in await reference_notes(store.AgentMemory(s, "monitoring"), {"topic": word})]
        approved = {a["agent"]: a["items"]["approved"] for a in await review.agents_view(s, PersonScope("admin", True))}
    recall = json.dumps((await ask("what did JARVIS accomplish with %s" % word)).data)
    item, _ = await item_state(item_id)
    return {"relevant": item_id in relevant, "prompt": bool(block and item and item.title in block),
            "notes": item_id in notes,
            "recall": bool(re.search(r'"item": %d\b|item #%d\b' % (item_id, item_id), recall)),
            "counted": approved["supervisor"] > 0}


async def listed_by(client, item_id: int) -> dict[str, bool]:
    """Whether a viewer, an operator and an admin list the item (GET /api/brain/items)."""
    out = {}
    for who in ("viewer", "operator", "admin"):
        await as_(client, who)
        r = await client.get("/api/brain/items", params={"limit": 200})
        assert r.status_code == 200, (who, r.text)
        out[who] = item_id in [i["id"] for i in r.json()["items"]]
    return out


async def lock_waits() -> None:
    """Returns once another connection waits for a lock (a row lock or an advisory lock)."""
    for _ in range(200):
        async with session_scope() as s:
            if await s.scalar(text("SELECT count(*) FROM pg_locks WHERE NOT granted")):
                return
        await asyncio.sleep(0.05)
    raise AssertionError("nobody waits for a lock")


def quoting_task(*, message_id: int | None = None, item_id: int | None = None, request_id: int | None = None) -> Task:
    """A history answer quoting the message and the copy, with a reference note naming the copy."""
    answer, sources = ["### Accomplishments"], [None]
    if message_id is not None:
        answer.append('- 2026-03-02 · jarvis · "The %s certificate was renewed" (source: message 1)' % MSG)
        sources.append({"message": message_id})
    if item_id is not None:
        answer.append('- 2026-03-02 · approved memory (project) · "Copy %s: renewed" (item #%d)' % (TITLE, item_id))
        sources.append({"item": item_id})
    data = {"local_only": True, "answer": answer, "sources": sources}
    if item_id is not None:
        data["memory_notes"] = [{"id": item_id, "kind": "project", "title": "Copy " + TITLE}]
    return Task(agent="supervisor", capability="recall", params={}, status="succeeded", risk="read",
                request_id=request_id, result={"summary": "history", "data": data})


def precheck_sql() -> str:
    """The count scripts/brain-precheck.ps1 runs on the live database (read-only)."""
    script = (ROOT / "scripts" / "brain-precheck.ps1").read_text().replace("\r\n", "\n")
    return re.search(r"\$withheldCopiesSql = @'\n(.*?)\n'@", script, re.S).group(1)


async def withheld_counts() -> tuple[int, int]:
    """(importer status' withheld_copies_with_text, the pre-check's count)."""
    async with session_scope() as s:
        python = len(await archive.withheld_copies(s))
        sql = int(await s.scalar(text(precheck_sql())))
    return python, sql


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return bj.build(tmp_path_factory.mktemp("jarvis") / "fx")


@pytest.fixture
def fx(built, tmp_path):
    return bj.copy(built, tmp_path / "fx")


async def general_message(*, role: str = "user", run_id: int | None = None, exclude: tuple[int, ...] = ()
                          ) -> ArchiveMessage:
    """An imported message with text in a general conversation (promotable by anyone)."""
    async with session_scope() as s:
        query = (select(ArchiveMessage).join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                 .where(ArchiveSession.sensitivity == "general", ArchiveSession.agent != "credit_repair",
                        ArchiveMessage.sensitivity == "general", ArchiveMessage.withheld.is_(False),
                        ArchiveMessage.content != "", ArchiveMessage.role == role, ArchiveMessage.id.not_in(exclude))
                 .order_by(ArchiveMessage.id))
        if run_id is not None:
            query = query.where(ArchiveMessage.import_run_id == run_id)
        found = await s.scalar(query.limit(1))
        assert found is not None
        return found


async def message_by_key(key: str) -> ArchiveMessage | None:
    async with session_scope() as s:
        return await s.scalar(select(ArchiveMessage).where(ArchiveMessage.source_key == key))


async def message_of(source_session_id: str, role: str = "assistant") -> ArchiveMessage:
    """The first message of this role with text in an imported conversation."""
    async with session_scope() as s:
        sess = await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == source_session_id))
        return await s.scalar(select(ArchiveMessage).where(
            ArchiveMessage.session_id == sess.id, ArchiveMessage.content != "", ArchiveMessage.role == role)
            .order_by(ArchiveMessage.position).limit(1))


# --- Withholding before promotion -----------------------------------------------------------------------------------


async def test_withhold_before_promotion_leaves_nothing_to_promote(client, caplog):
    ids = await plant()
    assert (await withhold(client, ids["message"])).status_code == 200
    await as_(client, "operator")
    r = await client.post("/api/brain/messages/%d/promote" % ids["message"], json={"kind": "project", "title": "t"})
    assert r.status_code == 400 and "withheld" in r.json()["detail"]
    assert await nowhere(MSG, caplog=caplog) == []


# --- Withholding after promotion ------------------------------------------------------------------------------------


async def test_withhold_with_a_pending_copy_succeeds(client, caplog):
    """The 0.6.0 defect: a pending copy made the withhold fail with a CheckViolation, and nothing was withheld."""
    caplog.set_level(logging.DEBUG)
    ids = await plant()
    copy = await promote(ids["message"])
    r = await withhold(client, ids["message"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["message"]["withheld"] is True and body["copies"]["items"] == [copy]
    assert body["copies"]["versions_blanked"] == 1
    await assert_emptied(copy)
    for marker in (MSG, TITLE):
        assert await nowhere(marker, caplog=caplog) == [], marker
    assert "CheckViolation" not in caplog.text


async def test_withhold_empties_an_approved_copy_and_takes_it_from_every_agent_path(client):
    await allow_ai_context()
    ids = await plant()
    copy = await promote(ids["message"])
    await decide(copy, "approve")
    before = await agent_reads(copy, MSG)
    assert before == {"relevant": True, "prompt": True, "notes": True, "recall": True, "counted": True}
    assert (await withhold(client, ids["message"])).status_code == 200
    await assert_emptied(copy)
    assert await agent_reads(copy, MSG) == {"relevant": False, "prompt": False, "notes": False, "recall": False,
                                            "counted": False}
    assert (await ask("what did JARVIS accomplish with " + MSG)).data["counts"]["items"] == 0


async def test_withhold_empties_rejected_archived_and_soft_deleted_copies(client):
    ids = await plant()
    rejected, archived, deleted = [await promote(ids["message"], title="%s %s" % (w, TITLE))
                                   for w in ("Rejected", "Archived", "Deleted")]
    await decide(rejected, "reject")
    await decide(archived, "archive")
    async with session_scope() as s:
        gone = await store.delete_item(s, await s.get(MemoryItem, deleted), actor=ADMIN[0])
        deleted_at = gone.deleted_at
    r = await withhold(client, ids["message"])
    assert sorted(r.json()["copies"]["items"]) == sorted([rejected, archived, deleted])
    for copy in (rejected, archived, deleted):
        await assert_emptied(copy)
    assert (await item_state(deleted))[0].deleted_at == deleted_at  # stays deleted
    assert await nowhere(TITLE) == []
    befores = {e.detail["item"]: e.detail["status_before"] for e in await audits("memory.source_withheld")}
    assert befores == {rejected: "rejected", archived: "archived", deleted: "pending_review"}


async def test_withhold_empties_an_edited_copy_including_the_admins_own_text(client):
    ids = await plant()
    copy = await promote(ids["message"])
    async with session_scope() as s:
        await store.edit_item(s, await s.get(MemoryItem, copy), actor=ADMIN[0], body="Rewritten by the admin: " + EDIT,
                              tags=[TAG], note="edited")
    await decide(copy, "approve", note="Checked " + NOTE)
    assert (await item_state(copy))[0].review_note == "Checked " + NOTE
    r = await withhold(client, ids["message"])
    assert r.json()["copies"]["versions_blanked"] == 3  # promoted, edited, approved
    await assert_emptied(copy)
    for marker in (MSG, TITLE, TAG, NOTE, EDIT):
        assert await nowhere(marker) == [], marker


async def test_withhold_empties_a_copy_quarantined_at_promotion(client):
    """A message a secret was removed from: its copy was quarantined at promotion, so 0.6.0's withhold succeeded and
    only flagged it, keeping its text."""
    ids = await plant(content="The %s key [REDACTED:api_key] was rotated." % MSG, redactions={"api_key": 1})
    copy = await promote(ids["message"])
    assert (await item_state(copy))[0].status == "quarantined"
    assert (await withhold(client, ids["message"])).status_code == 200
    item = await assert_emptied(copy)
    assert item.flags["secrets_redacted"] == {"api_key": 1}  # counts only, kept
    assert await nowhere(MSG) == []


async def test_withhold_empties_every_copy_of_the_message(client):
    ids = await plant()
    copies = [await promote(ids["message"], title="Copy %d %s" % (n, TITLE)) for n in range(3)]
    other = await plant("w2", content="Another conversation entirely.")
    unrelated = await promote(other["message"], title="Unrelated copy")
    r = await withhold(client, ids["message"])
    assert sorted(r.json()["copies"]["items"]) == copies
    for copy in copies:
        await assert_emptied(copy)
    assert (await item_state(unrelated))[0].title == "Unrelated copy"


async def test_an_emptied_copy_keeps_its_ids_and_provenance(client):
    ids = await plant()
    copy = await promote(ids["message"])
    await decide(copy, "approve")
    before, versions_before = await item_state(copy)
    assert (await withhold(client, ids["message"])).status_code == 200
    item, versions = await item_state(copy)
    assert (item.id, item.origin, item.kind, item.agent, item.sensitivity, item.source_ref, item.source_at,
            item.created_by, item.created_at, item.reviewed_by) == (
        before.id, "promoted", before.kind, before.agent, before.sensitivity, before.source_ref, before.source_at,
        before.created_by, before.created_at, before.reviewed_by)
    assert item.source_ref == archive.promoted_ref("w1", 1)
    assert {k: item.flags[k] for k in ("message_id", "archive_session_id", "message_source_key")} == {
        "message_id": ids["message"], "archive_session_id": ids["session"], "message_source_key": ids["key"]}
    assert item.version == before.version + 1 and [v.version for v in versions] == list(range(item.version, 0, -1))
    old = {v.version: (v.id, v.change, v.changed_by, v.changed_at, v.status, v.kind) for v in versions_before}
    assert {v.version: (v.id, v.change, v.changed_by, v.changed_at, v.status, v.kind) for v in versions[1:]} == old
    assert (versions[0].change, versions[0].title, versions[0].note) == (
        "redacted", store.WITHHELD_COPY_TITLE, store.WITHHELD_COPY_NOTE)


async def test_an_emptied_copy_cannot_be_approved_reopened_or_edited(client):
    ids = await plant()
    copy = await promote(ids["message"])
    assert (await withhold(client, ids["message"])).status_code == 200
    url = "/api/brain/items/%d" % copy
    await as_(client, "admin+")
    for decision in ("approve", "reopen"):
        r = await client.post(url + "/review", json={"decision": decision})
        assert r.status_code == 400 and "cannot be approved or reopened" in r.json()["detail"], decision
    r = await client.patch(url, json={"body": "new text"})
    assert r.status_code == 400 and "write a new item instead" in r.json()["detail"]
    await as_(client, "operator")
    assert (await client.post(url + "/review", json={"decision": "approve"})).status_code == 404  # quarantined
    await as_(client, "admin+")
    assert (await client.post(url + "/review", json={"decision": "reject"})).status_code == 200
    await as_(client, "operator")  # rejected: an operator sees it, and may not approve or reopen it
    for decision in ("approve", "reopen"):
        r = await client.post(url + "/review", json={"decision": decision})
        assert r.status_code == 403 and "source_withheld" in r.json()["detail"], decision
    assert (await client.patch(url, json={"title": "Back"})).status_code == 403
    async with session_scope() as s:
        item = await s.get(MemoryItem, copy)
        for decision in ("approve", "reopen"):
            with pytest.raises(store.BrainError):
                await store.review_item(s, item, decision=decision, actor=ADMIN[0])
        with pytest.raises(store.BrainError):
            await store.edit_item(s, item, actor=ADMIN[0], title="Back again")
        await store.review_item(s, item, decision="archive", actor=ADMIN[0])  # still possible
    item = await assert_emptied(copy, status="archived")
    assert item.flags["source_withheld"] is True


async def test_an_emptied_copy_matches_no_search(client):
    ids = await plant()
    copy = await promote(ids["message"])
    await as_(client, "admin+")
    for q in (TITLE, MSG):
        assert (await client.get("/api/brain/items", params={"q": q})).json()["total"] == 1, q
    assert (await withhold(client, ids["message"])).status_code == 200
    await as_(client, "admin+")
    for q in (TITLE, MSG):
        items = (await client.get("/api/brain/items", params={"q": q})).json()
        found = (await client.get("/api/brain/search", params={"q": q})).json()
        assert items["total"] == 0 and found["memory"]["total"] == 0 and found["archive"]["total"] == 0, q
    assert (await client.get("/api/brain/items/%d" % copy)).json()["item"]["title"] == store.WITHHELD_COPY_TITLE


async def test_withhold_removes_quotes_and_reference_notes_of_the_message_and_its_copies(client):
    ids = await plant()
    copy = await promote(ids["message"])
    async with session_scope() as s:
        request = SupervisorRequest(message="what was renewed?", status="completed", summary="")
        s.add(request)
        await s.flush()
        task = quoting_task(message_id=ids["message"], item_id=copy, request_id=request.id)
        s.add(task)
        await s.flush()
        request.summary = "\n".join(task.result["data"]["answer"])
        request_id, task_id = request.id, task.id
    r = await withhold(client, ids["message"])
    assert r.json()["answers_scrubbed"] == 1
    async with session_scope() as s:
        data = (await s.get(Task, task_id)).result["data"]
        assert data["answer"][1:] == [REMOVED, REMOVED] and data["memory_notes"] == []
        assert MSG not in (await s.get(SupervisorRequest, request_id)).summary
    for marker in (MSG, TITLE):
        assert await nowhere(marker) == [], marker


async def test_withhold_blanks_rollback_copies_under_older_row_ids(client):
    ids = await plant()
    copy_row = {"session_id": ids["session"], "source_key": ids["key"], "position": 1, "role": "assistant",
                "content": "An older copy: " + MSG, "tool_calls": [], "labels": ["accomplishment"], "withheld": False,
                "sensitivity": "general", "truncated": False}
    async with session_scope() as s:
        runs = [ImportRun(status="rolled_back", created_by="importer (cli)") for _ in range(3)]
        s.add_all(runs)
        await s.flush()
        s.add_all([
            # A rewound row deleted by an import that was rolled back; the row came back under a new id since.
            ImportRecord(run_id=runs[0].id, source_key=ids["key"], record_type="message", action="updated",
                         reason="rewound since an earlier import", target_table="archive_messages", target_id=900001,
                         before={"deleted_row": {**copy_row, "id": 900001}}),
            ImportRecord(run_id=runs[1].id, source_key=ids["key"], record_type="message", action="updated",
                         reason="changed since an earlier import", target_table="archive_messages", target_id=900002,
                         before=copy_row),
            ImportRecord(run_id=runs[2].id, source_key="jarvis:message:other:1", record_type="message",
                         action="updated", reason="changed since an earlier import", target_table="archive_messages",
                         target_id=900003, before={**copy_row, "content": "unrelated text"}),
        ])
    r = await withhold(client, ids["message"])
    assert r.json()["rollback_copies_blanked"] == 2
    async with session_scope() as s:
        befores = {r.target_id: r.before for r in (await s.scalars(select(ImportRecord))).all()}
    assert befores[900001]["deleted_row"]["content"] == "" and befores[900001]["deleted_row"]["withheld"] is True
    assert befores[900002]["content"] == "" and befores[900002]["withheld"] is True
    assert befores[900003]["content"] == "unrelated text"
    assert await nowhere(MSG) == []


async def test_withhold_is_refused_while_an_import_rollback_or_rescan_holds_the_lock(client):
    ids = await plant()
    copy = await promote(ids["message"])
    locks = await imp.LockConnection.open()  # what a running import, rollback or rescan holds
    try:
        assert await locks.try_lock(imp.GLOBAL_LOCK)
        r = await withhold(client, ids["message"])
        assert r.status_code == 409 and "import, rollback or rescan is running" in r.json()["detail"]
    finally:
        await locks.close()
    async with session_scope() as s:
        message = await s.get(ArchiveMessage, ids["message"])
        assert not message.withheld and MSG in message.content
        assert await s.scalar(select(func.count()).select_from(BrainTombstone)) == 0
    assert (await item_state(copy))[0].title == "Copy " + TITLE
    assert await audits("archive.message_withheld") == []
    assert (await withhold(client, ids["message"])).status_code == 200


class _Refused(Exception):
    sqlstate = "23514"


async def test_a_failed_withhold_changes_nothing_and_logs_no_text(client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    ids = await plant()
    copy = await promote(ids["message"])
    async with session_scope() as s:
        run = ImportRun(status="completed", created_by="importer (cli)")
        s.add(run)
        await s.flush()
        s.add(ImportRecord(run_id=run.id, source_key=ids["key"], record_type="message", action="updated",
                           target_table="archive_messages", target_id=ids["message"],
                           before={"content": "older " + MSG, "withheld": False}))
    await as_(client, "admin")  # signed in before the snapshot (a sign-in is audited)
    before = await bj.stored_text(bj.BRAIN_TABLES + ("audit_log", "tasks"))

    async def refused(session, item, *, actor):  # the database refuses the copy's new version
        raise IntegrityError("UPDATE memory_items SET title = $1", {},
                             _Refused("new row violates check constraint\nDETAIL: Failing row contains (%s)" % TITLE))

    monkeypatch.setattr(store, "empty_withheld_copy", refused)
    r = await withhold(client, ids["message"])
    assert r.status_code == 409 and r.json() == {"detail": SAFE_DB_ERROR}
    assert await bj.stored_text(bj.BRAIN_TABLES + ("audit_log", "tasks")) == before  # message, tombstone, copy
    assert (await item_state(copy))[0].title == "Copy " + TITLE
    logged = [r.getMessage() for r in caplog.records if r.name.startswith("app")]
    assert any("IntegrityError (_Refused, SQLSTATE 23514)" in line for line in logged)
    assert not [line for line in logged if TITLE in line or MSG in line]


async def test_the_promoted_count_is_for_admins_only(client, service_headers):
    ids = await plant()
    shared = await promote(ids["message"])
    credit = await promote(ids["message"], title="Credit copy " + TITLE, agent="credit_repair")
    url = "/api/brain/messages/%d/promoted" % ids["message"]
    got = await statuses(client, "GET", url)
    assert got == {"viewer": 403, "operator": 403, "operator+": 403, "admin": 200, "admin+": 200}
    client.cookies.clear()
    assert (await client.get(url, headers=service_headers)).status_code == 403
    await as_(client, "admin")  # no sensitive-data permission: counted, not listed
    assert (await client.get(url)).json() == {"message": ids["message"], "count": 2,
                                              "items": [{"id": shared, "status": "pending_review", "deleted": False}]}
    await as_(client, "admin+")
    assert [i["id"] for i in (await client.get(url)).json()["items"]] == [shared, credit]
    assert (await withhold(client, ids["message"])).status_code == 200
    await as_(client, "admin+")
    body = (await client.get(url)).json()
    assert body["count"] == 2 and {i["status"] for i in body["items"]} == {"quarantined"}


async def test_withhold_finds_a_copy_promoted_before_a_rollback_and_reimport(built):
    first = await imp.run(bj.paths(built), ALL)
    message = await general_message(role="assistant")
    copy = await promote(message.id)
    async with session_scope() as s:
        await imp.rollback_run(s, first["run_id"], ADMIN[0])
    item, _ = await item_state(copy)
    assert item.source_ref.endswith("(source removed)") and item.body == message.content
    await imp.run(bj.paths(built), ALL)
    back = await message_by_key(message.source_key)
    assert back.id != message.id and item.flags["message_id"] == message.id  # a new row id; the copy names the old
    done = await withhold_service(back.id)
    assert [c["item"] for c in done.copies] == [copy]
    await assert_emptied(copy)
    assert await nowhere(TITLE) == []


async def test_withhold_finds_a_legacy_copy_by_source_ref_only_when_its_message_id_is_gone(client):
    """Items promoted by 0.6.0 have no message_source_key: their source_ref names the message, but a later message
    at the same position must not be taken for theirs."""
    ids = await plant()
    ref = archive.promoted_ref("w1", 1)
    async with session_scope() as s:
        def legacy(title: str, message_id: int, source_ref: str):
            return store.create_item(s, kind="project", title=title, body="A copy of " + MSG, actor=ADMIN[0],
                                     origin="promoted", source_ref=source_ref,
                                     flags={"message_id": message_id, "archive_session_id": ids["session"]})
        gone = (await legacy("Legacy " + TITLE, 999_001, ref + archive.SOURCE_REMOVED)).id
        gone_too = (await legacy("Legacy two " + TITLE, 999_002, ref)).id
        named = (await legacy("Named elsewhere", ids["first"], ref)).id  # its message_id is another live row
    assert (await withhold(client, ids["message"])).json()["copies"]["items"] == [gone, gone_too]
    await assert_emptied(gone)
    await assert_emptied(gone_too)
    item, _ = await item_state(named)
    assert item.title == "Named elsewhere" and MSG in item.body


async def test_the_agents_rule_finds_the_same_legacy_copies_as_the_sweep_through_the_session_index(client):
    """policy.withheld_source() reads a 0.6.0 copy's session id and position out of its source_ref ("(source removed)"
    or not), so the agents' and the Brain page's queries look its message up by index instead of formatting a
    source_ref for every withheld message; it selects exactly what archive.withheld_copies() and the pre-check do."""
    a, b = await plant("wa"), await plant("wb")
    await plant("wl")  # a live message at the same position in another conversation
    async with session_scope() as s:
        for ids in (a, b):
            await s.execute(update(ArchiveMessage).where(ArchiveMessage.id == ids["message"]).values(
                withheld=True, content="", labels=[]))

        async def legacy(name: str, ref: str, *, message_id: int = 999_000, origin: str = "promoted") -> int:
            return (await store.create_item(s, kind="project", title="%s %s" % (name, TITLE), body="A copy",
                                            actor=OPERATOR, origin=origin, source_ref=ref,
                                            flags={"message_id": message_id})).id

        found = [await legacy("withheld", archive.promoted_ref("wa", 1)),
                 await legacy("withheld, source removed", archive.promoted_ref("wb", 1) + archive.SOURCE_REMOVED)]
        others = [await legacy("live message", archive.promoted_ref("wl", 1)),
                await legacy("another position", archive.promoted_ref("wa", 2)),
                await legacy("no such conversation", archive.promoted_ref("w", 1)),
                await legacy("message_id names a live row", archive.promoted_ref("wa", 1), message_id=a["first"]),
                await legacy("not promoted", archive.promoted_ref("wa", 1), origin="manual"),
                await legacy("no position", "JARVIS session wa · message 9" * 3 + "1" * 30)]
        selected = set(await s.scalars(select(MemoryItem.id).where(policy.withheld_source())))
        assert selected == set(found) and not selected & set(others)
        assert {item.id for item, _ in await archive.withheld_copies(s)} == set(found)
        query = select(func.count()).select_from(MemoryItem).where(policy.withheld_source()).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        # The statement, which planner statistics cannot change (review 2, S28): the legacy rule parses the item's own
        # source_ref into a session id and a position and compares the indexed columns with them, so each lookup can
        # go through archive_sessions_source_session_idx and archive_messages_session_idx; it never formats a
        # source_ref for every withheld message (the regression 0.6.1 removed), nor compares the item's ref with one.
        sql_ = str(query)
        assert "format(" not in sql_.lower() and "memory_items.source_ref =" not in sql_, sql_
        assert "archive_sessions.source_session_id = SUBSTRING(regexp_replace(memory_items.source_ref" in sql_, sql_
        assert "archive_messages.session_id = archive_sessions.id AND archive_messages.position = CAST(SUBSTRING(" \
            "regexp_replace(memory_items.source_ref" in sql_, sql_
        indexes = dict((await s.execute(text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname IN "
            "('archive_sessions_source_session_idx', 'archive_messages_session_idx')"))).all())
        assert re.search(r"ON public\.archive_sessions USING btree \(source_session_id\)$",
                         indexes.get("archive_sessions_source_session_idx", "")), indexes
        assert re.search(r'ON public\.archive_messages USING btree \(session_id, "?position"?\)$',
                         indexes.get("archive_messages_session_idx", "")), indexes
        # The plan names the index only while these tables have no planner statistics. With statistics (an ANALYZE,
        # autovacuum, or pg_statistic that clean_db's TRUNCATE keeps from an earlier test) the planner may read the
        # few withheld messages once into a hashed subplan instead, so the plan is checked only without them.
        await s.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join((await (await s.connection()).exec_driver_sql("EXPLAIN " + str(query))).scalars())
        assert "format(" not in plan, plan
        analyzed = await s.scalar(text(
            "SELECT EXISTS (SELECT 1 FROM pg_stats WHERE schemaname = 'public' AND tablename IN "
            "('archive_sessions', 'archive_messages', 'memory_items'))"))
        if not analyzed:
            assert "archive_sessions_source_session_idx" in plan, plan
    assert await withheld_counts() == (2, 2)


async def test_a_promote_waits_for_a_withhold_of_the_same_message_and_is_refused(client):
    """The promote route locks the message: a withhold that has not committed yet makes it wait, and it then finds
    the message withheld (without the lock it read the old row and kept a full copy of the withheld text)."""
    ids = await plant()
    await as_(client, "operator")
    async with session_scope() as s:  # a withhold, flushed but not committed: it holds the message's row lock
        message = await s.scalar(select(ArchiveMessage).where(ArchiveMessage.id == ids["message"]).with_for_update())
        await review.withhold_message(s, message, actor=ADMIN[0])
        promoting = asyncio.ensure_future(client.post("/api/brain/messages/%d/promote" % ids["message"],
                                                      json={"kind": "project", "title": "Copy " + TITLE}))
        await lock_waits()
        assert not promoting.done()
    r = await promoting
    assert r.status_code == 400 and "withheld" in r.json()["detail"]
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(MemoryItem)) == 0
    assert await nowhere(MSG) == [] and await nowhere(TITLE) == []


async def test_a_withhold_waits_for_a_promote_of_the_same_message_and_empties_its_copy(client, monkeypatch):
    """A promote that has created its copy but not committed yet holds the message's row lock: the withhold waits
    for it, then finds the copy and empties it (without the lock it missed the uncommitted copy)."""
    ids = await plant()
    reached, release = asyncio.Event(), asyncio.Event()
    original = review.promote_message

    async def paused(*args, **kwargs):
        item = await original(*args, **kwargs)
        reached.set()
        await release.wait()
        return item

    monkeypatch.setattr(review, "promote_message", paused)
    await as_(client, "admin")
    promoting = asyncio.ensure_future(client.post("/api/brain/messages/%d/promote" % ids["message"],
                                                  json={"kind": "project", "title": "Copy " + TITLE}))
    try:
        await asyncio.wait_for(reached.wait(), 20)
        withholding = asyncio.ensure_future(client.post("/api/brain/messages/%d/withhold" % ids["message"]))
        await lock_waits()
        assert not withholding.done()
    finally:
        release.set()
    promoted, withheld = await promoting, await withholding
    assert promoted.status_code == 201, promoted.text
    copy = promoted.json()["item"]["id"]
    assert withheld.status_code == 200 and withheld.json()["copies"]["items"] == [copy]
    await assert_emptied(copy)
    async with session_scope() as s:
        assert await archive.withheld_copies(s) == []
    assert await nowhere(TITLE) == []


async def test_a_message_an_admin_withheld_cannot_be_promoted_while_its_row_still_has_text(client):
    """Its tombstone says an admin withheld it (decisions-apply could not, or an import wrote it back): the next
    import withholds the row; a promote meanwhile is refused."""
    ids = await plant()
    async with session_scope() as s:
        s.add(BrainTombstone(source_key=ids["key"], purged_by=ADMIN[0], reason=WITHHELD_BY_ADMIN))
    await as_(client, "operator")
    r = await client.post("/api/brain/messages/%d/promote" % ids["message"], json={"kind": "project", "title": "t"})
    assert r.status_code == 400 and "an admin withheld this message" in r.json()["detail"]


# --- Purging promoted memories --------------------------------------------------------------------------------------


async def test_purging_an_emptied_copy_removes_it_versions_conflicts_quotes_and_notes(client):
    ids = await plant()
    copy = await promote(ids["message"])
    await decide(copy, "approve")
    assert (await withhold(client, ids["message"])).status_code == 200
    async with session_scope() as s:
        run = ImportRun(status="completed", created_by="importer (cli)")
        s.add(run)
        await s.flush()
        s.add(MemoryConflict(run_id=run.id, kind="memory_item", title="Same title as an existing item",
                             existing_table="memory_items", existing_id=copy, existing={"id": copy}))
        task = quoting_task(item_id=copy)  # a quote a worker wrote after the withhold
        s.add(task)
        await s.flush()
        task_id = task.id
    await as_(client, "admin")
    assert (await client.delete("/api/brain/items/%d" % copy)).status_code == 200
    r = await client.delete("/api/brain/items/%d/purge" % copy)
    assert r.status_code == 200 and r.json() == {"purged": copy, "versions_removed": 4, "conflicts_removed": 1,
                                                 "tombstone": False}
    async with session_scope() as s:
        assert await s.get(MemoryItem, copy) is None
        assert await s.scalar(select(func.count()).select_from(MemoryItemVersion)) == 0
        assert await s.scalar(select(func.count()).select_from(MemoryConflict)) == 0
        data = (await s.get(Task, task_id)).result["data"]
        assert data["answer"][1] == REMOVED and data["memory_notes"] == []
    entry = (await audits("memory.purged"))[0]
    assert (entry.detail["source_withheld"], entry.detail["origin"], entry.detail["message"]) == (
        True, "promoted", ids["message"])
    assert await nowhere(TITLE) == [] and await nowhere(MSG) == []


async def test_purging_a_live_copy_keeps_its_source_message(client):
    ids = await plant()
    copy = await promote(ids["message"])
    await as_(client, "admin")
    await client.delete("/api/brain/items/%d" % copy)
    assert (await client.delete("/api/brain/items/%d/purge" % copy)).status_code == 200
    async with session_scope() as s:
        message = await s.get(ArchiveMessage, ids["message"])
        assert not message.withheld and MSG in message.content  # a purge never withholds the source
    assert await nowhere(MSG, allow=("archive_messages.content", "archive_messages.search")) == []
    assert await nowhere(TITLE) == []


async def test_purge_then_withhold_and_withhold_then_purge_end_the_same(client):
    first, second = await plant("w1"), await plant("w2")
    a, b = await promote(first["message"]), await promote(second["message"])
    await as_(client, "admin")
    await client.delete("/api/brain/items/%d" % a)
    assert (await client.delete("/api/brain/items/%d/purge" % a)).status_code == 200
    assert (await withhold(client, first["message"])).json()["copies"]["items"] == []
    assert (await withhold(client, second["message"])).json()["copies"]["items"] == [b]
    await as_(client, "admin")
    await client.delete("/api/brain/items/%d" % b)
    assert (await client.delete("/api/brain/items/%d/purge" % b)).status_code == 200
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(MemoryItem)) == 0
        for ids in (first, second):
            message = await s.get(ArchiveMessage, ids["message"])
            assert (message.withheld, message.content) == (True, "")
            assert (await s.get(BrainTombstone, ids["key"])).reason == WITHHELD_BY_ADMIN
    for ids in (first, second):
        assert (await client.get("/api/brain/messages/%d/promoted" % ids["message"])).json()["count"] == 0
    for marker in (MSG, TITLE):
        assert await nowhere(marker) == [], marker


async def test_purging_a_promoted_copy_writes_no_import_tombstone_and_a_complete_audit_entry(client):
    ids = await plant()
    copy = await promote(ids["message"])
    await as_(client, "admin")
    await client.delete("/api/brain/items/%d" % copy)
    assert (await client.delete("/api/brain/items/%d/purge" % copy)).status_code == 200
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(BrainTombstone)) == 0
    [entry] = await audits("memory.purged")
    assert entry.detail == {"item": copy, "kind": "project", "agent": "", "versions_removed": 2,
                            "conflicts_removed": 0, "tombstone": False, "origin": "promoted",
                            "message": ids["message"], "source_withheld": False, "answers_scrubbed": 0}


async def test_purge_and_version_redaction_are_refused_while_an_import_rollback_or_rescan_holds_the_lock(client):
    """A running import reads the tombstones once: an item purged meanwhile came back from its source with its text.
    A rescan re-cleans versions it read earlier: a redaction meanwhile was overwritten."""
    async with session_scope() as s:
        item = await store.create_item(s, kind="knowledge", title="Imported " + TITLE, body="Imported text.",
                                       actor="importer (cli)", origin="import", source_key="jarvis:file:planted.md")
        item_id = item.id
    await as_(client, "admin")
    assert (await client.patch("/api/brain/items/%d" % item_id, json={"body": "Edited text."})).status_code == 200
    assert (await client.delete("/api/brain/items/%d" % item_id)).status_code == 200
    locks = await imp.LockConnection.open()  # what a running import, rollback or rescan holds
    try:
        assert await locks.try_lock(imp.GLOBAL_LOCK)
        for method, url in (("POST", "/api/brain/items/%d/versions/1/redact" % item_id),
                            ("DELETE", "/api/brain/items/%d/purge" % item_id)):
            r = await client.request(method, url)
            assert r.status_code == 409 and "import, rollback or rescan is running" in r.json()["detail"], url
    finally:
        await locks.close()
    item, versions = await item_state(item_id)
    assert item is not None and versions[-1].title == "Imported " + TITLE  # version 1, not redacted
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(BrainTombstone)) == 0
    assert await audits("memory.purged") == [] and await audits("memory.version_redacted") == []
    assert (await client.post("/api/brain/items/%d/versions/1/redact" % item_id)).status_code == 200
    assert (await client.delete("/api/brain/items/%d/purge" % item_id)).status_code == 200


# --- Conflict handling ----------------------------------------------------------------------------------------------


async def conflict_on(copy: int, *, status: str = "completed") -> dict[str, int]:
    """Import run R raised a same-title conflict between the copy (existing) and an imported item (incoming)."""
    async with session_scope() as s:
        run = ImportRun(status=status, created_by="importer (cli)")
        s.add(run)
        await s.flush()
        incoming = await store.create_item(s, kind="project", title="Imported twin", body="Imported text.",
                                           actor="importer (cli)", origin="import", import_run_id=run.id,
                                           flags={"conflict": True})
        c = MemoryConflict(run_id=run.id, kind="memory_item", title="Same title as an existing item: twin",
                           existing_table="memory_items", existing_id=copy, incoming_item_id=incoming.id,
                           existing={"id": copy}, incoming={"id": incoming.id, "title": "Imported twin"})
        s.add(c)
        await s.flush()
        return {"run": run.id, "incoming": incoming.id, "conflict": c.id}


async def test_an_emptied_copy_on_the_existing_side_of_an_open_conflict_stays_decidable_by_an_admin(client):
    ids = await plant()
    copy = await promote(ids["message"])
    first, second = await conflict_on(copy), await conflict_on(copy)
    assert (await withhold(client, ids["message"])).status_code == 200
    await as_(client, "operator")  # the copy is quarantined now: its conflicts are an admin's
    assert (await client.get("/api/brain/conflicts")).json()["total"] == 0
    await as_(client, "admin")
    listed = {c["id"]: c for c in (await client.get("/api/brain/conflicts")).json()["conflicts"]}
    assert listed[first["conflict"]]["existing_item"]["title"] == store.WITHHELD_COPY_TITLE
    r = await resolve(client, first["conflict"], "use_incoming")
    assert r.status_code == 200, r.text
    assert r.json()["effects"]["items"][0]["id"] == copy
    await assert_emptied(copy, status="archived")
    assert (await resolve(client, second["conflict"], "keep_both")).status_code == 200


async def test_withhold_after_a_use_incoming_decision_does_not_block_that_runs_rollback(client):
    ids = await plant()
    copy = await promote(ids["message"])
    await decide(copy, "approve")
    c = await conflict_on(copy)
    await as_(client, "admin")
    assert (await resolve(client, c["conflict"], "use_incoming")).status_code == 200  # the copy is archived
    assert (await withhold(client, ids["message"])).status_code == 200
    await as_(client, "admin")
    r = await client.post("/api/brain/imports/%d/rollback" % c["run"], json={"confirm": str(c["run"])})
    assert r.status_code == 200, r.text
    assert r.json()["rollback"]["kept_withheld_copies"] == [copy]
    assert r.json()["rollback"]["reverted"]["items"] == 0
    await assert_emptied(copy)  # never back to approved, never its text again
    [entry] = await audits("brain.import_rolled_back")
    assert entry.detail["kept_withheld_copies"] == [copy]


async def test_an_emptied_copy_named_by_a_decided_conflict_can_be_purged(client):
    first, second = await plant("w1"), await plant("w2")
    emptied, live = await promote(first["message"]), await promote(second["message"])
    runs = [await conflict_on(emptied), await conflict_on(live)]
    await as_(client, "admin")
    for c in runs:
        assert (await resolve(client, c["conflict"], "use_incoming")).status_code == 200
    assert (await withhold(client, first["message"])).status_code == 200
    await as_(client, "admin")
    for copy in (emptied, live):
        assert (await client.delete("/api/brain/items/%d" % copy)).status_code == 200
    r = await client.delete("/api/brain/items/%d/purge" % live)  # a rollback of run 2 would revert it
    assert r.status_code == 400 and "import run %d" % runs[1]["run"] in r.json()["detail"]
    assert (await client.delete("/api/brain/items/%d/purge" % emptied)).status_code == 200


async def test_a_keep_existing_decision_does_not_block_purging_the_emptied_copy(client):
    """keep_existing rejects the incoming item, which the conflict's own run created: a rollback of that run skips
    the effect and deletes the item anyway, so purging the copy loses nothing it would revert. An effect on an item
    of another run still blocks."""
    first, second = await plant("w1"), await plant("w2")
    copy, other = await promote(first["message"]), await promote(second["message"])
    c, d = await conflict_on(copy), await conflict_on(other)
    await as_(client, "admin")
    for conflict in (c, d):
        r = await resolve(client, conflict["conflict"], "keep_existing")
        assert r.status_code == 200 and [e["id"] for e in r.json()["effects"]["items"]] == [conflict["incoming"]]
    async with session_scope() as s:  # d's incoming item is no longer its run's own: a rollback would revert it
        await s.execute(update(MemoryItem).where(MemoryItem.id == d["incoming"]).values(import_run_id=None))
    for message in (first, second):
        assert (await withhold(client, message["message"])).status_code == 200
    await as_(client, "admin")
    for item in (copy, other):
        assert (await client.delete("/api/brain/items/%d" % item)).status_code == 200
    r = await client.delete("/api/brain/items/%d/purge" % other)
    assert r.status_code == 400 and "import run %d" % d["run"] in r.json()["detail"]
    r = await client.delete("/api/brain/items/%d/purge" % copy)
    assert r.status_code == 200 and r.json()["conflicts_removed"] == 1
    rolled = await client.post("/api/brain/imports/%d/rollback" % c["run"],
                               json={"confirm": str(c["run"]), "discard_reviewed": True})
    assert rolled.status_code == 200, rolled.text
    async with session_scope() as s:
        assert await s.get(MemoryItem, c["incoming"]) is None and await s.get(MemoryItem, copy) is None


async def test_a_dismissed_conflict_no_longer_blocks_purge(client):
    async with session_scope() as s:
        item = await store.create_item(s, kind="business", title="Opening hours", body="9 to 5.", actor=ADMIN[0])
        item_id = item.id
    c = await conflict_on(item_id)
    await as_(client, "admin")
    for conflict, resolution in ((c["conflict"], "dismissed"), ((await conflict_on(item_id))["conflict"], "keep_both")):
        r = await resolve(client, conflict, resolution)
        assert r.json()["effects"] == {"items": [], "sites": [], "drafts": [], "purged_items": []}
    await client.delete("/api/brain/items/%d" % item_id)
    r = await client.delete("/api/brain/items/%d/purge" % item_id)
    assert r.status_code == 200 and r.json()["conflicts_removed"] == 2


async def test_an_import_after_emptying_raises_no_conflict_from_the_old_title(built):
    run = await imp.run(bj.paths(built), ALL)
    async with session_scope() as s:
        imported = await s.scalar(select(MemoryItem).where(MemoryItem.import_run_id == run["run_id"],
                                                           MemoryItem.kind == "knowledge",
                                                           MemoryItem.source_key.is_not(None))
                                  .order_by(MemoryItem.id).limit(1))
        kind, agent, title = imported.kind, imported.agent, imported.title
    await imp.rollback(run["run_id"], discard_reviewed=True, confirm=True)
    ids = await plant()
    copy = await promote(ids["message"], title=title, kind=kind, agent=agent)

    async def conflicts_naming_copy() -> int:
        async with session_scope() as s:
            return int(await s.scalar(select(func.count()).select_from(MemoryConflict).where(
                MemoryConflict.existing_table == "memory_items", MemoryConflict.existing_id == copy)) or 0)

    control = await imp.run(bj.paths(built), ALL)
    assert await conflicts_naming_copy() >= 1  # the same title raises a conflict while the copy has it
    await imp.rollback(control["run_id"], discard_reviewed=True, confirm=True)
    await withhold_service(ids["message"])
    await imp.run(bj.paths(built), ALL)
    assert await conflicts_naming_copy() == 0


# --- Test-copy review decisions -------------------------------------------------------------------------------------


async def test_decisions_export_lists_a_withhold_made_after_promotion(built, tmp_path):
    await imp.run(bj.paths(built), ALL)
    message = await general_message(role="assistant")
    copy = await promote(message.id)
    await withhold_service(message.id)
    await bj.settle(built)  # 0.6.2 plan 4.7: decisions-export refuses until the withhold is finished
    out = tmp_path / "decisions.json"
    result = await imp.decisions_export(str(out))
    exported = json.loads(out.read_text())
    [removal] = [r for r in exported["removals"] if r["source_key"] == message.source_key]
    assert removal["reason"] == WITHHELD_BY_ADMIN and removal["anchor"] == ""  # the admin's own decision
    assert exported["decisions"] == []  # the copy is no import: it is not exported
    flat = out.read_text()
    assert TITLE not in flat and json.dumps(message.content)[1:-1] not in flat
    assert (await item_state(copy))[0].source_key is None  # nothing to key a decision by
    [entry] = await audits("brain.decisions_exported")
    assert entry.detail == result and set(result) == {"exported", "skipped_without_source_key", "edits_not_exported",
                                                      "removals", "releases"}


async def _production(built, tmp_path, *, purge: bool = False) -> dict:
    """The test copy withheld a message (and purged an item, with purge); its decisions file; then production, a
    database that never saw either decision, imported the same and promoted that message."""
    test_copy = await imp.run(bj.paths(built), ALL)
    message = await general_message(role="assistant", run_id=test_copy["run_id"])
    key, text_ = message.source_key, message.content
    await withhold_service(message.id)
    item_key = None
    if purge:
        async with session_scope() as s:
            item = await s.scalar(select(MemoryItem).where(MemoryItem.import_run_id == test_copy["run_id"],
                                                           MemoryItem.status == "pending_review",
                                                           MemoryItem.sensitivity == "general",
                                                           MemoryItem.source_key.is_not(None))
                                  .order_by(MemoryItem.id).limit(1))
            item_key = item.source_key
            await store.delete_item(s, item, actor=ADMIN[0])
            await review.purge(s, item, actor=ADMIN[0])
    settled = await bj.settle(built)  # 0.6.2 plan 4.7: decisions-export refuses until the withhold is finished
    out = tmp_path / "decisions.json"
    await imp.decisions_export(str(out))
    await imp.rollback(settled["run_id"], discard_reviewed=True, confirm=True)  # the newest run first
    await imp.rollback(test_copy["run_id"], discard_reviewed=True, confirm=True)
    async with session_scope() as s:
        await s.execute(BrainTombstone.__table__.delete())
    production = await imp.run(bj.paths(built), ALL)
    message = await message_by_key(key)
    assert not message.withheld
    copy = await promote(message.id, title="Production copy " + TITLE)
    return {"out": str(out), "run": production["run_id"], "key": key, "message": message.id, "copy": copy,
            "text": text_, "item_key": item_key}


async def test_decisions_apply_withholds_and_empties_productions_own_copies(built, tmp_path):
    p = await _production(built, tmp_path)
    result = await imp.decisions_apply(p["out"], p["run"], actor=ADMIN[0], apply=True)
    assert result["removals"] == {"withheld": 1}
    message = await message_by_key(p["key"])
    assert (message.withheld, message.content) == (True, "")
    await assert_emptied(p["copy"])
    [withheld] = await audits("archive.message_withheld")
    assert withheld.detail["via"] == "decisions-apply" and withheld.detail["copies"] == [p["copy"]]
    assert withheld.actor == ADMIN[0]
    assert [e.detail["cause"] for e in await audits("memory.source_withheld")] == ["decisions-apply"]
    assert await nowhere(TITLE) == []


async def test_decisions_apply_dry_run_changes_nothing_and_logs_no_text(built, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    p = await _production(built, tmp_path)
    before = await bj.stored_text(bj.BRAIN_TABLES + ("audit_log", "tasks"))
    dry = await imp.decisions_apply(p["out"], p["run"], actor=ADMIN[0], apply=False)
    assert dry["removals"] == {"withheld": 1}
    assert await bj.stored_text(bj.BRAIN_TABLES + ("audit_log", "tasks")) == before
    assert (await item_state(p["copy"]))[0].title == "Production copy " + TITLE
    assert not [r for r in caplog.records if p["text"] in r.getMessage() or TITLE in r.getMessage()]


async def test_decisions_apply_keeps_the_withhold_tombstone_when_the_withhold_is_refused(built, tmp_path, monkeypatch):
    p = await _production(built, tmp_path)

    async def refused(*args, **kwargs):
        raise store.BrainError("refused for this test")

    monkeypatch.setattr(review, "withhold_message", refused)
    result = await imp.decisions_apply(p["out"], p["run"], actor=ADMIN[0], apply=True)
    assert result["removals"] == {}
    assert result["removals_not_applied"] == {
        "withheld by an admin on the test copy, refused here: refused for this test": [1]}
    async with session_scope() as s:
        assert (await s.get(BrainTombstone, p["key"])).reason == WITHHELD_BY_ADMIN  # kept
    assert not (await message_by_key(p["key"])).withheld
    monkeypatch.undo()
    again = await imp.run(bj.paths(built), ALL)  # the next import withholds it, and empties its copy
    message = await message_by_key(p["key"])
    assert (message.withheld, message.content) == (True, "") and "withheld_by_admin" in message.flags
    # 0.6.2: the import's identity repair withholds it by its key before anything is imported.
    assert again["report"]["repair"]["withheld_by_key"] == 1
    record = (await imp_records(again["run_id"]))[p["key"]]
    assert (record.action, record.reason, record.before) == ("duplicate", "already imported", {})
    await assert_emptied(p["copy"])


async def imp_records(run_id: int) -> dict[str, ImportRecord]:
    async with session_scope() as s:
        return {r.source_key: r for r in (await s.scalars(select(ImportRecord).where(
            ImportRecord.run_id == run_id))).all()}


async def test_a_removal_kept_for_later_imports_blanks_rollback_copies_and_the_next_import_brings_it_back_empty(
        fx, tmp_path):
    first = await imp.run(bj.paths(fx), ALL)
    modify_copy(fx)
    second = await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        record = await s.scalar(select(ImportRecord).where(
            ImportRecord.run_id == second["run_id"], ImportRecord.action == "updated",
            ImportRecord.target_table == "archive_messages", ImportRecord.before["content"].astext != "")
            .order_by(ImportRecord.id).limit(1))
        key, record_id = record.source_key, record.id
    for run in (second, first):
        await imp.rollback(run["run_id"], discard_reviewed=True, confirm=True)
    assert await message_by_key(key) is None
    async with session_scope() as s:
        assert (await s.get(ImportRecord, record_id)).before["content"]  # a rolled-back run's copy keeps its text
    out = tmp_path / "decisions.json"
    out.write_text(json.dumps({"format": "jarvis-brain-decisions", "version": 3, "decisions": [],
                               "removals": [{"source_key": key, "reason": WITHHELD_BY_ADMIN}]}))
    result = await imp.decisions_apply(str(out), first["run_id"], actor=ADMIN[0], apply=True)
    assert result["removals"] == {"kept for later imports": 1}
    async with session_scope() as s:
        before = (await s.get(ImportRecord, record_id)).before
        assert (before["content"], before["withheld"]) == ("", True)
    await imp.run(bj.paths(fx), ALL)
    back = await message_by_key(key)
    assert (back.withheld, back.content, back.tool_calls) == (True, "", []) and "withheld_by_admin" in back.flags


@pytest.mark.parametrize("gone", ["rolled back", "rewound"])
async def test_decisions_apply_empties_a_copy_whose_message_this_database_no_longer_holds(fx, tmp_path, client, gone):
    """The row of a message an admin withheld on the test copy is gone here (its run was rolled back, or a newer
    import deleted it as rewound); a copy promoted from it kept the text. decisions-apply empties it at once."""
    await allow_ai_context()
    first = await imp.run(bj.paths(fx), ALL)
    key = next(m.key for m in source_messages(fx, "20261011_0011") if m.role == "assistant")  # rewound later
    message = await message_by_key(key)
    copy = await promote(message.id)
    await decide(copy, "approve")
    async with session_scope() as s:
        task = quoting_task(item_id=copy)
        s.add(task)
        await s.flush()
        task_id = task.id
    if gone == "rolled back":
        await imp.rollback(first["run_id"], confirm=True)
    else:
        modify_copy(fx)
        await imp.run(bj.paths(fx), ALL)
    assert await message_by_key(message.source_key) is None
    assert (await agent_reads(copy, TITLE))["relevant"] is True  # its message is gone, not withheld
    out = tmp_path / "decisions.json"
    out.write_text(json.dumps({"format": "jarvis-brain-decisions", "version": 3, "decisions": [],
                               "removals": [{"source_key": message.source_key, "reason": WITHHELD_BY_ADMIN}]}))
    result = await imp.decisions_apply(str(out), first["run_id"], actor=ADMIN[0], apply=True)
    assert result["removals"] == {"kept for later imports": 1}
    await assert_emptied(copy)
    [entry] = await audits("memory.source_withheld")
    assert (entry.detail["cause"], entry.detail["item"], entry.detail["message"]) == ("decisions-apply", copy, None)
    assert entry.actor == ADMIN[0]
    async with session_scope() as s:
        data = (await s.get(Task, task_id)).result["data"]
        assert data["answer"][1] == REMOVED and data["memory_notes"] == []
    assert await withheld_counts() == (0, 0)
    assert await agent_reads(copy, TITLE) == NOT_READ
    assert await nowhere(TITLE) == []


async def test_decisions_apply_refuses_a_purge_without_the_sensitive_data_permission_and_leaves_no_tombstone(client,
                                                                                                         tmp_path):
    key = "jarvis:file:knowledge/credit/planted.md"
    async with session_scope() as s:
        item = await store.create_item(s, kind="knowledge", title="Credit " + TITLE, body="Dispute steps.",
                                       actor="importer (cli)", agent="credit_repair", origin="import", source_key=key)
        item_id = item.id
        run = ImportRun(status="completed", created_by="importer (cli)")
        s.add(run)
        await s.flush()
        run_id = run.id
        assert not (await s.scalar(select(User).where(User.email == ADMIN[0]))).can_access_sensitive
    out = tmp_path / "decisions.json"
    out.write_text(json.dumps({"format": "jarvis-brain-decisions", "version": 3, "decisions": [],
                               "removals": [{"source_key": key, "reason": imp.PURGED_BY_ADMIN}]}))
    result = await imp.decisions_apply(str(out), run_id, actor=ADMIN[0], apply=True)
    assert result["removals"] == {}
    assert result["removals_not_applied"] == {
        "purged by an admin on the test copy, refused here: needs the sensitive-data permission": [1]}
    async with session_scope() as s:
        assert await s.get(BrainTombstone, key) is None
        kept = await s.get(MemoryItem, item_id)
        assert kept is not None and kept.deleted_at is None and kept.title == "Credit " + TITLE
    assert await audits("memory.purged") == [] and await audits("memory.deleted") == []


async def test_decisions_apply_refused_by_the_database_keeps_the_withhold_tombstone_and_logs_no_text(
        built, tmp_path, monkeypatch, caplog):
    """The shape of the 0.6.0 defect: the database refuses the copy's change. The withhold is refused as a whole and
    reported without its text; its tombstone stays, so the next import withholds the message and empties the copy."""
    caplog.set_level(logging.DEBUG)
    p = await _production(built, tmp_path)

    async def refused(session, item, *, actor):
        raise IntegrityError("UPDATE memory_items SET title = $1", {},
                             _Refused("new row violates check constraint\nDETAIL: Failing row contains (%s)" % TITLE))

    monkeypatch.setattr(store, "empty_withheld_copy", refused)
    result = await imp.decisions_apply(p["out"], p["run"], actor=ADMIN[0], apply=True)
    assert result["removals"] == {}
    assert result["removals_not_applied"] == {
        "withheld by an admin on the test copy, refused here by the database": [1]}
    async with session_scope() as s:
        assert (await s.get(BrainTombstone, p["key"])).reason == WITHHELD_BY_ADMIN
    message = await message_by_key(p["key"])
    assert not message.withheld and message.content == p["text"]
    assert (await item_state(p["copy"]))[0].title == "Production copy " + TITLE
    assert await audits("archive.message_withheld") == [] and await audits("memory.source_withheld") == []
    async with session_scope() as s:
        flat = json.dumps([e.detail for e in (await s.scalars(select(AuditLog))).all()])
    assert TITLE not in flat and json.dumps(p["text"])[1:-1] not in flat
    assert not [r for r in caplog.records if TITLE in r.getMessage() or p["text"] in r.getMessage()]
    monkeypatch.undo()
    await imp.run(bj.paths(built), ALL)
    message = await message_by_key(p["key"])
    assert (message.withheld, message.content) == (True, "")
    await assert_emptied(p["copy"])


async def test_decisions_apply_twice_reports_already_done(built, tmp_path):
    p = await _production(built, tmp_path)
    assert (await imp.decisions_apply(p["out"], p["run"], actor=ADMIN[0], apply=True))["removals"] == {"withheld": 1}
    again = await imp.decisions_apply(p["out"], p["run"], actor=ADMIN[0], apply=True)
    assert again["removals"] == {"already done": 1} and again["removals_not_applied"] == {}
    assert len(await audits("archive.message_withheld")) == 1


async def test_decisions_apply_is_refused_while_an_import_is_running(built, tmp_path):
    p = await _production(built, tmp_path)
    locks = await imp.LockConnection.open()
    try:
        assert await locks.try_lock(imp.GLOBAL_LOCK)
        with pytest.raises(imp.Refused, match="import, rollback or rescan is running"):
            await imp.decisions_apply(p["out"], p["run"], actor=ADMIN[0], apply=True)
    finally:
        await locks.close()
    assert not (await message_by_key(p["key"])).withheld


async def test_decisions_apply_audits_each_removal_with_ids_and_counts_only(built, tmp_path):
    p = await _production(built, tmp_path, purge=True)
    async with session_scope() as s:
        item = await s.scalar(select(MemoryItem).where(MemoryItem.source_key == p["item_key"]))
        item_id, body = item.id, item.body
    result = await imp.decisions_apply(p["out"], p["run"], actor=ADMIN[0], apply=True)
    assert result["removals"] == {"withheld": 1, "purged": 1}
    [withheld] = await audits("archive.message_withheld")
    assert set(withheld.detail) == {"message", "session", "imported_messages", "withheld_messages", "tombstone",
                                    "copies", "versions_blanked", "rollback_copies_blanked", "answers_scrubbed", "via",
                                    "also_withheld", "held_for_review", "identical_withheld", "keys_tombstoned",
                                    "title_hidden"}
    assert (withheld.detail["message"], withheld.detail["tombstone"]) == (p["message"], True)
    [purged] = await audits("memory.purged")
    assert purged.target_id == str(item_id) and set(purged.detail) == {
        "item", "kind", "agent", "versions_removed", "conflicts_removed", "tombstone", "origin", "message",
        "source_withheld", "answers_scrubbed"}
    assert (purged.detail["origin"], purged.detail["tombstone"]) == ("import", True)
    async with session_scope() as s:
        entries = (await s.scalars(select(AuditLog))).all()
    flat = json.dumps([e.detail for e in entries])
    for planted in (TITLE, json.dumps(p["text"])[1:-1], json.dumps(body)[1:-1]):
        assert planted not in flat


# --- Later live-import consistency ----------------------------------------------------------------------------------


async def test_reimport_of_the_same_and_of_a_changed_source_keeps_the_message_and_copies_empty(fx):
    await imp.run(bj.paths(fx), ALL)
    key = next(m.key for m in source_messages(fx, "20261010_0010") if m.role == "user")  # compacted by modify_copy
    message = await message_by_key(key)
    copy = await promote(message.id)
    await withhold_service(message.id)
    emptied = await assert_emptied(copy)
    same = await imp.run(bj.paths(fx), ALL)
    assert (await imp.verify(same["run_id"], bj.paths(fx)))["passed"]
    modify_copy(fx)
    changed = await imp.run(bj.paths(fx), ALL)
    assert (await imp.verify(changed["run_id"], bj.paths(fx)))["passed"]
    back = await message_by_key(key)
    # 0.6.2 plan 5.2: the changed source is a later backup of a withheld family, whose stored rows are never
    # rewritten (it was "compacted" before the redesign); the row stays withheld and empty.
    assert (back.id, back.withheld, back.content, back.state) == (message.id, True, "", message.state)
    item = await assert_emptied(copy)
    assert item.version == emptied.version  # nothing to empty again
    assert await nowhere(TITLE) == []


async def test_reimport_withholds_again_a_row_an_admin_tombstone_names(built):
    """The race 0.6.0 allowed: a withhold while an import was writing the message back with its text. Here the row
    has its text but the tombstone says an admin withheld it; the next import withholds it, as it does a new row."""
    await imp.run(bj.paths(built), ALL)
    message = await general_message(role="assistant")
    copy = await promote(message.id)
    await decide(copy, "approve")
    async with session_scope() as s:
        s.add(BrainTombstone(source_key=message.source_key, purged_by=ADMIN[0], reason=WITHHELD_BY_ADMIN))
    again = await imp.run(bj.paths(built), ALL)
    back = await message_by_key(message.source_key)
    assert (back.withheld, back.content, back.tool_calls, back.labels) == (True, "", [], [])
    assert back.flags["withheld_by_admin"]["note"] == WITHHELD_BY_ADMIN and back.sensitivity == message.sensitivity
    # 0.6.2: the import's identity repair withholds it by its key before anything is imported, and keeps no copy of
    # its text for a rollback.
    assert again["report"]["repair"]["withheld_by_key"] == 1 and back.source_fp == "w1:live"
    record = (await imp_records(again["run_id"]))[message.source_key]
    assert (record.action, record.reason, record.before) == ("duplicate", "already imported", {})
    await assert_emptied(copy)
    assert [e.detail["cause"] for e in await audits("memory.source_withheld")] == ["repair"]
    assert (await imp.verify(again["run_id"], bj.paths(built)))["passed"]


async def test_resume_after_a_withhold_made_while_the_run_was_interrupted_keeps_it_withheld(built, monkeypatch):
    from .test_brain_import import _paused_run

    monkeypatch.setattr(imp, "BATCH_SESSIONS", 4)
    task, gate = await _paused_run(built, monkeypatch, 5)
    task.cancel()
    with pytest.raises(imp.ImportFailed, match="interrupted"):
        await task
    gate.set()
    imported = await general_message(role="assistant")
    await withhold_service(imported.id)
    later = next(m.key for m in source_messages(built, "20261020_0020") if m.role == "assistant")
    assert await message_by_key(later) is None  # not imported yet
    async with session_scope() as s:  # what decisions-apply leaves for a message not imported yet
        s.add(BrainTombstone(source_key=later, purged_by=ADMIN[0], reason=WITHHELD_BY_ADMIN))
    async with session_scope() as s:
        run_id = await s.scalar(select(ImportRun.id))
    done = await imp.resume(run_id, bj.paths(built))
    assert done["status"] == "completed" and (await imp.verify(run_id, bj.paths(built)))["passed"]
    back = await message_by_key(imported.source_key)
    assert (back.withheld, back.content) == (True, "")
    new = await message_by_key(later)
    assert (new.withheld, new.content, new.tool_calls) == (True, "", []) and "withheld_by_admin" in new.flags


async def test_an_import_that_newly_restricts_a_session_empties_its_copies_and_quotes(fx):
    await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        sess = await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == "20261016_0016"))
        message = await s.scalar(select(ArchiveMessage).where(
            ArchiveMessage.session_id == sess.id, ArchiveMessage.content != "", ArchiveMessage.role == "assistant")
            .order_by(ArchiveMessage.position).limit(1))
        assert sess.sensitivity != "restricted" and not message.withheld
    copy = await promote(message.id)
    async with session_scope() as s:
        task = quoting_task(message_id=message.id, item_id=copy)
        s.add(task)
        await s.flush()
        task_id = task.id
    modify_copy(fx)  # restricted data in that conversation now
    second = await imp.run(bj.paths(fx), ALL)
    await assert_emptied(copy)
    async with session_scope() as s:
        data = (await s.get(Task, task_id)).result["data"]
        assert data["answer"][1:] == [REMOVED, REMOVED] and data["memory_notes"] == []
    assert [e.detail["cause"] for e in await audits("memory.source_withheld")] == ["import"]
    assert (await imp.verify(second["run_id"], bj.paths(fx)))["passed"]
    assert await nowhere(TITLE) == []


async def test_an_import_that_recreates_a_restricted_session_after_a_rollback_empties_its_copies(fx, client):
    """A copy promoted and approved, its run rolled back (the copy keeps its text, and its message_id names a row
    that is gone), then the conversation imported again with restricted data in it: the session is created
    withheld, and that import empties the copy (found by its message_source_key)."""
    await allow_ai_context()
    first = await imp.run(bj.paths(fx), ALL)
    message = await message_of("20261016_0016")  # restricted data is added there by modify_copy
    copy = await promote(message.id)
    await decide(copy, "approve")
    await imp.rollback(first["run_id"], confirm=True)
    assert (await agent_reads(copy, TITLE))["relevant"] is True  # its message is gone, not withheld
    async with session_scope() as s:  # an answer and a reference note that quote the copy
        task = quoting_task(item_id=copy)
        s.add(task)
        await s.flush()
        task_id = task.id
    modify_copy(fx)
    second = await imp.run(bj.paths(fx), ALL)
    back = await message_by_key(message.source_key)
    assert back.id != message.id and (back.withheld, back.content) == (True, "")
    await assert_emptied(copy)
    async with session_scope() as s:  # every row that import wrote was new: the copy's quotes went all the same
        data = (await s.get(Task, task_id)).result["data"]
        assert data["answer"][1] == REMOVED and data["memory_notes"] == []
    [entry] = await audits("memory.source_withheld")
    assert (entry.detail["cause"], entry.detail["item"], entry.detail["message"]) == ("import", copy, back.id)
    assert await agent_reads(copy, TITLE) == NOT_READ
    assert await listed_by(client, copy) == {"viewer": False, "operator": False, "admin": True}
    assert (await imp.verify(second["run_id"], bj.paths(fx)))["passed"]
    assert await withheld_counts() == (0, 0)
    assert await nowhere(TITLE) == []


async def test_an_import_that_adds_a_row_to_an_already_restricted_session_empties_that_rows_copies(built):
    """An earlier import found restricted data in the conversation (every row withheld); the promoted row was
    deleted since (rewound). The next import writes it back without text and empties its copy."""
    await allow_ai_context()
    await imp.run(bj.paths(built), ALL)
    message = await general_message(role="assistant")
    copy = await promote(message.id)
    await decide(copy, "approve")
    async with session_scope() as s:
        await s.execute(update(ArchiveMessage).where(ArchiveMessage.session_id == message.session_id).values(
            content="", tool_calls=[], labels=[], withheld=True, sensitivity="restricted"))
        await s.execute(delete(ArchiveMessage).where(ArchiveMessage.id == message.id))
        sess = await s.get(ArchiveSession, message.session_id)
        sess.flags = {**sess.flags, "withheld_by_import": True}
        await recompute_session(s, sess.id)
        assert sess.sensitivity == "restricted"
    # 0.6.2: an item promoted from a restricted conversation is kept from agents before any import empties it
    # (policy.withheld_source(), finding 2's safety net).
    assert await agent_reads(copy, TITLE) == NOT_READ
    await imp.run(bj.paths(built), ALL)
    back = await message_by_key(message.source_key)
    assert back.id != message.id and (back.withheld, back.content) == (True, "")
    await assert_emptied(copy)
    assert [e.detail["cause"] for e in await audits("memory.source_withheld")] == ["import"]
    assert await agent_reads(copy, TITLE) == NOT_READ
    assert await nowhere(TITLE) == []


async def test_an_import_empties_copies_of_a_new_row_an_admin_tombstone_names(built):
    """The row of a message an admin withheld is gone (rolled back) and comes back with the next import, without its
    text: the copy promoted from its earlier row is emptied by that import."""
    first = await imp.run(bj.paths(built), ALL)
    message = await general_message(role="assistant")
    copy = await promote(message.id)
    await decide(copy, "approve")
    await imp.rollback(first["run_id"], confirm=True)
    assert await message_by_key(message.source_key) is None
    assert (await item_state(copy))[0].body == message.content  # the copy kept its text
    async with session_scope() as s:
        s.add(BrainTombstone(source_key=message.source_key, purged_by=ADMIN[0], reason=WITHHELD_BY_ADMIN))
    again = await imp.run(bj.paths(built), ALL)
    back = await message_by_key(message.source_key)
    assert back.id != message.id
    assert (back.withheld, back.content, back.tool_calls) == (True, "", []) and "withheld_by_admin" in back.flags
    assert (await imp_records(again["run_id"]))[message.source_key].action == "withheld"
    await assert_emptied(copy)
    assert [e.detail["cause"] for e in await audits("memory.source_withheld")] == ["import"]
    assert (await imp.verify(again["run_id"], bj.paths(built)))["passed"]
    assert await nowhere(TITLE) == []


async def tool_row(source_session_id: str = "20261014_0014") -> ArchiveMessage:
    """The tool row whose text modify_copy changes ("ls output" becomes "ls output, changed")."""
    async with session_scope() as s:
        sess = await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == source_session_id))
        return await s.scalar(select(ArchiveMessage).where(ArchiveMessage.session_id == sess.id,
                                                           ArchiveMessage.role == "tool"))


async def test_a_changed_source_reimport_of_a_row_an_admin_tombstone_names_withholds_it_and_its_copies(fx):
    await imp.run(bj.paths(fx), ALL)
    tool = await tool_row()
    copy = await promote(tool.id)
    await decide(copy, "approve")
    async with session_scope() as s:  # an admin withheld it (a refused decisions-apply withhold): the row kept its text
        s.add(BrainTombstone(source_key=tool.source_key, purged_by=ADMIN[0], reason=WITHHELD_BY_ADMIN))
    modify_copy(fx)
    second = await imp.run(bj.paths(fx), ALL)
    back = await message_by_key(tool.source_key)
    assert (back.withheld, back.content, back.tool_calls) == (True, "", []) and "withheld_by_admin" in back.flags
    # 0.6.2: the import's identity repair withholds it by its key first; a withheld row keeps no fingerprint of its
    # text, so the changed text is not even compared (it stays withheld whatever it became).
    assert second["report"]["repair"]["withheld_by_key"] == 1
    record = (await imp_records(second["run_id"]))[tool.source_key]
    assert (record.action, record.reason, record.before) == ("duplicate", "already imported", {})
    await assert_emptied(copy)
    assert [e.detail["cause"] for e in await audits("memory.source_withheld")] == ["repair"]
    assert await nowhere("ls output", allow=("archive_messages.content", "archive_messages.search")) == []
    assert (await imp.verify(second["run_id"], bj.paths(fx)))["passed"]


async def test_a_first_import_of_restricted_conversations_never_takes_the_quote_lock(fx, monkeypatch):
    """The rows a batch inserts cannot have been quoted by anyone: removing what a withheld message left elsewhere
    happens once per batch and never looks for quotes of them, so a first import neither reads every task nor holds
    the quote lock (recalls would be queued again for most of the run)."""
    calls: list[dict] = []
    locked: list[bool] = []
    original_withhold, original_lock = imp.withhold_copies, quotes_module.lock_quotes

    async def withhold_copies(session, **kw):
        calls.append(kw)
        return await original_withhold(session, **kw)

    async def lock_quotes(session, **kw):
        locked.append(kw.get("shared", False))
        return await original_lock(session, **kw)

    monkeypatch.setattr(imp, "withhold_copies", withhold_copies)
    monkeypatch.setattr(quotes_module, "lock_quotes", lock_quotes)
    await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        withheld = set((await s.scalars(select(ArchiveMessage.id).where(ArchiveMessage.withheld.is_(True)))).all())
    assert withheld and calls, "the fixture has restricted conversations"
    assert {i for c in calls for i in c["message_ids"]} == withheld
    assert all(set(c["message_ids"]) <= set(c["fresh_ids"]) for c in calls)
    assert locked == []


async def test_an_import_empties_the_copies_of_its_newly_restricted_conversations_once_per_batch(fx, monkeypatch):
    """Two conversations restricted by the same batch: one pass over their copies and quotes, at the batch's end."""
    await imp.run(bj.paths(fx), ALL)
    names = ("20261016_0016", bj.S_CARRIED)
    messages = [await message_of(name) for name in names]
    copies = [await promote(m.id, title="%s %s" % (n, TITLE)) for n, m in zip(names, messages)]
    async with session_scope() as s:
        tasks = [quoting_task(message_id=m.id, item_id=c) for m, c in zip(messages, copies)]
        s.add_all(tasks)
        await s.flush()
        task_ids = [t.id for t in tasks]
    con = bj.sqlite(fx["db"])
    try:
        for i, name in enumerate(names):
            con.execute("INSERT INTO messages (session_id, role, content, timestamp, active, compacted, message_uid) "
                        "VALUES (?, 'user', 'Her SSN is 219-09-9999', ?, 1, 0, ?)",
                        (name, 1793600010 + i, "ssn-%d" % i))
        con.commit()
    finally:
        con.close()
    calls: list[dict] = []
    original = imp.withhold_copies

    async def withhold_copies(session, **kw):
        calls.append(kw)
        return await original(session, **kw)

    monkeypatch.setattr(imp, "withhold_copies", withhold_copies)
    monkeypatch.setattr(imp, "BATCH_SESSIONS", 1000)  # every conversation in one batch
    monkeypatch.setattr(imp, "BATCH_MESSAGES", 1_000_000)
    await imp.run(bj.paths(fx), ALL)
    assert len(calls) == 1 and {m.id for m in messages} <= set(calls[0]["message_ids"])
    for copy in copies:
        await assert_emptied(copy)
    async with session_scope() as s:
        for task_id in task_ids:
            data = (await s.get(Task, task_id)).result["data"]
            assert data["answer"][1:] == [REMOVED, REMOVED] and data["memory_notes"] == []
    assert await nowhere(MSG) == [] and await nowhere(TITLE) == []


async def test_an_import_that_deletes_a_rewound_row_removes_its_quotes(fx):
    """A row the source no longer holds (rewound) is deleted by the import, and the answers quoting it lose those
    lines then, as for any removed source."""
    await imp.run(bj.paths(fx), ALL)
    key = next(m.key for m in source_messages(fx, "20261011_0011") if m.role == "assistant")  # rewound later
    message = await message_by_key(key)
    async with session_scope() as s:
        task = quoting_task(message_id=message.id)
        s.add(task)
        await s.flush()
        task_id = task.id
    modify_copy(fx)
    await imp.run(bj.paths(fx), ALL)
    assert await message_by_key(key) is None
    async with session_scope() as s:
        assert (await s.get(Task, task_id)).result["data"]["answer"][1] == REMOVED


async def test_removing_quotes_finds_the_tasks_through_the_index(client):
    """scrub_quotes() reads only the tasks that can quote the removed sources (tasks_quotes_idx), not every task."""
    async with session_scope() as s:
        others = [quoting_task(message_id=900_000 + i, item_id=800_000 + i) for i in range(50)]
        quoting = [quoting_task(message_id=5), quoting_task(item_id=7)]
        s.add_all(others + quoting)
        await s.flush()
        wanted = {t.id for t in quoting}
    loaded: set[int] = set()

    def on_load(task, context):
        loaded.add(task.id)

    event.listen(Task, "load", on_load)
    try:
        async with session_scope() as s:
            assert await quotes_module.scrub_quotes(s, message_ids=[5], item_ids=[7]) == 2
    finally:
        event.remove(Task, "load", on_load)
    assert loaded == wanted
    async with session_scope() as s:
        await s.execute(text("SET LOCAL enable_seqscan = off"))
        query = select(Task.id).where(or_(*next(quotes_module._quoting({5}, {7})))).compile(
            dialect=postgresql.dialect())
        values = {k: "'%s'" % json.dumps(v) for k, v in query.params.items()}  # the JSON looked for, as sent
        plan = "\n".join((await (await s.connection()).exec_driver_sql("EXPLAIN " + str(query) % values)).scalars())
    assert "tasks_quotes_idx" in plan, plan


async def test_a_withhold_removes_the_quotes_of_an_older_row_of_the_message(client):
    """An answer quoted the message under the row id an earlier import gave it; a rollback, or a newer import that
    found it rewound, deleted that row (0.6.0 left such quotes) and a new import stored it again under a new id.
    Withholding it removes the old row's quotes too: the rows its import_records name."""
    ids = await plant()
    async with session_scope() as s:
        run = ImportRun(status="rolled_back", created_by="importer (cli)")
        s.add(run)
        await s.flush()
        s.add(ImportRecord(run_id=run.id, source_key=ids["key"], record_type="message", action="imported",
                           target_table="archive_messages", target_id=900_001))
        task = quoting_task(message_id=900_001)
        s.add(task)
        await s.flush()
        task_id = task.id
    r = await withhold(client, ids["message"])
    assert r.status_code == 200 and r.json()["answers_scrubbed"] == 1
    async with session_scope() as s:
        assert (await s.get(Task, task_id)).result["data"]["answer"][1] == REMOVED
    assert await nowhere(MSG) == []


@pytest.mark.parametrize("action", ["updated", "duplicate"])
async def test_a_withhold_of_a_message_with_no_row_removes_the_quotes_of_its_old_rows_only(client, action):
    """decisions-apply's withhold of a message this database holds no row of (an import deleted it as rewound, and
    the answer quoting it was kept): its old row's quotes go. A 'duplicate' record names the row of another message
    (the one it duplicates), whose quotes stay."""
    ids, other = await plant("w1"), await plant("w2", content="Another conversation entirely.")
    async with session_scope() as s:
        run = ImportRun(status="completed", created_by="importer (cli)")
        s.add(run)
        await s.flush()
        row = await s.get(ArchiveMessage, ids["message"])
        if action == "updated":  # deleted as rewound by that run
            s.add(ImportRecord(run_id=run.id, source_key=ids["key"], record_type="message", action="updated",
                               reason="rewound since an earlier import", target_table="archive_messages",
                               target_id=row.id, before={"deleted_row": {"id": row.id, "content": row.content,
                                                                         "session_id": row.session_id}}))
            quoted = row.id
        else:  # never stored: a duplicate of the other conversation's message
            s.add(ImportRecord(run_id=run.id, source_key=ids["key"], record_type="message", action="duplicate",
                               target_table="archive_messages", target_id=other["message"]))
            quoted = other["message"]
        task = quoting_task(message_id=quoted)
        s.add(task)
        await s.flush()
        task_id = task.id
        await s.execute(delete(ArchiveMessage).where(ArchiveMessage.id == ids["message"]))
    async with session_scope() as s:
        done = await archive.withhold_copies(s, source_keys=[ids["key"]], actor=ADMIN[0], cause="decisions-apply")
    async with session_scope() as s:
        line = (await s.get(Task, task_id)).result["data"]["answer"][1]
    if action == "updated":
        assert done.answers_scrubbed == 1 and line == REMOVED
        assert await nowhere(MSG) == []
    else:
        assert done.answers_scrubbed == 0 and MSG in line


async def test_a_rescan_that_withholds_a_session_empties_its_copies(built):
    await imp.run(bj.paths(built), ALL)
    message = await general_message(role="assistant")
    copy = await promote(message.id)
    async with session_scope() as s:
        other = await s.scalar(select(ArchiveMessage).where(ArchiveMessage.session_id == message.session_id,
                                                            ArchiveMessage.id != message.id,
                                                            ArchiveMessage.content != "").limit(1))
        # Restricted data an earlier detector missed, in another message of the same conversation.
        await s.execute(update(ArchiveMessage).where(ArchiveMessage.id == other.id)
                        .values(content=other.content + " Her SSN is 219-09-9999"))
    dry = await imp.rescan(apply=False, actor=ADMIN[0])
    assert (dry["copies_withheld"], dry["copy_versions_blanked"]) == (1, 1)
    assert (await item_state(copy))[0].title == "Copy " + TITLE  # nothing was changed
    done = await imp.rescan(apply=True, actor=ADMIN[0])
    assert (done["sessions_withheld"], done["copies_withheld"], done["copy_versions_blanked"]) == (1, 1, 1)
    await assert_emptied(copy)
    [entry] = await audits("memory.source_withheld")
    assert (entry.detail["cause"], entry.detail["item"], entry.detail["message"]) == ("rescan", copy, message.id)
    [rescanned] = await audits("brain.rescanned")
    assert (rescanned.detail["copies_withheld"], rescanned.detail["copy_versions_blanked"]) == (1, 1)
    again = await imp.rescan(apply=True, actor=ADMIN[0])
    assert again["copies_withheld"] == 0


async def test_rescan_refresh_removes_notes_and_quotes_of_emptied_copies(client):
    ids = await plant()
    copy = await promote(ids["message"])
    assert (await withhold(client, ids["message"])).status_code == 200
    async with session_scope() as s:  # a task that finished while the withhold ran kept its note and its quote
        task = quoting_task(item_id=copy)
        s.add(task)
        await s.flush()
        task_id = task.id
    result = await imp.rescan(apply=True, actor=ADMIN[0])
    assert result["answers_changed"] == 1
    async with session_scope() as s:
        data = (await s.get(Task, task_id)).result["data"]
        assert data["answer"][1] == REMOVED and data["memory_notes"] == []
    assert await nowhere(TITLE) == []


async def test_the_startup_check_empties_what_a_0_6_0_database_left(client):
    """What 0.6.0 left: (a) a copy flagged source_withheld with its text (quarantined at promotion, so 0.6.0's
    withhold succeeded); (b) an approved copy whose message an import or rescan withheld; (c) a copy whose message
    key an admin withheld (tombstone) while the row has gone; (d) a 0.6.0 copy (no message_source_key) whose row was
    rolled back and came back withheld under a new id; (e) a 0.6.1 copy whose row was rolled back and came back
    withheld under a new id (found by its message_source_key only). A copy of a live message is left alone, and so
    is a 0.6.0 copy whose source_ref names a withheld row while its message_id names another, live row (n)."""
    a, b, d, e, n, live = [await plant(k) for k in ("wa", "wb", "wd", "we", "wn", "wl")]
    copies = {name: await promote(ids["message"], title="%s %s" % (name, TITLE))
              for name, ids in (("a", a), ("b", b), ("d", d), ("e", e), ("live", live))}
    await decide(copies["b"], "approve")
    async with session_scope() as s:
        c_item = await store.create_item(
            s, kind="project", title="c " + TITLE, body="A copy of " + MSG, actor=OPERATOR, origin="promoted",
            source_ref=archive.promoted_ref("wc", 1),
            flags={"message_id": 999_003, "archive_session_id": 999_004, "message_source_key": "jarvis:message:wc:1"})
        copies["c"] = c_item.id
        s.add(BrainTombstone(source_key="jarvis:message:wc:1", purged_by=ADMIN[0], reason=WITHHELD_BY_ADMIN))
        n_item = await store.create_item(
            s, kind="project", title="n " + TITLE, body="A copy of " + MSG, actor=OPERATOR, origin="promoted",
            source_ref=archive.promoted_ref("wn", 1),
            flags={"message_id": a["first"], "archive_session_id": n["session"]})
        copies["n"] = n_item.id
        withheld = {"content": "", "tool_calls": [], "labels": [], "withheld": True}
        for ids in (a, b, d, e, n):  # withheld as 0.6.0's import, rescan or withhold left them: copies untouched
            await s.execute(update(ArchiveMessage).where(ArchiveMessage.id == ids["message"]).values(**withheld))
        item_a = await s.get(MemoryItem, copies["a"])
        item_a.flags = {**item_a.flags, "source_withheld": True}
        item_a.status = "quarantined"
        item_d = await s.get(MemoryItem, copies["d"])
        item_d.flags = {"message_id": 999_005, "archive_session_id": d["session"]}  # promoted by 0.6.0, row gone
        item_e = await s.get(MemoryItem, copies["e"])
        item_e.flags = {**item_e.flags, "message_id": 999_006}  # the row its run created was rolled back
        assert item_e.flags["message_source_key"] == e["key"]
    assert await withheld_counts() == (5, 5)
    assert (await imp.status())["withheld_copies_with_text"] == 5
    async with session_scope() as s:
        found = {item.id: message for item, message in await archive.withheld_copies(s)}
    assert found[copies["e"]].id == e["message"]
    await check_withheld_copies()
    for name in "abcde":
        await assert_emptied(copies[name])
    for name in ("live", "n"):
        item, _ = await item_state(copies[name])
        assert item.title == "%s %s" % (name, TITLE) and "source_withheld" not in item.flags, name
    assert await withheld_counts() == (0, 0)
    entries = await audits("memory.source_withheld")
    assert sorted(e.detail["item"] for e in entries) == sorted(copies[n] for n in "abcde")
    assert {e.detail["cause"] for e in entries} == {"startup-check"}
    assert {e.detail["item"]: e.detail["message"] for e in entries}[copies["e"]] == e["message"]
    await check_withheld_copies()  # a second start changes nothing
    assert len(await audits("memory.source_withheld")) == 5
    status = await imp.status()
    assert (status["migration_0007_brain_withhold"], status["withheld_copies_with_text"]) == (True, 0)


async def test_the_startup_check_is_skipped_while_an_import_holds_the_lock_and_never_stops_the_start(
        client, monkeypatch, caplog):
    """It never waits for the lock: the next start, or rescan --yes, empties what it skipped."""
    caplog.set_level(logging.DEBUG)
    ids = await plant()
    copy = await promote(ids["message"])
    async with session_scope() as s:
        await s.execute(update(ArchiveMessage).where(ArchiveMessage.id == ids["message"]).values(
            withheld=True, content="", labels=[]))
    locks = await imp.LockConnection.open()
    try:
        assert await locks.try_lock(imp.GLOBAL_LOCK)
        await check_withheld_copies()
    finally:
        await locks.close()
    assert (await item_state(copy))[0].title == "Copy " + TITLE and "skipped" in caplog.text

    async def broken(session, *, actor, cause="startup-check"):
        raise IntegrityError("UPDATE memory_items", {}, _Refused("Failing row contains (%s)" % TITLE))

    monkeypatch.setattr(archive, "sweep_withheld_copies", broken)
    await check_withheld_copies()  # logged by its class, never raised
    assert "withheld-copies check failed: IntegrityError" in caplog.text and TITLE not in caplog.text


async def test_the_startup_check_quarantines_again_an_emptied_copy_0_6_0_approved_or_reopened(client):
    """After going back to 0.6.0 (which let an admin approve or reopen an emptied copy) and up to 0.6.1 again: an
    emptied copy is never approved or waiting for review. A rejected or archived one stays so."""
    copies = {}
    for status in ("approved", "pending_review", "rejected"):
        ids = await plant("w" + status[:3])
        copies[status] = await promote(ids["message"])
        assert (await withhold(client, ids["message"])).status_code == 200
    async with session_scope() as s:  # as 0.6.0's review left them: status changed, no text
        for status, copy in copies.items():
            await s.execute(update(MemoryItem).where(MemoryItem.id == copy).values(status=status))
    before = len(await audits("memory.source_withheld"))
    await check_withheld_copies()
    for status, copy in copies.items():
        item, versions = await item_state(copy)
        assert (item.title, item.body, item.tags) == (store.WITHHELD_COPY_TITLE, "", [])
        assert all(store.blank_version(v) for v in versions)
        if status == "rejected":
            assert (item.status, item.version) == ("rejected", 2)
        else:
            assert (item.status, item.version, versions[0].change, versions[0].status) == (
                "quarantined", 3, "quarantined", "quarantined")
    entries = (await audits("memory.source_withheld"))[before:]
    assert sorted((e.detail["item"], e.detail["status_before"], e.detail["cause"]) for e in entries) == sorted(
        [(copies[status], status, "startup-check") for status in ("approved", "pending_review")])
    await check_withheld_copies()  # a second start changes nothing
    assert len(await audits("memory.source_withheld")) == before + 2


async def test_the_startup_check_never_waits_for_a_task_writing_a_history_answer(client, caplog):
    """A start (api or worker) while a recall holds the quote lock: skipped at once, and the next start does it."""
    caplog.set_level(logging.DEBUG)
    ids = await plant()
    copy = await promote(ids["message"])
    async with session_scope() as s:  # as 0.6.0 left it: the message withheld, its copy full
        await s.execute(update(ArchiveMessage).where(ArchiveMessage.id == ids["message"]).values(
            withheld=True, content="", labels=[]))
    recall = sessionmaker()()
    try:
        await lock_quotes(recall, shared=True)  # as the worker holds it for supervisor.recall
        await asyncio.wait_for(check_withheld_copies(), 5)
    finally:
        await recall.rollback()
        await recall.close()
    assert "withheld-copies check skipped: a task is writing a history answer" in caplog.text
    assert (await item_state(copy))[0].title == "Copy " + TITLE
    await check_withheld_copies()
    await assert_emptied(copy)


async def test_a_rescan_holds_the_quote_lock_only_for_its_last_pass(client, monkeypatch):
    """Removing quotes takes the quote lock until the rescan commits: it comes after the long re-clean of conflicts,
    drafts and rollback copies, so recalls are not queued again for all of it."""
    ids = await plant()
    copy = await promote(ids["message"])
    async with session_scope() as s:  # as 0.6.0 left it: the rescan's sweep empties the copy and removes quotes
        await s.execute(update(ArchiveMessage).where(ArchiveMessage.id == ids["message"]).values(
            withheld=True, content="", labels=[]))
    free: list[bool] = []
    recleaned = imp._rescan_json

    async def watched(s, counts, apply):
        async with session_scope() as other:
            free.append(await lock_quotes(other, shared=True, wait=False))
        await recleaned(s, counts, apply)

    monkeypatch.setattr(imp, "_rescan_json", watched)
    for apply in (False, True):
        done = await imp.rescan(apply=apply, actor=ADMIN[0])
        assert done["copies_withheld"] == 1
    assert free == [True, True]
    await assert_emptied(copy)


async def test_the_startup_check_blanks_rollback_copies_0_6_0_left_under_an_older_row_id(client):
    """0.6.0's withhold blanked a message's rollback copies by its current row id only: those kept under an older id
    of the same message (a rollback and a new import gave it a new one) kept the text, whoever withheld it."""
    by_admin, by_import = await plant("w1"), await plant("w2")
    other = await plant("w3", content="Another conversation entirely.")
    row = {"position": 1, "role": "assistant", "content": "An older copy: " + MSG, "tool_calls": [],
           "labels": ["accomplishment"], "withheld": False, "sensitivity": "general", "truncated": False}
    async with session_scope() as s:
        run = ImportRun(status="rolled_back", created_by="importer (cli)")
        s.add(run)
        await s.flush()
        s.add_all([
            ImportRecord(run_id=run.id, source_key=by_admin["key"], record_type="message", action="updated",
                         reason="changed since an earlier import", target_table="archive_messages", target_id=900001,
                         before={**row, "session_id": by_admin["session"], "source_key": by_admin["key"]}),
            ImportRecord(run_id=run.id, source_key=by_import["key"], record_type="message", action="updated",
                         reason="rewound since an earlier import", target_table="archive_messages", target_id=900002,
                         before={"deleted_row": {**row, "id": 900002, "session_id": by_import["session"],
                                                 "source_key": by_import["key"]}}),
            ImportRecord(run_id=run.id, source_key=other["key"], record_type="message", action="updated",
                         reason="changed since an earlier import", target_table="archive_messages", target_id=900003,
                         before={**row, "content": "unrelated text"}),
        ])
        withheld = {"content": "", "tool_calls": [], "labels": [], "withheld": True}
        for ids in (by_admin, by_import):  # as 0.6.0 left them: the current row withheld, older copies untouched
            await s.execute(update(ArchiveMessage).where(ArchiveMessage.id == ids["message"]).values(**withheld))
        s.add(BrainTombstone(source_key=by_admin["key"], purged_by=ADMIN[0], reason=WITHHELD_BY_ADMIN))
    assert (await imp.status())["withheld_rollback_copies_with_text"] == 2
    await check_withheld_copies()
    async with session_scope() as s:
        befores = {r.target_id: r.before for r in (await s.scalars(select(ImportRecord))).all()}
    assert (befores[900001]["content"], befores[900001]["withheld"]) == ("", True)
    assert (befores[900002]["deleted_row"]["content"], befores[900002]["deleted_row"]["withheld"]) == ("", True)
    assert befores[900003]["content"] == "unrelated text"
    assert (await imp.status())["withheld_rollback_copies_with_text"] == 0
    await check_withheld_copies()  # a second start changes nothing
    async with session_scope() as s:
        assert {r.target_id: r.before for r in (await s.scalars(select(ImportRecord))).all()} == befores
    assert await nowhere(MSG) == []


async def test_the_startup_check_removes_quotes_tasks_kept_of_withheld_messages_and_emptied_copies(client):
    """A task that quoted a message or a copy and committed while the withhold ran, before 0.6.1's quote lock."""
    ids = await plant()
    copy = await promote(ids["message"])
    assert (await withhold(client, ids["message"])).status_code == 200
    async with session_scope() as s:
        task = quoting_task(message_id=ids["message"], item_id=copy)
        s.add(task)
        await s.flush()
        task_id = task.id
    await check_withheld_copies()
    async with session_scope() as s:
        data = (await s.get(Task, task_id)).result["data"]
        assert data["answer"][1:] == [REMOVED, REMOVED] and data["memory_notes"] == []
    assert await nowhere(MSG) == [] and await nowhere(TITLE) == []


async def test_bootstrap_runs_the_withheld_copies_check_after_migrations(client, monkeypatch, settings):
    """Every start of the api and the worker (app/bootstrap.py): what 0.6.0 left is emptied once the migrations
    ran, which is what jarvis-import.ps1's production check waits for."""
    b, c = await plant("wb"), await plant("wc")
    copies = [await promote(ids["message"], title="%s %s" % (k, TITLE)) for k, ids in (("b", b), ("c", c))]
    await decide(copies[0], "approve")
    async with session_scope() as s:
        await s.execute(update(ArchiveMessage).where(ArchiveMessage.id == b["message"]).values(
            content="", tool_calls=[], labels=[], withheld=True))  # an import or a rescan of 0.6.0 withheld it
        await s.execute(delete(ArchiveMessage).where(ArchiveMessage.id == c["message"]))
        s.add(BrainTombstone(source_key=c["key"], purged_by=ADMIN[0], reason=WITHHELD_BY_ADMIN))
    assert (await imp.status())["withheld_copies_with_text"] == 2
    order: list[str] = []
    check = startup.check_withheld_copies

    async def migrations():
        order.append("migrations")
        return []

    async def checked():
        order.append("check")
        await check()

    async def no_admin(settings):
        return False

    monkeypatch.setattr(startup, "run_migrations", migrations)
    monkeypatch.setattr(startup, "check_withheld_copies", checked)
    monkeypatch.setattr(startup, "ensure_admin", no_admin)
    await startup.bootstrap(settings)
    assert order == ["migrations", "check"]
    for copy in copies:
        await assert_emptied(copy)
    assert {e.detail["cause"] for e in await audits("memory.source_withheld")} == {"startup-check"}
    assert (await imp.status())["withheld_copies_with_text"] == 0


# --- Rollback behaviour ---------------------------------------------------------------------------------------------


async def test_rollback_reinserts_a_tombstoned_message_without_text(fx):
    await imp.run(bj.paths(fx), ALL)
    key = next(m.key for m in source_messages(fx, "20261011_0011") if m.role == "assistant")  # rewound later
    message = await message_by_key(key)
    copy = await promote(message.id)
    modify_copy(fx)
    second = await imp.run(bj.paths(fx), ALL)
    assert await message_by_key(key) is None  # deleted (rewound); its rollback copy keeps the row
    async with session_scope() as s:
        s.add(BrainTombstone(source_key=key, purged_by=ADMIN[0], reason=WITHHELD_BY_ADMIN))
    result = await imp.rollback(second["run_id"], confirm=True)
    assert result["withheld_reinserted"] == 1
    back = await message_by_key(key)
    assert (back.id, back.withheld, back.content, back.tool_calls, back.labels) == (message.id, True, "", [], [])
    assert back.flags["withheld_by_admin"] == {"note": WITHHELD_BY_ADMIN} and back.sensitivity == message.sensitivity
    await assert_emptied(copy)
    assert [e.detail["cause"] for e in await audits("memory.source_withheld")] == ["rollback"]
    [entry] = await audits("brain.import_rolled_back")
    assert entry.detail["withheld_reinserted"] == 1


async def test_rollback_of_an_update_brings_a_row_an_admin_tombstone_names_back_without_text(fx):
    """The row is not withheld (a refused decisions-apply withhold kept only its tombstone): the rollback puts the
    earlier version back without its text, and empties the copy."""
    await imp.run(bj.paths(fx), ALL)
    tool = await tool_row()
    copy = await promote(tool.id)
    await decide(copy, "approve")
    modify_copy(fx)
    second = await imp.run(bj.paths(fx), ALL)
    record = (await imp_records(second["run_id"]))[tool.source_key]
    assert record.before["content"] == "ls output"
    async with session_scope() as s:
        s.add(BrainTombstone(source_key=tool.source_key, purged_by=ADMIN[0], reason=WITHHELD_BY_ADMIN))
    current = await message_by_key(tool.source_key)
    assert not current.withheld
    result = await imp.rollback(second["run_id"], confirm=True)
    assert result["withheld_reinserted"] == 1
    back = await message_by_key(tool.source_key)
    assert (back.id, back.withheld, back.content, back.tool_calls, back.labels) == (tool.id, True, "", [], [])
    assert back.flags["withheld_by_admin"] == {"note": WITHHELD_BY_ADMIN} and back.sensitivity == current.sensitivity
    await assert_emptied(copy)
    assert [e.detail["cause"] for e in await audits("memory.source_withheld")] == ["rollback"]
    async with session_scope() as s:
        assert (await s.get(ImportRecord, record.id)).before["content"] == ""
    assert await nowhere("ls output", allow=("archive_messages.content", "archive_messages.search")) == []
    [entry] = await audits("brain.import_rolled_back")
    assert entry.detail["withheld_reinserted"] == 1


async def test_rollback_after_a_withhold_keeps_message_rollback_copies_and_copies_empty(fx):
    await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        sess = await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == "20261014_0014"))
        tool = await s.scalar(select(ArchiveMessage).where(ArchiveMessage.session_id == sess.id,
                                                           ArchiveMessage.role == "tool"))
    copy = await promote(tool.id)
    modify_copy(fx)  # changes that tool row's text
    second = await imp.run(bj.paths(fx), ALL)
    record = (await imp_records(second["run_id"]))[tool.source_key]
    assert record.before["content"] == "ls output"
    done = await withhold_service(tool.id)
    assert done.rollback_copies_blanked == 1 and [c["item"] for c in done.copies] == [copy]
    await imp.rollback(second["run_id"], confirm=True)
    back = await message_by_key(tool.source_key)
    assert (back.withheld, back.content) == (True, "")
    async with session_scope() as s:
        assert (await s.get(ImportRecord, record.id)).before["content"] == ""
    await assert_emptied(copy)
    assert await nowhere("ls output", allow=("archive_messages.content", "archive_messages.search")) == []


async def test_rollback_marks_emptied_copies_source_removed_and_keeps_them_empty(built):
    first = await imp.run(bj.paths(built), ALL)
    message = await general_message(role="assistant")
    copy = await promote(message.id)
    await withhold_service(message.id)
    source_ref = (await item_state(copy))[0].source_ref
    result = await imp.rollback(first["run_id"], confirm=True)
    assert result["promoted_items_marked"] == 1
    assert await message_by_key(message.source_key) is None
    item = await assert_emptied(copy)
    assert item.source_ref == source_ref + archive.SOURCE_REMOVED
    assert await nowhere(TITLE) == []


# --- Agent access restrictions ----------------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["withheld", "back", "gone"])
async def test_no_agent_path_reads_an_emptied_copy_even_if_forced_back_to_approved(client, source):
    """With its message no longer withheld (back) or gone, and no tombstone, only the source_withheld flag keeps
    agents away from it."""
    await allow_ai_context()
    ids = await plant()
    copy = await promote(ids["message"])
    await decide(copy, "approve")
    assert (await withhold(client, ids["message"])).status_code == 200
    async with session_scope() as s:  # forced by hand: approved again, with text again
        await s.execute(update(MemoryItem).where(MemoryItem.id == copy).values(
            status="approved", title="Forced " + TITLE, body="Forced back: " + MSG))
        if source != "withheld":  # first: while its stone is there, the database guard keeps the row without text
            await s.execute(delete(BrainTombstone))
        if source == "back":
            await s.execute(update(ArchiveMessage).where(ArchiveMessage.id == ids["message"]).values(
                withheld=False, content="Forced back."))
        elif source == "gone":
            await s.execute(delete(ArchiveMessage).where(ArchiveMessage.id == ids["message"]))
    assert await agent_reads(copy, MSG) == NOT_READ
    if source != "withheld":  # without the flag the agents would read it: the flag alone kept them away
        async with session_scope() as s:
            item = await s.get(MemoryItem, copy)
            item.flags = {k: v for k, v in item.flags.items() if k != "source_withheld"}
        assert (await agent_reads(copy, MSG))["relevant"] is True


@pytest.mark.parametrize("state", ["newer row", "tombstone", "legacy", "legacy, source removed"])
async def test_no_agent_or_person_reads_a_copy_whose_message_is_withheld_elsewhere(client, state):
    """Before any sweep empties it: the message is withheld under a newer row id than the copy names (found by
    flags.message_source_key); an admin withheld it and no row of it is left (tombstone); or the copy was promoted by
    0.6.0 (no message_source_key), its message_id names no row and its source_ref names a withheld row. Nobody reads
    it, an admin included (0.6.2: 0.6.1 listed it, with its text, to admins)."""
    await allow_ai_context()
    ids = await plant()
    copy = await promote(ids["message"])
    await decide(copy, "approve")
    assert (await agent_reads(copy, MSG))["relevant"] is True
    await as_(client, "admin+")
    assert (await client.get("/api/brain/export")).json()["count"] == 1
    async with session_scope() as s:
        item = await s.get(MemoryItem, copy)
        if state == "tombstone":
            await s.execute(delete(ArchiveMessage).where(ArchiveMessage.id == ids["message"]))
            s.add(BrainTombstone(source_key=ids["key"], purged_by=ADMIN[0], reason=WITHHELD_BY_ADMIN))
        else:
            await s.execute(update(ArchiveMessage).where(ArchiveMessage.id == ids["message"]).values(
                content="", tool_calls=[], labels=[], withheld=True))
        if state == "newer row":
            item.flags = {**item.flags, "message_id": 999_001}
        elif state.startswith("legacy"):
            item.flags = {"message_id": 999_002, "archive_session_id": ids["session"]}
            if state.endswith("removed"):
                item.source_ref += archive.SOURCE_REMOVED
    assert await agent_reads(copy, MSG) == NOT_READ
    assert await listed_by(client, copy) == {"viewer": False, "operator": False, "admin": False}
    await as_(client, "admin+")
    assert (await client.get("/api/brain/export")).json()["count"] == 0
    for who in ("operator", "admin", "admin+"):
        await as_(client, who)
        assert (await client.get("/api/brain/items/%d" % copy)).status_code == 404, who
        assert (await client.get("/api/brain/search", params={"q": MSG})).json()["memory"]["total"] == 0, who
    assert await withheld_counts() == (1, 1)  # the start-up check, the status count and the pre-check find it
    await check_withheld_copies()
    await assert_emptied(copy)


@pytest.mark.parametrize("status", ["approved", "rejected", "archived", "pending_review"])
async def test_a_flagged_copy_0_6_0_left_with_its_text_is_shown_to_nobody_until_it_is_emptied(client, status):
    """0.6.0's withhold of a message whose copy was quarantined at promotion flagged the copy source_withheld and kept
    its text, and 0.6.0 let an admin approve, reject, archive or reopen it. Until the start-up check empties it, the
    flag alone never shows it to anyone, an admin included (0.6.2): only an emptied copy is shown by its flag."""
    ids = await plant(content="The %s key [REDACTED:api_key] was rotated." % MSG, redactions={"api_key": 1})
    copy = await promote(ids["message"])
    async with session_scope() as s:  # as 0.6.0 left it
        await s.execute(update(ArchiveMessage).where(ArchiveMessage.id == ids["message"]).values(
            withheld=True, content="", tool_calls=[], labels=[]))
        item = await s.get(MemoryItem, copy)
        item.flags = {**item.flags, "source_withheld": True}
        item.status = status
    assert await listed_by(client, copy) == {"viewer": False, "operator": False, "admin": False}
    for who in ("viewer", "operator", "admin", "admin+"):
        await as_(client, who)
        assert (await client.get("/api/brain/items/%d" % copy)).status_code == 404, who
        assert (await client.get("/api/brain/search", params={"q": MSG})).json()["memory"]["total"] == 0, who
    await check_withheld_copies()
    await assert_emptied(copy)
    await decide(copy, "reject")  # emptied, then rejected: an operator sees the empty placeholder by its flag
    assert await listed_by(client, copy) == {"viewer": False, "operator": True, "admin": True}
    dashboard = (ROOT / "dashboard" / "src" / "pages" / "Brain.tsx").read_text(encoding="utf-8")
    assert 'WITHHELD_COPY_TITLE = "%s"' % policy.WITHHELD_COPY_TITLE in dashboard  # how the page tells it emptied


async def test_no_agent_path_reads_an_approved_copy_whose_message_is_withheld(client):
    """What 0.6.0 left until the start-up check: a message withheld by an import or a rescan, its copy approved and
    full. The agents' own rule keeps them away from it."""
    await allow_ai_context()
    ids = await plant()
    copy = await promote(ids["message"])
    await decide(copy, "approve")
    assert (await agent_reads(copy, MSG))["relevant"] is True
    async with session_scope() as s:
        await s.execute(update(ArchiveMessage).where(ArchiveMessage.id == ids["message"]).values(
            withheld=True, content="", labels=[]))
    assert await agent_reads(copy, MSG) == {"relevant": False, "prompt": False, "notes": False, "recall": False,
                                            "counted": False}
    assert (await item_state(copy))[0].body.count(MSG) == 1  # its text is still there: only the sweep empties it


async def test_a_withhold_while_a_history_answer_is_being_written_removes_its_quotes(client, make_worker,
                                                                                   monkeypatch):
    """The answer read the message and its copy before the withhold, and commits after it: the withhold waits for it
    (the quote lock, held from before the recall's first read), then removes its lines, its reference note and the
    request's summary of it."""
    ids = await plant()
    copy = await promote(ids["message"])
    await decide(copy, "approve")
    reached, release = asyncio.Event(), asyncio.Event()
    original = Worker._add_memory_notes

    async def paused(self, session, task_id, memory, params, result):
        if "answer" in (result.data or {}):  # the recall, after its reads and before its reference notes
            reached.set()
            await release.wait()
        return await original(self, session, task_id, memory, params, result)

    monkeypatch.setattr(Worker, "_add_memory_notes", paused)
    await as_(client, "admin")
    r = await client.post("/api/supervisor/requests",
                          json={"message": "What did JARVIS accomplish with the certificate?"})
    assert r.status_code == 202, r.text
    request_id = r.json()["request"]["id"]
    working = asyncio.ensure_future(make_worker().run_until_idle())
    try:
        await asyncio.wait_for(reached.wait(), 30)
        withholding = asyncio.ensure_future(client.post("/api/brain/messages/%d/withhold" % ids["message"]))
        await lock_waits()
        assert not withholding.done()
    finally:
        release.set()
    await working
    assert (await withholding).status_code == 200
    async with session_scope() as s:
        recall = await s.scalar(select(Task).where(Task.capability == "recall"))
        request = await s.get(SupervisorRequest, request_id)
        assert recall.status == "succeeded" and REMOVED in recall.result["data"]["answer"]
        assert request.summary and REMOVED in request.summary
    await assert_emptied(copy)
    assert await nowhere(MSG) == [] and await nowhere(TITLE) == []


class SlowModel(LLMProvider):
    """An AI model whose summary takes until the test releases it (an AI provider's HTTP call can take minutes)."""
    name, available = "slow-model", True

    def __init__(self) -> None:
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def text(self, *, purpose, system, prompt, max_tokens=16000):
        if purpose != "summarize":
            raise LLMUnavailable("not this one")
        self.entered.set()
        await self.release.wait()
        return "summary from the model"


async def test_a_model_summary_never_delays_a_withhold_a_purge_or_a_recall(client, make_worker, settings):
    """A summary written by an AI model quotes no brain text (only a local-only answer is copied, and that never goes
    to a model): it holds no quote lock, so a removal and a new history answer never wait for the model's call."""
    ids = await plant()
    copy = await promote(ids["message"])
    await decide(copy, "approve")
    other = await promote((await plant("w2"))["message"], title="Other copy")
    async with session_scope() as s:  # a finished request whose summary the model writes
        req = SupervisorRequest(message="check the sites", status="running", planner="rule_based")
        s.add(req)
        await s.flush()
        s.add(Task(agent="monitoring", capability="check_site", risk="read", status="succeeded", request_id=req.id,
                   summary="site up", result={"data": {}}))
        await task_service.create_task(s, settings, agent="supervisor", capability="summarize_request",
                                       params={"request_id": req.id}, source="supervisor", created_by="supervisor",
                                       request_id=req.id, priority=50)
        summarized = req.id
    model = SlowModel()
    working = asyncio.ensure_future(make_worker(llm=model).run_until_idle())
    try:
        await asyncio.wait_for(model.entered.wait(), 30)
        assert (await asyncio.wait_for(withhold(client, ids["message"]), 15)).status_code == 200
        await as_(client, "admin")
        assert (await client.delete("/api/brain/items/%d" % other)).status_code == 200
        assert (await asyncio.wait_for(client.delete("/api/brain/items/%d/purge" % other), 15)).status_code == 200
        r = await client.post("/api/supervisor/requests", json={"message": "What did JARVIS accomplish last month?"})
        assert r.status_code == 202, r.text
        await asyncio.wait_for(make_worker().run_until_idle(), 30)  # a second worker, while the model still writes
        async with session_scope() as s:
            recall = await s.scalar(select(Task).where(Task.capability == "recall"))
            assert recall.status == "succeeded" and recall.attempts == 1
        assert not working.done()
    finally:
        model.release.set()
        await working
    async with session_scope() as s:
        request = await s.get(SupervisorRequest, summarized)
        assert (request.status, request.summarizer, request.summary) == ("completed", "slow-model",
                                                                        "summary from the model")
    await assert_emptied(copy)


async def test_a_summary_that_copies_a_history_answer_waits_for_a_removal_in_progress(client, make_worker, settings):
    """A request summary copies a recall's local-only answer as it is: it takes the quote lock first, so a removal
    that has not committed yet is waited for, and the summary copies the answer without the removed line."""
    ids = await plant()
    async with session_scope() as s:
        req = SupervisorRequest(message="What did JARVIS accomplish?", status="running", planner="rule_based")
        s.add(req)
        await s.flush()
        s.add(quoting_task(message_id=ids["message"], request_id=req.id))
        await task_service.create_task(s, settings, agent="supervisor", capability="summarize_request",
                                       params={"request_id": req.id}, source="supervisor", created_by="supervisor",
                                       request_id=req.id, priority=50)
        request_id = req.id
    removal = sessionmaker()()
    try:
        await lock_quotes(removal)  # a withhold that has not committed yet
        working = asyncio.ensure_future(make_worker().run_until_idle())
        await lock_waits()
        assert not working.done()
        assert await quotes_module.scrub_quotes(removal, message_ids=[ids["message"]]) == 1
        await removal.commit()
    finally:
        await removal.rollback()
        await removal.close()
    await working
    async with session_scope() as s:
        request = await s.get(SupervisorRequest, request_id)
        assert request.status == "completed" and REMOVED in request.summary and MSG not in request.summary


async def test_a_recall_is_queued_again_while_a_removal_holds_the_quote_lock(client, make_worker):
    """It never waits for the lock in a worker slot: back in the queue, not an attempt, and it runs once the removal
    committed."""
    await as_(client, "admin")
    r = await client.post("/api/supervisor/requests", json={"message": "What did JARVIS accomplish last month?"})
    assert r.status_code == 202, r.text
    request_id = r.json()["request"]["id"]
    removal = sessionmaker()()
    try:
        await lock_quotes(removal)  # a withhold, purge or redaction that has not committed yet
        await asyncio.wait_for(make_worker().run_until_idle(), 20)
        async with session_scope() as s:
            recall = await s.scalar(select(Task).where(Task.capability == "recall"))
            assert (recall.status, recall.attempts, recall.started_at) == ("queued", 0, None)
            assert recall.run_after > datetime.now(UTC) + timedelta(seconds=10)
            logs = (await s.scalars(select(TaskLog.message).where(TaskLog.task_id == recall.id))).all()
            assert any("a JARVIS Brain removal is running" in m for m in logs)
    finally:
        await removal.rollback()
        await removal.close()
    async with session_scope() as s:
        await s.execute(update(Task).where(Task.capability == "recall").values(run_after=func.now()))
    await make_worker().run_until_idle()
    async with session_scope() as s:
        recall = await s.scalar(select(Task).where(Task.capability == "recall"))
        request = await s.get(SupervisorRequest, request_id)
        assert (recall.status, recall.attempts, request.status) == ("succeeded", 1, "completed")


async def test_recall_never_quotes_a_withheld_message_or_its_copies(client):
    ids = await plant()
    copy = await promote(ids["message"])
    await decide(copy, "approve")
    question = "what did JARVIS accomplish with " + MSG
    before = await ask(question)
    sources = [s for s in before.data["sources"] if s]
    assert {"message": ids["message"]} in sources and {"item": copy} in sources
    assert MSG in json.dumps(before.data)
    assert (await withhold(client, ids["message"])).status_code == 200
    after = await ask(question)
    flat = json.dumps(after.data)
    assert MSG not in flat and TITLE not in flat
    assert {"message": ids["message"]} not in after.data.get("sources", []) and {"item": copy} not in after.data.get(
        "sources", [])


async def test_every_role_after_withhold_and_purge(client, service_headers):
    ids = await plant()
    copy = await promote(ids["message"])
    await decide(copy, "approve")
    assert (await withhold(client, ids["message"])).status_code == 200

    async def seen(who: str) -> str:
        await as_(client, who)
        pages = ["/api/brain/items?include_deleted=%s" % ("true" if who.startswith("admin") else "false"),
                 "/api/brain/search?q=" + MSG, "/api/brain/search?q=" + TITLE, "/api/brain/overview",
                 "/api/brain/agents"]
        if who != "viewer":
            pages += ["/api/brain/sessions", "/api/brain/sessions/%d" % ids["session"], "/api/brain/conflicts"]
        out = []
        for page in pages:
            r = await client.get(page)
            assert r.status_code == 200, (who, page, r.status_code)
            out.append(r.text)
        return "\n".join(out)

    for who in ("viewer", "operator", "operator+", "admin", "admin+"):
        text_ = await seen(who)
        assert MSG not in text_ and TITLE not in text_, who
    await as_(client, "admin+")
    listed = (await client.get("/api/brain/items")).json()["items"]
    assert [(i["id"], i["title"]) for i in listed] == [(copy, store.WITHHELD_COPY_TITLE)]  # the placeholder only
    exported = await client.get("/api/brain/export?include_customer=true")
    assert exported.json()["count"] == 0
    client.cookies.clear()
    for page in ("/api/brain/items", "/api/brain/items/%d" % copy, "/api/brain/messages/%d/promoted" % ids["message"]):
        assert (await client.get(page, headers=service_headers)).status_code == 403
    await as_(client, "admin")
    await client.delete("/api/brain/items/%d" % copy)
    assert (await client.delete("/api/brain/items/%d/purge" % copy)).status_code == 200
    await as_(client, "admin+")
    assert (await client.get("/api/brain/items?include_deleted=true")).json()["total"] == 0


# --- Audit and logs -------------------------------------------------------------------------------------------------


async def test_no_audit_entry_or_log_line_holds_planted_text_across_the_lifecycle(client, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    first, second, third = await plant("w1"), await plant("w2"), await plant("w3")
    await as_(client, "operator")
    r = await client.post("/api/brain/messages/%d/promote" % first["message"],
                          json={"kind": "project", "title": "Copy " + TITLE})
    copy = r.json()["item"]["id"]
    await as_(client, "admin+")
    await client.patch("/api/brain/items/%d" % copy, json={"body": "Edited " + EDIT, "tags": [TAG], "note": NOTE})
    await client.post("/api/brain/items/%d/review" % copy, json={"decision": "approve", "note": "ok " + NOTE})
    await client.post("/api/brain/items/%d/versions/1/redact" % copy)
    assert (await client.post("/api/brain/messages/%d/withhold" % first["message"])).status_code == 200
    await client.delete("/api/brain/items/%d" % copy)
    assert (await client.delete("/api/brain/items/%d/purge" % copy)).status_code == 200
    other = await promote(second["message"])  # 0.6.0 left this one: withheld message, full copy
    async with session_scope() as s:
        await s.execute(update(ArchiveMessage).where(ArchiveMessage.id == second["message"]).values(
            withheld=True, content=""))
        run = ImportRun(status="completed", created_by="importer (cli)")
        s.add(run)
        await s.flush()
        run_id = run.id
    await check_withheld_copies()
    out = tmp_path / "decisions.json"
    await imp.decisions_export(str(out))
    out.write_text(json.dumps({"format": "jarvis-brain-decisions", "version": 3, "decisions": [],
                               "removals": [{"source_key": third["key"], "reason": WITHHELD_BY_ADMIN}]}))
    assert (await imp.decisions_apply(str(out), run_id, actor=ADMIN[0], apply=True))["removals"] == {"withheld": 1}
    await imp.rescan(apply=True, actor=ADMIN[0])
    await assert_emptied(other)
    async with session_scope() as s:
        entries = (await s.scalars(select(AuditLog))).all()
    flat = json.dumps([{"action": e.action, "target": e.target_id, "detail": e.detail} for e in entries])
    assert {"memory.promoted", "memory.edited", "memory.approve", "memory.version_redacted",
            "archive.message_withheld", "memory.source_withheld", "memory.purged", "brain.decisions_exported",
            "brain.decisions_applied", "brain.rescanned"} <= {e.action for e in entries}
    logged = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("httpx"))
    for marker in (MSG, TITLE, TAG, NOTE, EDIT):
        assert marker not in flat and marker.lower() not in logged.lower(), marker
        assert await nowhere(marker) == [], marker


async def test_audit_details_have_exactly_the_specified_keys(client, tmp_path):
    ids = await plant()
    copy = await promote(ids["message"])
    await as_(client, "admin")
    await client.patch("/api/brain/items/%d" % copy, json={"body": "Edited text."})
    await client.post("/api/brain/items/%d/versions/1/redact" % copy)
    assert (await withhold(client, ids["message"])).status_code == 200
    await as_(client, "admin")
    await client.delete("/api/brain/items/%d" % copy)
    await client.delete("/api/brain/items/%d/purge" % copy)
    await imp.decisions_export(str(tmp_path / "decisions.json"))
    await imp.rescan(apply=True, actor=ADMIN[0])
    expected = {
        "archive.message_withheld": {"message", "session", "imported_messages", "withheld_messages", "tombstone",
                                     "copies", "versions_blanked", "rollback_copies_blanked", "answers_scrubbed",
                                     "via", "also_withheld", "held_for_review", "identical_withheld",
                                     "keys_tombstoned", "title_hidden", "left_readable"},  # 0.6.2 plan 4.5
        "memory.source_withheld": {"item", "kind", "agent", "status_before", "status", "version", "deleted",
                                   "versions_blanked", "message", "session", "cause"},
        "memory.purged": {"item", "kind", "agent", "versions_removed", "conflicts_removed", "tombstone", "origin",
                          "message", "source_withheld", "answers_scrubbed"},
        "memory.version_redacted": {"item", "version", "kind", "answers_scrubbed"},
        "brain.decisions_exported": {"exported", "skipped_without_source_key", "edits_not_exported", "removals",
                                     "releases"},
    }
    for action, keys in expected.items():
        [entry] = await audits(action)
        assert set(entry.detail) == keys, action
    [withheld] = await audits("archive.message_withheld")
    assert withheld.detail["copies"] == [copy] and withheld.detail["via"] == "api"
    [emptied] = await audits("memory.source_withheld")
    # promoted (1), edited (2), version 1 redacted already: one version to blank, and the emptied one (3)
    assert emptied.detail == {"item": copy, "kind": "project", "agent": "", "status_before": "pending_review",
                              "status": "quarantined", "version": 3, "deleted": False, "versions_blanked": 1,
                              "message": ids["message"], "session": ids["session"], "cause": "admin"}
    [rescanned] = await audits("brain.rescanned")
    assert {"copies_withheld", "copy_versions_blanked"} <= set(rescanned.detail)


async def test_a_failed_task_records_the_database_error_class_only(admin, gate_agent, make_worker, monkeypatch,
                                                                   caplog):
    caplog.set_level(logging.DEBUG)

    async def refused(ctx, capability, params):
        raise IntegrityError("INSERT INTO memory_items (title) VALUES ($1)", {},
                             _Refused("new row violates check constraint\nDETAIL: Failing row contains (%s)" % MSG))

    monkeypatch.setattr(gate_agent, "run", refused)
    r = await admin.post("/api/agents/gate_test/tasks", json={"capability": "explode", "params": {}})
    await make_worker().run_until_idle()
    detail = (await admin.get("/api/tasks/%d" % r.json()["task"]["id"])).json()
    assert detail["task"]["status"] == "failed"
    assert detail["task"]["error"] == "IntegrityError (_Refused, SQLSTATE 23514)"
    assert [e["message"] for e in detail["logs"]] == ["IntegrityError (_Refused, SQLSTATE 23514)"]
    assert await nowhere(MSG, caplog=caplog) == []


# --- 0.6.2: findings 2 and 3 (a restricted conversation), legacy copies of rowless messages, the pre-check -----------
# (spec 19.8; findings F3/F4 and F8 of the 0.6.1 review, with the attacker's and the skeptic's own probes.)

LATER = 1796000000.0  # after every fixture session
SSN_TEXT = "His SSN is 219-09-9999"  # synthetic restricted value


def _conversation(fx: dict[str, str], sid: str, mark: str, started: float) -> list[int]:
    bj.add_session(fx["db"], sid, started=started)
    return [bj.add_row(fx["db"], sid, "user", "How did the backup go?", started + 1),
            bj.add_row(fx["db"], sid, "assistant", "The %s backup finished at noon." % mark, started + 2),
            bj.add_row(fx["db"], sid, "user", "Great.", started + 3)]


async def _rows_of(sid: str) -> list[ArchiveMessage]:
    async with session_scope() as s:
        sess = await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == sid))
        return list((await s.scalars(select(ArchiveMessage).where(ArchiveMessage.session_id == sess.id)
                                     .order_by(ArchiveMessage.position, ArchiveMessage.id))).all())


async def _session_of(sid: str) -> ArchiveSession:
    async with session_scope() as s:
        return await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == sid))


async def _promoted_backup_copy(fx: dict[str, str], sid: str, mark: str, started: float) -> tuple[list[int], int]:
    ids = _conversation(fx, sid, mark, started)
    await allow_ai_context()
    await imp.run(bj.paths(fx), ALL)
    message = next(r for r in await _rows_of(sid) if mark in r.content)
    copy = await promote(message.id, title="Backup " + mark)
    await decide(copy, "approve")
    return ids, copy


async def _emptied_for_everyone(client, copy: int, mark: str) -> None:
    """The copy is emptied and quarantined, no agent path reads it, only an admin lists it (as a placeholder), and
    the mark is nowhere."""
    await assert_emptied(copy)
    assert await agent_reads(copy, mark) == NOT_READ
    assert await listed_by(client, copy) == {"viewer": False, "operator": False, "admin": True}
    assert await nowhere(mark) == []


@pytest.mark.parametrize("order", ["K2A same import", "K2B rewind first", "K2C restrict first"])
async def test_finding_2_copy_of_a_rewound_row_of_a_restricted_conversation_is_emptied(fx, client, order):
    """F3 (the skeptic's K2A-K2C): the row a copy was promoted from is rewound in JARVIS, and the conversation is
    restricted (restricted data appears) in the same import, a later one, or an earlier one. Whatever the order, the
    copy is emptied and no agent reads it."""
    mark = "zqktwo%s62" % order[2].lower()
    sid = "k2_%s" % order[2].lower()
    ids, copy = await _promoted_backup_copy(fx, sid, mark, LATER + 6000)

    def rewind() -> None:
        bj.change(fx["db"], "UPDATE messages SET active = 0, compacted = 0 WHERE id = ?", ids[1])

    def restrict() -> None:
        bj.add_row(fx["db"], sid, "user", SSN_TEXT, LATER + 6050)

    if order.startswith("K2A"):
        rewind()
        restrict()
    elif order.startswith("K2B"):
        rewind()
        await imp.run(bj.paths(fx), ALL)  # rewound only: the conversation is still general, the copy stays
        assert (await agent_reads(copy, mark))["relevant"] is True
        restrict()
    else:
        restrict()
        await imp.run(bj.paths(fx), ALL)
        rewind()
    last = await imp.run(bj.paths(fx), ALL)
    assert (await _session_of(sid)).sensitivity == "restricted"
    await _emptied_for_everyone(client, copy, mark)
    assert (await imp.verify(last["run_id"], bj.paths(fx)))["passed"]


async def test_finding_2_attacker_probe_c(fx, client):
    """The attacker's probe C: one import rewinds the promoted assistant turn and finds restricted data."""
    mark = "rewoundwombatqc62"
    sid = "20261117_0303"
    ids, copy = await _promoted_backup_copy(fx, sid, mark, LATER + 2000)
    bj.change(fx["db"], "UPDATE messages SET active = 0, compacted = 0 WHERE id = ?", ids[1])
    bj.add_row(fx["db"], sid, "user", SSN_TEXT, LATER + 2050)
    second = await imp.run(bj.paths(fx), ALL)
    assert (await _session_of(sid)).sensitivity == "restricted"
    assert (await imp.verify(second["run_id"], bj.paths(fx)))["passed"]
    await _emptied_for_everyone(client, copy, mark)


@pytest.mark.parametrize("later", [False, True], ids=["K3A same import", "K3B deleted earlier"])
async def test_finding_3_a_row_the_source_no_longer_holds_keeps_no_text_in_a_restricted_conversation(fx, client,
                                                                                                    later):
    """F4 (K3A, K3B): a stored row JARVIS no longer holds, in a conversation an import restricts, keeps no text: not
    even an admin with the sensitive-data permission reads it, and a rescan has nothing left to do."""
    mark = "skgoneotter%s62" % ("b" if later else "a")
    sid = "k3_%s" % ("b" if later else "a")
    ids = _conversation(fx, sid, mark, LATER + 9000)
    await imp.run(bj.paths(fx), ALL)
    bj.change(fx["db"], "DELETE FROM messages WHERE id = ?", ids[1])
    if later:
        await imp.run(bj.paths(fx), ALL)
    bj.add_row(fx["db"], sid, "user", SSN_TEXT, LATER + 9050)
    last = await imp.run(bj.paths(fx), ALL)
    sess = await _session_of(sid)
    assert sess.sensitivity == "restricted" and sess.imported_messages == 0
    assert all(r.withheld and r.content == "" for r in await _rows_of(sid))
    for who in ("operator", "admin", "admin+"):
        await as_(client, who)
        r = await client.get("/api/brain/sessions/%d" % sess.id)
        assert mark not in r.text, who
        assert mark not in (await client.get("/api/brain/search", params={"q": mark, "scope": "archive"})).text
    async with session_scope() as s:
        hits, _ = await store.AgentMemory(s, "supervisor").history(mark)
    assert not hits
    assert (await imp.verify(last["run_id"], bj.paths(fx)))["passed"]
    assert await nowhere(mark) == []
    out = await imp.rescan(apply=True, actor=ADMIN[0])
    assert not out.get("sessions_withheld") and await nowhere(mark) == []


async def test_finding_3_attacker_probe_d(fx, client):
    """The attacker's probe D: the promoted row is deleted from the backup and the conversation restricted in one
    import: the row keeps no text, the copy is emptied, admins and admins+ read nothing."""
    mark = "missingwombatqd62"
    sid = "20261117_0304"
    ids, copy = await _promoted_backup_copy(fx, sid, mark, LATER + 3000)
    bj.change(fx["db"], "DELETE FROM messages WHERE id = ?", ids[1])
    bj.add_row(fx["db"], sid, "user", SSN_TEXT, LATER + 3050)
    second = await imp.run(bj.paths(fx), ALL)
    assert (await imp.verify(second["run_id"], bj.paths(fx)))["passed"]
    sess = await _session_of(sid)
    assert (sess.sensitivity, sess.imported_messages) == ("restricted", 0)
    for who in ("admin", "admin+"):
        await as_(client, who)
        assert mark not in (await client.get("/api/brain/sessions/%d" % sess.id)).text
        assert mark not in (await client.get("/api/brain/search", params={"q": mark})).text
    await _emptied_for_everyone(client, copy, mark)


# Legacy copies (0.6.0's promote wrote flags.message_id and archive_session_id, no message_source_key) of a message
# withheld while this database holds no row of it (F8).

LEGACY_SID = "20261011_0011"  # the conversation modify_copy rewinds (its first assistant row goes inactive)
L_BODY, L_TITLE, L_TAG, L_NOTE, L_EDIT = "zqcfbody62", "zqcftitle62", "zqcftag62", "zqcfnote62", "zqcfedit62"


def _plant_legacy_body(fx: dict[str, str]) -> None:
    con = bj.sqlite(fx["db"])
    try:
        mid = con.execute("SELECT min(id) FROM messages WHERE session_id = ? AND role = 'assistant'",
                          (LEGACY_SID,)).fetchone()[0]
        con.execute("UPDATE messages SET content = content || ' (cert %s)' WHERE id = ?" % L_BODY, (mid,))
        con.commit()
    finally:
        con.close()


async def _legacy_copy(message_id: int, *, title: str = "Cert renewal " + L_TITLE, edited: bool = True) -> int:
    """What 0.6.0's review.promote_message wrote (5ccdf21), then an admin's approval with a note, an edit and a
    second approval."""
    async with session_scope() as s:
        message = await s.get(ArchiveMessage, message_id)
        sess = await s.get(ArchiveSession, message.session_id)
        item = await store.create_item(
            s, kind="project", title=title, body=message.content, actor=OPERATOR, agent="", origin="promoted",
            flags={"message_id": message.id, "archive_session_id": sess.id}, sensitivity="general",
            source_system="jarvis", source_ref=archive.promoted_ref(sess.source_session_id, message.position),
            source_at=message.at, import_run_id=None, note="promoted from an archived JARVIS message", tags=[L_TAG])
        await store.review_item(s, item, decision="approve", actor=ADMIN[0], note="ok " + L_NOTE)
        if edited:
            await store.edit_item(s, item, actor=ADMIN[0], body=message.content + " " + L_EDIT)
            await store.review_item(s, item, decision="approve", actor=ADMIN[0])
        return item.id


async def _legacy_gone(fx: dict[str, str], gone: str) -> tuple[dict, str, int]:
    """The legacy copy of the planted assistant row, then that row gone: its run rolled back, or rewound by a newer
    import. Returns (the first run, the row's key, the copy)."""
    await allow_ai_context()
    _plant_legacy_body(fx)
    first = await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        message = await s.scalar(select(ArchiveMessage).where(ArchiveMessage.content.contains(L_BODY)))
    assert message is not None and not message.withheld
    copy = await _legacy_copy(message.id)
    if gone == "rolled back":
        await imp.rollback(first["run_id"], confirm=True)
    else:
        modify_copy(fx)
        await imp.run(bj.paths(fx), ALL)
    assert await message_by_key(message.source_key) is None
    assert (await agent_reads(copy, L_TITLE))["relevant"] is True  # its row is gone, not withheld: still read
    return first, message.source_key, copy


def _removal_file(tmp_path, key: str) -> str:
    out = tmp_path / "decisions.json"
    out.write_text(json.dumps({"format": "jarvis-brain-decisions", "version": 3, "decisions": [],
                               "removals": [{"source_key": key, "reason": WITHHELD_BY_ADMIN}]}))
    return str(out)


async def _legacy_clean_everywhere(client, copy: int) -> None:
    await _emptied_for_everyone(client, copy, L_TITLE)
    for mark in (L_BODY, L_TAG, L_NOTE, L_EDIT):
        assert await nowhere(mark) == [], mark
    for who in ("viewer", "operator"):
        await as_(client, who)
        assert (await client.get("/api/brain/items", params={"q": L_BODY})).json()["total"] == 0
        assert L_BODY not in (await client.get("/api/brain/search", params={"q": L_BODY})).text
        assert (await client.get("/api/brain/items/%d" % copy)).status_code == 404
    await as_(client, "admin+")
    assert L_BODY not in (await client.get("/api/brain/export?include_customer=true")).text
    assert await withheld_counts() == (0, 0)


@pytest.mark.parametrize("gone", ["rolled back", "rewound"])
async def test_p06_legacy_copy_of_a_rowless_withheld_message_is_emptied_by_decisions_apply(fx, tmp_path, client,
                                                                                           gone):
    """P06 and the skeptic's C1: decisions-apply withholds a message this database holds no row of ("kept for later
    imports"); the 0.6.0 copy of its old row is found through the import records and emptied at once."""
    first, key, copy = await _legacy_gone(fx, gone)
    result = await imp.decisions_apply(_removal_file(tmp_path, key), first["run_id"], actor=ADMIN[0], apply=True)
    assert result["removals"] == {"kept for later imports": 1}
    await _legacy_clean_everywhere(client, copy)
    if gone == "rolled back":  # the same source again: the row comes back withheld, the copy stays empty
        await imp.run(bj.paths(fx), ALL)
        back = await message_by_key(key)
        assert back is not None and back.withheld and back.content == ""
        await assert_emptied(copy)


async def test_p06b_the_start_up_check_and_rescan_empty_a_legacy_copy_whose_key_0_6_0_tombstoned(fx, client):
    """P06b and the skeptic's C2: what 0.6.0 left (its run rolled back, its decisions-apply wrote only the withhold
    tombstone); 0.6.2 starts, then rescans."""
    _, key, copy = await _legacy_gone(fx, "rolled back")
    async with session_scope() as s:
        s.add(BrainTombstone(source_key=key, purged_by=ADMIN[0], reason=WITHHELD_BY_ADMIN))
    assert await check_withheld_copies() is True
    await assert_emptied(copy)
    await imp.rescan(apply=True, actor=ADMIN[0])
    await _legacy_clean_everywhere(client, copy)


async def test_p06c_before_the_start_up_check_a_legacy_copy_is_counted_and_kept_from_everyone(fx, client):
    """P06c: the same state, before any check: status and the pre-check count it, no agent and no person reads it, an
    admin included (policy.withheld_source(): the import records name the withheld key its gone row held)."""
    _, key, copy = await _legacy_gone(fx, "rolled back")
    async with session_scope() as s:
        s.add(BrainTombstone(source_key=key, purged_by=ADMIN[0], reason=WITHHELD_BY_ADMIN))
    assert await withheld_counts() == (1, 1)
    assert (await imp.status())["withheld_copies_with_text"] == 1
    assert await agent_reads(copy, L_TITLE) == NOT_READ
    assert await listed_by(client, copy) == {"viewer": False, "operator": False, "admin": False}
    await check_withheld_copies()
    await _legacy_clean_everywhere(client, copy)


async def test_confirm_c3_control_a_rowful_withhold_empties_the_same_legacy_copy(fx, tmp_path, client):
    """The skeptic's C3 (control): while the row is there, decisions-apply withholds it and empties the same copy."""
    await allow_ai_context()
    _plant_legacy_body(fx)
    first = await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        message = await s.scalar(select(ArchiveMessage).where(ArchiveMessage.content.contains(L_BODY)))
    copy = await _legacy_copy(message.id)
    result = await imp.decisions_apply(_removal_file(tmp_path, message.source_key), first["run_id"], actor=ADMIN[0],
                                       apply=True)
    assert result["removals"] == {"withheld": 1}
    await _legacy_clean_everywhere(client, copy)


async def test_precheck_sql_parity(client):
    """scripts/brain-precheck.ps1 counts exactly what the start-up check finds (archive.withheld_copies), branch by
    branch: a withheld row by id, by a newer row's key, by an admin's tombstone with no row, a 0.6.0 copy by its
    source_ref, a 0.6.0 copy whose row is gone by the import records (0.6.2), a copy of a restricted conversation
    (0.6.2) and a flagged 0.6.0 copy that kept its text. Copies of readable messages and emptied copies are never
    counted."""
    expected = 0

    async def check() -> None:
        assert await withheld_counts() == (expected, expected)

    readable = await plant("pq0")
    await promote(readable["message"])
    await check()
    for branch in ("id", "newer key", "tombstone", "legacy ref", "legacy record", "restricted", "flagged"):
        ids = await plant("pq_%s" % branch.replace(" ", "_"))
        copy = await promote(ids["message"])
        async with session_scope() as s:
            item = await s.get(MemoryItem, copy)
            withhold_row = update(ArchiveMessage).where(ArchiveMessage.id == ids["message"]).values(
                content="", tool_calls=[], labels=[], withheld=True)
            if branch == "id":
                await s.execute(withhold_row)
            elif branch == "newer key":
                await s.execute(withhold_row)
                item.flags = {**item.flags, "message_id": 990_001}
            elif branch == "tombstone":
                await s.execute(delete(ArchiveMessage).where(ArchiveMessage.id == ids["message"]))
                s.add(BrainTombstone(source_key=ids["key"], purged_by=ADMIN[0], reason=WITHHELD_BY_ADMIN))
            elif branch == "legacy ref":
                await s.execute(withhold_row)
                item.flags = {"message_id": 990_002, "archive_session_id": ids["session"]}
            elif branch == "legacy record":
                run = ImportRun(status="completed", created_by="importer (cli)")
                s.add(run)
                await s.flush()
                s.add(ImportRecord(run_id=run.id, source_key=ids["key"], record_type="message", action="imported",
                                   target_table="archive_messages", target_id=ids["message"]))
                await s.execute(delete(ArchiveMessage).where(ArchiveMessage.id == ids["message"]))
                s.add(BrainTombstone(source_key=ids["key"], purged_by=ADMIN[0], reason=WITHHELD_BY_ADMIN))
                item.flags = {"message_id": ids["message"], "archive_session_id": ids["session"]}
            elif branch == "restricted":
                sess = await s.get(ArchiveSession, ids["session"])
                sess.sensitivity = "restricted"
            else:
                item.flags = {**item.flags, "source_withheld": True}
        expected += 1
        await check()
    assert await check_withheld_copies() is True
    expected = 0
    await check()


async def test_agents_never_read_a_row_whose_key_has_a_withhold_stone(client):
    """The agent key guard (spec 11): a row whose key an admin's withhold names is withheld by every writer and by
    the repair; even if one were readable (here: a stone written after the row, before any repair), the Supervisor's
    history and recall never read it."""
    ids = await plant("kg1")
    async with session_scope() as s:
        hits, _ = await store.AgentMemory(s, "supervisor").history(MSG)
        assert [h.message.id for h in hits] == [ids["message"]]
        s.add(BrainTombstone(source_key=ids["key"], purged_by=ADMIN[0], reason=WITHHELD_BY_ADMIN))
    async with session_scope() as s:
        assert (await s.get(ArchiveMessage, ids["message"])).withheld is False  # not repaired yet
        hits, _ = await store.AgentMemory(s, "supervisor").history(MSG)
        assert hits == []
    assert MSG not in json.dumps((await ask("what did JARVIS accomplish with %s" % MSG)).data)
    await check_withheld_copies()  # the repair withholds the row itself
    async with session_scope() as s:
        assert (await s.get(ArchiveMessage, ids["message"])).withheld is True
