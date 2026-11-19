"""0.6.2 option C (rebuild R2): the central sweep holds every row of a withheld conversation group (hgr_*).

A withhold, every import and Settle, decisions-apply (live and restore's removals-only file), a rollback, the legacy
reset and the start-up repair run review.hold_group_rows: every readable row of a conversation a recorded group holds
is held with its conversation (a pending stone in that group, the row without its text, listed for an admin with its
evidence), unless an admin released that exact row for each group holding it. 'none' only when every withheld text was
in hand; otherwise 'other_not_compared' (never released in one click). The database guard (0008) holds a group row any
code writes readable, and the repair's R10 holds what is left (no withheld text in hand: 'not in this backup'). The
counters say how many group rows wait and how many are readable without a release. Synthetic data only."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select, text

from app.brain import groups, identity, review
from app.brain import importer as imp
from app.db import session_scope
from app.models import ArchiveMessage, ArchiveSession, BrainTombstone

from .. import brain_jarvis as bj
from ..conftest import ADMIN
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_later_062 import holds, pending, state
from .test_brain_withhold_identity import DAY, T, _fresh_production, conversation, message_of, run, secret, withhold

pytestmark = pytest.mark.usefixtures("clean_db")


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


async def archive_id(sid: str) -> int:
    async with session_scope() as s:
        return int(await s.scalar(select(ArchiveSession.id).where(ArchiveSession.source_session_id == sid)))


async def row_id(sid: str, source_row: int) -> int:
    return (await message_of(sid, source_row))[0].id


async def readable_with(mark: str) -> int:
    async with session_scope() as s:
        return int(await s.scalar(text("SELECT count(*) FROM archive_messages WHERE NOT withheld "
                                       "AND content LIKE '%' || :m || '%'"), {"m": mark}))


async def join(sid: str, anchor: str, *, state_: str = groups.MEMBER) -> None:
    """Records a conversation in a group without the sweep (as restore's file or older code would)."""
    async with session_scope() as s:
        await groups.record(s, anchor, {sid: groups.Member("child", state_, "elsewhere")})
        await s.commit()


async def test_hgr_a_withhold_holds_every_row_of_its_group_with_its_evidence(client, fx):
    """Every other row of the withheld conversation and of its markerless child is held with its conversation at the
    withhold (i22 and R1 left them readable): each listed with its evidence against the withheld text in hand ('none'
    for an unrelated row when it is the only withhold, 'contains' for a row repeating it); an unrelated conversation
    stays readable."""
    db = fx["db"]
    w = secret("zqhgr1")
    pa = conversation(db, "hg_p", T, [("user", "What is the deal worth?"), ("assistant", w), ("user", "Thanks.")])
    ca = conversation(db, "hg_c", T + DAY, [("user", "Forward this: %s" % w), ("assistant", "Forwarded.")],
                      parent="hg_p")
    oa = conversation(db, "hg_o", T + 2 * DAY, [("user", "Book the train."), ("assistant", "Booked.")])
    await run(fx)
    await withhold(client, await row_id("hg_p", pa[1]))
    assert [await state("hg_p", r) for r in pa] == ["held", "certain", "held"]
    assert [await state("hg_c", r) for r in ca] == ["held", "held"]
    assert [await state("hg_o", r) for r in oa] == ["readable", "readable"]
    listed = await holds(client)
    unrelated, related = listed[await row_id("hg_p", pa[0])], listed[await row_id("hg_c", ca[0])]
    assert unrelated["relations"] == [identity.NO_RELATION] and unrelated["rule"] == identity.GROUP_HOLD
    assert identity.CONTAINS in related["relations"] and related["confirm_required"]
    assert (await pending())["group_rows_held"] == 4 and (await pending())["group_rows_readable"] == 0
    # the one-click release frees only exactly 'none'; the next import brings the text back, the related row stays
    r = await client.post("/api/brain/sessions/%d/holds/release-unrelated" % await archive_id("hg_c"))
    assert r.status_code == 200, r.text
    await run(fx)
    assert [await state("hg_c", r) for r in ca] == ["held", "readable"]


async def test_hgr_none_needs_every_withheld_text_in_hand(client, fx):
    """A second withhold, in another conversation: the first withheld text is no longer in hand (its row has no text),
    so the second group's unrelated rows are 'other_not_compared', never 'none', and the one-click release skips
    them; Settle, with both texts in the backup, computes 'none' again."""
    db = fx["db"]
    pa = conversation(db, "nn_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqnn1"))])
    qa = conversation(db, "nn_q", T + DAY, [("user", "And the other deal?"), ("assistant", secret("zqnn2"))])
    await run(fx)
    await withhold(client, await row_id("nn_p", pa[1]))
    await withhold(client, await row_id("nn_q", qa[1]))
    h = (await holds(client))[await row_id("nn_q", qa[0])]
    assert h["relations"] == [identity.OTHER_NOT_COMPARED] and h["rule"] == identity.GROUP_HOLD
    r = await client.post("/api/brain/sessions/%d/holds/release-unrelated" % await archive_id("nn_q"))
    assert r.status_code == 200 and r.json()["released"] == []
    assert await state("nn_q", qa[0]) == "held"
    await bj.settle(fx, keep=False)
    assert (await holds(client))[await row_id("nn_q", qa[0])]["relations"] == [identity.NO_RELATION]


