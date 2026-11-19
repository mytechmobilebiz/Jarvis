"""D-49 (rebuild S4, 13658): A3 / C3-evidence for a row the Command Center stores cut (spec: tool results cut to
4,000 characters; here seed 13658's session_search read, cut inside a nested value): a hold computed on the stored text shows that text's relations (the withheld text cut short at
the cut), and they are admitted and meet the demand. Still reported: 'none' on such a row while the full text holds
the withheld text, a relation neither text gives, and 'cut' on a row that is not cut (the full text is the stored
one). No database, synthetic text only."""

from __future__ import annotations

import json
import os

from . import hermes_sim as hs
from . import oracle as o
from . import selftest_kit as k

_FIX = json.load(open(os.path.join(os.path.dirname(__file__), "s4_13658_read.json")))
W = _FIX["W"]
READ = _FIX["read"]  # the simulator's session_search read of seed 13658: W 3,863 characters in, 5,655 in all


def _evidence(tmp, shown: list[str], *, text: str = READ) -> list[str]:
    w = k.world(tmp)
    a, c = w.session(), w.session()
    r1, lm = w.row(a, "tool", W, call="call_w")
    x, _ = w.row(c, "tool", text, carries={lm}, call="call_x")
    snap = w.sim.backup("d")
    rows = k.cc_rows(snap, withheld={(a, r1), (c, x)})
    run = k.run_of(w, snap)
    wh = k.withhold(run, rows, a, r1)
    rx = next(r for r in rows if r["source_message_id"] == str(x))
    st = k.state(rows, [k.stone(rx, via=o.CARRIER_VIA, anchor=wh.key, relations=shown)])
    o.check_evidence(run, "A3", [k.hold(rx, shown)], st, snap, "self-test")
    return run.problems


def test_s4_the_fixture_is_cut_where_the_withheld_text_is():
    stored = o.stored_form(hs.Version("s", 1, "tool", 1.0, None, None, READ, hs.Tag(1, frozenset({1})), 1, 0, ()))
    assert stored is not None and W not in stored.content and W[:40] in stored.content
    assert o.relations_c([READ], W, group=False, carrier=True)[0] == {"contains"}
    assert "cut" in o.relations_c([stored.content], W, group=False, carrier=True)[0]
    assert o.stored_form(hs.Version("s", 1, "tool", 1.0, None, None, READ[:3_990], hs.Tag(1, frozenset({1})), 1, 0,
                                    ())) is None  # the spec's 4,000: nothing shorter is cut


def test_s4_the_stored_texts_cut_is_admitted_for_a_tool_row_cut_at_4000(tmp_path):
    assert not _evidence(tmp_path, ["cut"])


def test_s4_the_full_texts_contains_is_still_honest(tmp_path):
    assert not _evidence(tmp_path, ["contains"])


def test_s4_none_on_a_cut_row_is_still_a_violation(tmp_path):
    assert _evidence(tmp_path, ["none"])


def test_s4_a_relation_neither_text_gives_is_still_a_violation(tmp_path):
    assert _evidence(tmp_path, ["identical"])


def test_s4_cut_on_a_row_that_is_not_cut_is_still_a_violation(tmp_path):
    short = READ[:3_000] + W + '"}]}'
    assert len(short) < 4_000
    assert _evidence(tmp_path, ["cut"], text=short)
