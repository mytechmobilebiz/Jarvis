"""0.6.2 option C (rebuild R3, round-3 derived3): quote spans (N1), the capped iterative decode (N2), the repr escape
cut (N4), default-ignorable code points (N5), W read without '> ' quote markers (13546), and the skeptic's pins on
Hermes's tools/tool_result_storage.py previews (M1: unrepr_cut_keep, open_literal_off, cut_per_line_off). Unit level,
synthetic text built the way the vendored Hermes builds it."""

from __future__ import annotations

import json

import pytest

from app.brain import identity as I

W = "Don't tell anyone: the Lagerhaus alarm code is Kranich-7741\nand the spare key sits under the blue pot."
WD = "Die Grundstücksgröße beträgt genau 1.250 Quadratmeter am Seeufer, Flurstück 88/4."


def carries(text, w=W):
    return I.carrier_relations([text], w)


# --- N1: Hermes's f-string summaries with apostrophes and line breaks ------------------------------------------------


def test_n1_vision_analyze_summary_with_an_apostrophe_and_a_line_break_is_a_cut():
    line = "[vision_analyze] '%s' (%s chars)" % (W[:50], format(2048, ","))
    assert "'" in W[:50] and "\n" not in W[:50]
    assert I.CUT in carries(line)
    w2 = "Look:\nit's the picture of the safe at Kranich-7741 with the dial on 3"
    line2 = "[vision_analyze] '%s' (99 chars)" % w2[:50]
    assert "\n" in w2[:50] and "'" in w2[:50]
    assert I.CUT in carries(line2, w2)


@pytest.mark.parametrize("tool", ["web_search", "search_files", "delegate_task"])
def test_n1_quoted_argument_of_a_summary_is_read_whole_across_apostrophes_and_lines(tool):
    if tool == "web_search":
        line = "[web_search] query='%s' (5,120 chars result)" % W
    elif tool == "search_files":
        line = "[search_files] content search for '%s' in /home/anna -> 3 matches" % W
    else:
        goal = W if len(W) <= 60 else W[:57] + "..."
        line = "[delegate_task] '%s' (812 chars result)" % goal
    got = carries(line)
    assert got & {I.CONTAINS, I.IDENTICAL_REL, I.CUT}, (tool, got)
    if tool == "delegate_task":
        assert I.CUT in got


def test_n1_a_summary_quoting_unrelated_words_holds_nothing():
    assert not carries("[vision_analyze] 'What is in this picture of a cat?' (2,048 chars)")
    assert not carries("[web_search] query='weather in Bern tomorrow' (5,120 chars result)")


# --- N2: the iterative decode with caps --------------------------------------------------------------------------


def nest(text: str, levels: int, *, ascii_: bool = True) -> str:
    out = text
    for n in range(levels):
        out = json.dumps({"level": n, "content": out}, ensure_ascii=ascii_)
    return out


@pytest.mark.parametrize("levels", [1, 2, 3, 4, 8, 10])
def test_n2_w_nested_in_json_any_number_of_levels_is_read(levels):
    text = nest(W, levels)
    assert I.CONTAINS in carries(text)
    assert not I.canon(text).capped


def test_n2_an_unrelated_nested_read_is_neither_related_nor_capped():
    text = nest("The train to Bern leaves at nine from platform four.", 6)
    assert not carries(text)
    assert not I.canon(text).capped


def test_n2_nesting_past_the_depth_cap_is_not_checked(monkeypatch):
    """JSON escaping doubles a string's backslashes at each level, so the size budget or the step cap usually stops a
    deep chain first; the depth cap itself, made small here, is a cap the same way."""
    monkeypatch.setattr(I, "CANON_DEPTH", 3)
    I._canon_cached.cache_clear()
    text = nest("nothing of the withheld text here", 6)
    c = I._canon(text)
    assert c.capped
    assert I.NOT_CHECKED in I.carrier_relations([text + " "], W)
    I._canon_cached.cache_clear()


def test_n2_a_chain_past_the_size_budget_is_not_checked(monkeypatch):
    monkeypatch.setattr(I, "CANON_BUDGET", 1_500)
    text = nest("nothing of the withheld text here " * 20, 4)
    assert I._canon(text).capped


def test_n2_more_decode_steps_than_the_cap_is_not_checked():
    parts = [json.dumps({"n": i, "x": "a\\\\b %d" % i}) for i in range(I.CANON_STEPS + 5)]
    text = json.dumps({"items": parts})
    assert I.canon(text).capped
    assert I.NOT_CHECKED in carries(text)


def test_n2_json_nested_past_the_parser_is_capped():
    text = "[" * 200_000 + "]" * 200_000
    assert I.canon(text).capped
    assert I.NOT_CHECKED in carries(text)


def test_n2_a_field_over_the_size_limit_is_not_checked():
    text = "x" * (I.CANON_MAX_TEXT + 1)
    assert I.canon(text).capped
    assert I.NOT_CHECKED in carries(text)
    assert I.quick_related([text], I.quick_of(W))


def test_n2_a_capped_field_is_held_in_a_group_too_never_none():
    text = "[" * 200_000 + "]" * 200_000
    assert I.group_relations(text, W, all_in_hand=True) == (I.NOT_CHECKED,)
    assert I.needs_confirmation([I.NOT_CHECKED])


# --- N4: the repr escape cut ------------------------------------------------------------------------------------


