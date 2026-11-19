"""0.6.2 option C rebuild S5: unit-level closing tests for the R7 gaps. The five dec_json_off closing tests the round-2
report counts (7 in all; 2 were in the rebuilt tree: test_skc_a_json_value_only_json_loads_reads_back_is_a_carrier_and_
evidence and test_dec_json_a_short_w_as_a_whole_value_of_pretty_printed_ascii_json_is_identical; the other five names
are not in the sources), each failing on its own under m18:dec_json_off (the JSON parse of the canonical decode
switched off); and cr2:quick_no_quote_bypass caught on its own (the quick filter's bypass for a quote shape).
Mutants: progress/S5-mutation/s5mut.py. Synthetic text only."""

from __future__ import annotations

import json

from app.brain import identity as I

W = ("Der Notfallschlüssel für das Archiv liegt hinter der dritten Kachel links, Kombination Möwe-5512, "
     "das bleibt bitte unter uns.")
SHORT = "Ja, Möwe geht."


# --- m18:dec_json_off: only the JSON parse gives a whole value (a JSON text with no escape and no open literal) ----


def test_dec_json_a_short_w_as_a_whole_value_of_a_compact_json_list_is_identical():
    text = json.dumps([SHORT, "Nein, morgen."], ensure_ascii=False)
    assert "\\" not in text
    assert I.IDENTICAL_REL in I.carrier_relations([text], SHORT)
    assert I.IDENTICAL_REL in I.family_relations(text, SHORT)


def test_dec_json_a_short_w_as_a_tool_call_argument_value_is_identical():
    """A send_message call whose arguments (a JSON text inside the tool calls' JSON) hold the short W as a whole value:
    the unescape reads the arguments back, only their parse gives the whole value."""
    calls = json.dumps([{"id": "call_1", "type": "function", "function": {
        "name": "send_message", "arguments": json.dumps({"target": "telegram", "text": SHORT}, ensure_ascii=False)}}],
        ensure_ascii=False)
    assert I.IDENTICAL_REL in I.carrier_relations(["", calls], SHORT)
    assert I.IDENTICAL_REL in I.carrier_relations([calls], SHORT)


def test_dec_json_a_short_w_in_a_session_search_result_is_identical():
    text = json.dumps({"success": True, "results": [{"session_id": "s1", "role": "user", "content": SHORT}],
                       "count": 1}, ensure_ascii=False)
    assert I.IDENTICAL_REL in I.carrier_relations([text], SHORT)


def test_dec_json_a_json_value_that_is_ws_beginning_is_a_cut_and_a_group_row_holding_it_is_never_none():
    """A whole JSON value that is W's first 40 characters (a preview a tool returned): a cut; a group row holding it,
    every withheld text in hand, is never 'none' (one click)."""
    text = json.dumps({"preview": W[:40], "chars": len(W)}, ensure_ascii=False)
    assert I.CUT in I.carrier_relations([text], W)
    relations = I.group_relations([text], W, all_in_hand=True)
    assert I.NO_RELATION not in relations and I.CUT in relations


def test_dec_json_a_short_w_as_a_whole_value_is_identical_in_a_group_rows_evidence():
    """A group row whose only copy of the short W is a whole JSON value: identical (never only 'contains'), whether W
    is the group's own text or another group's in hand."""
    text = json.dumps({"reply": {"to": "anna", "text": SHORT}}, ensure_ascii=False)
    assert I.IDENTICAL_REL in I.group_relations([text], SHORT, all_in_hand=True)
    assert I.IDENTICAL_REL in I.group_relations([text], "Something else entirely here.", [SHORT], all_in_hand=True)


# --- cr2:quick_no_quote_bypass: a cut quoted over '>'-marked lines, fewer than 8 of the skeleton in a row -----------


def test_p2_a_cut_quoted_over_marked_lines_is_never_skipped_by_the_quick_filter():
    """W's beginning quoted over two '>'-marked lines and cut ('…'): the markers break every 8 characters of W's
    skeleton, and the quoted digits hold fewer than 8 word characters, so no gram of W's is in the row's skeletons
    and nothing in it needs decoding. Only the quote-shape bypass keeps the row for the comparison, which reads a cut;
    the same cut without markers is found by its grams."""
    w = "4, 7, 1, 9, 2, 6, 3 opens the vault behind the third tile."
    row = "Earlier you wrote:\n> 4, 7, 1,\n> 9, 2, 6…\nok"
    q = I.quick_of(w)
    assert I.CUT in I.carrier_relations([row], w)
    assert not I._NEEDS_DECODE.search(row) and not I.SNAPSHOT_LEAD.search(row)
    assert I._grams(I.skeleton(row)).isdisjoint(q.grams) and I._grams(I.word_skeleton(row)).isdisjoint(q.word_grams)
    assert not any(x in I.skeleton(row) or x in I.word_skeleton(row) for x in q.whole)
    assert I.quick_related([row], q)
    plain = "4, 7, 1,\n9, 2, 6…"
    assert I.CUT in I.carrier_relations([plain], w) and not I._grams(I.skeleton(plain)).isdisjoint(q.grams)
