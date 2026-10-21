"""JARVIS Brain 0.6.2: decisions files version 4 and restore.ps1's removals (spec 9, D10).

A version 4 file carries each withhold with the JARVIS row it names, an admin's keep and an admin's release of a held
row; decisions-apply --removals-only puts the withholds, purges and releases back without an import run or an admin
(restore.ps1, after restoring an older backup), audited as "restore". The round trip restores a real pg_dump of the
test database with restore.ps1's pg_restore flags. Synthetic data only.

The restore test needs docker (pg_dump and pg_restore in the cc-test-pg container) and is skipped without it;
REQUIRE_DOCKER_TESTS=1 makes a missing docker or container a failure instead."""

from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import select

from app import db
from app.bootstrap import check_withheld_copies
from app.brain import identity, review, store
from app.brain import importer as imp
from app.db import session_scope
from app.models import ArchiveMessage, AuditLog, BrainPurgedContent, BrainWithholdExemption, MemoryItem

from .. import brain_jarvis as bj
from ..conftest import ADMIN
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_holds import STUB, _tool_backup
from .test_brain_schema import _container, _pg, needs_docker
from .test_brain_withhold_identity import (
    DAY,
    T,
    _fresh_production,
    conversation,
    leak_free,
    message_of,
    row_with,
    run,
    secret,
    stones,
    withhold,
)

pytestmark = pytest.mark.usefixtures("clean_db")

OWN_DB = os.environ["TEST_DATABASE_URL"].rsplit("/", 1)[1]
DUMP = "/tmp/%s_restore_point.dump" % OWN_DB  # this run's own (never another build's or the release runs')


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


def _held_backup(fx: dict[str, str], mark: str, sid: str = "prune_01") -> tuple[int, int]:
    """A tool result JARVIS pruned: the full text archived as a compacted twin, the live row a stub with the same uid.
    Withholding the twin holds the stub for review (same uid, other text). (stub row, twin row)"""
    if sid == "prune_01":
        tool = _tool_backup(fx, mark)[1]
    else:
        bj.add_session(fx["db"], sid, started=T + DAY)
        bj.add_row(fx["db"], sid, "user", "Run the second report.", T + DAY + 1)
        tool = bj.add_row(fx["db"], sid, "tool", "REPORT " + secret(mark), T + DAY + 2, tool_call_id="call_2",
                          tool_name="terminal")
    return tool, bj.prune_twin(fx["db"], sid, tool, STUB)


def _knowledge(fx: dict[str, str], mark: str) -> None:
    os.makedirs(os.path.join(fx["profile"], "knowledge"), exist_ok=True)
    with open(os.path.join(fx["profile"], "knowledge", "vendors.md"), "w", encoding="utf-8") as f:
        f.write("# Vendors\nThe courier code is %s.\n" % mark)


async def _purge_vendors() -> str:
    async with session_scope() as s:
        item = await s.scalar(select(MemoryItem).where(MemoryItem.source_key.like("%knowledge/vendors.md")))
        key = item.source_key
        await store.delete_item(s, item, actor=ADMIN[0])
        await review.purge(s, item, actor=ADMIN[0])
    return key


async def audits(action: str) -> list[AuditLog]:
    async with session_scope() as s:
        return list((await s.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id))).all())


async def _hold_decision(client, sid: str, stub_row: int, twin_row: int, how: str) -> None:
    [full] = await message_of(sid, twin_row)
    await withhold(client, full.id)
    [stub] = await message_of(sid, stub_row)
    assert (await stones())[stub.source_key].review == identity.PENDING
    r = await client.post("/api/brain/holds/%d/%s" % (stub.id, how))
    assert r.status_code == 200, r.text


async def test_a_kept_hold_and_a_release_carry_over_to_the_live_installation(client, fx, tmp_path):
    """The test copy's admin keeps one held row and releases another. On the live installation (the same backup, no
    decision yet) decisions-apply withholds both pruned results, keeps the first stub withheld for good and releases
    the second, whose text the next import brings back. The withheld texts stay out."""
    released_mark, kept_mark = "zqv4release62", "zqv4kept62"
    stub1, twin1 = _held_backup(fx, released_mark)
    stub2, twin2 = _held_backup(fx, kept_mark, sid="prune_02")
    await run(fx)
    await _hold_decision(client, "prune_01", stub1, twin1, "release")
    await _hold_decision(client, "prune_02", stub2, twin2, "keep")
    out = tmp_path / "decisions.json"
    assert (await imp.decisions_export(str(out)))["releases"] == 1
    await _fresh_production()
    production = await run(fx)
    result = await imp.decisions_apply(str(out), production["run_id"], actor=ADMIN[0], apply=True)
    assert result["removals"] == {"withheld": 2, "already done": 1}  # both twins; the kept stub with its twin
    assert result["releases"] == {"released": 1} and result["releases_not_applied"] == {}
    [kept] = await message_of("prune_02", stub2)
    assert kept.withheld and (await stones())[kept.source_key].review == identity.KEPT
    [released] = await message_of("prune_01", stub1)
    # The release is applied before the withholds (rules062): the released row is never blanked here.
    assert (released.withheld, released.content) == (False, STUB) and released.source_key not in await stones()
    [api, entry] = await audits("archive.hold_released")  # the test copy's admin, then decisions-apply
    assert "via" not in api.detail and entry.detail["via"] == "decisions-apply" and entry.actor == ADMIN[0]
    [applied] = await audits("brain.decisions_applied")
    assert applied.detail["releases"] == {"released": 1}
    later = await run(fx)
    [back] = await message_of("prune_01", stub1)
    assert (back.withheld, back.content) == (False, STUB)
    [still] = await message_of("prune_02", stub2)
    assert still.withheld and still.content == ""
    await leak_free(client, released_mark, fx, (later["run_id"],))
    await leak_free(client, kept_mark)


