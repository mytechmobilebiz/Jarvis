"""0.6.2 option C stabilisation S4: the round-3 replay seed 1 (test_rv3_bug_seed_replayed_exactly[1], open since R2).

The scripted review released two independent holds (messages 34, 35) the holds list showed and the API answered
"this message is not held for review": the release of an earlier row (14, JARVIS row s001_cbr1:9) removed "every hold
of this exact JARVIS row in this group", and the holds of 34 and 35 (other stored messages, renumbered copies that
JARVIS stored under their own uid keys, held as 'same JARVIS row' and bound to rows 9 and 10) were among them. Those
messages stayed withheld with nothing to review.

RECON-61: a hold whose key is another stored message that is still withheld is that message's own hold; a release of
one row leaves it for its own review. Only a key no other withheld message carries (a key adoption's old key) goes
with the release (control). Synthetic data only."""

from __future__ import annotations

import pytest

from app.brain import identity
from app.db import session_scope
from app.models import ArchiveMessage, BrainTombstone

from .. import brain_jarvis as bj
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_later_062 import holds, state
from .test_brain_withhold_identity import DAY, T, conversation, message_of, run, secret, withhold

pytestmark = pytest.mark.usefixtures("clean_db")


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


async def release(client, message_id: int):
    await as_(client, "admin")
    return await client.post("/api/brain/holds/%d/release" % message_id, json={"confirm": identity.CONFIRM_WORD})


async def setup(client, fx) -> tuple[ArchiveMessage, ArchiveMessage]:
    db = fx["db"]
    mark = "zqs4tw61"
    rows = conversation(db, "s4_w", T, [("user", "Plan the trip to Lisbon in May."),
                                        ("assistant", secret(mark)),
                                        ("user", "Book the hotel near the river front.")])
    await run(fx)
    await withhold(client, (await message_of("s4_w", rows[1]))[0].id)
    a = (await message_of("s4_w", rows[0]))[0]
    b = (await message_of("s4_w", rows[2]))[0]
    assert await state("s4_w", rows[0]) == "held" and await state("s4_w", rows[2]) == "held"
    return a, b


async def bind_to(key: str, a: ArchiveMessage) -> None:
    """The hold under `key` binds JARVIS row `a` (as the replay's 'same JARVIS row' holds of the renumbered copies
    bound the rows they copied)."""
    async with session_scope() as s:
        st_a = await s.get(BrainTombstone, a.source_key)
        st = await s.get(BrainTombstone, key)
        st.source_row_id, st.role, st.tool_call_id = st_a.source_row_id, st_a.role, st_a.tool_call_id
        st.source_at, st.message_uid, st.uid_known = st_a.source_at, st_a.message_uid, st_a.uid_known
        st.via = "same JARVIS row"
        await s.commit()


async def test_s4_release_of_a_row_keeps_the_hold_of_another_withheld_message_bound_to_it(client, fx):
    """RECON-61: releasing row A leaves the hold of message B (another stored, withheld message whose hold binds A's
    JARVIS row) pending and listed; B is released on its own review."""
    a, b = await setup(client, fx)
    await bind_to(b.source_key, a)
    r = await release(client, a.id)
    assert r.status_code == 200, r.text
    assert await state("s4_w", int(b.source_message_id)) == "held"
    assert b.id in await holds(client)
    r = await release(client, b.id)
    assert r.status_code == 200, r.text  # was 400 'this message is not held for review'


async def test_s4_release_of_a_row_ends_its_old_key_hold_control(client, fx):
    """Control: a hold under a key no stored message carries (a key adoption's old key) binding the same JARVIS row
    goes with the release, as before."""
    a, b = await setup(client, fx)
    async with session_scope() as s:
        st_a = await s.get(BrainTombstone, a.source_key)
        old = BrainTombstone(source_key="jarvis:message:s4_w:old-key", reason=st_a.reason,
                             source_session_id=st_a.source_session_id, source_row_id=st_a.source_row_id,
                             role=st_a.role, tool_call_id=st_a.tool_call_id, source_at=st_a.source_at,
                             message_uid=st_a.message_uid, uid_known=st_a.uid_known, anchor_key=st_a.anchor_key,
                             via=st_a.via, review=identity.PENDING, relations=list(st_a.relations or []),
                             compared=list(st_a.compared or []), decided_on=st_a.decided_on, form=st_a.form)
        s.add(old)
        await s.commit()
    r = await release(client, a.id)
    assert r.status_code == 200, r.text
    async with session_scope() as s:
        assert await s.get(BrainTombstone, "jarvis:message:s4_w:old-key") is None
    assert await state("s4_w", int(b.source_message_id)) == "held"


