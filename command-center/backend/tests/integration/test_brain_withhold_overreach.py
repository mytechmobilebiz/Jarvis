"""JARVIS Brain 0.6.2: what a withhold must never take (spec 18, 19.5).

The owner's rule: never withhold an unrelated message merely because its text matches. Each test withholds a message,
then drives JARVIS (Hermes) through the change that could make another row look like it (re-imports, compaction,
branches, rotation, backfilled uids, collisions, purges) and checks that the other row stays readable to every reader
that may read it (archive search and the conversation for operators and admins, with and without the sensitive-data
permission; the Supervisor's history; promotion) and that no stone names it. The last test lists the outcomes that
are withheld on purpose (fail-closed), each as documented. Synthetic data only.
"""

from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import select

from app.bootstrap import check_withheld_copies
from app.brain import identity, review, store
from app.brain import importer as imp
from app.db import session_scope
from app.models import ArchiveMessage, ArchiveSession, MemoryItem

from .. import brain_jarvis as bj
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_identity_migration import _fx, scratch  # noqa: F401 - scratch is a fixture
from .test_brain_withhold_identity import DAY, T, conversation, message_of, records, run, secret, stones, withhold

pytestmark = pytest.mark.usefixtures("clean_db")

PEOPLE = ("operator", "operator+", "admin", "admin+")  # viewers never read the archive (spec 3)


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


async def readable_everywhere(client, sid: str, source_id: int, words: str, mark: str) -> ArchiveMessage:
    """The stored row of JARVIS row `source_id` holds `words`, is not withheld, no stone names it, and every reader
    that may read it does: archive search and its conversation for every role that reads the archive, the
    Supervisor's history, and promote (identity.matches_withheld says no)."""
    found = [m for m in await message_of(sid, source_id) if not m.withheld]
    assert len(found) == 1, (sid, source_id, [(m.id, m.withheld) for m in await message_of(sid, source_id)])
    row = found[0]
    assert row.content == words
    assert row.source_key not in await stones()
    async with session_scope() as s:
        sess = await s.get(ArchiveSession, row.session_id)
        assert not await identity.matches_withheld(s, row, sess)
        hits, _ = await store.AgentMemory(s, "supervisor").history(mark, limit=50)
    assert row.id in [h.message.id for h in hits]
    for who in PEOPLE:
        await as_(client, who)
        r = await client.get("/api/brain/search", params={"q": mark, "scope": "archive", "limit": 50})
        assert row.id in [h["message"]["id"] for h in r.json()["archive"]["hits"]], who
        assert words in (await client.get("/api/brain/sessions/%d" % row.session_id)).text, who
    return row


async def held_for_review_only(client, fx: dict[str, str], sid: str, source_id: int, words: str, mark: str, *,
                               preselected: bool | None = None) -> None:
    """0.6.2 plan 5.3: a new row of a withheld conversation family in a later backup that is neither certain nor a
    safe copy is held for review (fail-safe), never withheld for good: its stone is pending ('new in a later backup',
    with its computed evidence), it is listed for an admin, and a release (typed when it is, holds or quotes the
    withheld text: plan 4.6) brings its text back at the next import, readable everywhere."""
    [m] = await message_of(sid, source_id)
    st = (await stones())[m.source_key]
    assert (m.withheld, m.content, st.review, st.via) == (True, "", identity.PENDING, identity.LATER_HOLD), (
        sid, source_id, st.via, st.review)
    await as_(client, "admin")
    [hold] = [h for h in (await client.get("/api/brain/holds")).json()["holds"] if h["message"] == m.id]
    if preselected is not None:
        assert hold["preselected"] is preselected, hold
    body = {"confirm": identity.CONFIRM_WORD} if hold["confirm_required"] else None
    r = await client.post("/api/brain/holds/%d/release" % m.id, json=body)
    assert r.status_code == 200, r.text
    await run(fx)
    await readable_everywhere(client, sid, source_id, words, mark)


