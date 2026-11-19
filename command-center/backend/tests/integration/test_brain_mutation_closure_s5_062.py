"""0.6.2 option C rebuild S5: closing tests for guards the rebuild's mutation drivers had not caught on their own
(reconstruction-check.md, R7 gaps): rel3:imp_carriers_group_skip, rel3:flip_any_anchor, r3:release_on_evidence_carrier_off
and the rebuild's stand-in for the round-3 crafted-state test of c2:imp_stored_none_every_off. Each test is killed by
its mutant (progress/S5-mutation/s5mut.py). Synthetic data only."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.brain import identity
from app.db import session_scope
from app.models import ArchiveMessage

from .. import brain_jarvis as bj
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_derived3_062 import W, row_id, set_content, tool_row
from .test_brain_later_062 import holds, state
from .test_brain_withhold_identity import DAY, T, conversation, run, secret, withhold

pytestmark = pytest.mark.usefixtures("clean_db")

TYPED = {"confirm": identity.CONFIRM_WORD}


async def readable_with(words: str) -> list[int]:
    """The readable stored rows whose text holds these words (the archive's message rows)."""
    async with session_scope() as s:
        return list((await s.scalars(select(ArchiveMessage.id).where(
            ArchiveMessage.withheld.is_(False), ArchiveMessage.content.contains(words)))).all())


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


async def test_s5_a_release_of_a_carrier_hold_covers_the_content_it_was_compared_on(client, fx):
    """r3:release_on_evidence_carrier_off (RECON-20 for a carrier hold): a tool row of an unlinked conversation that
    holds W is held on its own (CARRIER_HOLD, decided on the backup it was compared on). JARVIS then rewrites that row
    in place (still with W in it) and a later backup is imported while it is held. The admin's typed release covers
    the content the hold was compared on only: the next import holds the row again, never readable with W."""
    db = fx["db"]
    pa = conversation(db, "rec_p", T, [("user", "Where is the key?"), ("assistant", W)])
    conversation(db, "rec_o", T + 5 * DAY, [("user", "What did I say about the archive?")])
    t = tool_row(db, "rec_o", "Earlier you wrote: %s" % W, T + 5 * DAY + 10)
    await run(fx)
    await withhold(client, await row_id("rec_p", pa[1]))
    assert await state("rec_o", t) == "held"
    set_content(db, t, "Rewritten in place. Earlier you wrote: %s Nothing else." % W)
    await run(fx)
    mid = await row_id("rec_o", t)
    assert (await holds(client))[mid]["rule"] == identity.CARRIER_HOLD
    r = await client.post("/api/brain/holds/%d/release" % mid, json=TYPED)
    assert r.status_code == 200, r.text
    await run(fx)
    assert await state("rec_o", t) == "held"
    assert await readable_with("Möwe-5512") == []


async def test_s5_a_typed_release_of_an_uncertain_row_after_a_later_withhold_is_held_again_by_the_next_import(
        client, fx):
    """rel3:imp_carriers_group_skip on its own (the round-3 importer's carrier pass, importer.py:1922; in the rebuild
    every import's sweep runs review.hold_carriers with the backup's withheld texts in hand): X is a row of a
    conversation an uncertain link puts in W1's group (its Hermes parent is W1's conversation, its _delegate_from
    names one no backup holds) and carries W2. The group sweep never compares an uncertain row with the other withheld
    texts (groups.hold_uncertain), so after the admin's typed release of X under W1's group only the carrier pass holds
    it for W2: the next import holds it again, never readable with W2. A row of a certain member is held for W2 by the
    group sweep too (test_rel3_typed_release_after_a_later_withhold_is_held_again_by_the_next_import, the double)."""
    db = fx["db"]
    w2 = secret("zqs5ug")
    pa = conversation(db, "s5u_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqs5ua"))])
    qa = conversation(db, "s5u_q", T + DAY, [("user", "Forward this: %s" % w2), ("assistant", "Forwarded.")],
                      parent="s5u_p", model_config={"_delegate_from": "s5u_unknown"})
    wa = conversation(db, "s5u_w", T + 5 * DAY, [("user", "And the other deal?"), ("assistant", w2)])
    await run(fx)
    await withhold(client, await row_id("s5u_p", pa[1]))
    assert await state("s5u_q", qa[0]) == "held"
    await withhold(client, await row_id("s5u_w", wa[1]))
    x = await row_id("s5u_q", qa[0])
    assert (await holds(client))[x]["uncertain"]
    r = await client.post("/api/brain/holds/%d/release" % x, json=TYPED)
    assert r.status_code == 200, r.text
    await run(fx)
    assert await state("s5u_q", qa[0]) == "held"
    assert await readable_with("zqs5ug") == []
