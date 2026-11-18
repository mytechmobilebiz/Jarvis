"""0.6.2 option C (rebuild R3): carriers of a withheld text in derived forms, unit level (cr2:*, m18:dec_json_off).

The comparison reads every field of a row through the canonical decode (identity.canon) and compares under fold()
(O-CI: NFKC, casefold, Turkish i, default-ignorable code points removed); generic-v1 pieces and pieces under 16
characters never count (O-D4); Hermes's generic prune line keeping W's first 40 characters is a cut; the quick filter of
the full stored-row scan never skips a row a reading relates. Synthetic text only."""

from __future__ import annotations

import json
import unicodedata

import pytest

from app.brain import identity as I

W = "Die Zugangsdaten zum Lagerbüro lauten Kranich-7741, bitte niemandem weitersagen."
W_TR = "Istanbul ofisinin kapı şifresi İkiyüz-Kırk dört ve kimseye söyleme"


def carries(text, w=W):
    return I.carrier_relations([text], w)


# --- m18:dec_json_off --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("shape", ["ensure_ascii", "double", "plain_control"])
def test_skc_a_json_value_only_json_loads_reads_back_is_a_carrier_and_evidence(shape):
    """A JSON value whose text only json.loads reads back (ensure_ascii escapes, a JSON string inside a JSON string)
    is a carrier and evidence (family too); the control is plain JSON whose text is readable as written."""
    if shape == "ensure_ascii":
        text = json.dumps({"result": {"note": W}}, ensure_ascii=True)
        assert I.fflat(W) not in I.fflat(text)  # only the decode reads it
    elif shape == "double":
        text = json.dumps({"content": json.dumps({"inner": W}, ensure_ascii=True)})
        assert I.fflat(W) not in I.fflat(text)
    else:
        text = json.dumps({"note": W}, ensure_ascii=False)
    assert I.CONTAINS in carries(text)
    assert I.CONTAINS in I.family_relations(text, W)
    assert I.CONTAINS in I.text_relations([text], W)


# --- O-CI (cr2:ci_casefold_off, ci_turkish_off, ci_nfkc_off, ci_wordseq_off) -------------------------------------


def _fullwidth(s: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else ("　" if c == " " else c) for c in s)


FORMS = {
    "en-all caps": (W, W.upper()),
    "en-title case": (W, W.title()),
    "en-fullwidth": (W, _fullwidth(W)),
    "en-nfd": (W, unicodedata.normalize("NFD", W)),
    "en-soft hyphen": (W, W.replace("Lagerbüro", "Lager­büro").replace("Kranich", "Kra​nich")),
    "tr-all caps": (W_TR, "İSTANBUL OFİSİNİN KAPI ŞİFRESİ İKİYÜZ-KIRK DÖRT VE KİMSEYE SÖYLEME"),
    "tr-lower": (W_TR, "istanbul ofisinin kapı şifresi ikiyüz-kırk dört ve kimseye söyleme"),
    "tr-dotless": (W_TR, "ıstanbul ofısının kapı şıfresı ıkıyüz-kırk dört ve kımseye söyleme"),
}


@pytest.mark.parametrize("case", sorted(FORMS))
def test_oci_every_relation_ignores_case_and_normalisation(case):
    """Identical, contains, a cut beginning and a reply quote are found in another case, width, normal form or with
    invisible characters, in the product's every mode."""
    w, form = FORMS[case]
    assert I.IDENTICAL_REL in I.text_relations(form, w)
    assert I.CONTAINS in I.carrier_relations(["Note: %s (end)" % form], w)
    cut = form[:40] + "…"
    assert I.CUT in I.carrier_relations(["Preview: " + cut], w)
    quote = '[Replying to: "%s"]\n\nok' % form[5:40]
    assert I.PART_OF in I.carrier_relations([quote], w)