async def withhold_leaving(client, sid: str, source_id: int, *readable_ids: int) -> None:
    """The admin withholds a row and unticks, in the dialog, the stored rows she knows are other messages (0.6.2
    plan 4.5: rows that equal or contain the withheld text are listed pre-ticked; unticking writes 'left readable')."""
    [m] = await message_of(sid, source_id)
    others = [r.id for i in readable_ids for r in await message_of(sid, i)]
    await as_(client, "admin")
    r = await client.post("/api/brain/messages/%d/withhold" % m.id, json={"leave_readable": others})
    assert r.status_code == 200, r.text


async def withheld_row(sid: str, source_id: int) -> ArchiveMessage:
    [m] = await message_of(sid, source_id)
    assert m.withheld and m.content == ""
    return m


def _two_turns(db: str, sid: str, start: float, answer: str, **kw) -> list[int]:
    return conversation(db, sid, start, [("user", "Please summarise the vendor situation."), ("assistant", answer)],
                        **kw)


# --- O1-O5: same or similar words are not the same message ------------------------------------------------------


async def test_o1_identical_text_in_another_conversation(client, fx):
    mark = "zqoone62"
    words = "Thanks, that is all for today (%s)." % mark
    a = conversation(fx["db"], "o1_a", T, [("user", words), ("assistant", "Glad to help.")])
    b = conversation(fx["db"], "o1_b", T + DAY, [("user", words), ("assistant", "You are welcome.")])
    await run(fx)
    [target] = await message_of("o1_a", a[0])
    # Even ticked, "identical text" covers the conversation and its continuations and branches only.
    await as_(client, "admin")
    r = await client.post("/api/brain/messages/%d/withhold" % target.id, json={"include_identical": True})
    assert r.status_code == 200 and r.json()["identical_withheld"] == 0, r.text
    bj.hermes_compact(fx["db"], "o1_a", head_n=1, tail_n=1, ts=T + 100)  # copies in the withheld conversation
    await run(fx)
    await run(fx)
    await readable_everywhere(client, "o1_b", b[0], words, mark)


async def test_o2_similar_text(client, fx):
    mark = "zqotwo62"
    ids = conversation(fx["db"], "o2_a", T, [("user", "Give me the headline number."), ("assistant", secret(mark)),
                                             ("user", "And the other one?"),
                                             ("assistant", secret(mark[:-1] + "x"))])
    other = _two_turns(fx["db"], "o2_b", T + DAY, secret(mark[:-1] + "y"))
    await run(fx)
    await withhold(client, (await message_of("o2_a", ids[1]))[0].id)
    bj.hermes_compact(fx["db"], "o2_a", head_n=1, tail_n=4, ts=T + 100)  # the similar answer is copied too
    await run(fx)
    similar = [r for r in bj.rows(fx["db"], "o2_a") if r["content"] == secret(mark[:-1] + "x") and r["active"]]
    # the stored similar answer stays as it was (plan 5.2); its copy, new in this later backup with another time, is
    # held for review only (plan 5.3), never withheld for good
    await held_for_review_only(client, fx, "o2_a", similar[0]["id"], secret(mark[:-1] + "x"), mark[:-1] + "x")
    await readable_everywhere(client, "o2_a", ids[3], secret(mark[:-1] + "x"), mark[:-1] + "x")
    await readable_everywhere(client, "o2_b", other[1], secret(mark[:-1] + "y"), mark[:-1] + "y")


