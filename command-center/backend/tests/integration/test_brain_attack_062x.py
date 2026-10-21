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
from sqlalchemy import text

from app.brain import identity
from app.brain import importer as imp
from app.db import session_scope
from app.models import ArchiveMessage, ArchiveSession, BrainTombstone, BrainWithholdExemption  # noqa: F401

from .. import brain_jarvis as bj
from ..conftest import ADMIN
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_rules_062r import (STUB, _family, add_turns, holds_listed, promote_status, readable_rows,
                                    readable_with, rotation_tail, who_reads, withhold_api)
from .test_brain_withhold_identity import DAY, T, _fresh_production, conversation, message_of, run, stones, withhold

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
    await withhold(client, (await message_of("x2", ids[1]))[0].id)
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


async def imp_records(key: str) -> list[tuple]:
    async with session_scope() as s:
        return [tuple(r) for r in (await s.execute(text(
            "SELECT run_id, action, reason, target_id FROM import_records WHERE source_key = :k ORDER BY id"),
            {"k": key})).all()]


async def holds_full(client) -> list[dict]:
    await as_(client, "admin")
    r = await client.get("/api/brain/holds")
    assert r.status_code == 200, r.text
    return r.json()["holds"]


async def test_x3_same_uid_composite_holding_the_short_request_is_labelled_different_text(client, fx):
    """The user's short request M ("Pay ... now.") and the next user turn X are folded by Hermes's micro-compaction:
    the composite KEEPS M's uid (M's text comes first) and holds M's text verbatim. Stored before the withhold. The
    admin withholds M's own row: the composite is held, and the holds list must not tell the admin it has "different
    text" (R6: the evidence an admin releases on). Released on that evidence, M's text is readable again."""
    mark = "zqx3pay"
    short = "Pay %s now." % mark
    bj.add_session(fx["db"], "x3", started=T)
    bj.add_row(fx["db"], "x3", "assistant", "How can I help today?", T + 1)
    m = bj.add_row(fx["db"], "x3", "user", short, T + 2)
    x = bj.add_row(fx["db"], "x3", "user", "Also the rent for March, please, by Friday.", T + 3)
    composite = micro_merge(fx["db"], "x3", m, x, T + 10)
    await run(fx)
    await withhold(client, (await message_of("x3", m))[0].id)
    await run(fx)
    [comp] = await message_of("x3", composite)
    listed = [h for h in await holds_full(client) if h["message"] == comp.id]
    show("composite", await state("x3", composite))
    show("holds entry", [(h["rule"], h["compare"]) for h in listed])
    assert comp.withheld
    evidence = [h["compare"] for h in listed]
    await as_(client, "admin")
    r = await client.post("/api/brain/holds/%d/release" % comp.id)
    show("release on that evidence", r.status_code)
    await run(fx)
    after = await who_reads(client, mark)
    show("after release + import", after["readable_rows"])
    assert evidence != ["different text"], "the holds list says 'different text' for a row holding the withheld request"
    assert evidence == ["contains the withheld text"], evidence
    assert r.status_code == 200 and (await state("x3", composite))[0] is False  # the admin's release sticks
    assert (await state("x3", m))[0] is True  # the request's own row stays withheld



