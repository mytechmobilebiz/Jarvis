"""0.6.2 option C stabilisation S2 (A3-nib-second): holds of a stored row that the newest backup no longer holds.

RECON-55 (fresh seeds 13450, 13010, 13034, ~107 seeds of R5's re-run): rows of a withheld conversation stored from an
earlier backup B1 that the decision backup B2 no longer holds (JARVIS compacted the conversation, dropped or renumbered
the rows). The withhold compared them on their stored words (B1's content); Settle on B2 then decided every hold of
the decision on B2, the admin's release was decided there, and no import of B2 could bring the text back: the next
import's sweep listed the rows again, 'not in this backup', decided on B2, and the oracle found that evidence on a
backup holding the withheld text. A hold of a row is decided on the newest backup that held that row; the release
covers that content. Synthetic data only."""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.brain import identity, review
from app.brain import importer as imp
from app.brain import source as src
from app.db import session_scope
from app.models import ArchiveMessage, BrainTombstone, BrainWithholdExemption

from .. import brain_jarvis as bj
from ..conftest import ADMIN
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_hold_r2_062 import row_id
from .test_brain_later_062 import holds, state
from .test_brain_withhold_identity import ALL, DAY, T, conversation, message_of, run, secret, withhold

pytestmark = pytest.mark.usefixtures("clean_db")

STALE = "Sure, here is the plan for the week ahead."


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


async def stone_of(sid: str, row: int) -> BrainTombstone | None:
    async with session_scope() as s:
        m = (await message_of(sid, row))[-1]
        return await s.get(BrainTombstone, m.source_key)


async def release(client, message_id: int) -> None:
    await as_(client, "admin")
    r = await client.post("/api/brain/holds/%d/release" % message_id, json={"confirm": identity.CONFIRM_WORD})
    assert r.status_code == 200, r.text


async def sweep() -> None:
    async with session_scope() as s:
        await review.hold_group_rows(s, actor=ADMIN[0], cause="test")
        await s.commit()


async def stale_setup(client, fx, tmp_path, mark: str):
    """B1 holds the conversation with a fourth row; B2, the decision backup, no longer holds that row (JARVIS dropped
    it). The test copy imported B1, then B2; the admin withholds the first row. (ids, B1's paths, B1's SHA-256, B2's)"""
    db = fx["db"]
    ids = conversation(db, "s2_p", T, [("user", secret(mark)), ("assistant", "Noted."),
                                       ("user", "Anything else to plan for the week?"), ("assistant", STALE)])
    b1 = bj.copy(fx, tmp_path / "b1")
    await run(b1)
    bj.change(db, "DELETE FROM messages WHERE id = ?", ids[3])
    conversation(db, "s2_o", T + DAY, [("user", "Book the train."), ("assistant", "Booked.")])
    await run(fx)
    await withhold(client, await row_id("s2_p", ids[0]))
    return ids, b1, src.sha256_file(b1["db"]), src.sha256_file(db)



async def test_s2_settle_keeps_a_hold_of_a_row_the_backup_no_longer_holds_on_the_backup_that_held_it(client, fx,
                                                                                                      tmp_path):
    """RECON-55 (13450): Settle on B2 decides the hold of the row B2 no longer holds on B1, the backup its compared
    content came from (before: on B2). The admin releases it; the next import of B2 cannot bring its text back and
    lists it again, decided on B1 (before: on B2, 'not in this backup', for good); an import of B1, which holds the
    content the release covers, brings it back."""
    mark = "zqs2st55"
    ids, b1, b1_sha, b2_sha = await stale_setup(client, fx, tmp_path, mark)
    st = await stone_of("s2_p", ids[3])
    assert st is not None and st.review == identity.PENDING and st.via == identity.GROUP_HOLD
    assert st.decided_on == b1_sha
    await bj.settle(fx, keep=False)
    st = await stone_of("s2_p", ids[3])
    assert st is not None and st.review == identity.PENDING and st.decided_on == b1_sha  # before: b2_sha
    stale = await row_id("s2_p", ids[3])
    await release(client, stale)
    async with session_scope() as s:
        [e] = (await s.scalars(select(BrainWithholdExemption).where(
            BrainWithholdExemption.source_row_id == str(ids[3])))).all()
        assert e.decided_on == b1_sha  # the release covers B1's content, the one compared
    await run(fx)  # the documented next import (B2): B2 does not hold the row
    [still] = await message_of("s2_p", ids[3])
    assert still.withheld and still.content == ""
    st = await stone_of("s2_p", ids[3])
    assert st is not None and st.review == identity.PENDING and st.decided_on == b1_sha  # before: b2_sha
    assert stale in await holds(client)
    await imp.run(bj.paths(b1), ALL)  # B1 holds the released content
    [back] = await message_of("s2_p", ids[3])
    assert not back.withheld and back.content == STALE  # before: withheld, the release on B2 covering nothing
    assert await state("s2_p", ids[0]) == "certain"
    assert await bj.nowhere(mark) == []


