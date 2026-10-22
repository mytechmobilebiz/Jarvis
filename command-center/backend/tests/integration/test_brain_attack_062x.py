"""The counter-attack on the first 0.6.2 rules revision (rules062), kept as regression tests (rules062-confirm, ported
by the stage that applied it; deviations of 2026-10-21). Synthetic data only.

Each probe asserts the SAFE outcome under the principle (deviation 61 of 2026-10-21): withheld for good only by
provenance; everything uncertain a listed, releasable hold with honest evidence; a release covers only the content it
released. Where the principle fixes the exact outcome (readable, a hold with a given rule, a member), the probe
asserts it, not only "nothing leaked".
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.brain import identity
from app.brain import importer as imp
from app.models import ArchiveMessage, ArchiveSession, BrainTombstone, BrainWithholdExemption  # noqa: F401

from .. import brain_jarvis as bj
from ..conftest import ADMIN
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_rules_062r import (STUB, _family, add_turns, holds_listed, promote_status, readable_with,
                                    rotation_tail, who_reads, withhold_api)
from .test_brain_withhold_identity import DAY, T, conversation, message_of, run, stones, withhold

pytestmark = pytest.mark.usefixtures("clean_db")


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


def secret(mark: str) -> str:
    return "The new vendor contract with %s is worth forty thousand dollars a year." % mark


def show(label: str, value) -> None:
    print("PROBE %s: %s" % (label, json.dumps(value, default=str, sort_keys=True)))


async def state(sid: str, row_id: int) -> tuple:
    rows = await message_of(sid, row_id)
    if not rows:
        return ("no stored row",)
    m = rows[0]
    st = (await stones()).get(m.source_key)
    return (m.withheld, m.content, st and st.via, st and st.review)


def micro_merge(db: str, sid: str, first: int, second: int, ts: float) -> int:
    """Hermes micro-compaction's adjacent-user merge (agent/micro_compaction.py _merge_adjacent_user_turns, then
    archive_and_compact): every active row is archived (compacted 1), the compacted list is written again as new live
    rows with the same uids; the two adjacent user turns become ONE composite that keeps the first one's uid
    (record_absorbed_message: prev keeps its uid) and records the second one's uid in absorbed_message_uids.
    Returns the composite's id."""
    def go(con: sqlite3.Connection) -> int:
        if "absorbed_message_uids" not in {r[1] for r in con.execute("PRAGMA table_info(messages)")}:
            con.execute("ALTER TABLE messages ADD COLUMN absorbed_message_uids TEXT")
        live = con.execute("SELECT id, role, content, message_uid, tool_call_id, tool_name FROM messages "
                           "WHERE session_id = ? AND active = 1 ORDER BY id", (sid,)).fetchall()
        con.execute("UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ? AND active = 1", (sid,))
        composite = 0
        ins = ("INSERT INTO messages (session_id, role, content, message_uid, tool_call_id, tool_name, timestamp, "
               "absorbed_message_uids) VALUES (?,?,?,?,?,?,?,?)")
        by_id = {r[0]: r for r in live}
        for r in live:
            if r[0] == second:
                continue
            if r[0] == first:
                b = by_id[second]
                composite = con.execute(ins, (sid, r[1], r[2] + "\n\n" + b[2], r[3], None, None, ts,
                                              json.dumps([b[3]]))).lastrowid
                continue
            con.execute(ins, (sid, r[1], r[2], r[3], r[4], r[5], ts, None))
        return composite
    return bj._write_db(db, go)


# ======================= Over-reach =======================

A_TEXT = "Please book the dentist for Tuesday at nine in the morning."


