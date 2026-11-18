"""0.6.2 option C, groups (rebuild R1): a withheld message is held with its conversation group (app/brain/groups.py).

O-D1: a markerless child joins its parent's group, from the parent down only; a parent that does not resolve, or two
parents, give the 'uncertain' state: its rows are held, never released in one click (a per-row release needs the typed
word), and the decisions export waits. The group is recorded for good (brain_withheld_sessions): the title trigger
hides every group conversation's title by membership alone, a rollback and a re-import keep it, and restore's
removals-only file carries it. The startup repair blanks the rollback copies of a group's rows (R9). Synthetic data
only."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select, text

from app.brain import groups as brain_groups
from app.brain import identity
from app.brain import importer as imp
from app.db import session_scope
from app.models import ArchiveSession, BrainSessionLink, BrainTombstone, BrainWithheldSession, ImportRun

from .. import brain_jarvis as bj
from ..conftest import ADMIN
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_later_062 import decided, holds, pending, state
from .test_brain_withhold_identity import DAY, T, _fresh_production, conversation, message_of, run, secret, withhold

pytestmark = pytest.mark.usefixtures("clean_db")


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


async def group() -> dict[str, tuple[str, str]]:
    """Every recorded group conversation: JARVIS session -> (how it joined, member or uncertain)."""
    async with session_scope() as s:
        return {w.source_session_id: (w.how, w.state)
                for w in (await s.scalars(select(BrainWithheldSession))).all()}


async def title(sid: str) -> str | None:
    async with session_scope() as s:
        return await s.scalar(select(ArchiveSession.title).where(ArchiveSession.source_session_id == sid))


async def archive_id(sid: str) -> int:
    async with session_scope() as s:
        return int(await s.scalar(select(ArchiveSession.id).where(ArchiveSession.source_session_id == sid)))


async def test_d1_a_markerless_child_joins_from_the_parent_down_only(client, fx):
    """W in the parent: its markerless child (Hermes parent set, no marker, the parent not ended by a compression or a
    branch) is in the group, its title hidden by membership. W in a child: the parent stays out (pending owner
    decision groups-paths B and D)."""
    db = fx["db"]
    pa = conversation(db, "d1_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqd1a"))],
                      title="Deal value")
    conversation(db, "d1_c", T + DAY, [("user", "Draft the follow-up mail."), ("assistant", "Here it is.")],
                 parent="d1_p", title="Follow-up mail")
    conversation(db, "d1_q", T + 2 * DAY, [("user", "Book the train."), ("assistant", "Booked.")], title="Train")
    cq = conversation(db, "d1_r", T + 3 * DAY, [("user", "Which seat?"), ("assistant", secret("zqd1b"))],
                      parent="d1_q", title="Seat")
    await run(fx)
    await withhold(client, (await message_of("d1_p", pa[1]))[0].id)
    found = await group()
    assert found["d1_p"] == ("withheld_here", "member") and found["d1_c"] == ("child", "member")
    assert await title("d1_c") == ""
    await withhold(client, (await message_of("d1_r", cq[1]))[0].id)
    found = await group()
    assert found["d1_r"] == ("withheld_here", "member") and "d1_q" not in found  # from the parent down only
    assert await title("d1_q") == "Train"


async def test_a_delegated_sub_agent_and_a_reset_join_and_their_links_are_recorded(client, fx):
    """J4, J5: a gateway reset is linked both ways, a delegated sub-agent from the parent down; their parent and marker
    links are recorded (gd1:marker_links_off), so a later backup whose Hermes dropped both keeps the group."""
    db = fx["db"]
    ids = conversation(db, "rd_p", T, [("user", "Plan the offsite."), ("assistant", secret("zqrd"))],
                       end_reason="new_session")
    conversation(db, "rd_r", T + DAY, [("user", "New topic: lunch.")], parent="rd_p",
                 model_config={"_reset_from": "rd_p"})
    conversation(db, "rd_d", T + 2 * DAY, [("user", "Sub-task: list venues.")], parent="rd_p",
                 model_config={"_delegate_from": "rd_p"})
    await run(fx)
    async with session_scope() as s:
        links = {(a, b, how) for a, b, how in (await s.execute(select(
            BrainSessionLink.a, BrainSessionLink.b, BrainSessionLink.how))).all()}
    assert {("rd_p", "rd_r", "reset_from"), ("rd_p", "rd_d", "delegate_from"), ("rd_p", "rd_r", "child")} <= links
    await withhold(client, (await message_of("rd_p", ids[1]))[0].id)
    assert {k: v for k, v in (await group()).items()} == {
        "rd_p": ("withheld_here", "member"), "rd_r": ("reset", "member"), "rd_d": ("delegated", "member")}
    # Hermes deletes the parent's link and the markers (a later backup): the recorded links keep the group, and a new
    # row of the reset there is held as a later backup's row of the group
    bj.change(db, "UPDATE sessions SET parent_session_id = NULL, model_config = '{}' WHERE id IN ('rd_r', 'rd_d')")
    turn = bj.add_row(db, "rd_r", "user", "Another lunch question.", T + 5 * DAY)
    await run(fx)
    assert set(await group()) == {"rd_p", "rd_r", "rd_d"}
    assert await state("rd_r", turn) == "held"


async def _relinked(client, fx) -> tuple[list[int], list[int]]:
    """W in d2_p, settled and kept; its markerless child d2_c; then Hermes re-links d2_c to a conversation no backup
    ever held: its parent does not resolve here."""
    db = fx["db"]
    pa = conversation(db, "d2_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqd2"))])
    ca = conversation(db, "d2_c", T + DAY, [("user", "Draft the follow-up mail."), ("assistant", "Here it is.")],
                      parent="d2_p")
    # The owner's flow (decided()), except that d2_c's rows stay held with their conversation (rebuild R2, D-4: the
    # central sweep holds every group row; decided() would keep them, and kept rows are not the uncertain holds here)
    await run(fx)
    await withhold(client, (await message_of("d2_p", pa[1]))[0].id)
    await bj.settle(fx, keep=False)
    child = await archive_id("d2_c")
    for mid, h in (await holds(client)).items():
        if h["session"] != child:
            assert (await client.post("/api/brain/holds/%d/keep" % mid)).status_code == 200
    assert {await state("d2_c", r) for r in ca} == {"held"}
    assert (await group())["d2_c"] == ("child", "member")
    bj.add_session(db, "d2_gone", started=T + 3 * DAY)
    bj.change(db, "UPDATE sessions SET parent_session_id = 'd2_gone' WHERE id = 'd2_c'")
    bj.change(db, "DELETE FROM sessions WHERE id = 'd2_gone'")
    await run(fx)
    return pa, ca


async def test_uncertain_a_parent_that_does_not_resolve_is_held_never_one_click_and_the_export_waits(client, fx,
                                                                                                    tmp_path):
    pa, ca = await _relinked(client, fx)
    assert (await group())["d2_c"] == ("child", "uncertain")  # never a member again
    assert [await state("d2_c", r) for r in ca] == ["held", "held"]
    listed = await holds(client)
    rows = [listed[(await message_of("d2_c", r))[0].id] for r in ca]
    assert all(h["uncertain"] and identity.UNCERTAIN_REL in h["relations"] and h["confirm_required"]
               and not h["preselected"] for h in rows)
    assert (await pending())["uncertain_waiting"] == 2
    with pytest.raises(imp.Refused, match="uncertain"):
        await imp.decisions_export(str(tmp_path / "decisions.json"))
    # never one click; a per-row release needs the typed word
    r = await client.post("/api/brain/sessions/%d/holds/release-unrelated" % await archive_id("d2_c"))
    assert r.status_code == 400 and "uncertain" in r.text
    first, second = [(await message_of("d2_c", r))[0].id for r in ca]
    assert (await client.post("/api/brain/holds/%d/release" % first)).status_code == 400
    assert (await client.post("/api/brain/holds/%d/release" % first,
                              json={"confirm": identity.CONFIRM_WORD})).status_code == 200
    assert (await client.post("/api/brain/holds/%d/keep" % second)).status_code == 200
    assert (await pending())["uncertain_waiting"] == 0
    await imp.decisions_export(str(tmp_path / "decisions.json"))
    data = json.loads((tmp_path / "decisions.json").read_text())
    assert {"anchor": data["groups"][0]["anchor"], "session": "d2_c", "how": "child", "state": "uncertain",
            "via": "d2_p"} in data["groups"]


async def test_uncertain_conflicting_parents_one_unknown_is_held_one_in_the_group_joins(client, fx):
    """Two parents: Q's Hermes parent is the withheld conversation, its _delegate_from names a conversation no backup
    holds. Q joins the group as uncertain (held), and so does its own child (uncertain spreads); a plain child of the
    withheld conversation joins as a member."""
    db = fx["db"]
    pa = conversation(db, "cp_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqcp"))])
    qa = conversation(db, "cp_q", T + DAY, [("user", "Summarise the call.")], parent="cp_p",
                      model_config={"_delegate_from": "cp_unknown"})
    ya = conversation(db, "cp_y", T + 2 * DAY, [("user", "And the next steps?")], parent="cp_q")
    conversation(db, "cp_m", T + 3 * DAY, [("user", "Thanks.")], parent="cp_p")
    await run(fx)
    await withhold(client, (await message_of("cp_p", pa[1]))[0].id)
    found = await group()
    assert found["cp_q"] == ("delegated", "uncertain") and found["cp_y"] == ("child", "uncertain")
    assert found["cp_m"] == ("child", "member")
    assert await state("cp_q", qa[0]) == "held" and await state("cp_y", ya[0]) == "held"


async def test_uncertain_members_never_move_the_decision_to_another_backup(client, tmp_path):
    """gd1:definite_off: the withhold is decided on the newest backup that wrote the group's certain conversations;
    an uncertain one written by a newer backup never moves it."""
    one, two = bj.mini(tmp_path / "one"), bj.mini(tmp_path / "two")
    pa = conversation(one["db"], "dm_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqdm"))])
    conversation(one["db"], "dm_q", T + DAY, [("user", "Summarise the call.")], parent="dm_p",
                 model_config={"_delegate_from": "dm_unknown"})
    conversation(two["db"], "dm_q", T + DAY, [("user", "Summarise the call."), ("assistant", "Done.")],
                 parent="dm_p", model_config={"_delegate_from": "dm_unknown"})
    await run(one)
    await run(two)
    async with session_scope() as s:
        shas = [identity.run_sha256(src) for src in (await s.scalars(select(ImportRun.source).order_by(
            ImportRun.id))).all()]
    await withhold(client, (await message_of("dm_p", pa[1]))[0].id)
    assert (await group())["dm_q"][1] == "uncertain"
    async with session_scope() as s:
        decided_on = await s.scalar(select(BrainTombstone.decided_on).where(
            BrainTombstone.anchor_key == "", BrainTombstone.source_session_id == "dm_p"))
    assert decided_on == shas[0] != shas[1]


async def test_d1_child_and_uncertain_members_survive_a_restore_and_the_reimport(client, fx, tmp_path):
    """The group outlives the archive: rolled back and re-imported, the child's title is hidden again at once and the
    uncertain rows are held again; restore.ps1's removals-only file carries the group to a database restored without
    it."""
    await _relinked(client, fx)
    async with session_scope() as s:
        runs = list((await s.scalars(select(ImportRun.id).where(ImportRun.status == "completed")
                                     .order_by(ImportRun.id.desc()))).all())
    out = tmp_path / "removals.json"
    await imp.decisions_export(str(out), removals_only=True)
    for run_id in runs:
        await imp.rollback(run_id, actor=ADMIN[0], confirm=True)
    assert set(await group()) == {"d2_p", "d2_c"}
    await run(fx)
    assert await title("d2_c") == "" and (await group())["d2_c"][1] == "uncertain"
    ca = [r["id"] for r in bj.rows(fx["db"], "d2_c")]
    assert {await state("d2_c", r) for r in ca} == {"held"}
    # a restore of an older dump: no group table rows, nothing imported; the file brings the group back
    await _fresh_production()
    async with session_scope() as s:
        await s.execute(text("TRUNCATE brain_withheld_sessions, brain_hidden_titles"))
    await imp.decisions_apply(str(out), None, actor=None, apply=True, removals_only=True)
    assert (await group())["d2_c"] == ("child", "uncertain")
    await run(fx)
    assert await title("d2_c") == "" and {await state("d2_c", r) for r in ca} == {"held"}


async def copies_with(mark: str) -> int:
    async with session_scope() as s:
        return int(await s.scalar(text("SELECT count(*) FROM import_records WHERE record_type = 'message' "
                                       "AND before::text LIKE '%' || :m || '%'"), {"m": mark}))


async def test_p2_the_startup_repair_blanks_a_copy_of_a_changed_then_deleted_group_row(client, fx):
    """R9 (gd1:r9_copy_session_off): a row of a conversation changed by one import and deleted by the next keeps its
    older text only in rollback copies (an update copy, which names no conversation, and the deletion copy). Once the
    conversation is in a withheld group, the startup repair blanks both, whatever wrote them."""
    db = fx["db"]
    mark = "zqp2"
    pa = conversation(db, "p2_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqp2w"))])
    ca = conversation(db, "p2_c", T + DAY, [("user", "The older text %s here." % mark), ("assistant", "Noted.")],
                      parent="p2_p")
    await run(fx)
    bj.change(db, "UPDATE messages SET content = 'A newer text.' WHERE id = ?", ca[0])
    await run(fx)
    bj.change(db, "UPDATE messages SET active = 0 WHERE id = ?", ca[0])  # rewound: the next import deletes it
    await run(fx)
    assert await message_of("p2_c", ca[0]) == []  # deleted: its update copy names a row no longer stored
    assert await copies_with(mark) >= 1  # the update copy keeps the older text
    # The group recorded without the sweep (rebuild R2, D-5: a withhold's sweep blanks these copies itself now:
    # test_skc_d_*), as restore's removals-only file or older code records it: the startup repair (R9) blanks them.
    async with session_scope() as s:
        anchor = (await message_of("p2_p", pa[1]))[0].source_key
        await brain_groups.record(s, anchor, {"p2_p": brain_groups.Member("withheld_here"),
                                              "p2_c": brain_groups.Member("child", brain_groups.MEMBER, "p2_p")})
        await s.commit()
    assert (await group())["p2_c"] == ("child", "member")
    async with session_scope() as s:
        counts = await identity.repair(s, actor="system (startup-check)", cause="startup-check")
        await s.commit()
    assert counts["group_rollback_copies_blanked"] >= 1
    assert await copies_with(mark) == 0


async def test_uncertain_hermes_relinked_a_child_to_another_known_conversation(client, fx):
    """gd1:link_parents_off: the recorded parent link and the backup name two different parents, both known here: the
    child is uncertain (the recorded links count as a parent claim)."""
    db = fx["db"]
    pa = conversation(db, "rl_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqrl"))])
    conversation(db, "rl_o", T + DAY, [("user", "Another topic.")])
    ca = conversation(db, "rl_c", T + 2 * DAY, [("user", "Draft the follow-up mail.")], parent="rl_p")
    await run(fx)
    bj.change(db, "UPDATE sessions SET parent_session_id = 'rl_o' WHERE id = 'rl_c'")
    await run(fx)
    await withhold(client, (await message_of("rl_p", pa[1]))[0].id)
    assert (await group())["rl_c"] == ("child", "uncertain")
    assert await state("rl_c", ca[0]) == "held"


async def test_a_marker_naming_a_withheld_conversation_joins_in_the_import_that_first_sees_it(client, fx):
    """sk3_graph_marker_edges_off: a later backup brings Q, whose Hermes parent is unrelated and whose _delegate_from
    names the withheld conversation: the backup's marker links Q to the group in that very import (two parents:
    uncertain, its rows held), before any link of Q is recorded."""
    db = fx["db"]
    pa = conversation(db, "me_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqme"))])
    conversation(db, "me_x", T + DAY, [("user", "Unrelated.")])
    await run(fx)
    await withhold(client, (await message_of("me_p", pa[1]))[0].id)
    qa = conversation(db, "me_q", T + 2 * DAY, [("user", "Summarise the call.")], parent="me_x",
                      model_config={"_delegate_from": "me_p"})
    await run(fx)
    assert (await group())["me_q"] == ("delegated", "uncertain")
    assert await state("me_q", qa[0]) == "held"


async def test_the_archive_lineage_links_a_child_whose_link_was_never_recorded(client, fx):
    """sk3_archive_parent_edge_off: data an earlier importer stored without the new link kinds (no recorded 'child'
    link): the archive's own parent and lineage still put the markerless child in the group."""
    db = fx["db"]
    pa = conversation(db, "al_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqal"))])
    conversation(db, "al_c", T + DAY, [("user", "Draft the follow-up mail.")], parent="al_p")
    await run(fx)
    async with session_scope() as s:
        await s.execute(text("TRUNCATE brain_session_links"))
    await withhold(client, (await message_of("al_p", pa[1]))[0].id)
    assert (await group())["al_c"] == ("child", "member")


