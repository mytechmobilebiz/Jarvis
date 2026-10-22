"""0.6.2 step 5: later JARVIS backups (RECOMMENDATION.md plan 5.1-5.6, app/brain/later.py). Synthetic data only.

A decision is taken and finished (Settle) on one JARVIS backup; every other backup is a later backup for its
conversation family. There, nothing is inferred: a stored row of the family never changes but to lose its text (5.2);
a new row is withheld for good only on provenance (exact identity, Hermes's copy identity, an absorbed uid), readable
only as a safe copy of a readable stored row of the family read by the same people, and held for review otherwise
(5.3); the family remembers every link it found (5.1); a conversation outside every family is a plain import (5.4);
the tripwire holds what the classifier would have shown with a withheld text in it and fails verify, and lists such
rows outside the families (5.5); a plan says how many rows each withheld conversation would hold (5.6)."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from app.brain import identity, later
from app.brain import importer as imp
from app.db import session_scope
from app.models import ArchiveMessage, ArchiveSession, BrainSessionLink, ImportRecord

from .. import brain_jarvis as bj
from ..conftest import ADMIN
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_withhold_identity import ALL, DAY, T, conversation, message_of, run, secret, stones, withhold

pytestmark = pytest.mark.usefixtures("clean_db")
OTHER = "Please double check the landing page copy when you have a moment."


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


async def holds(client) -> dict[int, dict]:
    await as_(client, "admin")
    r = await client.get("/api/brain/holds")
    assert r.status_code == 200, r.text
    return {h["message"]: h for h in r.json()["holds"]}


async def state(sid: str, row: int) -> str:
    """readable / held / certain / absent, as the contract names them."""
    found = await message_of(sid, row)
    if not found:
        return "absent"
    m = found[-1]
    if not m.withheld:
        return "readable"
    st = (await stones()).get(m.source_key)
    return "held" if st is not None and st.review == identity.PENDING else "certain"


async def links() -> set[tuple[str, str, str]]:
    async with session_scope() as s:
        return {(a, b, how) for a, b, how in (await s.execute(select(
            BrainSessionLink.a, BrainSessionLink.b, BrainSessionLink.how))).all()}


async def pending() -> dict:
    async with session_scope() as s:
        return dict((await s.execute(text("SELECT brain_withhold_pending()"))).scalar())


async def decided(client, fx, sid: str, row: int) -> None:
    """The owner's flow on the decision backup: withhold, Settle, every hold kept."""
    await run(fx)
    await withhold(client, (await message_of(sid, row))[0].id)
    await bj.settle(fx, keep=False)
    for mid in await holds(client):
        assert (await client.post("/api/brain/holds/%d/keep" % mid)).status_code == 200


def uid_of(db: str, sid: str, row: int) -> str:
    return next(r["message_uid"] for r in bj.rows(db, sid) if r["id"] == row)


def ts_of(db: str, sid: str, row: int) -> float:
    return next(r["timestamp"] for r in bj.rows(db, sid) if r["id"] == row)


# --- 5.1 the family ---------------------------------------------------------------------------------------------------


