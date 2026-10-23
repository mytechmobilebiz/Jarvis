"""JARVIS Brain 0.6.2: regressions for what the integrator's oracle runs found (deviation log 240+). Synthetic data
only; the conversations are built with tests/property/hermes_sim.py and the Command Center is driven through
tests/property/cc_adapter.py, as in test_brain_contract_062.py."""

from __future__ import annotations

import pytest

from ..property import cc_adapter as cc
from ..property import hermes_sim as hs
from ..property import oracle as o
from . import test_brain_contract_062 as contract

W1, W2 = contract.W1, contract.W2
client, world = contract.client, contract.world  # the contract file's fixtures

pytestmark = pytest.mark.usefixtures("clean_db")


@pytest.mark.parametrize("move", ["branch_of_the_moved_continuation", "branch_then_rotation"])
async def test_a_title_carried_past_ancestors_whose_title_moved_on_stays_hidden(world, move):
    """Deviation 247 (oracle seed 6257): the admin hid the title of the withheld message's conversation. Hermes moved
    that title on (rotations clear the parent's title) and a later backup brings a branch carrying it ('<title> #2')
    whose parent no longer shows any title. The branch's title stays hidden after the admin reviewed its rows and the
    next import brought their text back, for every reader."""
    w = world()
    p = w.session("cli")
    first = w.add(p, "user", W2)  # Hermes derives the title from it
    w.add(p, "assistant", "Understood, I will do it.")
    w.turn(p, "Please tidy up the onboarding checklist before lunch.")
    c = w.sim.rotate(p, 0, 2)  # the title moves to the continuation
    d = w.snap("d")
    await w.imp(d)
    await w.withhold(p, first, hide_title=True)
    w.settle(d)
    await w.review_all()
    if move == "branch_of_the_moved_continuation":
        c2 = w.sim.rotate(c, 0, 2)  # the title moves on again: c shows none
        b = w.sim.cli_branch(c2)
        c3 = w.sim.rotate(c2, 0, 2)  # and again: the branch's parent shows none either
        assert c3
    else:
        b = w.sim.cli_branch(c)
        assert w.sim.rotate(c, 0, 2)  # the title moves on from the branch's parent
    w.turn(b, "Please plan the newsletter for next week.")
    x = w.snap("x")
    title = hs.derive_title(W2)
    assert x.sessions[b]["title"] and title[:20] in x.sessions[b]["title"]
    await w.imp(x)
    rows = {r["id"]: r for r in await cc.rows()}
    for h in await w.holds():  # every row of the branch is independent of the withheld message: released
        r = rows[h["message"]]
        resp = await (w.api.release(h["message"]) if r["sid"] == b else w.api.keep(h["message"]))
        assert resp.status_code == 200, resp.text
    await w.imp(x)  # the released rows' text comes back at the next import
    assert not [r for r in await cc.rows() if r["sid"] == b and r.get("withheld")]
    stored = [s for s in await cc.sessions() if s.get("source_session_id") == b]
    assert stored and title[:20] not in (stored[0].get("title") or "")
    resp = await w.api.call("GET", "/api/brain/sessions", params={"limit": 200})
    assert resp.status_code == 200 and title[:20] not in resp.text


