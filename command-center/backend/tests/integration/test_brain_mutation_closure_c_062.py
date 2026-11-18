"""0.6.2 option C (rebuild R1): closing tests of the groups guards the mutation matrix names (m18 rows): the title
trigger by membership alone, the R9 repair of rollback copies written without the sweep, a pending hold never seeding
a group, and a gateway reset never joining by copied history. Each fails with its guard switched off. Synthetic data
only."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from app.brain import groups, identity, review
from app.brain import importer as imp
from app.db import session_scope
from app.models import ArchiveMessage, ArchiveSession, BrainTombstone, BrainWithheldSession, ImportRecord

from .. import brain_jarvis as bj
from ..conftest import ADMIN
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_later_062 import holds, state
from .test_brain_withhold_identity import DAY, T, conversation, message_of, run, secret, withhold

pytestmark = pytest.mark.usefixtures("clean_db")


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


async def group() -> dict[str, tuple[str, str]]:
    async with session_scope() as s:
        return {w.source_session_id: (w.how, w.state)
                for w in (await s.scalars(select(BrainWithheldSession))).all()}


async def join(sid: str, anchor: str = "jarvis:message:uid:anchor") -> None:
    async with session_scope() as s:
        await groups.record(s, anchor, {sid: groups.Member("child", groups.MEMBER, "elsewhere")})
        await s.commit()


async def test_title_trigger_hides_the_title_of_a_group_conversation_by_membership_alone(client, fx):
    """m18:title_trig_groups: a conversation recorded in a group loses its title at once (no withhold in it, no title
    choice), its rollback copies too, and every later write (a re-import) keeps it hidden."""
    db = fx["db"]
    conversation(db, "tt_a", T, [("user", "Plan the garden party.")], title="Garden party plans")
    await run(fx)
    bj.change(db, "UPDATE sessions SET title = 'Garden party plans, revised' WHERE id = 'tt_a'")
    bj.add_row(db, "tt_a", "assistant", "Revised.", T + 50)
    await run(fx)
    async with session_scope() as s:
        copies = (await s.scalars(select(ImportRecord.before["title"].astext).where(
            ImportRecord.target_table == "archive_sessions", ImportRecord.before.has_key("title")))).all()
    assert "Garden party plans" in copies
    await join("tt_a")
    async with session_scope() as s:
        sess = await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == "tt_a"))
        assert sess.title == "" and sess.flags.get("title_withheld") is True
        assert set((await s.scalars(select(ImportRecord.before["title"].astext).where(
            ImportRecord.target_table == "archive_sessions", ImportRecord.before.has_key("title")))).all()) == {""}
    bj.change(db, "UPDATE sessions SET title = 'Garden party plans, third' WHERE id = 'tt_a'")
    await run(fx)
    async with session_scope() as s:
        assert await s.scalar(select(ArchiveSession.title).where(ArchiveSession.source_session_id == "tt_a")) == ""
        await s.execute(text("UPDATE archive_sessions SET title = 'Written by any code' WHERE source_session_id = "
                             "'tt_a'"))
        await s.commit()
        assert await s.scalar(select(ArchiveSession.title).where(ArchiveSession.source_session_id == "tt_a")) == ""


async def test_r9_the_repair_blanks_a_rollback_copy_of_a_group_row_written_without_the_sweep(client, fx):
    """m18:r9_off: a rollback copy of a row of a group conversation that still holds text (no sweep wrote it) is
    blanked by the repair, whatever row of the conversation it copies."""
    db = fx["db"]
    mark = "zqr9"
    ids = conversation(db, "r9_a", T, [("user", "Notes about %s, first draft." % mark), ("assistant", "Noted.")])
    await run(fx)
    bj.change(db, "UPDATE messages SET content = 'Notes, second draft.' WHERE id = ?", ids[0])
    await run(fx)

    async def with_text() -> int:
        async with session_scope() as s:
            return int(await s.scalar(text("SELECT count(*) FROM import_records WHERE record_type = 'message' AND "
                                           "before::text LIKE '%' || :m || '%'"), {"m": mark}))
    assert await with_text() == 1
    await join("r9_a")
    async with session_scope() as s:
        out = await identity.repair_rows(s, cause="startup-check")
        await s.commit()
    assert out["group_rollback_copies_blanked"] == 1 and await with_text() == 0


async def test_a_pending_hold_never_seeds_a_group(client, fx):
    """m18:seed_pending_too: a row of another conversation held for review in a withhold's group (pending) never puts
    its conversation in the group; a member (certain) does."""
    db = fx["db"]
    pa = conversation(db, "sp_a", T, [("user", "What is the deal worth?"), ("assistant", secret("zqsp"))])
    ba = conversation(db, "sp_b", T + 30 * DAY, [("user", "Unrelated: book a table.")])
    ca = conversation(db, "sp_c", T + 31 * DAY, [("user", "Unrelated: call the plumber.")])
    await run(fx)
    [w] = await message_of("sp_a", pa[1])
    await withhold(client, w.id)
    async with session_scope() as s:
        b = (await message_of("sp_b", ba[0]))[0]
        c = (await message_of("sp_c", ca[0]))[0]
        await review.hold_message(s, await s.get(type(b), b.id), anchor=w.source_key, actor="admin@test.local",
                                  cause="test", rule="same message id")
        await review.hold_message(s, await s.get(type(c), c.id), anchor=w.source_key, actor="admin@test.local",
                                  cause="test", rule=identity.FOLD, review=identity.CERTAIN)
        await groups.refresh(s)
        await s.commit()
    found = await group()
    assert "sp_b" not in found and found["sp_c"] == ("withheld_here", "member")


async def test_a_reset_with_copied_looking_rows_never_joins_by_copied_history(client, fx):
    """m18:j8_reset_copies: a gateway reset copies no rows; one whose parent no backup holds, with rows at the very
    role and time of the withheld conversation's rows, joins only as 'uncertain' (held, never a member)."""
    db = fx["db"]
    pa = conversation(db, "rc_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqrc")),
                                      ("user", "Thanks.")])
    bj.add_session(db, "rc_r", started=T + DAY, model_config={"_reset_from": "rc_gone"})
    for n, (role, content) in enumerate([("user", "What is the deal worth?"), ("user", "Thanks.")]):
        bj.add_row(db, "rc_r", role, content, T + (1 if n == 0 else 3))
    await run(fx)
    await withhold(client, (await message_of("rc_p", pa[1]))[0].id)
    assert (await group())["rc_r"] == ("copied_rows", "uncertain")