@pytest.mark.parametrize("order", ["fold_before_withhold", "fold_after_withhold"])
async def test_x1_fold_survivor_uid_takes_the_other_constituents_own_row(client, fx, order):
    """Two adjacent user turns A and B; Hermes's micro-compaction folds B into A's composite (it keeps A's uid and
    absorbs B's). The admin withholds B. The composite holds B's text, so it is withheld; but A's own archived
    original holds only A's text (another Hermes message, A's uid): it must stay readable (or at most be a hold an
    admin reviews), never be withheld for good."""
    mark = "zqx1fold%s62" % order[5:8]
    bj.add_session(fx["db"], "x1", started=T)
    bj.add_row(fx["db"], "x1", "assistant", "How can I help today?", T + 1)
    a = bj.add_row(fx["db"], "x1", "user", A_TEXT, T + 2)
    b = bj.add_row(fx["db"], "x1", "user", secret(mark), T + 3)
    if order == "fold_before_withhold":
        composite = micro_merge(fx["db"], "x1", a, b, T + 10)
        await run(fx)
        await withhold(client, (await message_of("x1", b))[0].id)
        # 0.6.2 plan 4.5: at withhold time the composite (it contains B) is only listed pre-ticked; Settle decides it
        # certain by its absorbed uid (4.2 rule 3)
        await bj.settle(fx)
    else:
        await run(fx)
        await withhold(client, (await message_of("x1", b))[0].id)
        composite = micro_merge(fx["db"], "x1", a, b, T + 10)
    show("after withhold: A's original", await state("x1", a))
    await run(fx)
    await run(fx)
    show("composite", await state("x1", composite))
    show("A's original", await state("x1", a))
    show("holds", await holds_listed(client))
    assert (await who_reads(client, mark))["readable_rows"] == []  # B's text: withheld everywhere (control)
    [orig_a] = await message_of("x1", a)
    st = (await stones()).get(orig_a.source_key)
    if not orig_a.withheld:
        show("operator promote of A's readable original", await promote_status(client, orig_a.id))
    assert not orig_a.withheld or (st is not None and st.review == identity.PENDING), (
        "A's own message (another uid, never B's text) was withheld for good: %s" % ((st.via, st.review) if st
                                                                                     else None,))
    # The principle's outcome: A's original is another message, readable, with no stone and no exemption needed.
    assert (orig_a.withheld, orig_a.content, st) == (False, A_TEXT, None), (orig_a.withheld, st)
    [comp] = await message_of("x1", composite)
    assert comp.withheld and (await stones())[comp.source_key].review == identity.CERTAIN


@pytest.mark.parametrize("order", ["branch_before_withhold", "branch_after_withhold"])
async def test_x2_gateway_branch_of_a_donor_timestamp_rotation(client, fx, order):
    """Hermes's rotation gives the carried copy of readable row 5 the timestamp of row 1 (the parent's first active row
    with that content: _carry_parent_timestamps); its uid still says it is row 5's copy, so the continuation's copy
    stays readable (the revision's own test). Then the user branches the CONTINUATION in the gateway: the branch copies
    that row with a fresh uid and its (donor) timestamp. Withholding row 1 must not take the branch's copy of row 5's
    copy: it is a copy of a readable row."""
    yes = "Yes, go ahead. zqx2donor%s62" % order[7:10]
    ids = add_turns(fx["db"], "x2", T, _family(yes))
    rotation_tail(fx["db"], "x2", "x2_child", T + DAY, tail_n=4)  # carries rows 4-7: row 5's copy gets row 1's time
    if order == "branch_before_withhold":
        bj.gateway_branch(fx["db"], "x2_child", "x2_gw", T + 2 * DAY)
    await run(fx)
    # 0.6.2 plan 5.3 rv3-fb-4/rv3-ov-2: the rows with row 1's text are listed pre-ticked in the dialog (never certain:
    # the parent has two rows with that text); the admin unticks the ones she knows are row 5's, and that choice stands
    await withhold_api(client, (await message_of("x2", ids[1]))[0].id, leave_readable=[
        m.id for m in await readable_with(yes) if m.id != (await message_of("x2", ids[1]))[0].id])
    if order == "branch_after_withhold":
        bj.gateway_branch(fx["db"], "x2_child", "x2_gw", T + 2 * DAY)
    await run(fx)
    await run(fx)
    t1 = bj.rows(fx["db"], "x2")[1]["timestamp"]
    [child_copy] = [r for r in bj.rows(fx["db"], "x2_child") if r["content"] == yes]
    [gw_copy] = [r for r in bj.rows(fx["db"], "x2_gw") if r["content"] == yes]
    assert child_copy["timestamp"] == t1 == gw_copy["timestamp"]
    assert child_copy["message_uid"] == bj.rows(fx["db"], "x2")[5]["message_uid"]  # row 5's copy, by its uid
    show("parent row 5", await state("x2", ids[5]))
    show("continuation's copy of row 5", await state("x2_child", child_copy["id"]))
    show("gateway branch's copy of it", await state("x2_gw", gw_copy["id"]))
    show("holds", await holds_listed(client))
    assert (await state("x2", ids[5]))[:2] == (False, yes)
    assert (await state("x2_child", child_copy["id"]))[:2] == (False, yes)
    gw = await state("x2_gw", gw_copy["id"])
    assert gw[:2] == (False, yes) or gw[3] == identity.PENDING, (
        "the branch's copy of a readable row was withheld for good: %s" % (gw,))
    assert gw[:2] == (False, yes), gw  # the principle's outcome: a copy of a readable row is readable