async def test_links_found_through_a_shared_uid_or_copied_history_are_recorded_and_never_forgotten(client, fx):
    """Plan 5.1: a conversation with no recorded link or marker joins the family through a row that carries the
    withheld message's uid ('shared_uid') or a row at or before its start at a stored row's role, call and time
    ('copied_rows'). The import records the link; a later backup where that evidence is gone keeps the family (its new
    rows are held), and a rollback never removes a link."""
    mark = "zqlater1"
    w = secret(mark)
    ids = conversation(fx["db"], "lt1", T, [("user", "What is the deal worth?"), ("assistant", w),
                                            ("user", "Thanks.")])
    await decided(client, fx, "lt1", ids[1])
    u = uid_of(fx["db"], "lt1", ids[1])
    # An unlinked conversation carrying the withheld message's uid (another time), and one whose history starts with a
    # copy of the family's first row (its role and time, before its own start): neither names a parent.
    bj.add_session(fx["db"], "lt1_u", started=T + 2 * DAY)
    shared = bj.add_row(fx["db"], "lt1_u", "assistant", w, T + 2 * DAY + 5, uid=u)
    bj.add_session(fx["db"], "lt1_c", started=T + 3 * DAY)
    copied = bj.add_row(fx["db"], "lt1_c", "user", "What is the deal worth?", ts_of(fx["db"], "lt1", ids[0]))
    later_c = bj.add_row(fx["db"], "lt1_c", "assistant", "Shall I draft the reply?", T + 3 * DAY + 5)
    x = await run(fx)
    found = await links()
    assert ("lt1", "lt1_u", "shared_uid") in found and ("lt1", "lt1_c", "copied_rows") in found
    assert await state("lt1_u", shared) == "held"  # the same uid at another time: never certain (5.3)
    assert identity.IDENTICAL_REL in (await holds(client))[(await message_of("lt1_u", shared))[0].id]["relations"]
    assert await state("lt1_c", later_c) == "held"
    assert await state("lt1_c", copied) == "readable"  # a safe copy of the family's stored readable row
    # The evidence goes (JARVIS deleted those rows); the links stay, so new turns there are still held.
    bj.change(fx["db"], "DELETE FROM messages WHERE id IN (?, ?)", shared, copied)
    again_u = bj.add_row(fx["db"], "lt1_u", "user", "Another question about lunch.", T + 4 * DAY)
    again_c = bj.add_row(fx["db"], "lt1_c", "user", "Another question about the train.", T + 4 * DAY + 1)
    x2 = await run(fx)
    assert await state("lt1_u", again_u) == "held" and await state("lt1_c", again_c) == "held"
    await imp.rollback(x2["run_id"], actor=ADMIN[0], confirm=True)
    await imp.rollback(x["run_id"], actor=ADMIN[0], confirm=True)
    assert {("lt1", "lt1_u", "shared_uid"), ("lt1", "lt1_c", "copied_rows")} <= await links()
    assert await bj.nowhere(mark) == []


# --- 5.2 stored rows of the family never change -----------------------------------------------------------------------


def faithful_fold(db: str, sid: str, first: int, second: int, ts: float) -> int:
    """Hermes's micro-compaction fold as archive_and_compact commits it (tests/property/hermes_sim.py fold): both
    turns archived, the composite inserted as a new row with the first turn's uid and time, recording the second's
    uid as absorbed. Returns the composite's id."""
    def run(con):
        if "absorbed_message_uids" not in {r[1] for r in con.execute("PRAGMA table_info(messages)")}:
            con.execute("ALTER TABLE messages ADD COLUMN absorbed_message_uids TEXT")
        a = con.execute("SELECT content, message_uid, timestamp FROM messages WHERE id = ?", (first,)).fetchone()
        b = con.execute("SELECT content, message_uid FROM messages WHERE id = ?", (second,)).fetchone()
        con.execute("UPDATE messages SET active = 0, compacted = 1 WHERE id IN (?, ?)", (first, second))
        return con.execute(
            "INSERT INTO messages (session_id, role, content, message_uid, timestamp, absorbed_message_uids) "
            "VALUES (?, 'user', ?, ?, ?, ?)", (sid, a[0] + "\n\n" + b[0], a[1], a[2], '["%s"]' % b[1])).lastrowid
    return bj._write_db(db, run)