async def test_a_withhold_in_a_delegate_never_makes_its_parent_a_withheld_family(client, fx):
    """O-D1 overreach guard (the parent side is a pending owner decision): option C records the delegate's parent link
    ('child', 'delegate_from'), but i22's families read only the links that copy rows, so a later backup's new row in
    the parent stays readable."""
    db = fx["db"]
    conversation(db, "dp_p", T, [("user", "Plan the quarter."), ("assistant", "Delegating.")])
    da = conversation(db, "dp_d", T + DAY, [("user", "Work out the deal value."), ("assistant", secret("zqdp"))],
                      parent="dp_p", model_config={"_delegate_from": "dp_p"})
    await decided(client, fx, "dp_d", da[1])
    assert "dp_p" not in await group()
    turn = bj.add_row(db, "dp_p", "user", "What about the venue?", T + 5 * DAY)
    await run(fx)
    assert await state("dp_p", turn) == "readable"


async def test_settle_finishes_a_decision_over_the_whole_group(client, fx):
    """settle_scope: Settle reads every conversation of the withhold's group, so a delegated sub-agent's row that
    repeats the withheld text is held when the decision is finished (i22's families left a delegate out)."""
    db = fx["db"]
    w = secret("zqsg")
    pa = conversation(db, "sg_p", T, [("user", "What is the deal worth?"), ("assistant", w)])
    da = conversation(db, "sg_d", T + DAY, [("user", "Summarise: %s" % w), ("assistant", "Summarised.")],
                      parent="sg_p", model_config={"_delegate_from": "sg_p"})
    await run(fx)
    await withhold(client, (await message_of("sg_p", pa[1]))[0].id)
    # rebuild R2 (D-6): held with its conversation group at the withhold already (i22 and R1: readable until Settle)
    assert await state("sg_d", da[0]) == "held"
    await bj.settle(fx, keep=False)
    assert await state("sg_d", da[0]) == "held"
    assert identity.CONTAINS in (await holds(client))[(await message_of("sg_d", da[0]))[0].id]["relations"]


