"""0.6.2 option C (rebuild R3, round-3 derived3): carriers in derived forms through the flows (withhold, Settle, a
later import, a release, a rollback), every field of a row (tool call arguments too), the capped decode held as
not_checked, a group row's evidence, and the rollback copy whose older text carries W in a derived form. Synthetic
data built the way the vendored Hermes writes it."""

from __future__ import annotations

import json
import sqlite3

import pytest
from sqlalchemy import select

from app.brain import identity
from app.brain import importer as imp
from app.db import session_scope
from app.models import ImportRecord

from .. import brain_jarvis as bj
from ..conftest import ADMIN
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_later_062 import holds, state
from .test_brain_withhold_identity import DAY, T, conversation, message_of, run, withhold

pytestmark = pytest.mark.usefixtures("clean_db")

W = ("Der Notfallschlüssel für das Archiv liegt hinter der dritten Kachel links, Kombination Möwe-5512, "
     "das bleibt bitte unter uns.")
W_SHORT = "Ja, Möwe geht."


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


async def row_id(sid: str, source_row: int) -> int:
    return (await message_of(sid, source_row))[0].id


def nest(text: str, levels: int) -> str:
    out = text
    for n in range(levels):
        out = json.dumps({"level": n, "content": out}, ensure_ascii=True)
    return out


def tool_row(db: str, sid: str, content: str, ts: float, *, name: str = "session_search") -> int:
    rid = bj.add_row(db, sid, "tool", content, ts, tool_call_id="call_%d" % int(ts), tool_name=name)
    return rid


def set_calls(db: str, rid: int, calls: list[dict]) -> None:
    con = sqlite3.connect(db)
    con.execute("UPDATE messages SET tool_calls = ? WHERE id = ?", (json.dumps(calls), rid))
    con.commit()
    con.close()


def set_content(db: str, rid: int, content: str) -> None:
    con = sqlite3.connect(db)
    con.execute("UPDATE messages SET content = ? WHERE id = ?", (content, rid))
    con.commit()
    con.close()


async def test_derived3_a_nested_session_search_read_is_held_outside_every_group_until_released(client, fx):
    """N2: W three JSON levels deep in a session_search read of another, unlinked conversation: held on its own at
    the withhold, still held after the next import; the untyped release is refused, the typed one frees it and the
    next import brings its text back (the release covers the content it was decided on)."""
    db = fx["db"]
    pa = conversation(db, "dn_p", T, [("user", "Where is the key?"), ("assistant", W)])
    conversation(db, "dn_o", T + 5 * DAY, [("user", "What did I say about the archive?")])
    t = tool_row(db, "dn_o", nest(W, 3), T + 5 * DAY + 10)
    await run(fx)
    await withhold(client, await row_id("dn_p", pa[1]))
    assert await state("dn_o", t) == "held"
    h = (await holds(client))[await row_id("dn_o", t)]
    assert h["rule"] == identity.CARRIER_HOLD and identity.CONTAINS in h["relations"] and h["confirm_required"]
    await run(fx)
    assert await state("dn_o", t) == "held"
    mid = await row_id("dn_o", t)
    assert (await client.post("/api/brain/holds/%d/release" % mid)).status_code in (400, 422)
    r = await client.post("/api/brain/holds/%d/release" % mid, json={"confirm": identity.CONFIRM_WORD})
    assert r.status_code == 200, r.text
    await run(fx)
    assert await state("dn_o", t) == "readable"


async def test_derived3_a_carrier_added_later_is_held_by_the_import_with_w_in_hand(client, fx):
    """A prune line keeping W's first 40 characters (P1) arrives in a later backup, after Settle finished the decision,
    with W still in it: the import (W in hand) holds it on its own."""
    db = fx["db"]
    pa = conversation(db, "dl_p", T, [("user", "Where is the key?"), ("assistant", W)])
    await run(fx)
    await withhold(client, await row_id("dl_p", pa[1]))
    await bj.settle(fx, keep=False)
    conversation(db, "dl_o", T + 6 * DAY, [("user", "Make a picture of it.")])
    t = tool_row(db, "dl_o", "[image_generate] prompt=%s size=1024 (88 chars result)" % W[:40], T + 6 * DAY + 5,
                 name="image_generate")
    await run(fx)
    assert await state("dl_o", t) == "held"
    assert identity.CUT in (await holds(client))[await row_id("dl_o", t)]["relations"]


async def test_derived3_every_field_of_a_row_is_read_tool_call_arguments_too(client, fx):
    """An assistant row whose only copy of W is in its tool call arguments (a send_message call): held; its
    conversation's other rows stay readable."""
    db = fx["db"]
    pa = conversation(db, "df_p", T, [("user", "Where is the key?"), ("assistant", W)])
    oa = conversation(db, "df_o", T + 7 * DAY, [("user", "Forward my note to Anna."), ("assistant", "")])
    set_calls(db, oa[1], [{"id": "call_1", "type": "function", "function": {
        "name": "send_message", "arguments": json.dumps({"to": "anna", "text": W}, ensure_ascii=True)}}])
    await run(fx)
    await withhold(client, await row_id("df_p", pa[1]))
    assert await state("df_o", oa[1]) == "held"
    assert await state("df_o", oa[0]) == "readable"
    stored = (await message_of("df_o", oa[1]))[0]
    assert stored.tool_calls == [] and stored.content == ""