async def test_hgr_a_release_under_one_group_never_frees_a_row_of_two(client, fx):
    """A conversation in two groups: an admin's release of one of its rows under one group leaves it held under the
    other (the sweep, the import, and the database guard alike)."""
    db = fx["db"]
    pa = conversation(db, "tg_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqtg1"))])
    ca = conversation(db, "tg_c", T + DAY, [("user", "Draft the follow-up mail."), ("assistant", "Drafted.")])
    await run(fx)
    w = (await message_of("tg_p", pa[1]))[0]
    await withhold(client, w.id)
    await join("tg_c", w.source_key)
    await join("tg_c", "jarvis:message:uid:another-anchor")
    async with session_scope() as s:
        await review.hold_group_rows(s, actor=ADMIN[0], cause="test")
        await s.commit()
    first = await row_id("tg_c", ca[0])
    async with session_scope() as s:
        stone = await s.get(BrainTombstone, (await s.get(ArchiveMessage, first)).source_key)
        assert stone.review == identity.PENDING
        anchor = stone.anchor_key
    # rebuild R4 (B6, D-34): a hold one click could not release ('not in this backup') needs the typed word
    assert (await client.post("/api/brain/holds/%d/release" % first)).status_code == 400
    assert (await client.post("/api/brain/holds/%d/release" % first, json={"confirm": "RELEASE"})).status_code == 200
    out = await run(fx)
    # released under one group: the next import holds it under the other one (the import itself: the guard is quiet)
    assert await state("tg_c", ca[0]) == "held"
    checks = (await imp.verify(out["run_id"], bj.paths(fx)))["verification"]["checks"]
    assert checks["database_guard_quiet"]["ok"], checks["database_guard_quiet"]
    async with session_scope() as s:
        stone = await s.get(BrainTombstone, (await s.get(ArchiveMessage, first)).source_key)
        assert stone.review == identity.PENDING and stone.anchor_key != anchor


async def test_hgr_a_later_backup_s_rows_of_a_group_conversation_are_held_by_the_import(client, fx):
    """A later backup brings new rows to conversations of the group, one of them recorded in the group without the
    sweep (restore's file, older code: its stored rows still readable), and a new markerless child: the import holds
    every such row itself, the stored ones too (the database guard never acts: verify's database_guard_quiet)."""
    db = fx["db"]
    pa = conversation(db, "lb_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqlb"))])
    xa = conversation(db, "lb_x", T + DAY, [("user", "Plan the offsite."), ("assistant", "Planned.")])
    await run(fx)
    w = (await message_of("lb_p", pa[1]))[0]
    await withhold(client, w.id)
    await join("lb_x", w.source_key)
    assert [await state("lb_x", r) for r in xa] == ["readable", "readable"]
    turn = bj.add_row(db, "lb_p", "user", "One more question.", T + 3 * DAY)
    more = bj.add_row(db, "lb_x", "user", "And the agenda?", T + 3 * DAY)
    ka = conversation(db, "lb_k", T + 4 * DAY, [("user", "Draft the mail."), ("assistant", "Drafted.")],
                      parent="lb_p")
    out = await run(fx)
    assert await state("lb_p", turn) == "held" and [await state("lb_k", r) for r in ka] == ["held", "held"]
    assert [await state("lb_x", r) for r in [*xa, more]] == ["held", "held", "held"]
    assert (await pending())["group_rows_readable"] == 0
    checks = (await imp.verify(out["run_id"], bj.paths(fx)))["verification"]["checks"]
    assert checks["database_guard_quiet"]["ok"] and checks["withheld_identity_holds"]["ok"], checks