async def test_settle_reads_the_withheld_text_from_its_hermes_copy_when_the_row_was_renumbered(world):
    """Deviation 251 (round-3 replay seed 322): the conversation already holds a decision, so its stored rows keep
    their JARVIS rows; a gateway /retry then rewrote it (same uids and times, new row ids). The admin withholds a
    stored message whose JARVIS row the newest backup no longer holds under that row id, only under its Hermes copy
    identity. Settle on that backup computes the evidence with the withheld text, as an import does
    (importer._w_text): a row with the very words is held 'identical' (typed confirmation), and no hold of that
    decision says 'the withheld text is not in this backup'."""
    w = world()
    p = w.session("telegram")
    first, _ = w.turn(p, "Please summarise the vendor situation for me.")
    wrow = w.add(p, "assistant", W1)
    w.turn(p, "Please send the weekly summary.", "Sent it a minute ago.")
    d0 = w.snap("d")
    await w.imp(d0)
    await w.withhold(p, first)  # an earlier decision: the family's stored rows keep their JARVIS rows (plan 5.2)
    w.settle(d0)
    await w.review_all()
    w.sim.gateway_retry(p)  # same uids and times, new row ids: the JARVIS row W1 was stored from is gone
    _, repeat = w.turn(p, "Please send over the newsletter draft.", W1)  # independent, the very words
    d1 = w.snap("x")
    w1_uid = d0.rows[(p, wrow)].uid
    assert (p, wrow) not in d1.rows or d1.rows[(p, wrow)].uid != w1_uid
    assert any(v.content == W1 and v.uid == w1_uid for v in d1.rows.values())  # its Hermes copy identity
    await w.imp(d1)
    w1 = [r for r in await cc.rows() if r["sid"] == p and r.get("source_uid") == w1_uid and not r.get("withheld")]
    assert w1, "the stored row of W1 is readable before its withhold"
    resp = await w.api.withhold(w1[0]["id"])
    assert resp.status_code == 200, resp.text
    w.settle(d1)
    holds = await w.holds()
    for h in holds:  # both withheld texts are in this backup, under their Hermes copy identity
        assert "not_in_backup" not in o.reported_relations(h)[0], h
    rel = await w.relations(p, repeat)  # whichever decision it is listed under
    assert "identical" in rel, rel
    assert rel != {"none"}


async def test_the_title_is_hidden_by_default_when_a_fold_re_inserted_the_first_user_message(world):
    """Deviation 252 (oracle seed 6341): the withheld message is its conversation's first user turn, so Hermes titled
    the conversation from it. A micro-compaction fold re-inserted the history with new row ids (same uids and times),
    and a rotation moved the title to the continuation. The admin withholds it with the dialog's default: the title
    is hidden, in the continuation it moved to as well, for every reader and after the next import."""
    w = world()
    p = w.session("cli")
    w.add(p, "user", W2)  # Hermes derives the title from it
    w.add(p, "assistant", "Understood, I will do it.")
    u2, _ = w.turn(p, "Please tidy up the onboarding checklist before lunch.")
    u3, _ = w.turn(p, "Please plan the newsletter for next week.")
    await w.imp(w.snap("d0"))  # the conversation is stored before the fold
    assert w.sim.fold(p, (u2, u3))
    c = w.sim.rotate(p, 0, 2)  # the title moves to the continuation
    d = w.snap("d")
    title = hs.derive_title(W2)
    assert title[:20] in (d.sessions[c]["title"] or "") and not d.sessions[p]["title"]
    wrow = [row for (sid, row), v in d.rows.items() if sid == p and v.role == "user" and v.content == W2]
    assert wrow
    await w.imp(d)
    await w.withhold(p, wrow[-1])  # the dialog's default for the title
    w.settle(d)
    await w.review_all()
    await w.imp(d)
    for s in await cc.sessions():
        assert title[:20] not in (s.get("title") or ""), s.get("source_session_id")
    resp = await w.api.call("GET", "/api/brain/sessions", params={"limit": 200})
    assert resp.status_code == 200 and title[:20] not in resp.text