async def test_o3_identical_text_stored_before_the_withhold(client, fx):
    """The admin saw the same words stored twice and withheld one, unticking the other in the dialog (0.6.2 plan 4.5;
    the 'admin saw it' guard is gone): it stays, through re-imports and a later backup (plan 5.2); the copy a
    compaction makes of it with another time is held for review only (plan 5.3)."""
    mark = "zqothree62"
    ids = conversation(fx["db"], "o3", T, [("user", "Give me the headline number."), ("assistant", secret(mark)),
                                           ("user", "Say it again?"), ("assistant", secret(mark))])
    await run(fx)
    await withhold_leaving(client, "o3", ids[1], ids[3])
    await run(fx)
    bj.add_row(fx["db"], "o3", "user", "Thanks.", T + 10)
    await run(fx)
    await readable_everywhere(client, "o3", ids[3], secret(mark), mark)
    bj.hermes_compact(fx["db"], "o3", head_n=1, tail_n=5, ts=T + 100)
    await run(fx)
    copies = [r for r in bj.rows(fx["db"], "o3") if r["content"] == secret(mark) and r["active"]]
    by_uid = {r["message_uid"]: r for r in copies}
    original_uid = bj.rows(fx["db"], "o3")[3]["message_uid"]
    await readable_everywhere(client, "o3", ids[3], secret(mark), mark)
    await held_for_review_only(client, fx, "o3", by_uid[original_uid]["id"], secret(mark), mark)


async def test_o4_independent_repeat_own_uid(client, fx):
    """A later, non-adjacent repeat with its own uid, the withheld message's uid known: new in a later backup of the
    withheld family, so held for review (0.6.2 plan 5.3; the independence guard is gone, plan 4.3), pre-selected for
    release (its own uid, another time), never withheld for good."""
    mark = "zqofour62"
    ids = _two_turns(fx["db"], "o4", T, secret(mark))
    await run(fx)
    await withhold(client, (await message_of("o4", ids[1]))[0].id)
    bj.add_row(fx["db"], "o4", "user", "Next week: the same question.", T + DAY)
    later = bj.add_row(fx["db"], "o4", "assistant", secret(mark), T + DAY + 1)
    await run(fx)
    await run(fx)
    await held_for_review_only(client, fx, "o4", later, secret(mark), mark, preselected=True)


async def test_o5_parallel_tool_results(client, fx):
    """Two identical tool results of two calls (different tool_call_id), next to each other."""
    mark = "zqofive62"
    words = "HTTP 200 OK from the %s health check" % mark
    bj.add_session(fx["db"], "o5", started=T)
    bj.add_row(fx["db"], "o5", "user", "Check both sites.", T + 1)
    first = bj.add_row(fx["db"], "o5", "tool", words, T + 2, tool_call_id="call_1", tool_name="http")
    second = bj.add_row(fx["db"], "o5", "tool", words, T + 3, tool_call_id="call_2", tool_name="http")
    await run(fx)
    await withhold_leaving(client, "o5", first, second)  # listed pre-ticked (plan 4.5): the admin unticks it
    await run(fx)
    await readable_everywhere(client, "o5", second, words, mark)


# --- O6-O8: keys and uids that change ------------------------------------------------------------------------------


async def test_o6_unrelated_row_taking_the_key(client, fx):
    """C2: an unrelated row takes the withheld message's uid (so its key): it is never stored under, or withheld by,
    that key; its older row keeps its text."""
    mark = "zqosix62"
    unrelated = "Unrelated words about the team lunch (%s)." % mark
    uids = [bj.new_uid(), bj.new_uid()]
    bj.add_session(fx["db"], "o6", started=T)
    r164 = bj.add_row(fx["db"], "o6", "user", unrelated, T + 1, uid=uids[0])
    r165 = bj.add_row(fx["db"], "o6", "assistant", secret("zqosixw62"), T + 2, uid=uids[1])
    await run(fx)
    await withhold(client, (await message_of("o6", r165))[0].id)
    bj.change(fx["db"], "UPDATE messages SET message_uid = ? WHERE id = ?", uids[1], r164)
    await run(fx)
    await run(fx)
    await readable_everywhere(client, "o6", r164, unrelated, mark)


