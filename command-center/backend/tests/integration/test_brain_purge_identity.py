"""JARVIS Brain 0.6.2: an item an admin purged stays purged when it comes back under another key (spec 10, 19.10).

A memory entry moved or copied to another memory file (matched by the entry digest its key already holds), a file
renamed or copied (matched by a keyed fingerprint of its bytes, HMAC with a key derived from the database password,
never a plain hash), an item that already came back on 0.6.x (quarantined for an admin, never purged automatically),
a database password that changed (the import refuses until the fingerprints are forgotten, audited), and no password
at all (purges still hold by key and digest). Synthetic files only.
"""

from __future__ import annotations

import hashlib
import os
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.brain import identity, review, store
from app.brain import importer as imp
from app.db import session_scope
from app.models import AuditLog, BrainMaintenance, BrainPurgedContent, BrainTombstone, ImportRecord, MemoryItem

from .. import brain_jarvis as bj
from ..conftest import ADMIN

pytestmark = pytest.mark.usefixtures("clean_db")

ALL = imp.Selection(all=True)
FILE_PURGED = "purged by an admin"
SAME_TEXT = "purged by an admin: the same text as an item an admin purged"


def write(fx: dict[str, str], rel: str, text_: str) -> str:
    path = os.path.join(fx["profile"], rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text_)
    return path


def entries(*texts: str) -> str:
    return "\n§\n".join(texts) + "\n"


def file_key(rel: str) -> str:
    return "jarvis:file:profile/%s" % rel


async def item(key: str) -> MemoryItem | None:
    async with session_scope() as s:
        return await s.scalar(select(MemoryItem).where(MemoryItem.source_key == key))


async def item_with(text_: str) -> MemoryItem | None:
    async with session_scope() as s:
        return await s.scalar(select(MemoryItem).where(MemoryItem.body.contains(text_)).order_by(MemoryItem.id)
                              .limit(1))


async def purge(found: MemoryItem) -> None:
    async with session_scope() as s:
        target = await s.get(MemoryItem, found.id)
        await store.delete_item(s, target, actor=ADMIN[0])
        await review.purge(s, target, actor=ADMIN[0])


async def records(run_id: int) -> dict[str, ImportRecord]:
    async with session_scope() as s:
        return {r.source_key: r for r in (await s.scalars(select(ImportRecord).where(
            ImportRecord.run_id == run_id))).all()}


async def fingerprints() -> list[str]:
    async with session_scope() as s:
        return list((await s.scalars(select(BrainPurgedContent.fp))).all())


async def run(fx: dict[str, str]) -> dict:
    return await imp.run(bj.paths(fx), ALL)


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


async def test_entry_moved_and_copied_between_memory_files(fx):
    mark = "zqentry62"
    secret = "The office alarm code is changed every month (%s)." % mark
    write(fx, "memories/MEMORY.md", entries("Invoices go out on the first working day.", secret))
    await run(fx)
    found = await item_with(mark)
    assert found is not None and "#entry-" in found.source_key
    await purge(found)
    digest = identity.ENTRY.search(found.source_key).group(1)
    # JARVIS moves it to USER.md and copies it into a second memory file.
    write(fx, "memories/MEMORY.md", entries("Invoices go out on the first working day."))
    write(fx, "memories/USER.md", entries("The owner prefers short answers.", secret))
    write(fx, "memories/notes/MEMORY.md", entries(secret))
    second = await run(fx)
    moved = [k for k, r in (await records(second["run_id"])).items() if k.endswith("#entry-" + digest)]
    assert len(moved) == 2
    for key in moved:
        r = (await records(second["run_id"]))[key]
        assert (r.action, r.reason) == ("excluded", SAME_TEXT)
    assert await item_with(mark) is None
    async with session_scope() as s:
        stones = {t.source_key: t.via for t in (await s.scalars(select(BrainTombstone))).all()}
    assert {stones[k] for k in moved} == {"same text"}
    assert await bj.nowhere(mark) == []
    v = await imp.verify(second["run_id"], bj.paths(fx))
    assert v["passed"] and v["verification"]["checks"]["purged_stay_purged"]["ok"]
    # Inverse (O15): an entry with different text stays.
    assert await item_with("The owner prefers short answers.") is not None


