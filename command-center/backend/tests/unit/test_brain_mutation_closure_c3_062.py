"""0.6.2 option C (rebuild R3): closing tests of the round-3 consolidation (c3:recursion_open, c3:spans_per_line_off)
and their controls, product side (the oracle's own cap is R5). Synthetic text only."""

from __future__ import annotations

import json

from app.brain import identity as I

W = "Bitte überweise die Kaution von 2.400 EUR erst nach der Schlüsselübergabe am Freitag."


def test_c3_json_nested_past_the_parser_is_capped_and_fails_closed():
    """c3:recursion_open: JSON nested past the parser's recursion (RecursionError) is a cap: not_checked, held."""
    text = '{"a": ' * 150_000 + '"x"' + "}" * 150_000
    c = I.canon(text)
    assert c.capped
    assert I.NOT_CHECKED in I.carrier_relations([text], W)
    assert I.NOT_CHECKED in I.family_relations(text, W)


def test_c3_control_ordinary_nested_json_is_not_capped():
    text = json.dumps({"a": {"b": [{"c": "plain text, nothing private"}] * 3}})
    assert not I.canon(text).capped
    assert not I.carrier_relations([text], W)


def test_c3_a_piece_of_w_quoted_on_one_line_of_several_is_read_on_its_line():
    """c3:spans_per_line_off: a summary line inside a longer text (a compaction's list of earlier tool calls) quotes
    W's beginning cut at its width: read on its line."""
    text = "\n".join([
        "Earlier tool calls:",
        "[terminal] ran `ls -la` -> exit 0, 12 lines output",
        "[vision_analyze] '%s' (4,096 chars)" % W[:50],
        "[read_file] read notes.md from line 1 (900 chars)",
    ])
    assert I.CUT in I.carrier_relations([text], W)


def test_c3_control_a_summary_line_of_other_words_among_several_holds_nothing():
    text = "\n".join(["Earlier tool calls:", "[vision_analyze] 'what does the sign on the left say?' (4,096 chars)",
                      "[terminal] ran `ls` -> exit 0, 2 lines output"])
    assert not I.carrier_relations([text], W)
