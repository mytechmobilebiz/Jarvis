"""0.6.2 option C stabilisation S1 (restore and live): the fresh-seed classes A5 and B7-restore of the R5 triage.

- RECON-51 (A5 (a), (c)): a row an earlier backup stored and a later backup repeats unchanged ('already imported')
  holds that later backup's content too: a hold of it is decided on the newest backup that holds its content, and a
  release decided there covers it, on the test copy (the next import brings its text back) and on live (live stored
  the row from the earlier backup, then imports the decision backup with the decisions file).
- RECON-52 (A5 (c), B7): a row an admin released waits for its text without being listed again as a hold of its own
  ('not in this backup'): a sweep before the next import does not ask the admin again, and the export does not wait
  for it (an uncertain row released per row with the typed word included).
Synthetic data only."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.brain import groups, identity, review
from app.brain import importer as imp
from app.brain import source as src
from app.db import session_scope
from app.models import ArchiveMessage, BrainTombstone

from .. import brain_jarvis as bj
from ..conftest import ADMIN
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_hold_r2_062 import join, row_id
from .test_brain_later_062 import holds, pending, state
from .test_brain_withhold_identity import ALL, DAY, T, _fresh_production, conversation, message_of, run, secret
from .test_brain_withhold_identity import withhold

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


async def sweep() -> None:
    """The central sweep as a Settle, a withhold or decisions-apply runs it between imports."""
    async with session_scope() as s:
        await review.hold_group_rows(s, actor=ADMIN[0], cause="test")
        await s.commit()


async def test_s1_a_hold_of_a_row_an_earlier_backup_stored_is_decided_on_the_backup_that_repeats_it(client, fx,
                                                                                                     tmp_path):
    """RECON-51 (13075, 13159; live 13002, 13008): the test copy imported an earlier backup B0, then the decision
    backup B1, which holds the conversation unchanged. The rows the withhold holds with its conversation are decided
    on B1 (B1 holds their very content), the admin's release is decided there, and the next import of B1 brings the
    released text back (before: decided on B0, held again by the import's sweep, the export kept waiting). Live
    stored the rows from B0 first, then imports B1 with the decisions file: the released row is readable there too
    (before: held, a release decided on B0's content never covering the stored row), the withheld one is not."""
    db = fx["db"]
    mark = "zqs1rc51"
    ids = conversation(db, "s1_p", T, [("user", secret(mark)), ("assistant", "Noted."),
                                       ("user", "Anything else to plan for the week?")])
    b0 = bj.copy(fx, tmp_path / "b0")  # the earlier backup, as the test copy and live imported it
    await run(b0)
    conversation(db, "s1_o", T + DAY, [("user", "Book the train."), ("assistant", "Booked.")])
    b1_sha = src.sha256_file(db)
    await run(fx)  # B1: s1_p unchanged ('already imported')
    await withhold(client, await row_id("s1_p", ids[0]))
    st = await stone_of("s1_p", ids[2])
    assert st is not None and st.review == identity.PENDING and st.via == identity.GROUP_HOLD
    assert st.decided_on == b1_sha  # the newest backup that holds the row's content
    released = await row_id("s1_p", ids[2])
    await release(client, released)
    await run(fx)
    [back] = await message_of("s1_p", ids[2])
    assert not back.withheld and back.content == "Anything else to plan for the week?"
    assert released not in await holds(client)
    await bj.settle(fx)  # 0.6.2 plan 4.7: the export waits for Settle
    assert await state("s1_p", ids[2]) == "readable"
    out = tmp_path / "decisions.json"
    await imp.decisions_export(str(out))
    assert mark not in out.read_text()
    await _fresh_production()
    await imp.run(bj.paths(b0), ALL)  # live imported B0 before any decision existed
    live = await imp.run(bj.paths(fx), ALL, actor=ADMIN[0], decisions=str(out))
    assert live["decisions"]["warnings"] == []
    assert await state("s1_p", ids[2]) == "readable"
    [on_live] = await message_of("s1_p", ids[2])
    assert on_live.content == "Anything else to plan for the week?"
    assert await state("s1_p", ids[0]) == "certain" and await state("s1_p", ids[1]) == "held"
    assert await bj.nowhere(mark) == []


async def test_s1_a_released_group_row_waiting_for_its_text_is_not_listed_again(client, fx):
    """RECON-52 (13825, 13150 and the B7-restore group rows): the admin releases a row held with its conversation;
    until the next import of a backup holding it, it stays without its text, and a sweep in between (Settle, another
    withhold, decisions-apply) leaves it as the admin's release: no new 'not in this backup' hold asks the admin again
    (before: listed again, and the one-click release of the conversation refused). The next import brings it back."""
    db = fx["db"]
    mark = "zqs1rl52"
    ids = conversation(db, "s1_q", T, [("user", secret(mark)), ("assistant", "Noted."),
                                       ("user", "Anything else to plan for the week?")])
    await run(fx)
    await withhold(client, await row_id("s1_q", ids[0]))
    released = await row_id("s1_q", ids[2])
    assert released in await holds(client)
    await release(client, released)
    await sweep()
    [waiting] = await message_of("s1_q", ids[2])
    assert waiting.withheld and waiting.content == ""
    assert ((waiting.flags or {}).get("withheld_by_admin") or {}).get("released_at")
    assert await stone_of("s1_q", ids[2]) is None
    assert released not in await holds(client)
    await run(fx)
    assert await state("s1_q", ids[2]) == "readable"
    assert await state("s1_q", ids[1]) == "held" and await state("s1_q", ids[0]) == "certain"
    assert await bj.nowhere(mark) == []


async def test_s1_a_complete_review_of_an_uncertain_conversation_lets_the_export_go(client, fx, tmp_path):
    """RECON-53 (O-D1, 13075 / 13159 / 13355's refused export): every row of a conversation an uncertain link puts in
    the group is held, never one-click; the export waits while one is neither kept nor released. A complete review is
    each one kept, or released per row with the typed word: a released row waiting for its text no longer waits for
    the admin (before: the sweep listed it again as an uncertain hold and the export stayed refused), while a row
    still unreviewed keeps the export waiting."""
    db = fx["db"]
    mark = "zqs1od53"
    pa = conversation(db, "s1_w", T, [("user", "What is the deal worth?"), ("assistant", secret(mark))])
    ua = conversation(db, "s1_u", T + DAY, [("user", "Draft the follow-up mail."), ("assistant", "Drafted."),
                                            ("user", "Send it on Monday.")])
    await run(fx)
    w = (await message_of("s1_w", pa[1]))[0]
    await withhold(client, w.id)
    await join("s1_u", w.source_key, state_=groups.UNCERTAIN)
    await sweep()
    await bj.settle(fx, keep=False)
    listed = await holds(client)
    u = [await row_id("s1_u", r) for r in ua]
    assert all(listed[i]["uncertain"] and listed[i]["confirm_required"] for i in u)
    assert (await pending())["uncertain_waiting"] == 3
    out = tmp_path / "decisions.json"
    with pytest.raises(imp.Refused):
        await imp.decisions_export(str(out))
    # a release without the typed word is refused (B6): the row still waits
    await as_(client, "admin")
    assert (await client.post("/api/brain/holds/%d/release" % u[0])).status_code in (400, 422)
    await release(client, u[0])
    await release(client, u[1])
    async with session_scope() as s:
        await review.keep_hold(s, await s.get(ArchiveMessage, u[2]), actor=ADMIN[0])
        await s.commit()
    for i in pa:  # the withheld conversation's own unrelated row: reviewed too (kept)
        if i != pa[1] and await state("s1_w", i) == "held":
            async with session_scope() as s:
                await review.keep_hold(s, await s.get(ArchiveMessage, await row_id("s1_w", i)), actor=ADMIN[0])
                await s.commit()
    await sweep()  # a sweep before the next import (another withhold, Settle, decisions-apply)
    assert (await pending())["uncertain_waiting"] == 0
    assert u[0] not in await holds(client) and u[1] not in await holds(client)
    await imp.decisions_export(str(out))
    data = json.loads(out.read_text())
    assert mark not in out.read_text()
    assert {(r["session"], r["row"]) for r in data["releases"]} >= {("s1_u", str(ua[0])), ("s1_u", str(ua[1]))}
    await run(fx)
    assert [await state("s1_u", r) for r in ua][:2] == ["readable", "readable"]
    [kept] = await message_of("s1_u", ua[2])
    assert kept.withheld and kept.content == "" and (await stone_of("s1_u", ua[2])).review == identity.KEPT
    assert await bj.nowhere(mark) == []