async def test_x3b_withhold_time_evidence_for_a_restated_or_containing_row(client, fx):
    """The same composite as x3, looked at right after the withhold (before any import): group_rows holds it as
    'same message id, different text', while the import treats it as a certain member (it keeps M's uid and holds
    M's text). The holds list must not say 'different text', and a release the API accepts must stick."""
    mark = "zqx3bpay"
    short = "Pay %s now." % mark
    bj.add_session(fx["db"], "x3b", started=T)
    bj.add_row(fx["db"], "x3b", "assistant", "How can I help today?", T + 1)
    m = bj.add_row(fx["db"], "x3b", "user", short, T + 2)
    x = bj.add_row(fx["db"], "x3b", "user", "Also the rent for March, please, by Friday.", T + 3)
    composite = micro_merge(fx["db"], "x3b", m, x, T + 10)
    await run(fx)
    await withhold(client, (await message_of("x3b", m))[0].id)
    [comp] = await message_of("x3b", composite)
    listed = [(h["rule"], h["compare"]) for h in await holds_full(client) if h["message"] == comp.id]
    show("holds entry right after the withhold", listed)
    await as_(client, "admin")
    r = await client.post("/api/brain/holds/%d/release" % comp.id)
    show("release", r.status_code)
    await run(fx)
    show("composite after the next import", await state("x3b", composite))
    assert [c for _, c in listed] != ["different text"], listed
    assert listed == [(identity.CONTAINS_SHORT, "contains the withheld text")], listed
    assert r.status_code == 200 and (await state("x3b", composite))[0] is False  # a release the API accepted sticks


# ======================= Releases and holds carried to a differently keyed live backup =======================


@pytest.mark.parametrize("decided", ["released", "unreviewed"])
async def test_x4_test_copy_decision_on_a_renumbered_live_backup(client, tmp_path, decided):
    """Test copy: the admin withholds a full tool output (the prune twin); its pruned stub (the same Hermes message,
    other text) is held; the admin releases it (or leaves it for review). Export. Live imports the CURRENT backup, in
    which Hermes's replace_messages renumbered the conversation (new row ids, the same uids). decisions-apply must keep
    the admin's decision: the released stub readable (or the unreviewed stub a hold), never withheld for good."""
    mark = "zqx4%s62" % decided[:3]
    fx = bj.mini(tmp_path / "fx")
    from .test_brain_rules_062r import _held_stub
    ids, stub = await _held_stub(client, fx, mark)
    if decided == "released":
        await as_(client, "admin")
        assert (await client.post("/api/brain/holds/%d/release" % stub.id)).status_code == 200
        await run(fx)
        assert (await state("c2", ids[2]))[:2] == (False, STUB)
    out = tmp_path / "decisions.json"
    await imp.decisions_export(str(out))
    data = json.loads(out.read_text())
    show("exported", {"removals": [(r["source_key"][-14:], r["review"], r["row"]) for r in data["removals"]],
                      "releases": data["releases"]})
    mapping = bj.replace_renumber(fx["db"], "c2")
    await _fresh_production()
    live = await run(fx)
    applied = await imp.decisions_apply(str(out), live["run_id"], actor=ADMIN[0], apply=True)
    show("apply", {k: applied[k] for k in ("removals", "removals_not_applied", "releases", "releases_not_applied")})
    await run(fx)
    new_stub = mapping[ids[2]]
    st = await state("c2", new_stub)
    show("live stub", st)
    show("live holds", await holds_listed(client))
    assert (await who_reads(client, mark))["readable_rows"] == []
    if decided == "released":
        assert st[:2] == (False, STUB), "the admin's release on the test copy is lost on live: %s" % (st,)
    else:
        assert st[0] and st[3] == identity.PENDING, "an unreviewed hold became a permanent member on live: %s" % (st,)
        assert st[2] in identity.HOLD_VIA, st
        [row] = await message_of("c2", new_stub)
        assert row.id in [m for m, _ in await holds_listed(client)]



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