async def test_removals_only_needs_no_run_and_no_admin_and_audits_restore(client, fx, tmp_path):
    """restore.ps1's file and apply: only removals and releases; no import run; the actor is "system (restore)"
    (every decision in it was an admin's when it was taken) unless an admin is named; each withhold and purge is
    audited as the API audits it, with via "restore"; a dry run changes nothing; item decisions are ignored."""
    mark, purge_mark = "zqv4restore62", "zqv4purge62"
    ids = conversation(fx["db"], "rs_01", T, [("user", "What is it worth?"), ("assistant", secret(mark))])
    _knowledge(fx, purge_mark)
    await run(fx)
    [message] = await message_of("rs_01", ids[1])
    await withhold(client, message.id)
    key = await _purge_vendors()
    out = tmp_path / "removals.json"
    assert await imp.decisions_export(str(out), removals_only=True) == {"removals": 2, "releases": 0,
                                                                         "removals_only": True}
    data = json.loads(out.read_text())
    assert "decisions" not in data and data["removals_only"] is True
    data["decisions"] = [{"source_key": "jarvis:file:profile/x.md", "content_hash": "0", "status": "approved"}]
    out.write_text(json.dumps(data))  # an item decision in the file: ignored with --removals-only
    await _fresh_production()
    await run(fx)
    assert await row_with(mark) is not None
    dry = await imp.decisions_apply(str(out), None, actor=None, apply=False, removals_only=True)
    assert dry["removals"] == {"withheld": 1, "purged": 1} and dry["would_apply"] == 0
    assert await row_with(mark) is not None and await audits("brain.decisions_applied") == []
    with pytest.raises(imp.Refused, match="active admin"):
        await imp.decisions_apply(str(out), None, actor="nobody@test.local", apply=True, removals_only=True)
    result = await imp.decisions_apply(str(out), None, actor=None, apply=True, removals_only=True)
    assert result["removals"] == {"withheld": 1, "purged": 1} and result["not_applied"] == {}
    assert await row_with(mark) is None and await bj.nowhere(purge_mark) == []
    async with session_scope() as s:
        assert await s.scalar(select(MemoryItem).where(MemoryItem.source_key == key)) is None
        assert await s.scalar(select(BrainPurgedContent.fp).where(BrainPurgedContent.source_key == key))
    [applied] = await audits("brain.decisions_applied")
    assert (applied.actor, applied.target_type, applied.detail["via"], applied.detail["run"]) == (
        imp.RESTORE_ACTOR, "brain", "restore", None)
    [withheld] = [a for a in await audits("archive.message_withheld") if a.actor == imp.RESTORE_ACTOR]
    assert withheld.detail["via"] == "restore"
    assert [a.actor for a in await audits("memory.purged")] == [imp.RESTORE_ACTOR]
    again = await imp.decisions_apply(str(out), None, actor=ADMIN[0], apply=True, removals_only=True)
    assert again["removals"] == {"already done": 2}
    await leak_free(client, mark)


async def test_a_newer_file_and_a_missing_run_are_refused(tmp_path):
    out = tmp_path / "decisions.json"
    out.write_text(json.dumps({"format": "jarvis-brain-decisions", "version": 5, "removals": []}))
    with pytest.raises(imp.Refused, match="newer Command Center"):
        await imp.decisions_apply(str(out), 1, actor=ADMIN[0], apply=True)
    out.write_text(json.dumps({"format": "jarvis-brain-decisions", "version": 4, "removals": []}))
    with pytest.raises(imp.Refused, match="--run"):
        await imp.decisions_apply(str(out), None, actor=ADMIN[0], apply=True)


