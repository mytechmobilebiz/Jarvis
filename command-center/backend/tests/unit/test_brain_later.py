"""JARVIS Brain 0.6.2 step 5: the later-backup classifier's pure rules (app/brain/later.py), without a database.

CERTAIN only by provenance (exact identity, Hermes's copy identity, an absorbed uid; a fold stone is never a uid
source; a uid on a readable or released row is never a copy identity), the safe copy's text and readers, the computed
evidence of a hold and the decision it is listed under, and the tripwire's comparison. Synthetic values only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.brain import identity, later
from app.brain.source import SourceMessage

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
W = "The new vendor contract with Northwind is worth forty thousand dollars a year."


def msg(row_id: int, content: str = W, *, role: str = "assistant", session: str = "S1", uid: str = "",
        key: str | None = None, at: datetime | None = T0, absorbed: tuple[str, ...] = (),
        tool_call_id: str = "") -> SourceMessage:
    key = key or ("jarvis:message:uid:%s" % uid if uid else "jarvis:message:%s:%d" % (session, row_id))
    return SourceMessage(id=row_id, session_id=session, role=role, content=content, tool_name="",
                         tool_call_id=tool_call_id, tool_calls_raw=None, at=at, time_assumed_local=False,
                         active=1, compacted=0, key=key, uid=uid, absorbed=absorbed)


def own(**kw) -> identity.Stone:
    base = dict(key="jarvis:message:uid:u1", session="S1", row="2", role="assistant", uid="u1", at=T0, bound=True,
                decided_on="d" * 64)
    base.update(kw)
    return identity.Stone(**base)


def gone(session: str, row: str) -> bool:
    return False


def present(session: str, row: str) -> bool:
    return True


def test_certain_by_exact_identity_copy_identity_or_absorbed_uid_only():
    ids = identity.Identities([own()])
    # the exact row
    assert later.certain(ids, "S1", msg(2, uid="u1"), present, set()).via == "key"
    # a copy elsewhere: the same uid, time, role and call (its key is another row's)
    mt = later.certain(ids, "S2", msg(7, uid="u1", session="S2", key="jarvis:message:S2:7"), present, set())
    assert (mt.via, mt.review) == ("copied by JARVIS", identity.CERTAIN)
    # the same uid at another time: no certainty (it is listed by the classifier)
    assert later.certain(ids, "S2", msg(8, uid="u1", session="S2", key="jarvis:message:S2:8",
                                        at=T0 + timedelta(seconds=5)), present, set()) is None
    # a uid a readable or released stored row carries is never a copy identity
    assert later.certain(ids, "S2", msg(7, uid="u1", session="S2", key="jarvis:message:S2:7"), present,
                         {"u1"}) is None
    # a row absorbing the withheld uid
    mt = later.certain(ids, "S1", msg(9, "Composite", uid="u9", absorbed=("u1",)), present, set())
    assert (mt.via, mt.review) == (identity.FOLD, identity.CERTAIN)


def test_a_fold_stone_is_never_a_uid_source():
    fold = own(key="jarvis:message:uid:s1", row="5", uid="s1", via=identity.FOLD, anchor="jarvis:message:uid:u1")
    ids = identity.Identities([own(), fold])
    assert later.certain(ids, "S2", msg(3, "Survivor text", uid="s1", session="S2", key="jarvis:message:S2:3"),
                         present, set()) is None
    assert later.certain(ids, "S2", msg(4, "x", uid="u4", session="S2", absorbed=("s1",)), present, set()) is None


def test_the_key_passes_to_another_row_only_with_the_stones_copy_identity():
    ids = identity.Identities([own()])
    # the bound row is gone: the next holder of the uid key with the same uid, time, role and call is it
    assert later.certain(ids, "S3", msg(1, uid="u1", session="S3"), gone, set()).review == identity.CERTAIN
    # at another time it is never withheld for good by the key: left out (MOVED)
    assert later.certain(ids, "S3", msg(1, uid="u1", session="S3", at=T0 + timedelta(minutes=1)), gone,
                         set()) is later.MOVED
    # the bound row is still in the backup: the key names another message
    assert later.certain(ids, "S3", msg(1, uid="u1", session="S3"), present, set()) is later.MOVED
    # the same row id with another time (a reused row id after a snapshot restore)
    keyed = own(key="jarvis:message:S1:2", uid="")
    ids = identity.Identities([keyed])
    assert later.certain(ids, "S1", msg(2, "Another message", at=T0 + timedelta(days=1)), present,
                         set()) is later.MOVED
    # a hold passes as the hold it is
    hold = own(key="jarvis:message:uid:h1", uid="h1", review=identity.PENDING, anchor="jarvis:message:uid:u1",
               row="8")
    ids = identity.Identities([own(), hold])
    mt = later.certain(ids, "S3", msg(1, "Other", uid="h1", session="S3", at=T0 + timedelta(hours=1)), gone, set())
    assert mt.review == identity.PENDING


def test_relations_and_the_decision_a_hold_is_listed_under():
    d1 = later.Decision("g1", own(key="g1"), W, uids={"u1"})
    d2 = later.Decision("g2", own(key="g2", session="S9", row="1", uid="u9", at=T0 - timedelta(days=3)), None,
                        uids={"u9"})
    here = identity.exact_of_source("S2", msg(4, "Remind me: " + W, uid="x", session="S2"))
    d, rel = later.strongest(msg(4, "Remind me: " + W, uid="x", session="S2"), here, [d2, d1])
    assert d is d1 and identity.CONTAINS in rel and identity.NOT_IN_BACKUP not in rel
    # no text relation: listed under the decision whose text the backup lacks ('not in this backup'), never 'none'
    other = msg(5, "Shall we book the venue?", uid="y", session="S2", at=T0 + timedelta(hours=2))
    d, rel = later.strongest(other, identity.exact_of_source("S2", other), [d1, d2])
    assert d is d2 and rel == (identity.NOT_IN_BACKUP,)
    # every text in the backup and no relation: 'none'
    d, rel = later.strongest(other, identity.exact_of_source("S2", other), [d1])
    assert rel == (identity.NO_RELATION,)
    # structural relations: the same time and role, the same uid with other text, a reused row number
    stub = msg(2, "[pruned]", uid="u1", session="S1")
    _, rel = later.strongest(stub, identity.exact_of_source("S1", stub), [d1])
    assert set(rel) == {identity.SAME_UID}
    reused = msg(2, "Another message", uid="z", session="S1", at=T0 + timedelta(days=1))
    _, rel = later.strongest(reused, identity.exact_of_source("S1", reused), [d1])
    assert identity.REUSED_ROW in rel
    copy = msg(7, W, uid="fresh", session="S2")
    _, rel = later.strongest(copy, identity.exact_of_source("S2", copy), [d1])
    assert {identity.IDENTICAL_REL, identity.SAME_TIME_REL} <= set(rel)


def test_a_safe_copy_needs_its_text_time_and_readers():
    text = ("user", "", "please call me back", "[]")
    stored = later.Readable(1, 10, text, "supervisor", "jarvis", "general", "general", False)
    found = {("user", "", T0): [stored]}
    here = identity.exact("S2", "5", "user", "", T0, "")
    assert later.safe_copy(here, text, "supervisor", "jarvis", "general", found) is stored
    assert later.safe_copy(here, text, "supervisor", "jarvis", "customer", found) is stored  # fewer readers
    assert later.safe_copy(here, ("user", "", "other words", "[]"), "supervisor", "jarvis", "general", found) is None
    assert later.safe_copy(here, text, "communications", "jarvis", "general", found) is None  # other agent
    assert later.safe_copy(identity.exact("S2", "5", "user", "", T0 + timedelta(seconds=1), ""), text,
                           "supervisor", "jarvis", "general", found) is None  # another time
    customer = later.Readable(1, 10, text, "supervisor", "jarvis", "customer", "customer", False)
    assert later.safe_copy(here, text, "supervisor", "jarvis", "general", {("user", "", T0): [customer]}) is None
    in_customer_conversation = later.Readable(1, 10, text, "supervisor", "jarvis", "general", "customer", False)
    assert later.safe_copy(here, text, "supervisor", "jarvis", "general",
                           {("user", "", T0): [in_customer_conversation]}) is None


def test_the_tripwire_compares_identical_and_containing_texts_only():
    assert later.tripwire_relations(W, [W]) == {identity.IDENTICAL_REL, identity.CONTAINS}
    assert identity.CONTAINS in later.tripwire_relations("Noted: %s ok" % W, [W])
    assert later.tripwire_relations("forty thousand dollars", [W]) == set()  # part of it: not a hit
    assert later.tripwire_relations("Something else entirely.", [W, "Short"]) == set()
    assert later.tripwire_relations("anything", []) == set()


def test_the_tripwire_tally_round_trips_counts_and_locations_only():
    tw = later.Tripwire(inside=1, outside=2, admin_decided=3, inside_at=[{"session": 1, "message": 2}],
                        outside_at=[{"session": 3, "message": 4}] * 300, texts=2)
    back = later.Tripwire.from_tally(tw.to_tally())
    assert (back.inside, back.outside, back.admin_decided, back.texts) == (1, 2, 3, 2)
    assert len(back.outside_at) == later.TRIPWIRE_LIST_CAP
    assert later.Tripwire.from_tally(None).inside == 0
