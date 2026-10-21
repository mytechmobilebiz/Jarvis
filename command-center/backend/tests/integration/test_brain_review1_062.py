"""0.6.2 review round 1: the confirmed findings outside the matching rules (findings-062-round1.md R15-R36), as
regressions. Synthetic data only; unique markers. Every test asserts the SAFE outcome and names its finding.

Stage 1, restore and durability (R20-R28, and R5, R16, R21, R31, R33 where they touch restore.ps1, the decisions file
or a rollback): a restore of an older backup re-applies every withhold, purge, release, title choice, access
narrowing and audit entry made after that backup; a rollback keeps an admin's title choice; an older importer is
refused on a 0.6.2 database.

The restore tests take a real pg_dump of the test database (the restore point: "the nightly backup"), make the
admin's decisions, export what restore.ps1 exports, pg_restore with restore.ps1's own flags, and apply what
restore.ps1 applies, in its order (restore-carry apply, then decisions-apply --removals-only), before any reader.
tests/unit/test_restore_ps1.py runs restore.ps1 itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import asyncpg
import pytest
from sqlalchemy import select, text

from app import restore_carry
from app.bootstrap import check_withheld_copies
from app.brain import identity, store
from app.brain import importer as imp
from app.db import session_scope
from app.models import (
    ArchiveSession,
    AuditLog,
    BrainPurgedContent,
    BrainPurgedItem,
    ImportRun,
    MemoryItem,
    User,
)

from .. import brain_jarvis as bj
from .test_brain_agents import allow_ai_context
from .test_brain_api import USERS, as_, client  # noqa: F401 - client is a fixture
from .test_brain_identity_migration import _base
from .test_brain_schema import _container, _migrate, _pg
from .test_brain_upgrade_062 import LEGACY_SCRIPT, _release_061
from .test_brain_withhold_identity import T, conversation, message_of, run, secret, stones

pytestmark = pytest.mark.usefixtures("clean_db")

OWN_DB = os.environ["TEST_DATABASE_URL"].rsplit("/", 1)[1]
DUMP = "/tmp/%s_review1_point.dump" % OWN_DB
TITLE = "Divorce settlement with Mrs Quokka zqrevtitle"


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


@pytest.fixture
def restore_point(migrated, tmp_path, client):
    """A real pg_dump of the test database, and restore.ps1's pg_restore of it with the carry-over around it."""
    container = _container()

    class Point:
        def take(self) -> None:
            done = _pg(container, "pg_dump", "-U", "cc", "-Fc", "-f", DUMP, OWN_DB)
            assert done.returncode == 0, done.stderr

        async def export(self) -> tuple[str, str]:
            """What restore.ps1 exports before it stops anything."""
            removals = str(tmp_path / ("removals-%d.json" % len(os.listdir(tmp_path))))
            carry = str(tmp_path / ("access-%d.json" % len(os.listdir(tmp_path))))
            await imp.decisions_export(removals, removals_only=True)
            await restore_carry.export(carry)
            return removals, carry

        async def restore(self, files: tuple[str, str] | None = None) -> dict:
            """pg_restore (restore.ps1's flags), then, before any reader, what restore.ps1 applies: restore-carry
            apply (audit, migrate, access) and decisions-apply --removals-only."""
            await migrated.dispose()
            done = _pg(container, "pg_restore", "-U", "cc", "-d", OWN_DB, "--clean", "--if-exists",
                       "--single-transaction", "--exit-on-error", DUMP)
            await migrated.dispose()
            assert done.returncode == 0, done.stderr
            out: dict = {}
            if files is not None:
                removals, carry = files
                out["carry"] = await restore_carry.apply(carry, apply=True)
                out["removals"] = await imp.decisions_apply(removals, None, actor=None, apply=True,
                                                            removals_only=True)
            assert await check_withheld_copies() is True  # the api's start-up, after the re-apply
            client.brain_tokens.clear()  # every sign-in ended: the test's roles sign in again
            client.cookies.clear()
            return out

    yield Point()
    _pg(container, "rm", "-f", DUMP)


async def session_titles(client, who: str = "operator") -> dict[str, str]:
    await as_(client, who)
    r = await client.get("/api/brain/sessions", params={"limit": 200})
    assert r.status_code == 200, r.text
    return {s["source_session_id"]: s["title"] for s in r.json()["sessions"]}


async def title_flag(sid: str) -> object:
    async with session_scope() as s:
        sess = await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == sid))
        return None if sess is None else (sess.flags or {}).get("title_withheld")


