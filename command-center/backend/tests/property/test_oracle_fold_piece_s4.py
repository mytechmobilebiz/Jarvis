"""D-47 (rebuild S4, RECON-64): A3 / C3-evidence when the withheld row is a fold and this backup's copy of it lost a
message it had folded in (seed 13536): the text in hand is a piece of what the admin withheld, so a hold may say
'not in this backup' for it. Still reported: 'none' on the folded message back as a row of its own (part of the text
as withheld, O5), and 'not in this backup' when the backup holds the withheld text whole. No database, synthetic
text only."""

from __future__ import annotations

import dataclasses

from . import oracle as o
from . import selftest_kit as k

FIRST = "Please look again at the hosting migration before lunch."
SECOND = "Please look again at the landing page copy with the usual care."


def _evidence(tmp, shown: list[str], *, folded: bool = True) -> list[str]:
    w = k.world(tmp)
    a = w.session()
    r1, _ = w.row(a, "user", FIRST)
    x, _ = w.row(a, "user", SECOND)
    snap = w.sim.backup("d")
    rows = k.cc_rows(snap, withheld={(a, r1), (a, x)})
    run = k.run_of(w, snap)
    wh = k.withhold(run, rows, a, r1)
    if folded:  # withheld as the fold of both requests (it had absorbed the second one's uid)
        run.withholds[-1] = wh = dataclasses.replace(wh, text=FIRST + "\n\n" + SECOND, absorbed=("ab-second",))
    rx = next(r for r in rows if r["source_message_id"] == str(x))
    st = k.state(rows, [k.stone(rx, via="new in a later backup", anchor=wh.key, relations=shown)])
    assert o.w_text_in(run, wh, snap, st) == FIRST
    o.check_evidence(run, "A3", [k.hold(rx, shown)], st, snap, "self-test")
    return run.problems


def test_s4_not_in_backup_for_a_piece_of_a_withheld_fold_is_admitted(tmp_path):
    assert not _evidence(tmp_path, ["not_in_backup"])


def test_s4_none_on_the_folded_message_is_still_a_violation(tmp_path):
    assert _evidence(tmp_path, ["none"])


def test_s4_not_in_backup_for_a_whole_withheld_text_is_still_a_violation(tmp_path):
    assert _evidence(tmp_path, ["not_in_backup"], folded=False)