async def test_a_later_backup_never_rewrites_rekeys_or_deletes_a_stored_family_row(client, fx):
    """Plan 5.2: JARVIS rewrites a readable row of the family in place, rewinds another, gives a third a uid it did
    not have (a new key) and folds the withheld message into a fourth in place: every stored row keeps what it holds
    (the folded one its own words, readable: it is judged by what it holds, never by JARVIS's edit since: owner
    question 7), nothing is deleted or re-keyed, the new key is not stored twice, and the withheld text is nowhere
    readable."""
    mark = "zqlater2"
    w = secret(mark)
    db = fx["db"]
    bj.add_session(db, "lt2", started=T)
    first = bj.add_row(db, "lt2", "user", "Please read me the contract.", T + 1)
    wrow = bj.add_row(db, "lt2", "assistant", w, T + 2)
    rewritten = bj.add_row(db, "lt2", "user", "And the delivery date?", T + 3)
    rewound = bj.add_row(db, "lt2", "assistant", "Next Tuesday, at the latest.", T + 4)
    keyless = bj.add_row(db, "lt2", "user", "Thanks, that is clear.", T + 5, uid=False)
    survivor = bj.add_row(db, "lt2", "assistant", "Noted.", T + 6)
    await decided(client, fx, "lt2", wrow)
    before = {r: (await message_of("lt2", r))[0] for r in (first, rewritten, rewound, keyless, survivor)}
    bj.change(db, "UPDATE messages SET content = ? WHERE id = ?", "And the delivery date, please?", rewritten)
    bj.change(db, "UPDATE messages SET active = 0 WHERE id = ?", rewound)
    bj.change(db, "UPDATE messages SET message_uid = ? WHERE id = ?", bj.new_uid(), keyless)
    bj.fold(db, "lt2", survivor, [wrow])  # the survivor absorbs the withheld message's uid
    await run(fx)
    for r in (first, rewritten, rewound, keyless, survivor):
        [now] = await message_of("lt2", r)
        assert (now.id, now.source_key, now.content, now.withheld) == (
            before[r].id, before[r].source_key, before[r].content, before[r].withheld), r
    assert before[survivor].content == "Noted." and before[survivor].source_key not in await stones()
    async with session_scope() as s:
        assert len((await s.scalars(select(ArchiveMessage.id).join(ArchiveSession).where(
            ArchiveSession.source_session_id == "lt2"))).all()) == len(before) + 1
    assert await bj.nowhere(mark) == []


async def test_a_fold_new_in_a_later_backup_that_absorbed_the_withheld_uid_is_certain(client, fx):
    """Plan 5.3 CERTAIN: Hermes folds two adjacent user turns into a new composite row that records the withheld
    message's uid as absorbed: withheld for good ('merged into another message'); the archived turns stay as stored."""
    mark = "zqlater2b"
    db = fx["db"]
    ids = conversation(db, "lt2b", T, [("user", "Please look again at the backup schedule."), ("user", secret(mark)),
                                       ("assistant", "Noted both of them.")])
    await decided(client, fx, "lt2b", ids[1])
    composite = faithful_fold(db, "lt2b", ids[0], ids[1], T + DAY)
    await run(fx)
    [row] = await message_of("lt2b", composite)
    st = (await stones())[row.source_key]
    assert row.withheld and (st.via, st.review) == (identity.FOLD, identity.CERTAIN)
    assert await state("lt2b", ids[0]) == "readable"
    assert await bj.nowhere(mark) == []


# --- 5.3 the outcomes -------------------------------------------------------------------------------------------------


async def test_certain_only_by_exact_or_copy_identity_and_held_otherwise(client, fx):
    """Plan 5.3: a CLI branch made after the decision copies the withheld message with its uid and time: certain
    ('copied by JARVIS'). JARVIS repeating the text under the same uid at another time, and a gateway branch's copy
    (fresh uid, the same time and text), are held with their evidence, never certain; the branch's copy of a readable
    row is a safe copy; a new turn is held with no relation."""
    mark = "zqlater3"
    w = secret(mark)
    db = fx["db"]
    ids = conversation(db, "lt3", T, [("user", "What is the deal worth?"), ("assistant", w),
                                      ("user", OTHER), ("assistant", "Will do.")])
    await decided(client, fx, "lt3", ids[1])
    cli = bj.cli_branch(db, "lt3", "lt3_cli", T + DAY)
    gw = bj.gateway_branch(db, "lt3", "lt3_gw", T + DAY + 10)
    again = bj.add_row(db, "lt3", "assistant", w, T + 2 * DAY, uid=uid_of(db, "lt3", ids[1]))
    new_turn = bj.add_row(db, "lt3_gw", "user", "Shall we book the venue?", T + 2 * DAY + 5)
    await run(fx)
    st = await stones()
    [cli_copy] = await message_of("lt3_cli", cli[1])
    assert cli_copy.withheld and (st[cli_copy.source_key].review, st[cli_copy.source_key].via) == (
        identity.CERTAIN, "copied by JARVIS")
    found = await holds(client)
    [again_row] = await message_of("lt3", again)
    assert again_row.withheld and {identity.IDENTICAL_REL} <= set(found[again_row.id]["relations"])
    [gw_copy] = await message_of("lt3_gw", gw[1])
    assert set(found[gw_copy.id]["relations"]) >= {identity.IDENTICAL_REL, identity.SAME_TIME_REL}
    assert await state("lt3_gw", gw[2]) == "readable"  # a safe copy of the readable request
    assert await state("lt3_cli", cli[2]) == "readable"
    [turn] = await message_of("lt3_gw", new_turn)
    assert found[turn.id]["relations"] == [identity.NO_RELATION] and found[turn.id]["rule"] == identity.LATER_HOLD
    assert await bj.nowhere(mark) == []


