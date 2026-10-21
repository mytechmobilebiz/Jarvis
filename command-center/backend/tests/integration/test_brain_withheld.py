"""Withheld stays withheld: a message an admin or a rescan withheld after its import keeps no text anywhere, not even
in the rollback copies (import_records.before), and rolling back the import never brings it back. Promoted text that
held a secret goes to admin review; verify scans the rollback copies too."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select, update

from app.brain import importer as imp
from app.brain import review
from app.db import session_scope
from app.models import ArchiveMessage, ArchiveSession, ImportRecord, MemoryItem

from .. import brain_jarvis as bj
from .test_brain_import import modify_copy

pytestmark = pytest.mark.usefixtures("clean_db")
ALL = imp.Selection(all=True)
ADMIN = "admin@test.local"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return bj.build(tmp_path_factory.mktemp("jarvis") / "fx")


@pytest.fixture
def fx(built, tmp_path):
    return bj.copy(built, tmp_path / "fx")


async def _two_runs(fx) -> tuple[int, ImportRecord]:
    """Two runs over a changed copy; one message the second run updated (a rollback copy holds its earlier text)."""
    await imp.run(bj.paths(fx), ALL)
    modify_copy(fx)
    second = await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        record = await s.scalar(select(ImportRecord).join(ArchiveMessage, ArchiveMessage.id == ImportRecord.target_id)
                                .where(ImportRecord.run_id == second["run_id"], ImportRecord.action == "updated",
                                       ImportRecord.target_table == "archive_messages",
                                       ImportRecord.before["content"].astext != "",
                                       ArchiveMessage.withheld.is_(False))
                                .order_by(ImportRecord.id))
        assert record is not None
    return second["run_id"], record


async def test_a_rollback_never_brings_back_a_message_an_admin_withheld(fx):
    run_id, record = await _two_runs(fx)
    async with session_scope() as s:
        await review.withhold_message(s, await s.get(ArchiveMessage, record.target_id))
    async with session_scope() as s:
        copy = (await s.get(ImportRecord, record.id)).before
        # no text; the copy keeps its own sensitivity: one message withheld, not restricted data in the conversation
        # (review 2, S13: a 'restricted' copy made the whole conversation restricted when a rollback put it back)
        assert (copy["content"], copy["tool_calls"], copy["withheld"]) == ("", [], True)
        assert copy["sensitivity"] != "restricted"
    async with session_scope() as s:
        await imp.rollback_run(s, run_id, ADMIN)
    async with session_scope() as s:
        message = await s.get(ArchiveMessage, record.target_id)
        assert message.withheld and message.content == "" and message.tool_calls == []
        assert "withheld_by_admin" in message.flags and message.sensitivity != "restricted"  # one message, not the conversation


async def test_a_rescan_withholds_for_good_and_blanks_the_rollback_copies(fx):
    run_id, record = await _two_runs(fx)
    async with session_scope() as s:
        message = await s.get(ArchiveMessage, record.target_id)
        sid = message.session_id
        # Restricted data an earlier detector missed, found by a rescan with the current ones.
        await s.execute(update(ArchiveMessage).where(ArchiveMessage.id == message.id)
                        .values(content=message.content + " Her SSN is 219-09-9999"))
    result = await imp.rescan(apply=True, actor=ADMIN)
    assert result["sessions_withheld"] >= 1
    async with session_scope() as s:
        copies = (await s.scalars(select(ImportRecord.before).where(
            ImportRecord.target_table == "archive_messages", ImportRecord.target_id.in_(
                select(ArchiveMessage.id).where(ArchiveMessage.session_id == sid))))).all()
        assert copies and all(c == {} or (c["content"] == "" and c["withheld"]) for c in copies)
    async with session_scope() as s:
        await imp.rollback_run(s, run_id, ADMIN)
    async with session_scope() as s:
        sess = await s.get(ArchiveSession, sid)
        assert sess.title == "" and "withheld_by_rescan" in sess.flags and sess.sensitivity == "restricted"
        rows = (await s.scalars(select(ArchiveMessage).where(ArchiveMessage.session_id == sid))).all()
        assert rows and all(m.withheld and m.content == "" and m.tool_calls == [] for m in rows)


async def test_verify_scans_the_rollback_copies(fx):
    run_id, record = await _two_runs(fx)
    assert (await imp.verify(run_id, bj.paths(fx)))["passed"]
    async with session_scope() as s:
        planted = dict(record.before, content="the root password is " + bj.BRAIN_PLANTED["root_password"])
        await s.execute(update(ImportRecord).where(ImportRecord.id == record.id).values(before=planted))
    result = await imp.verify(run_id, bj.paths(fx))
    checks = result["verification"]["checks"]
    assert not checks["no_secrets_or_restricted_data"]["ok"]
    assert {w["table"] for w in checks["no_secrets_or_restricted_data"]["where"]} == {"import_records"}


async def test_promoted_text_that_held_a_secret_needs_an_admin(built):
    await imp.run(bj.paths(built), ALL)
    async with session_scope() as s:
        message = await s.scalar(select(ArchiveMessage).where(ArchiveMessage.withheld.is_(False),
                                                              ArchiveMessage.redactions != {},
                                                              ArchiveMessage.content != "").order_by(ArchiveMessage.id))
        sess = await s.get(ArchiveSession, message.session_id)
        item = await review.promote_message(s, message, sess, kind="knowledge", title="From JARVIS", agent="",
                                            actor=ADMIN)
        assert item.status == "quarantined" and item.flags["secrets_redacted"] == message.redactions
        clean = await s.scalar(select(ArchiveMessage).where(
            ArchiveMessage.withheld.is_(False), ArchiveMessage.redactions == {}, ArchiveMessage.content != "",
            ArchiveMessage.session_id.in_(select(ArchiveSession.id).where(ArchiveSession.sensitivity != "restricted")))
            .order_by(ArchiveMessage.id))
        plain = await review.promote_message(s, clean, await s.get(ArchiveSession, clean.session_id), kind="knowledge",
                                             title="Plain", agent="", actor=ADMIN)
        assert plain.status == "pending_review" and "secrets_redacted" not in plain.flags
        assert await s.get(MemoryItem, item.id) is not None


async def test_a_newer_copy_never_brings_back_a_message_an_admin_withheld(fx):
    await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        sess = await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == "20261010_0010"))
        first_user = await s.scalar(select(ArchiveMessage).where(
            ArchiveMessage.session_id == sess.id, ArchiveMessage.role == "user", ArchiveMessage.withheld.is_(False))
            .order_by(ArchiveMessage.position))
        await review.withhold_message(s, first_user)
    modify_copy(fx)  # the newer backup compacts that very row; its text is unchanged
    await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        message = await s.get(ArchiveMessage, first_user.id)
        assert (message.withheld, message.content, message.tool_calls, message.state) == (True, "", [], "compacted")
        assert "withheld_by_admin" in message.flags
        # One withheld message does not make its conversation restricted: the next import withholds nothing more.
        assert message.sensitivity == first_user.sensitivity
        assert (await s.get(ArchiveSession, sess.id)).sensitivity != "restricted"
        others = (await s.scalars(select(ArchiveMessage).where(ArchiveMessage.session_id == sess.id,
                                                               ArchiveMessage.id != message.id))).all()
        assert others and not any(m.withheld for m in others)


async def test_an_admin_withhold_outlives_a_rollback_of_the_run_that_created_the_message(built):
    first = await imp.run(bj.paths(built), ALL)
    async with session_scope() as s:
        message = await s.scalar(select(ArchiveMessage).where(ArchiveMessage.withheld.is_(False),
                                                              ArchiveMessage.content != "").order_by(ArchiveMessage.id))
        key, text = message.source_key, message.content
        await review.withhold_message(s, message, actor=ADMIN)
    async with session_scope() as s:
        await imp.rollback_run(s, first["run_id"], ADMIN)
    again = await imp.run(bj.paths(built), ALL)
    assert (await imp.verify(again["run_id"], bj.paths(built)))["passed"]
    async with session_scope() as s:
        back = await s.scalar(select(ArchiveMessage).where(ArchiveMessage.source_key == key))
        assert (back.withheld, back.content, back.tool_calls) == (True, "", []) and "withheld_by_admin" in back.flags
        assert text  # it had text before the admin withheld it


async def test_the_withholding_runs_own_session_record_keeps_no_title(fx):
    await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        title = (await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == "20261016_0016"))).title
    assert title
    modify_copy(fx)  # restricted data in that conversation
    second = await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        copies = (await s.scalars(select(ImportRecord.before).where(
            ImportRecord.run_id == second["run_id"], ImportRecord.source_key == "jarvis:session:20261016_0016"))).all()
    assert copies and all(c.get("title") == "" for c in copies)


async def test_rollback_and_rescan_remove_the_quotes_of_what_they_change(fx):
    from app.brain.quotes import REMOVED
    from app.models import Task

    run_id, record = await _two_runs(fx)
    async with session_scope() as s:
        other = await s.scalar(select(ArchiveMessage).where(ArchiveMessage.withheld.is_(False),
                                                            ArchiveMessage.id != record.target_id,
                                                            ArchiveMessage.content != "").order_by(ArchiveMessage.id))
        quoted = Task(agent="supervisor", capability="recall", params={}, status="succeeded", risk="read", result={
            "summary": "x", "data": {"local_only": True,
                                     "answer": ["- a line quoting the changed message", "- the root password is "
                                                + bj.BRAIN_PLANTED["root_password"], "- an ordinary line"],
                                     "sources": [{"message": record.target_id}, {"message": other.id},
                                                 {"message": other.id + 10 ** 6}]}})
        s.add(quoted)
    async with session_scope() as s:
        await imp.rollback_run(s, run_id, ADMIN)
    async with session_scope() as s:
        answer = (await s.get(Task, quoted.id)).result["data"]["answer"]
        assert answer[0] == REMOVED and answer[1] != REMOVED  # the run changed the first source only
    await imp.rescan(apply=True, actor=ADMIN)
    async with session_scope() as s:
        answer = (await s.get(Task, quoted.id)).result["data"]["answer"]
        # A secret the current detectors find, and a source that no longer exists, are removed as well.
        assert answer == [REMOVED, REMOVED, REMOVED]