# ======================= Leak through the evidence an admin releases on =======================



# ======================= Releases and holds carried to a differently keyed live backup =======================


# 0.6.2 core-1: the "released" variant is retired (as test_brain_rules_062r.test_c2: plan 1.2, 5.3).

# ======================= Rollback copies at rest =======================


@pytest.mark.parametrize("where", ["no_uids", "cli_branch_child"])
async def test_x6_rewound_original_rollback_copy_after_a_later_withhold(client, tmp_path, where):
    """R7 without a uid key: JARVIS compacts in place with the answer in the protected tail; the next import deletes the
    rewound original (its rollback copy keeps the text) and stores the live copy. Then the admin withholds that copy.
    no_uids: a Hermes backup without message_uid (the original's key is row-form). cli_branch_child: the same in a CLI
    branch, whose rows keep the parent's uids (the parent's row holds the uid key, the child's rows are row-form).
    Expected: right after the withhold the text is nowhere at rest, and a rollback of that run brings nothing back."""
    mark = "zqx6%s62" % where[:5].replace("_", "")
    fx = bj.mini(tmp_path / "fx")
    turns = [("user", "Hello."), ("assistant", "Hi."), ("user", "Give me the headline."), ("assistant", secret(mark))]
    sid = "x6"
    if where == "no_uids":
        conversation(fx["db"], sid, T, turns, uid=False)
    else:
        conversation(fx["db"], "x6_parent", T, turns)
        bj.cli_branch(fx["db"], "x6_parent", sid, T + DAY)
        bj.add_row(fx["db"], sid, "user", "Continue in the branch.", T + DAY + 10)
    await run(fx)
    bj.hermes_compact(fx["db"], sid, head_n=1, tail_n=3, ts=T + 2 * DAY)
    second = await run(fx)
    copies = [r for r in bj.rows(fx["db"], sid) if mark in r["content"] and r["active"] == 1]
    [copy] = await message_of(sid, copies[0]["id"])
    assert not copy.withheld
    if where == "cli_branch_child":  # the parent's own row is withheld too (the same uid, the same text)
        pass
    await withhold_api(client, copy.id)
    at_rest = await bj.nowhere(mark)
    show("columns holding the text after the withhold", at_rest)
    from app.bootstrap import check_withheld_copies
    show("start-up check", await check_withheld_copies())
    show("columns after the start-up check", await bj.nowhere(mark))
    res = await imp.rollback(second["run_id"], actor=ADMIN[0], confirm=True)
    show("rollback", {k: res.get(k) for k in ("status", "error") if k in res})
    after = await who_reads(client, mark)
    show("after rollback", {"readable": after["readable_rows"], "api": after["api"], "columns": after["columns"]})
    assert at_rest == [], "the withheld text stays at rest in %s" % at_rest
    assert after["readable_rows"] == [] and after["api"] == [], "a rollback brings the withheld text back"



# ======================= O1 in a backup without message ids =======================



# ======================= A release is bound to a row id whose text Hermes rewrites in place =======================




# ======================= A request restated after a second compaction =======================

END_MARKER = "--- END OF CONTEXT SUMMARY \u2014 respond to the message below, not the summary above ---"


def compaction_merging_inflight(db: str, sid: str, ts: float, inflight_id: int) -> int:
    """Hermes's in-place compaction while the user's last request is unanswered and the transcript ends on a user
    row (agent/context_compressor.py _reappend_inflight_user_task, merge branch): every active row is archived, and
    the handoff carrier (role user) holds the summary, the end marker, then the replay header and the request, and
    records the request's uid in absorbed_message_uids. Returns the carrier's id."""
    def go(con: sqlite3.Connection) -> int:
        if "absorbed_message_uids" not in {r[1] for r in con.execute("PRAGMA table_info(messages)")}:
            con.execute("ALTER TABLE messages ADD COLUMN absorbed_message_uids TEXT")
        content, uid = con.execute("SELECT content, message_uid FROM messages WHERE id = ?", (inflight_id,)).fetchone()
        request = content.rsplit(identity.REPLAY_HEADER, 1)[-1].strip()
        con.execute("UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ? AND active = 1", (sid,))
        return con.execute(
            "INSERT INTO messages (session_id, role, content, message_uid, timestamp, absorbed_message_uids) "
            "VALUES (?,?,?,?,?,?)", (sid, "user", "[CONTEXT COMPACTION] Summary one.\n%s\n\n%s\n%s" % (
                END_MARKER, identity.REPLAY_HEADER, request), bj.new_uid(), ts, json.dumps([uid]))).lastrowid
    return bj._write_db(db, go)