async def test_a_safe_copy_needs_the_same_readers(client, fx):
    """Plan 5.3 (section 2 correction 3): the copy of a readable stored row is readable only when whoever reads the
    copy could read the stored row. A row of a customer conversation (WhatsApp: customer data) copied into a CLI
    branch (general data, readable by operators) is held; copied into a branch of the same channel it is readable."""
    mark = "zqlater4"
    db = fx["db"]
    ids = conversation(db, "lt4", T, [("user", "What is the deal worth?"), ("assistant", secret(mark)),
                                      ("user", OTHER)], source="whatsapp")
    await decided(client, fx, "lt4", ids[1])
    [stored] = await message_of("lt4", ids[2])
    assert stored.sensitivity == "customer" and not stored.withheld
    wider = bj.gateway_branch(db, "lt4", "lt4_cli", T + DAY)  # source 'cli': general data
    bj.add_session(db, "lt4_wa", started=T + DAY + 10, parent="lt4", model_config={"_branched_from": "lt4"},
                   source="whatsapp")
    same = bj.add_row(db, "lt4_wa", "user", OTHER, ts_of(db, "lt4", ids[2]))
    await run(fx)
    assert await state("lt4_cli", wider[2]) == "held"
    assert await state("lt4_wa", same) == "readable"


async def test_a_safe_copy_points_only_at_a_readable_row_of_the_same_family(client, fx):
    """Plan 5.3 (section 2 correction 3): a new row of the family with the very role, time and text of a readable row
    of an unrelated conversation is not a safe copy of it: held. The same row copied from the family's own readable
    row is readable."""
    mark = "zqlater13"
    db = fx["db"]
    ids = conversation(db, "lt13", T, [("user", "What is the deal worth?"), ("assistant", secret(mark))])
    unrelated = conversation(db, "lt13_u", T + 5 * DAY, [("user", OTHER), ("assistant", "Sure.")])
    await decided(client, fx, "lt13", ids[1])
    assert await state("lt13_u", unrelated[0]) == "readable"
    bj.add_session(db, "lt13_b", started=T + 6 * DAY, parent="lt13", model_config={"_branched_from": "lt13"})
    lookalike = bj.add_row(db, "lt13_b", "user", OTHER, ts_of(db, "lt13_u", unrelated[0]))
    own = bj.add_row(db, "lt13_b", "user", "What is the deal worth?", ts_of(db, "lt13", ids[0]))
    await run(fx)
    assert await state("lt13_b", lookalike) == "held"
    assert await state("lt13_b", own) == "readable"


async def test_a_new_conversation_of_the_family_keeps_its_title_hidden_until_no_row_of_it_is_withheld(client, fx):
    """Plan 5.3: a conversation a later backup brings new into a withheld family has its title hidden while a row of
    it is withheld by an admin; once its holds are released, the next import brings the title back."""
    mark = "zqlater5"
    db = fx["db"]
    ids = conversation(db, "lt5", T, [("user", "What is the deal worth?"), ("assistant", secret(mark))])
    await decided(client, fx, "lt5", ids[1])
    bj.add_session(db, "lt5_b", started=T + DAY, parent="lt5", model_config={"_branched_from": "lt5"},
                   title="Branch about the garden party")
    turn = bj.add_row(db, "lt5_b", "user", "Shall we book the venue for the garden party?", T + DAY + 5)
    await run(fx)
    async with session_scope() as s:
        sess = await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == "lt5_b"))
        assert sess.title == "" and sess.flags.get("title_held") == later.TITLE_HELD
    [row] = await message_of("lt5_b", turn)
    await as_(client, "admin")
    assert (await client.post("/api/brain/holds/%d/release" % row.id)).status_code == 200
    await run(fx)
    async with session_scope() as s:
        sess = await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == "lt5_b"))
        assert sess.title == "Branch about the garden party" and "title_held" not in sess.flags
    assert await state("lt5_b", turn) == "readable"