@pytest.mark.parametrize("uids", ["legacy", "modern"])
async def test_live_keeps_a_hidden_title_of_a_conversation_its_decisions_import_creates(world, uids):
    """Deviation 256 (oracle seed 7704): the admin withholds a branch's copy of the parent's first user message and
    hides the title. The parent's original becomes a member (same uid, or Settle's rule 6 in reverse, S14), so its
    title, derived from that message, is hidden too. Live imports the same backup with the decisions file: the import
    creates the parent conversation and hides its title, and the flag that keeps it hidden is stored as on the test
    copy. A later backup whose first user row of the parent is new (a fold re-inserted it) keeps it hidden."""
    w = world(uids=uids)
    p = w.session("cli")
    w.add(p, "user", W2)  # Hermes derives the parent's title from it
    w.add(p, "assistant", "Understood, I will do it.")
    u2, _ = w.turn(p, "Please tidy up the onboarding checklist before lunch.")
    u3, _ = w.turn(p, "Please plan the newsletter for next week.")
    b = w.sim.cli_branch(p)  # copies the history with its times
    w.turn(b, "Please draft the agenda for Monday.")
    d = w.snap("d")
    title = hs.derive_title(W2)
    assert title[:20] in (d.sessions[p]["title"] or "")
    [brow] = [row for (sid, row), v in d.rows.items() if sid == b and v.role == "user" and v.content == W2]
    await w.imp(d)
    await w.withhold(b, brow, hide_title=True)
    w.settle(d)
    await w.review_all()
    tc = {s["source_session_id"]: s for s in await cc.sessions()}
    assert title[:20] not in (tc[p].get("title") or "") and (tc[p].get("flags") or {}).get("title_withheld")
    decisions = await w.export()
    await w.live(d, decisions)
    live = {s["source_session_id"]: s for s in await cc.sessions()}
    assert title[:20] not in (live[p].get("title") or "")
    assert (live[p].get("flags") or {}).get("title_withheld"), live[p].get("flags")  # as on the test copy (C1)
    assert w.sim.fold(p, (u2, u3))  # the parent's first user row is re-inserted under a new row id
    w.turn(p, "Please send the weekly summary.")
    x = w.snap("x")
    await w.imp(x)
    for s in await cc.sessions():
        assert title[:20] not in (s.get("title") or ""), s.get("source_session_id")
    resp = await w.api.call("GET", "/api/brain/sessions", params={"limit": 200})
    assert resp.status_code == 200 and title[:20] not in resp.text


async def test_a_fold_that_merges_a_reply_quoting_the_withheld_text_is_never_released_as_unrelated(world):
    """Deviation 258 (oracle seed 6221, B6): Hermes's micro-compaction fold merges two adjacent user turns as
    prev + '\\n\\n' + msg, so the second turn's reply pointer quoting the withheld text sits after a blank line, not at
    the start. Its evidence is 'part of (reply quote)' (plan 4.3, 4.6), not 'none', so 'release this conversation's
    rows with no relation' leaves it held."""
    w = world()
    p = w.session("telegram")
    w.turn(p, "Please summarise the vendor situation for me.")
    wrow = w.add(p, "assistant", W1)
    d = w.snap("d")
    await w.imp(d)
    await w.withhold(p, wrow)
    w.settle(d)
    await w.review_all()
    a = w.add(p, "user", "Please tidy up the onboarding checklist before lunch.")
    b = w.add(p, "user", hs.reply_pointer(W1[4:40], "Is that the yearly figure?"))
    comp = w.sim.fold(p, (a, b))
    assert comp
    x = w.snap("x")
    assert "[Replying to" in x.rows[(p, comp)].content and not x.rows[(p, comp)].content.startswith("[Replying")
    await w.imp(x)
    assert await w.status(p, comp) == "held"
    assert "part_of" in await w.relations(p, comp)
    [sess] = [s for s in await cc.sessions() if s.get("source_session_id") == p]
    resp = await w.api.call("POST", "/api/brain/sessions/%d/holds/release-unrelated" % sess["id"])
    assert resp.status_code == 200, resp.text
    [row] = (await w.stored(p, comp))[-1:]
    assert row["id"] not in resp.json()["released"]
    assert await w.status(p, comp) == "held"


