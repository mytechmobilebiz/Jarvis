"""The option C oracle's release properties (rebuild R5): N-A ('none' against every withheld text in hand), N-B (a
released row carrying another withheld text is a leak), 'later_withhold' undecided, one click (O6), released_ok (O7).
Self-tests on crafted states (no database): real violations are still reported; each guard has a mutant in
docs/rebuild-062/progress/R5-mutation/r5mut.py that these tests kill. Synthetic text only."""

from __future__ import annotations

from . import oracle as o
from . import selftest_kit as k

W1 = "Please rewrite the landing page copy for the team meeting."
W2 = "Please double check the partner agreement before lunch."


def _two(tmp):
    """Conversation A withholds W1; conversation B withholds W2; row X of A repeats W2's words (a copy of W2)."""
    w = k.world(tmp)
    a, b = w.session(), w.session()
    r1, _ = w.row(a, "user", W1)
    r2, lm2 = w.row(b, "user", W2)
    x, _ = w.row(a, "assistant", "As you asked elsewhere: " + W2, carries={lm2})
    y, _ = w.row(a, "assistant", "Done, the vendor review is ready.")
    snap = w.sim.backup("d")
    return w, a, b, r1, r2, x, y, snap


def test_na_none_must_hold_against_every_withheld_text_in_hand(tmp_path):
    """rel3:oracle_na_own_only: a hold of A says 'none' under W1 while it carries W2 (withheld too, in hand): N-A
    violation."""
    w, a, b, r1, r2, x, y, snap = _two(tmp_path)
    rows = k.cc_rows(snap, withheld={(a, r1), (b, r2), (a, x)})
    run = k.run_of(w, snap)
    w1 = k.withhold(run, rows, a, r1)
    k.withhold(run, rows, b, r2)
    rx = next(r for r in rows if r["source_message_id"] == str(x))
    st = k.state(rows, [k.stone(rx, via=o.GROUP_VIAS_C[0], anchor=w1.key, relations=["none"])])
    o.check_evidence(run, "A3", [k.hold(rx, ["none"])], st, snap, "self-test")
    assert any("'none' but" in p for p in run.problems), run.problems


def test_na_control_none_of_an_unrelated_row_with_every_text_in_hand_is_honest(tmp_path):
    w, a, b, r1, r2, x, y, snap = _two(tmp_path)
    rows = k.cc_rows(snap, withheld={(a, r1), (b, r2), (a, y)})
    run = k.run_of(w, snap)
    w1 = k.withhold(run, rows, a, r1)
    k.withhold(run, rows, b, r2)
    ry = next(r for r in rows if r["source_message_id"] == str(y))
    st = k.state(rows, [k.stone(ry, via=o.GROUP_VIAS_C[0], anchor=w1.key, relations=["none"])])
    o.check_evidence(run, "A3", [k.hold(ry, ["none"])], st, snap, "self-test")
    assert not run.problems, run.problems


def test_nb_a_released_row_of_a_withheld_conversation_carrying_another_withheld_text_is_a_leak(tmp_path):
    """rel3:oracle_nb_off: X (A's group) was released for W1 and is readable with its released text; it carries W2,
    withheld later: O8."""
    w, a, b, r1, r2, x, y, snap = _two(tmp_path)
    rows = k.cc_rows(snap, withheld={(a, r1), (b, r2)})
    run = k.run_of(w, snap)
    k.withhold(run, rows, a, r1)
    k.withhold(run, rows, b, r2)
    rx = next(r for r in rows if r["source_message_id"] == str(x))
    ry = next(r for r in rows if r["source_message_id"] == str(y))
    for r in (rx, ry):
        run.released[r["source_key"]] = [r["content"]]
    o.check_c(run, "self-test", k.state(rows), k.groups({a, b}), snap)
    assert [p for p in run.problems if p.startswith("O8") and "row %d " % rx["id"] in p], run.problems
    assert not [p for p in run.problems if "row %d " % ry["id"] in p], run.problems


def test_o2_a_readable_group_row_never_released_or_released_with_other_text_is_reported(tmp_path):
    """O2/O7: a readable row of a group conversation is a violation unless that exact row was released and holds the
    released text (the Command Center's own marks aside)."""
    w, a, b, r1, r2, x, y, snap = _two(tmp_path)
    rows = k.cc_rows(snap, withheld={(a, r1), (b, r2), (a, x)})
    run = k.run_of(w, snap)
    k.withhold(run, rows, a, r1)
    k.withhold(run, rows, b, r2)
    ry = next(r for r in rows if r["source_message_id"] == str(y))
    o.check_c(run, "self-test", k.state(rows), k.groups({a, b}), snap)
    assert [p for p in run.problems if p.startswith("O2") and "row %d " % ry["id"] in p]
    run.problems.clear()
    run.released[ry["source_key"]] = ["Done, the partner agreement is ready."]
    o.check_c(run, "self-test", k.state(rows), k.groups({a, b}), snap)
    assert [p for p in run.problems if p.startswith("O2") and "row %d " % ry["id"] in p]
    run.problems.clear()
    run.released[ry["source_key"]] = [ry["content"]]
    o.check_c(run, "self-test", k.state(rows), k.groups({a, b}), snap)
    assert not run.problems, run.problems


def test_later_withhold_is_an_undecided_state_for_the_oracle(tmp_path):
    """rel3:oracle_later_unread: 'later_withhold' says the hold was not compared with a later withheld text yet: an
    honest undecided state even when the row relates (it is never 'none' and never one click); with 'none' beside it,
    or offered for one click, it is a violation."""
    w, a, b, r1, r2, x, y, snap = _two(tmp_path)
    rows = k.cc_rows(snap, withheld={(a, r1), (b, r2), (a, x)})
    run = k.run_of(w, snap)
    k.withhold(run, rows, a, r1)
    w2 = k.withhold(run, rows, b, r2)
    rx = next(r for r in rows if r["source_message_id"] == str(x))
    st = k.state(rows, [k.stone(rx, via=o.GROUP_VIAS_C[0], anchor=w2.key, relations=["later_withhold"])])
    o.check_evidence(run, "A3", [k.hold(rx, ["later_withhold"])], st, snap, "self-test")
    assert not run.problems, run.problems
    o.check_evidence(run, "A3", [k.hold(rx, ["later_withhold", "none"])], st, snap, "self-test")
    assert run.problems
    run.problems.clear()
    o.check_one_click(run, "self-test", [k.hold(rx, ["later_withhold"], one_click=True)], st, snap, k.groups({a}))
    assert any(p.startswith("O6") for p in run.problems)


def test_o6_one_click_only_for_exactly_none_user_or_assistant_plain_and_certain(tmp_path):
    w, a, b, r1, r2, x, y, snap = _two(tmp_path)
    rows = k.cc_rows(snap, withheld={(a, r1), (a, y)})
    run = k.run_of(w, snap)
    k.withhold(run, rows, a, r1)
    ry = next(r for r in rows if r["source_message_id"] == str(y))
    st = k.state(rows)
    o.check_one_click(run, "t", [k.hold(ry, ["none"], one_click=True)], st, snap, k.groups({a}))
    assert not run.problems
    for bad in (dict(relations=["none", "same_uid"]), dict(role="tool"), dict(form="summary"), dict(form=""),
                dict(uncertain=True)):
        h = k.hold(ry, ["none"], one_click=True)
        h.update(bad)
        o.check_one_click(run, "t", [h], st, snap, k.groups({a}))
        assert run.problems, bad
        run.problems.clear()
