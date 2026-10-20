"""JARVIS Brain 0.6.2: the withhold identity under concurrency (spec 15, 19.7).

An admin's withhold, the repair and the hand-run commands while an import holds the import lock; a promote racing
the repair (whichever commits first, no text survives); the start-up check while an import runs (quotes refreshed,
the repair left to that import, the worker retrying); two starts at once (one repair); an import interrupted after
its batch wrote stones, then resumed. Synthetic data only.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.bootstrap import check_withheld_copies
from app.brain import identity, review, store
from app.brain import importer as imp
from app.brain.quotes import REMOVED
from app.db import session_scope, sessionmaker
from app.models import ArchiveMessage, ArchiveSession, AuditLog, BrainTombstone, ImportRun, MemoryItem, Task

from .. import brain_jarvis as bj
from ..conftest import ADMIN
from .test_brain_api import USERS, as_, client  # noqa: F401 - client is a fixture
from .test_brain_withhold_copies import lock_waits, quoting_task

pytestmark = pytest.mark.usefixtures("clean_db")

ALL = imp.Selection(all=True)
BACKEND = Path(__file__).resolve().parents[2]
T = 1795000000.0


def secret(mark: str) -> str:
    return "The new vendor contract with %s is worth forty thousand dollars a year." % mark


def backup(tmp_path, mark: str) -> dict[str, str]:
    fx = bj.mini(tmp_path / "fx")
    bj.add_session(fx["db"], "cc_01", started=T)
    for n, (role, content) in enumerate([("user", "Please summarise the vendor situation."),
                                         ("assistant", "Sure, here it is."), ("user", "The headline number?"),
                                         ("assistant", secret(mark)), ("user", "And the other vendor?"),
                                         ("assistant", "No change there.")]):
        bj.add_row(fx["db"], "cc_01", role, content, T + n + 1)
    return fx


async def row_with(text_: str) -> ArchiveMessage | None:
    async with session_scope() as s:
        return await s.scalar(select(ArchiveMessage).where(ArchiveMessage.content.contains(text_)).limit(1))


async def stones() -> list[str]:
    async with session_scope() as s:
        return sorted((await s.scalars(select(BrainTombstone.source_key).where(
            BrainTombstone.reason == identity.WITHHELD_BY_ADMIN))).all())


async def held_import_lock() -> imp.LockConnection:
    locks = await imp.LockConnection.open()
    assert await locks.try_lock(imp.GLOBAL_LOCK)
    return locks


async def test_api_withhold_during_import_409_and_cli_refusals(client, tmp_path):
    mark = "zqconc409"
    fx = backup(tmp_path, mark)
    await imp.run(bj.paths(fx), ALL)
    row = await row_with(mark)
    locks = await held_import_lock()
    try:
        await as_(client, "admin")
        r = await client.post("/api/brain/messages/%d/withhold" % row.id)
        assert r.status_code == 409
        for refused in (imp.repair(apply=True, actor=ADMIN[0]), imp.rescan(apply=True, actor=ADMIN[0]),
                        imp.forget_fingerprints(apply=True, actor=ADMIN[0])):
            with pytest.raises(imp.Refused):
                await refused
        env = {**os.environ, "DATABASE_URL": os.environ["TEST_DATABASE_URL"]}
        done = subprocess.run([sys.executable, "-m", "app.cli", "brain-import", "repair", "--yes"], cwd=BACKEND,
                              env=env, capture_output=True, text=True, timeout=120)
        assert done.returncode == 2 and "refused" in done.stdout, done.stdout + done.stderr
        pending = subprocess.run([sys.executable, "-m", "app.cli", "brain-import", "repair"], cwd=BACKEND, env=env,
                                 capture_output=True, text=True, timeout=120)
        assert pending.returncode == 0 and "nothing was changed" in pending.stdout  # read-only: never refused
    finally:
        await locks.close()
    assert not (await row_with(mark)).withheld  # nothing changed while the import held the lock


async def test_promote_holding_row_lock_then_import_inherits_copy_is_emptied(client, tmp_path):
    """The promote commits first (it holds the row's lock while it copies the text); the import's repair waits for
    it, withholds the row, and empties the new copy."""
    mark = "zqconcpromote"
    fx = backup(tmp_path, mark)
    await imp.run(bj.paths(fx), ALL)
    row = await row_with(mark)
    async with session_scope() as s:  # a withhold decisions-apply kept for this key (its row still readable)
        s.add(BrainTombstone(source_key=row.source_key, purged_by=ADMIN[0], reason=identity.WITHHELD_BY_ADMIN))
    promoting = sessionmaker()()
    try:
        message = await promoting.scalar(select(ArchiveMessage).where(ArchiveMessage.id == row.id).with_for_update())
        sess = await promoting.get(ArchiveSession, message.session_id)
        with pytest.raises(store.BrainError):  # the stone is already there: refused before any copy
            await review.promote_message(promoting, message, sess, kind="project", title="Copy", agent="",
                                         actor=USERS["operator"][0])
    finally:
        await promoting.rollback()
        await promoting.close()
    async with session_scope() as s:  # so promote the race's other order: no stone yet, row locked by the promote
        await s.execute(BrainTombstone.__table__.delete())
    promoting = sessionmaker()()
    try:
        message = await promoting.scalar(select(ArchiveMessage).where(ArchiveMessage.id == row.id).with_for_update())
        sess = await promoting.get(ArchiveSession, message.session_id)
        item = await review.promote_message(promoting, message, sess, kind="project", title="Copy " + mark,
                                            agent="", actor=USERS["operator"][0])
        item_id = item.id
        async with session_scope() as s:  # the admin's decision arrives (decisions-apply's tombstone)
            s.add(BrainTombstone(source_key=row.source_key, purged_by=ADMIN[0],
                                 reason=identity.WITHHELD_BY_ADMIN))
        importing = asyncio.ensure_future(imp.run(bj.paths(fx), ALL))
        await lock_waits()  # the import's repair waits for the promote's row lock
        await promoting.commit()
    finally:
        await promoting.close()
    await importing
    async with session_scope() as s:
        copy = await s.get(MemoryItem, item_id)
        assert (copy.title, copy.body, copy.status) == (store.WITHHELD_COPY_TITLE, "", "quarantined")
    assert await bj.nowhere(mark) == []


async def test_promote_waiting_on_group_rows_is_refused(tmp_path, monkeypatch):
    """The repair holds the row (its update not committed yet); a promote that waits for the row's lock finds it
    withheld once the repair commits, and is refused."""
    mark = "zqconcwait"
    fx = backup(tmp_path, mark)
    await imp.run(bj.paths(fx), ALL)
    row = await row_with(mark)
    async with session_scope() as s:
        s.add(BrainTombstone(source_key=row.source_key, purged_by=ADMIN[0], reason=identity.WITHHELD_BY_ADMIN))
    paused, resume = asyncio.Event(), asyncio.Event()
    original = identity.repair_rows

    async def slow_repair_rows(s, *, cause):
        counts = await original(s, cause=cause)
        paused.set()
        await resume.wait()
        return counts
    monkeypatch.setattr(identity, "repair_rows", slow_repair_rows)
    repairing = asyncio.ensure_future(imp.repair(apply=True, actor=ADMIN[0]))
    await paused.wait()

    async def promote() -> None:
        async with session_scope() as s:
            message = await s.scalar(select(ArchiveMessage).where(ArchiveMessage.id == row.id).with_for_update())
            sess = await s.get(ArchiveSession, message.session_id)
            await review.promote_message(s, message, sess, kind="project", title="Copy", agent="",
                                         actor=USERS["operator"][0])
    promoting = asyncio.ensure_future(promote())
    await lock_waits()
    resume.set()
    await repairing
    with pytest.raises(store.BrainError, match="withheld"):
        await promoting
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(MemoryItem)) == 0


async def test_start_up_check_with_import_lock_refreshes_quotes_and_worker_retries(tmp_path, make_worker, caplog):
    caplog.set_level(logging.INFO)
    mark = "zqconcquote"
    fx = backup(tmp_path, mark)
    await imp.run(bj.paths(fx), ALL)
    row = await row_with(mark)
    async with session_scope() as s:  # as 0.6.0 left it: withheld by an import, its quote kept
        await s.execute(ArchiveMessage.__table__.update().where(ArchiveMessage.id == row.id).values(
            withheld=True, content="", labels=[]))
        task = quoting_task(message_id=row.id)
        s.add(task)
        await s.flush()
        task_id = task.id
        s.add(BrainTombstone(source_key="jarvis:message:cc_01:999", purged_by=ADMIN[0],
                             reason=identity.WITHHELD_BY_ADMIN))  # a stone the repair still has to bind
    locks = await held_import_lock()
    try:
        assert await check_withheld_copies() is False
        assert "identity repair skipped" in caplog.text
        async with session_scope() as s:
            answer = (await s.get(Task, task_id)).result["data"]["answer"]
            assert answer[1] == REMOVED  # quotes are refreshed even while the import holds the lock
            assert not (await s.get(BrainTombstone, "jarvis:message:cc_01:999")).bound  # the repair waits
        worker = make_worker(withheld_check_done=False)
        await worker.tick()
        assert worker._withheld_check_done is False
    finally:
        await locks.close()
    await worker.tick()  # within the minute: not retried yet
    assert worker._withheld_check_done is False
    worker._last_withheld_check = None  # a minute later
    await worker.tick()
    assert worker._withheld_check_done is True
    async with session_scope() as s:
        assert (await s.get(BrainTombstone, "jarvis:message:cc_01:999")).bound


async def test_two_bootstraps_one_repair_one_audit(tmp_path, monkeypatch):
    """The api and the worker start together: one repairs, the other finds the locks taken and skips (the worker
    retries later and finds nothing to do)."""
    mark = "zqconctwo"
    fx = backup(tmp_path, mark)
    await imp.run(bj.paths(fx), ALL)
    row = await row_with(mark)
    async with session_scope() as s:
        s.add(BrainTombstone(source_key=row.source_key, purged_by=ADMIN[0], reason=identity.WITHHELD_BY_ADMIN))
    original = identity.repair_rows

    async def slow_repair_rows(s, *, cause):  # the first start is still repairing when the second one comes
        counts = await original(s, cause=cause)
        await asyncio.sleep(0.5)
        return counts
    monkeypatch.setattr(identity, "repair_rows", slow_repair_rows)
    results = await asyncio.gather(check_withheld_copies(), check_withheld_copies())
    assert sorted(results) == [False, True]
    assert await check_withheld_copies() is True  # a later start: nothing left to do
    async with session_scope() as s:
        entries = list((await s.scalars(select(AuditLog).where(
            AuditLog.action == "brain.withhold_identity_repaired").order_by(AuditLog.id))).all())
    sql_entries = [e for e in entries if "withheld_by_key" in e.detail]
    assert len(sql_entries) == 1 and sql_entries[0].detail["withheld_by_key"] == 1
    assert len(entries) <= 2  # and at most the Python step's own entry (the copies, the session recomputed)
    assert await bj.nowhere(mark) == []


async def test_import_cancelled_mid_batch_then_resumed_no_duplicate_stones(client, tmp_path, monkeypatch):
    mark = "zqconccancel"
    fx = backup(tmp_path, mark)
    await imp.run(bj.paths(fx), ALL)
    await as_(client, "admin")
    r = await client.post("/api/brain/messages/%d/withhold" % (await row_with(mark)).id)
    assert r.status_code == 200
    bj.hermes_compact(fx["db"], "cc_01", head_n=1, tail_n=4, ts=T + 100)
    bj.add_session(fx["db"], "cc_02", started=T + 200)
    bj.add_row(fx["db"], "cc_02", "user", "A later conversation.", T + 201)
    original = imp.Processor.withhold_batch

    async def cancel_in_batch(self):
        raise asyncio.CancelledError()  # SIGTERM in the middle of the batch: after its stones and rows
    monkeypatch.setattr(imp.Processor, "withhold_batch", cancel_in_batch)
    with pytest.raises(imp.ImportFailed, match="interrupted"):
        await imp.run(bj.paths(fx), ALL)
    monkeypatch.setattr(imp.Processor, "withhold_batch", original)
    assert await bj.nowhere(mark) == []
    before = await stones()
    async with session_scope() as s:
        run_id = await s.scalar(select(func.max(ImportRun.id)))
        assert (await s.get(ImportRun, run_id)).status == "interrupted"
    resumed = await imp.resume(run_id, bj.paths(fx))
    after = await stones()
    assert len(after) == len(set(after)) and set(before) <= set(after) and len(after) > len(before)
    again = await imp.run(bj.paths(fx), ALL)
    assert await stones() == after and again["report"]["stones_added"] == 0
    v = await imp.verify(resumed["run_id"], bj.paths(fx))
    assert v["passed"], {k: c for k, c in v["verification"]["checks"].items() if not c["ok"]}
    assert await bj.nowhere(mark) == []


TWIN = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"  # the twin's uid (a uid backfill), no marker in it


async def test_two_admins_withholding_twins(client, tmp_path):
    """Two admins withhold the two stored rows of the same JARVIS message at once (0.6.1 stored a uid backfill twice):
    the second waits for nothing (409 while the first holds the import lock), then finds its row already withheld as
    a member of the first admin's group (400). One admin stone, the twin a member: no text, no second group."""
    mark = "zqconctwins"
    fx = backup(tmp_path, mark)
    await imp.run(bj.paths(fx), ALL)
    first = await row_with(mark)
    async with session_scope() as s:  # what 0.6.1 left after a uid backfill: the same JARVIS row stored twice
        twin = ArchiveMessage(session_id=first.session_id, source_key="jarvis:message:uid:" + TWIN,
                              source_message_id=first.source_message_id, source_uid=TWIN,
                              position=first.position, role=first.role, content=first.content, at=first.at,
                              source_fp="fp-twin")
        s.add(twin)
        await s.flush()
        twin_id = twin.id
    async with sessionmaker()() as a:  # admin A's withhold, not committed yet (as the API runs it)
        assert await imp.try_xact_lock(a, imp.GLOBAL_LOCK)
        message = await a.get(ArchiveMessage, first.id, with_for_update=True)
        done = await review.withhold_message(a, message, actor=ADMIN[0])
        assert done.also_withheld == [twin_id]
        await as_(client, "admin")
        r = await client.post("/api/brain/messages/%d/withhold" % twin_id)  # admin B, meanwhile
        assert r.status_code == 409, r.text
        await a.commit()
    r = await client.post("/api/brain/messages/%d/withhold" % twin_id)
    assert r.status_code == 400 and "already withheld" in r.text
    async with session_scope() as s:
        st = {t.source_key: t for t in (await s.scalars(select(BrainTombstone).where(
            BrainTombstone.reason == identity.WITHHELD_BY_ADMIN))).all()}
        rows = {m.id: m for m in (await s.scalars(select(ArchiveMessage).where(
            ArchiveMessage.id.in_([first.id, twin_id])))).all()}
    assert set(st) == {first.source_key, "jarvis:message:uid:" + TWIN}
    assert st[first.source_key].anchor_key == "" and st["jarvis:message:uid:" + TWIN].anchor_key == \
        first.source_key
    assert st["jarvis:message:uid:" + TWIN].via == "same JARVIS row"
    assert all(m.withheld and m.content == "" and m.source_fp.startswith("w1:") for m in rows.values())
    assert await bj.nowhere(mark) == []