async def test_x7_uidless_first_tail_row_identical_to_an_archived_withheld_row(client, fx):
    """O1 without message ids (a Hermes older than message_uid): the admin withholds row 4 ("Yes, go ahead."); row 8
    is another, readable message with the same text. JARVIS compacts in place: rows 0-7 archived, rows 8-11 the
    protected tail (rewound, then written again right after the summary). The tail copy of row 8 is the copy of row 8
    (its rewound twin), not of the archived row 4. The release notes promise, for a backup without message ids, that
    a later verbatim repeat is at most HELD (releasable); it must never be withheld for good."""
    yes = "Yes, go ahead. zqx7yes62"
    turns = [("user", "Morning. Let us go through the clinic list."), ("assistant", "Sure, where do we start?"),
             ("user", "The lab results first."), ("assistant", "Should I share the biopsy result with your sister?"),
             ("user", yes), ("assistant", "Shared with her."), ("user", "Can you book the follow-up visit?"),
             ("assistant", "Shall I book Tuesday at nine?"), ("user", yes), ("assistant", "Booked for Tuesday."),
             ("user", "Thanks."), ("assistant", "You are welcome.")]
    bj.add_session(fx["db"], "x7", started=T)
    ids = [bj.add_row(fx["db"], "x7", role, text_, T + 10 * (n + 1), uid=False) for n, (role, text_) in
           enumerate(turns)]
    await run(fx)
    await withhold(client, (await message_of("x7", ids[4]))[0].id)
    assert (await state("x7", ids[8]))[:2] == (False, yes)  # the admin saw it and left it readable
    bj.hermes_compact(fx["db"], "x7", head_n=3, tail_n=4, ts=T + DAY)
    await run(fx)
    await run(fx)
    [copy8] = [r for r in bj.rows(fx["db"], "x7") if r["content"] == yes and r["active"] == 1]
    st = await state("x7", copy8["id"])
    show("tail copy of row 8", st)
    show("readable rows with the text", [(m.id, m.state) for m in await readable_with(yes)])
    show("holds", await holds_listed(client))
    key = "jarvis:message:x7:%d" % copy8["id"]
    stone = (await stones()).get(key)
    show("tail copy's key: records, stone", [await imp_records(key), stone and (stone.via, stone.review)])
    held_ok = stone is not None and stone.review == identity.PENDING
    assert await readable_with(yes) or held_ok, (
        "row 8's text (another message the admin left readable) is readable nowhere, and not even held")
    # Review 2 (S21): the admin's choice in the withhold dialog (row 8 left readable) is kept, and follows row 8 to its
    # tail copy as a release does: readable, not even held (it was a listed 'same text' hold before that fix).
    assert st[:2] == (False, yes) and not held_ok, (st, stone and (stone.via, stone.review))
    [copy_row] = await message_of("x7", copy8["id"])
    assert copy_row.id not in [m for m, _ in await holds_listed(client)]



# ======================= A release is bound to a row id whose text Hermes rewrites in place =======================


@pytest.mark.parametrize("flow", ["live", "restore"])
async def test_x8_release_of_a_pruned_stub_applied_to_an_older_live_backup(client, tmp_path, flow):
    """Hermes prunes a tool result in place: the row keeps its id and uid, its content becomes a stub, and the full
    output moves to a new archived twin. Test copy (the newer backup): the admin withholds the full output (the twin);
    the stub is held; the admin releases the stub (row 3: "this is not the secret"). Live imports the OLDER backup (as
    in the revision's own H10 test), where row 3 still holds the full output. decisions-apply must not make that full
    output readable through the release of row 3."""
    mark = "zqx8prune62"
    fx = bj.mini(tmp_path / "fx")
    from .test_brain_rules_062r import _tool_backup
    ids = _tool_backup(fx, mark)
    older = bj.copy(fx, tmp_path / "live_older")  # row ids[2] holds the full output here
    twin = bj.prune_twin(fx["db"], "c2", ids[2], STUB)
    await run(fx)
    [full] = await message_of("c2", twin)
    await withhold(client, full.id)
    [stub] = await message_of("c2", ids[2])
    await as_(client, "admin")
    assert (await client.post("/api/brain/holds/%d/release" % stub.id)).status_code == 200
    await run(fx)
    assert (await state("c2", ids[2]))[:2] == (False, STUB)
    assert await readable_rows(mark) == []
    out = tmp_path / "decisions.json"
    await imp.decisions_export(str(out), removals_only=flow == "restore")
    show("releases exported", json.loads(out.read_text())["releases"])
    await _fresh_production()
    live = await run(older)  # restore: the database as an older dump left it (imported from the older backup)
    if flow == "live":
        applied = await imp.decisions_apply(str(out), live["run_id"], actor=ADMIN[0], apply=True)
    else:  # restore.ps1: decisions-apply --removals-only, no run, no actor
        applied = await imp.decisions_apply(str(out), None, actor=None, apply=True, removals_only=True)
    show("apply", {k: applied[k] for k in ("removals", "removals_not_applied", "releases")})
    after_apply = await who_reads(client, mark)
    show("live after apply", {"readable": after_apply["readable_rows"], "api": after_apply["api"][:4]})
    await run(older)
    after = await who_reads(client, mark)
    show("live after re-import", {"readable": after["readable_rows"], "api": after["api"][:4]})
    assert after_apply["readable_rows"] == [] and after["readable_rows"] == [], (
        "the withheld tool output is readable on live through the release of the pruned row")
    assert after_apply["api"] == [] and after["api"] == []
    row3 = await state("c2", ids[2])
    assert row3[0] and (row3[2], row3[3]) == (identity.UID_UNCOMPARED, identity.PENDING), row3