def test_oci_a_short_withheld_text_in_another_case_is_ambiguous_not_none():
    """A withheld text of fewer than 16 characters of words, in another case inside a longer row of its group, is
    'contains' (ambiguous, never 'none'); a whole value equal in another case is identical anywhere; a row with other
    words is not related."""
    w = "Code ist 4711!"
    assert len(I.fwords(w)) < I.MIN_RELATED
    assert I.CONTAINS in I.family_relations("ja, der CODE IST 4711, wie besprochen", w)
    assert I.group_relations("ja, der CODE IST 4711, wie besprochen", w, all_in_hand=True) != (I.NO_RELATION,)
    assert I.IDENTICAL_REL in I.carrier_relations([json.dumps({"reply": "code IST 4711"})], w)
    assert not I.family_relations("der Code ist 4712, wie besprochen", w)
    # outside every group a short text inside a longer one is not demanded (O-D4), only a whole value
    assert not I.carrier_relations(["ja, der CODE IST 4711, wie besprochen"], w)


# --- O-D4 (cr2:generic_off, generic_overbroad) --------------------------------------------------------------------


def test_skc_a_common_url_value_in_an_unrelated_search_result_is_no_carrier():
    """A withheld text that starts with a common URL: an unrelated web_search result listing that URL (a cut piece of
    the withheld text that is generic-v1) is no carrier; the URL with more of the withheld text is."""
    w = "https://www.wikipedia.org/ meine Notizen zum Fall Kranich-7741 liegen im Ordner Q"
    result = json.dumps({"results": [{"url": "https://www.wikipedia.org/", "title": "Wikipedia"}]})
    assert not carries(result, w)
    assert not carries("See https://www.wikipedia.org/ …", w)
    assert I.generic("https://www.wikipedia.org/")
    assert I.CUT in carries("See https://www.wikipedia.org/ meine Notizen zum Fall …", w)


@pytest.mark.parametrize("piece,held", [
    ("Thank you very much", False), ("Hello there", False), ("Good morning", False), ("https://www.google.com/", False),
    ("Danke schön", False), ("Teşekkür ederim", False),
    ("Thank you very much, Anna", True), ("Good morning, the vault", True)])
def test_od4_greeting_and_url_cases_hold_nothing_and_anything_more_is_held(piece, held):
    """A cut beginning of the withheld text that is only a greeting, a thanks or a bare URL holds nothing; anything
    longer than 16 characters beyond them is held."""
    w = piece + " " + "is the one who keeps the vault code Kranich-7741 for us"
    got = carries("Earlier: %s…" % piece, w)
    assert (I.CUT in got) is held
    assert I.generic(piece) is not held
    assert not I.generic("Kranich-7741 for the vault")
    assert I.generic("short piece")  # under 16 characters


def test_od4_a_generic_whole_withheld_text_is_no_carrier_but_its_group_still_compares_it():
    w = "Thank you very much!"
    assert not carries("Thank you very much!", w)
    assert I.IDENTICAL_REL in I.family_relations("Thank you very much!", w)


# --- P1: Hermes's generic prune line (cr2:prune_off) --------------------------------------------------------------


@pytest.mark.parametrize("tool,args", [
    ("image_generate", {"prompt": W, "size": "1024x1024"}),
    ("send_message", {"to": "anna", "text": W}),
    ("mcp_notes_create", {"body": W}),
    ("unknown_tool", {"q": W, "page": 2}),
    ("send_message", {"to": "anna", "text": "hello"}),  # control
    ("image_generate", {"prompt": "a cat on a sofa in the evening light", "size": "1"}),  # control
])
def test_p1_hermes_generic_prune_line_keeping_ws_first_40_characters_is_a_cut(tool, args):
    """agent/context_compressor.py _summarize_tool_result_unguarded: '[<tool>] k=<str(v)[:40]> ... (N chars result)'
    keeps W's first 40 characters with no cut mark: a cut, held outside every group; the controls hold nothing."""
    first_arg = "".join(" %s=%s" % (k, str(v)[:40]) for k, v in list(args.items())[:2])
    line = "[%s]%s (%s chars result)" % (tool, first_arg, format(1234, ","))
    related = W in args.values()
    assert (I.CUT in carries(line)) is related
    assert bool(carries(line)) is related