async def test_o7_uid_collision_other_role(client, fx):
    """A user row sharing the withheld assistant answer's uid, even with the same words, is another message."""
    mark = "zqoseven62"
    shared = bj.new_uid()
    bj.add_session(fx["db"], "o7", started=T)
    bj.add_row(fx["db"], "o7", "user", "Read it back to me.", T + 1)
    answer = bj.add_row(fx["db"], "o7", "assistant", secret(mark), T + 2, uid=shared)
    echo = bj.add_row(fx["db"], "o7", "user", secret(mark), T + 3, uid=shared)
    await run(fx)
    await as_(client, "admin")
    [target] = await message_of("o7", answer)
    preview = (await client.get("/api/brain/messages/%d/withhold-preview" % target.id)).json()
    assert preview["same_message_rows"] == [] and preview["held_rows"] == []
    assert [r["id"] for r in preview["identical_text_rows"]] == [(await message_of("o7", echo))[0].id]
    await withhold_leaving(client, "o7", answer, echo)  # the echo: listed pre-ticked, unticked (plan 4.5)
    bj.hermes_compact(fx["db"], "o7", head_n=0, tail_n=2, ts=T + 100)
    await run(fx)
    copies = [r for r in bj.rows(fx["db"], "o7") if r["role"] == "user" and r["content"] == secret(mark)
              and r["active"]]
    assert copies[0]["id"] != echo
    await readable_everywhere(client, "o7", echo, secret(mark), mark)  # the stored row, kept (plan 5.2)
    # its copy (the shared uid, another time): never certain, held for review only (plan 5.3)
    await held_for_review_only(client, fx, "o7", copies[0]["id"], secret(mark), mark)


async def test_o8_twins_of_readable_messages(client, fx):
    """A uid backfill re-keys a conversation where nothing is withheld (a withhold elsewhere): every row keeps its
    text, the archive is not duplicated and no stone names any of them."""
    mark = "zqoeight62"
    elsewhere = _two_turns(fx["db"], "o8_w", T, secret("zqoeightw62"))
    ids = conversation(fx["db"], "o8", T + DAY, [("user", "What did the vendor say? (%s)" % mark),
                                                 ("assistant", "They agreed to the terms (%s)." % mark)], uid=False)
    await run(fx)
    await withhold(client, (await message_of("o8_w", elsewhere[1]))[0].id)
    before = {m.id for i in ids for m in await message_of("o8", i)}
    bj.backfill_uids(fx["db"], "o8")
    second = await run(fx)
    assert second["report"]["keys_adopted"] == len(ids)
    assert {m.id for i in ids for m in await message_of("o8", i)} == before
    await readable_everywhere(client, "o8", ids[0], "What did the vendor say? (%s)" % mark, mark)
    await readable_everywhere(client, "o8", ids[1], "They agreed to the terms (%s)." % mark, mark)


# --- O9-O13: other messages around it, children, delegated sessions, compacted repeats ----------------------------


async def test_o9_paired_tool_result_and_summary(client, fx):
    mark = "zqonine62"
    bj.add_session(fx["db"], "o9", started=T)
    bj.add_row(fx["db"], "o9", "user", "Run the vendor check.", T + 1)
    call = bj.add_row(fx["db"], "o9", "assistant", secret("zqoninew62"), T + 2)
    bj.change(fx["db"], "UPDATE messages SET tool_calls = ? WHERE id = ?",
              json.dumps([{"id": "call_9", "type": "function", "function": {"name": "check", "arguments": "{}"}}]),
              call)
    result = bj.add_row(fx["db"], "o9", "tool", "Vendor check result %s: all good." % mark, T + 3,
                        tool_call_id="call_9", tool_name="check")
    after = bj.add_row(fx["db"], "o9", "assistant", "Everything is in order (%s)." % mark, T + 4)
    await run(fx)
    await withhold(client, (await message_of("o9", call))[0].id)
    await readable_everywhere(client, "o9", result, "Vendor check result %s: all good." % mark, mark)
    await readable_everywhere(client, "o9", after, "Everything is in order (%s)." % mark, mark)
    bj.hermes_compact(fx["db"], "o9", head_n=0, tail_n=0, ts=T + 100)
    await run(fx)
    summary = [r for r in bj.rows(fx["db"], "o9") if r["content"].startswith("[CONTEXT COMPACTION]")]
    for source_id in (result, after):  # archived, still readable (stored rows of the family are kept: plan 5.2)
        assert not (await message_of("o9", source_id))[0].withheld
    # the summary is new in this later backup: held for review only (plan 5.3), never withheld for good
    await held_for_review_only(client, fx, "o9", summary[0]["id"], summary[0]["content"], "CONTEXT COMPACTION")


