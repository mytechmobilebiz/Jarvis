"""0.6.2 option C (rebuild R4): hold states and releases without a database (D3, B6, 6725 / F1, carried_via).

settled_relations (none_every, keep_later), eligible_now (every withheld text compared), the form a hold's text was
read in (text_form), Identities.released / unchecked (trust from the covering release only), and a decisions file's
kept hold keeping its rule (carried_via). Synthetic data only."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.brain import identity, review
from app.brain import importer as imp

NONE, LATER, OTHER = identity.NO_RELATION, identity.LATER_WITHHOLD, identity.OTHER_NOT_COMPARED


def test_none_every_none_needs_every_withheld_text_in_hand():
    assert identity.settled_relations([NONE], all_in_hand=True) == (NONE,)
    assert identity.settled_relations([NONE], all_in_hand=False) == (OTHER,)
    assert identity.settled_relations([], all_in_hand=False) == (OTHER,)
    assert identity.settled_relations([identity.SAME_UID, NONE], all_in_hand=False) == (identity.SAME_UID, OTHER)


def test_keep_later_keeps_the_mark_until_every_text_was_in_hand():
    assert identity.settled_relations([NONE], [LATER], all_in_hand=False) == (LATER, OTHER)
    assert identity.settled_relations([identity.CONTAINS], [LATER], all_in_hand=False) == (identity.CONTAINS, LATER)
    assert identity.settled_relations([NONE], [LATER], all_in_hand=True) == (NONE,)


def eligible(relations=(NONE,), *, role="user", form=identity.PLAIN_FORM, uncertain=False, compared=("a",),
             anchors=("a",)) -> bool:
    return review.eligible_now(relations, role=role, form=form, uncertain=uncertain, compared=compared,
                               anchors=anchors)


def test_eligible_now_compares_every_withheld_text_in_hand():
    assert eligible()
    assert not eligible(anchors=("a", "b"))  # a withhold that came later: never compared with it
    assert eligible(compared=("a", "b"), anchors=("a", "b"))
    assert not eligible(compared=(), anchors=("a",))  # a 'none' a file carried without what it was compared with


def test_uncertain_is_never_one_click_eligible_and_never_eligible_now():
    assert not review.one_click_eligible([NONE], role="user", form=identity.PLAIN_FORM, uncertain=True)
    assert not review.one_click_eligible([NONE, identity.UNCERTAIN_REL], role="user", form=identity.PLAIN_FORM,
                                         uncertain=False)
    assert not eligible(uncertain=True)


@pytest.mark.parametrize("relations", [(LATER,), (OTHER,), (identity.NOT_IN_BACKUP,), (identity.SAME_UID,),
                                       (identity.NOT_CHECKED,), ()])
def test_d3_states_that_were_not_compared_are_never_one_click(relations):
    assert not review.one_click_eligible(relations, role="user", form=identity.PLAIN_FORM, uncertain=False)


@pytest.mark.parametrize("content,calls,form", [
    ("What is the deal worth?", None, identity.PLAIN_FORM),
    ("Thanks, that is all.", None, identity.PLAIN_FORM),
    ("", None, ""),
    ("[Old tool output cleared to save context space]", None, ""),
    ("Looking it up.", [{"id": "c1", "function": {"name": "web_search"}}], identity.TOOL_FORM),
    ("[CONTEXT COMPACTION] Summary of the earlier turns: the user asked about deals.", None, identity.SUMMARY_FORM),
    ("[web_search] query='vendor deals' (1,204 chars result)", None, identity.SUMMARY_FORM),
    ('[Replying to: "the deal is done"]\n\nGood to hear.', None, identity.WRAPPED_FORM),
    ("> the deal is done\n\nGood to hear.", None, identity.WRAPPED_FORM),
    ('{"note": {"inner": "the deal is done"}}', None, identity.ENCODED_FORM),
    ('line one\\nline two \\"quoted\\"', None, identity.ENCODED_FORM),
])
def test_text_form_reads_the_form_a_hold_was_compared_in(content, calls, form):
    assert identity.text_form(content, calls) == form


def _ids(trusted_on: dict[str, bool]) -> tuple[identity.Identities, identity.Exact]:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    row = identity.exact("s1", "7", "user", "", at, "u7")
    bindings = {("g", "s1", "7"): [identity.Binding("user", "", at, "u7", on, tr) for on, tr in trusted_on.items()]}
    return identity.Identities([], [("g", "s1", "7")], bindings), row


def test_identities_released_takes_trust_from_the_covering_release_only():
    ids, row = _ids({"b1": True, "b2": False})
    assert ids.released("g", row, backup="b1")
    assert not ids.released("g", row, backup="b2")  # the trusted release covers another backup's content
    assert [b.decided_on for b in ids.unchecked("g", row, backup="b2")] == ["b2"]
    assert ids.unchecked("g", row, backup="b1") == []


def _kept(via: str) -> dict:
    return {"source_key": "jarvis:message:s1:7", "reason": identity.WITHHELD_BY_ADMIN, "anchor": "jarvis:message:s1:2",
            "session": "s1", "row": "7", "role": "user", "uid": "u7", "via": via, "review": identity.KEPT,
            "kept_by": "admin", "kept_at": "2026-01-02T00:00:00+00:00", "relations": [NONE]}


@pytest.mark.parametrize("via", [identity.GROUP_HOLD, identity.SETTLE_HOLD, identity.CARRIER_HOLD])
def test_carried_via_a_kept_hold_keeps_the_rule_that_held_it(via):
    assert imp._carried_stone(_kept(via), via="restore", after=0).via == via
    assert imp._carried_stone(_kept("not a rule"), via="restore", after=0).via == "restore"


def test_a_file_s_hold_carries_what_its_none_was_compared_with_and_its_form():
    r = {**_kept(identity.GROUP_HOLD), "review": identity.PENDING, "compared": ["a", "b", 3], "form": "plain"}
    st = imp._carried_stone(r, via="decisions", after=0)
    assert st.compared == ("a", "b") and st.form == identity.PLAIN_FORM
    st = imp._carried_stone({**r, "compared": "a", "form": "<script>"}, via="decisions", after=0)
    assert st.compared == () and st.form == ""
