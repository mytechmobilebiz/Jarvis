"""0.6.2 step 4: finishing decisions ("Settle", RECOMMENDATION.md plan 4.1-4.7). Synthetic data only.

Settle (brain-import settle --backup FILE, jarvis-import -Mode Settle) reads the conversation family of each unfinished
withhold in the JARVIS backup, with the withheld text in memory, and decides it once: certain only by the rules of 4.2,
every other related row held for review with computed evidence (4.3, 4.6), the result decided on that backup and the
backup settled (4.4). The withhold dialog only lists and blanks at once (4.5); the export waits for Settle and for the
review of what it held (4.7, owner question 5 at its default). An import of the settled backup replays it (C1).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.brain import identity
from app.brain import importer as imp
from app.db import session_scope
from app.models import ArchiveMessage, BrainDecisionBackup

from .. import brain_jarvis as bj
from ..conftest import ADMIN
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_withhold_identity import DAY, T, conversation, message_of, run, secret, stones, withhold

pytestmark = pytest.mark.usefixtures("clean_db")


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


async def holds(client) -> dict[int, dict]:
    await as_(client, "admin")
    r = await client.get("/api/brain/holds")
    assert r.status_code == 200, r.text
    return {h["message"]: h for h in r.json()["holds"]}


async def pending() -> dict:
    async with session_scope() as s:
        from sqlalchemy import text

        return dict((await s.execute(text("SELECT brain_withhold_pending()"))).scalar())


async def backups() -> dict[str, int | None]:
    async with session_scope() as s:
        return dict((await s.execute(select(BrainDecisionBackup.sha256, BrainDecisionBackup.settled_run_id))).all())


async def states() -> dict[str, tuple]:
    async with session_scope() as s:
        return {m.source_key: (m.withheld, m.content) for m in (await s.scalars(select(ArchiveMessage))).all()}


async def test_export_waits_for_settle_and_a_rollback_of_settle_unsettles(client, fx, tmp_path):
    """Plan 4.4, 4.7: the decision is unfinished until Settle runs on its backup (the export refuses and says how to
    finish it); Settle decides it on that backup and settles it (settled_run_id); a rollback of the Settle run makes it
    unfinished again."""
    mark = "zqsettle1"
    ids = conversation(fx["db"], "st1", T, [("user", "What is the deal worth?"), ("assistant", secret(mark)),
                                            ("user", "Thanks.")])
    await run(fx)
    await withhold(client, (await message_of("st1", ids[1]))[0].id)
    out = str(tmp_path / "decisions.json")
    with pytest.raises(imp.Refused, match="brain-import settle --backup"):
        await imp.decisions_export(out)
    assert (await pending())["unsettled_decisions"] == 1
    done = await bj.settle(fx, keep=False)
    assert done["decisions"] == 1 and done["run_id"]
    assert list((await backups()).values()) == [done["run_id"]]
    assert (await pending())["unsettled_decisions"] == 0
    assert (await imp.decisions_export(out))["removals"] == 1
    again = await bj.settle(fx, keep=False)  # nothing left to finish: no run, no change
    assert (again["run_id"], again["status"]) == (None, "nothing to finish")
    await imp.rollback(done["run_id"], actor=ADMIN[0], confirm=True)
    assert list((await backups()).values()) == [None]
    with pytest.raises(imp.Refused, match="not finished yet"):
        await imp.decisions_export(str(tmp_path / "again.json"))


async def test_settle_holds_related_rows_with_computed_evidence_and_the_export_waits_for_their_review(
        client, fx, tmp_path):
    """Plan 4.3, 4.6, 4.7: rows that contain the withheld text, quote part of it through JARVIS's reply pointer (N1)
    or restate it after the replay header are held with that computed relation (the wrappers peeled); a row with no
    relation is not touched. Releasing a hold that holds the text needs the typed word; the export waits until every
    hold Settle found is kept or released."""
    mark = "zqsettle2"
    w = secret(mark)
    part = w[-40:]  # the words after the marker
    ids = conversation(fx["db"], "st2", T, [
        ("user", "What is the deal worth?"), ("assistant", w),
        ("user", "Noted: %s Please confirm." % w),                         # contains
        ("user", '[Replying to: "%s"]\n\nThanks, that helps.' % part),     # part of (reply quote, N1)
        ("user", "%s\n%s" % (identity.REPLAY_HEADER, w)),                   # restated
        ("assistant", "The weather is fine today.")])                       # no relation
    await run(fx)
    await as_(client, "admin")
    r = await client.post("/api/brain/messages/%d/withhold" % (await message_of("st2", ids[1]))[0].id,
                          json={"include_identical": False})  # the box cleared: nothing ticked at withhold time
    assert r.status_code == 200, r.text
    for i in (2, 3, 4):  # left readable by the admin's choice in the dialog: Settle never overrides it
        assert not (await message_of("st2", ids[i]))[0].withheld
    # The same conversation withheld again with the rows ticked (the default): Settle computes their evidence.
    mark3 = "zqsettle3"
    w3 = secret(mark3)
    ids3 = conversation(fx["db"], "st3", T + DAY, [
        ("user", "And the other deal?"), ("assistant", w3), ("user", "Noted: %s Please confirm." % w3),
        ("user", '[Replying to: "%s"]\n\nThanks, that helps.' % w3[-40:]), ("user", "%s\n%s" % (
            identity.REPLAY_HEADER, w3)), ("assistant", "The weather is fine today, again.")])
    await run(fx)
    await withhold(client, (await message_of("st3", ids3[1]))[0].id)
    await bj.settle(fx, keep=False)
    for i in (2, 3, 4):  # the first decision: the admin's 'left readable' choices stand (plan 4.5)
        assert not (await message_of("st2", ids[i]))[0].withheld
    found = await holds(client)
    rows = {i: (await message_of("st3", ids3[i]))[0] for i in range(2, 6)}
    assert identity.CONTAINS in found[rows[2].id]["relations"]
    assert found[rows[3].id]["relations"] == [identity.PART_OF]
    assert identity.RESTATED_REL in found[rows[4].id]["relations"]
    assert rows[5].id not in found and not rows[5].withheld
    for i in (2, 3, 4):
        h = found[rows[i].id]
        assert (h["rule"], h["confirm_required"], h["decided_on"] != "") == (identity.SETTLE_HOLD, True, True), h
        assert mark3 not in str(h)
    assert (await pending())["unreviewed_settle_holds"] == 3
    with pytest.raises(imp.Refused, match="wait for review"):
        await imp.decisions_export(str(tmp_path / "decisions.json"))
    r = await client.post("/api/brain/holds/%d/release" % rows[3].id)
    assert r.status_code == 400 and identity.CONFIRM_WORD in r.text
    r = await client.post("/api/brain/holds/%d/release" % rows[3].id, json={"confirm": identity.CONFIRM_WORD})
    assert r.status_code == 200, r.text
    for i in (2, 4):
        assert (await client.post("/api/brain/holds/%d/keep" % rows[i].id)).status_code == 200
    await imp.decisions_export(str(tmp_path / "decisions.json"))
    await run(fx)  # the released quote comes back at the next import; the kept rows stay withheld
    assert not (await message_of("st3", ids3[3]))[0].withheld
    assert [(await message_of("st3", ids3[i]))[0].withheld for i in (2, 4)] == [True, True]
    assert await bj.nowhere(mark3) == []


async def test_settle_copy_position_is_certain_only_for_a_unique_same_time_origin(client, fx):
    """Plan 4.2 (6): a gateway branch (fresh uids, the parent's times) copies the conversation before the withhold.
    The copy at the withheld row's time and text is certain ('copied by JARVIS'); the copy of an identical, other
    message of the parent (another time) is not: it is held, 'identical', and pre-selected only if it looks
    independent. Nothing else of the branch is touched."""
    mark = "zqsettle4"
    w = secret(mark)
    ids = conversation(fx["db"], "st4", T, [("user", "Read it to me."), ("assistant", w), ("user", "Again?"),
                                            ("assistant", w), ("user", "Thanks.")])
    bj.gateway_branch(fx["db"], "st4", "st4_b", started_at=T + DAY)
    await run(fx)
    await withhold(client, (await message_of("st4", ids[1]))[0].id)
    await bj.settle(fx, keep=False)
    child = {r["timestamp"]: r["id"] for r in bj.rows(fx["db"], "st4_b")}
    parent = {r["id"]: r["timestamp"] for r in bj.rows(fx["db"], "st4")}
    [copy_w] = await message_of("st4_b", child[parent[ids[1]]])
    [copy_twin] = await message_of("st4_b", child[parent[ids[3]]])
    st = await stones()
    assert copy_w.withheld and (st[copy_w.source_key].via, st[copy_w.source_key].review) == (
        "copied by JARVIS", identity.CERTAIN)
    assert copy_twin.withheld and st[copy_twin.source_key].review == identity.PENDING
    found = await holds(client)
    assert identity.IDENTICAL_REL in found[copy_twin.id]["relations"]
    [twin] = await message_of("st4", ids[3])
    assert twin.withheld and st[twin.source_key].review == identity.PENDING  # identical, another message: held
    for i in (0, 2, 4):
        assert not (await message_of("st4_b", child[parent[ids[i]]]))[0].withheld


async def test_the_settled_backup_is_replayed_word_for_word(client, fx):
    """C1 (plan 3.2, 4.4): an import of the very backup Settle finished changes nothing: every row keeps its state
    and every stone its review and evidence."""
    mark = "zqsettle5"
    ids = conversation(fx["db"], "st5", T, [("user", "Q?"), ("assistant", secret(mark)),
                                            ("user", "Noted: %s ok." % secret(mark)), ("assistant", "Fine.")])
    bj.gateway_branch(fx["db"], "st5", "st5_b", started_at=T + DAY)
    await run(fx)
    await withhold(client, (await message_of("st5", ids[1]))[0].id)
    await bj.settle(fx, keep=False)
    before = (await states(), {k: (st.via, st.review, st.relations, st.decided_on)
                               for k, st in (await stones()).items()})
    await run(fx)
    after = (await states(), {k: (st.via, st.review, st.relations, st.decided_on)
                              for k, st in (await stones()).items()})
    assert after == before
    assert await bj.nowhere(mark) == []


async def test_settle_runs_on_the_newest_backup_of_the_family_only(client, fx, tmp_path):
    """Plan 4.1: a decision is finished on the newest backup that wrote rows of its family; a Settle of an older copy
    of the file refuses before any change and names what waits elsewhere."""
    mark = "zqsettle6"
    ids = conversation(fx["db"], "st6", T, [("user", "Q?"), ("assistant", secret(mark))])
    older = bj.copy(fx, tmp_path / "older")
    await run(fx)
    await withhold(client, (await message_of("st6", ids[1]))[0].id)
    bj.add_row(fx["db"], "st6", "user", "One more turn.", T + DAY)
    await run(fx)  # a later backup of the family
    with pytest.raises(imp.Refused, match="wait on another one"):
        await bj.settle(older, keep=False)
    assert all(v is None for v in (await backups()).values())
    done = await bj.settle(fx, keep=False)
    assert done["decisions"] == 1