async def test_a_recorded_group_is_never_forgotten_and_an_uncertain_member_never_becomes_a_member(client):
    """brain_withheld_sessions keeps every conversation (a delete is refused, whatever code runs it) and an uncertain
    link stays uncertain (an update to 'member' is turned back); hidden titles are append-only too."""
    async with session_scope() as s:
        await groups.record(s, "a", {"x": groups.Member("child", groups.UNCERTAIN, "p")})
        await groups.hide_titles(s, ["y"], anchor="a", reason="test")
        await s.commit()
    async with session_scope() as s:
        await s.execute(text("UPDATE brain_withheld_sessions SET state = 'member'"))
        await s.commit()
        assert await s.scalar(text("SELECT state FROM brain_withheld_sessions")) == "uncertain"
    for sql in ("DELETE FROM brain_withheld_sessions", "DELETE FROM brain_hidden_titles",
                "UPDATE brain_hidden_titles SET reason = ''"):
        async with session_scope() as s:
            with pytest.raises(Exception, match="forgotten|append-only"):
                await s.execute(text(sql))


async def test_the_startup_repair_records_the_groups_again(client, fx):
    """A database restored from a dump without the group rows (or without the groups at all): the start-up repair
    computes each withhold's group again from what the database knows, before anything reads it."""
    db = fx["db"]
    pa = conversation(db, "sr_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqsr"))])
    conversation(db, "sr_c", T + DAY, [("user", "Draft the follow-up mail.")], parent="sr_p", title="Follow-up")
    await run(fx)
    await withhold(client, (await message_of("sr_p", pa[1]))[0].id)
    async with session_scope() as s:
        await s.execute(text("TRUNCATE brain_withheld_sessions"))
        await s.execute(text("UPDATE archive_sessions SET title = 'Follow-up' WHERE source_session_id = 'sr_c'"))
        await s.commit()
    async with session_scope() as s:
        await identity.repair(s, actor="system (startup-check)", cause="startup-check")
        await s.commit()
    assert (await group()) == {"sr_p": ("withheld_here", "member"), "sr_c": ("child", "member")}
    async with session_scope() as s:
        assert await s.scalar(select(ArchiveSession.title).where(ArchiveSession.source_session_id == "sr_c")) == ""


# --- rebuild R2: the database guard's branch for group rows, and the rollback of copies a withhold blanked ---------


async def _two_groups(fx) -> tuple[list[int], list[ArchiveMessage]]:
    db = fx["db"]
    ids = conversation(db, "tr_a", T, [("user", "Plan the garden party."), ("assistant", "Planned.")])
    await run(fx)
    await join("tr_a", "jarvis:message:uid:tr-one")
    await join("tr_a", "jarvis:message:uid:tr-two")
    return ids, [(await message_of("tr_a", r))[0] for r in ids]


async def _write_readable(*row_ids: int) -> None:
    async with session_scope() as s:  # any code: a plain update of the stored rows
        await s.execute(text("UPDATE archive_messages SET content = content || ' (edited)' WHERE id = ANY(:ids)"),
                        {"ids": list(row_ids)})
        await s.commit()


