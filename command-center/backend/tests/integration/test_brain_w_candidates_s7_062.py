"""0.6.2 option C stabilisation S7: a carrier of a withheld text never becomes readable because W's own row is gone.

Fresh seeds 13457 and 13756 (P1, both live-history arms; a regression from S4's RECON-62 second part, 881c9c6): W's
own row (its conversation) was gone from a later backup; W's words were in hand only through a member whose kept hold
said 'identical' (another uid and time: not a copy of W's own row, so RECON-62 no longer takes it as W's text), and
through rows with W's uid ('same message id', never taken as W's text since round 3). W then counted as not in this
backup, so the carrier scan outside every group had nothing to compare, and a later backup's session_search read
carrying W word for word stayed readable. The same root gave F1 (13138 ... 13893): holds said 'not in this backup'
where the backup held W's very words in a kept 'identical' member.

RECON-68 (fail-closed): the texts such members hold are compared for carriers (held per row, also when they are not
W's text: holding more, never less); a kept 'identical' member read as authored words stands in for W's text only when
it cannot be another withheld text (every other one verified in hand, none identical to it, one stand-in), and then
'none' is never said against it ('not compared with every withheld text' when nothing relates). RECON-62's protection
stays: a JSON read 'identical' by one of its values never stands in (13409; test_brain_release_twin_s4_062).
Synthetic data only."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text as sql

from app.brain import identity
from app.brain.importer import Processor
from app.db import session_scope

from .. import brain_jarvis as bj
from .test_brain_api import client  # noqa: F401 - client is a fixture
from .test_brain_later_062 import holds, state
from .test_brain_release_twin_s4_062 import keep
from .test_brain_withhold_identity import DAY, T, conversation, message_of, run, secret, withhold

pytestmark = pytest.mark.usefixtures("clean_db")


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


def search_read(sid: str, content: str) -> str:
    """A Hermes session_search read of another conversation (its messages as JSON values)."""
    return json.dumps({"success": True, "mode": "read", "session_id": sid,
                       "messages": [{"id": 1, "role": "user", "content": "Where did we leave the contract?"},
                                    {"id": 2, "role": "assistant", "content": content}]})


async def readable_with(mark: str) -> int:
    async with session_scope() as s:
        return int(await s.scalar(sql("SELECT count(*) FROM archive_messages WHERE NOT withheld "
                                      "AND content LIKE '%' || :m || '%'"), {"m": mark}) or 0)


async def kept_identical_scenario(client, fx, mark: str) -> tuple[str, int, int]:
    """13457's shape: W in s7_w; the same words as a row of another conversation (s7_k: another uid and time), held as
    W's carrier at the withhold ('identical') and kept by the admin; then W's conversation is gone from the backup.
    Returns the kept member's conversation and its kept row's id."""
    db = fx["db"]
    rows = conversation(db, "s7_w", T, [("user", "Please compare the vendor review and keep it short."),
                                        ("assistant", secret(mark))])
    other = conversation(db, "s7_k", T + DAY, [("user", "What did the vendor say again?"),
                                               ("assistant", secret(mark))])
    await run(fx)
    await withhold(client, (await message_of("s7_w", rows[1]))[0].id)
    k = (await message_of("s7_k", other[1]))[0]
    async with session_scope() as s:
        from app.models import BrainTombstone
        st = await s.get(BrainTombstone, k.source_key)
        assert st is not None and identity.IDENTICAL_REL in (st.relations or []), (st and st.relations)
    await keep(k.id)
    bj.change(db, "DELETE FROM messages WHERE session_id = ?", "s7_w")
    bj.change(db, "DELETE FROM sessions WHERE id = ?", "s7_w")
    return "s7_k", other[1], k.id


async def test_s7_a_read_carrying_w_is_held_when_only_a_kept_identical_member_holds_its_words(client, fx):
    """P1 (13457): W's own row is gone; a later backup's session_search read in a new conversation outside every group
    carries W's words. The kept 'identical' member's text is compared for carriers: the read is held, never readable
    (it was readable on 23632ad: W 'not in this backup', nothing compared outside the group)."""
    mark = "zqs7kc01"
    await kept_identical_scenario(client, fx, mark)
    db = fx["db"]
    bj.add_session(db, "s7_r", started=T + 3 * DAY)
    bj.add_row(db, "s7_r", "user", "Find the vendor contract numbers for me.", T + 3 * DAY + 1)
    read = bj.add_row(db, "s7_r", "tool", search_read("s7_k", secret(mark)), T + 3 * DAY + 2,
                      tool_call_id="call_s7kc", tool_name="session_search")
    await run(fx)
    assert await state("s7_r", read) == "held"
    assert await readable_with(mark) == 0


async def test_s7_evidence_with_a_stand_in_for_w_is_never_none_and_shows_its_relations(client, fx):
    """F1 (13476, 13138): with the kept 'identical' member standing in for W, a new row in its conversation with W's very
    words shows 'identical' (not only 'not in this backup'), and a new unrelated row says 'not compared with every
    withheld text', never 'none' (no one-click release against a text that is not W's own row)."""
    mark = "zqs7ev02"
    sid, _, _ = await kept_identical_scenario(client, fx, mark)
    db = fx["db"]
    plain = bj.add_row(db, sid, "user", "Book the hotel near the river front, please.", T + 2 * DAY)
    same = bj.add_row(db, sid, "assistant", secret(mark), T + 2 * DAY + 5)
    await run(fx)
    h = await holds(client)
    hs = h.get((await message_of(sid, same))[-1].id) or {}
    hp = h.get((await message_of(sid, plain))[-1].id) or {}
    assert hs and identity.IDENTICAL_REL in (hs.get("relations") or []), hs
    assert hp, "the new row of the kept member's conversation is held"
    assert "none" not in (hp.get("relations") or []) and not hp.get("one_click"), hp
    assert identity.OTHER_NOT_COMPARED in (hp.get("relations") or []), hp
    assert await readable_with(mark) == 0


async def test_s7_a_read_carrying_w_is_held_when_only_rows_with_its_uid_hold_its_words(client, fx):
    """P1 (13457's rotation copies): W's own row is gone; a continuation JARVIS made with W's uid at another time
    ('same message id', a certain member never taken as W's text) holds W's words; a read outside every group
    carrying them is held."""
    db = fx["db"]
    mark = "zqs7su03"
    uid = "s7uid000000000000000000000000003"
    bj.add_session(db, "s7_s", started=T, end_reason="compression")
    bj.add_row(db, "s7_s", "user", "Please compare the vendor review and keep it short.", T + 1)
    w = bj.add_row(db, "s7_s", "assistant", secret(mark), T + 2, uid=uid)
    bj.add_session(db, "s7_sc", started=T + DAY, parent="s7_s", model_config={"_compressed_from": "s7_s"})
    bj.add_row(db, "s7_sc", "assistant", secret(mark), T + DAY + 1, uid=uid)  # W's uid, another time
    await run(fx)
    await withhold(client, (await message_of("s7_s", w))[0].id)
    async with session_scope() as s:
        vias = {r[0] for r in (await s.execute(sql(
            "SELECT via FROM brain_tombstones WHERE source_session_id = 's7_sc' AND review <> 'pending'"))).all()}
    assert "same message id" in vias, vias
    bj.change(db, "DELETE FROM messages WHERE session_id = ?", "s7_s")
    bj.add_session(db, "s7_r", started=T + 3 * DAY)
    read = bj.add_row(db, "s7_r", "tool", search_read("s7_sc", secret(mark)), T + 3 * DAY + 2,
                      tool_call_id="call_s7su", tool_name="session_search")
    await run(fx)
    assert await state("s7_r", read) == "held"
    assert await readable_with(mark) == 0


async def test_s7_a_read_carrying_w_is_held_when_only_a_pending_identical_hold_holds_its_words(client, fx):
    """P1, the pending variant: after the withhold JARVIS continues W's conversation with W's uid at another time; the
    import holds that row pending ('new in a later backup', 'identical'); then W's own row is gone. The pending hold's
    text is compared for carriers (never a stand-in for W): a read outside every group carrying it is held."""
    db = fx["db"]
    mark = "zqs7ph06"
    uid = "s7uid000000000000000000000000006"
    bj.add_session(db, "s7_p", started=T)
    bj.add_row(db, "s7_p", "user", "Please compare the vendor review and keep it short.", T + 1)
    w = bj.add_row(db, "s7_p", "assistant", secret(mark), T + 2, uid=uid)
    await run(fx)
    await withhold(client, (await message_of("s7_p", w))[0].id)
    bj.change(db, "UPDATE sessions SET end_reason = 'compression' WHERE id = ?", "s7_p")
    bj.add_session(db, "s7_pc", started=T + DAY, parent="s7_p", model_config={"_compressed_from": "s7_p"})
    c = bj.add_row(db, "s7_pc", "assistant", secret(mark), T + DAY + 1, uid=uid)  # W's uid, another time
    await run(fx)
    held = await holds(client)
    hc = held.get((await message_of("s7_pc", c))[-1].id) or {}
    assert identity.IDENTICAL_REL in (hc.get("relations") or []), hc
    bj.change(db, "DELETE FROM messages WHERE session_id = ?", "s7_p")
    bj.add_session(db, "s7_r", started=T + 3 * DAY)
    read = bj.add_row(db, "s7_r", "tool", search_read("s7_pc", secret(mark)), T + 3 * DAY + 2,
                      tool_call_id="call_s7ph", tool_name="session_search")
    await run(fx)
    assert await state("s7_r", read) == "held"
    assert await readable_with(mark) == 0


async def test_s7_a_gateway_copy_of_w_at_its_time_stands_in_when_w_is_gone(client, fx):
    """F1 (13340, 13893): a gateway branch copied W with a fresh uid and W's own time (a 'copied by JARVIS' member that
    RECON-62's copy test refuses for its other uid); W's own row is then gone. The copy stands in for W's text: a new
    row of the branch with W's words shows 'identical', an unrelated one 'not compared with every withheld text' (never
    'none', never one click), and a read outside every group carrying W's words is held."""
    db = fx["db"]
    mark = "zqs7gw05"
    rows = conversation(db, "s7_g", T, [("user", "Please compare the vendor review and keep it short."),
                                        ("assistant", secret(mark))])
    copies = bj.gateway_branch(db, "s7_g", "s7_gb", T + 600)
    await run(fx)
    await withhold(client, (await message_of("s7_g", rows[1]))[0].id)
    await bj.settle(fx, keep=False)  # the decision backup's Settle finds the copy ('copied by JARVIS', certain)
    async with session_scope() as s:
        via = await s.scalar(sql("SELECT t.via FROM brain_tombstones t JOIN archive_messages m ON m.source_key = "
                                 "t.source_key JOIN archive_sessions a ON a.id = m.session_id WHERE "
                                 "a.source_session_id = 's7_gb' AND m.source_message_id = :r"), {"r": str(copies[1])})
    assert via == "copied by JARVIS", via
    bj.change(db, "DELETE FROM messages WHERE session_id = ?", "s7_g")
    plain = bj.add_row(db, "s7_gb", "user", "Book the hotel near the river front, please.", T + DAY)
    same = bj.add_row(db, "s7_gb", "assistant", secret(mark), T + DAY + 5)
    bj.add_session(db, "s7_r", started=T + 3 * DAY)
    read = bj.add_row(db, "s7_r", "tool", search_read("s7_gb", secret(mark)), T + 3 * DAY + 2,
                      tool_call_id="call_s7gw", tool_name="session_search")
    await run(fx)
    h = await holds(client)
    hs = h.get((await message_of("s7_gb", same))[-1].id) or {}
    hp = h.get((await message_of("s7_gb", plain))[-1].id) or {}
    assert hs and identity.IDENTICAL_REL in (hs.get("relations") or []), hs
    assert hp and "none" not in (hp.get("relations") or []) and not hp.get("one_click"), hp
    assert identity.OTHER_NOT_COMPARED in (hp.get("relations") or []), hp
    assert await state("s7_r", read) == "held"
    assert await readable_with(mark) == 0


async def test_s7_without_a_withheld_text_or_candidate_a_new_conversation_stays_readable_control(client, fx):
    """Control: W's conversation gone, no member holding its words; a new conversation's read of unrelated words stays
    readable (the change holds carriers of candidate texts only)."""
    db = fx["db"]
    mark = "zqs7ct04"
    rows = conversation(db, "s7_w", T, [("user", "Please compare the vendor review and keep it short."),
                                        ("assistant", secret(mark))])
    await run(fx)
    await withhold(client, (await message_of("s7_w", rows[1]))[0].id)
    bj.change(db, "DELETE FROM messages WHERE session_id = ?", "s7_w")
    bj.add_session(db, "s7_r", started=T + 3 * DAY)
    read = bj.add_row(db, "s7_r", "tool", search_read("s7_x", "The hotel near the river front is booked."),
                      T + 3 * DAY + 2, tool_call_id="call_s7ct", tool_name="session_search")
    await run(fx)
    assert await state("s7_r", read) == "readable"


# --- the stand-in rule (unit level) -----------------------------------------------------------------------------------


W1 = "Please send over the backup schedule before the weekly call."
W2 = "Quick summary of the quarterly plan: on track, no blockers today."


def test_s7_stand_in_only_when_it_cannot_be_another_withheld_text():
    """RECON-68: one stand-in, every other withheld text verified, none identical to it: the stand-in; otherwise none
    (a kept 'identical' hold may have been identical to another withheld text, or to one not in hand)."""
    pick = Processor._w_unverified_text
    roots = {"w1", "w2"}
    assert pick("w2", [(W2, True)], {"w1": W1}, roots) == W2
    assert pick("w2", [(W2, True)], {}, roots) is None  # w1 not verified here: W2's text could be w1's
    assert pick("w2", [(W1.upper(), True)], {"w1": W1}, roots) is None  # identical (under fold) to w1's text
    assert pick("w2", [(W2, True), (W1 + " Thanks.", True)], {"w1": W1}, roots) is None  # two stand-ins
    assert pick("w2", [(W2, False)], {"w1": W1}, roots) is None  # a candidate for carriers only
    assert pick("w2", [(W2, True), (W2.lower(), True)], {"w1": W1}, {"w1", "w2"}) in (W2, W2.lower())