async def test_the_legacy_reset_records_the_groups_of_the_withholds_it_binds(client, fx, tmp_path):
    """Legacy data (a 0.6.1 withhold, no group): the reset binds the withhold to its exact JARVIS row and records its
    group before its rollbacks, and its version 5 file carries the group."""
    db = fx["db"]
    pa = conversation(db, "lg_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqlg"))])
    conversation(db, "lg_c", T + DAY, [("user", "Draft the follow-up mail.")], parent="lg_p")
    first = await run(fx)
    await withhold(client, (await message_of("lg_p", pa[1]))[0].id)
    async with session_scope() as s:
        await s.execute(text("UPDATE import_runs SET importer_version = '1.0.4' WHERE id = :r"),
                        {"r": first["run_id"]})
        await s.execute(text(
            "UPDATE brain_tombstones SET source_session_id = '', source_row_id = '', role = '', tool_call_id = '', "
            "message_uid = '', uid_known = false, decided_on = '', bound = false, source_at = NULL "
            "WHERE reason = :r AND anchor_key = ''"), {"r": identity.WITHHELD_BY_ADMIN})
        await s.execute(text("INSERT INTO brain_maintenance (name, detail) VALUES ('legacy_brain_data', "
                             "'{\"found_by\": \"test\"}'::jsonb)"))
        await s.execute(text("TRUNCATE brain_withheld_sessions"))  # 0.6.1 kept no group
    out = tmp_path / "reset.json"
    await imp.legacy_reset(str(out), actor=ADMIN[0], confirm=True)
    assert (await group()) == {"lg_p": ("withheld_here", "member"), "lg_c": ("child", "member")}
    assert {(g["session"], g["how"]) for g in json.loads(out.read_text())["groups"]} == {
        ("lg_p", "withheld_here"), ("lg_c", "child")}


