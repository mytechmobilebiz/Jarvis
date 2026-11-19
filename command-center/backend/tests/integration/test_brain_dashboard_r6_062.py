"""0.6.2 option C (rebuild R6): what the dashboard shows of the groups, in counts and codes, never text.

- the decisions counters say how many rows of uncertain conversations the export waits on (`uncertain_waiting`);
- the holds list says how each hold's conversation joined its withheld message's group (`joined`, `joined_state`);
- the withhold preview counts the group the withhold would hold (`group`), read only, and leaves conversations
  outside the admin's scope out of every count (RECON-48).
Synthetic data only."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.db import session_scope
from app.models import BrainWithheldSession

from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_groups_d1_062 import _relinked, fx  # noqa: F401 - fx is a fixture
from .test_brain_later_062 import holds
from .test_brain_review1_062 import R19_T0, r19_plant
from .test_brain_withhold_identity import DAY, T, conversation, message_of, run, secret, withhold

pytestmark = pytest.mark.usefixtures("clean_db")


async def _recorded() -> int:
    async with session_scope() as s:
        return int(await s.scalar(select(func.count()).select_from(BrainWithheldSession)) or 0)


async def test_r6_the_counters_say_how_many_uncertain_rows_the_export_waits_on(client, fx):
    """O-D1 (the export waits): the holds list's status and the overview's decisions both carry `uncertain_waiting`,
    the count the decisions export refuses on; it falls to 0 once each uncertain row is kept or released."""
    _, ca = await _relinked(client, fx)
    await as_(client, "admin")
    status = (await client.get("/api/brain/holds")).json()["status"]
    assert status["uncertain_waiting"] == 2 and status["uncertain_conversations"] == 1, status
    overview = (await client.get("/api/brain/overview")).json()
    assert overview["decisions"]["uncertain_waiting"] == 2, overview["decisions"]
    first, second = [(await message_of("d2_c", r))[0].id for r in ca]
    assert (await client.post("/api/brain/holds/%d/keep" % first)).status_code == 200
    assert (await client.get("/api/brain/holds")).json()["status"]["uncertain_waiting"] == 1
    assert (await client.post("/api/brain/holds/%d/keep" % second)).status_code == 200
    assert (await client.get("/api/brain/holds")).json()["status"]["uncertain_waiting"] == 0


async def test_r6_the_holds_list_says_how_each_conversation_joined_its_group(client, fx):
    """The group view: a hold of a reset says 'reset', of a delegated sub-agent 'delegated' (members); a hold of a
    conversation only an uncertain link holds says how it joined and 'uncertain'."""
    db = fx["db"]
    ids = conversation(db, "j6_p", T, [("user", "Plan the offsite."), ("assistant", secret("zqj6"))],
                       end_reason="new_session")
    ra = conversation(db, "j6_r", T + DAY, [("user", "New topic: lunch.")], parent="j6_p",
                      model_config={"_reset_from": "j6_p"})
    da = conversation(db, "j6_d", T + 2 * DAY, [("user", "Sub-task: list venues.")], parent="j6_p",
                      model_config={"_delegate_from": "j6_p"})
    await run(fx)
    await withhold(client, (await message_of("j6_p", ids[1]))[0].id)
    listed = await holds(client)
    reset = listed[(await message_of("j6_r", ra[0]))[0].id]
    sub = listed[(await message_of("j6_d", da[0]))[0].id]
    assert (reset["joined"], reset["joined_state"]) == ("reset", "member"), reset
    assert (sub["joined"], sub["joined_state"]) == ("delegated", "member"), sub


async def test_r6_an_uncertain_hold_says_how_it_joined_and_that_it_is_uncertain(client, fx):
    _, ca = await _relinked(client, fx)
    listed = await holds(client)
    for r in ca:
        h = listed[(await message_of("d2_c", r))[0].id]
        assert (h["joined"], h["joined_state"], h["uncertain"], h["one_click"]) == (
            "child", "uncertain", True, False), h


async def test_r6_the_withhold_preview_counts_the_group_it_would_hold_and_writes_nothing(client, fx):
    """The withhold dialog's group: the other conversations (a reset, a delegated sub-agent; an unrelated one is not
    counted), how each would join, and their stored rows held with their conversation. Read only: no group is
    recorded by a preview."""
    db = fx["db"]
    ids = conversation(db, "g6_p", T, [("user", "Plan the offsite."), ("assistant", secret("zqg6"))],
                       end_reason="new_session")
    conversation(db, "g6_r", T + DAY, [("user", "New topic: lunch."), ("assistant", "Pasta.")], parent="g6_p",
                 model_config={"_reset_from": "g6_p"})
    conversation(db, "g6_d", T + 2 * DAY, [("user", "Sub-task: list venues.")], parent="g6_p",
                 model_config={"_delegate_from": "g6_p"})
    conversation(db, "g6_x", T + 3 * DAY, [("user", "Book the train."), ("assistant", "Booked.")])
    await run(fx)
    await as_(client, "admin")
    w = (await message_of("g6_p", ids[1]))[0]
    r = await client.get("/api/brain/messages/%d/withhold-preview" % w.id)
    assert r.status_code == 200, r.text
    assert r.json()["group"] == {"conversations": 2, "uncertain": 0, "how": {"delegated": 1, "reset": 1},
                                 "rows": 3}, r.json()["group"]
    assert await _recorded() == 0


async def test_r6_the_withhold_preview_counts_an_uncertain_conversation(client, fx):
    """Two parents (a Hermes parent in the group, a _delegate_from no backup holds): the preview counts it as
    uncertain, as the withhold then records it."""
    db = fx["db"]
    pa = conversation(db, "u6_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqu6"))])
    conversation(db, "u6_q", T + DAY, [("user", "Summarise the call.")], parent="u6_p",
                 model_config={"_delegate_from": "u6_unknown"})
    conversation(db, "u6_m", T + 2 * DAY, [("user", "Thanks.")], parent="u6_p")
    await run(fx)
    await as_(client, "admin")
    w = (await message_of("u6_p", pa[1]))[0]
    group = (await client.get("/api/brain/messages/%d/withhold-preview" % w.id)).json()["group"]
    assert (group["conversations"], group["uncertain"]) == (2, 1), group


async def test_r6_the_preview_group_never_counts_a_conversation_outside_the_admins_scope(client):
    """RECON-48: a Credit Repair conversation shares the previewed row's uid (it would join the group). The plain
    admin (no sensitive-data permission) sees no conversation of the group and no row of it counted; admin+ sees it."""
    await r19_plant("r6_cr", "credit_repair", "sarah", "Dispute letter for the client sent, balance 4410 dollars.",
                    R19_T0)
    _, gen_msg = await r19_plant("r6_gen", "supervisor", "jarvis", "A different general answer.",
                                 R19_T0 + timedelta(days=1))
    await as_(client, "admin")
    group = (await client.get("/api/brain/messages/%d/withhold-preview" % gen_msg)).json()["group"]
    assert group == {"conversations": 0, "uncertain": 0, "how": {}, "rows": 0}, group
    await as_(client, "admin+")
    group = (await client.get("/api/brain/messages/%d/withhold-preview" % gen_msg)).json()["group"]
    assert group["conversations"] == 1 and group["how"] == {"shared_uid": 1} and group["rows"] == 2, group
