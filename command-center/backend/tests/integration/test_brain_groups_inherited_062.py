"""0.6.2 option C, groups (rebuild R1): Hermes's inherited and orphan lineage markers.

Round 3 fix A (groups-paths A, HIGH): an older Hermes copies a sub-agent's _delegate_from onto its rotation; the rotation
is the sub-agent's continuation (one conversation, linked both ways), not a second delegate of the sub-agent's parent.
Round 3 skeptic groups-paths N1 (HIGH) and N2 (MEDIUM), read fail-closed (RECON-5): an orphan (Hermes deleted its
parent) whose only parent reading comes from a delegate marker (the inherited _delegate_from, or Hermes v16's
'__orphaned__' tag) is not anchored by it; its copied history joins it to the conversation it copied (J8). The data
comes from an older Hermes without uids, so no shared uid links them. Synthetic data only."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db import session_scope
from app.models import BrainWithheldSession

from .. import brain_jarvis as bj
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_withhold_identity import DAY, T, conversation, message_of, run, secret, withhold

pytestmark = pytest.mark.usefixtures("clean_db")
V16_ORPHAN_SQL = ("UPDATE sessions SET model_config = json_set(COALESCE(model_config, '{}'), '$._delegate_from', "
                  "'__orphaned__') WHERE id = ?")


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


async def group() -> dict[str, tuple[str, str]]:
    async with session_scope() as s:
        return {w.source_session_id: (w.how, w.state)
                for w in (await s.scalars(select(BrainWithheldSession))).all()}


def rotation(db: str, parent: str, child: str, started: float, config: dict) -> list[int]:
    """An older Hermes's rotating compression: the parent ends 'compression', the child copies the parent's model_config
    (here its markers) and its rows with their times (no uids)."""
    bj.change(db, "UPDATE sessions SET end_reason = 'compression' WHERE id = ?", parent)
    bj.add_session(db, child, started=started, parent=parent, model_config=config, title="Rotation")
    return bj._copy_into(db, parent, child, fresh_uid=False)


def sub_agent(db: str) -> tuple[list[int], list[int]]:
    """P, its sub-agent D (with the private answer), and D's rotation D2 that inherited _delegate_from: P."""
    conversation(db, "ia_p", T, [("user", "Plan the quarter."), ("assistant", "Delegating the numbers.")], uid=False)
    d = conversation(db, "ia_d", T + DAY, [("user", "Work out the deal value."), ("assistant", secret("zqia"))],
                     uid=False, parent="ia_p", model_config={"_delegate_from": "ia_p"})
    copies = rotation(db, "ia_d", "ia_d2", T + 2 * DAY, {"_delegate_from": "ia_p"})
    d2 = copies + [bj.add_row(db, "ia_d2", "user", "And the margin?", T + 2 * DAY + 5, uid=False)]
    return d, d2


@pytest.mark.parametrize("where", ["w_in_rotation", "w_in_sub_agent"])
async def test_a_rotation_that_inherited_delegate_from_is_one_conversation_with_its_sub_agent(client, fx, where):
    """Fix A: D2 is D's continuation (J6, both ways); P, the sub-agent's parent, joins only from the parent down."""
    d, d2 = sub_agent(fx["db"])
    await run(fx)
    sid, row = ("ia_d2", d2[1]) if where == "w_in_rotation" else ("ia_d", d[1])
    await withhold(client, (await message_of(sid, row))[0].id)
    found = await group()
    assert {"ia_d", "ia_d2"} <= set(found) and "ia_p" not in found
    assert all(state == "member" for _, state in found.values())


