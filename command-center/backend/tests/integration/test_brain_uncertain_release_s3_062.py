"""0.6.2 option C stabilisation S3: the refused export behind the B1 / B5 cascades of fresh seeds 13075, 13159, 13355
(after a complete review the export was refused, live imported with no decision at all, and B1, B5, A1, B2 followed).

- RECON-59: a row of an uncertain conversation the admin released one by one with the typed word waits for its text
  while no import run since the release held the row (JARVIS dropped it, compacted it away or renumbered it); it is
  not listed again as an uncertain hold at every import (the admin was asked again for a row they decided, with
  nothing new to compare, and the export waited for good). It stays withheld; a backup holding the row again with
  other content lists it again (control).
- RECON-60: a pending hold of another group on a row the uncertain group's admin released is that other group's hold:
  it travels in the decisions file as a hold and does not keep the export waiting for the uncertain group; the
  uncertain group's own pending hold still does (control).
Synthetic data only."""

from __future__ import annotations

import json

import pytest

from app.brain import groups, identity, review
from app.brain import importer as imp
from app.db import session_scope
from app.models import ArchiveMessage, BrainTombstone

from .. import brain_jarvis as bj
from ..conftest import ADMIN
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_hold_r2_062 import join, row_id
from .test_brain_later_062 import holds, pending, state
from .test_brain_withhold_identity import DAY, T, conversation, message_of, run, secret, withhold

pytestmark = pytest.mark.usefixtures("clean_db")


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


async def keep(message_id: int) -> None:
    async with session_scope() as s:
        await review.keep_hold(s, await s.get(ArchiveMessage, message_id), actor=ADMIN[0])
        await s.commit()


async def sweep() -> None:
    async with session_scope() as s:
        await review.hold_group_rows(s, actor=ADMIN[0], cause="test")
        await s.commit()


async def keep_own_rows(sid: str, ids: list[int], skip: int) -> None:
    for i in ids:
        if i != skip and await state(sid, i) == "held":
            await keep(await row_id(sid, i))


async def test_s3_a_released_uncertain_row_no_backup_holds_again_is_not_listed_again_and_the_export_goes(
        client, fx, tmp_path):
    db = fx["db"]
    mark = "zqs3ur59"
    pa = conversation(db, "s3_w", T, [("user", "What is the deal worth?"), ("assistant", secret(mark))])
    ua = conversation(db, "s3_u", T + DAY, [("user", "Draft the follow-up mail."), ("assistant", "Drafted.")])
    await run(fx)
    w = (await message_of("s3_w", pa[1]))[0]
    await withhold(client, w.id)
    await join("s3_u", w.source_key, state_=groups.UNCERTAIN)
    await sweep()
    await bj.settle(fx, keep=False)
    u = [await row_id("s3_u", r) for r in ua]
    for i in u:
        await release(client, i)
    await keep_own_rows("s3_w", pa, pa[1])
    assert (await pending())["uncertain_waiting"] == 0
    # JARVIS drops the conversation's rows (compacted away): no backup since the release holds them
    bj.change(db, "DELETE FROM messages WHERE session_id = ?", "s3_u")
    await run(fx)
    await run(fx)
    for r in ua:
        [m] = await message_of("s3_u", r)
        assert m.withheld and m.content == ""  # still withheld, waiting for its text
        assert await stone_of("s3_u", r) is None  # not listed again as an uncertain hold
    listed = await holds(client)
    assert not set(u) & set(listed)
    assert (await pending())["uncertain_waiting"] == 0
    out = tmp_path / "decisions.json"
    await imp.decisions_export(str(out))
    data = json.loads(out.read_text())
    assert mark not in out.read_text()
    assert {(r["session"], r["row"]) for r in data["releases"]} >= {("s3_u", str(r)) for r in ua}
    assert await bj.nowhere(mark) == []