async def test_o10_new_turns_of_children(client, fx):
    """A rotation child carries the withheld answer on (withheld) and then says new things, an identical repeat with
    its own uid included: new rows of the withheld family in a later backup, held for review only (0.6.2 plan 5.3),
    never withheld for good; a release brings each back."""
    mark = "zqoten62"
    ids = _two_turns(fx["db"], "o10", T, secret(mark))
    await run(fx)
    await withhold(client, (await message_of("o10", ids[1]))[0].id)
    bj.rotate(fx["db"], "o10", "o10_child", started_at=T + DAY)
    new = bj.add_row(fx["db"], "o10_child", "user", "A new question about the budget (%s)." % mark, T + DAY + 10)
    repeat = bj.add_row(fx["db"], "o10_child", "assistant", secret(mark), T + DAY + 20)
    await run(fx)
    carried = [r for r in bj.rows(fx["db"], "o10_child") if r["content"] == secret(mark) and r["id"] != repeat]
    for c in carried:
        assert (await message_of("o10_child", c["id"]))[0].withheld
    await held_for_review_only(client, fx, "o10_child", new, "A new question about the budget (%s)." % mark, mark)
    await held_for_review_only(client, fx, "o10_child", repeat, secret(mark), mark, preselected=True)


async def test_o11_delegated_session(client, fx):
    """A subagent (delegated) session quoting the parent with copied timestamps and fresh uids is not lineage."""
    mark = "zqoeleven62"
    ids = _two_turns(fx["db"], "o11", T, secret(mark))
    await run(fx)
    await withhold(client, (await message_of("o11", ids[1]))[0].id)
    bj.add_session(fx["db"], "o11_sub", started=T + DAY, parent="o11", model_config={"_delegate_from": "o11"})
    copied = bj._copy_into(fx["db"], "o11", "o11_sub", fresh_uid=True)
    await run(fx)
    async with session_scope() as s:
        assert (await s.scalar(select(ArchiveSession.lineage).where(
            ArchiveSession.source_session_id == "o11_sub"))) == "delegated"
    await readable_everywhere(client, "o11_sub", copied[1], secret(mark), mark)


async def test_o12_restricted_cli_branch_does_not_withhold_the_parent(client, fx):
    """The inverse of identity-set's D10: a CLI branch (same uids) becomes restricted; its parent's rows stay."""
    mark = "zqotwelve62"
    ids = _two_turns(fx["db"], "o12", T, "The plan for the %s launch is ready." % mark)
    bj.cli_branch(fx["db"], "o12", "o12_child", started_at=T + DAY)
    bj.add_row(fx["db"], "o12_child", "user", "Her SSN is 219-09-9999", T + DAY + 10)
    await run(fx)
    async with session_scope() as s:
        child = await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == "o12_child"))
    assert child.sensitivity == "restricted"
    await run(fx)
    await check_withheld_copies()
    await readable_everywhere(client, "o12", ids[1], "The plan for the %s launch is ready." % mark, mark)
    assert await stones() == {}