async def test_s2_a_second_withhold_s_settle_never_moves_the_hold_onto_a_backup_without_the_row(client, fx, tmp_path):
    """RECON-55 (13450, 13010, 13034: the second-withhold arm): after the review and the next import, a second
    message of the same conversation is withheld and Settle runs on B2 again: the row B2 does not hold stays held,
    decided on B1, the backup its content came from (before: on B2, with 'not in this backup' while B2 holds the
    first withheld text); no hold of it is decided on B2."""
    mark = "zqs2sw55"
    ids, b1, b1_sha, b2_sha = await stale_setup(client, fx, tmp_path, mark)
    await bj.settle(fx, keep=False)
    await release(client, await row_id("s2_p", ids[3]))
    await release(client, await row_id("s2_p", ids[2]))
    await run(fx)
    assert await state("s2_p", ids[2]) == "readable"
    await withhold(client, await row_id("s2_p", ids[2]))
    await bj.settle(fx, keep=False)
    async with session_scope() as s:
        m = (await message_of("s2_p", ids[3]))[-1]
        found = (await s.scalars(select(BrainTombstone).where(
            BrainTombstone.source_session_id == "s2_p", BrainTombstone.source_row_id == str(ids[3])))).all()
    assert m.withheld and m.content == ""
    assert found and all(st.decided_on == b1_sha for st in found), [(st.via, st.decided_on) for st in found]
    assert all(st.review == identity.PENDING for st in found)
    assert await bj.nowhere(mark) == []


async def test_s2_a_listed_row_with_no_text_is_decided_on_the_backup_that_held_it(client, fx, tmp_path):
    """RECON-55 (list_unlisted): a row of a group conversation stored without its text and without any stone (older
    code, a rollback, a restore) is listed as a hold decided on the newest backup that held the row (before: on no
    backup, so a release of it took the conversation's newest backup B2, which does not hold the row); released, an
    import of B1 brings the content it covers back."""
    mark = "zqs2lu55"
    ids, b1, b1_sha, b2_sha = await stale_setup(client, fx, tmp_path, mark)
    async with session_scope() as s:  # its hold lost (older code): withheld with no stone
        m = (await message_of("s2_p", ids[3]))[-1]
        await s.execute(delete(BrainTombstone).where(BrainTombstone.source_key == m.source_key))
        await s.commit()
    await sweep()
    st = await stone_of("s2_p", ids[3])
    assert st is not None and st.review == identity.PENDING and st.relations == [identity.NOT_IN_BACKUP]
    assert st.decided_on == b1_sha  # before: ''
    await release(client, await row_id("s2_p", ids[3]))
    async with session_scope() as s:
        [e] = (await s.scalars(select(BrainWithholdExemption).where(
            BrainWithholdExemption.source_row_id == str(ids[3])))).all()
        assert e.decided_on == b1_sha  # before: b2_sha (the conversation's newest backup)
    await imp.run(bj.paths(b1), ALL)
    [back] = await message_of("s2_p", ids[3])
    assert not back.withheld and back.content == STALE
    assert await bj.nowhere(mark) == []


async def test_s2_a_row_the_backup_holds_is_still_decided_on_that_backup(client, fx, tmp_path):
    """Control (RECON-55 changes only rows the backup does not hold): a row B2 still holds is decided on B2 by Settle,
    released there, and the next import of B2 brings its text back."""
    mark = "zqs2ct55"
    ids, b1, b1_sha, b2_sha = await stale_setup(client, fx, tmp_path, mark)
    await bj.settle(fx, keep=False)
    st = await stone_of("s2_p", ids[1])
    assert st is not None and st.review == identity.PENDING and st.decided_on == b2_sha
    await release(client, await row_id("s2_p", ids[1]))
    await run(fx)
    assert await state("s2_p", ids[1]) == "readable"
    async with session_scope() as s:
        assert await s.scalar(select(ArchiveMessage.content).where(
            ArchiveMessage.id == await row_id("s2_p", ids[1]))) == "Noted."
    assert await bj.nowhere(mark) == []