async def seen_by(client, mark: str) -> dict[str, bool]:
    """Which readers show the marker: search, the sessions list, items and the Supervisor."""
    seen: dict[str, bool] = {}
    for who in ("viewer", "operator", "admin", "admin+"):
        await as_(client, who)
        seen["search:" + who] = mark in (await client.get("/api/brain/search", params={"q": mark})).text
        seen["items:" + who] = mark in (await client.get("/api/brain/items", params={"q": mark})).text
        if who != "viewer":
            seen["sessions:" + who] = mark in (await client.get("/api/brain/sessions",
                                                                params={"limit": 200})).text
    async with session_scope() as s:
        mem = store.AgentMemory(s, "supervisor")
        seen["supervisor relevant"] = any(mark in (i.title + i.body) for i in await mem.relevant(mark))
        seen["supervisor prompt_block"] = mark in await mem.prompt_block("note " + mark)
        hits, _ = await mem.history(mark)
        seen["supervisor history"] = any(mark in (h.message.content or "") for h in hits)
    return {k: v for k, v in seen.items() if v}


# ======================= R20: purges of items without a source key ==============================================


async def in_items(mark: str) -> int:
    async with session_scope() as s:
        return int(await s.scalar(text(
            "SELECT (SELECT count(*) FROM memory_items WHERE title ILIKE :m OR body ILIKE :m) + "
            "(SELECT count(*) FROM memory_item_versions WHERE title ILIKE :m OR body ILIKE :m)"),
            {"m": "%" + mark + "%"}) or 0)


async def test_r20_restore_purges_again_a_purged_promoted_and_a_purged_manual_item(client, fx, restore_point):
    """R20 (durability #0): an admin purges a promoted item and a manually created one after the nightly backup.
    restore.ps1 must purge them again: they have no source key, so before this fix no tombstone carried them."""
    mark_p, mark_m = "zqrevpurgeprom", "zqrevpurgemanual"
    ids = conversation(fx["db"], "r20_s", T, [("user", "What did the lawyer say?"), ("assistant", secret(mark_p))])
    await run(fx)
    await allow_ai_context()
    [m] = await message_of("r20_s", ids[1])
    await as_(client, "admin")
    r = await client.post("/api/brain/messages/%d/promote" % m.id, json={"kind": "project",
                                                                           "title": "Lawyer note " + mark_p})
    assert r.status_code == 201, r.text
    promoted = r.json()["item"]["id"]
    r = await client.post("/api/brain/items", json={"kind": "business", "title": "Private note " + mark_m,
                                                    "body": "Keep this private: " + secret(mark_m)})
    assert r.status_code == 201, r.text
    manual = r.json()["item"]["id"]
    for item_id in (promoted, manual):
        assert (await client.post("/api/brain/items/%d/review" % item_id, json={"decision": "approve"})
                ).status_code == 200
    restore_point.take()  # the nightly backup: both items exist, approved
    for item_id in (promoted, manual):
        assert (await client.delete("/api/brain/items/%d" % item_id)).status_code == 200
        assert (await client.delete("/api/brain/items/%d/purge" % item_id)).status_code == 200
    assert await bj.nowhere(mark_m) == [] and await in_items(mark_p) == 0
    files = await restore_point.export()
    assert len(json.load(open(files[0]))["purged_items"]) == 2
    out = await restore_point.restore(files)
    print("\nR20 re-apply:", out["removals"]["removals"], out["removals"]["purged_items_not_applied"])
    async with session_scope() as s:
        back = (await s.scalars(select(MemoryItem.id).where(MemoryItem.id.in_([promoted, manual])))).all()
        ledger = {(p.item_id, p.purged_by) for p in (await s.scalars(select(BrainPurgedItem))).all()}
    assert back == [], "purged items came back after the restore"
    assert out["removals"]["removals"].get("purged") == 2 and out["removals"]["purged_items"] == {"purged": 2}
    assert not out["removals"]["purged_items_not_applied"]
    assert await bj.nowhere(mark_m) == [] and await in_items(mark_p) == 0
    assert await seen_by(client, mark_m) == {}
    assert not [k for k in await seen_by(client, mark_p) if k.startswith("items:") or "relevant" in k
                or "prompt_block" in k]  # the message itself stays readable: only its promoted copy was purged
    assert ledger == {(promoted, "admin@test.local"), (manual, "admin@test.local")}  # who purged, kept (R28)


async def test_r20_restore_purges_again_a_purged_incoming_version(client, fx, restore_point):
    """R20: a changed knowledge file's new version waits for review as an item without a source key
    (flags.replaces_source_key); the admin purges it after the backup. After the restore it is purged again, and the
    next import does not offer it again (its hash is rejected on the item it would replace)."""
    mark = "zqrevincoming"
    path = os.path.join(fx["profile"], "knowledge", "pricing.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Pricing\nThe standard rate is 100 dollars.\n")
    await run(fx)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Pricing\nThe secret discount for %s is 40 percent.\n" % mark)
    await run(fx)
    async with session_scope() as s:
        incoming = await s.scalar(select(MemoryItem).where(MemoryItem.source_key.is_(None),
                                                           MemoryItem.body.contains(mark)))
        assert incoming is not None
        incoming_id = incoming.id
    restore_point.take()
    await as_(client, "admin")
    assert (await client.delete("/api/brain/items/%d" % incoming_id)).status_code == 200
    assert (await client.delete("/api/brain/items/%d/purge" % incoming_id)).status_code == 200
    assert await bj.nowhere(mark) == []
    out = await restore_point.restore(await restore_point.export())
    later = await run(fx)
    async with session_scope() as s:
        back = (await s.scalars(select(MemoryItem.id).where(MemoryItem.body.contains(mark)))).all()
    print("\nR20b re-apply:", out["removals"]["removals"], "| next import:", later["report"]["by_action"])
    assert back == [], "the purged incoming version came back after the restore"
    assert await bj.nowhere(mark) == [] and await seen_by(client, mark) == {}