async def test_settle_never_takes_a_fold_s_own_uid_as_the_withheld_message_s(world):
    """Plan 4.2 (3) and rv3-res-6 inside Settle (integrator mutation check, plan 5.4 'fold stones not being uid
    sources'): a fold that absorbed the withheld message's uid is certain, but its own uid is the survivor's message
    (A), not the withheld one. An import before Settle stores that fold under a 'merged into another message' stone.
    In a branch, another fold absorbs A's uid and never carries the withheld words: Settle must not make it certain."""
    w = world()
    p = w.session("cli")
    w.add(p, "user", "Please plan the newsletter for next week.")
    w.add(p, "assistant", "Noted, I will take care of it.")
    a = w.add(p, "user", "Please tidy up the onboarding checklist before lunch.")
    w.add(p, "assistant", "Done, it is tidy now.")
    wr = w.add(p, "user", W2)
    w.add(p, "assistant", "Understood, I will do it.")
    await w.imp(w.snap("d0"))
    await w.withhold(p, wr)
    b = w.sim.cli_branch(p)  # copies the history with its uids (CLI /branch)
    s1 = w.snap("s1")
    users = sorted(row for (sid, row), v in s1.rows.items() if sid == b and v.role == "user")
    assert len(users) == 3
    f = w.sim.fold(p, (a, wr))  # A + W2: A's uid, W2's absorbed
    f2 = w.sim.fold(b, (users[0], users[1]))  # C + A in the branch: C's uid, A's absorbed; no word of W2
    assert f and f2
    d1 = w.snap("d1")
    assert W2 in d1.rows[(p, f)].content and W2 not in d1.rows[(b, f2)].content
    await w.imp(d1)  # the test copy imports the newer backup before finishing the decision
    w.settle(d1)
    assert await w.status(p, f) == "certain"
    assert await w.status(b, f2) != "certain"


async def test_a_withheld_tool_output_jarvis_pruned_is_not_in_the_backup_and_blocks_the_one_click_release(world):
    """Deviation 264 (oracle seed 6725): JARVIS's proactive prune rewrote the withheld tool output in place with
    Hermes's placeholder (its original sits in an archived twin, here without a uid, so nothing links to it). The
    placeholder is not the withheld text: the later backup's new rows of the conversation say 'the withheld text is
    not in this backup', never 'none', and 'release this conversation's rows with no relation' is refused (owner Q6:
    review row by row)."""
    w = world(uids="legacy")
    p = w.session("telegram")
    w.turn(p, "Please fetch the vendor report.", "Fetching it now.")
    wrow = w.add(p, "tool", W1, call="call_vendor01")
    w.add(p, "assistant", "Here is the summary of the report.")
    d = w.snap("d")
    await w.imp(d)
    await w.withhold(p, wrow)
    w.settle(d)
    await w.review_all()
    new_u, _ = w.turn(p, "Please send the weekly summary.", "Sent it a minute ago.")
    assert w.sim.prune(p, wrow)
    x = w.snap("x")
    assert x.rows[(p, wrow)].content == hs.STUB
    await w.imp(x)
    assert await w.status(p, new_u) == "held"
    rel = await w.relations(p, new_u)
    assert "not_in_backup" in rel and "none" not in rel, rel
    [sess] = [s for s in await cc.sessions() if s.get("source_session_id") == p]
    resp = await w.api.call("POST", "/api/brain/sessions/%d/holds/release-unrelated" % sess["id"])
    assert resp.status_code == 400, resp.text
    assert await w.status(p, new_u) == "held"


async def test_a_new_branch_keeps_a_hidden_title_when_jarvis_deleted_its_parent(world):
    """Deviation 272 (oracle seed 7415): the withheld message is its conversation's first user turn, so its title is
    hidden; Hermes moved the title to a continuation. A later backup brings a branch of that continuation ('<title>
    #2') whose rows are all safe copies, and JARVIS has deleted the continuation itself. The Command Center still holds
    the continuation with its hidden title, so the branch's title stays hidden after the import that releases its
    held title (no row of it withheld)."""
    w = world()
    p = w.session("telegram")
    w.add(p, "user", W2)  # Hermes derives the title from it
    w.add(p, "assistant", "Understood, I will do it.")
    w.turn(p, "Please tidy up the onboarding checklist before lunch.", "Done, it is tidy now.")
    w.turn(p, "Please plan the newsletter for next week.", "Planned for Monday.")
    c = w.sim.rotate(p, 0, 2)  # the title moves to the continuation
    d = w.snap("d")
    title = hs.derive_title(W2)
    assert title[:20] in (d.sessions[c]["title"] or "")
    await w.imp(d)
    [wrow] = [row for (sid, row), v in d.rows.items() if sid == p and v.content == W2]
    await w.withhold(p, wrow)  # the dialog's default hides the title (its first user message)
    w.settle(d)
    await w.review_all()
    b = w.sim.gateway_branch(c)
    w.sim.delete_session(c)
    x = w.snap("x")
    assert c not in x.sessions and title[:20] in (x.sessions[b]["title"] or "")
    await w.imp(x)
    await w.imp(x)  # the next import (a held title is released once no row of the conversation is withheld)
    for s in await cc.sessions():
        assert title[:20] not in (s.get("title") or ""), s.get("source_session_id")
    resp = await w.api.call("GET", "/api/brain/sessions", params={"limit": 200})
    assert resp.status_code == 200 and title[:20] not in resp.text


