"""0.6.2 review round 1: the confirmed findings outside the matching rules (findings-062-round1.md R15-R36), as
regressions. Synthetic data only; unique markers. Every test asserts the SAFE outcome and names its finding.

Stage 1, restore and durability (R20-R28, and R5, R16, R21, R31, R33 where they touch restore.ps1, the decisions file
or a rollback): a restore of an older backup re-applies every withhold, purge, release, title choice, access
narrowing and audit entry made after that backup; a rollback keeps an admin's title choice; an older importer is
refused on a 0.6.2 database.

Stage 2, readers, titles and test gaps (R15-R19, R29, R30, R32; R34 and R35 are in test_brain_identity_migration.py,
R36 in test_brain_upgrade_062.py): JARVIS's copies made before a withhold are withheld (R15); a hidden title stays
hidden on the live installation, after a restore to a fresh database and in continuation and branch children (R16,
R17), and no quote of another message names it (R18); the holds list and the preview tell an admin without the
sensitive-data permission nothing about a row outside the scope (R19); a withhold blanks the rollback copies of rows an
import deleted and a rollback brings them back withheld (R29); verify's checks 9-12 fail on the state each exists to
catch (R30); a stoned key JARVIS gave to an unrelated row is never written under it (R32).

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
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from sqlalchemy import select, text

from app import restore_carry
from app.bootstrap import check_withheld_copies
from app.brain import identity, store
from app.brain import importer as imp
from app.db import session_scope
from app.models import (
    ArchiveMessage,
    ArchiveSession,
    AuditLog,
    BrainPurgedContent,
    BrainPurgedItem,
    BrainTombstone,
    ImportRecord,
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
async def test_r33_rollback_puts_back_a_copy_that_only_shares_the_uid_held(client, tmp_path, monkeypatch, how):
    """R33 (tests-integrity #4): a continuation or CLI /branch child's copies are rewound and deleted by run 2; the
    admin withholds the parent's answer; run 2 is rolled back. The child's copy (another key, another JARVIS row,
    the same uid) comes back withheld, held for review: Identities.match_row's uid branch. Since review 1 R29 the
    withhold itself stones and blanks such a rollback copy; the test recreates the state an earlier withhold left (the
    copy with its text, no stone on its key: deleted_copies switched off for the withhold), so the uid branch stays
    covered for that state and for decisions carried over."""
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
    real = identity.deleted_copies

    async def before_r29(s, row, uid):
        return []

    monkeypatch.setattr(identity, "deleted_copies", before_r29)
    await as_(client, "admin")
    assert (await client.post("/api/brain/messages/%d/withhold" % rows[0][1])).status_code == 200
    monkeypatch.setattr(identity, "deleted_copies", real)
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



# =================================================================================================================
# Stage 2: readers, titles and test gaps
# =================================================================================================================

ALL = imp.Selection(all=True)
DAY = 86400.0


async def rows_of(sid: str) -> list[ArchiveMessage]:
    async with session_scope() as s:
        return list((await s.scalars(select(ArchiveMessage).join(
            ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id).where(
            ArchiveSession.source_session_id == sid).order_by(ArchiveMessage.id))).all())


# ======================= R15: JARVIS's copies made before the withhold ===========================================


def r15_secret(mark: str) -> str:
    return "Review note: the %s supplier deal closes at ninety one thousand euros." % mark


def r15_family(db: str, how: str, mark: str, *, later_repeat: bool = False) -> None:
    """A parent holding the secret and a child JARVIS made BEFORE any import or withhold: a gateway /branch (fresh
    uids, copied timestamps), a rotation in a backup without a uid column, or (control) a CLI /branch (same uids)."""
    bj.add_session(db, "r15_p", started=T)
    bj.add_row(db, "r15_p", "user", "What about the supplier?", T + 1)
    bj.add_row(db, "r15_p", "assistant", r15_secret(mark), T + 2)
    bj.add_row(db, "r15_p", "user", "ok thanks", T + 3)
    if how == "gateway_branch":
        bj.gateway_branch(db, "r15_p", "r15_c", started_at=T + DAY)
    elif how == "cli_branch_control":
        bj.cli_branch(db, "r15_p", "r15_c", started_at=T + DAY)
    else:
        bj.drop_uid_column(db)
        bj.change(db, "UPDATE sessions SET end_reason = 'compression' WHERE id = ?", "r15_p")
        bj.add_session(db, "r15_c", started=T + DAY, parent="r15_p", title="Continuation")
        for r in bj.rows(db, "r15_p"):
            bj.add_row(db, "r15_c", r["role"], r["content"], r["timestamp"], uid=False)
    if later_repeat:  # the user types the words again later in the child: not a copy (another role, another time)
        bj.add_row(db, "r15_c", "user", r15_secret(mark), T + DAY + 10, uid=False)
    else:
        bj.add_row(db, "r15_c", "user", "a new turn in the child", T + DAY + 10)


@pytest.mark.parametrize("how", ["gateway_branch", "rotation_no_uids", "cli_branch_control"])
async def test_r15_a_copy_jarvis_made_before_the_withhold_is_withheld_and_never_promoted(client, tmp_path, how):
    """R15 (readers-privacy #0): the child's copy, stored by the same import before the admin's withhold, is withheld
    by the withhold itself (a CLI branch's copy, the same uid and text, as the same message; a fresh uid or none:
    listed pre-ticked, 0.6.2 plan 4.5), certain once Settle runs (5.3 R1/R15: rule 6), stays withheld through a
    re-import and a later backup, is read by no reader (sessions, Supervisor history, recall), cannot be promoted, and
    verify and status agree."""
    from .test_brain_agents import ask
    mark = "zqr15" + how.replace("_", "")[:9]
    fx = bj.mini(tmp_path / "fx")
    r15_family(fx["db"], how, mark)
    await imp.run(bj.paths(fx), ALL)
    [orig] = [m for m in await rows_of("r15_p") if mark in (m.content or "")]
    [copy] = [m for m in await rows_of("r15_c") if mark in (m.content or "")]
    await as_(client, "admin")
    preview = (await client.get("/api/brain/messages/%d/withhold-preview" % orig.id)).json()
    member = how == "cli_branch_control"
    assert [r["id"] for r in preview["same_message_rows"]] == ([copy.id] if member else []), preview
    assert [r["id"] for r in preview["identical_text_rows"]] == ([] if member else [copy.id]), preview
    w = await client.post("/api/brain/messages/%d/withhold" % orig.id)
    assert w.status_code == 200 and w.json()["also_withheld" if member else "held_for_review"] == [copy.id], w.text
    await bj.settle(fx, keep=False)
    async with session_scope() as s:
        assert (await s.get(BrainTombstone, copy.source_key)).review == identity.CERTAIN
    await imp.run(bj.paths(fx), ALL)
    bj.add_row(fx["db"], "r15_p", "user", "one more question later", T + 2 * DAY)
    later = await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        c = await s.get(ArchiveMessage, copy.id)
        assert c.withheld and mark not in (c.content or "")
        hits, _ = await store.AgentMemory(s, "supervisor").history(mark)
        assert not any(mark in (h.message.content or "") for h in hits)
    assert mark not in json.dumps((await ask("what did JARVIS say about the %s supplier" % mark)).data)
    for who in ("operator", "admin", "admin+"):
        await as_(client, who)
        assert mark not in (await client.get("/api/brain/sessions/%d" % c.session_id)).text, who
    await as_(client, "operator")
    promote = await client.post("/api/brain/messages/%d/promote" % copy.id, json={"kind": "project", "title": "P"})
    assert promote.status_code in (400, 404), promote.text
    assert (await imp.verify(later["run_id"], bj.paths(fx)))["passed"]
    assert (await imp.status())["readable_rows_matching_a_withhold"] == 0
    assert await bj.nowhere(mark) == []


@pytest.mark.parametrize("how", ["gateway_branch", "rotation_no_uids"])
async def test_r15_the_next_import_repairs_a_copy_an_earlier_withhold_missed(client, tmp_path, monkeypatch, how):
    """R15: a database where the withhold already missed the copy (as 0.6.2 before review 1 did: the lineage walk is
    switched off for the withhold) is repaired by Settle (0.6.2 plan 4, 5.3 R1/R15: the copy is certain by rule 6;
    the 'admin saw it' guard is gone). The user's own later repeat of the words in the child (another role, another
    time: never certain) is held for review, 'identical', pre-selected for release only when it has its own uid."""
    mark = "zqr15rep" + how[:4]
    fx = bj.mini(tmp_path / "fx")
    r15_family(fx["db"], how, mark, later_repeat=True)
    await imp.run(bj.paths(fx), ALL)
    [orig] = [m for m in await rows_of("r15_p") if mark in (m.content or "")]
    [missed] = [m for m in await rows_of("r15_c") if m.role == "assistant" and mark in (m.content or "")]
    [repeat] = [m for m in await rows_of("r15_c") if m.role == "user" and mark in (m.content or "")]
    real = identity.lineage

    async def only_self(s, ids):
        return {i: "self" for i in ids}

    # The lineage walk off: no family, and (since rules062-confirm, which looks up a row's DIRECT continuation and
    # branch children itself) no child that copied the row.
    kinds = identity.LINEAGE
    monkeypatch.setattr(identity, "lineage", only_self)
    monkeypatch.setattr(identity, "LINEAGE", ())
    await as_(client, "admin")
    assert (await client.post("/api/brain/messages/%d/withhold" % orig.id)).status_code == 200
    monkeypatch.setattr(identity, "lineage", real)
    monkeypatch.setattr(identity, "LINEAGE", kinds)
    await bj.settle(fx, keep=False)
    after = {m.id: m for m in await rows_of("r15_c")}
    assert after[missed.id].withheld and mark not in (after[missed.id].content or "")
    async with session_scope() as s:
        assert (await s.get(BrainTombstone, missed.source_key)).review == identity.CERTAIN
        assert (await s.get(BrainTombstone, repeat.source_key)).review == identity.PENDING
    await as_(client, "admin")
    [held] = [h for h in (await client.get("/api/brain/holds")).json()["holds"] if h["message"] == repeat.id]
    assert identity.IDENTICAL_REL in held["relations"] and held["confirm_required"], held


# ======================= R16 / R17: the hidden title on live, after a restore, and in children ====================


def r16_first(mark: str) -> str:
    return "Contract %s with the vendor is worth sixty two thousand dollars, keep it quiet." % mark


def r16_derived(mark: str) -> str:  # Hermes's derive_title: the first 48 characters of the first user message
    return r16_first(mark)[:48].rstrip() + "…"


async def title_readers(client, mark: str) -> dict[str, list[str]]:
    """Which routes show the marker, for operator, admin and admin+: the sessions list, its search, the search page
    and every conversation."""
    async with session_scope() as s:
        ids = list((await s.scalars(select(ArchiveSession.id))).all())
    out: dict[str, list[str]] = {}
    for who in ("operator", "admin", "admin+"):
        await as_(client, who)
        seen = []
        if mark in (await client.get("/api/brain/sessions", params={"limit": 200})).text:
            seen.append("GET /sessions")
        if mark in (await client.get("/api/brain/sessions", params={"q": "vendor", "limit": 200})).text:
            seen.append("GET /sessions?q=vendor")
        if mark in (await client.get("/api/brain/search", params={"q": "vendor"})).text:
            seen.append("GET /search?q=vendor")
        for i in ids:
            if mark in (await client.get("/api/brain/sessions/%d" % i)).text:
                seen.append("GET /sessions/%d" % i)
        if seen:
            out[who] = seen
    return out


async def test_r16_restore_to_a_fresh_database_keeps_the_title_hidden(client, tmp_path):
    """R16 (c), restore.ps1's flow: the removals are exported (--removals-only), the database is empty again, they are
    applied before the conversation exists ('kept for later imports'), then the backup is imported: the first user
    message comes back withheld and the title (derived from it) hidden."""
    from .test_brain_withhold_identity import _fresh_production
    mark = "zqr16rest"
    fx = bj.mini(tmp_path / "fx")
    bj.add_session(fx["db"], "r16_a", started=T, title=r16_derived(mark))
    bj.add_row(fx["db"], "r16_a", "user", r16_first(mark), T + 1)
    bj.add_row(fx["db"], "r16_a", "assistant", "Understood, noted.", T + 2)
    await imp.run(bj.paths(fx), ALL)
    [m] = [r for r in await rows_of("r16_a") if r.role == "user"]
    await as_(client, "admin")
    assert (await client.post("/api/brain/messages/%d/withhold" % m.id)).json()["title_hidden"] is True
    out = tmp_path / "removals.json"
    await imp.decisions_export(str(out), removals_only=True)
    await _fresh_production()
    await imp.decisions_apply(str(out), None, actor=None, apply=True, removals_only=True)
    await imp.run(bj.paths(fx), ALL)
    [back] = [r for r in await rows_of("r16_a") if r.role == "user"]
    assert back.withheld
    assert await title_flag("r16_a") is True and (await session_titles(client))["r16_a"] == ""
    assert await title_readers(client, mark) == {} and await bj.nowhere(mark) == []


async def test_r16_an_explicit_hide_title_travels_from_the_test_copy_to_live(client, tmp_path):
    """R16 (d): the admin withholds a message that is not the first user message and ticks 'Hide the conversation
    title' on the test copy; on live (a fresh database, the same backup, decisions-apply) the title stays hidden."""
    from app.brain.importer import decisions_apply, decisions_export

    from ..conftest import ADMIN
    from .test_brain_withhold_identity import _fresh_production
    mark = "zqr16exp"
    fx = bj.mini(tmp_path / "fx")
    bj.add_session(fx["db"], "r16_b", started=T, title="Vendor %s deal: sixty two thousand" % mark)
    bj.add_row(fx["db"], "r16_b", "user", "Hello there.", T + 1)
    bj.add_row(fx["db"], "r16_b", "assistant", "Vendor %s deal: sixty two thousand dollars agreed." % mark, T + 2)
    await imp.run(bj.paths(fx), ALL)
    [m] = [r for r in await rows_of("r16_b") if r.role == "assistant"]
    await as_(client, "admin")
    r = await client.post("/api/brain/messages/%d/withhold" % m.id, json={"hide_title": True})
    assert r.status_code == 200 and r.json()["title_hidden"] is True, r.text
    await bj.settle(fx)  # 0.6.2 plan 4.7: decisions-export refuses until the withhold is finished
    out = tmp_path / "decisions.json"
    await decisions_export(str(out))
    assert [x.get("title_hidden") for x in json.loads(out.read_text())["removals"]] == [True]
    await _fresh_production()
    live = await imp.run(bj.paths(fx), ALL)
    await decisions_apply(str(out), live["run_id"], actor=ADMIN[0], apply=True)
    assert await title_flag("r16_b") is True
    assert await title_readers(client, mark) == {} and await bj.nowhere(mark) == []


@pytest.mark.parametrize("how", ["rotation", "gateway_branch", "gateway_branch_after"])
async def test_r17_a_child_that_carries_the_hidden_title_hides_it_too(client, tmp_path, how):
    """R17 (readers-privacy #2): Hermes carries the title to a rotation's continuation (and clears the parent's) and
    names a /branch '<title> #2'. Hiding the parent's title hides the child's, also for a branch made after the
    withhold and imported later; no reader shows it, and a re-import keeps it hidden."""
    mark = "zqr17" + how[:3] + ("a" if how.endswith("after") else "")
    fx = bj.mini(tmp_path / "fx")
    db = fx["db"]
    bj.add_session(db, "r17_p", started=T, title=r16_derived(mark))
    bj.add_row(db, "r17_p", "user", r16_first(mark), T + 1)
    bj.add_row(db, "r17_p", "assistant", "Noted, I will keep the vendor figure private.", T + 2)

    def make_child() -> None:
        if how == "rotation":
            bj.rotate(db, "r17_p", "r17_c", started_at=T + DAY)
            bj.change(db, "UPDATE sessions SET title = ? WHERE id = 'r17_c'", r16_derived(mark))
            bj.change(db, "UPDATE sessions SET title = NULL WHERE id = 'r17_p'")  # Hermes moves the title
        else:
            bj.gateway_branch(db, "r17_p", "r17_c", started_at=T + DAY)
            bj.change(db, "UPDATE sessions SET title = ? WHERE id = 'r17_c'", r16_derived(mark) + " #2")
        bj.add_row(db, "r17_c", "user", "A later turn in the child.", T + DAY + 5)

    if how != "gateway_branch_after":
        make_child()
    await imp.run(bj.paths(fx), ALL)
    [m] = [r for r in await rows_of("r17_p") if r.role == "user"]
    await as_(client, "admin")
    assert (await client.post("/api/brain/messages/%d/withhold" % m.id)).json()["title_hidden"] is True
    if how == "gateway_branch_after":
        make_child()
    await imp.run(bj.paths(fx), ALL)
    assert await title_flag("r17_c") is True
    assert await title_readers(client, mark) == {}
    await imp.run(bj.paths(fx), ALL)
    assert await bj.nowhere(mark) == []


# ======================= R18: quotes naming a hidden title =======================================================


R18_T0 = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)