async def test_r20_a_restored_item_with_the_purged_id_but_another_creation_time_is_never_purged(client, fx,
                                                                                                restore_point):
    """R20, the opposite direction: a restore resets the id sequence, so an id alone can name another item. The
    re-apply purges only the very item (same id and creation time); an unrelated item with that id stays."""
    await as_(client, "admin")
    r = await client.post("/api/brain/items", json={"kind": "business", "title": "Keep me zqrevkeepme",
                                                    "body": "Unrelated and wanted."})
    keep = r.json()["item"]["id"]
    restore_point.take()
    files = await restore_point.export()
    data = json.load(open(files[0]))
    data["purged_items"] = [{"item": keep, "created_at": "2026-01-01T00:00:00+00:00", "by": "admin@test.local",
                             "at": "2026-01-02T00:00:00+00:00"}]
    with open(files[0], "w", encoding="utf-8") as f:
        json.dump(data, f)
    out = await restore_point.restore(files)
    async with session_scope() as s:
        assert await s.get(MemoryItem, keep) is not None
    assert out["removals"]["removals"] == {} and out["removals"]["purged_items"] == {"not here": 1}


# ======================= R21 / R16: the hide-title decision ====================================================


async def test_r21_r16_title_stays_hidden_after_rollback_of_the_creating_run_and_a_reimport(client, fx):
    """R21 (1), R16: withholding the first user message hides the title by default; a rollback of the run that
    created the conversation, then a re-import, keeps it hidden (the choice is on the admin's stone). Right after the
    withhold no rollback copy keeps the title either (R16 (a))."""
    mark = "zqrevtitleroll1"
    ids = conversation(fx["db"], "r21_t1", T, [("user", secret(mark)), ("assistant", "Understood.")],
                       title="Settlement %s with the vendor" % mark)
    first = await run(fx)
    [m] = await message_of("r21_t1", ids[0])
    await as_(client, "admin")
    r = await client.post("/api/brain/messages/%d/withhold" % m.id)
    assert r.status_code == 200 and r.json()["title_hidden"] is True, r.text
    assert await bj.nowhere(mark) == []  # not in archive_sessions.title nor in import_records.before
    await imp.rollback(first["run_id"], actor="admin@test.local", discard_reviewed=True, confirm=True)
    await run(fx)
    [again] = await message_of("r21_t1", ids[0])
    assert again.withheld
    assert (await session_titles(client))["r21_t1"] == "" and await title_flag("r21_t1") is True
    assert await bj.nowhere(mark) == []


async def test_r21_r16_title_stays_hidden_after_rollback_of_a_run_that_updated_the_conversation(client, fx):
    """R21 (2), R16 (b): run 2 updated the conversation before the admin hid its title; rolling run 2 back puts its
    `before` back, but never the hidden title, and the next import keeps it hidden."""
    mark = "zqrevtitleroll2"
    ids = conversation(fx["db"], "r21_t2", T, [("user", secret(mark)), ("assistant", "Understood.")],
                       title="Settlement %s with the vendor" % mark)
    await run(fx)
    bj.add_row(fx["db"], "r21_t2", "user", "One more thing.", T + 10)
    second = await run(fx)
    [m] = await message_of("r21_t2", ids[0])
    await as_(client, "admin")
    assert (await client.post("/api/brain/messages/%d/withhold" % m.id)).json()["title_hidden"] is True
    await imp.rollback(second["run_id"], actor="admin@test.local", confirm=True)
    assert (await session_titles(client))["r21_t2"] == ""
    await run(fx)
    assert (await session_titles(client))["r21_t2"] == "" and await bj.nowhere(mark) == []


async def test_r21_r16_restore_keeps_an_explicit_hide_title_on_another_message(client, fx, restore_point):
    """R21 (3), R16 (c): the admin withholds a message that is NOT the first user message and ticks 'Hide the
    conversation title' after the backup; the restore keeps the title hidden."""
    mark = "zqrevtitlerest"
    ids = conversation(fx["db"], "r21_t3", T, [("user", "Hello."), ("assistant", "Hi."),
                                               ("user", secret(mark)), ("assistant", "Noted.")], title=TITLE)
    await run(fx)
    restore_point.take()
    [m] = await message_of("r21_t3", ids[2])
    await as_(client, "admin")
    r = await client.post("/api/brain/messages/%d/withhold" % m.id, json={"hide_title": True})
    assert r.status_code == 200 and r.json()["title_hidden"] is True, r.text
    await restore_point.restore(await restore_point.export())
    [back] = await message_of("r21_t3", ids[2])
    assert back.withheld
    assert (await session_titles(client))["r21_t3"] == "", "the explicit hide-title decision was lost by the restore"