async def test_skc_d_the_older_text_of_a_group_row_changed_then_deleted_keeps_no_text_in_any_rollback_copy(client,
                                                                                                         fx):
    """gd1:updated_copies_gone_off (rebuild R2): a row of a group conversation changed by one import and deleted by the
    next keeps its older text in its update copy (which names a row no longer stored) and in its deletion copy. The
    withhold's sweep itself (no start-up repair) blanks both: the update copy is found through the deletion copy."""
    db = fx["db"]
    mark = "zqskcd"
    pa = conversation(db, "sd_p", T, [("user", "What is the deal worth?"), ("assistant", secret("zqsdw"))])
    ca = conversation(db, "sd_c", T + DAY, [("user", "The older text %s here." % mark), ("assistant", "Noted.")],
                      parent="sd_p")
    await run(fx)
    bj.change(db, "UPDATE messages SET content = 'A newer text, %s too.' WHERE id = ?" % (mark + "n"), ca[0])
    await run(fx)
    bj.change(db, "UPDATE messages SET active = 0 WHERE id = ?", ca[0])  # rewound: the next import deletes it
    await run(fx)
    assert await message_of("sd_c", ca[0]) == []
    assert await copies_with(mark) >= 2  # the update copy (older text) and the deletion copy (newer text)
    await withhold(client, (await message_of("sd_p", pa[1]))[0].id)
    assert (await group())["sd_c"] == ("child", "member")
    assert await copies_with(mark) == 0