async def test_a_reply_quote_of_sixteen_characters_as_written_is_held(world):
    """Deviation 273 (oracle seed 7931): plan 4.3 holds a reply quote of at least 16 characters of W. JARVIS's quote
    ' new vendor contr' is 16 characters as written (it starts with a space); it is held 'part of', not readable."""
    w = world()
    p = w.session("telegram")
    w.turn(p, "Please summarise the vendor situation for me.")
    wrow = w.add(p, "assistant", W1)
    q = W1[3:20]
    assert len(q) == 17 and q[0] == " "
    q = q[:16]
    r = w.add(p, "user", hs.reply_pointer(q, "Is that the yearly figure?"))
    d = w.snap("d")
    await w.imp(d)
    await w.withhold(p, wrow)
    w.settle(d)
    assert await w.status(p, r) == "held"
    assert "part_of" in await w.relations(p, r)


async def test_the_original_of_a_message_jarvis_folded_into_the_withheld_one_is_held(world):
    """Deviation 274 (oracle seed 6967): a rotation copied the conversation without its times (no donor timestamps,
    no uids), and JARVIS then folded the copy of its first user message with the next turn: the admin withholds that
    fold. The parent's original of the message is part of the withheld text, in the same family: Settle holds it
    'part of' (listed, releasable with the typed confirmation), it is never left readable without review."""
    w = world(uids="legacy", donor=False)
    p = w.session("cli")
    a = w.add(p, "user", "Please summarise the vendor review before lunch.")
    w.add(p, "assistant", "Here is the summary of the vendor review.")
    w.turn(p, "Please tidy up the onboarding checklist before lunch.", "Done, it is tidy now.")
    c = w.sim.rotate(p, 2, 0, donor=False)
    s1 = w.snap("s1")
    [a2] = [row for (sid, row), v in s1.rows.items() if sid == c and v.role == "user"]
    yes = w.add(c, "user", "yes please")
    comp = w.sim.fold(c, (a2, yes))
    assert comp
    d = w.snap("d")
    assert d.rows[(c, comp)].content.startswith("Please summarise the vendor review before lunch.")
    assert round(d.rows[(p, a)].ts, 3) != round(d.rows[(c, comp)].ts, 3)
    await w.imp(d)
    await w.withhold(c, comp)
    w.settle(d)
    assert await w.status(p, a) == "held"
    assert "part_of" in await w.relations(p, a)


async def test_a_later_fold_of_the_withheld_message_is_not_taken_as_its_text(world):
    """Deviation 275 (oracle seed 6976): after the decision JARVIS folded the withheld message with the next turn (the
    fold keeps W's uid and time and absorbs the other turn's uid), then rewrote the conversation (replace_messages:
    W's own row is gone) and a gateway /branch copied it. The fold is not W's text: the branch's copy of it is not
    'identical' to W, and with W's own text gone from the backup the evidence says 'not in this backup'."""
    w = world()
    p = w.session("telegram")
    w.turn(p, "Please summarise the vendor situation for me.")
    wrow = w.add(p, "user", W2)
    b_row = w.add(p, "user", "and send it to the team too")
    w.add(p, "assistant", "Will do.")
    d = w.snap("d")
    await w.imp(d)
    await w.withhold(p, wrow)
    w.settle(d)
    await w.review_all()
    comp = w.sim.fold(p, (wrow, b_row))
    assert comp
    w.sim.replace(p, drop_last_turn=False)  # destructive: W's own (now archived) row is deleted
    br = w.sim.gateway_branch(p)
    x = w.snap("x")
    assert not any(v.content == W2 for v in x.rows.values())
    copies = [row for (sid, row), v in x.rows.items() if sid == br and v.content.startswith(W2)]
    assert copies
    await w.imp(x)
    rel = await w.relations(br, copies[0])
    assert "identical" not in rel and "not_in_backup" in rel, rel


