"""quote_spans / span_relations (round 3 consolidation oracle change 1, seeds 13154 and 13774): a relation a quote
span of a Hermes f-string summary gives is admitted on the side of what a hold MAY show, never demanded; a relation
nothing gives is still a violation. A mutant in docs/rebuild-062/progress/R5-mutation/r5mut.py that admits any
relation is killed here. No database, synthetic text only."""

from __future__ import annotations

from . import oracle as o
from . import selftest_kit as k

W = "Please rewrite the landing page copy for the team meeting."


def test_spans_read_hermes_summaries_across_apostrophes_and_line_breaks():
    spans = [r.text for r in o.quote_spans("[delegate_task] 'it's done\nnext line' (12 chars result)")]
    assert "it's done\nnext line" in spans and "next line" in spans
    cut = o.quote_spans("[vision_analyze] '%s' (4,096 chars)" % W[:50])
    assert cut and cut[0].cut_end


def test_spans_are_admitted_never_demanded():
    held = ["[delegate_task] '%s' (812 chars result)" % W]
    dem, alw = o.relations_c(held, W, group=False, carrier=True)
    assert "identical" in alw and "identical" not in dem and "contains" in dem
    assert o.span_relations(held, W, group=False) >= {"identical"}


def _evidence(tmp, text: str, shown: list[str]):
    w = k.world(tmp)
    a, c = w.session(), w.session()
    r1, lm = w.row(a, "user", W)
    x, _ = w.row(c, "tool", text, carries={lm})
    snap = w.sim.backup("d")
    rows = k.cc_rows(snap, withheld={(a, r1), (c, x)})
    run = k.run_of(w, snap)
    wh = k.withhold(run, rows, a, r1)
    rx = next(r for r in rows if r["source_message_id"] == str(x))
    st = k.state(rows, [k.stone(rx, via=o.CARRIER_VIA, anchor=wh.key, relations=shown)])
    o.check_evidence(run, "A3", [k.hold(rx, shown)], st, snap, "self-test")
    return run.problems


def test_a_span_relation_shown_is_honest(tmp_path):
    assert not _evidence(tmp_path, "[delegate_task] '%s' (812 chars result)" % W, ["identical", "contains"])


def test_a_relation_no_reading_gives_is_still_a_violation(tmp_path):
    assert _evidence(tmp_path / "a", "[delegate_task] '%s' (812 chars result)" % W, ["part_of"])
    assert _evidence(tmp_path / "b", "Fetched page for the vendor review: headings.", ["contains"])


def test_a_demanded_relation_not_shown_is_still_a_violation(tmp_path):
    assert _evidence(tmp_path, "Earlier: %s Then nothing." % W, ["none"])


def test_cut_per_line_is_admitted_never_demanded():
    text = "[truncated preview]\n%s\nmore" % W[:30]
    dem, alw = o.relations_c([text], W, group=False, carrier=True)
    assert "cut" in alw and "cut" not in dem