async def r18_conversation(mark: str, sid: str) -> int:
    """A conversation titled with its first user message (Hermes's derived title) and a readable accomplishment a
    history answer quotes. Returns the first user message's id."""
    from app.brain.archive import recompute_session
    async with session_scope() as s:
        sess = ArchiveSession(source_key="jarvis:session:" + sid, source_session_id=sid, title=r16_derived(mark),
                              staff="jarvis", agent="supervisor", sensitivity="general", started_at=R18_T0)
        s.add(sess)
        await s.flush()
        rows = [ArchiveMessage(session_id=sess.id, source_key="jarvis:message:%s:%d" % (sid, p),
                               source_message_id=str(p), position=p, role=r, content=c,
                               at=R18_T0 + timedelta(minutes=p), labels=lab)
                for p, r, c, lab in ((0, "user", r16_first(mark), []),
                                     (1, "assistant", "The certificate was renewed and the site is up again.",
                                      ["accomplishment"]),
                                     (2, "assistant", "Closing the ticket now.", []))]
        s.add_all(rows)
        await s.flush()
        await recompute_session(s, sess.id)
        return rows[0].id


async def r18_lines(mark: str) -> list[str]:
    """Every stored answer line and summary line that holds the marker or quotes message 1 of r18_*."""
    async with session_scope() as s:
        answers = [str(line) for a in (await s.scalars(text(
            "SELECT result -> 'data' -> 'answer' FROM tasks WHERE result -> 'data' ? 'answer'"))).all()
            for line in (a or [])]
        summaries = [line for x in (await s.scalars(text("SELECT summary FROM supervisor_requests"))).all()
                     for line in (x or "").split("\n")]
    return [line for line in answers + summaries if mark in line or "· message 1)" in line]