async def test_x8b_release_of_a_pruned_stub_then_jarvis_restored_from_an_older_backup(client, tmp_path):
    """x8 without a decisions file: the same installation imports the older JARVIS backup again (JARVIS itself was
    restored from a backup taken before the prune), where row 3 still holds the full output: the release of row 3
    (the stub) must not bring that output back."""
    mark = "zqx8bprune62"
    fx = bj.mini(tmp_path / "fx")
    from .test_brain_rules_062r import _tool_backup
    ids = _tool_backup(fx, mark)
    older = bj.copy(fx, tmp_path / "jarvis_older")
    twin = bj.prune_twin(fx["db"], "c2", ids[2], STUB)
    await run(fx)
    await withhold(client, (await message_of("c2", twin))[0].id)
    [stub] = await message_of("c2", ids[2])
    await as_(client, "admin")
    assert (await client.post("/api/brain/holds/%d/release" % stub.id)).status_code == 200
    await run(fx)
    assert (await state("c2", ids[2]))[:2] == (False, STUB)
    await run(older)
    after = await who_reads(client, mark)
    show("after importing the older JARVIS backup", {"readable": after["readable_rows"], "api": after["api"][:4]})
    assert after["readable_rows"] == [] and after["api"] == [], "the withheld output is readable through the release"
    row3 = await state("c2", ids[2])
    assert row3[0] and (row3[2], row3[3]) == (identity.UID_UNCOMPARED, identity.PENDING), row3
    await run(fx)  # the newer backup again: the stub, the content released, is readable again
    assert (await state("c2", ids[2]))[:2] == (False, STUB)
    assert (await who_reads(client, mark))["readable_rows"] == []


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


def compaction_restating_a_carrier(db: str, sid: str, ts: float, carrier_id: int) -> int:
    """The next in-place compaction, the request still unanswered, the head now ending on a user row so the summary
    is an assistant row (_reappend_inflight_user_task, append branch): the in-flight turn is the earlier carrier
    (_has_merged_inflight_replay), so Hermes restates it as a FRESH plain user row {"role": "user", "content":
    header + request} (stamp_message_uid mints it a new uid; nothing absorbed). Returns the restatement's id."""
    def go(con: sqlite3.Connection) -> int:
        content = con.execute("SELECT content FROM messages WHERE id = ?", (carrier_id,)).fetchone()[0]
        request = content.rsplit(identity.REPLAY_HEADER, 1)[-1].strip()
        con.execute("UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ? AND active = 1", (sid,))
        con.execute("INSERT INTO messages (session_id, role, content, message_uid, timestamp) VALUES (?,?,?,?,?)",
                    (sid, "assistant", "[CONTEXT COMPACTION] Summary two.\n%s" % END_MARKER, bj.new_uid(), ts))
        return con.execute("INSERT INTO messages (session_id, role, content, message_uid, timestamp) "
                           "VALUES (?,?,?,?,?)", (sid, "user", "%s\n%s" % (identity.REPLAY_HEADER, request),
                                                  bj.new_uid(), ts + 1)).lastrowid
    return bj._write_db(db, go)