async def test_trig_release_any_group_a_release_under_one_group_never_frees_a_row_of_two(client, fx):
    """m18:trig_release_any_group: a conversation in two groups; a row an admin released under one group only is held
    by the guard when any code writes it readable; a row released under both stays readable."""
    ids, (first, second) = await _two_groups(fx)
    async with session_scope() as s:
        for row, anchors in ((first, ["jarvis:message:uid:tr-one"]),
                             (second, ["jarvis:message:uid:tr-one", "jarvis:message:uid:tr-two"])):
            for anchor in anchors:
                await identity.put_exemption(s, identity.exact_of_row(row, "tr_a"), anchor=anchor, reason="test")
        await s.commit()
    await _write_readable(first.id, second.id)
    assert await state("tr_a", ids[0]) == "held" and await state("tr_a", ids[1]) == "readable"
    async with session_scope() as s:
        stone = await s.get(BrainTombstone, first.source_key)
        assert stone.anchor_key == "jarvis:message:uid:tr-two" and stone.review == identity.PENDING


async def test_trig_release_not_exact_a_release_never_frees_another_message_at_its_row_id(client, fx):
    """m18:trig_release_not_exact: a release names a JARVIS row id with its time and uid; another message at that row
    id (Hermes reused it after a snapshot came back: another time, another uid) written readable is held."""
    ids, (first, second) = await _two_groups(fx)
    async with session_scope() as s:
        for anchor in ("jarvis:message:uid:tr-one", "jarvis:message:uid:tr-two"):
            await identity.put_exemption(s, identity.Exact("tr_a", first.source_message_id, first.role,
                                                           first.tool_call_id or "", first.at, "another-uid"),
                                         anchor=anchor, reason="test")
            await identity.put_exemption(s, identity.exact_of_row(second, "tr_a"), anchor=anchor, reason="test")
        await s.commit()
    await _write_readable(first.id, second.id)
    assert await state("tr_a", ids[0]) == "held" and await state("tr_a", ids[1]) == "readable"


async def test_a_rolled_back_carrier_changed_in_place_comes_back_held_on_its_own(client, fx):
    """m18:v_f8_rollback_scope: a row outside every group carried the withheld text, and a later import changed it in
    place (its update copy keeps the older text). The withhold blanks that copy and marks it; a rollback of that
    import puts the row back as a hold of its own (listed, with its evidence), never readable and never an unlisted
    withheld row."""
    db = fx["db"]
    mark = "zqvf8"
    w = secret(mark)
    pa = conversation(db, "vf_p", T, [("user", "What is the deal worth?"), ("assistant", w)])
    xa = conversation(db, "vf_x", T + 5 * DAY, [("user", "Forwarding this: %s" % w), ("assistant", "Thanks.")])
    await run(fx)
    bj.change(db, "UPDATE messages SET content = 'Forwarding this: (removed).' WHERE id = ?", xa[0])
    changing = await run(fx)
    await withhold(client, (await message_of("vf_p", pa[1]))[0].id)
    assert "vf_x" not in await group() and await state("vf_x", xa[0]) == "readable"
    async with session_scope() as s:
        assert int(await s.scalar(text("SELECT count(*) FROM import_records WHERE before::text LIKE '%' || :m || '%'"),
                                  {"m": mark})) == 0
    await imp.rollback(changing["run_id"], actor=ADMIN[0], confirm=True)
    assert await state("vf_x", xa[0]) == "held"
    h = (await holds(client))[(await message_of("vf_x", xa[0]))[0].id]
    assert identity.CONTAINS in h["relations"] and h["rule"] == identity.COPY_HOLD and h["confirm_required"]


async def test_hgr_verify_uncertain_a_left_readable_choice_never_frees_a_row_an_uncertain_link_holds(client, fx):
    """hgr_verify_uncertain_off: an admin's 'left readable' choice at a withhold counts for a certain link only: a row
    of a conversation an uncertain link holds is held by the sweep and by the guard all the same."""
    db = fx["db"]
    ids = conversation(db, "vu_a", T, [("user", "Plan the garden party."), ("assistant", "Planned.")])
    await run(fx)
    await join("vu_a", "jarvis:message:uid:vu-anchor")
    async with session_scope() as s:
        await groups.record(s, "jarvis:message:uid:vu-anchor", {"vu_a": groups.Member("child", groups.UNCERTAIN, "x")})
        row = (await message_of("vu_a", ids[0]))[0]
        await identity.put_exemption(s, identity.exact_of_row(row, "vu_a"), anchor="jarvis:message:uid:vu-anchor",
                                     kind="left_readable", reason=identity.LEFT_READABLE)
        await s.commit()
    await _write_readable(row.id)
    assert await state("vu_a", ids[0]) == "held"
    async with session_scope() as s:
        await review.hold_group_rows(s, actor="system (test)", cause="test")
        await s.commit()
    assert [await state("vu_a", r) for r in ids] == ["held", "held"]