async def test_r18_hiding_a_title_rewrites_every_quote_that_names_it(client, make_worker, service_headers):
    """R18 (readers-privacy #3): a history answer quotes a readable message of a conversation and names its source by
    the conversation's title; the admin then withholds the first user message (title hidden). At once, before any
    start-up check, no task result and no request summary holds the title, and the quote itself stays, naming the
    conversation as recall names one without a title."""
    from .test_brain_quotes_readers import quote_everything, sweep
    mark = "zqr18now"
    first = await r18_conversation(mark, "r18_a")
    await quote_everything(client, make_worker)
    assert [c for c in await bj.nowhere(mark) if c.startswith(("tasks.", "supervisor_requests."))]  # precondition
    await as_(client, "admin")
    w = await client.post("/api/brain/messages/%d/withhold" % first)
    assert w.status_code == 200 and w.json()["title_hidden"] is True, w.text
    left = [c for c in await bj.nowhere(mark) if c.startswith(("tasks.", "supervisor_requests."))]
    assert left == [], "the hidden title is still stored in %s" % left
    lines = await r18_lines(mark)
    assert lines and all("(source: session r18_a · message 1)" in line for line in lines), lines
    assert any("The certificate was renewed" in line for line in lines), "the readable quote itself was lost"
    assert await sweep(client, service_headers, (mark,)) == {}


