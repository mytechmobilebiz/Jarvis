"""0.6.2 option C (rebuild R2): the evidence of a row held with its conversation group (identity.group_relations).
'none' only when nothing relates and every withheld text was in hand; otherwise 'other_not_compared' (never released
in one click); 'not in this backup' when the group's own withheld text is not in hand; 'uncertain' for an uncertain
link. Pure; synthetic text only."""

from __future__ import annotations

from app.brain import identity

W = "The new vendor contract with zqunit is worth forty thousand dollars a year."
W2 = "The second secret: the office moves to the harbour building in March."


def test_none_only_when_every_withheld_text_is_in_hand():
    assert identity.group_relations("Book the train.", W, [], all_in_hand=True) == (identity.NO_RELATION,)
    assert identity.group_relations("Book the train.", W, [], all_in_hand=False) == (identity.OTHER_NOT_COMPARED,)


def test_a_relation_to_the_group_s_own_or_any_other_withheld_text_in_hand_is_shown():
    assert identity.CONTAINS in identity.group_relations("Forward this: " + W, W, [], all_in_hand=False)
    found = identity.group_relations("Note: " + W2, W, [W2], all_in_hand=True)
    assert identity.CONTAINS in found and identity.NO_RELATION not in found
    assert identity.needs_confirmation(found)


def test_the_group_s_own_text_not_in_hand_and_an_uncertain_link():
    assert identity.group_relations("Book the train.", None, [], all_in_hand=True) == (identity.NOT_IN_BACKUP,)
    found = identity.group_relations("Book the train.", W, [], all_in_hand=True, uncertain=True)
    assert found == (identity.UNCERTAIN_REL,) and identity.needs_confirmation(found)


def test_structural_relations_are_kept_and_never_none():
    found = identity.group_relations("Other text.", W, [], all_in_hand=True, structural={identity.SAME_TIME_REL})
    assert found == (identity.SAME_TIME_REL,)


def test_the_new_hold_vias_and_the_state_are_known_codes():
    assert identity.GROUP_HOLD in identity.HOLD_VIA and identity.COPY_HOLD in identity.HOLD_VIA
    assert identity.OTHER_NOT_COMPARED in identity.RELATIONS and identity.OTHER_NOT_COMPARED in identity.RELATION_TEXT
    assert not identity.needs_confirmation([identity.OTHER_NOT_COMPARED])
