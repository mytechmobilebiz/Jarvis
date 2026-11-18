"""0.6.2 option C (rebuild R3): closing tests of the matrix's m18:dec_* guards, unit level (the canonical decode's
escape and repr readings). Synthetic text only."""

from __future__ import annotations

import json

import pytest

from app.brain import identity as I

W = 'Er sagte: "Der Tresor öffnet mit Kranich-7741"\nund dann ging er nach Hause, ohne Licht.'


def test_dec_escape_a_cut_json_text_carries_w_through_its_escapes_only():
    """A JSON text cut short (no parse): W's quotes and line break are escapes; only the whole-text unescape reads W
    back (contains); a cut inside it is a cut."""
    full = json.dumps({"from": "anna", "text": W + " Ende."}, ensure_ascii=False)
    cut = full[:-5]
    with pytest.raises(ValueError):
        json.loads(cut)
    assert I.fflat(W) not in I.fflat(cut)
    assert I.CONTAINS in I.carrier_relations([cut], W)
    early = full[:full.index("nach Hause")]
    assert I.CUT in I.carrier_relations([early + "…"], W)
    # escapes outside any literal (a log line holding a JSON string's body): only the whole-text unescape reads W
    log = "send payload=" + json.dumps(W)[1:-1] + " status=200"
    assert I.fflat(W) not in I.fflat(log)
    assert I.CONTAINS in I.carrier_relations([log], W)


@pytest.mark.parametrize("shape", ["str_dict", "code", "short_value"])
def test_dec_repr_a_repr_literal_only_unrepr_reads_back_carries_w(shape):
    """A Python repr literal (a FAILED suffix's str(dict), a literal in code) whose text only an unrepr reads back; a
    short W as a whole repr value is identical only through the literal read (a whole value)."""
    if shape == "short_value":
        w = "Ja, Möwe geht."
        text = "[clarify] FAILED: %s" % str({"reply": w.encode("ascii", "backslashreplace").decode("ascii")})
        assert I.IDENTICAL_REL in I.carrier_relations([text], w)
        return
    if shape == "str_dict":
        text = "[send_message] to=anna (12 chars result) FAILED: %s" % str({"error": W.encode("ascii", "backslashreplace")
                                                                            .decode("ascii")})
    else:
        text = "msg = %s\nsend(msg)" % ascii(W)
    assert I.fflat(W) not in I.fflat(text)
    assert I.CONTAINS in I.carrier_relations([text], W)


def test_dec_json_a_short_w_as_a_whole_value_of_pretty_printed_ascii_json_is_identical():
    """m18:dec_json_off: a short W as a whole value of an indented ensure_ascii JSON document (no value context the
    literal read knows): only the JSON parse gives the whole value."""
    w = "Ja, Möwe geht."
    text = json.dumps({"replies": [w, "other"]}, ensure_ascii=True, indent=2)
    assert I.IDENTICAL_REL in I.carrier_relations([text], w)