async def test_r21_r22_restore_keeps_the_default_title_hide_after_a_compaction(client, fx, restore_point):
    """R21 (4), R22: the admin withholds the first user message (title hidden by default); Hermes then compacts in
    place and the next import stores the live copy withheld, a member stone whose row-form key sorts before the
    admin's uid-form key. The restore applies the admin's own withhold first: it withholds the message itself (not a
    hold), and the title stays hidden."""
    from app.models import AuditLog as Audit
    mark = "zqrevtitleorder"
    sid = "r21_t4"
    conversation(fx["db"], sid, T, [("user", secret(mark)), ("assistant", "Noted.")], title=TITLE)
    await run(fx)
    restore_point.take()
    row = (await message_of(sid, 1))[0]
    await as_(client, "admin")
    r = await client.post("/api/brain/messages/%d/withhold" % row.id)
    assert r.status_code == 200 and r.json()["title_hidden"] is True, r.text
    bj.hermes_compact(fx["db"], sid, head_n=0, tail_n=2, ts=T + 100)
    await run(fx)
    assert (await session_titles(client))[sid] == ""
    files = await restore_point.export()
    keys = [r["source_key"] for r in json.load(open(files[0]))["removals"]]
    assert keys == sorted(keys) and len(keys) >= 2  # the member's key comes first in the file
    out = await restore_point.restore(files)
    [back] = await message_of(sid, 1)
    async with session_scope() as s:
        actions = [a.action for a in (await s.scalars(select(Audit).where(
            Audit.action.like("archive.message_%")).order_by(Audit.id))).all()]
    print("\nR21-4 re-apply:", out["removals"]["removals"], "| audit:", actions)
    assert back.withheld and "review" not in (back.flags.get("withheld_by_admin") or {})
    assert "archive.message_withheld" in actions
    assert (await session_titles(client))[sid] == "" and await title_flag(sid) is True


# ======================= R22: the re-apply order ===============================================================


async def test_r22_restore_keeps_the_admins_group_and_a_release(client, fx, restore_point):
    """R22 (durability #2): a CLI /branch child keeps the uid; its copy of the withheld tool row is pruned into a twin
    and a stub; the admin withholds the parent's row and releases the stub after the backup. The restore must keep the
    admin's own stone the group's root and the release in force (the stub stays readable, nothing is listed)."""
    mark = "zqrevinvert"
    parent, child = "r22_p", "r22_q"
    bj.add_session(fx["db"], parent, started=T)
    bj.add_row(fx["db"], parent, "user", "Run the vendor report.", T + 1)
    bj.add_row(fx["db"], parent, "tool", "REPORT " + secret(mark), T + 2, tool_call_id="call_1",
               tool_name="terminal")
    copies = bj.cli_branch(fx["db"], parent, child, T + 10)
    stub_row = copies[1]
    bj.prune_twin(fx["db"], child, stub_row)
    await run(fx)
    restore_point.take()
    [orig] = await message_of(parent, 2)
    await as_(client, "admin")
    assert (await client.post("/api/brain/messages/%d/withhold" % orig.id)).status_code == 200
    [stub] = await message_of(child, stub_row)
    assert (await client.post("/api/brain/holds/%d/release" % stub.id)).status_code == 200
    before = {k: (t.anchor_key, t.review) for k, t in (await stones()).items()}
    out = await restore_point.restore(await restore_point.export())
    after = {k: (t.anchor_key, t.review) for k, t in (await stones()).items()}
    [stub_after] = await message_of(child, stub_row)
    await as_(client, "admin")
    holds = (await client.get("/api/brain/holds")).json()["holds"]
    print("\nR22 re-apply:", out["removals"]["removals"], out["removals"]["releases"])
    print("R22 stones before / after:", before, after)
    assert after[orig.source_key][0] == "", "the admin's own stone joined another group"
    assert all(a in ("", orig.source_key) for a, _ in after.values()), "a copy became a group's root"
    assert not stub_after.withheld or stub_after.flags["withheld_by_admin"].get("released_at"), \
        "the admin's release was lost: the stub is held for review again"
    assert holds == []
    await run(fx)  # and the next import keeps it so
    [stub_next] = await message_of(child, stub_row)
    assert not stub_next.withheld