async def test_o13_independent_repeat_first_seen_compacted(client, fx):
    mark = "zqothirteen62"
    ids = _two_turns(fx["db"], "o13", T, secret(mark))
    await run(fx)
    await withhold(client, (await message_of("o13", ids[1]))[0].id)
    bj.add_row(fx["db"], "o13", "user", "Months later, the same question.", T + 30 * DAY)
    archived = bj.add_row(fx["db"], "o13", "assistant", secret(mark), T + 30 * DAY + 1, active=0, compacted=1)
    await run(fx)
    # 0.6.2 plan 5.3 (the independence guard is gone): new in a later backup, held for review only, pre-selected
    await held_for_review_only(client, fx, "o13", archived, secret(mark), mark, preselected=True)
    [row] = await message_of("o13", archived)
    assert row.state == "compacted"


# --- O14-O16: a released hold, purges, an unrelated rollback copy --------------------------------------------------


async def test_o14_released_hold_text_returns_at_next_import(client, fx):
    mark = "zqofourteen62"
    stub = "[tool output pruned to save context: %s]" % mark
    bj.add_session(fx["db"], "o14", started=T)
    bj.add_row(fx["db"], "o14", "user", "Run the vendor report.", T + 1)
    tool = bj.add_row(fx["db"], "o14", "tool", "REPORT " + secret("zqofourteenw62"), T + 2, tool_call_id="c1",
                      tool_name="terminal")
    twin = bj.prune_twin(fx["db"], "o14", tool, stub)
    await run(fx)
    await withhold(client, (await message_of("o14", twin))[0].id)
    [held] = await message_of("o14", tool)
    assert held.withheld
    await as_(client, "admin")
    assert (await client.post("/api/brain/holds/%d/release" % held.id)).status_code == 200
    await run(fx)
    await check_withheld_copies()
    await run(fx)
    await readable_everywhere(client, "o14", tool, stub, mark)


async def test_o15_different_entry_text_and_same_name_file(client, fx):
    """A purged memory entry and a purged file: an entry with other words, and a file with the same name elsewhere
    and other bytes, stay importable and listed."""
    mark = "zqofifteen62"

    def write(rel: str, body: str) -> None:
        path = os.path.join(fx["profile"], rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)

    write("memories/MEMORY.md", "The alarm code changes monthly.\n§\nInvoices go out on the first (%s).\n" % mark)
    write("knowledge/vendors.md", "# Vendors\nThe courier is Speedy.\n")
    await run(fx)
    async with session_scope() as s:
        entry = await s.scalar(select(MemoryItem).where(MemoryItem.body.contains("alarm code")))
        vendors = await s.scalar(select(MemoryItem).where(MemoryItem.body.contains("Speedy")))
        for item in (entry, vendors):
            target = await s.get(MemoryItem, item.id)
            await store.delete_item(s, target, actor="admin@test.local")
            await review.purge(s, target, actor="admin@test.local")
    write("memories/USER.md", "The owner prefers short answers (%s).\n" % mark)
    write("knowledge/archive/vendors.md", "# Vendors\nThe courier changed to Swift (%s).\n" % mark)
    await run(fx)
    await as_(client, "admin+")  # USER.md is the owner's profile: quarantined, an admin reviews it
    listed = (await client.get("/api/brain/items", params={"q": mark, "limit": 50})).json()["items"]
    bodies = " ".join(i["body"] for i in listed)
    for words in ("Invoices go out on the first", "The owner prefers short answers", "The courier changed to Swift"):
        assert words in bodies, words
    assert not any((i["flags"] or {}).get("purged_before") for i in listed)


async def test_o16_unrelated_rollback_copy_kept(scratch):
    """0.6.1's C3 overwrite kept another row's own text as a rollback copy of the row it overwrote: the repair blanks
    only copies of the same JARVIS row, so that one stays; and the unrelated row 0.6.1's C2 left (209) stays readable
    with no stone (O6 after an upgrade)."""
    conn = await _fx(scratch)
    try:
        assert await conn.fetchval("SELECT count(*) FROM import_records WHERE before ->> 'content' = 'Plain user text' "
                                   "AND before ->> 'withheld' = 'false'") == 1
        assert await conn.fetchval("SELECT withheld FROM archive_messages WHERE id = 209") is False
        assert await conn.fetchval(
            "SELECT count(*) FROM brain_tombstones WHERE source_key = 'jarvis:message:uid:uA'") == 0
    finally:
        await conn.close()


