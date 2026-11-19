"""The option C oracle's excuses for A4/B4 (rebuild R5): kept_carrier (round 3, step 2: 13546's B4 shape) and a carrier
outside every group held on its own (RECON-27). Each excuse is narrow: a pending hold, another reason, no hold, or a row
that never carried the withheld text is still a difference. Mutants in docs/rebuild-062/progress/R5-mutation/r5mut.py
loosen each one; these tests kill them. No database, synthetic text only."""

from __future__ import annotations

from . import oracle as o
from . import selftest_kit as k

W = "Please rewrite the landing page copy for the team meeting."


def _world(tmp):
    w = k.world(tmp)
    a, c = w.session(), w.session()
    r1, lm = w.row(a, "user", W)
    carrier, _ = w.row(c, "assistant", "From the other chat: " + W, carries={lm})
    plain, _ = w.row(c, "assistant", "Done, the vendor review is ready.")
    snap = w.sim.backup("d")
    rows = k.cc_rows(snap, withheld={(a, r1), (c, carrier), (c, plain)})
    run = k.run_of(w, snap)
    k.withhold(run, rows, a, r1)
    rc = next(r for r in rows if r["source_message_id"] == str(carrier))
    rp = next(r for r in rows if r["source_message_id"] == str(plain))
    return run, rows, rc, rp, a


def _outside(run, rows, stones, a):
    mine = k.state(rows, stones)
    plain = k.state([dict(r, withheld=False, content=r["content"] or "x") for r in rows])
    o.compare_outside(run, "B4", mine, plain, {a}, "self-test")
    return run.problems


def test_kept_carrier_excuses_only_a_kept_carrier_hold_whose_row_carried_w(tmp_path):
    run, rows, rc, rp, a = _world(tmp_path)
    assert not _outside(run, rows, [k.stone(rc, review="kept", via=o.CARRIER_VIA),
                                    k.stone(rp, review="kept", via=o.CARRIER_VIA)], a) or \
        all("(%s)" % rp["sid"] in p for p in run.problems)
    assert o.kept_carrier(rc, k.state(rows, [k.stone(rc, review="kept", via=o.CARRIER_VIA)]), run)


def test_kept_carrier_never_excuses_a_pending_hold_another_reason_no_hold_or_a_row_that_never_carried_w(tmp_path):
    run, rows, rc, rp, a = _world(tmp_path)
    assert not o.kept_carrier(rc, k.state(rows, [k.stone(rc, review="pending", via=o.CARRIER_VIA)]), run)
    assert not o.kept_carrier(rc, k.state(rows, [k.stone(rc, review="kept", via="new in a later backup")]), run)
    assert not o.kept_carrier(rc, k.state(rows), run)
    assert not o.kept_carrier(rp, k.state(rows, [k.stone(rp, review="kept", via=o.CARRIER_VIA)]), run)


def test_kept_carrier_a_row_that_never_carried_w_is_still_a_difference(tmp_path):
    run, rows, rc, rp, a = _world(tmp_path)
    problems = _outside(run, rows, [k.stone(rp, review="kept", via=o.CARRIER_VIA)], a)
    assert problems and "jarvis:message:%s:%s" % (rp["sid"], rp["source_message_id"]) in problems[0]


def test_carrier_hold_excuses_only_a_row_an_admitted_reading_relates(tmp_path):
    """A pending carrier hold outside every group is excused only when a reading relates its row to a withheld text;
    a carrier hold on an unrelated row is a difference (over-hold)."""
    run, rows, rc, rp, a = _world(tmp_path)
    assert o.is_carrier_hold(rc, k.state(rows, [k.stone(rc, via=o.CARRIER_VIA)]), run)
    assert not o.is_carrier_hold(rp, k.state(rows, [k.stone(rp, via=o.CARRIER_VIA)]), run)
    assert not o.is_carrier_hold(rc, k.state(rows, [k.stone(rc, via="new in a later backup")]), run)
    problems = _outside(run, rows, [k.stone(rc, via=o.CARRIER_VIA), k.stone(rp, via=o.CARRIER_VIA)], a)
    assert len(problems) == 1 and "jarvis:message:%s:%s" % (rp["sid"], rp["source_message_id"]) in problems[0]
    assert "jarvis:message:%s:%s " % (rc["sid"], rc["source_message_id"]) not in problems[0]


def test_o3_a_readable_carrier_outside_every_group_is_reported(tmp_path):
    """O3 (B9): the carrier row readable outside the groups while W was in hand is a violation; unheld unrelated rows
    are not."""
    run, rows, rc, rp, a = _world(tmp_path)
    rows = [dict(r, withheld=False, content=r["content"] or "x") if r["sid"] != a else r for r in rows]
    for r in rows:
        if r["id"] == rc["id"]:
            r["content"] = "From the other chat: " + W
        if r["id"] == rp["id"]:
            r["content"] = "Done, the vendor review is ready."
    run.at_withhold[run.withholds[0].key] = {r["source_key"] for r in rows}
    o.check_c(run, "self-test", k.state(rows), k.groups({a}), run.imported[0])
    assert [p for p in run.problems if p.startswith("O3") and "row %d " % rc["id"] in p], run.problems
    assert not [p for p in run.problems if "row %d " % rp["id"] in p]