async def test_r22_a_member_withheld_before_its_admins_message_never_becomes_the_root(client, fx):
    """R22, whatever the order: decisions-apply reaches a member's row (a copy whose stone joined the admin's group)
    before the admin's own message is withheld here. review.withhold_message then roots the group at the admin's
    stone: the admin's message gets its own stone (anchor ''), never one anchored to its copy."""
    from app.brain import review
    mark = "zqrevrootfirst"
    parent, child = "r22r_p", "r22r_q"
    bj.add_session(fx["db"], parent, started=T)
    bj.add_row(fx["db"], parent, "user", "Run the vendor report.", T + 1)
    bj.add_row(fx["db"], parent, "tool", "REPORT " + secret(mark), T + 2, tool_call_id="call_1",
               tool_name="terminal")
    copies = bj.cli_branch(fx["db"], parent, child, T + 10)
    await run(fx)
    [orig] = await message_of(parent, 2)
    [copy] = await message_of(child, copies[1])
    assert identity.row_uid(orig) == identity.row_uid(copy) and orig.source_key != copy.source_key
    async with session_scope() as s:  # the member's stone, as the file carried it, applied first
        sid = await s.scalar(select(ArchiveSession.source_session_id).where(ArchiveSession.id == copy.session_id))
        await identity.put_stone(s, identity.binding_of(copy, sid, anchor=orig.source_key, via="copied by JARVIS",
                                                        uid_known=True), actor=imp.RESTORE_ACTOR)
    async with session_scope() as s:
        row = await s.get(type(copy), copy.id)
        await review.withhold_message(s, row, actor=imp.RESTORE_ACTOR, cause="restore", via="restore")
    after = {k: t.anchor_key for k, t in (await stones()).items()}
    print("\nR22 root stones:", after)
    assert after[orig.source_key] == "", "the admin's own message joined its copy's group"
    assert copy.source_key not in after.values(), "a copy became a group's root"
    assert await bj.nowhere(mark) == []


# ======================= R5: holds still waiting for review =====================================================


async def test_r5_restore_keeps_a_hold_waiting_for_review(client, fx, restore_point):
    """R5 (resurrection #4), the restore path: a row held for review after the backup (a later verbatim repeat
    without a uid) is held again after the restore, not readable."""
    mark = "zqrevpending"
    ids = conversation(fx["db"], "r5_s", T, [("user", "Hello."), ("user", secret(mark)), ("assistant", "Noted.")],
                       uid=False)
    await run(fx)
    restore_point.take()
    [m] = await message_of("r5_s", ids[1])
    await as_(client, "admin")
    assert (await client.post("/api/brain/messages/%d/withhold" % m.id)).status_code == 200
    bj.add_row(fx["db"], "r5_s", "user", secret(mark), T + 50, uid=False)  # a later verbatim repeat
    await run(fx)
    pending = {k for k, st in (await stones()).items() if st.review == identity.PENDING}
    assert pending, "precondition: the repeat is held for review"
    await restore_point.restore(await restore_point.export())
    assert {k for k, st in (await stones()).items() if st.review == identity.PENDING} >= pending
    assert await bj.nowhere(mark) == [] and await seen_by(client, mark) == {}
    await run(fx)  # the next import brings the repeat back: held again, never readable
    assert {k for k, st in (await stones()).items() if st.review == identity.PENDING} >= pending
    assert await bj.nowhere(mark) == [] and await seen_by(client, mark) == {}


# ======================= R25: purge fingerprints ===============================================================


async def test_r25_restore_keeps_the_purge_fingerprint_of_an_item_imported_after_the_backup(client, fx,
                                                                                            restore_point):
    """R25 (durability #5): the backup predates the import of a knowledge file the admin then purged. After the
    restore JARVIS renames the file: the next import leaves it out, as without a restore (its keyed fingerprint came
    back with the restore file)."""
    mark = "zqrevpurgefp"
    await run(fx)
    restore_point.take()
    kdir = os.path.join(fx["profile"], "knowledge")
    os.makedirs(kdir, exist_ok=True)
    with open(os.path.join(kdir, "courier.md"), "w", encoding="utf-8") as f:
        f.write("# Courier\nThe gate code is %s.\n" % mark)
    await run(fx)
    await as_(client, "admin")
    item_id = (await client.get("/api/brain/items", params={"q": mark})).json()["items"][0]["id"]
    assert (await client.delete("/api/brain/items/%d" % item_id)).status_code == 200
    assert (await client.delete("/api/brain/items/%d/purge" % item_id)).status_code == 200
    async with session_scope() as s:
        fps = {(p.fp, p.purged_by, p.purged_at) for p in (await s.scalars(select(BrainPurgedContent))).all()}
    out = await restore_point.restore(await restore_point.export())
    async with session_scope() as s:
        fps_after = {(p.fp, p.purged_by, p.purged_at) for p in (await s.scalars(select(BrainPurgedContent))).all()}
    assert out["removals"]["purge_fingerprints"] == {"carried": 1}
    assert fps_after == fps  # the same fingerprint, who made it and when
    os.rename(os.path.join(kdir, "courier.md"), os.path.join(kdir, "courier-old.md"))
    later = await run(fx)
    print("\nR25 next import after the rename:", later["report"]["by_action"])
    assert await bj.nowhere(mark) == [], "the purged file came back under its new name after the restore"
    assert await seen_by(client, mark) == {}