# --- RECON-62: the withheld text is never taken from a JARVIS copy of another kept row ---------------------------------


async def keep(message_id: int) -> None:
    from app.brain import review
    from ..conftest import ADMIN
    async with session_scope() as s:
        await review.keep_hold(s, await s.get(ArchiveMessage, message_id), actor=ADMIN[0])
        await s.commit()


async def w_text_scenario(client, fx, *, keep_rows: bool) -> tuple[dict, str]:
    """W's conversation, its other rows held and (keep_rows) kept; JARVIS branches it (the copies keep their uids and
    times: certain 'copied by JARVIS' members of the kept rows); then W's row and its copy are gone and the branch
    has a new row with W's very words (no uid or time of W). The new row's hold, and W's mark."""
    db = fx["db"]
    mark = "zqs4wt62"
    rows = conversation(db, "s4_wt", T, [("user", "Please compare the vendor review and keep it short."),
                                         ("assistant", secret(mark)),
                                         ("user", "Book the hotel near the river front, please.")])
    await run(fx)
    await withhold(client, (await message_of("s4_wt", rows[1]))[0].id)
    if keep_rows:
        for r in (rows[0], rows[2]):
            await keep((await message_of("s4_wt", r))[0].id)
    copies = bj.cli_branch(db, "s4_wt", "s4_wtb", T + 3600)
    await run(fx)  # the copies are stored as certain members ('copied by JARVIS') of the kept rows
    bj.change(db, "DELETE FROM messages WHERE id IN (?, ?)", rows[1], copies[1])
    new = bj.add_row(db, "s4_wtb", "assistant", secret(mark), T + 7200)
    await run(fx)
    held = await holds(client)
    m = (await message_of("s4_wtb", new))[-1]
    return held.get(m.id) or {}, mark


async def test_s4_w_text_is_never_a_copy_of_another_kept_row(client, fx):
    """RECON-62: with W's own row and its copy gone, the branch's copies of the kept rows are certain members, but
    they hold those rows' text, not W's: W is not in this backup, and a new row with W's words is held 'not in this
    backup', never 'none' (one click) against a kept row's text taken as W's."""
    h, mark = await w_text_scenario(client, fx, keep_rows=True)
    assert h, "the new row with W's words is held"
    assert "none" not in (h.get("relations") or []) and not h.get("one_click"), h
    assert "not_in_backup" in (h.get("relations") or []), h
    async with session_scope() as s:
        from sqlalchemy import text as sql
        assert not await s.scalar(sql("SELECT count(*) FROM archive_messages WHERE NOT withheld "
                                      "AND content LIKE '%' || :m || '%'"), {"m": mark})


async def test_s4_w_text_without_kept_rows_control(client, fx):
    """Control: the same history with the other rows only pending (no certain copies of them): the new row is held
    'not in this backup' as well."""
    h, _ = await w_text_scenario(client, fx, keep_rows=False)
    assert h and "none" not in (h.get("relations") or []) and not h.get("one_click"), h


async def test_s4_w_text_is_never_a_kept_row_identical_by_one_of_its_values(client, fx):
    """RECON-62 (13409): a kept hold whose evidence says 'identical' because one of its values (a session_search read's
    message) is a withheld text, here the second one, holds a JSON read, not W1. With W1's row gone, a new row with
    W1's very words is held 'not in this backup', never 'none' (one click) against the JSON read taken as W1's text."""
    db = fx["db"]
    mark = "zqs4wj62"
    w2 = "Please rewrite the keyword sweep for the team meeting."
    read = ('{"success": true, "mode": "read", "session_id": "s4_wj2", "messages": [{"id": 1, "role": "user", '
            '"content": "%s"}]}' % w2)
    rows = conversation(db, "s4_wj", T, [("user", "Please compare the vendor review and keep it short."),
                                         ("assistant", secret(mark))])
    call = bj.add_row(db, "s4_wj", "tool", read, T + 10, tool_call_id="call_s4wj", tool_name="session_search")
    other = conversation(db, "s4_wj2", T - DAY, [("user", w2), ("assistant", "Rewritten.")])
    await run(fx)
    await withhold(client, (await message_of("s4_wj", rows[1]))[0].id)
    await withhold(client, (await message_of("s4_wj2", other[0]))[0].id)
    await bj.settle(fx, keep=False)
    j = (await message_of("s4_wj", call))[0]
    async with session_scope() as s:
        st = await s.get(BrainTombstone, j.source_key)
        assert st is not None and identity.IDENTICAL_REL in (st.relations or []), (st and st.relations)
    for m in await message_of("s4_wj", rows[0]) + [j]:
        if await state("s4_wj", int(m.source_message_id)) == "held":
            await keep(m.id)
    bj.change(db, "DELETE FROM messages WHERE id = ?", rows[1])
    new = bj.add_row(db, "s4_wj", "assistant", secret(mark), T + 7200)
    await run(fx)
    h = (await holds(client)).get((await message_of("s4_wj", new))[-1].id) or {}
    assert h, "the new row with W1's words is held"
    assert "none" not in (h.get("relations") or []) and not h.get("one_click"), h