async def test_r18_quotes_made_before_a_title_was_hidden_are_counted_hidden_at_read_time_and_rewritten_at_start_up(
        client, make_worker, service_headers):
    """R18 (2): a title hidden without rewriting its quotes (0.6.1's withhold, or a start-up check that could not run)
    is counted by status, never served by a task or request reader (the read-time guard), and rewritten in the stored
    answers and summaries by the start-up check."""
    from .test_brain_quotes_readers import quote_everything, sweep
    mark = "zqr18old"
    await r18_conversation(mark, "r18_b")
    await quote_everything(client, make_worker)
    async with session_scope() as s:  # what 0.6.1's hide did: the title only
        await s.execute(text("UPDATE archive_sessions SET title = '', flags = flags || '{\"title_withheld\": true}' "
                             "WHERE source_session_id = 'r18_b'"))
    assert [c for c in await bj.nowhere(mark) if c.startswith(("tasks.", "supervisor_requests."))]
    assert (await imp.status())["quotes_of_removed_sources"] >= 1
    assert await sweep(client, service_headers, (mark,)) == {}, "a reader served the hidden title"
    await as_(client, "operator")
    async with session_scope() as s:
        rid = await s.scalar(text("SELECT id FROM supervisor_requests ORDER BY id LIMIT 1"))
    summary = (await client.get("/api/supervisor/requests/%d" % rid)).json()["request"]["summary"]
    assert "(source: session r18_b · message 1)" in summary, summary  # the quote stays, without the title
    assert await check_withheld_copies() is True
    assert [c for c in await bj.nowhere(mark) if c.startswith(("tasks.", "supervisor_requests."))] == []
    assert (await imp.status())["quotes_of_removed_sources"] == 0


# ======================= R19: the holds list and the preview, outside the admin's scope ========================