@pytest.mark.parametrize("when", ["withhold_between", "withhold_after_both"])
async def test_x10_request_restated_by_a_second_compaction(client, fx, when):
    """The user's request M is unanswered across two in-place compactions. The first merges it onto the handoff
    carrier (absorbing M's uid: withheld as a fold, R1). The second restates the carrier's request as a fresh user
    row: the fixed replay header, then M's text verbatim, with a new uid and nothing absorbed. The withheld request
    must not be readable in that restatement."""
    mark = "zqx10rest%s" % when[8:11]
    bj.add_session(fx["db"], "x10", started=T)
    bj.add_row(fx["db"], "x10", "assistant", "Hello, what can I do for you?", T + 1)
    m = bj.add_row(fx["db"], "x10", "user", secret(mark), T + 2)
    carrier = compaction_merging_inflight(fx["db"], "x10", T + 100, m)
    if when == "withhold_between":
        await run(fx)
        await withhold(client, (await message_of("x10", m))[0].id)
        restated_id = compaction_restating_a_carrier(fx["db"], "x10", T + 200, carrier)
        await run(fx)
    else:
        restated_id = compaction_restating_a_carrier(fx["db"], "x10", T + 200, carrier)
        await run(fx)
        await withhold(client, (await message_of("x10", m))[0].id)
        await run(fx)
    show("carrier", await state("x10", carrier))
    show("restatement", await state("x10", restated_id))
    after = await who_reads(client, mark)
    show("who reads", {"readable": after["readable_rows"], "api": after["api"][:4]})
    assert after["readable_rows"] == [] and after["api"] == [], "the restated withheld request is readable"
    assert (await state("x10", carrier))[2:] == (identity.FOLD, identity.CERTAIN)
    assert (await state("x10", restated_id))[2:] == (identity.RESTATED, identity.PENDING)



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


@pytest.mark.parametrize("how", ["cli_branch", "rotate", "gateway_branch"])
async def test_x11_short_identical_text_written_independently_in_a_child(client, fx, how):
    """The admin withholds a short "ok." in the parent; after a branch or rotation the user writes "ok." again in the
    child (a new turn, its own uid). It is another message: readable. The child's copy of the withheld one: withheld."""
    ok = "ok. zqx11%s" % how[:3]
    ids = conversation(fx["db"], "x11", T, [("user", "Shall I send it?"), ("assistant", ok), ("user", "Thanks.")])
    await run(fx)
    await withhold(client, (await message_of("x11", ids[1]))[0].id)
    {"cli_branch": bj.cli_branch, "rotate": bj.rotate, "gateway_branch": bj.gateway_branch}[how](
        fx["db"], "x11", "x11_child", T + DAY)
    later = bj.add_row(fx["db"], "x11_child", "assistant", ok, T + DAY + 20)
    await run(fx)
    await run(fx)
    copies = [r for r in bj.rows(fx["db"], "x11_child") if r["content"] == ok and r["id"] != later]
    show("child copy of the withheld one", [await state("x11_child", c["id"]) for c in copies])
    show("new turn in the child", await state("x11_child", later))
    assert copies and all(st[0] for st in [await state("x11_child", c["id"]) for c in copies])
    assert (await state("x11_child", later))[:2] == (False, ok)


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
    assert len(await readable_with(STUB)) >= 2 and await holds_listed(client) == []
    assert await bj.nowhere(mark) == []