async def test_s3_control_a_released_uncertain_row_a_backup_holds_with_other_text_is_listed_again(client, fx,
                                                                                                    tmp_path):
    db = fx["db"]
    mark = "zqs3uc59"
    pa = conversation(db, "s3_w", T, [("user", "What is the deal worth?"), ("assistant", secret(mark))])
    ua = conversation(db, "s3_u", T + DAY, [("user", "Draft the follow-up mail."), ("assistant", "Drafted.")])
    await run(fx)
    w = (await message_of("s3_w", pa[1]))[0]
    await withhold(client, w.id)
    await join("s3_u", w.source_key, state_=groups.UNCERTAIN)
    await sweep()
    await bj.settle(fx, keep=False)
    u1 = await row_id("s3_u", ua[1])
    await release(client, u1)
    await keep(await row_id("s3_u", ua[0]))
    await keep_own_rows("s3_w", pa, pa[1])
    assert (await pending())["uncertain_waiting"] == 0
    # JARVIS rewrites the released row in place: the backup holds the row with other content than the release covers
    bj.change(db, "UPDATE messages SET content = ? WHERE id = ?", "Drafted, and %s too." % mark, ua[1])
    await run(fx)
    assert await state("s3_u", ua[1]) == "held"
    st = await stone_of("s3_u", ua[1])
    assert st is not None and st.review == identity.PENDING
    assert (await pending())["uncertain_waiting"] >= 1
    with pytest.raises(imp.Refused):
        await imp.decisions_export(str(tmp_path / "decisions.json"))
    assert await bj.nowhere(mark) == []


async def test_s3_another_groups_hold_on_a_row_the_uncertain_group_released_does_not_keep_the_export_waiting(
        client, fx, tmp_path):
    db = fx["db"]
    m1, m2 = "zqs3xa60", "zqs3xb60"
    pa = conversation(db, "s3_a", T, [("user", "What is the deal worth?"), ("assistant", secret(m1))])
    pb = conversation(db, "s3_b", T + DAY, [("user", "Who signs the lease?"), ("assistant", secret(m2))])
    ua = conversation(db, "s3_u", T + 2 * DAY, [("user", "Draft the follow-up mail."), ("assistant", "Drafted.")])
    await run(fx)
    wa = (await message_of("s3_a", pa[1]))[0]
    wb = (await message_of("s3_b", pb[1]))[0]
    await withhold(client, wa.id)
    await join("s3_u", wa.source_key, state_=groups.UNCERTAIN)
    await sweep()
    await withhold(client, wb.id)
    await bj.settle(fx, keep=False)
    u = [await row_id("s3_u", r) for r in ua]
    st0 = await stone_of("s3_u", ua[0])
    assert st0 is not None and st0.anchor_key == wa.source_key
    await release(client, u[0])  # the uncertain group A releases row 0 (typed); row 1 stays A's pending hold
    await keep_own_rows("s3_a", pa, pa[1])
    await keep_own_rows("s3_b", pb, pb[1])
    # the conversation is also a member of B's group: B lists row 0 as its own hold
    await join("s3_u", wb.source_key, state_=groups.MEMBER)
    await sweep()
    st0 = await stone_of("s3_u", ua[0])
    assert st0 is not None and st0.review == identity.PENDING and st0.anchor_key == wb.source_key
    assert await state("s3_u", ua[0]) == "held"
    # control: A's own pending hold (row 1) keeps the export waiting; B's hold on row 0 does not count for A
    assert (await pending())["uncertain_waiting"] == 1
    with pytest.raises(imp.Refused):
        await imp.decisions_export(str(tmp_path / "d0.json"))
    await keep(u[1])
    assert (await pending())["uncertain_waiting"] == 0
    out = tmp_path / "decisions.json"
    await imp.decisions_export(str(out))
    assert m1 not in out.read_text() and m2 not in out.read_text()
    assert await state("s3_u", ua[0]) == "held"  # B's hold stays a hold
    assert await bj.nowhere(m1) == [] and await bj.nowhere(m2) == []