async def test_whole_file_renamed_after_062_purge(fx):
    mark = "zqrename62"
    write(fx, "knowledge/vendors.md", "# Vendors\nThe preferred courier is %s.\n" % mark)
    write(fx, "knowledge/other.md", "# Other\nNothing to see here.\n")
    await run(fx)
    found = await item(file_key("knowledge/vendors.md"))
    await purge(found)
    [fp] = await fingerprints()
    assert re.fullmatch(r"k1:[0-9a-f]{8}:[0-9a-f]{64}", fp) and found.source_sha256 not in fp
    os.rename(os.path.join(fx["profile"], "knowledge/vendors.md"), os.path.join(fx["profile"], "knowledge/v2.md"))
    second = await run(fx)
    r = (await records(second["run_id"]))[file_key("knowledge/v2.md")]
    assert (r.action, r.reason) == ("excluded", SAME_TEXT)
    assert await item_with(mark) is None and await bj.nowhere(mark) == []
    assert second["report"]["purged_same_text"] == 1
    # Inverse (O15): a file with the same name in another folder, and other bytes, is imported.
    write(fx, "knowledge/archive/vendors.md", "# Vendors\nThe preferred courier changed.\n")
    third = await run(fx)
    assert (await records(third["run_id"]))[file_key("knowledge/archive/vendors.md")].action == "imported"


async def test_copy_in_same_backup_of_legacy_purge_then_renamed(fx):
    """A purge made before 0.6.2 has no fingerprint: a copy of the file in the same backup is recognised by its bytes,
    and the import learns the fingerprint from the purged file itself, so a later rename is recognised too."""
    mark = "zqlegacy62"
    content = "# Prices\nThe special price list for %s.\n" % mark
    write(fx, "knowledge/a.md", content)
    await run(fx)
    found = await item(file_key("knowledge/a.md"))
    async with session_scope() as s:  # as 0.6.1 purged it: the item gone, a tombstone, no fingerprint
        await s.delete(await s.get(MemoryItem, found.id))
        s.add(BrainTombstone(source_key=found.source_key, purged_by=ADMIN[0], reason=FILE_PURGED))
    write(fx, "knowledge/c.md", content)
    second = await run(fx)
    recs = await records(second["run_id"])
    assert (recs[file_key("knowledge/a.md")].reason, recs[file_key("knowledge/c.md")].reason) == (FILE_PURGED,
                                                                                                 SAME_TEXT)
    assert len(await fingerprints()) == 1  # learned from a.md
    os.remove(os.path.join(fx["profile"], "knowledge/a.md"))
    os.rename(os.path.join(fx["profile"], "knowledge/c.md"), os.path.join(fx["profile"], "knowledge/d.md"))
    third = await run(fx)
    assert (await records(third["run_id"]))[file_key("knowledge/d.md")].reason == SAME_TEXT
    assert await bj.nowhere(mark) == []


async def test_legacy_entry_already_back_is_quarantined_by_r8(fx):
    """On 0.6.x the purged entry already came back under another memory file's key (a 0.6.1 purge kept no
    fingerprint, and 0.6.1 matched entries by key only): no automatic purge, but the first start-up step of 0.6.2
    quarantines it for an admin (flag purged_before), so no agent reads it. Other entries are left as they are."""
    from app.bootstrap import check_withheld_copies
    from app.brain.policy import RISK_FLAGS
    from app.models import ImportRun

    mark = "zqbackentry62"
    secret = "The storage unit is number %s." % mark
    write(fx, "memories/MEMORY.md", entries("Invoices go out on the first working day.", secret))
    await run(fx)
    found = await item_with(mark)
    other = await item_with("Invoices go out")
    await purge(found)
    async with session_scope() as s:
        await s.execute(BrainPurgedContent.__table__.delete())  # a 0.6.1 purge: no fingerprint
        await s.execute(BrainMaintenance.__table__.delete())  # 0.6.2 has not started yet
        later = ImportRun(status="completed", importer_version="1.0.1")  # a 0.6.1 import after the purge
        s.add(later)
        await s.flush()
        back = await store.create_item(
            s, kind="business", title="Storage", body=secret, actor="importer (cli)", origin="import",
            source_system="jarvis", source_key=found.source_key.replace("memories/MEMORY.md", "memories/USER.md"),
            source_sha256=hashlib.sha256(secret.encode()).hexdigest(), import_run_id=later.id, status="approved")
        back_id = back.id
    assert await check_withheld_copies() is True
    async with session_scope() as s:
        quarantined = await s.get(MemoryItem, back_id)
        assert quarantined.status == "quarantined" and quarantined.flags.get("purged_before") is True
        kept = await s.get(MemoryItem, other.id)
        assert "purged_before" not in kept.flags
        agent_ids = [i.id for i in await store.AgentMemory(s, "supervisor").relevant(mark)]
    assert back_id not in agent_ids and "purged_before" in RISK_FLAGS
    assert await check_withheld_copies() is True  # once: the marker is set