R19_T0 = datetime(2026, 3, 5, 15, 0, tzinfo=UTC)
R19_UID = "c0ffee00c0ffee00c0ffee00c0ffee19"


async def r19_plant(key: str, agent: str, staff: str, words: str, at: datetime) -> tuple[int, int]:
    from app.brain.archive import recompute_session
    async with session_scope() as s:
        sess = ArchiveSession(source_key="jarvis:session:%s" % key, source_session_id=key, title="t " + key,
                              staff=staff, agent=agent, sensitivity="general", started_at=at)
        s.add(sess)
        await s.flush()
        rows = [ArchiveMessage(session_id=sess.id, source_key="jarvis:message:%s:%d" % (key, p),
                               source_message_id=str(p), position=p, role=r, content=c, at=at + timedelta(minutes=p),
                               source_uid=u, content_chars=len(c))
                for p, r, c, u in ((0, "user", "Opening line for %s." % key, ""), (1, "assistant", words, R19_UID))]
        s.add_all(rows)
        await s.flush()
        await recompute_session(s, sess.id)
        return sess.id, rows[1].id


async def test_r19_an_admin_without_the_sensitive_data_permission_learns_nothing_of_a_row_outside_the_scope(client):
    """R19 (readers-privacy #4): a Credit Repair row and a general row share a uid (planted). The plain admin (404 on
    the Credit Repair conversation) previews the general row: rows outside the scope are one total, never per list
    (a per-list count told whether the hidden text was equal). admin+ withholds the Credit Repair row, which holds the
    general one; the plain admin's holds list names the hold but not the withheld message's id, position, time or
    length. admin+ (in scope) still sees them."""
    cr_sess, cr_msg = await r19_plant("r19_cr", "credit_repair", "sarah",
                                      "Dispute letter for the client sent, balance 4410 dollars.", R19_T0)
    _, gen_msg = await r19_plant("r19_gen", "supervisor", "jarvis", "A different general answer.",
                                 R19_T0 + timedelta(days=1))
    await as_(client, "admin")
    assert (await client.get("/api/brain/sessions/%d" % cr_sess)).status_code == 404
    preview = (await client.get("/api/brain/messages/%d/withhold-preview" % gen_msg)).json()
    assert not [k for k in preview if k.endswith("_hidden")], preview
    assert preview["rows_outside_your_scope"] == 1 and preview["held_rows"] == [], preview
    await as_(client, "admin+")
    w = await client.post("/api/brain/messages/%d/withhold" % cr_msg)
    assert w.status_code == 200 and w.json()["held_for_review"] == [gen_msg], w.text
    await as_(client, "admin")
    [hold] = (await client.get("/api/brain/holds")).json()["holds"]
    assert hold["message"] == gen_msg
    assert hold["withheld_message"] == {"id": None, "position": None, "role": "assistant", "at": None,
                                        "content_chars": None}, hold
    await as_(client, "admin+")
    [hold] = (await client.get("/api/brain/holds")).json()["holds"]
    assert hold["withheld_message"]["id"] == cr_msg and hold["withheld_message"]["position"] == 1


# ======================= R29: rollback copies of rows an import deleted =========================================


R29_MARK = "zqr29tailmark"


async def r29_records(mark: str) -> list[tuple]:
    async with session_scope() as s:
        return [tuple(r) for r in (await s.execute(text(
            "SELECT id, source_key, reason FROM import_records WHERE before::text LIKE :m ORDER BY id"),
            {"m": "%" + mark + "%"})).all()]


@pytest.mark.parametrize("via", ["api", "direct"])
async def test_r29_a_withhold_blanks_the_rollback_copy_of_the_rewound_original_at_once(client, tmp_path, via):
    """R29 (tests-integrity #0): Hermes's in-place compaction rewinds the protected tail and writes it again; the next
    import deletes the rewound original and keeps its rollback copy under its uid key. The admin then withholds the
    live copy: right after the withhold no rollback copy holds the text and status says 0 (not only after the next
    start-up check)."""
    from ..conftest import ADMIN
    await allow_ai_context()
    fx = bj.mini(tmp_path / "j")
    bj.add_session(fx["db"], "r29_a", started=T)
    convo = [("user", "Start the weekly plan."), ("assistant", "Started."), ("user", "List the chores."),
             ("assistant", "Chores listed."), ("user", "Tell me the locker combo."),
             ("assistant", "The locker combo is %s." % R29_MARK), ("user", "Thanks.")]
    for n, (role, content) in enumerate(convo):
        bj.add_row(fx["db"], "r29_a", role, content, T + n + 1)
    await imp.run(bj.paths(fx), ALL)
    bj.hermes_compact(fx["db"], "r29_a", head_n=2, tail_n=3, ts=T + 500)
    await imp.run(bj.paths(fx), ALL)
    [live] = [m for m in await rows_of("r29_a") if R29_MARK in (m.content or "")]
    assert [r[2] for r in await r29_records(R29_MARK)] == ["rewound since an earlier import"]  # precondition
    if via == "api":
        await as_(client, "admin")
        assert (await client.post("/api/brain/messages/%d/withhold" % live.id)).status_code == 200
    else:
        from app.brain import review
        async with session_scope() as s:
            await review.withhold_message(s, await s.get(ArchiveMessage, live.id), actor=ADMIN[0])
    assert await bj.nowhere(R29_MARK) == []
    assert (await imp.status())["withheld_rollback_copies_with_text"] == 0