async def test_a_conversation_an_admin_stopped_importing_is_left_out_and_counted(client, fx):
    """Plan 5.3, 9.3: 'stop importing this conversation': later backups leave it out (records 'not imported', the
    report and the status count it); what is stored of it stays as it is."""
    mark = "zqlater6"
    db = fx["db"]
    ids = conversation(db, "lt6", T, [("user", "What is the deal worth?"), ("assistant", secret(mark)),
                                      ("user", OTHER)])
    await decided(client, fx, "lt6", ids[1])
    [sess_id] = {m.session_id for m in await message_of("lt6", ids[0])}
    await as_(client, "admin")
    r = await client.post("/api/brain/sessions/%d/stop-importing" % sess_id)
    assert r.status_code == 200, r.text
    new_turn = bj.add_row(db, "lt6", "user", "Anything else for today?", T + DAY)
    done = await run(fx)
    assert await state("lt6", new_turn) == "absent" and await state("lt6", ids[2]) == "readable"
    assert done["report"]["frozen_sessions_skipped"] == 1
    async with session_scope() as s:
        reasons = set((await s.scalars(select(ImportRecord.reason).where(
            ImportRecord.run_id == done["run_id"], ImportRecord.source_key.like("jarvis:%")))).all())
    assert later.FROZEN in reasons
    assert (await pending())["frozen_sessions"] == 1
    assert (await imp.status())["frozen_conversations"] == 1
    v = await imp.verify(done["run_id"], bj.paths(fx))
    assert v["passed"], {k: c for k, c in v["verification"]["checks"].items() if not c["ok"]}


# --- 5.4 outside every family; 5.5 the tripwire -------------------------------------------------------------------


async def test_outside_every_family_a_plain_import_and_the_tripwire_lists_the_withheld_text_by_location(client, fx):
    """Plan 5.4, 5.5 (owner question 8 (b)): a conversation outside every family that says the withheld text again is
    imported as it is (never withheld for its text); the tripwire lists that row to admins by its archive ids, with no
    text and no fingerprint; verify passes (inside the families nothing was found)."""
    mark = "zqlater7"
    w = secret(mark)
    db = fx["db"]
    ids = conversation(db, "lt7", T, [("user", "What is the deal worth?"), ("assistant", w)])
    await decided(client, fx, "lt7", ids[1])
    elsewhere = conversation(db, "lt7_other", T + DAY, [("user", "Remind me: %s" % w), ("assistant", "Yes.")])
    done = await run(fx)
    assert await state("lt7_other", elsewhere[0]) == "readable"
    [row] = await message_of("lt7_other", elsewhere[0])
    tw = done["report"]["tripwire"]
    assert (tw["inside_family"], tw["outside_family"]) == (0, 1)
    assert tw["outside"] == [{"session": row.session_id, "message": row.id}]
    last = (await pending())["last_tripwire"]
    assert (last["run"], last["outside_family"]) == (done["run_id"], 1) and mark not in str(last)
    status = await imp.status()
    assert status["last_tripwire"]["outside"] == [{"session": row.session_id, "message": row.id}]
    v = await imp.verify(done["run_id"], bj.paths(fx))
    assert v["passed"] and v["verification"]["checks"]["tripwire_quiet_in_withheld_families"]["ok"]