async def test_restore_undoing_a_purge_quarantines_and_reapplies(fx, tmp_path):
    """A restore of a dump older than a purge brings the item back with the purged key (here the tombstone is back
    before the purge itself is applied again): the start-up step quarantines it (rule a), so no agent and no person
    but an admin reads it meanwhile. Then restore.ps1 applies the removals it exported before the restore
    (decisions-apply --removals-only): the item is purged again, its fingerprint kept, audited as a restore.
    (test_brain_decisions_v4.py restores a real pg_dump.)"""
    mark = "zqrestore62"
    write(fx, "knowledge/vendors.md", "# Vendors\nThe preferred courier is %s.\n" % mark)
    await run(fx)
    found = await item(file_key("knowledge/vendors.md"))
    async with session_scope() as s:
        s.add(BrainTombstone(source_key=found.source_key, purged_by=ADMIN[0], reason=FILE_PURGED))
    out = tmp_path / "removals-before-restore.json"
    assert (await imp.decisions_export(str(out), removals_only=True))["removals"] == 1
    from app.bootstrap import check_withheld_copies

    assert await check_withheld_copies() is True
    async with session_scope() as s:
        back = await s.get(MemoryItem, found.id)
    assert back.status == "quarantined" and back.flags["purged_before"] is True
    result = await imp.decisions_apply(str(out), None, actor=None, apply=True, removals_only=True)
    assert result["removals"] == {"purged": 1}
    assert await item(found.source_key) is None and await bj.nowhere(mark) == []
    assert await fingerprints() == [identity.purge_fp(identity.purge_key(), found.source_sha256)]
    async with session_scope() as s:
        entry = await s.scalar(select(AuditLog).where(AuditLog.action == "memory.purged"))
        applied = await s.scalar(select(AuditLog).where(AuditLog.action == "brain.decisions_applied"))
    assert entry.actor == imp.RESTORE_ACTOR and applied.detail["via"] == "restore"


async def test_key_id_mismatch_refuses_and_forget_other_key_audits(fx):
    mark = "zqkeyid62"
    write(fx, "knowledge/vendors.md", "# Vendors\nThe preferred courier is %s.\n" % mark)
    await run(fx)
    found = await item(file_key("knowledge/vendors.md"))
    await purge(found)
    other = identity.purge_key(SimpleNamespace(database_url="postgresql+asyncpg://cc:another-password@db/cc"))
    async with session_scope() as s:  # a fingerprint made under another database password
        s.add(BrainPurgedContent(fp=identity.purge_fp(other, "a" * 64), source_key=found.source_key))
    with pytest.raises(imp.Refused, match="another database password"):
        await run(fx)
    status = await imp.status()
    assert status["purge_fingerprints_made_with_another_key"] == 1
    dry = await imp.forget_fingerprints(apply=False, actor=ADMIN[0])
    assert dry["fingerprints_made_with_another_key"] == 1 and len(await fingerprints()) == 2
    done = await imp.forget_fingerprints(apply=True, actor=ADMIN[0])
    assert done["fingerprints_made_with_another_key"] == 1 and len(await fingerprints()) == 1
    async with session_scope() as s:
        entry = await s.scalar(select(AuditLog).where(AuditLog.action == "brain.purge_fingerprints_forgotten"))
    assert entry.detail == {"count": 1}
    again = await run(fx)
    assert (await records(again["run_id"]))[file_key("knowledge/vendors.md")].reason == FILE_PURGED


