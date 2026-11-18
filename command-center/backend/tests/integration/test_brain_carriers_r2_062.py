"""0.6.2 option C (rebuild R3): carriers outside every withheld group, held per row (cr2:scan_longest64,
titles_longest64, items_longest64, preview_narrow, preview_items_case, preview_title_case).

A withhold and every import with the withheld text in hand scan every readable stored row (no prefilter by the
withheld text's 64 longest words): a row anywhere that carries it (a reply quote of part of it, another case, a
derived form) is held on its own (a pending stone in that withhold's group, listed, released per row with the typed
word); a conversation title and an imported memory item that carry it are hidden and quarantined. The withhold
preview reads another case. Synthetic data only."""

from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import select

from app.brain import identity, review
from app.db import session_scope
from app.models import ArchiveSession, BrainHiddenTitle, MemoryItem

from .. import brain_jarvis as bj
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_later_062 import holds, state
from .test_brain_withhold_identity import DAY, T, conversation, message_of, run, withhold

pytestmark = pytest.mark.usefixtures("clean_db")

# A long withheld text of short words only (the 64-longest-words prefilter missed rows that held none of its longest)
W_LONG = ("Die Pläne für das Lager in Halle sieben liegen ab Montag im grauen Schrank, der Schlüssel dazu "
          "hängt bei Frau Okonkwo am Brett, Code Reiher-3307, und niemand außer uns darf das wissen.")
W_SHORT_WORDS = "we go to the old mill at ten on the day of the fair and we do not say why or how"
# A W of more than 64 words whose middle part is short words only: none of its 64 longest words is in that part
W_MANY = (" ".join("Verwaltungsvorschrift%02d" % i for i in range(70)) + " and " + W_SHORT_WORDS + " "
          + " ".join("Grundbuchauszug%02d" % i for i in range(10)))


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


async def row_id(sid: str, source_row: int) -> int:
    return (await message_of(sid, source_row))[0].id