async def test_a_restated_request_that_quotes_the_withheld_text_is_never_released_as_unrelated(world):
    """Deviation 280 (oracle seed 7072, B6/B2): a compaction restates the unfinished request as REPLAY_HEADER + '\\n' +
    the request (Hermes context_compressor). When that request is a reply quoting the withheld text, its pointer
    follows the header, not the start of the text: it is still the request's own wrapper, so the restatement is held
    'part of (reply quote)' (plan 4.3, 4.6), never 'none', and 'release rows with no relation' leaves it held."""
    w = world()
    p = w.session("telegram")
    w.turn(p, "Please summarise the vendor situation for me.")
    wrow = w.add(p, "assistant", W1)
    d = w.snap("d")
    await w.imp(d)
    await w.withhold(p, wrow)
    w.settle(d)
    await w.review_all()
    w.add(p, "user", hs.reply_pointer(W1[4:40], "Is that the yearly figure?"))
    new = w.sim.compact(p, 0, 0, restate="row")
    restated = new[-1]
    x = w.snap("x")
    content = x.rows[(p, restated)].content
    assert content.startswith(hs.REPLAY_HEADER) and "[Replying to" in content
    await w.imp(x)
    assert await w.status(p, restated) == "held"
    assert "part_of" in await w.relations(p, restated)
    [sess] = [s for s in await cc.sessions() if s.get("source_session_id") == p]
    resp = await w.api.call("POST", "/api/brain/sessions/%d/holds/release-unrelated" % sess["id"])
    assert resp.status_code == 200, resp.text
    [row] = (await w.stored(p, restated))[-1:]
    assert row["id"] not in resp.json()["released"]
    assert await w.status(p, restated) == "held"


async def test_a_held_title_derived_from_the_withheld_message_stays_hidden_when_no_lineage_names_it(world):
    """Deviation 282 (oracle seeds 6596, 7126, 7529; B2, C4): the withheld message is its conversation's first user
    turn, so its title is hidden. Hermes moved the title to a continuation, branched it ('<title> #2') and deleted the
    continuation before any import saw it: no lineage the Command Center knows leads from the branch to the hidden
    title. The branch joins the family through its copied history, so its title is held as 'new in a later backup'.
    After the admin released every row of the branch (all independent) the next import must not bring the title
    back: a held title is released only when it has no relation to a withheld text of its family that this backup
    holds, and this one is a part of the withheld message (Hermes derived it from it)."""
    w = world()
    p = w.session("cli")
    w.add(p, "user", W2)  # Hermes derives the title from it
    w.add(p, "assistant", "Understood, I will do it.")
    w.turn(p, "Please tidy up the onboarding checklist before lunch.", "Done, it is tidy now.")
    d = w.snap("d")
    await w.imp(d)
    [wrow] = [row for (sid, row), v in d.rows.items() if sid == p and v.content == W2]
    await w.withhold(p, wrow)  # the dialog's default hides the title (its first user message)
    w.settle(d)
    await w.review_all()
    c = w.sim.rotate(p, 0, 2)  # the title moves to the continuation
    b = w.sim.cli_branch(c)  # '<title> #2', copies the continuation's history with its times
    w.sim.delete_session(c)  # before any import saw it: the branch's parent link is gone
    w.turn(b, "Please plan the newsletter for next week.", "Planned for Monday.")
    x = w.snap("x")
    title = hs.derive_title(W2)
    assert c not in x.sessions and title[:20] in (x.sessions[b]["title"] or "")
    assert any(v.content == W2 for v in x.rows.values())  # the withheld text is in this backup
    await w.imp(x)
    stored = [s for s in await cc.sessions() if s.get("source_session_id") == b]
    assert stored and title[:20] not in (stored[0].get("title") or ""), "the branch is not in the family"
    rows = {r["id"]: r for r in await cc.rows()}
    released = 0
    for h in await w.holds():  # every row of the branch is independent of the withheld message: released
        r = rows[h["message"]]
        resp = await (w.api.release(h["message"]) if r["sid"] == b else w.api.keep(h["message"]))
        assert resp.status_code == 200, resp.text
        released += r["sid"] == b
    assert released
    await w.imp(x)  # the released rows' text comes back at the next import
    assert not [r for r in await cc.rows() if r["sid"] == b and r.get("withheld")]
    for s in await cc.sessions():
        assert title[:20] not in (s.get("title") or ""), s.get("source_session_id")
    resp = await w.api.call("GET", "/api/brain/sessions", params={"limit": 200})
    assert resp.status_code == 200 and title[:20] not in resp.text