async def test_hgr_the_database_guard_holds_a_group_row_any_code_writes_readable(client, fx):
    """0008's guard: a row of a group conversation written readable by any code is held (a pending stone in that
    group, listed), unless an admin released that exact row for that group."""
    db = fx["db"]
    ids = conversation(db, "dg_a", T, [("user", "Plan the garden party."), ("assistant", "Planned.")])
    await run(fx)
    await join("dg_a", "jarvis:message:uid:dg-anchor")
    first, second = await row_id("dg_a", ids[0]), await row_id("dg_a", ids[1])
    async with session_scope() as s:
        row = await s.get(ArchiveMessage, second)
        await identity.put_exemption(s, identity.exact_of_row(row, "dg_a"), anchor="jarvis:message:uid:dg-anchor",
                                     reason="test release")
        await s.execute(text("UPDATE archive_messages SET content = content || ' (edited)' WHERE id IN (:a, :b)"),
                        {"a": first, "b": second})
        await s.commit()
    assert await state("dg_a", ids[0]) == "held" and await state("dg_a", ids[1]) == "readable"
    async with session_scope() as s:
        row = await s.get(ArchiveMessage, first)
        assert row.flags["withheld_by_admin"]["guard"] == "group" and row.content == ""
        stone = await s.get(BrainTombstone, row.source_key)
        assert stone.anchor_key == "jarvis:message:uid:dg-anchor" and stone.via == identity.GROUP_HOLD


async def test_r10_the_repair_holds_a_readable_row_of_a_group_conversation_and_hides_its_title(client, fx):
    """R10: a group recorded without the sweep (restore's file, older code): the repair holds every readable row of
    its conversation ('not in this backup': no withheld text in hand) and hides the title and its rollback copies."""
    db = fx["db"]
    ids = conversation(db, "rt_a", T, [("user", "Plan the garden party."), ("assistant", "Planned.")],
                       title="Garden party")
    await run(fx)
    async with session_scope() as s:
        await s.execute(text("ALTER TABLE brain_withheld_sessions DISABLE TRIGGER brain_withheld_sessions_hide_title"))
        await groups.record(s, "jarvis:message:uid:rt-anchor", {"rt_a": groups.Member("child")})
        await s.execute(text("ALTER TABLE brain_withheld_sessions ENABLE TRIGGER brain_withheld_sessions_hide_title"))
        await s.commit()
    assert (await pending())["group_rows_readable"] == 2
    async with session_scope() as s:
        out = await identity.repair_rows(s, cause="startup-check")
        await s.commit()
    assert out["group_rows_held"] == 2 and out["group_titles_hidden"] >= 1
    assert [await state("rt_a", r) for r in ids] == ["held", "held"]
    assert (await pending())["group_rows_readable"] == 0
    listed = await holds(client)
    assert listed[await row_id("rt_a", ids[0])]["relations"] == [identity.NOT_IN_BACKUP]
    async with session_scope() as s:
        assert await s.scalar(select(ArchiveSession.title).where(ArchiveSession.source_session_id == "rt_a")) == ""