async def test_r25_a_fingerprint_made_with_another_database_password_is_not_carried(client, fx, restore_point):
    """R25, the opposite direction: a fingerprint keyed with another password would only make every import refuse;
    it is counted and left out."""
    restore_point.take()
    files = await restore_point.export()
    data = json.load(open(files[0]))
    data["purge_fingerprints"] = [{"fp": "k1:00000000:" + "ab" * 32, "source_key": "", "kind": "file",
                                   "by": "admin@test.local", "at": "2026-01-02T00:00:00+00:00", "via": "purge"}]
    with open(files[0], "w", encoding="utf-8") as f:
        json.dump(data, f)
    out = await restore_point.restore(files)
    assert out["removals"]["purge_fingerprints"] == {"other database password": 1}
    async with session_scope() as s:
        assert (await s.scalars(select(BrainPurgedContent.fp))).all() == []
    await run(fx)  # the importer still runs (no fingerprint of another key was stored)


# ======================= R26: an older importer on a 0.6.2 database ============================================


@pytest.fixture
async def scratch(settings):
    name = OWN_DB + "_r1old"
    admin = await asyncpg.connect(_base(settings) + "/postgres")
    try:
        await admin.execute("DROP DATABASE IF EXISTS %s WITH (FORCE)" % name)
        await admin.execute("CREATE DATABASE %s" % name)
    finally:
        await admin.close()
    try:
        yield _base(settings) + "/" + name
    finally:
        admin = await asyncpg.connect(_base(settings) + "/postgres")
        try:
            await admin.execute("DROP DATABASE IF EXISTS %s WITH (FORCE)" % name)
        finally:
            await admin.close()