# --- Withheld on purpose (release notes, spec 18) -----------------------------------------------------------------


async def test_documented_fail_closed_outcomes_a_to_f(client, fx, scratch):
    # (a) an adjacent verbatim repeat first seen after the withhold, with its own uid: the importer's duplicate of the
    # withheld row (no row of its own), kept withheld with its stone, but certain only with that row's uid (plan 4.2
    # (5), 5.3): its key holds for review.
    ids = _two_turns(fx["db"], "fa", T, secret("zqfaila62"))
    await run(fx)
    withheld = await withheld_row_after(client, "fa", ids[1])
    repeat = bj.add_row(fx["db"], "fa", "assistant", secret("zqfaila62"), T + 3)
    await run(fx)
    key = "jarvis:message:uid:%s" % [r for r in bj.rows(fx["db"], "fa") if r["id"] == repeat][0]["message_uid"]
    stone = (await stones())[key]
    assert (stone.anchor_key, stone.via, stone.review) == (withheld.source_key, "duplicate", identity.PENDING)
    # (b) a backup without uids: a later verbatim repeat in the same conversation is a hold, an admin can release it.
    ids = conversation(fx["db"], "fb", T + DAY, [("user", "Q?"), ("assistant", secret("zqfailb62"))], uid=False)
    await run(fx)
    await withheld_row_after(client, "fb", ids[1])
    bj.add_row(fx["db"], "fb", "user", "Later, again?", T + 2 * DAY, uid=False)
    later = bj.add_row(fx["db"], "fb", "assistant", secret("zqfailb62"), T + 2 * DAY + 1, uid=False)
    second = await run(fx)
    assert (await records(second["run_id"]))["jarvis:message:fb:%d" % later].reason.startswith("held for")
    [held] = await message_of("fb", later)
    assert held.withheld and (await stones())[held.source_key].review == identity.PENDING
    await as_(client, "admin")
    assert (await client.post("/api/brain/holds/%d/release" % held.id)).status_code == 400  # identical: typed
    assert (await client.post("/api/brain/holds/%d/release" % held.id,
                              json={"confirm": identity.CONFIRM_WORD})).status_code == 200
    await run(fx)
    assert not (await message_of("fb", later))[0].withheld
    # (c) a same-role uid collision with nothing to compare (the withheld row is gone from the backup): a member.
    shared = bj.new_uid()
    bj.add_session(fx["db"], "fc", started=T + 3 * DAY)
    gone = bj.add_row(fx["db"], "fc", "assistant", secret("zqfailc62"), T + 3 * DAY + 1, uid=shared)
    await run(fx)
    await withheld_row_after(client, "fc", gone)
    bj.change(fx["db"], "DELETE FROM messages WHERE id = ?", gone)
    bj.add_row(fx["db"], "fc", "assistant", "Other words entirely.", T + 3 * DAY + 2, uid=shared)
    await run(fx)
    async with session_scope() as s:  # the uid key passed to the new row: the stored row under it stays withheld
        member = await s.scalar(select(ArchiveMessage).where(
            ArchiveMessage.source_key == "jarvis:message:uid:" + shared))
        assert not await s.scalar(select(ArchiveMessage.id).where(ArchiveMessage.content == "Other words entirely."))
    assert member.withheld and (await stones())[member.source_key].review == ""
    # (d) a later backup where JARVIS folded the withheld text into a stored row in place: the stored row keeps what
    # it holds, its own words (plan 5.2; owner question 7: JARVIS's edits there are not followed), and the composite
    # holding the withheld text is never stored; the withheld row stays withheld, never releasable. (A fold Hermes
    # commits as a new row is certain: test_brain_later_062.)
    ids = conversation(fx["db"], "fd", T + 4 * DAY, [("user", secret("zqfaild62")), ("assistant", "Noted."),
                                                     ("user", "Anything else?")])
    await run(fx)
    withheld_d = await withheld_row_after(client, "fd", ids[0])
    bj.fold(fx["db"], "fd", survivor=ids[2], dropped=[ids[0]])
    await run(fx)
    [survivor] = await message_of("fd", ids[2])
    assert (survivor.withheld, survivor.content) == (False, "Anything else?")
    assert await bj.nowhere("zqfaild62") == []
    [still] = await message_of("fd", ids[0])
    assert still.id == withheld_d.id and still.withheld
    await as_(client, "admin")
    assert (await client.post("/api/brain/holds/%d/release" % still.id)).status_code == 400
    # (e) and (f), after the upgrade of a real 0.6.1 leak database: 0.6.1's C2 victim (row 210, a row it rewrote and
    # withheld under a moved key) stays withheld; the data is legacy (0.6.2 plan 1.7): marked for the legacy lock and
    # reset, never re-derived into holds by the upgrade's text and uid heuristics (plan 1.6).
    conn = await _fx(scratch)
    try:
        assert await conn.fetchval("SELECT withheld FROM archive_messages WHERE id = 210") is True
        assert await conn.fetchval("SELECT count(*) FROM brain_tombstones WHERE review = 'pending'") == 0
        assert await conn.fetchval("SELECT count(*) FROM brain_maintenance WHERE name = 'legacy_brain_data'") == 1
    finally:
        await conn.close()