async def test_hgr_the_startup_repair_and_a_rollback_hold_the_group_rows(client, fx):
    """The start-up repair holds a group conversation's rows (R10, then the sweep), and a rollback of an import that
    deleted a group row puts it back held, listed (the guard and the sweep), its copies without text."""
    db = fx["db"]
    mark = "zqsrb"
    pa = conversation(db, "sb_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqsbw"))])
    ca = conversation(db, "sb_c", T + DAY, [("user", "Note %s for later." % mark), ("assistant", "Noted.")],
                      parent="sb_p")
    await run(fx)
    bj.change(db, "UPDATE messages SET active = 0 WHERE id = ?", ca[0])  # rewound: the next import deletes it
    deleting = await run(fx)
    assert await message_of("sb_c", ca[0]) == []
    w = (await message_of("sb_p", pa[1]))[0]
    await withhold(client, w.id)
    async with session_scope() as s:  # what a restore of an older dump leaves: the rows readable again
        await s.execute(text("ALTER TABLE archive_messages DISABLE TRIGGER archive_messages_keep_withheld"))
        await s.execute(text("UPDATE archive_messages m SET withheld = false, content = 'Noted.' FROM archive_sessions s "
                             "WHERE s.id = m.session_id AND s.source_session_id = 'sb_c'"))
        await s.execute(text("DELETE FROM brain_tombstones WHERE source_session_id = 'sb_c'"))
        await s.execute(text("ALTER TABLE archive_messages ENABLE TRIGGER archive_messages_keep_withheld"))
        await s.commit()
    assert await state("sb_c", ca[1]) == "readable"
    async with session_scope() as s:
        await identity.repair(s, actor="system (startup-check)", cause="startup-check")
        await s.commit()
    assert await state("sb_c", ca[1]) == "held"
    await imp.rollback(deleting["run_id"], actor=ADMIN[0], confirm=True)
    assert await state("sb_c", ca[0]) == "held" and await readable_with(mark) == 0
    assert await row_id("sb_c", ca[0]) in await holds(client)


async def test_hgr_restore_s_removals_only_file_holds_the_group_rows(client, fx, tmp_path):
    """restore.ps1: a database restored without the holds (nothing withheld yet in the dump), whose import no longer
    holds the withheld row itself (JARVIS deleted it since: the file's withhold is kept for later imports, no withhold
    runs here); the removals-only file brings the withhold and its group, and decisions-apply's sweep holds every row
    of the group's conversations at once."""
    db = fx["db"]
    pa = conversation(db, "rr_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqrr"))])
    ca = conversation(db, "rr_c", T + DAY, [("user", "Draft the follow-up mail."), ("assistant", "Drafted.")],
                      parent="rr_p")
    await run(fx)
    await withhold(client, await row_id("rr_p", pa[1]))
    out = tmp_path / "removals.json"
    await imp.decisions_export(str(out), removals_only=True)
    await _fresh_production()
    async with session_scope() as s:  # an older dump: no group recorded yet
        await s.execute(text("TRUNCATE brain_withheld_sessions, brain_hidden_titles"))
    bj.change(db, "DELETE FROM messages WHERE id = ?", pa[1])
    extra = bj.add_row(db, "rr_c", "user", "One more question.", T + 2 * DAY)  # after the export: no hold carried
    await run(fx)
    assert await state("rr_c", extra) == "readable" and await message_of("rr_p", pa[1]) == []
    await imp.decisions_apply(str(out), None, actor=None, apply=True, removals_only=True)
    assert [await state("rr_c", r) for r in [*ca, extra]] == ["held", "held", "held"]
    assert await state("rr_p", pa[0]) == "held"


async def test_hgr_decision_status_counts_the_group_rows(client, fx):
    """The decision counters (admins, brain-import status): the groups' conversations, the rows held with them and the
    rows still readable without a release (0 after the sweep)."""
    db = fx["db"]
    pa = conversation(db, "ds_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqds")),
                                      ("user", "Thanks.")])
    await run(fx)
    await withhold(client, await row_id("ds_p", pa[1]))
    async with session_scope() as s:
        found = await review.decision_status(s)
    assert found["group_conversations"] == 1 and found["group_rows_held"] == 2 and found["group_rows_readable"] == 0


