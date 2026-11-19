"""The option C oracle's readings (rebuild R5): O-CI fold, O-D4 generic-v1, the generic prune line, short texts by
words. Self-tests: the oracle still demands what the contract demands (each guard has a mutant in
docs/rebuild-062/progress/R5-mutation/r5mut.py that these tests kill). No database, synthetic text only."""

from __future__ import annotations

import json

from . import oracle as o

W = "Please rewrite the landing page copy for the team meeting."


def test_oci_the_oracle_demands_a_carrier_in_another_case_or_form():
    """cr2:oracle_fold_off: a carrier in another case, in fullwidth letters, with a soft hyphen or zero-width space,
    or with Turkish dotted capitals is demanded."""
    for held in (W.upper(), "ＰＬＥＡＳＥ rewrite the landing page copy for the team meeting.",
                 "Please re­write the landing​ page copy for the team meeting.",
                 "PLEASE REWRİTE THE LANDİNG PAGE COPY FOR THE TEAM MEETİNG."):
        dem, _ = o.relations_c([held], W, group=False, carrier=True)
        assert dem & {"identical", "contains"}, held


def test_oci_control_other_words_in_another_case_are_no_carrier():
    dem, alw = o.relations_c(["PLEASE ARCHIVE THE PARTNER AGREEMENT BEFORE LUNCH."], W, group=False, carrier=True)
    assert not dem and not alw


def test_od4_the_oracle_neither_demands_nor_allows_a_generic_piece_and_demands_anything_more():
    """cr2:oracle_generic_off: a generic withheld text ('Thanks, that is all.' is not on the list; 'thank you very
    much' is) is no carrier outside the groups; a generic cut beginning is neither demanded nor allowed; anything
    longer is demanded."""
    g = "Thank you very much!"
    dem, alw = o.relations_c(["Earlier you said: thank you very much! Then nothing."], g, group=False, carrier=True)
    assert not dem & {"identical", "contains"} and not alw & {"identical", "contains"}
    assert o.generic("hello there") and o.generic("https://www.example.com/") and o.generic("short piece")
    assert not o.generic("Please rewrite the landing")  # 26 characters, not on the list
    dem, _ = o.relations_c(["[web_fetch] prompt=Please rewrite the landing (300 chars result)"], W, group=False,
                           carrier=True)
    assert "cut" in dem


def test_od4_the_oracle_lists_generic_v1_as_the_product_does():
    """O-D4's list is the contract's: the oracle's copy equals the product's (a change to either is caught)."""
    from app.brain import identity as I
    assert o.GENERIC_V1 == I._GENERIC_PHRASES
    assert o.MIN_REL == I.MIN_RELATED


def test_p1_the_oracle_demands_ws_first_40_characters_in_the_generic_prune_line():
    """cr2:oracle_prune_off: Hermes's generic prune line keeps str(v)[:40] of the first two arguments: W's first 40
    characters there are a demanded cut, also inside a session_search read of that conversation (JSON)."""
    line = "[session_search] query=%s session_id=20261101_000007_abc (812 chars result)" % W[:40]
    dem, _ = o.relations_c([line], W, group=False, carrier=True)
    assert "cut" in dem
    read = json.dumps({"success": True, "messages": [{"role": "tool", "content": line}]}, ensure_ascii=False)
    dem, _ = o.relations_c([read], W, group=False, carrier=True)
    assert "cut" in dem


def test_p1_control_a_prune_line_of_other_words_is_no_carrier():
    line = "[session_search] query=Please archive the partner agreement bef session_id=x (812 chars result)"
    assert not o.relations_c([line], W, group=False, carrier=True)[0]


def test_fwords_a_short_withheld_text_is_identical_as_a_quoted_block_without_its_markers():
    """13546's shape (round 2, fixed in round 3 with fqw/fwords): a short W quoted with '> ' markers is a whole value
    without them: identical, demanded."""
    short = "Yes, send it."
    dem, _ = o.relations_c(["In that conversation you wrote:\n> Yes, send it."], short, group=False, carrier=True)
    assert "identical" in dem


def test_decode_cap_is_the_products_and_fails_closed_the_same_way():
    """The decode cap (owner rule: at the cap fail closed, the same in the oracle): the oracle's constants are the
    product's, JSON nested past the parser is capped on both sides, and an ordinary nested read is capped on
    neither."""
    from app.brain import identity as I
    assert (o.CAP_STEPS, o.CAP_DEPTH, o.CAP_BUDGET, o.CAP_TEXT, o.CAP_STRINGS) == (
        I.CANON_STEPS, I.CANON_DEPTH, I.CANON_BUDGET, I.CANON_MAX_TEXT, I.CANON_MAX_STRINGS)
    deep = '{"a": ' * 150_000 + '"x"' + "}" * 150_000
    assert o.readings(deep)[1] and I.canon(deep).capped
    assert "not_checked" in o.relations_c([deep], W, group=False, carrier=True)[0]
    nested = json.dumps({"a": {"b": [{"c": W}] * 3}})
    assert not o.readings(nested)[1] and not I.canon(nested).capped
    many = json.dumps([json.dumps({"m": json.dumps({"x": W})}) for _ in range(40)])
    assert o.readings(many)[1] == I.canon(many).capped
    long = "x" * (o.CAP_TEXT + 1)
    assert o.readings(long)[1] and I.canon(long).capped


def test_ignorables_are_the_products():
    from app.brain import identity as I
    assert set(o._IGN) == set(I.IGNORABLE) and len(o._IGN) == 4174