async def withheld_row_after(client, sid: str, source_id: int) -> ArchiveMessage:
    [m] = await message_of(sid, source_id)
    await withhold(client, m.id)
    return await withheld_row(sid, source_id)


async def test_no_stone_was_created_for_readable_rows_after_every_case(client, fx):
    """Across a session with every inverse case at once (identical text before, a later own-uid repeat, a parallel
    tool result, a similar sentence) and three later backups: the identical row the admin unticked in the dialog and
    the similar sentence stay readable with no stone (0.6.2 plan 4.5, 5.2); the rows new in the later backups (the
    own-uid repeat, the follow-ups) are held for review only (plan 5.3); nothing but the admin's own row is certain."""
    mark = "zqoall62"
    bj.add_session(fx["db"], "oall", started=T)
    ask = bj.add_row(fx["db"], "oall", "user", "Give me the number.", T + 1)
    target = bj.add_row(fx["db"], "oall", "assistant", secret(mark), T + 2)
    bj.add_row(fx["db"], "oall", "user", "And again?", T + 3)
    before = bj.add_row(fx["db"], "oall", "assistant", secret(mark), T + 4)
    similar = bj.add_row(fx["db"], "oall", "assistant", secret(mark[:-1] + "q"), T + 5)
    await run(fx)
    await withhold_leaving(client, "oall", target, before)
    bj.add_row(fx["db"], "oall", "user", "Next month?", T + DAY)
    later = bj.add_row(fx["db"], "oall", "assistant", secret(mark), T + DAY + 1)
    for n in range(3):
        bj.add_row(fx["db"], "oall", "user", "Follow-up %d." % n, T + DAY + 10 + n)
        last = await run(fx)
    [withheld] = await message_of("oall", target)
    certain = {k for k, st in (await stones()).items() if st.review != identity.PENDING}
    assert certain == {withheld.source_key}
    assert {st.via for st in (await stones()).values() if st.review == identity.PENDING} == {identity.LATER_HOLD}
    v = await imp.verify(last["run_id"], bj.paths(fx))
    assert v["passed"], {k: c for k, c in v["verification"]["checks"].items() if not c["ok"]}
    for source_id, words, m in ((before, secret(mark), mark), (similar, secret(mark[:-1] + "q"), mark[:-1] + "q")):
        await readable_everywhere(client, "oall", source_id, words, m)
    assert not (await message_of("oall", ask))[0].withheld
    await held_for_review_only(client, fx, "oall", later, secret(mark), mark, preselected=True)