def memory_entry(fx: dict[str, str], text: str) -> None:
    path = os.path.join(fx["profile"], "memories", "MEMORY.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write("§\n%s\n" % text)


async def title_of(sid: str) -> str:
    async with session_scope() as s:
        return (await s.scalar(select(ArchiveSession.title).where(ArchiveSession.source_session_id == sid))) or ""


@pytest.mark.parametrize("when", ["stored_before_the_withhold", "stored_after_the_withhold"])
async def test_skc_a_native_partial_reply_quote_of_a_long_w_is_held_wherever_it_is_found(client, fx, when):
    """A Discord/Telegram-style native reply quoting a middle part of a long W (more than 64 words; the part holds
    none of its 64 longest), in a conversation with no link to the withheld one: held on its own (a carrier: 'part of the withheld text', the typed word needed), whether it was
    stored before the withhold (the withhold's full scan) or arrives after it (the import with W in hand); the
    conversation's other row stays readable."""
    db = fx["db"]
    pa = conversation(db, "cq_p", T, [("user", "Where are the plans?"), ("assistant", W_MANY)])
    quote = '[Replying to: "%s"]\n\nWhich mill exactly?' % W_SHORT_WORDS[6:60]
    assert not any(x in quote for x in sorted(W_MANY.split(), key=len)[-64:])
    if when == "stored_before_the_withhold":
        oa = conversation(db, "cq_o", T + 9 * DAY, [("user", quote), ("assistant", "The grey one.")])
    await run(fx)
    await withhold(client, await row_id("cq_p", pa[1]))
    if when == "stored_after_the_withhold":
        oa = conversation(db, "cq_o", T + 9 * DAY, [("user", quote), ("assistant", "The grey one.")])
        await run(fx)
    assert await state("cq_o", oa[0]) == "held"
    assert await state("cq_o", oa[1]) == "readable"
    h = (await holds(client))[await row_id("cq_o", oa[0])]
    # held on its own: by the full scan (a carrier), or by the later backup's own quote rule (i22's LATER_HOLD)
    assert h["rule"] in (identity.CARRIER_HOLD, identity.LATER_HOLD) and identity.PART_OF in h["relations"]
    assert h["confirm_required"]
    # the next import keeps it held (its own stone), the untyped release is refused
    await run(fx)
    assert await state("cq_o", oa[0]) == "held"
    r = await client.post("/api/brain/holds/%d/release" % await row_id("cq_o", oa[0]))
    assert r.status_code in (400, 422), r.text


async def test_p2_a_title_and_an_imported_memory_item_of_short_words_are_carriers_too(client, fx):
    """A W of short words: another conversation's title derived from it (Hermes: the first line, cut with '…') is
    hidden for good, and an imported memory entry that quotes it is quarantined (admin only, out of every agent's
    reach); an unrelated title and entry stay."""
    db = fx["db"]
    pa = conversation(db, "ct_p", T, [("user", "What is the plan?"), ("assistant", W_SHORT_WORDS)])
    conversation(db, "ct_o", T + 3 * DAY, [("user", "Remind me later."), ("assistant", "Sure.")],
                 title=W_SHORT_WORDS[:44].rstrip() + "…")
    conversation(db, "ct_u", T + 4 * DAY, [("user", "Book the train."), ("assistant", "Booked.")],
                 title="Train to Bern")
    memory_entry(fx, "Plan noted: %s." % W_SHORT_WORDS.upper())
    memory_entry(fx, "Invoices go out on the first working day.")
    await run(fx)
    async with session_scope() as s:
        items = {i.body: i for i in (await s.scalars(select(MemoryItem).where(MemoryItem.origin == "import"))).all()}
    assert any(W_SHORT_WORDS.upper() in b for b in items)
    await withhold(client, await row_id("ct_p", pa[1]))
    assert await title_of("ct_o") == ""
    assert await title_of("ct_u") == "Train to Bern"
    async with session_scope() as s:
        hidden = set((await s.scalars(select(BrainHiddenTitle.source_session_id))).all())
        assert "ct_o" in hidden and "ct_u" not in hidden
        found = {i.body: i for i in (await s.scalars(select(MemoryItem).where(MemoryItem.origin == "import"))).all()}
    for body, item in found.items():
        carrier = W_SHORT_WORDS.upper() in body
        assert (item.status == "quarantined" and bool((item.flags or {}).get(review.CARRIER_ITEM_FLAG))) is carrier, body
    # a re-import never shows the title again
    await run(fx)
    assert await title_of("ct_o") == ""


async def test_oci_the_withhold_preview_reads_another_case(client, fx):
    """The withhold preview lists, in another case: the family row (identical_text_rows), a carrier in another
    conversation (carrier_rows: a JSON tool result), the imported memory item (items_with_this_text) and the
    titles that carry it; ids and codes only, never text."""
    db = fx["db"]
    pa = conversation(db, "cp_p", T, [("user", "Where are the plans?"), ("assistant", W_LONG),
                                      ("user", "So: " + W_LONG.upper())])
    oa = conversation(db, "cp_o", T + 2 * DAY, [("user", "Search my notes"),
                                                 ("assistant", json.dumps({"hits": [W_LONG.upper()]}))],
                      title=W_LONG.upper()[:40] + "…")
    memory_entry(fx, "Note: " + W_LONG.upper())
    await run(fx)
    await as_(client, "admin")
    r = await client.get("/api/brain/messages/%d/withhold-preview" % await row_id("cp_p", pa[1]))
    assert r.status_code == 200, r.text
    out = r.json()
    assert W_LONG not in r.text and W_LONG.upper() not in r.text
    assert await row_id("cp_p", pa[2]) in {x["id"] for x in out["identical_text_rows"]}
    carriers = {x["id"]: x for x in out["carrier_rows"]}
    assert identity.CONTAINS in carriers[await row_id("cp_o", oa[1])]["relations"]
    assert len(out["items_with_this_text"]) == 1
    assert out["titles_with_this_text"] >= 1
    # and the withhold holds what the preview listed
    await withhold(client, await row_id("cp_p", pa[1]))
    assert await state("cp_o", oa[1]) == "held" and await state("cp_o", oa[0]) == "readable"
    assert await title_of("cp_o") == ""


async def test_skc_an_unrelated_conversation_stays_readable_and_untouched(client, fx):
    """Control: nothing of W anywhere else: every other row, title and memory item stays as it was."""
    db = fx["db"]
    pa = conversation(db, "cu_p", T, [("user", "Where are the plans?"), ("assistant", W_LONG)])
    oa = conversation(db, "cu_o", T + 5 * DAY, [("user", "Book the train to Bern."),
                                                 ("assistant", json.dumps({"ok": True, "seat": "14B"}))],
                      title="Train to Bern")
    memory_entry(fx, "Invoices go out on the first working day.")
    await run(fx)
    await withhold(client, await row_id("cu_p", pa[1]))
    assert [await state("cu_o", r) for r in oa] == ["readable", "readable"]
    assert await title_of("cu_o") == "Train to Bern"
    async with session_scope() as s:
        assert not [i for i in (await s.scalars(select(MemoryItem))).all() if i.status == "quarantined"]