async def test_r29_a_branch_childs_rewound_copy_is_blanked_by_the_withhold(client, tmp_path):
    """R29, the CLI /branch variant: the child's copy (the parent's uid, a key of its own) was rewound and deleted by
    an import before the withhold. The withhold of the parent's row blanks that rollback copy at once."""
    mark = "zqr29branch"
    await allow_ai_context()
    fx = bj.mini(tmp_path / "j")
    bj.add_session(fx["db"], "r29_p", started=T)
    bj.add_row(fx["db"], "r29_p", "user", "What is the alarm code?", T + 1)
    bj.add_row(fx["db"], "r29_p", "assistant", "The alarm code is %s." % mark, T + 2)
    child = bj.cli_branch(fx["db"], "r29_p", "r29_c", T + 10)
    bj.add_row(fx["db"], "r29_c", "user", "Thanks, branching here.", T + 11)
    await imp.run(bj.paths(fx), ALL)
    bj.change(fx["db"], "UPDATE messages SET active = 0, compacted = 0 WHERE id = ?", child[1])  # rewound
    await imp.run(bj.paths(fx), ALL)
    [parent] = [m for m in await rows_of("r29_p") if mark in (m.content or "")]
    assert len(await r29_records(mark)) == 1  # precondition: the child's deleted copy holds the text
    await as_(client, "admin")
    assert (await client.post("/api/brain/messages/%d/withhold" % parent.id)).status_code == 200
    assert await bj.nowhere(mark) == []
    assert (await imp.status())["withheld_rollback_copies_with_text"] == 0


@pytest.mark.parametrize("uids", [True, False])
async def test_r29_rolling_back_the_compaction_run_brings_the_original_back_withheld(client, tmp_path, uids):
    """R29: after the withhold, a rollback of the run that deleted the rewound original puts it back withheld, without
    text, and no reader shows it, also for a backup whose rows carry no message_uid (the rewound twin in the same
    conversation, deleted by the run that stored the live copy)."""
    mark = "zqr29rb" + ("uid" if uids else "nouid")
    await allow_ai_context()
    fx = bj.mini(tmp_path / "j")
    bj.add_session(fx["db"], "r29_r", started=T)
    convo = [("user", "Start."), ("assistant", "Ok."), ("user", "Combo?"),
             ("assistant", "The combo is %s." % mark), ("user", "Thanks.")]
    for n, (role, content) in enumerate(convo):
        bj.add_row(fx["db"], "r29_r", role, content, T + n + 1, uid=None if uids else False)
    await imp.run(bj.paths(fx), ALL)
    bj.hermes_compact(fx["db"], "r29_r", head_n=1, tail_n=3, ts=T + 500)
    second = await imp.run(bj.paths(fx), ALL)
    [live] = [m for m in await rows_of("r29_r") if mark in (m.content or "")]
    await as_(client, "admin")
    assert (await client.post("/api/brain/messages/%d/withhold" % live.id)).status_code == 200
    assert await bj.nowhere(mark) == []
    await imp.rollback(second["run_id"], discard_reviewed=True, confirm=True)
    for who in ("viewer", "operator", "admin"):
        await as_(client, who)
        assert mark not in (await client.get("/api/brain/search", params={"q": mark})).text, who
    assert await bj.nowhere(mark) == []
    back = [m for m in await rows_of("r29_r") if m.role == "assistant" and m.withheld]
    assert back, "the rewound original did not come back withheld"


async def test_r29_inverse_an_identical_message_with_its_own_uid_keeps_its_rollback_copy(client, tmp_path):
    """R29 inverse (owner requirement 2): another Hermes message with the same words (its own uid) that an import
    deleted as rewound is not the withheld message: the withhold writes no stone on its key and leaves its rollback
    copy as it is."""
    mark = "zqr29inverse"
    await allow_ai_context()
    fx = bj.mini(tmp_path / "j")
    bj.add_session(fx["db"], "r29_i", started=T)
    bj.add_row(fx["db"], "r29_i", "user", "Read it out.", T + 1)
    bj.add_row(fx["db"], "r29_i", "assistant", "The code is %s." % mark, T + 2)
    bj.add_row(fx["db"], "r29_i", "user", "Again please.", T + 3)
    other = bj.add_row(fx["db"], "r29_i", "assistant", "The code is %s." % mark, T + 4)
    await imp.run(bj.paths(fx), ALL)
    bj.change(fx["db"], "UPDATE messages SET active = 0, compacted = 0 WHERE id = ?", other)  # rewound
    await imp.run(bj.paths(fx), ALL)
    [first] = [m for m in await rows_of("r29_i") if mark in (m.content or "")]
    [(rec_id, key, _)] = await r29_records(mark)
    await as_(client, "admin")
    assert (await client.post("/api/brain/messages/%d/withhold" % first.id)).status_code == 200
    async with session_scope() as s:
        assert await s.get(BrainTombstone, key) is None, "an independent identical message's key got a stone"
        assert mark in json.dumps((await s.get(ImportRecord, rec_id)).before)