async def test_hgr_the_legacy_reset_holds_the_group_rows(client, fx, tmp_path):
    """Legacy data (0.6.1: a withhold, no group, the rest of the conversation readable): the legacy reset records the
    group and the sweep holds every row of it."""
    db = fx["db"]
    pa = conversation(db, "lr_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqlr"))])
    ca = conversation(db, "lr_c", T + DAY, [("user", "Draft the follow-up mail."), ("assistant", "Drafted.")],
                      parent="lr_p")
    first = await run(fx)
    await withhold(client, await row_id("lr_p", pa[1]))
    async with session_scope() as s:  # what 0.6.1 left: the withheld row only, no group, its conversation readable
        await s.execute(text("UPDATE import_runs SET importer_version = '1.0.4' WHERE id = :r"),
                        {"r": first["run_id"]})
        await s.execute(text(
            "UPDATE brain_tombstones SET source_session_id = '', source_row_id = '', role = '', tool_call_id = '', "
            "message_uid = '', uid_known = false, decided_on = '', bound = false, source_at = NULL "
            "WHERE reason = :r AND anchor_key = ''"), {"r": identity.WITHHELD_BY_ADMIN})
        await s.execute(text("DELETE FROM brain_tombstones WHERE anchor_key <> ''"))
        await s.execute(text("ALTER TABLE archive_messages DISABLE TRIGGER archive_messages_keep_withheld"))
        await s.execute(text("UPDATE archive_messages m SET withheld = false, content = 'Drafted.', flags = '{}' "
                             "FROM archive_sessions s WHERE s.id = m.session_id AND s.source_session_id = 'lr_c'"))
        await s.execute(text("ALTER TABLE archive_messages ENABLE TRIGGER archive_messages_keep_withheld"))
        await s.execute(text("INSERT INTO brain_maintenance (name, detail) VALUES ('legacy_brain_data', "
                             "'{\"found_by\": \"test\"}'::jsonb)"))
        await s.execute(text("TRUNCATE brain_withheld_sessions"))  # 0.6.1 kept no group
        await s.commit()
    assert await state("lr_c", ca[1]) == "readable"
    out = tmp_path / "reset.json"
    await imp.legacy_reset(str(out), actor=ADMIN[0], confirm=True)
    # the reset's version 5 file carries a hold for each row of the group conversation (decided before the rollbacks),
    # and the re-import the owner runs next holds them
    carried = {r["source_key"]: r for r in json.loads(out.read_text())["removals"]}
    keys = [k for k, r in carried.items() if r.get("via") == identity.GROUP_HOLD and r.get("review") == "pending"]
    assert len(keys) >= 2
    await run(fx)
    assert [await state("lr_c", r) for r in ca] == ["held", "held"]


async def test_hgr_an_import_of_the_decision_backup_holds_a_group_conversation_it_stores_first(client, fx):
    """The test copy imported part of a backup (a date range), the admin withheld a message there, and then imports
    the whole backup: a markerless child the first import left out joins the group in that import and its rows are
    held by the import itself, as it stores them (the database guard never acts)."""
    db = fx["db"]
    pa = conversation(db, "dr_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqdr"))])
    ca = conversation(db, "dr_c", T + 10 * DAY, [("user", "Draft the follow-up mail."), ("assistant", "Drafted.")],
                      parent="dr_p")
    await imp.run(bj.paths(fx), imp.Selection(since="2026-11-17", until="2026-11-19"))
    assert await message_of("dr_c", ca[0]) == []
    await withhold(client, await row_id("dr_p", pa[1]))
    out = await run(fx)
    assert [await state("dr_c", r) for r in ca] == ["held", "held"]
    checks = (await imp.verify(out["run_id"], bj.paths(fx)))["verification"]["checks"]
    assert checks["database_guard_quiet"]["ok"], checks["database_guard_quiet"]


async def test_hgr_a_release_of_a_group_hold_covers_the_content_it_was_compared_on(client, fx):
    """RECON-20: a row held with its conversation was compared on its stored words ('none'); JARVIS then folds the
    withheld text into that very row in place, and the admin releases the hold. The release covers the content it was
    compared on only: the next import holds the row again (its text is the composite now), never readable with the
    withheld text, and never labelled restricted data."""
    db = fx["db"]
    mark = "zqrc20"
    ids = conversation(db, "rc_p", T, [("user", secret(mark)), ("assistant", "Noted."), ("user", "Anything else?")])
    await run(fx)
    await withhold(client, await row_id("rc_p", ids[0]))
    bj.fold(db, "rc_p", survivor=ids[2], dropped=[ids[0]])  # the withheld text folded into the survivor in place
    await run(fx)
    held = (await message_of("rc_p", ids[2]))[0]
    h = (await holds(client))[held.id]
    assert h["rule"] == identity.GROUP_HOLD and h["relations"] == [identity.NO_RELATION]
    assert (await client.post("/api/brain/holds/%d/release" % held.id)).status_code == 200
    await run(fx)
    [again] = await message_of("rc_p", ids[2])
    assert again.withheld and again.content == "" and "withheld" not in (again.flags or {})
    assert again.id in await holds(client)
    assert await readable_with(mark) == 0 and await bj.nowhere(mark) == []
