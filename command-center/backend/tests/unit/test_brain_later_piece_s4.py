"""0.6.2 option C stabilisation S4 (RECON-64): a withheld fold whose copy in this backup lost a message it had folded
in. The later-backup classifier's pure rules (app/brain/later.py), without a database: a decision whose text in hand
is a piece of the withheld fold never gives 'none' ('not in this backup' instead, its relations to the piece shown),
a whole text still does (control), and a row carrying a uid the withheld fold absorbed is never 'none'. Synthetic
values only."""

from __future__ import annotations

from datetime import timedelta

from app.brain import identity, later

from .test_brain_later import T0, msg, own

FIRST = "Please look again at the hosting migration before lunch."
SECOND = "Please look again at the landing page copy with the usual care."


def test_s4_a_piece_of_the_withheld_fold_never_gives_none():
    d = later.Decision("g1", own(key="g1"), FIRST, piece=True)
    row = msg(9, SECOND, uid="x9", session="S1", at=T0 + timedelta(hours=1), role="user")
    _, rel = later.strongest(row, identity.exact_of_source("S1", row), [d])
    assert rel == (identity.NOT_IN_BACKUP,)
    same = msg(10, FIRST, uid="x10", session="S1", at=T0 + timedelta(hours=2), role="user")
    _, rel = later.strongest(same, identity.exact_of_source("S1", same), [d])
    assert identity.IDENTICAL_REL in rel and identity.NO_RELATION not in rel


def test_s4_a_whole_withheld_text_still_gives_none_control():
    d = later.Decision("g1", own(key="g1"), FIRST)
    row = msg(9, SECOND, uid="x9", session="S1", at=T0 + timedelta(hours=1), role="user")
    _, rel = later.strongest(row, identity.exact_of_source("S1", row), [d])
    assert rel == (identity.NO_RELATION,)


def test_s4_a_row_with_a_uid_the_withheld_fold_absorbed_is_never_none():
    d = later.Decision("g1", own(key="g1"), FIRST, absorbed={"ab1"})
    row = msg(9, SECOND, uid="ab1", session="S1", at=T0 + timedelta(hours=1), role="user")
    _, rel = later.strongest(row, identity.exact_of_source("S1", row), [d])
    assert identity.SAME_UID in rel and identity.NO_RELATION not in rel


def test_s4_decision_of_keeps_the_absorbed_uids_but_never_the_own_uid():
    ids = identity.Identities.__new__(identity.Identities)
    ids.by_key = {"g1": own(key="g1", uid="u1")}
    ids.groups = {}
    d = later.decision_of(ids, "g1", FIRST, "b" * 64, ["ab1", "u1", ""], piece=True)
    assert d is not None and d.absorbed == {"ab1"} and d.piece
    assert not later.decision_of(ids, "g1", None, "b" * 64, ["ab1"], piece=True).piece