async def test_r29_a_deleted_row_that_only_shares_the_uid_gets_no_stone_and_a_rollback_holds_it(client, tmp_path):
    """R29 under the rules062-confirm principle (deviation of 2026-10-21): a CLI /branch child's copy of a tool output
    (the parent's uid, a key of its own) is written again by Hermes with the output pruned (the same uid, a stub), and
    the next import deletes the full copy as rewound. The admin withholds the child's stub. The deleted full copy only
    shares the uid (other text): it gets no stone (never a member: no provenance says it is the stub), but its
    rollback copy loses the text at once, as the start-up sweep's uid rule would (status says 0 right away); a
    rollback of that import brings it back as a hold, listed, never readable."""
    mark = "zqr29uidonly"
    stub = "[tool output pruned to save context]"
    await allow_ai_context()
    fx = bj.mini(tmp_path / "j")
    bj.add_session(fx["db"], "r29_up", started=T)
    bj.add_row(fx["db"], "r29_up", "user", "Run the vendor report.", T + 1)
    bj.add_row(fx["db"], "r29_up", "tool", "REPORT " + secret(mark), T + 2, tool_call_id="call_1", tool_name="terminal")
    child = bj.cli_branch(fx["db"], "r29_up", "r29_uc", T + 10)
    bj.add_row(fx["db"], "r29_uc", "user", "Branching here.", T + 11)
    await imp.run(bj.paths(fx), ALL)
    [full] = [r for r in bj.rows(fx["db"], "r29_uc") if r["id"] == child[1]]
    bj.change(fx["db"], "UPDATE messages SET active = 0, compacted = 0 WHERE id = ?", child[1])  # rewound
    again = bj.add_row(fx["db"], "r29_uc", "tool", stub, T + 12, uid=full["message_uid"], tool_call_id="call_1",
                       tool_name="terminal")
    second = await imp.run(bj.paths(fx), ALL)
    [(rec_id, key, reason)] = await r29_records(mark)
    assert reason == "rewound since an earlier import" and not key.startswith(identity.UID_KEY), key  # precondition
    [stub_row] = await message_of("r29_uc", again)
    await as_(client, "admin")
    assert (await client.post("/api/brain/messages/%d/withhold" % stub_row.id)).status_code == 200
    async with session_scope() as s:
        assert await s.get(BrainTombstone, key) is None, "a row that only shares the uid became a member"
        assert mark not in json.dumps((await s.get(ImportRecord, rec_id)).before)
    assert (await imp.status())["withheld_rollback_copies_with_text"] == 0
    await imp.rollback(second["run_id"], discard_reviewed=True, confirm=True)
    [back] = await message_of("r29_uc", child[1])
    st = (await stones()).get(back.source_key)
    assert back.withheld and st is not None and st.review == identity.PENDING, (back.withheld, st and st.review)
    # A blanked rollback copy comes back restricted (archive.WITHHELD_ROW): listed to an admin who may open it.
    await as_(client, "admin+")
    assert back.id in [h["message"] for h in (await client.get("/api/brain/holds")).json()["holds"]]
    for who in ("viewer", "operator", "admin", "admin+"):
        await as_(client, who)
        assert mark not in (await client.get("/api/brain/search", params={"q": mark})).text, who


async def test_r29_a_withhold_finds_deleted_copies_through_the_partial_indexes():
    """R29: the withhold's lookup of rollback copies of deleted rows never scans every rollback copy (an import writes
    one record per message). The query is prepared exactly as the driver sends it (values as parameters) and planned
    generically (as a reused prepared statement is): its plan reads one of 0008's partial indexes of deleted rows
    (which one depends on the table's statistics), by uid and by conversation. With the JSON keys or the record type
    sent as parameters, no partial index applies. Since 0.6.2 step 6 the lookup is by uid only (the same-time
    lookup of a uid-less row was a time rule, plan 4.3): there is no query without a uid."""
    from types import SimpleNamespace

    def literal(v: object) -> str:
        return str(v) if isinstance(v, int) else "'%s'" % str(v).replace("'", "''")

    row = SimpleNamespace(role="assistant", import_run_id=3, session_id=7)
    async with session_scope() as s:
        conn = await s.connection()
        await conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
        await conn.exec_driver_sql("SET LOCAL plan_cache_mode = force_generic_plan")
        assert identity.deleted_copies_query(row, "") is None
        for n, uid in enumerate(("u-r29-plan",)):
            compiled = identity.deleted_copies_query(row, uid).compile(dialect=conn.dialect)
            params = compiled.construct_params()
            values = [params[k] for k in compiled.positiontup or []]
            await conn.exec_driver_sql("PREPARE r29_plan_%d AS %s" % (n, compiled))
            plan = "\n".join((await conn.exec_driver_sql("EXPLAIN EXECUTE r29_plan_%d(%s)" % (
                n, ", ".join(literal(v) for v in values)))).scalars())
            await conn.exec_driver_sql("DEALLOCATE r29_plan_%d" % n)
            assert "import_records_deleted_" in plan and "Seq Scan on import_records" not in plan, plan


# ======================= R30: verify's checks 9-12 fail on their own leak state =================================


R30_CHECKS = ("withheld_identity_holds", "withheld_hold_no_fingerprint", "purged_stay_purged", "database_guard_quiet")


@pytest.fixture(scope="module")
def built_r30(tmp_path_factory):
    return bj.build(tmp_path_factory.mktemp("r30") / "fx")


@pytest.mark.parametrize("case, check", [
    ("stone_after_a_readable_row", "withheld_identity_holds"),
    ("plain_fingerprint_on_a_withheld_row", "withheld_hold_no_fingerprint"),
    ("purged_item_imported_again", "purged_stay_purged"),
    ("database_guard_blanked_a_row", "database_guard_quiet")])