async def test_absent_key_purges_hold_by_key(fx, monkeypatch):
    """No database password (development): no fingerprint is kept, and purges still hold by key, by entry digest and
    by a copy in the same backup."""
    monkeypatch.setattr(identity, "purge_key", lambda settings=None: None)
    mark = "zqnokey62"
    secret = "The safe combination hint is %s." % mark
    write(fx, "memories/MEMORY.md", entries("Invoices go out on the first working day.", secret))
    write(fx, "knowledge/vendors.md", "# Vendors\n%s\n" % mark)
    await run(fx)
    await purge(await item_with("The safe combination hint"))
    await purge(await item(file_key("knowledge/vendors.md")))
    assert await fingerprints() == []
    write(fx, "memories/USER.md", entries(secret))
    write(fx, "knowledge/vendors-copy.md", "# Vendors\n%s\n" % mark)
    second = await run(fx)
    recs = await records(second["run_id"])
    assert recs[file_key("knowledge/vendors.md")].reason == FILE_PURGED
    assert recs[file_key("knowledge/vendors-copy.md")].reason == SAME_TEXT
    assert [r.reason for k, r in recs.items() if "USER.md#entry-" in k] == [SAME_TEXT]
    assert await bj.nowhere(mark) == []


async def test_fingerprints_are_k1_only(fx):
    mark = "zqkone62"
    write(fx, "knowledge/a.md", "# A\nFirst %s.\n" % mark)
    write(fx, "memories/MEMORY.md", entries("Entry one about %s." % mark, "Entry two."))
    await run(fx)
    shas = []
    for found in (await item(file_key("knowledge/a.md")), await item_with("Entry one about")):
        shas.append(found.source_sha256)
        await purge(found)
    fps = await fingerprints()
    assert len(fps) == 2 and all(re.fullmatch(r"k1:[0-9a-f]{8}:[0-9a-f]{64}", fp) for fp in fps)
    for sha in shas:  # no plain hash of a purged item's bytes is kept anywhere
        assert await bj.nowhere(sha) == []
    assert await bj.nowhere(mark) == []


async def test_decisions_apply_purge_stores_live_fingerprint(fx, tmp_path):
    mark = "zqapply62"
    write(fx, "knowledge/vendors.md", "# Vendors\nThe preferred courier is %s.\n" % mark)
    first = await run(fx)
    found = await item(file_key("knowledge/vendors.md"))
    async with session_scope() as s:  # the test copy's decision, as a version 3 file names it
        s.add(BrainTombstone(source_key=found.source_key, purged_by=ADMIN[0], reason=FILE_PURGED))
    out = tmp_path / "decisions.json"
    await imp.decisions_export(str(out))
    async with session_scope() as s:
        await s.execute(BrainTombstone.__table__.delete())
    result = await imp.decisions_apply(str(out), first["run_id"], actor=ADMIN[0], apply=True)
    assert result["removals"] == {"purged": 1}
    [fp] = await fingerprints()
    assert fp == identity.purge_fp(identity.purge_key(), found.source_sha256)
    os.rename(os.path.join(fx["profile"], "knowledge/vendors.md"), os.path.join(fx["profile"], "knowledge/v2.md"))
    second = await run(fx)
    assert (await records(second["run_id"]))[file_key("knowledge/v2.md")].reason == SAME_TEXT
    assert await bj.nowhere(mark) == []


async def test_an_item_back_under_a_purged_key_is_kept_from_agents_and_people_before_r8(fx):
    """The read guard (spec 10): a restore of a dump older than a purge brings the item back under its purged key.
    Before the start-up check quarantines it (R8), no agent reads it and only an admin lists it."""
    from app.brain.policy import POLICIES, PersonScope, agent_items, person_items

    mark = "zqpurgeguard62"
    write(fx, "knowledge/guard.md", "# Guard\nThe gate code is %s.\n" % mark)
    await run(fx)
    found = await item(file_key("knowledge/guard.md"))
    async with session_scope() as s:  # approved, then (what a restore of an older dump leaves) its key tombstoned
        target = await s.get(MemoryItem, found.id)
        await store.review_item(s, target, decision="approve", actor=ADMIN[0])
        s.add(BrainTombstone(source_key=found.source_key, purged_by=ADMIN[0], reason=FILE_PURGED))
    async with session_scope() as s:
        for agent, policy in POLICIES.items():
            readable = await s.scalar(select(MemoryItem.id).where(MemoryItem.id == found.id, agent_items(policy)))
            assert readable is None, agent
        for role, sensitive in (("viewer", False), ("operator", True), ("admin", False)):
            visible = await s.scalar(select(MemoryItem.id).where(MemoryItem.id == found.id,
                                                                 person_items(PersonScope(role, sensitive))))
            assert (visible is not None) == (role == "admin"), role
        hits = await store.AgentMemory(s, "supervisor").relevant(mark)
        assert not hits
