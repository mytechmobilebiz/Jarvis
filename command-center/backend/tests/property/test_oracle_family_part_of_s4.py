"""D-46 (rebuild S4, RECON-63): A3 / C3-evidence admits 'part_of' (and a short withheld text's word sequence as
'contains', 13880) on a hold of a row of the decision's family (i22's
family: its stones bind the conversation) outside the ground-truth group, held by the family's own rules (Settle, a
later backup), where a value of the row is part of the withheld text: the product computes those holds with i22's
family rule (seeds 13062, 13147, 13226, 13266: a session_search read whose title value is the withheld request cut
short). Admitted only, never demanded. Still reported: the same 'part_of' on a per-row carrier hold, on a row outside
the decision's family, a 'part_of' no value gives, and a hold that hides a demanded relation. No database, synthetic
text only."""

from __future__ import annotations

import json

from . import oracle as o
from . import selftest_kit as k

W = "Please summarise the backup schedule before the weekly call with the vendor."
PIECE = "the backup schedule before the weekly call"  # a middle piece of W, not generic


def _evidence(tmp, value: str, shown: list[str], *, via: str = "found when finishing decisions",
              in_family: bool = True) -> list[str]:
    w = k.world(tmp)
    a, c = w.session(), w.session()
    r1, lm = w.row(a, "user", W)
    read = json.dumps({"success": True, "mode": "read", "session_meta": {"title": value},
                       "messages": [{"role": "user", "content": "Something else entirely, nothing to see."}]})
    x, _ = w.row(c, "tool", read)
    snap = w.sim.backup("d")
    rows = k.cc_rows(snap, withheld={(a, r1), (c, x)})
    run = k.run_of(w, snap)
    wh = k.withhold(run, rows, a, r1)
    rx = next(r for r in rows if r["source_message_id"] == str(x))
    anchor = wh.key if in_family else "jarvis:message:elsewhere:1"
    st = k.state(rows, [k.stone(rx, via=via, anchor=anchor, relations=shown)])
    assert (c in o.decision_family(run, wh, snap, st)) == in_family
    assert c not in o.truth_group(run, snap, st)
    o.check_evidence(run, "A3", [k.hold(rx, shown)], st, snap, "self-test")
    return run.problems


def test_s4_family_row_value_part_of_is_admitted(tmp_path):
    assert not _evidence(tmp_path, PIECE, ["part_of"])


def test_s4_family_row_part_of_on_a_carrier_hold_is_still_a_violation(tmp_path):
    assert _evidence(tmp_path, PIECE, ["part_of"], via=o.CARRIER_VIA)


def test_s4_part_of_outside_the_family_is_still_a_violation(tmp_path):
    assert _evidence(tmp_path, PIECE, ["part_of"], in_family=False)


def test_s4_part_of_no_value_gives_is_still_a_violation(tmp_path):
    assert _evidence(tmp_path, "Fetched page for the vendor review: headings.", ["part_of"])


def test_s4_hiding_a_demanded_relation_is_still_a_violation(tmp_path):
    assert _evidence(tmp_path, W, ["part_of"])


def test_s4_family_part_of_is_never_demanded():
    held = [json.dumps({"title": PIECE})]
    dem, _ = o.relations_c(held, W, group=False, carrier=True)
    assert "part_of" not in dem


SHORT = "Sounds good."


def _short(tmp, shown: list[str], *, in_family: bool = True) -> list[str]:
    """13880: W is a short reply restated after Hermes's replay header; a family row elsewhere says it again."""
    w = k.world(tmp)
    a, c = w.session(), w.session()
    r1, _ = w.row(a, "user", SHORT)
    x, _ = w.row(c, "assistant", SHORT + " I will send it on Monday.")
    snap = w.sim.backup("d")
    rows = k.cc_rows(snap, withheld={(a, r1), (c, x)})
    run = k.run_of(w, snap)
    wh = k.withhold(run, rows, a, r1)
    rx = next(r for r in rows if r["source_message_id"] == str(x))
    anchor = wh.key if in_family else "jarvis:message:elsewhere:1"
    st = k.state(rows, [k.stone(rx, via="found when finishing decisions", anchor=anchor, relations=shown)])
    o.check_evidence(run, "A3", [k.hold(rx, shown)], st, snap, "self-test")
    return run.problems


def test_s4_family_row_short_w_words_contains_is_admitted(tmp_path):
    assert not _short(tmp_path, ["contains"])


def test_s4_short_w_words_contains_outside_the_family_is_still_a_violation(tmp_path):
    assert _short(tmp_path, ["contains"], in_family=False)