async def test_r30_each_identity_check_fails_on_the_state_it_exists_to_catch(built_r30, case, check):
    """R30 (tests-integrity #1): after a clean import (verify passes), the leak state of one check is planted; verify
    fails, that check fails, and no other identity check does (exact set: a false positive fails too)."""
    done = await imp.run(bj.paths(built_r30), ALL)
    run_id = done["run_id"]

    async def failed() -> tuple[bool, set[str]]:
        v = await imp.verify(run_id, bj.paths(built_r30))
        return v["passed"], {k for k, c in v["verification"]["checks"].items() if not c["ok"]}

    ok, bad = await failed()
    assert ok and not bad, bad
    async with session_scope() as s:
        m = await s.scalar(select(ArchiveMessage).where(ArchiveMessage.import_run_id == run_id,
                                                        ArchiveMessage.withheld.is_(False),
                                                        ArchiveMessage.content != "").order_by(ArchiveMessage.id))
        sid = await s.scalar(select(ArchiveSession.source_session_id).where(ArchiveSession.id == m.session_id))
        if case == "stone_after_a_readable_row":
            await s.execute(text(
                "INSERT INTO brain_tombstones (source_key, purged_at, purged_by, reason, source_session_id, "
                "source_row_id, role, tool_call_id, message_uid, uid_known, anchor_key, via, review, bound, after_run) "
                "VALUES (:k, now(), 'admin', 'withheld by an admin', :sid, :row, :role, '', :uid, false, '', 'admin', "
                "'', true, 0)"), {"k": m.source_key, "sid": sid, "row": m.source_message_id, "role": m.role,
                                  "uid": m.source_uid or ""})
        elif case == "plain_fingerprint_on_a_withheld_row":
            w = await s.scalar(select(ArchiveMessage).where(ArchiveMessage.withheld.is_(True)).limit(1))
            assert w is not None, "the fixture has a withheld row"
            await s.execute(text("UPDATE archive_messages SET source_fp = repeat('ab', 32) WHERE id = :i"),
                            {"i": w.id})
        elif case == "purged_item_imported_again":
            item = await s.scalar(select(MemoryItem).where(MemoryItem.import_run_id == run_id,
                                                           MemoryItem.source_key.is_not(None)).order_by(MemoryItem.id))
            await s.execute(text("INSERT INTO brain_tombstones (source_key, purged_at, purged_by, reason) "
                                 "VALUES (:k, now() - interval '3 days', 'admin', 'purged by an admin')"),
                            {"k": item.source_key})
        else:
            await s.execute(text("UPDATE archive_messages SET flags = flags || "
                                 "'{\"withheld_by_admin\": {\"guard\": \"key\"}}'::jsonb WHERE id = :i"), {"i": m.id})
    ok, bad = await failed()
    assert ok is False and bad & set(R30_CHECKS) == {check}, (ok, bad)


# ======================= R32: a withheld message's key that JARVIS gave to another row ==========================


async def test_r32_a_stoned_key_given_to_an_unrelated_row_is_never_written_under_it(client, tmp_path):
    """R32 (tests-integrity #3): the admin withholds an answer stored under its uid key; its run is rolled back (the
    stone stays, no row); JARVIS then gives that uid to an earlier, unrelated user row while the answer is still in
    the backup. The import excludes the key as reassigned: nothing is stored under it, the database guard blanks
    nothing, the user's words stay readable, and verify passes."""
    mark, ask_words = "zqr32moved", "zqr32askwords"
    await allow_ai_context()
    fx = bj.mini(tmp_path / "j")
    bj.add_session(fx["db"], "r32", started=T)
    bj.add_row(fx["db"], "r32", "user", "Hello there.", T + 1)
    ask_id = bj.add_row(fx["db"], "r32", "user", "Please tell me the vault phrase %s now." % ask_words, T + 2)
    ans_id = bj.add_row(fx["db"], "r32", "assistant", "The vault phrase is %s." % mark, T + 3)
    bj.add_row(fx["db"], "r32", "user", "Thanks.", T + 4)
    first = await imp.run(bj.paths(fx), ALL)
    [ans] = [m for m in await rows_of("r32") if mark in (m.content or "")]
    key = ans.source_key
    assert key.startswith(identity.UID_KEY) and ask_id < ans_id
    await as_(client, "admin")
    assert (await client.post("/api/brain/messages/%d/withhold" % ans.id)).status_code == 200
    await imp.rollback(first["run_id"], discard_reviewed=True, confirm=True)
    async with session_scope() as s:
        assert await s.get(BrainTombstone, key) is not None
    bj.change(fx["db"], "UPDATE messages SET message_uid = ? WHERE id = ?", key[len(identity.UID_KEY):], ask_id)
    second = await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        records = [tuple(r) for r in (await s.execute(select(ImportRecord.action, ImportRecord.reason).where(
            ImportRecord.run_id == second["run_id"], ImportRecord.source_key == key))).all()]
        stored = (await s.execute(text("SELECT id FROM archive_messages WHERE source_key = :k"), {"k": key})).all()
        guarded = (await s.execute(text("SELECT id FROM archive_messages "
                                        "WHERE flags -> 'withheld_by_admin' ? 'guard'"))).all()
        asked = (await s.execute(text(
            "SELECT m.withheld FROM archive_messages m JOIN archive_sessions x ON x.id = m.session_id "
            "WHERE x.source_session_id = 'r32' AND m.source_message_id = :i"), {"i": str(ask_id)})).all()
    assert records == [("excluded", "key collision: JARVIS gave the key of a withheld message to another row")]
    assert stored == [] and guarded == [], (stored, guarded)
    # The unrelated row is left out under that key (spec 17), never stored there and never withheld.
    assert not any(w for (w,) in asked), "the unrelated user row was withheld"
    assert second["report"]["key_reassigned"] == 1, second["report"]
    v = await imp.verify(second["run_id"], bj.paths(fx))
    assert v["passed"], [k for k, c in v["verification"]["checks"].items() if not c["ok"]]
    assert await bj.nowhere(mark) == []