async def test_r26_the_0_6_1_importer_is_refused_on_a_0_6_2_database(scratch, tmp_path):
    """R26 (durability #6): back to 0.6.1 code on a 0.6.2 database, a 0.6.1 import is refused before any change
    (it would store Hermes's live copy of a withheld message with its text, which the 0.6.2 start-up cannot see)."""
    backend = _release_061(tmp_path / "release")
    assert await _migrate(scratch)  # the 0.6.2 schema, with 0008
    fx = bj.mini(tmp_path / "fx")
    conversation(fx["db"], "r26_s", T, [("user", "Hello."), ("assistant", secret("zqrevold061"))])
    env = {**os.environ, "DATABASE_URL": scratch.replace("postgresql://", "postgresql+asyncpg://", 1),
           "PYTHONPATH": str(backend)}
    script = backend.parent / "legacy_061.py"
    script.write_text(LEGACY_SCRIPT)
    done = subprocess.run([sys.executable, str(script), "run", fx["db"], fx["profile"], fx["content"]],
                          cwd=str(backend), env=env, capture_output=True, text=True, timeout=300)
    print("\nR26 0.6.1 import:", done.returncode, (done.stdout + done.stderr)[-300:])
    conn = await asyncpg.connect(scratch)
    try:
        runs = await conn.fetchval("SELECT count(*) FROM import_runs")
        rows = await conn.fetchval("SELECT count(*) FROM archive_messages")
    finally:
        await conn.close()
    assert done.returncode != 0 and "0.6.2 importer (1.1.0 or later)" in done.stdout + done.stderr
    assert runs == 0 and rows == 0
    # The 0.6.2 importer still runs on it.
    engine_url = scratch.replace("postgresql://", "postgresql+asyncpg://", 1)
    done = subprocess.run([sys.executable, "-c", (
        "import asyncio, sys\n"
        "from app.brain import importer as imp\n"
        "from app.db import dispose_engine\n"
        "async def main():\n"
        "    out = await imp.run(imp.SourcePaths(db=sys.argv[1], profile=sys.argv[2], content=sys.argv[3]), "
        "imp.Selection(all=True))\n"
        "    print('run', out['run_id'])\n"
        "    await dispose_engine()\n"
        "asyncio.run(main())\n"), fx["db"], fx["profile"], fx["content"]],
        env={**os.environ, "DATABASE_URL": engine_url}, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0 and done.stdout.startswith("run "), done.stdout + done.stderr


async def test_r26_runs_written_without_a_version_or_by_this_importer_are_accepted():
    """R26, the opposite direction: only a version older than 1.1.0 is refused."""
    async with session_scope() as s:
        s.add(ImportRun(status="completed"))
        s.add(ImportRun(status="completed", importer_version=imp.IMPORTER_VERSION))
    async with session_scope() as s:
        assert len((await s.scalars(select(ImportRun.id))).all()) == 2
    with pytest.raises(Exception, match="0.6.2 importer"):
        async with session_scope() as s:
            s.add(ImportRun(status="completed", importer_version="1.0.1"))


# ======================= R27: users and permissions ============================================================


async def _users() -> dict[str, tuple]:
    async with session_scope() as s:
        return {u.email: (u.role, u.is_active, u.can_access_sensitive) for u in (await s.scalars(select(User))).all()}


async def test_r27_restore_reapplies_a_revocation_and_a_deactivation_and_ends_old_sign_ins(client, fx,
                                                                                         restore_point):
    """R27 (durability #7): after the backup an admin revokes an operator's sensitive-data permission and
    deactivates another operator. The restore applies both again, ends every sign-in session (the deactivated
    operator's old cookie no longer works) and lists what it does revert."""
    from app.config import get_settings
    conversation(fx["db"], "r27_s", T, [("user", "Hello."), ("assistant", "Plain text.")])
    await run(fx)
    await as_(client, "operator")  # the deactivated operator's browser session, made before the backup
    old_cookie = client.brain_tokens["operator"]
    await as_(client, "operator+")
    await as_(client, "viewer")  # the viewer's, whose password an admin resets after the backup
    viewer_cookie = client.brain_tokens["viewer"]
    restore_point.take()
    await as_(client, "admin")
    async with session_scope() as s:
        ids = {u.email: u.id for u in (await s.scalars(select(User))).all()}
    op, ops = USERS["operator"][0], USERS["operator+"][0]
    assert (await client.patch("/api/users/%d" % ids[ops], json={"can_access_sensitive": False})).status_code == 200
    assert (await client.patch("/api/users/%d" % ids[op], json={"is_active": False})).status_code == 200
    # and a widening change, which the restore does revert (listed): the viewer becomes an operator
    assert (await client.patch("/api/users/%d" % ids[USERS["viewer"][0]], json={"role": "operator"})
            ).status_code == 200
    assert (await client.patch("/api/users/%d" % ids[USERS["viewer"][0]],
                               json={"password": "a-new-password-after-the-backup"})).status_code == 200
    out = await restore_point.restore(await restore_point.export())
    carry = out["carry"]
    print("\nR27 carry:", json.dumps(carry, indent=1))
    users = await _users()
    assert users[ops][2] is False and users[op][1] is False
    assert users[USERS["viewer"][0]][0] == "viewer"
    assert any(USERS["viewer"][0] in line and "role raised" in line for line in carry["reverted"])
    assert carry["sessions_ended"] >= 1
    client.cookies.clear()
    client.cookies.set(get_settings().session_cookie_name, old_cookie)
    r = await client.get("/api/brain/sessions", params={"limit": 5})
    assert r.status_code == 401, "the deactivated operator's old sign-in works again after the restore"
    assert any(USERS["viewer"][0] in line and "password changed" in line for line in carry["reverted"])
    client.cookies.clear()
    client.cookies.set(get_settings().session_cookie_name, viewer_cookie)
    r = await client.get("/api/brain/search", params={"q": "anything"})
    assert r.status_code == 401, "a sign-in ended by a password reset after the backup works again after the restore"
    async with session_scope() as s:
        audits = [(a.actor, a.action) for a in (await s.scalars(select(AuditLog).where(
            AuditLog.actor == restore_carry.ACTOR, AuditLog.action == "user.updated"))).all()]
    assert len(audits) == 2


async def test_r27_restore_never_leaves_no_active_admin_and_leaves_another_installations_users_alone(client, fx,
                                                                                                   restore_point):
    """R27, the opposite direction: (a) a narrowing that would leave no active admin is listed, not applied; (b) a
    backup of another installation (users with other ids or creation times) is left as it is."""
    restore_point.take()
    files = await restore_point.export()
    data = json.load(open(files[1]))
    admin = [u for u in data["users"] if u["role"] == "admin" and u["is_active"]]
    for u in data["users"]:  # as if every admin had been deactivated after the backup
        if u["role"] == "admin":
            u["is_active"] = False
    with open(files[1], "w", encoding="utf-8") as f:
        json.dump(data, f)
    out = await restore_point.restore(files)
    users = await _users()
    assert any(users[u["email"]][1] for u in admin), "the restore left no active admin"
    assert out["carry"]["skipped"]
    restore_point.take()
    files = await restore_point.export()
    data = json.load(open(files[1]))
    for u in data["users"]:
        u["id"] += 1000
        u["can_access_sensitive"] = False
    with open(files[1], "w", encoding="utf-8") as f:
        json.dump(data, f)
    before = await _users()
    out = await restore_point.restore(files)
    assert await _users() == before and out["carry"]["kept"] == [] and out["carry"]["access_not_carried"]


# ======================= R28: who decided, and when; the audit log =============================================


async def test_r28_restore_keeps_who_withheld_and_when_and_the_audit_entries_of_the_undone_period(client, fx,
                                                                                                restore_point):
    """R28 (durability #8): after the restore, the stone still says who withheld the message, when and how
    ('admin'), and the archive.message_withheld audit entry made after the backup is still there with its id; the
    re-apply adds entries of its own, after it."""
    mark = "zqrevwho"
    ids = conversation(fx["db"], "r28_s", T, [("user", "Hello."), ("assistant", secret(mark))])
    await run(fx)
    restore_point.take()
    [m] = await message_of("r28_s", ids[1])
    await as_(client, "admin")
    assert (await client.post("/api/brain/messages/%d/withhold" % m.id)).status_code == 200
    st = (await stones())[m.source_key]
    before = (st.purged_by, st.purged_at, st.via)
    files = await restore_point.export()
    async with session_scope() as s:
        audit_before = [(a.id, a.at, a.actor, a.action) for a in (await s.scalars(
            select(AuditLog).order_by(AuditLog.id))).all()]
    assert ("admin@test.local", "archive.message_withheld") in {(a[2], a[3]) for a in audit_before}
    await restore_point.restore(files)
    st2 = (await stones())[m.source_key]
    assert (st2.purged_by, st2.purged_at, st2.via) == before, "who withheld the message, and when, was lost"
    async with session_scope() as s:
        audit_after = [(a.id, a.at, a.actor, a.action) for a in (await s.scalars(
            select(AuditLog).order_by(AuditLog.id))).all()]
    assert audit_after[:len(audit_before)] == audit_before, "audit entries made after the backup were lost"
    assert {a[2] for a in audit_after[len(audit_before):]} == {restore_carry.ACTOR}
    again = await restore_carry.apply(files[1], apply=True)  # the commands restore.ps1 prints, run a second time
    assert again["audit_kept"] == 0 and again["audit_not_kept"] == "" and again["kept"] == []


async def test_r28_audit_entries_are_not_put_back_into_another_databases_backup(client, fx, restore_point):
    """R28, the opposite direction: when the restored backup's newest audit entry is not in the file (another
    database), nothing is put back."""
    await as_(client, "admin")
    restore_point.take()
    files = await restore_point.export()
    data = json.load(open(files[1]))
    for e in data["audit"]:
        e["action"] = "something.else"
    data["audit"].append({"id": 10 ** 6, "at": "2026-01-01T00:00:00+00:00", "actor": "x", "action": "y",
                          "target_type": "", "target_id": "", "detail": {}, "ip": ""})
    with open(files[1], "w", encoding="utf-8") as f:
        json.dump(data, f)
    out = await restore_point.restore(files)
    assert out["carry"]["audit_kept"] == 0 and out["carry"]["audit_not_kept"]
    async with session_scope() as s:
        assert await s.get(AuditLog, 10 ** 6) is None


# ======================= R33: a rollback's uid-only hold ========================================================


@pytest.mark.parametrize("how", ["rotate", "cli_branch"])
async def test_r33_rollback_puts_back_a_copy_that_only_shares_the_uid_held(client, tmp_path, how):
    """R33 (tests-integrity #4): a continuation or CLI /branch child's copies are rewound and deleted by run 2; the
    admin withholds the parent's answer; run 2 is rolled back. The child's copy (another key, another JARVIS row,
    the same uid) comes back withheld, held for review: Identities.match_row's uid branch."""
    mark = "zqrevrbuid"
    await allow_ai_context()
    fx = bj.mini(tmp_path / "j")
    bj.add_session(fx["db"], "r33_p", started=T)
    bj.add_row(fx["db"], "r33_p", "user", "Remind me of the boat lock code.", T + 1)
    bj.add_row(fx["db"], "r33_p", "assistant", "The boat lock code is %s." % mark, T + 2)
    bj.add_row(fx["db"], "r33_p", "user", "Ok.", T + 3)
    copied = getattr(bj, how)(fx["db"], "r33_p", "r33_c", T + 20)
    bj.add_row(fx["db"], "r33_c", "user", "New topic in the child.", T + 21)
    await run(fx)
    bj.change(fx["db"], "UPDATE messages SET active = 0, compacted = 0 WHERE id IN (?, ?, ?)",
              copied[1], copied[2], copied[0])  # the child's copied turns undone
    second = await run(fx)
    async with session_scope() as s:
        rows = (await s.execute(text(
            "SELECT x.source_session_id, m.id FROM archive_messages m JOIN archive_sessions x ON x.id = m.session_id "
            "WHERE m.content LIKE :m ORDER BY m.id"), {"m": "%" + mark + "%"})).all()
    assert [r[0] for r in rows] == ["r33_p"], rows
    await as_(client, "admin")
    assert (await client.post("/api/brain/messages/%d/withhold" % rows[0][1])).status_code == 200
    # The state before review 1's rules: no stone on the child's copy keys (only the uid links them).
    async with session_scope() as s:
        await s.execute(text("DELETE FROM brain_tombstones WHERE source_key LIKE 'jarvis:message:r33_c:%'"))
    await imp.rollback(second["run_id"], discard_reviewed=True, confirm=True)
    async with session_scope() as s:
        back = (await s.execute(text(
            "SELECT m.source_key, m.withheld, m.content LIKE :m, t.review, t.via FROM archive_messages m "
            "JOIN archive_sessions x ON x.id = m.session_id LEFT JOIN brain_tombstones t ON t.source_key = m.source_key "
            "WHERE x.source_session_id = 'r33_c' AND m.role = 'assistant'"), {"m": "%" + mark + "%"})).all()
    print("\nR33", how, "child's copy after the rollback:", back)
    assert back and all(w and not has and review == identity.PENDING for _, w, has, review, _ in back), back
    for who in ("viewer", "operator", "admin"):
        await as_(client, who)
        assert mark not in (await client.get("/api/brain/search", params={"q": mark})).text, who
    assert await bj.nowhere(mark) == []

