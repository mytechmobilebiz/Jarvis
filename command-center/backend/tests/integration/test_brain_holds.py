"""JARVIS Brain 0.6.2: rows held for an admin's review, the withhold preview and the withhold's choices (spec 8, 19.6).

A hold is a row withheld because it may be a withheld message (the same JARVIS uid with other text, the same text
outside a copy position, an upgrade heuristic). The admin sees the evidence, never the text, and keeps it withheld for
good or releases it; a release brings the text back at the next import, and only the hold rules ever skip it after.
The withhold preview lists what a withhold also takes (the same JARVIS message stored again, held rows, copies with no
row), what it takes only when asked (identical text in the conversation and its continuations and branches), the
title choice and the imported memory items that contain the text (listed, never removed). Synthetic data only.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select, text, update

from app.bootstrap import check_withheld_copies
from app.brain import identity
from app.brain import importer as imp
from app.db import session_scope
from app.models import (
    ArchiveMessage,
    ArchiveSession,
    AuditLog,
    BrainWithholdExemption,
    MemoryItem,
)

from .. import brain_jarvis as bj
from ..conftest import SERVICE_TOKEN
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_withhold_identity import DAY, T, conversation, message_of, run, secret, stones, withhold

pytestmark = pytest.mark.usefixtures("clean_db")

STUB = "[tool output pruned to save context]"
EVIDENCE = {"message", "session", "title", "position", "role", "at", "content_chars", "state", "rule",
            "withheld_message", "compare", "neighbours", "evidence"}  # evidence: rules062-confirm's honest sentence


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


def _tool_backup(fx: dict[str, str], mark: str) -> list[int]:
    bj.add_session(fx["db"], "prune_01", started=T)
    return [bj.add_row(fx["db"], "prune_01", "user", "Run the vendor report.", T + 1),
            bj.add_row(fx["db"], "prune_01", "tool", "REPORT " + secret(mark), T + 2, tool_call_id="call_1",
                       tool_name="terminal"),
            bj.add_row(fx["db"], "prune_01", "assistant", "The report ran.", T + 3)]


async def held_stub(client, fx: dict[str, str], mark: str) -> tuple[ArchiveMessage, ArchiveMessage]:
    """JARVIS pruned a tool result (the full text archived as a compacted twin, the live row a stub with the same
    uid); the admin withholds the archived full text. The stub shares its uid with other text: held for review."""
    ids = _tool_backup(fx, mark)
    twin = bj.prune_twin(fx["db"], "prune_01", ids[1], STUB)
    await run(fx)
    [full] = await message_of("prune_01", twin)
    await withhold(client, full.id)
    [stub] = await message_of("prune_01", ids[1])
    assert stub.withheld and (await stones())[stub.source_key].review == identity.PENDING
    return full, stub


async def audit_of(action: str) -> list[AuditLog]:
    async with session_scope() as s:
        return list((await s.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id))).all())


async def test_holds_list_shows_evidence_and_no_text(client, fx):
    mark = "zqholdlist62"
    full, stub = await held_stub(client, fx, mark)
    await as_(client, "admin+")
    r = await client.get("/api/brain/holds")
    assert r.status_code == 200, r.text
    [hold] = r.json()["holds"]
    assert set(hold) == EVIDENCE and r.json()["total"] == 1
    assert (hold["message"], hold["session"], hold["role"], hold["state"]) == (stub.id, stub.session_id, "tool", "live")
    assert (hold["rule"], hold["compare"]) == ("same message id, different text", "different text")
    assert hold["content_chars"] == len(STUB) and hold["title"] == "Probe conversation"
    assert hold["withheld_message"] == {"id": full.id, "position": full.position, "role": "tool",
                                        "at": full.at.isoformat(), "content_chars": full.content_chars}
    async with session_scope() as s:
        around = list((await s.scalars(select(ArchiveMessage.id).where(
            ArchiveMessage.session_id == stub.session_id, ArchiveMessage.withheld.is_(False))
            .order_by(ArchiveMessage.position, ArchiveMessage.id))).all())
    assert hold["neighbours"] == [around[0], around[1]]  # the request before it, the answer after it
    assert mark not in r.text and STUB not in r.text and "REPORT" not in r.text  # evidence, never text
    # A hidden title is not shown.
    async with session_scope() as s:
        sess = await s.get(ArchiveSession, stub.session_id)
        sess.flags = {**sess.flags, "title_withheld": True}
    [hold] = (await client.get("/api/brain/holds")).json()["holds"]
    assert hold["title"] is None


async def test_keep_makes_a_hold_permanent(client, fx):
    mark = "zqholdkeep62"
    _, stub = await held_stub(client, fx, mark)
    await as_(client, "admin")
    r = await client.post("/api/brain/holds/%d/keep" % stub.id)
    assert r.status_code == 200 and r.json()["review"] == identity.KEPT, r.text
    stone = (await stones())[stub.source_key]
    assert stone.review == identity.KEPT
    [kept] = await audit_of("archive.hold_kept")
    assert kept.detail == {"message": stub.id, "session": stub.session_id, "anchor": stone.anchor_key,
                           "rule": "same message id, different text"}
    assert (await client.get("/api/brain/holds")).json()["holds"] == []
    r = await client.post("/api/brain/holds/%d/release" % stub.id)
    assert r.status_code == 400 and "not held for review" in r.text
    again = await run(fx)  # a kept hold is a member: withheld for good
    [back] = await message_of("prune_01", stub.source_message_id)
    assert back.withheld and back.content == ""
    assert (await stones())[stub.source_key].review == identity.KEPT
    assert (await imp.status())["held_for_review"] == 0
    v = await imp.verify(again["run_id"], bj.paths(fx))
    assert v["passed"], {k: c for k, c in v["verification"]["checks"].items() if not c["ok"]}


async def test_release_refused_for_certain_matches(client, fx):
    mark = "zqholdrefuse62"
    full, stub = await held_stub(client, fx, mark)
    await as_(client, "admin")
    # The withheld message itself, and a readable row, are not held.
    for mid in (full.id, (await message_of("prune_01", 1))[0].id):
        r = await client.post("/api/brain/holds/%d/release" % mid)
        assert r.status_code == 400 and "not held for review" in r.text, r.text
    # A certain stone (decisions-apply's binding, an admin's own) names the held row's JARVIS row: it IS the message.
    async with session_scope() as s:
        await identity.put_stone(s, identity.Stone(
            key="jarvis:message:prune_01:%s:decided" % stub.source_message_id, session="prune_01",
            row=stub.source_message_id, role="tool", via="decisions-apply", bound=True), actor="admin")
    r = await client.post("/api/brain/holds/%d/release" % stub.id)
    assert r.status_code == 400 and "it cannot be released" in r.text, r.text
    assert (await stones())[stub.source_key].review == identity.PENDING  # nothing changed
    assert await audit_of("archive.hold_released") == []


async def test_release_restores_text_at_next_import_and_audits(client, fx):
    mark = "zqholdrelease62"
    full, stub = await held_stub(client, fx, mark)
    anchor = (await stones())[stub.source_key].anchor_key
    await as_(client, "admin")
    r = await client.post("/api/brain/holds/%d/release" % stub.id)
    assert r.status_code == 200 and r.json()["released"] is True, r.text
    [released] = await audit_of("archive.hold_released")
    assert released.detail == {"message": stub.id, "session": stub.session_id, "anchor": anchor,
                               "rule": "same message id, different text"}
    assert stub.source_key not in await stones()
    async with session_scope() as s:
        assert await s.get(BrainWithholdExemption, (anchor, "prune_01", stub.source_message_id)) is not None
        now_ = await s.get(ArchiveMessage, stub.id)
    assert now_.withheld and now_.source_fp == "" and now_.flags["withheld_by_admin"]["released_at"]
    assert (await client.get("/api/brain/holds")).json()["holds"] == []
    # Its text comes back at the next import; the withheld message stays withheld.
    second = await run(fx)
    [back] = await message_of("prune_01", stub.source_message_id)
    assert (back.withheld, back.content) == (False, STUB)
    [still] = await message_of("prune_01", full.source_message_id)
    assert still.withheld and still.content == ""
    assert await bj.nowhere(mark) == []
    # It stays readable: later imports, the start-up repair (the uid rule skips a released row) and promote.
    await run(fx)
    await check_withheld_copies()
    [after] = await message_of("prune_01", stub.source_message_id)
    assert (after.withheld, after.content) == (False, STUB) and after.source_key not in await stones()
    await as_(client, "operator")
    r = await client.post("/api/brain/messages/%d/promote" % after.id, json={"kind": "project", "title": "Stub"})
    assert r.status_code == 201, r.text
    v = await imp.verify(second["run_id"], bj.paths(fx))
    assert v["passed"], {k: c for k, c in v["verification"]["checks"].items() if not c["ok"]}


async def _release_digests() -> list[tuple[str, int, str]]:
    async with session_scope() as s:
        return [(e.source_row_id, e.content_chars, e.content_fp)
                for e in (await s.scalars(select(BrainWithholdExemption))).all()]


async def test_withholding_released_content_drops_its_digest(client, fx):
    """Review 2 S27 (tests-integrity #3): a release is bound to the content it released by a keyed digest of the
    released row's readable text (content_fp 'r1:...'). When an admin later withholds that very content, the withhold
    drops the digest at once (review.withhold_message -> identity.unbind_withheld): no keyed fingerprint of withheld
    text is kept, not even until the next import (whose own unbind would clear it later)."""
    _, stub = await held_stub(client, fx, "zqholdunbind62")
    await as_(client, "admin")
    assert (await client.post("/api/brain/holds/%d/release" % stub.id)).status_code == 200
    await run(fx)
    [back] = await message_of("prune_01", stub.source_message_id)
    assert (back.withheld, back.content) == (False, STUB)
    bound = await _release_digests()
    assert bound and all(fp.startswith("r1:") for _, _, fp in bound), bound
    await withhold(client, back.id)  # the admin withholds the released content itself
    async with session_scope() as s:
        now_ = await s.get(ArchiveMessage, back.id)
        assert now_.withheld and now_.content == ""
    assert all(fp == "" for _, _, fp in await _release_digests()), await _release_digests()  # before any import
    await run(fx)
    assert all(fp == "" for _, _, fp in await _release_digests()), await _release_digests()


async def test_holds_routes_are_admin_only_and_scoped(client, fx):
    mark = "zqholdscope62"
    full, stub = await held_stub(client, fx, mark)
    routes = [("GET", "/api/brain/holds"), ("POST", "/api/brain/holds/%d/keep" % stub.id),
              ("POST", "/api/brain/holds/%d/release" % stub.id),
              ("GET", "/api/brain/messages/%d/withhold-preview" % full.id)]
    for who in ("viewer", "operator", "operator+"):
        await as_(client, who)
        for method, path in routes:
            assert (await client.request(method, path)).status_code == 403, (who, path)
    client.cookies.clear()
    for method, path in routes:
        r = await client.request(method, path, headers={"Authorization": "Bearer %s" % SERVICE_TOKEN})
        assert r.status_code in (401, 403), (path, r.status_code)
    # A Credit Repair conversation: only an admin with the sensitive-data permission sees its hold.
    async with session_scope() as s:
        await s.execute(update(ArchiveSession).where(ArchiveSession.id == stub.session_id).values(
            agent="credit_repair"))
    await as_(client, "admin")
    assert (await client.get("/api/brain/holds")).json()["holds"] == []
    assert (await client.post("/api/brain/holds/%d/keep" % stub.id)).status_code == 404
    assert (await client.post("/api/brain/holds/%d/release" % stub.id)).status_code == 404
    await as_(client, "admin+")
    assert [h["message"] for h in (await client.get("/api/brain/holds")).json()["holds"]] == [stub.id]
    assert (await client.post("/api/brain/holds/%d/keep" % stub.id)).status_code == 200


async def test_holds_routes_409_during_import(client, fx):
    _, stub = await held_stub(client, fx, "zqhold409")
    locks = await imp.LockConnection.open()
    try:
        assert await locks.try_lock(imp.GLOBAL_LOCK)
        await as_(client, "admin")
        for what in ("keep", "release"):
            r = await client.post("/api/brain/holds/%d/%s" % (stub.id, what))
            assert r.status_code == 409 and "import" in r.text, r.text
        assert (await client.get("/api/brain/holds")).status_code == 200  # reading never waits
    finally:
        await locks.close()
    assert (await stones())[stub.source_key].review == identity.PENDING


# --- The withhold preview and its choices -------------------------------------------------------------------------


def _preview_backup(fx: dict[str, str], mark: str) -> dict[str, int]:
    """A conversation where the answer to withhold (uid U) is repeated right away (a duplicate with no row), said again
    later with its own uid (identical text), and shares its uid with another answer (crafted: other text); a CLI
    branch copies all of it keeping the uids; another conversation says the same words (never the same message)."""
    shared = bj.new_uid()
    bj.add_session(fx["db"], "pv_parent", started=T)
    out = {"ask": bj.add_row(fx["db"], "pv_parent", "user", "Please summarise the vendor situation.", T + 1),
           "answer": bj.add_row(fx["db"], "pv_parent", "assistant", secret(mark), T + 2, uid=shared),
           "repeat": bj.add_row(fx["db"], "pv_parent", "assistant", secret(mark), T + 3),
           "again": bj.add_row(fx["db"], "pv_parent", "user", "And once more?", T + 4),
           "identical": bj.add_row(fx["db"], "pv_parent", "assistant", secret(mark), T + 5),
           "other": bj.add_row(fx["db"], "pv_parent", "assistant", "A different answer altogether.", T + 6,
                               uid=shared)}
    bj.cli_branch(fx["db"], "pv_parent", "pv_child", started_at=T + DAY)
    conversation(fx["db"], "pv_other", T + 2 * DAY, [("user", "What did they say?"), ("assistant", secret(mark))])
    return out


async def test_withhold_preview_counts_and_include_identical(client, fx):
    mark = "zqpreview62"
    ids = _preview_backup(fx, mark)
    await run(fx)
    [answer] = await message_of("pv_parent", ids["answer"])
    [identical] = await message_of("pv_parent", ids["identical"])
    [other] = await message_of("pv_parent", ids["other"])
    child_rows = [m for r in bj.rows(fx["db"], "pv_child") for m in await message_of("pv_child", r["id"])]
    child_same = [m for m in child_rows if m.content == secret(mark) and m.source_uid == answer.source_uid]
    child_identical = [m for m in child_rows if m.content == secret(mark) and m.source_uid == identical.source_uid]
    child_other = [m for m in child_rows if m.content == "A different answer altogether."]
    assert len(child_same) == len(child_identical) == len(child_other) == 1
    [elsewhere] = [m for r in bj.rows(fx["db"], "pv_other") for m in await message_of("pv_other", r["id"])
                   if m.content == secret(mark)]
    await as_(client, "admin")
    r = await client.get("/api/brain/messages/%d/withhold-preview" % answer.id)
    assert r.status_code == 200, r.text
    p = r.json()
    assert [x["id"] for x in p["same_message_rows"]] == [child_same[0].id]
    assert sorted(x["id"] for x in p["held_rows"]) == sorted([other.id, child_other[0].id])
    assert sorted(x["id"] for x in p["identical_text_rows"]) == sorted([identical.id, child_identical[0].id])
    assert p["rowless_copies"] >= 1 and p["first_user_message"] is False and p["items_with_this_text"] == []
    assert mark not in r.text  # ids and positions, never text
    assert await stones() == {}  # read-only
    r = await client.post("/api/brain/messages/%d/withhold" % answer.id, json={"include_identical": True})
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["also_withheld"] == [child_same[0].id]
    assert sorted(done["held_for_review"]) == sorted([other.id, child_other[0].id])
    assert done["identical_withheld"] == 2 and done["title_hidden"] is False
    assert done["keys_tombstoned"] >= 6  # its own, the copy, two held, two identical, the repeat (no row)
    st = await stones()
    assert st[identical.source_key].via == identity.IDENTICAL and st[identical.source_key].review == ""
    assert st[other.source_key].review == identity.PENDING
    [entry] = await audit_of("archive.message_withheld")
    assert entry.detail["identical_withheld"] == 2 and entry.detail["also_withheld"] == [child_same[0].id]
    # Identical words in another conversation are never the same message, whatever the admin ticks.
    async with session_scope() as s:
        assert (await s.get(ArchiveMessage, elsewhere.id)).withheld is False
    assert elsewhere.source_key not in st
    second = await run(fx)  # the next import keeps every choice
    for m in (child_same[0], identical, child_identical[0], other, child_other[0]):
        async with session_scope() as s:
            assert (await s.get(ArchiveMessage, m.id)).withheld, m.id
    v = await imp.verify(second["run_id"], bj.paths(fx))
    assert v["passed"], {k: c for k, c in v["verification"]["checks"].items() if not c["ok"]}


async def test_withhold_without_the_box_leaves_identical_text_readable(client, fx):
    mark = "zqnobox62"
    ids = _preview_backup(fx, mark)
    await run(fx)
    [answer] = await message_of("pv_parent", ids["answer"])
    [identical] = await message_of("pv_parent", ids["identical"])
    await withhold(client, answer.id)
    second = await run(fx)
    async with session_scope() as s:
        again = await s.get(ArchiveMessage, identical.id)
    assert (again.withheld, again.content) == (False, secret(mark)) and identical.source_key not in await stones()
    v = await imp.verify(second["run_id"], bj.paths(fx))
    assert v["passed"], {k: c for k, c in v["verification"]["checks"].items() if not c["ok"]}


async def _title(sid: str) -> tuple[str, bool]:
    async with session_scope() as s:
        sess = await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == sid))
        return sess.title, bool((sess.flags or {}).get("title_withheld"))


async def test_hide_title_default_for_first_user_message(client, fx):
    cases = {"t_first": (0, None, True), "t_answer": (1, None, False), "t_asked": (1, True, True),
             "t_kept": (0, False, False)}
    for n, sid in enumerate(cases):
        bj.add_session(fx["db"], sid, started=T + n * DAY, title="Vendor contract for zqtitle62")
        bj.add_row(fx["db"], sid, "user", "What is the vendor contract worth? zqtitle62", T + n * DAY + 1)
        bj.add_row(fx["db"], sid, "assistant", secret("zqtitle62"), T + n * DAY + 2)
    await run(fx)
    await as_(client, "admin")
    for sid, (index, hide, hidden) in cases.items():
        source_id = bj.rows(fx["db"], sid)[index]["id"]
        [m] = await message_of(sid, source_id)
        body = {} if hide is None else {"hide_title": hide}
        r = await client.post("/api/brain/messages/%d/withhold" % m.id, json=body)
        assert r.status_code == 200 and r.json()["title_hidden"] is hidden, (sid, r.text)
        assert await _title(sid) == (("", True) if hidden else ("Vendor contract for zqtitle62", False)), sid
    await run(fx)  # the next import keeps a hidden title hidden
    assert await _title("t_first") == ("", True) and await _title("t_asked") == ("", True)
    assert (await _title("t_answer"))[0] == "Vendor contract for zqtitle62"


async def test_items_with_this_text_are_listed_not_purged(client, fx):
    mark = "zqitems62"
    words = secret(mark)
    for rel, heading in (("knowledge/vendors.md", "Vendors"), ("knowledge/credit.md", "Credit notes")):
        path = os.path.join(fx["profile"], rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# %s\nNotes from the call:\n%s\n" % (heading, words.replace(" is ", "  is\n")))
    conversation(fx["db"], "it_01", T, [("user", "What did the vendor say?"), ("assistant", words)])
    await run(fx)
    async with session_scope() as s:
        items = {i.source_key: i for i in (await s.scalars(select(MemoryItem).where(
            MemoryItem.body.contains(mark)))).all()}
        credit = items["jarvis:file:profile/knowledge/credit.md"]
        await s.execute(update(MemoryItem).where(MemoryItem.id == credit.id).values(agent="credit_repair"))
    vendors = items["jarvis:file:profile/knowledge/vendors.md"]
    [answer] = [m for r in bj.rows(fx["db"], "it_01") for m in await message_of("it_01", r["id"])
                if m.role == "assistant"]
    await as_(client, "admin")  # no sensitive-data permission: the Credit Repair item is not named
    p = (await client.get("/api/brain/messages/%d/withhold-preview" % answer.id)).json()
    assert p["items_with_this_text"] == [{"id": vendors.id, "title": vendors.title}]
    await as_(client, "admin+")
    p = (await client.get("/api/brain/messages/%d/withhold-preview" % answer.id)).json()
    assert sorted(i["id"] for i in p["items_with_this_text"]) == sorted([vendors.id, credit.id])
    r = await client.post("/api/brain/messages/%d/withhold" % answer.id)
    assert r.status_code == 200, r.text
    async with session_scope() as s:  # listed, never removed: a purge is the admin's own decision
        for item_id in (vendors.id, credit.id):
            item = await s.get(MemoryItem, item_id)
            assert item.deleted_at is None and mark in item.body and not item.flags.get("source_withheld")
        assert await s.scalar(text("SELECT count(*) FROM brain_tombstones WHERE reason = 'purged by an admin'")) == 0
    # A short message (under 32 characters) lists nothing: too little to tell.
    conversation(fx["db"], "it_02", T + DAY, [("user", "Yes, go ahead.")])
    await run(fx)
    [short] = [m for r in bj.rows(fx["db"], "it_02") for m in await message_of("it_02", r["id"])]
    await as_(client, "admin+")
    assert (await client.get("/api/brain/messages/%d/withhold-preview" % short.id)).json()[
        "items_with_this_text"] == []


async def test_every_reader_after_a_group_withhold(client, fx, make_worker, service_headers):
    """The whole group (the CLI branch's copy, the held row, the identical rows the admin ticked) is gone from every
    reader once the admin withholds: every role and the n8n token on the task, request and Brain pages, every agent
    path (relevant, prompt_block with memory in prompts on, history, reference notes, agents_view), the Supervisor's
    recall and request summaries, and the copies promoted from any of those rows before the withhold (emptied)."""
    from .test_brain_quotes_readers import CapturingModel, agent_paths, quote_everything, sweep

    mark = "zqgroupall62"
    words = "We finished the vendor contract with %s: forty thousand dollars a year." % mark
    shared = bj.new_uid()
    bj.add_session(fx["db"], "ga_parent", started=T)
    bj.add_row(fx["db"], "ga_parent", "user", "Please summarise the vendor situation.", T + 1)
    answer = bj.add_row(fx["db"], "ga_parent", "assistant", words, T + 2, uid=shared)
    bj.add_row(fx["db"], "ga_parent", "user", "And once more?", T + 4)
    identical = bj.add_row(fx["db"], "ga_parent", "assistant", words, T + 5)
    other = bj.add_row(fx["db"], "ga_parent", "assistant", "We finished it differently: %sother." % mark, T + 6,
                       uid=shared)
    bj.cli_branch(fx["db"], "ga_parent", "ga_child", started_at=T + DAY)
    await run(fx)
    titles = {}
    for name, sid, source_id in (("answer", "ga_parent", answer), ("identical", "ga_parent", identical),
                                 ("other", "ga_parent", other)):
        [row] = await message_of(sid, source_id)
        await as_(client, "operator")
        r = await client.post("/api/brain/messages/%d/promote" % row.id,
                              json={"kind": "project", "title": "Copy %s zqgrouptitle%s" % (name, name)})
        assert r.status_code == 201, r.text
        titles[name] = r.json()["item"]["id"]
        await as_(client, "admin")
        assert (await client.post("/api/brain/items/%d/review" % titles[name],
                                  json={"decision": "approve"})).status_code == 200
    child = [m for r in bj.rows(fx["db"], "ga_child") for m in await message_of("ga_child", r["id"])
             if m.content == words]
    await as_(client, "operator")
    r = await client.post("/api/brain/messages/%d/promote" % child[0].id,
                          json={"kind": "project", "title": "Copy child zqgrouptitlechild"})
    assert r.status_code == 201
    titles["child"] = r.json()["item"]["id"]
    model = CapturingModel()
    request_id = await quote_everything(client, make_worker, model)
    marks = (mark, "zqgrouptitle")
    assert (await sweep(client, service_headers, marks)).get("operator"), "quoted before the withhold"
    [target] = await message_of("ga_parent", answer)
    await as_(client, "admin")
    r = await client.post("/api/brain/messages/%d/withhold" % target.id, json={"include_identical": True})
    assert r.status_code == 200, r.text
    assert await sweep(client, service_headers, marks) == {}
    model2 = CapturingModel()
    await quote_everything(client, make_worker, model2)  # new answers, drafts and prompts after the withhold
    assert await sweep(client, service_headers, marks) == {}
    assert await agent_paths(marks, model2) == {}
    for who in ("viewer", "operator", "operator+", "admin", "admin+"):
        await as_(client, who)
        for page in ("/api/brain/search?q=%s" % mark, "/api/brain/search?q=zqgrouptitle", "/api/brain/items",
                     "/api/brain/items?q=zqgrouptitle", "/api/brain/agents", "/api/brain/overview"):
            assert not [m for m in marks if m in (await client.get(page)).text], (who, page)
        if who != "viewer":
            for sid in ("ga_parent", "ga_child"):
                sess = (await message_of(sid, bj.rows(fx["db"], sid)[0]["id"]))[0].session_id
                assert mark not in (await client.get("/api/brain/sessions/%d" % sess)).text, (who, sid)
    for item_id in titles.values():
        async with session_scope() as s:
            item = await s.get(MemoryItem, item_id)
            assert item.flags.get("source_withheld") and item.body == "" and item.status == "quarantined"
    for m in marks:
        assert await bj.nowhere(m) == [], m
    await as_(client, "operator")
    summary = (await client.get("/api/supervisor/requests/%d" % request_id)).json()["request"]["summary"]
    assert mark not in summary
