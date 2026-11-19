"""0.6.2 option C stabilisation S7 (RECON-68): a decision whose text in hand is a stand-in for W's (a kept 'identical'
member's text, or a copy of W at W's time with another uid; W's own row is gone). The later-backup classifier's pure
rules (app/brain/later.py), without a database: relations to the stand-in are shown, an unrelated row is never 'none'
('not compared with every withheld text' instead, never one click), a verified text still gives 'none' (control).
Synthetic values only."""

from __future__ import annotations

from datetime import timedelta

from app.brain import identity, later

from .test_brain_later import T0, msg, own

W = "Quick summary of the quarterly plan: on track, no blockers today."
OTHER = "Please book the hotel near the river front for the team."


def test_s7_a_stand_in_never_gives_none_and_shows_its_relations():
    d = later.Decision("g1", own(key="g1"), W, unverified=True)
    row = msg(9, OTHER, uid="x9", session="S1", at=T0 + timedelta(hours=1), role="user")
    _, rel = later.strongest(row, identity.exact_of_source("S1", row), [d])
    assert rel == (identity.OTHER_NOT_COMPARED,)
    same = msg(10, W, uid="x10", session="S1", at=T0 + timedelta(hours=2), role="assistant")
    _, rel = later.strongest(same, identity.exact_of_source("S1", same), [d])
    assert identity.IDENTICAL_REL in rel and identity.NO_RELATION not in rel
    assert identity.NOT_IN_BACKUP not in rel and identity.OTHER_NOT_COMPARED not in rel


def test_s7_a_verified_text_still_gives_none_control():
    d = later.Decision("g1", own(key="g1"), W)
    row = msg(9, OTHER, uid="x9", session="S1", at=T0 + timedelta(hours=1), role="user")
    _, rel = later.strongest(row, identity.exact_of_source("S1", row), [d])
    assert rel == (identity.NO_RELATION,)


def test_s7_decision_of_marks_a_stand_in_only_when_a_text_is_in_hand():
    ids = identity.Identities.__new__(identity.Identities)
    ids.by_key = {"g1": own(key="g1", uid="u1")}
    ids.groups = {}
    assert later.decision_of(ids, "g1", W, "b" * 64, unverified=True).unverified
    assert not later.decision_of(ids, "g1", None, "b" * 64, unverified=True).unverified
    assert not later.decision_of(ids, "g1", W, "b" * 64).unverified