async def test_the_tripwire_holds_a_safe_copy_with_the_withheld_text_and_fails_verify(client, fx):
    """Plan 5.5: inside a family, a row the classifier would show (a safe copy of a readable stored row nobody decided
    on) that is the withheld text is held instead, and the run's verify fails, naming where. Here a conversation that
    held an identical, independent row joins the family later (through the withheld uid), and its branch copies that
    row."""
    mark = "zqlater8"
    w = secret(mark)
    db = fx["db"]
    ids = conversation(db, "lt8", T, [("user", "What is the deal worth?"), ("assistant", w)])
    elsewhere = conversation(db, "lt8_q", T + DAY, [("user", "Read it again?"), ("assistant", w)])
    await decided(client, fx, "lt8", ids[1])
    assert await state("lt8_q", elsewhere[1]) == "readable"  # another conversation: never withheld for its text
    bj.add_row(db, "lt8_q", "assistant", "Noted.", T + 2 * DAY, uid=uid_of(db, "lt8", ids[1]))  # joins the family
    branch = bj.gateway_branch(db, "lt8_q", "lt8_qb", T + 3 * DAY)
    done = await run(fx)
    assert await state("lt8_qb", branch[1]) == "held"
    [held] = await message_of("lt8_qb", branch[1])
    tw = done["report"]["tripwire"]
    assert tw["inside_family"] == 1 and tw["inside"] == [{"session": held.session_id, "message": held.id}]
    v = await imp.verify(done["run_id"], bj.paths(fx))
    assert not v["passed"] and not v["verification"]["checks"]["tripwire_quiet_in_withheld_families"]["ok"]
    assert await state("lt8_qb", branch[0]) == "readable"  # the other copied row: a safe copy
    assert mark not in str((await pending())["last_tripwire"])


async def test_a_safe_copy_of_a_row_the_admin_left_readable_is_not_a_tripwire_hit(client, fx):
    """Plan 5.5: an identical row the admin chose to leave readable is the admin's decision; its later copies are safe
    copies, readable, and the tripwire counts them as decided, not as a miss."""
    mark = "zqlater9"
    w = secret(mark)
    db = fx["db"]
    ids = conversation(db, "lt9", T, [("user", "What is the deal worth?"), ("assistant", w),
                                      ("user", "Again please?"), ("assistant", w)])
    await run(fx)
    [twin] = await message_of("lt9", ids[3])
    await as_(client, "admin")
    r = await client.post("/api/brain/messages/%d/withhold" % (await message_of("lt9", ids[1]))[0].id,
                          json={"leave_readable": [twin.id]})
    assert r.status_code == 200, r.text
    await bj.settle(fx, keep=False)
    for mid in await holds(client):
        r = await client.post("/api/brain/holds/%d/release" % mid, json={"confirm": identity.CONFIRM_WORD})
        assert r.status_code == 200, r.text
    branch = bj.gateway_branch(db, "lt9", "lt9_b", T + DAY)
    done = await run(fx)
    assert await state("lt9_b", branch[3]) == "readable"
    assert await state("lt9_b", branch[1]) == "held"
    tw = done["report"]["tripwire"]
    assert tw["inside_family"] == 0 and tw["admin_decided"] >= 1


# --- 5.6 the plan; the one-click release -------------------------------------------------------------------------


async def test_a_plan_reports_how_many_rows_each_withheld_conversation_would_hold(client, fx):
    """Plan 5.6: jarvis-import -Mode Plan (brain-import plan) counts, per withheld conversation (its archive id), the
    rows a later backup would hold, withhold for good and show as safe copies; it writes nothing."""
    mark = "zqlater10"
    db = fx["db"]
    ids = conversation(db, "lt10", T, [("user", "What is the deal worth?"), ("assistant", secret(mark)),
                                       ("user", OTHER)])
    await decided(client, fx, "lt10", ids[1])
    bj.cli_branch(db, "lt10", "lt10_b", T + DAY)
    bj.add_row(db, "lt10", "user", "Book the venue.", T + 2 * DAY)
    bj.add_row(db, "lt10", "assistant", "Booked.", T + 2 * DAY + 1)
    report = await imp.plan(bj.paths(fx), ALL)
    [sess_id] = {m.session_id for m in await message_of("lt10", ids[0])}
    counts = report["later_backup"][str(sess_id)]
    assert counts["rows_held_for_review"] == 2 and counts["rows_withheld_for_good"] == 1
    assert counts["safe_copies"] == 2 and counts["new_conversations"] == 1
    assert await message_of("lt10_b", 1) == [] and await state("lt10", ids[2]) == "readable"