def retry_carrier(db: str, sid: str, carrier_id: int, ts: float) -> tuple[int, int]:
    """/retry of the compaction carrier's request (hermes_state_rewind.rewind_user_turn with the carrier as the target,
    preserve_compaction_handoff; hermes_state_mutation_transcript.rewind_in_transaction): the carrier is soft-deleted
    (active 0), its handoff-only scaffold is inserted as a new hidden row (new uid), and the request text (the live
    view after the end marker, header stripped: _restated_request_text) is sent again as a new user turn (new uid).
    Returns (re-sent row id, answer id)."""
    def go(con: sqlite3.Connection) -> tuple[int, int]:
        content = con.execute("SELECT content FROM messages WHERE id = ?", (carrier_id,)).fetchone()[0]
        request = content.rsplit(identity.REPLAY_HEADER, 1)[-1].strip()
        con.execute("UPDATE messages SET active = 0 WHERE session_id = ? AND id >= ? AND active = 1", (sid, carrier_id))
        ins = "INSERT INTO messages (session_id, role, content, message_uid, timestamp) VALUES (?,?,?,?,?)"
        con.execute(ins, (sid, "user", content.split(END_MARKER, 1)[0] + END_MARKER, bj.new_uid(), ts))
        again = con.execute(ins, (sid, "user", request, bj.new_uid(), ts + 1)).lastrowid
        answer = con.execute(ins, (sid, "assistant", "Done, as requested.", bj.new_uid(), ts + 2)).lastrowid
        return again, answer
    return bj._write_db(db, go)


async def test_x9_retry_of_the_compaction_carrier_resends_the_withheld_request(client, fx):
    """R3 for the request Hermes merged onto the compaction carrier: the user's /retry targets the carrier (the turn
    that holds the request), the carrier is rewound, and the request is sent again as a new turn with a new uid. The
    re-sent text is the withheld request verbatim; it must not be readable (a hold at least, as R3 holds a re-send)."""
    mark = "zqx9retry62"
    bj.add_session(fx["db"], "x9", started=T)
    bj.add_row(fx["db"], "x9", "assistant", "Hello, what can I do for you?", T + 1)
    m = bj.add_row(fx["db"], "x9", "user", secret(mark), T + 2)
    carrier = compaction_merging_inflight(fx["db"], "x9", T + 100, m)
    await run(fx)
    await withhold(client, (await message_of("x9", m))[0].id)
    assert (await state("x9", carrier))[0] is True
    again, _ = retry_carrier(fx["db"], "x9", carrier, T + 200)
    await run(fx)
    show("re-sent request", await state("x9", again))
    after = await who_reads(client, mark)
    show("who reads", {"readable": after["readable_rows"], "api": after["api"][:4]})
    assert after["readable_rows"] == [] and after["api"] == [], "the re-sent withheld request is readable"



# ======================= Controls that must keep holding =======================


async def test_x12_release_then_gateway_branch_then_compaction_of_the_branch(client, fx):
    """A released stub, copied by a gateway branch (a fresh uid), then compacted in place inside the branch (in the
    protected tail): it stays readable everywhere; the withheld output stays withheld."""
    mark = "zqx12rel62"
    from .test_brain_rules_062r import _held_stub
    ids, stub = await _held_stub(client, fx, mark)
    await as_(client, "admin")
    assert (await client.post("/api/brain/holds/%d/release" % stub.id)).status_code == 200
    await run(fx)
    bj.gateway_branch(fx["db"], "c2", "c2_gw", T + DAY)
    bj.add_row(fx["db"], "c2_gw", "user", "And then?", T + DAY + 10)
    await run(fx)
    bj.hermes_compact(fx["db"], "c2_gw", head_n=0, tail_n=3, ts=T + 2 * DAY)
    await run(fx)
    await run(fx)
    show("readable stubs", [(m.id, m.state) for m in await readable_with(STUB)])
    show("holds", await holds_listed(client))
    # The released stub and the branch's copy of it (a safe copy, plan 5.3) stay readable. The compaction's new rows
    # in the branch (this simulator stamps the tail copies with the compaction's time, so none is a safe copy, and the
    # summary and the branch's own turn are new) are held for review, never withheld for good (plan 5.3).
    assert len(await readable_with(STUB)) >= 2
    held = await holds_listed(client)
    assert held and {rule for _, rule in held} == {identity.LATER_HOLD}
    assert not [k for k, st in (await stones()).items() if st.via == identity.LATER_HOLD
                and st.review != identity.PENDING]
    assert await bj.nowhere(mark) == []