@pytest.mark.parametrize("where", ["w_in_orphaned_rotation", "w_in_stored_parent"])
async def test_skc3_n1_an_orphaned_rotation_with_an_inherited_delegate_marker_is_one_conversation_with_its_parent(
        client, fx, where):
    """N1: b1 holds P and its sub-agent D; then Hermes rotates D into D2 (inheriting _delegate_from: P) and D leaves
    Hermes (D2's parent becomes NULL). D2 is first seen orphaned: read as delegated under P (any marker counts with no
    parent, as in Hermes), it is still the copy of D's history, so D and D2 are one group."""
    db = fx["db"]
    d, d2 = sub_agent(db)
    bj.change(db, "DELETE FROM messages WHERE session_id = 'ia_d2'")
    bj.change(db, "DELETE FROM sessions WHERE id = 'ia_d2'")
    await run(fx)  # b1: P and D
    d2 = rotation(db, "ia_d", "ia_d2", T + 2 * DAY, {"_delegate_from": "ia_p"})
    d2.append(bj.add_row(db, "ia_d2", "user", "And the margin?", T + 2 * DAY + 5, uid=False))
    bj.change(db, "UPDATE sessions SET parent_session_id = NULL WHERE id = 'ia_d2'")
    bj.change(db, "DELETE FROM messages WHERE session_id = 'ia_d'")
    bj.change(db, "DELETE FROM sessions WHERE id = 'ia_d'")
    await run(fx)  # b2: P and the orphaned D2
    sid, row = ("ia_d2", d2[-1]) if where == "w_in_orphaned_rotation" else ("ia_d", d[1])
    await withhold(client, (await message_of(sid, row))[0].id)
    found = await group()
    assert {"ia_d", "ia_d2"} <= set(found), found
    assert found["ia_d" if sid == "ia_d2" else "ia_d2"] == ("copied_rows", "member")


@pytest.mark.parametrize("where", ["w_in_orphaned_rotation", "w_in_stored_parent"])
async def test_skc3_n2_hermes_v16_orphan_tag_never_switches_copied_history_off(client, fx, where):
    """N2: Hermes's schema migration v16 tags an untitled tool-using orphan _delegate_from '__orphaned__'. The orphaned
    rotation C2 of a stored conversation C is still C's copy (J8), whatever the tag says."""
    db = fx["db"]
    c = conversation(db, "v16_c", T, [("user", "Check the server."), ("assistant", secret("zqv16")),
                                      ("tool", "uptime 12 days")], uid=False)
    await run(fx)
    c2 = rotation(db, "v16_c", "v16_c2", T + DAY, {})
    c2.append(bj.add_row(db, "v16_c2", "user", "And the disk?", T + DAY + 5, uid=False))
    bj.change(db, "UPDATE sessions SET parent_session_id = NULL, title = NULL WHERE id = 'v16_c2'")
    bj.change(db, V16_ORPHAN_SQL, "v16_c2")
    bj.change(db, "DELETE FROM messages WHERE session_id = 'v16_c'")
    bj.change(db, "DELETE FROM sessions WHERE id = 'v16_c'")
    await run(fx)
    sid, row = ("v16_c2", c2[-1]) if where == "w_in_orphaned_rotation" else ("v16_c", c[1])
    await withhold(client, (await message_of(sid, row))[0].id)
    assert {"v16_c", "v16_c2"} <= set(await group())


async def test_controls_an_orphan_with_compressed_from_is_a_continuation_and_a_markerless_orphan_joins_by_history(
        client, fx):
    """Controls: the same orphan carrying _compressed_from (J2) is the parent's continuation; with no marker at all its
    copied history joins it (J8)."""
    db = fx["db"]
    conversation(db, "ct_p", T, [("user", "Plan the quarter.")], uid=False)
    d = conversation(db, "ct_d", T + DAY, [("user", "Work out the deal value."), ("assistant", secret("zqct"))],
                     uid=False)
    rotation(db, "ct_d", "ct_j2", T + 2 * DAY, {"_compressed_from": "ct_d"})
    rotation(db, "ct_d", "ct_j8", T + 3 * DAY, {})
    bj.change(db, "UPDATE sessions SET parent_session_id = NULL WHERE id IN ('ct_j2', 'ct_j8')")
    await run(fx)
    await withhold(client, (await message_of("ct_d", d[1]))[0].id)
    found = await group()
    assert found["ct_j2"] == ("continuation", "member") and found["ct_j8"] == ("copied_rows", "member")
    assert "ct_p" not in found