async def test_release_rows_with_no_relation_and_its_gate(client, fx):
    """Section 2 correction 4, plan 9.3: 'release this conversation's rows with no relation' releases exactly the
    holds whose evidence is 'none'; it is refused while a hold of the conversation could not be compared because the
    backup lacked a withheld text ('not in this backup')."""
    mark = "zqlater11"
    w = secret(mark)
    db = fx["db"]
    ids = conversation(db, "lt11", T, [("user", "What is the deal worth?"), ("assistant", w)])
    await decided(client, fx, "lt11", ids[1])
    unrelated = bj.add_row(db, "lt11", "user", "Shall we book the venue?", T + DAY)
    related = bj.add_row(db, "lt11", "user", "Remind me: %s" % w, T + DAY + 1)
    await run(fx)
    [sess_id] = {m.session_id for m in await message_of("lt11", ids[0])}
    await as_(client, "admin")
    r = await client.post("/api/brain/sessions/%d/holds/release-unrelated" % sess_id)
    assert r.status_code == 200, r.text
    assert r.json()["released"] == [(await message_of("lt11", unrelated))[0].id]
    assert await state("lt11", related) == "held"
    # JARVIS deleted the withheld message: what is new can no longer be compared with it
    bj.change(db, "DELETE FROM messages WHERE id = ?", ids[1])
    newer = bj.add_row(db, "lt11", "user", "And the catering?", T + 2 * DAY)
    await run(fx)
    assert identity.NOT_IN_BACKUP in (await holds(client))[(await message_of("lt11", newer))[0].id]["relations"]
    r = await client.post("/api/brain/sessions/%d/holds/release-unrelated" % sess_id)
    assert r.status_code == 400 and "not in this backup" in r.text
    assert await state("lt11", newer) == "held"


# --- the key of a withheld message passing to another row ---------------------------------------------------------


async def test_the_withheld_key_passes_to_another_row_only_with_its_copy_identity(client, fx):
    """Plan 5.3 CERTAIN: when JARVIS deletes the withheld row, its uid key names the next row with that uid. A CLI
    branch's copy (the same uid, time, role and call) is the withheld message: certain. A row with that uid at another
    time is never withheld for good through the key: it is left out as a key collision."""
    mark = "zqlater12"
    w = secret(mark)
    db = fx["db"]
    ids = conversation(db, "lt12", T, [("user", "What is the deal worth?"), ("assistant", w)])
    copy = bj.cli_branch(db, "lt12", "lt12_b", T + DAY)
    await decided(client, fx, "lt12", ids[1])
    u = uid_of(db, "lt12", ids[1])
    [stored_copy] = await message_of("lt12_b", copy[1])
    assert stored_copy.withheld and stored_copy.source_key != identity.UID_KEY + u  # the uid key was W's
    bj.change(db, "DELETE FROM messages WHERE id = ?", ids[1])
    done = await run(fx)
    # the uid key now names the branch's copy, whose stored row stays as it is (plan 5.2: never re-keyed)
    [now] = await message_of("lt12_b", copy[1])
    assert (now.id, now.source_key, now.withheld) == (stored_copy.id, stored_copy.source_key, True)
    async with session_scope() as s:
        rec = await s.scalar(select(ImportRecord).where(ImportRecord.run_id == done["run_id"],
                                                         ImportRecord.source_key == identity.UID_KEY + u))
    assert rec is not None and rec.action in ("duplicate", "excluded") and not rec.reason.startswith("key coll"), (
        rec.action, rec.reason)
    # the copy goes too, and a row with that uid at another time is all that is left of it: never stored (its key is
    # the withheld row's, whose stored row stays withheld), never readable
    bj.change(db, "DELETE FROM messages WHERE id = ?", copy[1])
    stray = bj.add_row(db, "lt12", "assistant", "A different answer altogether.", T + 3 * DAY, uid=u)
    done = await run(fx)
    assert await message_of("lt12", stray) == []
    [w_row] = await message_of("lt12", ids[1])
    assert w_row.withheld and w_row.source_key == identity.UID_KEY + u
    async with session_scope() as s:
        rec = await s.scalar(select(ImportRecord).where(ImportRecord.run_id == done["run_id"],
                                                         ImportRecord.source_key == identity.UID_KEY + u))
    assert rec.action in ("duplicate", "excluded") and rec.target_id in (None, w_row.id)
    async with session_scope() as s:
        assert not await s.scalar(select(ArchiveMessage.id).where(
            ArchiveMessage.content == "A different answer altogether."))
    assert await bj.nowhere(mark) == []