async def test_a_release_names_only_its_own_group_and_never_frees_a_certain_match(client, fx, tmp_path):
    """A release in the file whose JARVIS row is the withheld message itself here (a certain stone binds it) leaves
    it withheld; a release of another group frees nothing of this one."""
    mark = "zqv4certain62"
    stub, twin = _held_backup(fx, mark)
    await run(fx)
    [full] = await message_of("prune_01", twin)
    await withhold(client, full.id)
    [held] = await message_of("prune_01", stub)
    out = tmp_path / "decisions.json"
    out.write_text(json.dumps({"format": "jarvis-brain-decisions", "version": 4, "removals": [], "releases": [
        {"anchor": full.source_key, "session": "prune_01", "row": full.source_message_id},
        {"anchor": "jarvis:message:uid:another-group", "session": "prune_01", "row": held.source_message_id},
        {"anchor": "", "session": "prune_01", "row": "1"}]}))
    result = await imp.decisions_apply(str(out), None, actor=ADMIN[0], apply=True, removals_only=True)
    assert result["releases"] == {"kept for later imports": 2}
    assert result["releases_not_applied"] == {"not a release": [3]}
    [still_full] = await message_of("prune_01", twin)
    [still_held] = await message_of("prune_01", stub)
    assert still_full.withheld and still_held.withheld
    assert (await stones())[still_held.source_key].review == identity.PENDING
    await check_withheld_copies()  # the exemption of the full row is never read by a certain rule
    [after] = await message_of("prune_01", twin)
    assert after.withheld and after.content == ""
    await leak_free(client, mark)


# --- restore.ps1: a backup restored over later decisions (D10) --------------------------------------------------------


@pytest.fixture
def restore_point(migrated):
    """pg_dump of this run's test database now, and pg_restore of it later with restore.ps1's flags (the app's
    connections are closed around it, as restore.ps1 stops the application)."""
    container = _container()

    class Point:
        def take(self) -> None:
            done = _pg(container, "pg_dump", "-U", "cc", "-Fc", "-f", DUMP, OWN_DB)
            assert done.returncode == 0, done.stderr

        async def restore(self) -> None:
            await migrated.dispose()
            done = _pg(container, "pg_restore", "-U", "cc", "-d", OWN_DB, "--clean", "--if-exists",
                       "--single-transaction", "--exit-on-error", DUMP)
            await migrated.dispose()
            assert done.returncode == 0, done.stderr

    yield Point()
    _pg(container, "rm", "-f", DUMP)


@needs_docker
async def test_restore_of_an_older_backup_then_removals_only_puts_every_decision_back(client, fx, tmp_path,
                                                                                     restore_point):
    """The owner's backup is taken before an admin withholds a message, holds and releases a row and purges an item.
    restore.ps1 exports the removals first, restores the older backup (the decisions are undone: the text is back),
    starts the application (the start-up check) and applies the removals again: everything is as the admin left it,
    and the next import keeps it so."""
    mark, held_mark, purge_mark = "zqv4dump62", "zqv4dumpheld62", "zqv4dumppurge62"
    ids = conversation(fx["db"], "rs_02", T, [("user", "What is it worth?"), ("assistant", secret(mark))])
    stub, twin = _held_backup(fx, held_mark)
    _knowledge(fx, purge_mark)
    await run(fx)
    restore_point.take()  # backup.ps1, before any decision
    [message] = await message_of("rs_02", ids[1])
    await withhold(client, message.id)
    await _hold_decision(client, "prune_01", stub, twin, "release")
    key = await _purge_vendors()
    out = tmp_path / "removals-before-restore.json"
    await imp.decisions_export(str(out), removals_only=True)  # restore.ps1, step 1

    await restore_point.restore()
    client.brain_tokens.clear()  # the sign-ins made after the backup are gone with it
    assert await row_with(mark) is not None and await row_with(held_mark) is not None  # the backup's state
    async with session_scope() as s:
        assert await s.scalar(select(MemoryItem).where(MemoryItem.source_key == key)) is not None
        assert await s.scalar(select(BrainWithholdExemption.anchor_key)) is None
    assert await db.run_migrations() == []  # the next start: a 0.6.2 backup holds every migration
    assert await check_withheld_copies() is True

    result = await imp.decisions_apply(str(out), None, actor=None, apply=True, removals_only=True)  # step 2
    assert result["removals"] == {"withheld": 2, "purged": 1}
    assert result["releases"] == {"released": 1}
    assert await row_with(mark) is None and await row_with(held_mark) is None
    assert await bj.nowhere(purge_mark) == []
    [released] = await message_of("prune_01", stub)
    assert (released.withheld, released.content) == (False, STUB)  # released before the withholds (rules062)
    later = await run(fx)
    [back] = await message_of("prune_01", stub)
    assert (back.withheld, back.content) == (False, STUB)
    async with session_scope() as s:
        assert await s.scalar(select(MemoryItem).where(MemoryItem.source_key == key)) is None
        assert await s.scalar(select(ArchiveMessage.id).where(ArchiveMessage.content.contains(mark))) is None
    await leak_free(client, mark, fx, (later["run_id"],))
    await leak_free(client, held_mark)
    await leak_free(client, purge_mark)