# --- RECON-64: a piece of a withheld fold is not the withheld text ------------------------------------------------------


async def test_s4_a_message_the_withheld_fold_absorbed_is_never_none_when_the_fold_is_split_again(client, fx):
    """RECON-64 (13536): the admin withheld a fold (Hermes merged a request into the next one); a later backup holds
    the withheld row with only its own first request again and the folded request as a new row of its own. That row
    is part of what the admin withheld: held, never 'none' (one click), and the decision's other rows say 'not in this
    backup' rather than 'none' (the rest of the withheld text is not here)."""
    db = fx["db"]
    first = "Please look again at the hosting migration before lunch."
    second = "Please look again at the landing page copy with the usual care."
    rows = conversation(db, "s4_wf", T, [("user", "Hello there, one more thing for today."), ("user", first),
                                         ("user", second), ("assistant", "On it, both points are noted.")])
    bj.fold(db, "s4_wf", rows[1], [rows[2]])  # the fold keeps row 2 (the first request) and absorbs row 3
    await run(fx)
    w = (await message_of("s4_wf", rows[1]))[0]
    await withhold(client, w.id)
    bj.change(db, "UPDATE messages SET content = ?, absorbed_message_uids = '[]' WHERE id = ?", first, rows[1])
    new = bj.add_row(db, "s4_wf", "user", second, T + 3600)
    await run(fx)
    h = (await holds(client)).get((await message_of("s4_wf", new))[-1].id) or {}
    assert h, "the folded request, back as a row of its own, is held"
    assert "none" not in (h.get("relations") or []) and not h.get("one_click"), h
    assert "not_in_backup" in (h.get("relations") or []), h


async def test_s4_a_whole_copy_of_the_withheld_fold_is_its_text_before_a_piece(client, fx):
    """RECON-64 (13536 after D-48): the withheld fold's own row now holds its first request only, but a CLI branch
    copied the whole fold (its uid and time): the copy is the withheld text, so the folded request, back as a row of
    its own, is shown as part of it ('part of'), not only 'not in this backup'."""
    db = fx["db"]
    first = "Please look again at the hosting migration before lunch."
    second = "Please look again at the landing page copy with the usual care."
    rows = conversation(db, "s4_wg", T, [("user", "Hello there, one more thing for today."), ("user", first),
                                         ("user", second), ("assistant", "On it, both points are noted.")])
    bj.fold(db, "s4_wg", rows[1], [rows[2]])
    await run(fx)
    await withhold(client, (await message_of("s4_wg", rows[1]))[0].id)
    copies = bj.cli_branch(db, "s4_wg", "s4_wgb", T + 1800)  # the whole fold, with its uid and time
    bj.change(db, "UPDATE messages SET absorbed_message_uids = (SELECT absorbed_message_uids FROM messages WHERE id = ?) "
                  "WHERE id = ?", rows[1], copies[1])  # Hermes copies the row's absorbed uids with it
    await run(fx)  # the branch joins the group
    bj.change(db, "UPDATE messages SET content = ?, absorbed_message_uids = '[]' WHERE id = ?", first, rows[1])
    new = bj.add_row(db, "s4_wg", "user", second, T + 3600)
    await run(fx)
    h = (await holds(client)).get((await message_of("s4_wg", new))[-1].id) or {}
    assert h and "none" not in (h.get("relations") or []) and not h.get("one_click"), h
    assert "part_of" in (h.get("relations") or []), h