# --- 5.2 inside Settle, and a rollback (round-3 replay seed 322) ------------------------------------------------------


async def test_settle_of_a_newer_decision_keeps_the_stored_rows_of_an_earlier_decisions_family(client, fx):
    """A conversation holds a decision taken on one backup. JARVIS then folds that withheld text into another stored
    row in place, a later backup is imported, and the admin withholds another row there and finishes it (Settle on the
    later backup). For the first decision that backup is a later one (plan 5.2): the stored row keeps its own words,
    readable, and the fold holding the first withheld text is never stored, neither by the import nor by Settle.
    Round-3 replay seed 322 found Settle writing the fold readable. A rollback of that Settle and a re-import change
    nothing."""
    mark1, mark2 = "zqlater13a", "zqlater13b"
    db = fx["db"]
    ids = conversation(db, "lt13", T, [("user", "Please handle the vendor review today."), ("assistant", "On it."),
                                       ("user", secret(mark1)), ("assistant", "Noted."),
                                       ("user", "Also: " + secret(mark2)), ("assistant", "Understood.")])
    await decided(client, fx, "lt13", ids[2])
    [first] = await message_of("lt13", ids[0])
    bj.fold(db, "lt13", survivor=ids[0], dropped=[ids[2]])  # in place: the survivor's text holds the withheld one
    newer = bj.add_row(db, "lt13", "user", "One more thing for the review.", T + DAY)
    await run(fx)  # a later backup for the first decision
    assert await state("lt13", newer) == "held"
    await withhold(client, (await message_of("lt13", ids[4]))[0].id)  # decided on this later backup
    settled = await bj.settle(fx)
    [now] = await message_of("lt13", ids[0])
    assert (now.id, now.source_key, now.content, now.withheld, list(now.absorbed_uids or [])) == (
        first.id, first.source_key, "Please handle the vendor review today.", False, [])
    assert await bj.nowhere(mark1) == [] and await bj.nowhere(mark2) == []
    await imp.rollback(settled["run_id"], actor=ADMIN[0], confirm=True)
    await run(fx)
    [again] = await message_of("lt13", ids[0])
    assert (again.content, again.withheld, list(again.absorbed_uids or [])) == (now.content, False, [])
    assert await bj.nowhere(mark1) == [] and await bj.nowhere(mark2) == []


async def test_a_rollback_puts_back_the_uids_a_fold_absorbed(client, fx):
    """A plain import that brings a fold JARVIS made in place (the survivor's text now holds another message, and it
    records that message's uid as absorbed) is undone by a rollback with its absorbed uids: the restored row is not
    the folded message by provenance (plan 3.1, 4.2 (3)), so a later withhold of the folded message never withholds
    it for words it does not hold. Round-3 replay seed 322 found a rolled-back run's absorbed uids left behind."""
    db = fx["db"]
    ids = conversation(db, "lt14", T, [("user", "Please send the agenda."), ("assistant", "Sent."),
                                       ("user", "And the minutes?")])
    await run(fx)
    gone = uid_of(db, "lt14", ids[2])
    bj.fold(db, "lt14", survivor=ids[0], dropped=[ids[2]])
    folded = await run(fx)
    [row] = await message_of("lt14", ids[0])
    assert "And the minutes?" in row.content and list(row.absorbed_uids or []) == [gone]
    await imp.rollback(folded["run_id"], actor=ADMIN[0], confirm=True)
    [back] = await message_of("lt14", ids[0])
    assert (back.content, list(back.absorbed_uids or [])) == ("Please send the agenda.", [])