@pytest.mark.parametrize("where", ["merged_by_a_fold", "restated_after_a_compaction"])
async def test_a_reply_quoting_the_withheld_text_outside_every_family_is_held_wherever_hermes_puts_the_pointer(
        world, where):
    """Deviation 283 (owner question 8 (a), with deviation 280): outside every withheld family a later backup's reply
    that quotes the withheld text through JARVIS's reply pointer is held 'part of (reply quote)'. Hermes puts the
    pointer at the start of the row, but also after a blank line (a fold merged the reply into the previous turn) and
    after the replay header (a compaction restated the reply): read in every place, as inside a family."""
    w = world()
    p = w.session("telegram")
    w.turn(p, "Please summarise the vendor situation for me.")
    wrow = w.add(p, "assistant", W1)
    other = w.session("telegram")  # an unrelated conversation: no link to the withheld one
    w.turn(other, "Please plan the newsletter for next week.", "Planned for Monday.")
    d = w.snap("d")
    await w.imp(d)
    await w.withhold(p, wrow)
    w.settle(d)
    await w.review_all()
    if where == "merged_by_a_fold":
        a = w.add(other, "user", "Please tidy up the onboarding checklist before lunch.")
        b = w.add(other, "user", hs.reply_pointer(W1[4:40], "Is that the yearly figure?"))
        row = w.sim.fold(other, (a, b))
        assert row
    else:
        w.add(other, "user", hs.reply_pointer(W1[4:40], "Is that the yearly figure?"))
        row = w.sim.compact(other, 0, 0, restate="row")[-1]
    x = w.snap("x")
    content = x.rows[(other, row)].content
    assert "[Replying to" in content and not content.startswith("[Replying")
    await w.imp(x)
    assert await w.status(other, row) == "held"
    assert "part_of" in await w.relations(other, row)


async def test_the_stored_original_of_a_re_sent_message_folded_into_the_withheld_one_is_held(world):
    """Deviation 287 (oracle seed 8224, A1; same rule as deviation 274): the test copy stored a request; JARVIS's
    gateway /retry then deleted it and sent its text again as a new message (new uid, new time), and a fold merged
    that re-send with the next turn. The admin withholds the fold. The stored original is no longer in the backup
    and is not at the withheld row's time, but its text is a part of the withheld text, in the same conversation:
    Settle holds it 'part of' (listed, releasable with the typed confirmation), never leaves it readable."""
    w = world()
    p = w.session("telegram")
    w.turn(p, "Please plan the newsletter for next week.")
    x = "Please double check the support backlog and flag anything odd."
    orig, _ = w.turn(p, x)
    await w.imp(w.snap("d0"))  # the test copy stores the original
    sent = w.sim.gateway_retry(p)  # the original row is deleted, its text sent again (new uid, new time)
    y = w.add(p, "user", "Please compare the landing page copy with the usual care.")
    comp = w.sim.fold(p, (sent["user"], y))
    assert comp
    d = w.snap("d")
    assert (p, orig) not in d.rows and d.rows[(p, comp)].content.startswith(x + "\n\n")
    await w.imp(d)
    assert await w.status(p, orig) == "readable"
    await w.withhold(p, comp)
    w.settle(d)
    assert await w.status(p, orig) == "held"
    assert "part_of" in await w.relations(p, orig)
    assert not await w.readable_with(x)
