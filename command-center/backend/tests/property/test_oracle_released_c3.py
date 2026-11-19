"""cc_redacted_of in released_ok (round 3 consolidation oracle change 2, seeds 13763, 13863, 13879): a released row
stored with only the Command Center's own marks in place of spans is the released text; anything else is not. A mutant
in docs/rebuild-062/progress/R5-mutation/r5mut.py that accepts any marked text is killed here."""

from __future__ import annotations

from . import oracle as o

T = "Please mail the vendor review to someone@example.org and call 555 0100 before lunch."


def test_cc_redacted_of_accepts_the_released_text_with_the_command_centers_marks():
    assert o.cc_redacted_of(T, T)
    assert o.cc_redacted_of("Please mail the vendor review to [email] and call [phone] before lunch.", T)
    assert o.cc_redacted_of("Please mail the vendor review to [REDACTED:email] and call 555 0100 before lunch.", T)


def test_cc_redacted_of_refuses_other_text_beside_a_mark():
    assert not o.cc_redacted_of("Please mail the PARTNER AGREEMENT to [email] and call [phone] before lunch.", T)
    assert not o.cc_redacted_of("[email] Please rewrite the landing page copy for the team meeting.", T)


def test_cc_redacted_of_refuses_a_text_with_no_mark_that_differs():
    assert not o.cc_redacted_of("Please mail the vendor review before lunch.", T)


def test_cc_redacted_of_anchors_both_ends():
    assert not o.cc_redacted_of("Extra words. Please mail the vendor review to [email] and call [phone] before lunch.", T)
    assert not o.cc_redacted_of("Please mail the vendor review to [email] and call [phone] before lunch. Extra.", T)