def test_n4_execute_code_summary_cutting_a_repr_escape_in_half_is_a_cut():
    code = "notes = %s\nprint(notes)" % ascii(WD)
    preview = code[:60].replace("\n", " ") + ("..." if len(code) > 60 else "")
    assert preview.rstrip(".").endswith(("\\", "\\x", "\\xf", "\\xd")) or "\\x" in preview
    line = "[execute_code] `%s` (3 lines output)" % preview
    assert I.CUT in carries(line, WD)


@pytest.mark.parametrize("cut", range(1, 4))
def test_n4_a_repr_literal_cut_inside_an_escape_reads_what_came_before(cut):
    lit = ascii(WD)
    at = lit.index("\\xdf") + cut  # inside the escape for ß
    text = "value = " + lit[:at]
    assert I.CUT in carries(text, WD)


# --- N5: default-ignorable code points ---------------------------------------------------------------------------


def test_n5_fold_removes_the_default_ignorable_code_points():
    assert len(I.IGNORABLE) == 4174
    for c in ("­", "​", "‍", "️", "⁠", "ㅤ", "﻿", "\U000e0041", "᠎"):
        assert I.fold("Kra%snich" % c) == "kranich", hex(ord(c))


@pytest.mark.parametrize("mark", ["­", "​", "‍⁠", "️", "\U000e0020"])
def test_n5_invisible_formatting_never_hides_w(mark):
    hidden = "".join(c + mark if c.isalpha() else c for c in W)
    assert I.CONTAINS in carries("Copied: " + hidden)


# --- 13546: W read without '> ' quote markers -----------------------------------------------------------------------


def test_13546_a_short_w_quoted_with_markers_in_a_session_search_result_is_identical():
    w = "Ja, Kranich geht."
    assert len(I.fwords(w)) < I.MIN_RELATED
    hit = {"role": "user", "content": "Earlier you wrote:\n> Ja, Kranich geht.\n\nShall I book it?"}
    text = json.dumps({"success": True, "results": [{"session_id": "s1", "messages": [hit]}]})
    assert I.IDENTICAL_REL in carries(text, w)
    assert I.quick_related([text], I.quick_of(w))


def test_13546_a_long_w_quoted_over_several_marked_lines_is_read_without_the_markers():
    quoted = "\n".join("> " + ln for ln in W.split("\n"))
    assert I.fflat(W) not in I.fflat(quoted)
    assert I.CONTAINS in carries("You said:\n" + quoted + "\nRight?")


def test_13546_control_short_words_inside_prose_outside_every_group_hold_nothing():
    assert not carries("Ja, Kranich geht nicht zu uns, sagte sie.", "Ja, Kranich geht.")


# --- M1 pins: Hermes's large-result previews (tools/tool_result_storage.py) -----------------------------------------


def persisted(preview: str, has_more: bool, original: int) -> str:
    return ("<persisted-output>\nThis tool result was too large (%s characters, %.1f KB).\nFull output saved to: "
            "/tmp/x.txt\nUse the read_file tool with offset and limit to access specific sections of this output.\n\n"
            "Preview (first %d chars):\n" % (format(original, ","), original / 1024, len(preview))
            + preview + ("\n..." if has_more else "") + "\n</persisted-output>")


def generate_preview(content: str, max_chars: int) -> tuple[str, bool]:
    if len(content) <= max_chars:
        return content, False
    last_nl = content.rfind("\n", 0, max_chars)
    return content[:last_nl + 1 if last_nl > max_chars // 2 else max_chars], True


@pytest.mark.parametrize("into", [1, 2, 3, 4, 5])
def test_m1_prev_a_preview_cut_inside_ws_escape_is_a_cut(into):
    """unrepr_cut_keep / open_literal_off: a persisted-output preview of an ensure_ascii JSON cut inside W's escape
    for ß keeps W's first characters before it: read as a cut once the cut escape is dropped."""
    content = json.dumps({"path": "/srv/a", "text": WD + " " * 400}, ensure_ascii=True)
    at = content.index("\\u00df") + into
    preview, more = content[:at], True
    assert I.fflat(WD[:WD.index("ß")]) in I.fflat(I._unescape(preview))
    assert I.CUT in carries(persisted(preview, more, len(content)), WD)


def test_m1_inline_truncation_fallback_cut_at_a_line_is_a_cut():
    """cut_per_line_off: '<preview>\\n\\n[Truncated: tool response was N chars. ...]' where the preview ends at a
    line of W."""
    w = "Kontostand Treuhand: 48.300 EUR\nIBAN endet auf 7741, nur intern verwenden\nFreigabe durch Anna"
    content = "log start\n" + ("filler line\n" * 30) + w + "\n" + ("tail\n" * 400)
    preview, _ = generate_preview(content, len("log start\n" + "filler line\n" * 30) + 60)
    assert preview.endswith("\n") and w.split("\n")[0] in preview and w not in preview
    text = (preview + "\n\n[Truncated: tool response was %s chars. Full output could not be saved to sandbox.]"
            % format(len(content), ","))
    assert I.CUT in carries(text, w)


def test_m1_control_an_unrelated_preview_holds_nothing():
    content = json.dumps({"text": "Ordinary log output with nothing private in it at all. " * 50})
    assert not carries(persisted(content[:300], True, len(content)), WD)