async def test_derived3_a_row_the_decode_cannot_read_in_full_is_held_as_not_checked(client, fx):
    """N2 cap (fail-closed): a tool result the decode cannot read within its caps is held as not_checked whenever a
    withheld text is in hand; before any withhold it stays readable (nothing to compare)."""
    db = fx["db"]
    pa = conversation(db, "dc_p", T, [("user", "Where is the key?"), ("assistant", W)])
    conversation(db, "dc_o", T + 8 * DAY, [("user", "Dump the tree.")])
    # more decode steps than the cap (each listed result is itself a JSON document to read); the importer itself
    # refuses a text nested past the JSON parser (excluded: RecursionError), so that shape never reaches a row
    deep = json.dumps({"results": [json.dumps({"n": n, "text": "row %d" % n}) for n in range(identity.CANON_STEPS + 8)]})
    assert identity.canon_capped(deep)
    t = tool_row(db, "dc_o", deep, T + 8 * DAY + 5)
    await run(fx)
    assert await state("dc_o", t) == "readable"
    await withhold(client, await row_id("dc_p", pa[1]))
    assert await state("dc_o", t) == "held"
    assert (await holds(client))[await row_id("dc_o", t)]["relations"] == [identity.NOT_CHECKED]


async def test_derived3_13546_a_short_w_quoted_with_markers_stays_held_after_a_rollback(client, fx):
    """13546: a short W quoted with '> ' markers in a session_search result outside every group: held at the
    withhold, and still held after a later import is rolled back."""
    db = fx["db"]
    pa = conversation(db, "ds_p", T, [("user", "Can we use Möwe?"), ("assistant", W_SHORT)])
    hit = {"role": "assistant", "content": "Earlier:\n> %s\n\nAnything else?" % W_SHORT}
    conversation(db, "ds_o", T + 9 * DAY, [("user", "What did you answer about Möwe?")])
    t = tool_row(db, "ds_o", json.dumps({"success": True, "results": [{"messages": [hit]}]}), T + 9 * DAY + 3)
    await run(fx)
    await withhold(client, await row_id("ds_p", pa[1]))
    assert await state("ds_o", t) == "held"
    conversation(db, "ds_x", T + 10 * DAY, [("user", "Book the train."), ("assistant", "Booked.")])
    later = await run(fx)
    await imp.rollback(later["run_id"], actor=ADMIN[0], confirm=True)
    assert await state("ds_o", t) == "held"


async def test_derived3_a_group_row_cut_in_a_prune_line_is_never_none(client, fx):
    """A row of the withheld conversation that keeps W's first 40 characters in a prune line: its evidence says
    'cut' (the typed word needed), never 'none', so the one-click release skips it."""
    db = fx["db"]
    pa = conversation(db, "dg_p", T, [("user", "Where is the key?"), ("assistant", W)])
    t = tool_row(db, "dg_p", "[send_message] to=anna text=%s (31 chars result)" % W[:40], T + 50,
                 name="send_message")
    await run(fx)
    await withhold(client, await row_id("dg_p", pa[1]))
    h = (await holds(client))[await row_id("dg_p", t)]
    assert identity.CUT in h["relations"] and identity.NO_RELATION not in h["relations"] and h["confirm_required"]
    await as_(client, "admin+")
    r = await client.post("/api/brain/sessions/%d/holds/release-unrelated" % (await message_of("dg_p", t))[0].session_id)
    assert r.status_code == 200 and await row_id("dg_p", t) not in r.json()["released"]
    assert await state("dg_p", t) == "held"


async def test_derived3_a_rollback_copy_whose_older_text_carries_w_in_a_derived_form_loses_it(client, fx):
    """A row outside every group that JARVIS rewrote in place: its update copy keeps the older text, W in another
    case in a JSON tool result. The withhold blanks that copy (COPY_HOLD, compared over its every field and reading) and a
    rollback of the update puts the row back held, never readable."""
    db = fx["db"]
    pa = conversation(db, "dr_p", T, [("user", "Where is the key?"), ("assistant", W)])
    conversation(db, "dr_o", T + 11 * DAY, [("user", "Search my notes.")])
    t = tool_row(db, "dr_o", json.dumps({"hits": [W.upper()]}, ensure_ascii=True), T + 11 * DAY + 2)
    await run(fx)
    set_content(db, t, "[Old tool output cleared to save context space]")
    upd = await run(fx)
    await withhold(client, await row_id("dr_p", pa[1]))
    async with session_scope() as s:
        for rec in (await s.scalars(select(ImportRecord).where(ImportRecord.run_id == upd["run_id"]))).all():
            assert "KOMBINATION" not in json.dumps(rec.before or {}, ensure_ascii=False)
    await imp.rollback(upd["run_id"], actor=ADMIN[0], confirm=True)
    assert await state("dr_o", t) == "held"