# --- P2: the full scan's quick filter (cr2:quick_*) ---------------------------------------------------------------


def test_p2_the_skeleton_drops_every_whitespace_and_combining_character():
    s = "Ḱra n̈i\tch - 7741\n​"
    sk = I.skeleton(s)
    assert not any(c.isspace() or unicodedata.combining(c) for c in sk)
    assert "​" not in sk and "́" not in sk
    assert I.skeleton("Kranich - 7741") == I.skeleton("k r a n i c h-7741")
    # a row whose only relation spreads W over unusual whitespace and marks is still compared
    row = "Note: " + " ".join(W).replace("a", "á")[:200]
    assert I.quick_related([row], I.quick_of(W)) or not I.carrier_relations([row], W)
    spaced = "Note: " + W.replace(" ", "   ") + " end"
    assert I.CONTAINS in I.carrier_relations([spaced], W)
    assert I.quick_related([spaced], I.quick_of(W))


ROWS_RELATED = [
    "Note: " + W.upper(),
    json.dumps({"a": json.dumps({"b": W}, ensure_ascii=True)}),
    "[vision_analyze] '%s' (12 chars)" % W[:50],
    "[image_generate] prompt=%s size=1 (12 chars result)" % W[:40],
    "Preview (first 50 chars):\n" + json.dumps({"t": W}, ensure_ascii=True)[:60] + "\n...",
    "x = 'Die Zugangsdaten zum Lagerb\\xfcro lauten Kranich-7741, bitte niemandem weitersagen.'",
    "Result:\n> " + W[:30] + "\n> " + W[30:],
]


@pytest.mark.parametrize("row", ROWS_RELATED)
def test_p2_the_quick_filter_never_skips_a_row_the_comparison_relates(row):
    assert I.carrier_relations([row], W), row
    assert I.quick_related([row], I.quick_of(W))


def test_p2_the_quick_filter_skips_an_unrelated_row():
    assert not I.quick_related(["The train to Bern leaves at nine, platform four."], I.quick_of(W))


def test_p2_a_reply_quote_of_sixteen_characters_as_written_is_never_skipped_by_the_quick_filter():
    """A reply quote of 16 characters as JARVIS wrote it (whitespace kept) holds fewer than 8 characters of W's
    skeleton: the quick filter never skips a quote shape."""
    w = "a b c d e f g h i j k l m n o p q r s"
    quote = '[Replying to: "a  b  c  d  e  f"]\n\nyes'
    assert len('a  b  c  d  e  f') >= I.MIN_RELATED
    assert I.PART_OF in I.text_relations(quote, w)
    assert len(I.skeleton("a  b  c  d  e  f")) < I.QUICK_Q
    assert I.quick_related([quote], I.quick_of(w))


def test_p2_a_snapshot_of_earlier_turns_is_never_skipped_by_the_quick_filter():
    """cr2:quick_no_snapshot (an invariant pin): a summary or snapshot of earlier turns (SNAPSHOT_LEAD) is always
    compared, whatever the withheld text's grams."""
    for lead in ("[CONTEXT SUMMARY]: earlier turns", "## Historical Task Snapshot\n- the user asked", "## historical pending"):
        assert I.SNAPSHOT_LEAD.search(lead)
        assert I.quick_related([lead + " nothing alike"], I.quick_of(W))


def test_p2_the_quick_filter_compares_every_form_of_the_withheld_text():
    """cr2:quick_forms_one: the withheld text's authored text (peeled) and its whole text are both forms."""
    w = '[Replying to: "the earlier plan"]\n\n' + W
    assert I.quick_related(["Note: " + W], I.quick_of(w))
    assert I.CONTAINS in I.carrier_relations(["Note: " + W], w)
