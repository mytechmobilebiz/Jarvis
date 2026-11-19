"""0.6.2 option C (rebuild R4): releases and hold states (D3, B6, N-A, N-B, 6725, 13465, carried_via, _w_text).

- D3: a hold's 'none' needs every withheld text in hand; a later withhold makes every 'none' of other withholds stale
  at once ('later_withhold', mark_later_withhold), the decisions they belong to are finished again (reopen: the export
  waits), Settle with every text in hand computes them again, and a Settle without the later text keeps them stale.
- One click releases only exactly 'none' against every withheld text there is now, a plain user or assistant text
  (one_click_eligible, eligible_now); a per-row release of anything else needs the typed word on the server (B6:
  every untyped body gets 400 or 422, the row stays held).
- N-B: a row released for one withheld text that carries another is held again for it.
- 6725 / F1: a release's trust comes only from a release that covers the stored content (typed, checked or left
  readable); a check of one backup's content marks only that backup's release.
Synthetic data only; these tests show the listed shapes on these inputs, not that no leak remains."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text, update

from app.brain import identity, review
from app.brain import importer as imp
from app.db import session_scope
from app.models import (ArchiveMessage, ArchiveSession, BrainDecisionBackup, BrainTombstone,
                        BrainWithholdExemption)

from .. import brain_jarvis as bj
from ..conftest import ADMIN
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_later_062 import holds, state
from .test_brain_withhold_identity import DAY, T, conversation, message_of, run, secret, withhold

pytestmark = pytest.mark.usefixtures("clean_db")

TYPED = {"confirm": identity.CONFIRM_WORD}


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


async def archive_id(sid: str) -> int:
    async with session_scope() as s:
        return int(await s.scalar(select(ArchiveSession.id).where(ArchiveSession.source_session_id == sid)))


async def row_id(sid: str, source_row: int) -> int:
    return (await message_of(sid, source_row))[-1].id


async def stone_of(sid: str, source_row: int) -> BrainTombstone:
    async with session_scope() as s:
        m = (await message_of(sid, source_row))[-1]
        st = await s.get(BrainTombstone, m.source_key)
        assert st is not None
        return st


async def one_click(client, sid: str) -> list[int]:
    await as_(client, "admin")
    r = await client.post("/api/brain/sessions/%d/holds/release-unrelated" % await archive_id(sid))
    assert r.status_code == 200, r.text
    return r.json()["released"]


async def unsettled() -> set[str]:
    async with session_scope() as s:
        return set((await s.scalars(select(BrainDecisionBackup.sha256).where(
            BrainDecisionBackup.settled_run_id.is_(None)))).all())


async def export_refused(tmp_path) -> bool:
    try:
        await imp.decisions_export(str(tmp_path / "out.json"), actor=ADMIN[0])
    except imp.Refused:
        return True
    return False


def two_deals(db: str) -> tuple[list[int], str]:
    """P: an unrelated question and the first withheld text; its later sibling Q (another conversation) is added later."""
    w1 = secret("zqrel1")
    return conversation(db, "rl_p", T, [("user", "What is the deal worth?"), ("assistant", w1)]), w1


def add_q(db: str, w2: str) -> list[int]:
    return conversation(db, "rl_q", T + 5 * DAY, [("user", "And the other deal?"), ("assistant", w2)])


async def test_rel3_a_later_withhold_makes_none_stale_at_once_and_the_export_waits(client, fx, tmp_path):
    """X ('none' for W1, settled) says 'later_withhold' the moment W2 is withheld: never one click, an untyped
    per-row release is refused, and the earlier decision is finished again (its backup reopened: the export waits)."""
    db = fx["db"]
    pa, _ = two_deals(db)
    await run(fx)
    await withhold(client, await row_id("rl_p", pa[1]))
    await bj.settle(fx, keep=False)
    x = await row_id("rl_p", pa[0])
    assert (await holds(client))[x]["relations"] == [identity.NO_RELATION]
    first_backup = (await stone_of("rl_p", pa[0])).decided_on
    assert first_backup and first_backup not in await unsettled()
    qa = add_q(db, secret("zqrel2"))
    await run(fx)
    await withhold(client, await row_id("rl_q", qa[1]))
    h = (await holds(client))[x]
    assert h["relations"] == [identity.LATER_WITHHOLD] and h["later_withhold"]
    assert not h["one_click"] and h["confirm_required"]
    assert await one_click(client, "rl_p") == []
    assert (await client.post("/api/brain/holds/%d/release" % x)).status_code == 400
    assert await state("rl_p", pa[0]) == "held"
    assert first_backup in await unsettled()
    assert await export_refused(tmp_path)


async def test_rel3_settle_of_a_newer_backup_finishes_the_earlier_decision_again(client, fx):
    """After the later withhold, Settle of the newest backup (it holds both withheld texts) finishes the earlier
    decision again: X is 'none' against both, and one click releases it."""
    db = fx["db"]
    pa, _ = two_deals(db)
    await run(fx)
    await withhold(client, await row_id("rl_p", pa[1]))
    await bj.settle(fx, keep=False)
    qa = add_q(db, secret("zqrel3"))
    await run(fx)
    await withhold(client, await row_id("rl_q", qa[1]))
    await bj.settle(fx, keep=False)
    x = await row_id("rl_p", pa[0])
    h = (await holds(client))[x]
    assert h["relations"] == [identity.NO_RELATION] and h["one_click"], h
    st = await stone_of("rl_p", pa[0])
    assert set(st.compared) >= set(await _anchors())
    assert x in await one_click(client, "rl_p")


async def _anchors() -> list[str]:
    async with session_scope() as s:
        return await identity.admin_anchor_keys(s)


async def test_rel3_a_settle_without_the_later_text_keeps_the_evidence_stale(client, fx):
    """The later withheld text's conversation is gone from the newest backup: Settle cannot compare X with it, so X
    keeps 'later_withhold' (keep_later), never 'none'."""
    db = fx["db"]
    pa, _ = two_deals(db)
    await run(fx)
    await withhold(client, await row_id("rl_p", pa[1]))
    await bj.settle(fx, keep=False)
    qa = add_q(db, secret("zqrel4"))
    await run(fx)
    await withhold(client, await row_id("rl_q", qa[1]))
    bj.change(db, "DELETE FROM messages WHERE session_id = ?", "rl_q")
    bj.change(db, "DELETE FROM sessions WHERE id = ?", "rl_q")
    bj.change(db, "UPDATE sessions SET title = 'deals' WHERE id = ?", "rl_p")  # a new backup of P's family
    await run(fx)
    await bj.settle(fx, keep=False)
    x = await row_id("rl_p", pa[0])
    h = (await holds(client))[x]
    assert identity.NO_RELATION not in h["relations"] and identity.LATER_WITHHOLD in h["relations"], h
    assert not h["one_click"] and await one_click(client, "rl_p") == []


async def test_rel3_settle_with_every_text_in_hand_computes_it_again(client, fx):
    """Control of the above: with every withheld text in the backup Settle says 'none' again."""
    db = fx["db"]
    pa, _ = two_deals(db)
    await run(fx)
    await withhold(client, await row_id("rl_p", pa[1]))
    qa = add_q(db, secret("zqrel5"))
    await run(fx)
    await withhold(client, await row_id("rl_q", qa[1]))
    assert (await holds(client))[await row_id("rl_p", pa[0])]["relations"] == [identity.LATER_WITHHOLD]
    await bj.settle(fx, keep=False)
    assert (await holds(client))[await row_id("rl_p", pa[0])]["relations"] == [identity.NO_RELATION]


async def test_rel3_13465_a_later_row_identical_to_a_withheld_text_not_in_hand_is_never_none(client, fx):
    """W2's conversation is gone from the later backup, which brings a new row in W1's conversation saying W2's very
    words: it is held, and never 'none' (W2 was not in hand to compare it with); one click never frees it."""
    db = fx["db"]
    pa, _ = two_deals(db)
    w2 = secret("zqrel6")
    qa = add_q(db, w2)
    await run(fx)
    await withhold(client, await row_id("rl_p", pa[1]))
    await withhold(client, await row_id("rl_q", qa[1]))
    bj.change(db, "DELETE FROM messages WHERE session_id = ?", "rl_q")
    bj.change(db, "DELETE FROM sessions WHERE id = ?", "rl_q")
    new = bj.add_row(db, "rl_p", "user", w2, T + 9 * DAY)
    await run(fx)
    assert await state("rl_p", new) == "held"
    h = (await holds(client))[await row_id("rl_p", new)]
    assert identity.NO_RELATION not in h["relations"] and not h["one_click"], h
    assert await row_id("rl_p", new) not in await one_click(client, "rl_p")
    await run(fx)
    assert await state("rl_p", new) == "held"


async def test_skC_a_row_released_for_w1_that_carries_w2_is_held_when_w2_is_withheld(client, fx):
    """N-B: X in W1's conversation repeats W2 (still readable elsewhere); released in one click for W1 while W2 was
    not withheld, X is readable; withholding W2 holds X again (for W2), and the next import keeps it held."""
    db = fx["db"]
    w2 = secret("zqrel7")
    pa = conversation(db, "rl_p", T, [("user", "Forward this: %s" % w2), ("assistant", secret("zqrel8"))])
    qa = add_q(db, w2)
    await run(fx)
    await withhold(client, await row_id("rl_p", pa[1]))
    x = await row_id("rl_p", pa[0])
    assert (await holds(client))[x]["relations"] == [identity.NO_RELATION]
    assert x in await one_click(client, "rl_p")
    await run(fx)
    assert await state("rl_p", pa[0]) == "readable"
    await withhold(client, await row_id("rl_q", qa[1]))
    assert await state("rl_p", pa[0]) == "held"
    await run(fx)
    assert await state("rl_p", pa[0]) == "held"


async def test_rel3_typed_release_after_a_later_withhold_is_held_again_by_the_next_import(client, fx):
    """X (held with W1's conversation, 'none' then) carries W2: after the later withhold it says 'later_withhold'; an
    admin's typed release of X under W1's group never frees it from W2: the next import holds it again (for W2)."""
    db = fx["db"]
    w2 = secret("zqrel9")
    pa = conversation(db, "rl_p", T, [("user", "Forward this: %s" % w2), ("assistant", secret("zqrela"))])
    qa = add_q(db, w2)
    await run(fx)
    await withhold(client, await row_id("rl_p", pa[1]))
    await withhold(client, await row_id("rl_q", qa[1]))
    x = await row_id("rl_p", pa[0])
    assert (await holds(client))[x]["relations"] == [identity.LATER_WITHHOLD]
    r = await client.post("/api/brain/holds/%d/release" % x, json=TYPED)
    assert r.status_code == 200, r.text
    await run(fx)
    assert await state("rl_p", pa[0]) == "held"


# --- B6: the typed word, checked on the server -------------------------------------------------------------------

UNTYPED = {
    "empty": {}, "null": {"confirm": None}, "confirmed": {"confirmed": True}, "checked": {"checked": True},
    "trusted": {"trusted": True}, "lowercase": {"confirm": "release"}, "zero_width": {"confirm": "RE​LEASE"},
    "fullwidth": {"confirm": "ＲＥＬＥＡＳＥ"}, "a_list": {"confirm": ["RELEASE"]},
    "two_lines": {"confirm": "RELEASE\nRELEASE"}, "kind": {"kind": "left_readable", "confirm": "yes"},
}


async def _not_eligible_hold(client, fx) -> tuple[int, list[int]]:
    """A hold one click could not release: 'other_not_compared' (a second withhold, the first text not in hand)."""
    db = fx["db"]
    pa = conversation(db, "b6_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqb61"))])
    qa = conversation(db, "b6_q", T + DAY, [("user", "And the other deal?"), ("assistant", secret("zqb62"))])
    await run(fx)
    await withhold(client, await row_id("b6_p", pa[1]))
    await withhold(client, await row_id("b6_q", qa[1]))
    x = await row_id("b6_q", qa[0])
    h = (await holds(client))[x]
    assert h["relations"] == [identity.OTHER_NOT_COMPARED] and not h["one_click"] and h["confirm_required"]
    return x, qa


@pytest.mark.parametrize("body", sorted(UNTYPED))
async def test_b6_every_untyped_body_gets_400_or_422_and_the_row_stays_held(client, fx, body):
    x, qa = await _not_eligible_hold(client, fx)
    r = await client.post("/api/brain/holds/%d/release" % x, json=UNTYPED[body])
    assert r.status_code in (400, 422), (body, r.status_code, r.text)
    assert await state("b6_q", qa[0]) == "held"
    async with session_scope() as s:
        assert not (await s.scalars(select(BrainWithholdExemption))).all()


async def test_b6_the_typed_word_with_extra_fields_releases_it_and_the_release_is_trusted(client, fx):
    x, qa = await _not_eligible_hold(client, fx)
    r = await client.post("/api/brain/holds/%d/release" % x, json={**TYPED, "trusted": False, "kind": "x"})
    assert r.status_code == 200, r.text
    async with session_scope() as s:
        found = (await s.scalars(select(BrainWithholdExemption))).all()
    assert len(found) == 1 and found[0].trusted and found[0].kind == "release"


async def test_b6_an_eligible_none_hold_needs_no_word_and_its_release_is_checked(client, fx):
    """Control: exactly 'none' against every withheld text, a plain user text: the per-row release needs no word
    (one click could release it), and the release is trusted as checked."""
    db = fx["db"]
    pa = conversation(db, "b6_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqb63"))])
    await run(fx)
    await withhold(client, await row_id("b6_p", pa[1]))
    x = await row_id("b6_p", pa[0])
    assert (await holds(client))[x]["one_click"]
    assert (await client.post("/api/brain/holds/%d/release" % x)).status_code == 200
    async with session_scope() as s:
        assert [e.trusted for e in (await s.scalars(select(BrainWithholdExemption))).all()] == [True]


# --- 6725 / F1: trust from the release that covers the stored content -------------------------------------------

async def _released_row(client, fx) -> tuple[ArchiveMessage, str, str, str]:
    """W withheld; X (an unrelated question of its conversation) released in one click and readable again. Returns
    (X, its JARVIS session, the group's anchor, the backup X's content came from)."""
    db = fx["db"]
    pa = conversation(db, "tr_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqtr1"))])
    await run(fx)
    w = (await message_of("tr_p", pa[1]))[-1]
    await withhold(client, w.id)
    assert await row_id("tr_p", pa[0]) in await one_click(client, "tr_p")
    await run(fx)
    async with session_scope() as s:
        x = (await message_of("tr_p", pa[0]))[-1]
        assert not x.withheld
        return x, "tr_p", w.source_key, await identity.stored_backup(s, x)


async def _set_exemptions(anchor: str, x: ArchiveMessage, sid: str, decisions: list[tuple[str, bool]]) -> None:
    async with session_scope() as s:
        await s.execute(text("DELETE FROM brain_withhold_exemptions"))
        here = identity.exact_of_row(x, sid)
        for on, trusted in decisions:
            await identity.put_exemption(s, here, anchor=anchor, kind="release", decided_on=on, trusted=trusted)
        await s.commit()


OTHER = "f" * 64


async def test_skc2_exempt_covers_never_takes_trust_from_another_backups_release(client, fx):
    """A typed (trusted) release decided on another backup, and an unchecked one decided on the backup X's content
    came from: exempt_covers is False (the covering release is not trusted; the trusted one does not cover it)."""
    x, sid, anchor, sha = await _released_row(client, fx)
    await _set_exemptions(anchor, x, sid, [(OTHER, True), (sha, False)])
    async with session_scope() as s:
        row = await s.get(ArchiveMessage, x.id)
        assert await identity.exempt_covers(s, anchor, sid, row) is False
        ids = await identity.Identities.load(s)
        assert ids.released(anchor, identity.exact_of_row(row, sid), backup=sha) is False
    await _set_exemptions(anchor, x, sid, [(sha, True)])
    async with session_scope() as s:
        assert await identity.exempt_covers(s, anchor, sid, await s.get(ArchiveMessage, x.id)) is True


@pytest.mark.parametrize("case", ["typed_on_another_backup", "unchecked_alone"])
async def test_skc2_an_unchecked_release_never_stands_on_another_backups_typed_release(client, fx, case):
    """The sweep (no withheld text in hand: nothing can check X) holds X: its only covering release is unchecked."""
    x, sid, anchor, sha = await _released_row(client, fx)
    found = [(sha, False)] + ([(OTHER, True)] if case == "typed_on_another_backup" else [])
    await _set_exemptions(anchor, x, sid, found)
    async with session_scope() as s:
        await review.hold_group_rows(s, actor=ADMIN[0], cause="test")
        await s.commit()
    async with session_scope() as s:
        assert (await s.get(ArchiveMessage, x.id)).withheld


async def test_rel3_6725_a_check_of_one_backups_content_marks_only_that_backups_release(client, fx):
    """The sweep with every withheld text in hand finds no relation of X's content (from backup B) to any of them:
    the unchecked release decided on B is marked trusted, the one decided on another backup is not, and X stays
    readable."""
    x, sid, anchor, sha = await _released_row(client, fx)
    await _set_exemptions(anchor, x, sid, [(sha, False), (OTHER, False)])
    async with session_scope() as s:
        await review.hold_group_rows(s, actor=ADMIN[0], cause="test", texts={anchor: secret("zqtr1")})
        await s.commit()
    async with session_scope() as s:
        marks = {e.decided_on: e.trusted for e in (await s.scalars(select(BrainWithholdExemption))).all()}
        assert not (await s.get(ArchiveMessage, x.id)).withheld
    assert marks == {sha: True, OTHER: False}


async def test_rel3_6725_a_typed_release_never_makes_another_backups_release_trusted(client, fx):
    """An admin's typed release decided on one backup makes that release trusted, never the unchecked release of the
    same row decided on another backup."""
    x, sid, anchor, sha = await _released_row(client, fx)
    await _set_exemptions(anchor, x, sid, [(OTHER, False)])
    async with session_scope() as s:
        here = identity.exact_of_row(await s.get(ArchiveMessage, x.id), sid)
        await identity.put_exemption(s, here, anchor=anchor, decided_on=sha, trusted=True)
        await identity.put_exemption(s, here, anchor=anchor, decided_on=OTHER, trusted=False)
        await s.commit()
    async with session_scope() as s:
        marks = {e.decided_on: e.trusted for e in (await s.scalars(select(BrainWithholdExemption))).all()}
    assert marks == {sha: True, OTHER: False}


async def test_rel3_an_unchecked_file_release_frees_no_hold_and_a_trusted_one_does(client, fx, tmp_path):
    """decisions-apply: a release the file does not say was checked (an older file) frees no hold here; one the
    file says was checked (typed, or 'none' against every text) does."""
    db = fx["db"]
    pa = conversation(db, "fr_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqfr1"))])
    out = await run(fx)
    w = (await message_of("fr_p", pa[1]))[-1]
    await withhold(client, w.id)
    x = (await message_of("fr_p", pa[0]))[-1]
    async with session_scope() as s:
        sha = await identity.stored_backup(s, w)
    for trusted, freed in ((None, False), (True, True)):
        rel = {"anchor": w.source_key, "session": "fr_p", "row": str(pa[0]), "role": "user", "tool_call_id": "",
               "at_row": x.at.isoformat(), "uid": x.source_uid, "kind": "release", "decided_on": sha,
               "reason": "released by an admin", "by": "admin", "at": datetime.now(UTC).isoformat()}
        if trusted is not None:
            rel["trusted"] = trusted
        path = tmp_path / ("d%s.json" % trusted)
        path.write_text(json.dumps({"format": "jarvis-brain-decisions", "version": imp.DECISIONS_VERSION,
                                    "decisions": [], "removals": [],
                                    "releases": [rel]}))
        await imp.decisions_apply(str(path), out["run_id"], actor=ADMIN[0], apply=True)
        out = await run(fx)  # the next import brings a released row's text back
        assert await state("fr_p", pa[0]) == ("readable" if freed else "held"), trusted


# --- N-A through a decisions file or a restore's removals-only file (fail-closed, RECON-31) -----------------------

async def test_rel3_a_decisions_file_bringing_a_withhold_this_database_lacks_makes_none_stale(client, fx):
    """A decisions file brings an admin's withhold this database does not have yet (its row is not stored here):
    X's 'none' was never compared with it, so it says 'later_withhold' at once, and the earlier decision's backup is
    finished again."""
    db = fx["db"]
    pa, _ = two_deals(db)
    await run(fx)
    await withhold(client, await row_id("rl_p", pa[1]))
    await bj.settle(fx, keep=False)
    x = await row_id("rl_p", pa[0])
    first = (await stone_of("rl_p", pa[0])).decided_on
    data = {"version": imp.DECISIONS_VERSION, "decisions": [], "releases": [], "removals": [{
        "source_key": "jarvis:message:rl_gone:2", "reason": identity.WITHHELD_BY_ADMIN, "anchor": "",
        "session": "rl_gone", "row": "2", "role": "assistant", "tool_call_id": "", "uid": "", "uid_known": False,
        "via": "", "review": "", "title_hidden": False, "by": "admin", "at": datetime.now(UTC).isoformat(),
        "at_row": None, "decided_on": "e" * 64, "kept_by": "", "kept_at": "", "relations": []}]}
    async with session_scope() as s:
        await imp.decisions_before_import(s, data, imp._Decider(ADMIN[0], True))
        await s.commit()
    h = (await holds(client))[x]
    assert h["relations"] == [identity.LATER_WITHHOLD] and not h["one_click"]
    assert first in await unsettled()
    assert (await client.post("/api/brain/holds/%d/release" % x)).status_code == 400


# --- carried_via (12150) and the withheld text of a decision (13582 / 13590) -------------------------------------

async def test_a_kept_hold_keeps_the_rule_that_held_it_through_a_restore(client, fx, tmp_path):
    """held_before_the_point: the restored database has the hold pending; the removals-only file says the admin kept
    it: it is kept with the rule that held it (never the restore's own word)."""
    db = fx["db"]
    pa, _ = two_deals(db)
    await run(fx)
    await withhold(client, await row_id("rl_p", pa[1]))
    x = await row_id("rl_p", pa[0])
    assert (await client.post("/api/brain/holds/%d/keep" % x)).status_code == 200
    removals = tmp_path / "removals.json"
    await imp.decisions_export(str(removals), actor=ADMIN[0], removals_only=True)
    key = (await stone_of("rl_p", pa[0])).source_key
    async with session_scope() as s:  # the restore point: the hold still pending
        await s.execute(update(BrainTombstone).where(BrainTombstone.source_key == key).values(
            review=identity.PENDING, kept_by="", kept_at=None))
        await s.commit()
    await imp.decisions_apply(str(removals), None, actor=None, apply=True, removals_only=True)
    st = await stone_of("rl_p", pa[0])
    assert st.review == identity.KEPT and st.via == identity.GROUP_HOLD, (st.review, st.via)


async def test_seeds_r3_a_same_message_id_member_is_never_taken_as_the_withheld_text(client, fx):
    """13582 / 13590: W's own row is gone from the later backup; a member of its group by the same message id holds
    only a piece of W there. The piece is never W's text: a new row saying that piece is not 'identical' (W is not in
    hand: never 'none' either)."""
    db = fx["db"]
    w = secret("zqsm1")
    pa = conversation(db, "sm_p", T, [("user", "What is the deal worth?"), ("assistant", w)])
    uid = next(r["message_uid"] for r in bj.rows(db, "sm_p") if r["id"] == pa[1])
    bj.add_session(db, "sm_c", started=T + DAY)
    c = bj.add_row(db, "sm_c", "assistant", w, T + DAY, uid=uid)
    await run(fx)
    await withhold(client, await row_id("sm_p", pa[1]))
    st = await stone_of("sm_c", c)
    assert st.review != identity.PENDING and st.via == "same message id", (st.review, st.via)
    piece = w[:30]
    bj.change(db, "DELETE FROM messages WHERE session_id = ? AND id = ?", "sm_p", pa[1])
    bj.change(db, "UPDATE messages SET content = ? WHERE session_id = ? AND id = ?", piece, "sm_c", c)
    new = bj.add_row(db, "sm_p", "user", piece, T + 2 * DAY)
    await run(fx)
    h = (await holds(client))[await row_id("sm_p", new)]
    assert identity.IDENTICAL_REL not in h["relations"] and identity.NO_RELATION not in h["relations"], h
