"""0.6.2 identity rules, revised (rules062): the two adversarial reviews' confirmed findings, as regressions.

Ported from the skeptics' probes (synthetic data only, Hermes-shaped backups, tests/brain_jarvis.py):
- resurrection (rv062-run1-resurrection.json, findings R0-R7): withheld text that stayed or came back readable;
- over-reach (rv062-run2-overreach.json, findings O0-O5): unrelated or released text withheld without review.
Every test asserts the SAFE outcome: no withheld text readable anywhere, and nothing withheld that an admin did not
withhold or released (a hold, which an admin reviews, is the only uncertain outcome). Each test names its finding.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy import select, text

from app.bootstrap import check_withheld_copies
from app.brain import identity, store
from app.brain import importer as imp
from app.db import session_scope
from app.models import ArchiveMessage, ArchiveSession, BrainWithholdExemption, ImportRecord

from .. import brain_jarvis as bj
from ..conftest import ADMIN
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_identity_migration import _base
from .test_brain_upgrade_062 import LEGACY_SCRIPT, _release_061
from .test_brain_withhold_identity import DAY, T, _fresh_production, conversation, message_of, run, stones, withhold

pytestmark = pytest.mark.usefixtures("clean_db")
ROLES = ("viewer", "operator", "operator+", "admin", "admin+")
BACKEND = Path(__file__).resolve().parents[2]
STUB = "[tool output pruned to save context]"
HEADER = ("[STILL IN PROGRESS \u2014 this is the active request, restated after the compaction boundary because "
          "it was not finished yet. Continue it; do not start over.]")


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


# ======================= Resurrection (R0-R7) =======================


def secret(mark: str) -> str:
    return "The new vendor contract with %s is worth forty thousand dollars a year." % mark


async def readable_rows(mark: str) -> list[tuple]:
    async with session_scope() as s:
        return [tuple(r) for r in (await s.execute(text(
            "SELECT m.id, s.source_session_id, m.source_message_id, m.source_key, m.withheld, m.role "
            "FROM archive_messages m JOIN archive_sessions s ON s.id = m.session_id "
            "WHERE m.content ILIKE :m ORDER BY m.id"), {"m": "%" + mark + "%"})).all()]


async def who_reads(client, mark: str) -> dict:
    """Which readers show the marker: columns, API per role, the Supervisor's history, status."""
    out: dict = {"columns": await bj.nowhere(mark)}
    async with session_scope() as s:
        sessions = list((await s.scalars(select(ArchiveSession.id))).all())
    api = []
    for who in ROLES:
        await as_(client, who)
        r = await client.get("/api/brain/search", params={"q": mark})
        if mark in r.text:
            api.append("search:" + who)
        if who != "viewer":
            r = await client.get("/api/brain/sessions", params={"q": mark, "limit": 200})
            if mark in r.text:
                api.append("sessions?q:" + who)
            r = await client.get("/api/brain/sessions", params={"limit": 200})
            if mark in r.text:
                api.append("sessions:" + who)
            for sid in sessions:
                r = await client.get("/api/brain/sessions/%d" % sid)
                if mark in r.text:
                    api.append("session_detail:%s:%d" % (who, sid))
    out["api"] = api
    async with session_scope() as s:
        hits, _ = await store.AgentMemory(s, "supervisor").history(mark)
        out["supervisor_history"] = any(mark in (h.message.content or "") for h in hits)
    st = await imp.status()
    out["status_readable_matching"] = st["readable_rows_matching_a_withhold"]
    out["readable_rows"] = await readable_rows(mark)
    return out


async def promote_status(client, row_id: int) -> int:
    await as_(client, "operator")
    r = await client.post("/api/brain/messages/%d/promote" % row_id, json={"kind": "project", "title": "Probe copy"})
    return r.status_code


async def withhold_api(client, row_id: int, **body) -> dict:
    await as_(client, "admin")
    p = await client.get("/api/brain/messages/%d/withhold-preview" % row_id)
    r = await client.post("/api/brain/messages/%d/withhold" % row_id, json=body or None)
    assert r.status_code == 200, r.text
    return {"preview": p.json(), "withhold": r.json()}


def show(label: str, value) -> None:
    print("PROBE %s: %s" % (label, json.dumps(value, default=str, sort_keys=True)))


# --- Finding 0: a JARVIS copy with a fresh uid stored before the withhold -----------------------------------------


@pytest.mark.parametrize("how", ["gateway_branch", "legacy_rotation_null_uid"])
async def test_r0_copy_with_fresh_uid_stored_before_withhold(client, tmp_path, how):
    mark = "skzq0%s" % how.replace("_", "")[:10]
    fx = bj.mini(tmp_path / "fx")
    ids = conversation(fx["db"], "p0_parent", T, [("user", "What is the vendor deal worth?"),
                                                  ("assistant", secret(mark)), ("user", "Thanks.")],
                       uid=how == "gateway_branch")
    if how == "gateway_branch":
        bj.gateway_branch(fx["db"], "p0_parent", "p0_child", started_at=T + DAY)
    else:  # Hermes rotation: copies of NULL-uid dicts get a uid minted by stamp_message_uid; the parent stays NULL
        bj.change(fx["db"], "UPDATE sessions SET end_reason = 'compression' WHERE id = ?", "p0_parent")
        bj.add_session(fx["db"], "p0_child", started=T + DAY, parent="p0_parent", title="Continuation")
        for r in bj.rows(fx["db"], "p0_parent"):
            bj.add_row(fx["db"], "p0_child", r["role"], r["content"], r["timestamp"])  # uid None -> minted
    show("child rows", [(r["id"], r["role"], r["timestamp"], bool(r["message_uid"])) for r in
                        bj.rows(fx["db"], "p0_child")])
    await run(fx)
    [orig] = await message_of("p0_parent", ids[1])
    result = await withhold_api(client, orig.id)
    show("preview", {k: result["preview"][k] for k in ("same_message_rows", "held_rows", "identical_text_rows")})
    show("withhold", {k: result["withhold"].get(k) for k in ("also_withheld", "held_for_review")})
    after_withhold = await who_reads(client, mark)
    show("after withhold", after_withhold)
    await run(fx)
    after_reimport = await who_reads(client, mark)
    show("after re-import", after_reimport)
    bj.add_row(fx["db"], "p0_child", "user", "A later question in the child.", T + DAY + 50)
    later = await run(fx)
    after_later = await who_reads(client, mark)
    show("after later backup", after_later)
    if after_later["readable_rows"]:
        show("operator promote of readable copy", await promote_status(client, after_later["readable_rows"][0][0]))
    v = await imp.verify(later["run_id"], bj.paths(fx))
    show("verify passed", v["passed"])
    assert after_later["readable_rows"] == [], "a JARVIS copy keeps the withheld text"


async def test_r0_owner_flow_gateway_branch(client, tmp_path):
    """Test copy: import, withhold, export. Live: import the same backup, apply, then re-import."""
    mark = "skzq0ownerflow"
    fx = bj.mini(tmp_path / "fx")
    ids = conversation(fx["db"], "p0o_parent", T, [("user", "What is the vendor deal worth?"),
                                                   ("assistant", secret(mark)), ("user", "Thanks.")])
    bj.gateway_branch(fx["db"], "p0o_parent", "p0o_child", started_at=T + DAY)
    await run(fx)
    [orig] = await message_of("p0o_parent", ids[1])
    await withhold_api(client, orig.id)
    await bj.settle(fx)  # 0.6.2 plan 4.7: decisions-export refuses until the withhold is finished
    out = tmp_path / "decisions.json"
    await imp.decisions_export(str(out))
    await _fresh_production()
    live = await run(fx)
    applied = await imp.decisions_apply(str(out), live["run_id"], actor=ADMIN[0], apply=True)
    show("apply", applied["removals"])
    after_apply = await who_reads(client, mark)
    show("after apply", after_apply)
    await run(fx)
    after_reimport = await who_reads(client, mark)
    show("after re-import", after_reimport)
    assert after_reimport["readable_rows"] == []


async def test_r0_control_branch_after_withhold(client, tmp_path):
    mark = "skzq0control"
    fx = bj.mini(tmp_path / "fx")
    ids = conversation(fx["db"], "p0c_parent", T, [("user", "What is the vendor deal worth?"),
                                                   ("assistant", secret(mark)), ("user", "Thanks.")])
    await run(fx)
    [orig] = await message_of("p0c_parent", ids[1])
    await withhold_api(client, orig.id)
    bj.gateway_branch(fx["db"], "p0c_parent", "p0c_child", started_at=T + DAY)
    await run(fx)
    after = await who_reads(client, mark)
    show("control after branch import", after)
    assert after["readable_rows"] == []


# --- Finding 1: a fold composite stored before the withhold -------------------------------------------------------


async def test_r1_carrier_fold_stored_before_withhold(client, tmp_path):
    """The in-flight request M is merged onto the compaction carrier (record_absorbed_message(carrier, inflight)):
    the carrier holds M's text after the replay header and lists M's uid in absorbed_message_uids. M's own row is
    archived (compacted = 1)."""
    mark = "skzq1carrier"
    fx = bj.mini(tmp_path / "fx")
    ids = conversation(fx["db"], "p1_s", T, [("user", "Hello."), ("assistant", "Hi."),
                                             ("user", secret(mark))])
    await run(fx)
    [m_row] = await message_of("p1_s", ids[2])
    m_uid = bj.rows(fx["db"], "p1_s")[2]["message_uid"]

    def compact(con):
        con.execute("ALTER TABLE messages ADD COLUMN absorbed_message_uids TEXT")
        con.execute("UPDATE messages SET active = 0, compacted = 1 WHERE session_id = 'p1_s'")
        content = "[CONTEXT COMPACTION] Summary of earlier turns.\n\n%s\n%s" % (HEADER, secret(mark))
        con.execute("INSERT INTO messages (session_id, role, content, message_uid, timestamp, absorbed_message_uids)"
                    " VALUES (?,?,?,?,?,?)", ("p1_s", "user", content, bj.new_uid(), T + 100, json.dumps([m_uid])))
    bj._write_db(fx["db"], compact)
    await run(fx)
    before = await who_reads(client, mark)
    show("before withhold", before["readable_rows"])
    [m_row] = await message_of("p1_s", ids[2])
    result = await withhold_api(client, m_row.id)
    show("preview", {k: result["preview"][k] for k in ("same_message_rows", "held_rows", "identical_text_rows")})
    show("withhold", {k: result["withhold"].get(k) for k in ("also_withheld", "held_for_review")})
    after_withhold = await who_reads(client, mark)
    show("after withhold", after_withhold)
    show("start-up check ran", await check_withheld_copies())
    after_check = await who_reads(client, mark)
    show("after start-up repair", after_check)
    await run(fx)
    after_reimport = await who_reads(client, mark)
    show("after re-import", after_reimport)
    assert after_withhold["readable_rows"] == [], "the fold composite keeps the withheld text after the withhold"


# --- Finding 2: a release lost after a Hermes event ---------------------------------------------------------------

@pytest.mark.parametrize("event", ["compaction", "twin_gone"])
async def test_r2_release_kept_after_hermes_event(client, tmp_path, event):
    mark = "skzq2%s" % event.replace("_", "")
    fx = bj.mini(tmp_path / "fx")
    bj.add_session(fx["db"], "p2_s", started=T)
    bj.add_row(fx["db"], "p2_s", "user", "Run the vendor report.", T + 1)
    tool = bj.add_row(fx["db"], "p2_s", "tool", "REPORT " + secret(mark), T + 2, tool_call_id="call_1",
                      tool_name="terminal")
    bj.add_row(fx["db"], "p2_s", "assistant", "The report ran.", T + 3)
    twin = bj.prune_twin(fx["db"], "p2_s", tool, STUB)
    await run(fx)
    [full] = await message_of("p2_s", twin)
    await withhold_api(client, full.id)
    await run(fx)
    [stub] = await message_of("p2_s", tool)
    show("stub after withhold+import", {"withheld": stub.withheld, "stone": (await stones())[stub.source_key].review})
    await as_(client, "admin")
    r = await client.post("/api/brain/holds/%d/release" % stub.id)
    show("release", r.status_code)
    await run(fx)
    [stub] = await message_of("p2_s", tool)
    show("stub after release+import", {"withheld": stub.withheld, "content": stub.content})
    if event == "compaction":
        bj.change(fx["db"], "UPDATE messages SET active = 0, compacted = 1 WHERE session_id = 'p2_s'")
        bj.add_row(fx["db"], "p2_s", "assistant", "[CONTEXT COMPACTION] Summary.", T + 100)
    else:
        bj.change(fx["db"], "DELETE FROM messages WHERE id = ?", twin)
    await run(fx)
    [stub] = await message_of("p2_s", tool)
    st = (await stones()).get(stub.source_key)
    show("stub after event", {"withheld": stub.withheld, "content": stub.content,
                              "stone": (st.via, st.review) if st else None})
    async with session_scope() as s:
        ex = (await s.execute(text("SELECT anchor_key, source_session_id, source_row_id FROM "
                                   "brain_withhold_exemptions"))).all()
    show("exemptions", [tuple(e) for e in ex])
    await as_(client, "admin")
    r = await client.post("/api/brain/holds/%d/release" % stub.id)
    show("second release", [r.status_code, r.text[:120]])
    assert not stub.withheld, "the admin's release was lost"
    await run(fx)
    [stub] = await message_of("p2_s", tool)
    assert (stub.withheld, stub.content) == (False, STUB), "the admin's release was lost at the next import"
    assert await readable_rows(mark) == [], "the withheld full text is readable"


# --- Finding 3: /retry re-sends the withheld user message ---------------------------------------------------------


async def test_r3_cli_retry_resends_withheld(client, tmp_path):
    mark = "skzq3retry"
    fx = bj.mini(tmp_path / "fx")
    ids = conversation(fx["db"], "p3_s", T, [("user", "Hello."), ("assistant", "Hi."),
                                             ("user", secret(mark)), ("assistant", "A bad answer.")])
    await run(fx)
    [m_row] = await message_of("p3_s", ids[2])
    await withhold_api(client, m_row.id)
    show("after withhold", (await who_reads(client, mark))["readable_rows"])
    # CLI /retry: rewind_user_turn soft-deletes the last user turn and what follows (active = 0), then the text is
    # sent again as a new turn (a new row, uid minted by stamp_message_uid).
    bj.change(fx["db"], "UPDATE messages SET active = 0 WHERE session_id = 'p3_s' AND id >= ?", ids[2])
    bj.add_row(fx["db"], "p3_s", "user", secret(mark), T + 100)
    bj.add_row(fx["db"], "p3_s", "assistant", "A better answer.", T + 101)
    second = await run(fx)
    after = await who_reads(client, mark)
    show("after retry import", after)
    if after["readable_rows"]:
        show("operator promote", await promote_status(client, after["readable_rows"][0][0]))
    v = await imp.verify(second["run_id"], bj.paths(fx))
    show("verify passed", v["passed"])
    assert after["readable_rows"] == [], "the retried copy of the withheld message is readable"


# --- Finding 4: holds on the test copy are not carried to live ----------------------------------------------------


async def test_r4_pending_hold_carried_to_live(client, tmp_path):
    mark = "skzq4hold"
    fx = bj.mini(tmp_path / "fx")
    ids = conversation(fx["db"], "p4_s", T, [("user", "What is it worth?"), ("assistant", secret(mark)),
                                             ("user", "Ok.")], uid=False)
    await run(fx)
    [m_row] = await message_of("p4_s", ids[1])
    await withhold_api(client, m_row.id)
    rep = bj.add_row(fx["db"], "p4_s", "assistant", secret(mark), T + 50, uid=False)  # a later verbatim repeat
    await run(fx)
    [held] = await message_of("p4_s", rep)
    show("test copy repeat", {"withheld": held.withheld, "stone": (await stones())[held.source_key].review})
    await bj.settle(fx)  # 0.6.2 plan 4.1, 4.7: finished on the newest backup that wrote the conversation
    out = tmp_path / "decisions.json"
    await imp.decisions_export(str(out))
    show("exported keys", [r["source_key"] for r in json.loads(out.read_text())["removals"]])
    await _fresh_production()
    live = await run(fx)
    applied = await imp.decisions_apply(str(out), live["run_id"], actor=ADMIN[0], apply=True)
    show("apply", applied["removals"])
    after_apply = await who_reads(client, mark)
    show("live after apply", after_apply)
    await run(fx)
    after_reimport = await who_reads(client, mark)
    show("live after re-import", after_reimport)
    assert after_reimport["readable_rows"] == [], "the test copy holds it; live shows it"


# --- Finding 5: the hidden title reaches no continuation or branch child ------------------------------------------


@pytest.mark.parametrize("how", ["rotate", "gateway_branch"])
async def test_r5_child_title(client, tmp_path, how):
    mark = "skzq5ttl%s" % how[:4]
    fx = bj.mini(tmp_path / "fx")
    first = "Draft letter to %s about the overdue invoice" % mark
    title = first[:48]
    bj.add_session(fx["db"], "p5_parent", started=T, title=title)
    u = bj.add_row(fx["db"], "p5_parent", "user", first, T + 1)
    bj.add_row(fx["db"], "p5_parent", "assistant", "Here is the draft.", T + 2)
    if how == "rotate":
        bj.rotate(fx["db"], "p5_parent", "p5_child", started_at=T + DAY)
        # Hermes moves the title to the continuation (_carry_session_state_to_child; titles are unique)
        bj.change(fx["db"], "UPDATE sessions SET title = ? WHERE id = 'p5_child'", title)
        bj.change(fx["db"], "UPDATE sessions SET title = NULL WHERE id = 'p5_parent'")
    else:
        bj.gateway_branch(fx["db"], "p5_parent", "p5_child", started_at=T + DAY)
        bj.change(fx["db"], "UPDATE sessions SET title = ? WHERE id = 'p5_child'", title + " #2")
    await run(fx)
    [m_row] = await message_of("p5_parent", u)
    result = await withhold_api(client, m_row.id)
    show("withhold title_hidden", result["withhold"].get("title_hidden"))
    async with session_scope() as s:
        titles = [tuple(r) for r in (await s.execute(text(
            "SELECT source_session_id, lineage, title, coalesce((flags->>'title_withheld')::bool, false) "
            "FROM archive_sessions ORDER BY id"))).all()]
    show("titles", titles)
    after = await who_reads(client, mark)
    show("after withhold", after)
    await run(fx)
    after_reimport = await who_reads(client, mark)
    show("after re-import", after_reimport)
    assert after_reimport["columns"] == [] and after_reimport["api"] == []


# --- Finding 6: a short request restated after compaction -------------------------------------------------------


async def test_r6_short_request_restated(client, tmp_path):
    mark = "skzq6"
    fx = bj.mini(tmp_path / "fx")
    short = "Pay %s now." % mark
    ids = conversation(fx["db"], "p6_s", T, [("user", "Hello."), ("assistant", "Hi."), ("user", short)])
    await run(fx)
    [m_row] = await message_of("p6_s", ids[2])
    await withhold_api(client, m_row.id)
    uid = bj.rows(fx["db"], "p6_s")[2]["message_uid"]
    bj.change(fx["db"], "UPDATE messages SET active = 0, compacted = 1 WHERE session_id = 'p6_s'")
    bj.add_row(fx["db"], "p6_s", "assistant", "[CONTEXT COMPACTION] Summary.", T + 100)
    replay = bj.add_row(fx["db"], "p6_s", "user", HEADER + "\n" + short, T + 101, uid=uid)
    await run(fx)
    await as_(client, "admin")
    holds = (await client.get("/api/brain/holds")).json()
    show("holds", [(h["message"], h["rule"], h["compare"], h["content_chars"]) for h in
                   (holds if isinstance(holds, list) else holds.get("holds", []))])
    [held] = await message_of("p6_s", replay)
    assert held.withheld
    hl = holds if isinstance(holds, list) else holds.get("holds", [])
    compare = [h["compare"] for h in hl if h["message"] == held.id]
    show("compare for the restated row", compare)
    r = await client.post("/api/brain/holds/%d/release" % held.id)
    show("release", r.status_code)
    await run(fx)
    after = await who_reads(client, mark)
    show("after release + import", after)
    assert compare != ["different text"], "the hold evidence says 'different text' for a row containing it"
    assert after["readable_rows"] == [] and after["api"] == [], "the restated withheld request is readable"


# --- Finding 7: a tail rollback copy keeps the text --------------------------------------------------------------


async def test_r7_tail_rollback_copy(client, tmp_path):
    mark = "skzq7tail"
    fx = bj.mini(tmp_path / "fx")
    ids = conversation(fx["db"], "p7_s", T, [("user", "Hello."), ("assistant", "Hi."),
                                             ("user", "Give me the headline."), ("assistant", secret(mark))])
    await run(fx)
    bj.hermes_compact(fx["db"], "p7_s", head_n=1, tail_n=2, ts=T + 100)
    await run(fx)
    readable = await readable_rows(mark)
    show("readable before withhold", readable)
    copy_id = readable[0][0]
    await withhold_api(client, copy_id)

    async def recs():
        async with session_scope() as s:
            return [tuple(r) for r in (await s.execute(text(
                "SELECT id, run_id, source_key, action, reason FROM import_records WHERE before::text ILIKE :m"),
                {"m": "%" + mark + "%"})).all()]
    show("records after withhold", await recs())
    after_withhold_cols = await bj.nowhere(mark)
    show("columns after withhold", after_withhold_cols)
    show("start-up check ran", await check_withheld_copies())
    show("columns after start-up repair", await bj.nowhere(mark))
    st = await imp.status()
    show("status", {k: st[k] for k in ("readable_rows_matching_a_withhold", "withheld_copies_with_text_fingerprint")
                    if k in st})
    await run(fx)
    show("columns after re-import", await bj.nowhere(mark))
    show("records after re-import", await recs())
    show("start-up check ran", await check_withheld_copies())
    show("columns after re-import + start-up repair", await bj.nowhere(mark))
    assert after_withhold_cols == [], "the rollback copy keeps the withheld text after the withhold"


# --- Inverses for the fixes: what must stay readable ------------------------------------------------------------


@pytest.mark.parametrize("order", ["branch_before", "branch_after"])
async def test_r0_inverse_branch_copy_of_an_unwithheld_identical_message(client, tmp_path, order):
    """The parent says "Noted, I will send the contract to the vendor today." twice (two Hermes messages, two uids);
    the admin withholds the first only. A gateway branch copies both. The child's copy of the SECOND (not withheld)
    message must stay readable."""
    fx = bj.mini(tmp_path / "fx")
    same = "Noted, I will send the contract to the vendor today."
    ids = conversation(fx["db"], "i0_parent", T, [("user", "Send it?"), ("assistant", same),
                                                  ("user", "And again?"), ("assistant", same)])
    if order == "branch_before":
        bj.gateway_branch(fx["db"], "i0_parent", "i0_child", started_at=T + DAY)
    await run(fx)
    [first] = await message_of("i0_parent", ids[1])
    # 0.6.2 plan 4.5: the stored rows with the same text are listed pre-ticked; the admin unticks the second message
    # and (branch_before) the child's copy of it, and leaves the child's copy of the first ticked
    keep = tuple(m.id for m in await readable_with(same) if m.source_message_id in {
        r["id"] for r in bj.rows(fx["db"], "i0_child") if r["timestamp"] == T + 2}) if order == "branch_before" else ()
    await withhold_leaving(client, first.id, same, keep)
    if order == "branch_after":
        bj.gateway_branch(fx["db"], "i0_parent", "i0_child", started_at=T + DAY)
    await run(fx)
    await run(fx)
    child = bj.rows(fx["db"], "i0_child")
    second_copy = [r for r in child if r["content"] == same and r["timestamp"] == T + 4]
    first_copy = [r for r in child if r["content"] == same and r["timestamp"] == T + 2]
    [c2] = await message_of("i0_child", second_copy[0]["id"])
    [c1] = await message_of("i0_child", first_copy[0]["id"])
    [p2] = await message_of("i0_parent", ids[3])
    show("inverse " + order, {"parent second": p2.withheld, "child copy of first": c1.withheld,
                              "child copy of second": c2.withheld})
    assert not p2.withheld
    assert not c2.withheld, "the copy of a message the admin did not withhold was withheld"


async def test_r7_rollback_of_the_run_that_deleted_the_original(client, tmp_path):
    """After the withhold of the live tail copy, a rollback of the run that deleted the rewound original puts the
    original row back from its rollback copy: with text?"""
    mark = "skzq7rollback"
    fx = bj.mini(tmp_path / "fx")
    conversation(fx["db"], "p7b_s", T, [("user", "Hello."), ("assistant", "Hi."),
                                        ("user", "Give me the headline."), ("assistant", secret(mark))])
    await run(fx)
    bj.hermes_compact(fx["db"], "p7b_s", head_n=1, tail_n=2, ts=T + 100)
    second = await run(fx)
    copy_id = (await readable_rows(mark))[0][0]
    await withhold_api(client, copy_id)
    res = await imp.rollback(second["run_id"], actor=ADMIN[0], confirm=True)
    show("rollback", {k: res.get(k) for k in ("status", "error") if k in res} or str(res)[:300])
    after = await who_reads(client, mark)
    show("after rollback", after)
    assert after["readable_rows"] == [] and after["api"] == []


# ======================= Over-reach (O0-O5) =======================


async def readable_with(text_: str) -> list[ArchiveMessage]:
    async with session_scope() as s:
        return list((await s.scalars(select(ArchiveMessage).where(
            ArchiveMessage.content == text_, ArchiveMessage.withheld.is_(False)).order_by(ArchiveMessage.id))).all())


async def withhold_leaving(client, row_id: int, text_: str, keep: tuple[int, ...] = ()) -> dict:
    """The admin withholds a row and, in the dialog, unticks the other stored rows with the same text she knows are
    other messages (0.6.2 plan 4.5: they are listed pre-ticked, and unticking one writes 'left readable'); keep: the
    ids she leaves ticked. The 0.6.1 'admin saw it' guard (O3) is gone: this choice is how a twin stays readable."""
    others = [m.id for m in await readable_with(text_) if m.id != row_id and m.id not in keep]
    return await withhold_api(client, row_id, leave_readable=others)


async def recs(key: str) -> list[tuple]:
    async with session_scope() as s:
        return [(r.run_id, r.action, r.reason, r.target_id) for r in (await s.scalars(
            select(ImportRecord).where(ImportRecord.source_key == key).order_by(ImportRecord.id))).all()]


async def holds_listed(client) -> list[tuple]:
    await as_(client, "admin")
    r = await client.get("/api/brain/holds")
    assert r.status_code == 200, r.text
    return [(h["message"], h["rule"]) for h in r.json()["holds"]]


def add_turns(db: str, sid: str, start: float, turns: list[tuple[str, str]], **session_kw) -> list[int]:
    bj.add_session(db, sid, started=start, **session_kw)
    return [bj.add_row(db, sid, role, content, start + 10 * (n + 1)) for n, (role, content) in enumerate(turns)]


def rotation_tail(db: str, parent: str, child: str, started_at: float, tail_n: int) -> list[int]:
    """Hermes's rotating compaction (in_place False; hermes_state_compression._publish_compression_child_on_conn):
    the parent ends 'compression', the child gets model_config {'_compressed_from': parent}, then the handoff:
    the summary and the protected tail. Carried tail rows keep their message_uid (stamp_message_uid keeps a dict's
    uid) and get a donor timestamp from the parent's ACTIVE rows with the same content, first match in id order
    (_carry_parent_timestamps). Head rows are not carried (protect_first_n decays to 0 after a first compression)."""
    bj.change(db, "UPDATE sessions SET end_reason = 'compression' WHERE id = ?", parent)
    bj.add_session(db, child, started=started_at, parent=parent, model_config={"_compressed_from": parent},
                   title="Continuation of the probe")

    def go(con: sqlite3.Connection) -> list[int]:
        live = con.execute("SELECT role, content, message_uid, tool_call_id, tool_name, timestamp FROM messages "
                           "WHERE session_id = ? AND active = 1 AND role != 'system' ORDER BY id", (parent,)).fetchall()
        donors: dict[tuple, list[float]] = {}
        for role, content, _, call, name, ts in live:
            donors.setdefault((role, content, call, name), []).append(ts)
        out = [con.execute("INSERT INTO messages (session_id, role, content, message_uid, timestamp) "
                           "VALUES (?,?,?,?,?)", (child, "user", "[CONTEXT COMPACTION] Earlier turns summarised.",
                                                  bj.new_uid(), started_at)).lastrowid]
        for role, content, uid, call, name, _ in live[-tail_n:]:
            ts = donors[(role, content, call, name)].pop(0)
            out.append(con.execute(
                "INSERT INTO messages (session_id, role, content, message_uid, tool_call_id, tool_name, timestamp) "
                "VALUES (?,?,?,?,?,?,?)", (child, role, content, uid, call, name, ts)).lastrowid)
        return out
    return bj._write_db(db, go)


def _family(yes: str) -> list[tuple[str, str]]:
    return [("assistant", "Should I share the biopsy result with your sister?"),
            ("user", yes),                                   # 1: the admin withholds this answer
            ("assistant", "Shared with her."),
            ("user", "Can you book the follow-up visit?"),
            ("assistant", "Shall I book Tuesday at nine?"),
            ("user", yes),                                   # 5: an independent answer, its own uid; readable (O3)
            ("assistant", "Booked for Tuesday."),
            ("user", "Thanks, that is all.")]


# --- Problem 0: a child's copy of ANOTHER (readable) message with the same text ---------------------------------


# --- Problem 1: in-place compaction, first protected-tail row identical to an archived withheld row --------------


async def test_o1_first_tail_row_identical_to_archived_withheld_row(client, fx):
    yes = "Yes, go ahead. zqcov1yes62"
    turns = [("user", "Morning. Let us go through the clinic list."),         # 0 head
             ("assistant", "Sure, where do we start?"),                      # 1 head
             ("user", "The lab results first."),                             # 2 head
             ("assistant", "Should I share the biopsy result with your sister?"),  # 3
             ("user", yes),                                                  # 4: withheld
             ("assistant", "Shared with her."),                              # 5
             ("user", "Can you book the follow-up visit?"),                  # 6
             ("assistant", "Shall I book Tuesday at nine?"),                 # 7
             ("user", yes),                                                  # 8: own uid, readable; first tail row
             ("assistant", "Booked for Tuesday."),                           # 9
             ("user", "Thanks."),                                            # 10
             ("assistant", "You are welcome.")]                              # 11
    ids = add_turns(fx["db"], "c1", T, turns)
    await run(fx)
    await withhold_leaving(client, (await message_of("c1", ids[4]))[0].id, yes)  # row 8 unticked (plan 4.5)
    [r8] = await message_of("c1", ids[8])
    assert not r8.withheld and r8.content == yes
    uid8 = bj.rows(fx["db"], "c1")[8]["message_uid"]
    # Hermes's first in-place compaction: protect_first_n=3 head copies, the summary, the protected tail (rows 8-11)
    bj.hermes_compact(fx["db"], "c1", head_n=3, tail_n=4, ts=T + DAY)
    await run(fx)
    src_rows = bj.rows(fx["db"], "c1")
    copy8 = [r for r in src_rows if r["message_uid"] == uid8 and r["active"] == 1]
    assert len(copy8) == 1
    key = "jarvis:message:c1:%d" % copy8[0]["id"]
    st = (await stones()).get(key)
    print("records of the tail copy of row 8:", await recs(key))
    print("stone on its key:", st and (st.via, st.review, st.anchor_key[-12:], st.message_uid == uid8))
    print("readable rows with the text:", [(m.id, m.state, m.source_key[-24:]) for m in await readable_with(yes)])
    print("holds:", await holds_listed(client))
    await run(fx)
    # the next compaction archives that copy
    bj.add_row(fx["db"], "c1", "user", "One more thing.", T + 2 * DAY)
    bj.hermes_compact(fx["db"], "c1", head_n=0, tail_n=1, ts=T + 3 * DAY)
    await run(fx)
    print("after a second compaction, readable:", [(m.id, m.state) for m in await readable_with(yes)])
    assert await readable_with(yes), "row 8's text (its own uid, never withheld) is readable nowhere"


# --- Problem 2: R-uid's member shortcuts ignore a release and turn a hold into a member -----------------------------

def _tool_backup(fx: dict[str, str], mark: str) -> list[int]:
    bj.add_session(fx["db"], "c2", started=T)
    ids = [bj.add_row(fx["db"], "c2", "user", "Run the vendor report.", T + 1),
           bj.add_row(fx["db"], "c2", "assistant", "", T + 2),
           bj.add_row(fx["db"], "c2", "tool", "REPORT: the vendor %s owes forty thousand dollars." % mark, T + 3,
                      tool_call_id="call_1", tool_name="terminal"),
           bj.add_row(fx["db"], "c2", "assistant", "The report ran.", T + 4)]
    bj.change(fx["db"], "UPDATE messages SET tool_calls = ? WHERE id = ?", json.dumps(
        [{"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": "{\"cmd\": \"report\"}"}}]),
        ids[1])
    return ids


async def _held_stub(client, fx, mark):
    ids = _tool_backup(fx, mark)
    twin = bj.prune_twin(fx["db"], "c2", ids[2], STUB)
    await run(fx)
    [full] = await message_of("c2", twin)
    await withhold(client, full.id)
    [stub] = await message_of("c2", ids[2])
    assert stub.withheld and (await stones())[stub.source_key].review == identity.PENDING
    return ids, stub


async def test_o2_released_stub_archived_by_the_next_compaction(client, fx):
    ids, stub = await _held_stub(client, fx, "zqcov2a62")
    await as_(client, "admin")
    assert (await client.post("/api/brain/holds/%d/release" % stub.id)).status_code == 200
    await run(fx)
    [back] = await message_of("c2", ids[2])
    assert (back.withheld, back.content) == (False, STUB)
    bj.add_row(fx["db"], "c2", "user", "And the next report?", T + DAY)
    bj.add_row(fx["db"], "c2", "assistant", "Tomorrow.", T + DAY + 1)
    bj.hermes_compact(fx["db"], "c2", head_n=0, tail_n=2, ts=T + 2 * DAY)  # the stub row is archived in place
    await run(fx)
    [after] = await message_of("c2", ids[2])
    st = (await stones()).get(after.source_key)
    async with session_scope() as s:
        ex = (await s.scalars(select(BrainWithholdExemption))).all()
    print("released stub after the compaction:", after.withheld, repr(after.content), after.state,
          st and (st.via, st.review), (after.flags or {}).get("withheld_by_admin"), "exemptions:",
          [(e.source_row_id, e.reason) for e in ex])
    r = await client.post("/api/brain/holds/%d/release" % after.id)
    print("release again:", r.status_code, r.text)
    assert (after.withheld, after.content) == (False, STUB), "the admin's release was overridden"


async def test_o2_unreviewed_hold_archived_by_the_next_compaction(client, fx):
    ids, stub = await _held_stub(client, fx, "zqcov2b62")
    assert stub.id in [m for m, _ in await holds_listed(client)]
    bj.add_row(fx["db"], "c2", "user", "And the next report?", T + DAY)
    bj.add_row(fx["db"], "c2", "assistant", "Tomorrow.", T + DAY + 1)
    bj.hermes_compact(fx["db"], "c2", head_n=0, tail_n=2, ts=T + 2 * DAY)
    await run(fx)
    [after] = await message_of("c2", ids[2])
    st = (await stones()).get(after.source_key)
    listed = await holds_listed(client)
    print("unreviewed hold after the compaction:", after.withheld, after.state, st and (st.via, st.review),
          "listed:", listed)
    assert st is not None and st.review == identity.PENDING and after.id in [m for m, _ in listed], (
        "the hold became a permanent member without review")


# --- Problem 3: a release after Hermes's uid backfill (key adoption) -------------------------------------------------


# --- Problem 4: a released row copied by an in-place compaction (protected tail) ------------------------------------


async def test_o4_released_stub_copied_into_the_protected_tail(client, fx):
    ids, stub = await _held_stub(client, fx, "zqcov4rel62")
    await as_(client, "admin")
    assert (await client.post("/api/brain/holds/%d/release" % stub.id)).status_code == 200
    await run(fx)
    assert (await message_of("c2", ids[2]))[0].content == STUB
    bj.add_row(fx["db"], "c2", "user", "Thanks.", T + DAY)
    # the protected tail is the assistant tool call, its (pruned) result, the answer and the thanks
    bj.hermes_compact(fx["db"], "c2", head_n=0, tail_n=4, ts=T + 2 * DAY)
    await run(fx)
    listed = await holds_listed(client)
    print("holds after the compaction:", listed)
    print("readable stub rows:", [(m.id, m.source_message_id, m.state) for m in await readable_with(STUB)])
    assert await readable_with(STUB), "the released text is held again after one routine compaction"


# --- O0 inverses: true copies of a withheld message stay withheld -------------------------------------------


@pytest.mark.parametrize("how", ["cli_branch", "rotation_tail", "gateway_branch"])
async def test_o0_inverse_both_twins_withheld_separately_all_copies_withheld(client, fx, how):
    """Two identical messages, each withheld by its own decision (two groups): every child copy stays withheld."""
    yes = "Yes, go ahead. zqcovx%s62" % how.split("_")[0]
    ids = add_turns(fx["db"], "x", T, _family(yes))
    await run(fx)
    # 0.6.2 plan 4.5: row 5 is listed pre-ticked with row 1; the admin unticks it and withholds it on its own
    await withhold_leaving(client, (await message_of("x", ids[1]))[0].id, yes)
    await withhold(client, (await message_of("x", ids[5]))[0].id)
    if how == "cli_branch":
        bj.cli_branch(fx["db"], "x", "x_child", T + DAY)
    elif how == "gateway_branch":
        bj.gateway_branch(fx["db"], "x", "x_child", T + DAY)
    else:
        rotation_tail(fx["db"], "x", "x_child", T + DAY, tail_n=6)
    await run(fx)
    await run(fx)
    assert await readable_with(yes) == [], [(m.id, m.source_key) for m in await readable_with(yes)]


@pytest.mark.parametrize("how", ["cli_branch", "gateway_branch"])
async def test_o0_inverse_uidless_parent_then_branch_copy_withheld(client, fx, how):
    """A withhold in a backup without uids; the branch copies get fresh uids (the CLI stamps one on a dict that has
    none): the copy of the withheld row stays withheld, the copy of the readable twin stays readable."""
    yes = "Yes, go ahead. zqcovu%s62" % how.split("_")[0]
    bj.add_session(fx["db"], "u", started=T)
    ids = [bj.add_row(fx["db"], "u", role, text_, T + 10 * (n + 1), uid=False)
           for n, (role, text_) in enumerate(_family(yes))]
    await run(fx)
    await withhold_leaving(client, (await message_of("u", ids[1]))[0].id, yes)  # row 5 unticked (plan 4.5)
    bj.gateway_branch(fx["db"], "u", "u_child", T + DAY) if how == "gateway_branch" else (
        bj.cli_branch(fx["db"], "u", "u_child", T + DAY))
    if how == "cli_branch":  # the CLI's message_identity mints a uid for each copied dict without one
        for r in bj.rows(fx["db"], "u_child"):
            if not r["message_uid"]:
                bj.change(fx["db"], "UPDATE messages SET message_uid = ? WHERE id = ?", bj.new_uid(), r["id"])
    await run(fx)
    await run(fx)
    child = bj.rows(fx["db"], "u_child")
    p = bj.rows(fx["db"], "u")
    c1 = [r for r in child if r["timestamp"] == p[1]["timestamp"]]
    c5 = [r for r in child if r["timestamp"] == p[5]["timestamp"]]
    [m1] = await message_of("u_child", c1[0]["id"])
    [m5] = await message_of("u_child", c5[0]["id"])
    print(how, "copy of withheld row 1:", m1.withheld, repr(m1.content), "copy of readable row 5:", m5.withheld)
    assert m1.withheld and m1.content == ""
    [p5] = await message_of("u", ids[5])
    assert not p5.withheld and p5.content == yes, "the parent's readable twin was withheld"


# --- O5: an upgrade from a database the real 0.6.1 importer made -----------------------------------------

SCRATCH = os.environ["TEST_DATABASE_URL"].rsplit("/", 1)[1] + "_rupg"


@pytest.fixture
async def scratch(settings):
    admin = await asyncpg.connect(_base(settings) + "/postgres")
    try:
        await admin.execute("DROP DATABASE IF EXISTS %s WITH (FORCE)" % SCRATCH)
        await admin.execute("CREATE DATABASE %s" % SCRATCH)
    finally:
        await admin.close()
    try:
        yield _base(settings) + "/" + SCRATCH
    finally:
        admin = await asyncpg.connect(_base(settings) + "/postgres")
        try:
            await admin.execute("DROP DATABASE IF EXISTS %s WITH (FORCE)" % SCRATCH)
        finally:
            await admin.close()


def _env(url: str, **extra: str) -> dict[str, str]:
    return {**os.environ, "DATABASE_URL": url.replace("postgresql://", "postgresql+asyncpg://", 1), **extra}


@pytest.fixture
def rel061(tmp_path) -> Path:
    """The real 0.6.1 backend: extracted from the release commit (git), or BRAIN_RELEASE_061_BACKEND."""
    given = os.environ.get("BRAIN_RELEASE_061_BACKEND")
    if given and (Path(given) / "app").is_dir():
        return Path(given)
    return _release_061(tmp_path / "rel061")


def _legacy(rel: Path, url: str, *args: str) -> None:
    script = rel.parent / "legacy_061_r.py"
    script.write_text(LEGACY_SCRIPT)
    done = subprocess.run([sys.executable, str(script), *args], cwd=str(rel), env=_env(url, PYTHONPATH=str(rel)),
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr


# ======================= The revised rules: durability both ways (rules062) =======================


async def test_o2_release_carried_to_live_after_compaction(client, tmp_path):
    """The test copy releases a hold, then JARVIS archives the released row and copies it into the protected tail.
    On live (import, then decisions-apply) the withhold must not take the released row as a member by the archived
    shortcut, and its copy stays released: both readable, the withheld text nowhere."""
    mark = "zqrul2live62"
    fx = bj.mini(tmp_path / "fx")
    ids, stub = await _held_stub(client, fx, mark)
    await as_(client, "admin")
    assert (await client.post("/api/brain/holds/%d/release" % stub.id)).status_code == 200
    await run(fx)
    bj.add_row(fx["db"], "c2", "user", "Thanks.", T + DAY)
    bj.hermes_compact(fx["db"], "c2", head_n=0, tail_n=4, ts=T + 2 * DAY)
    await run(fx)
    assert await readable_with(STUB)
    # 0.6.2 plan 4: finished on the newest backup; Settle lists the compaction's copy of the released stub (the
    # withheld result's uid and tool call, other text) and the admin releases it as she did the stub (plan 4.7: the
    # export waits for that review)
    await bj.settle(fx, keep=False)
    for hold in await _holds_full(client):
        assert identity.IDENTICAL_REL not in hold["relations"], hold
        assert (await client.post("/api/brain/holds/%d/release" % hold["message"])).status_code == 200
    out = tmp_path / "decisions.json"
    await imp.decisions_export(str(out))
    await _fresh_production()
    live = await run(fx)
    applied = await imp.decisions_apply(str(out), live["run_id"], actor=ADMIN[0], apply=True)
    assert applied["removals"].get("withheld") and not applied["removals_not_applied"]
    await run(fx)
    assert await readable_with(STUB), "the released text is withheld on live"
    assert await holds_listed(client) == []
    assert await bj.nowhere(mark) == []


async def test_o0_rotation_with_a_donor_timestamp_before_the_withhold(client, fx):
    """Hermes gives a carried row the timestamp of the parent's first active row with that content
    (_carry_parent_timestamps): the rotated copy of readable row 5 carries row 1's time but row 5's uid. Withholding
    row 1 afterwards must not take that copy: its uid says which row it copies."""
    yes = "Yes, go ahead. zqrul5donor62"
    ids = add_turns(fx["db"], "d0", T, _family(yes))
    rotation_tail(fx["db"], "d0", "d0_child", T + DAY, tail_n=4)
    await run(fx)
    child = [r for r in bj.rows(fx["db"], "d0_child") if r["content"] == yes]
    assert child and child[0]["timestamp"] == bj.rows(fx["db"], "d0")[1]["timestamp"]  # row 1's time
    # 0.6.2 plan 5.3 rv3-fb-4/rv3-ov-2: listed pre-ticked in the dialog; the admin's 'left readable' choice stands
    await withhold_leaving(client, (await message_of("d0", ids[1]))[0].id, yes)
    [copy] = await message_of("d0_child", child[0]["id"])
    assert (copy.withheld, copy.content) == (False, yes)
    await run(fx)
    await run(fx)
    [copy] = await message_of("d0_child", child[0]["id"])
    [p5] = await message_of("d0", ids[5])
    assert (copy.withheld, p5.withheld) == (False, False)
    assert (await message_of("d0", ids[1]))[0].withheld


async def test_o2_a_shared_uid_outside_the_conversation_family_is_held_not_withheld(client, fx):
    """A row of an unrelated conversation that carries the withheld message's uid (a crafted backup: Hermes mints
    uuid4 uids), archived, with other text: an admin reviews it (a hold), it is never a certain member."""
    mark = "zqrul4coll62"
    ids = conversation(fx["db"], "k_s", T, [("user", "Hello."), ("assistant", secret(mark))])
    await run(fx)
    await withhold(client, (await message_of("k_s", ids[1]))[0].id)
    uid = bj.rows(fx["db"], "k_s")[1]["message_uid"]
    bj.add_session(fx["db"], "k_other", started=T + DAY)
    other = bj.add_row(fx["db"], "k_other", "assistant", "An unrelated archived answer.", T + DAY + 1, uid=uid,
                       active=0, compacted=1)
    await run(fx)
    [row] = await message_of("k_other", other)
    st = (await stones())[row.source_key]
    assert row.withheld and st.review == identity.PENDING
    assert row.id in [m for m, _ in await holds_listed(client)]
    await as_(client, "admin")
    assert (await client.post("/api/brain/holds/%d/release" % row.id)).status_code == 200
    await run(fx)
    [row] = await message_of("k_other", other)
    assert (row.withheld, row.content) == (False, "An unrelated archived answer.")
    assert await bj.nowhere(mark) == []


@pytest.mark.parametrize("hide", [True, False])
async def test_r5_title_choice_travels_to_live_and_comes_back_with_the_conversation(client, tmp_path, hide):
    """The admin's title choice is kept with the withhold: decisions-apply on live hides it (or not) as the admin
    chose, and a conversation an import creates again after a rollback hides it too."""
    mark = "zqrul3ttl%s" % ("y" if hide else "n")
    fx = bj.mini(tmp_path / "fx")
    first = "Draft letter to %s about the overdue invoice" % mark
    bj.add_session(fx["db"], "t_s", started=T, title=first[:48])
    u = bj.add_row(fx["db"], "t_s", "user", first, T + 1)
    bj.add_row(fx["db"], "t_s", "assistant", "Here is the draft.", T + 2)
    await run(fx)
    [m] = await message_of("t_s", u)
    await withhold_api(client, m.id, hide_title=hide)
    await bj.settle(fx)  # 0.6.2 plan 4.7
    out = tmp_path / "decisions.json"
    await imp.decisions_export(str(out))
    await _fresh_production()
    live = await run(fx)
    await imp.decisions_apply(str(out), live["run_id"], actor=ADMIN[0], apply=True)

    async def title() -> tuple[str, bool]:
        async with session_scope() as s:
            sess = await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == "t_s"))
            return sess.title, bool((sess.flags or {}).get("title_withheld"))

    expected = ("", True) if hide else (first[:48], False)
    assert await title() == expected
    await imp.rollback(live["run_id"], actor=ADMIN[0], confirm=True)
    await run(fx)
    assert await title() == expected
    assert (await bj.nowhere(mark) == []) == hide  # the title holds the marker only when it was left shown


async def test_r_branch_copy_of_a_held_row_is_held_until_released(client, fx):
    """A gateway branch copies a held row (a prune stub that shares the withheld result's id) with a fresh uid and
    its timestamp: the copy is held with the same evidence, never readable before an admin decides, and never a
    certain member; a release brings it back."""
    mark = "zqrul6hold62"
    ids, stub = await _held_stub(client, fx, mark)
    bj.gateway_branch(fx["db"], "c2", "c2_branch", T + DAY)
    await run(fx)
    copies = [r for r in bj.rows(fx["db"], "c2_branch") if r["content"] == STUB]
    [copy] = await message_of("c2_branch", copies[0]["id"])
    st = (await stones())[copy.source_key]
    # 0.6.2 plan 5.3: held for review, 'new in a later backup', with its computed relations (4.6)
    assert copy.withheld and st.review == identity.PENDING and st.via == identity.LATER_HOLD
    assert identity.SAME_CALL in (st.relations or [])
    assert copy.id in [m for m, _ in await holds_listed(client)]
    await as_(client, "admin")
    assert (await client.post("/api/brain/holds/%d/release" % copy.id)).status_code == 200
    await run(fx)
    [copy] = await message_of("c2_branch", copies[0]["id"])
    assert (copy.withheld, copy.content) == (False, STUB)
    assert await bj.nowhere(mark) == []


@pytest.mark.parametrize("gone", ["parent_deleted", "parent_retried"])
async def test_r0_gateway_branch_copy_after_the_parent_lost_the_withheld_row(client, tmp_path, gone):
    """(The resurrection attacker's probe P4.) After the withhold, the user branches the conversation in the gateway
    (fresh uids, the parent's timestamps); before the next backup the parent loses the withheld row (the conversation
    deleted, or the turn retried away). The branch's copy keeps the withheld row's time and role, which the archive
    keeps for the withheld row: it is held (nothing can compare its text), never readable."""
    mark = "zqrul8gw%s62" % gone[7:10]
    fx = bj.mini(tmp_path / "fx")
    parent, child = "g4_parent", "g4_child"
    ids = conversation(fx["db"], parent, T, [("user", "Hello there."), ("assistant", "Hi."),
                                             ("user", secret(mark)), ("assistant", "Noted, I will do that.")])
    await run(fx)
    [target] = await message_of(parent, ids[2])
    await withhold(client, target.id)
    bj.gateway_branch(fx["db"], parent, child, T + 2 * DAY)
    bj.add_row(fx["db"], child, "user", "Let us continue in the branch.", T + 2 * DAY + 10)
    if gone == "parent_deleted":
        bj.change(fx["db"], "DELETE FROM messages WHERE session_id = ?", parent)
        bj.change(fx["db"], "DELETE FROM sessions WHERE id = ?", parent)
    else:
        bj.change(fx["db"], "DELETE FROM messages WHERE session_id = ? AND id >= ?", parent, ids[2])
        bj.add_row(fx["db"], parent, "user", "Never mind that, what is the weather?", T + 3 * DAY)
    await run(fx)
    after = await who_reads(client, mark)
    assert after["readable_rows"] == [] and after["api"] == [], after
    copies = [r for r in bj.rows(fx["db"], child) if mark in r["content"]]
    [copy] = await message_of(child, copies[0]["id"])
    assert copy.withheld and (await stones())[copy.source_key].review == identity.PENDING
    # 0.6.2 plan 5.3: every other new row of a family session in a later backup is held for review (the branch's own
    # turn too, fail-safe); it is listed for an admin, never readable before one decides
    [own] = [r for r in bj.rows(fx["db"], child) if r["content"] == "Let us continue in the branch."]
    [turn] = await message_of(child, own["id"])
    assert turn.withheld and (await stones())[turn.source_key].review == identity.PENDING
    assert turn.id in [m for m, _ in await holds_listed(client)]


async def test_r4_decisions_apply_holds_the_rewound_twin_on_live(client, tmp_path):
    """(The resurrection attacker's probe P3c.) The test copy withholds an answer (no uids). Live imports the same
    backup, JARVIS compacts in place, and the next import deletes the rewound original and stores its copy. Then
    decisions-apply: no row of the withheld message is stored; the copy Hermes wrote again is held at once (fail-safe:
    another backup than the decision's), not only at the next import."""
    mark = "zqrul9twin62"
    fx = bj.mini(tmp_path / "fx")
    sid = "t3c"
    conversation(fx["db"], sid, T, [("user", "What is the plan for Friday?"), ("assistant", "Lunch at noon."),
                                    ("user", "And the payment?"), ("assistant", secret(mark))], uid=False)
    test_copy = bj.copy(fx, tmp_path / "test_copy")
    await run(test_copy)
    [row] = await readable_rows(mark)
    await withhold(client, row[0])
    await bj.settle(test_copy)  # 0.6.2 plan 4.7
    out = str(tmp_path / "decisions.json")
    await imp.decisions_export(out)
    await _fresh_production()
    await run(fx)
    bj.hermes_compact(fx["db"], sid, head_n=0, tail_n=2, ts=T + DAY)
    second = await run(fx)
    assert await readable_rows(mark)  # before the decisions: live shows it
    res = await imp.decisions_apply(out, second["run_id"], actor=ADMIN[0], apply=True)
    # 0.6.2: decisions-apply no longer re-derives the rewound twin (plan step 6); live's newest import of the
    # conversation is of another backup than the decision's, so the rows that import stored are held at once
    # (plan 5.3 R5/S2/rv3-res-3: a fail-safe hold), and the rows the decision backup stored stay as they are
    assert res["removals"] == {"held for review": 1}, res
    after = await who_reads(client, mark)
    assert after["readable_rows"] == [] and after["api"] == [] and after["columns"] == [], after
    await run(fx)
    assert (await who_reads(client, mark))["readable_rows"] == []
    assert await readable_with("Lunch at noon.")  # the other turns stay


async def test_r5_hidden_title_survives_the_rollback_of_a_run_that_updated_the_conversation(client, tmp_path):
    """(The durability review's probe T2.) Run 2 updates the conversation, then the admin hides its title with a
    withhold; rolling run 2 back puts its `before` back, but never the hidden title, and the next import keeps it
    hidden."""
    mark = "zqrul10roll62"
    fx = bj.mini(tmp_path / "fx")
    first = "Draft letter to %s about the overdue invoice" % mark
    bj.add_session(fx["db"], "t2_s", started=T, title=first[:48])
    u = bj.add_row(fx["db"], "t2_s", "user", first, T + 1)
    bj.add_row(fx["db"], "t2_s", "assistant", "Here is the draft.", T + 2)
    await run(fx)
    bj.add_row(fx["db"], "t2_s", "user", "One more thing.", T + 10)
    second = await run(fx)
    [m] = await message_of("t2_s", u)
    assert (await withhold_api(client, m.id))["withhold"]["title_hidden"] is True
    await imp.rollback(second["run_id"], actor=ADMIN[0], confirm=True)
    assert await bj.nowhere(mark) == []
    await run(fx)
    assert await bj.nowhere(mark) == []


# ======================= rules062-confirm: the eight confirmed attack findings (rv062-attack-rules.json) =============
#
# One principle: a member (withheld for good, never listed) only by provenance; everything uncertain is a hold
# (stored without text, listed with honest evidence, releasable); a release is bound to the content it released.


async def _state(sid: str, row_id: int) -> tuple:
    rows = await message_of(sid, row_id)
    if not rows:
        return ("no stored row",)
    m = rows[0]
    st = (await stones()).get(m.source_key)
    return (m.withheld, m.content, st and st.via, st and st.review)


async def _holds_full(client) -> list[dict]:
    await as_(client, "admin")
    r = await client.get("/api/brain/holds")
    assert r.status_code == 200, r.text
    return r.json()["holds"]


def _micro_merge(db: str, sid: str, first: int, second: int, ts: float) -> int:
    """Hermes's micro-compaction adjacent-user merge (agent/micro_compaction.py, record_absorbed_message: the
    composite keeps the FIRST turn's uid and absorbs the second's), then archive_and_compact: every active row is
    archived and the compacted list written again. Returns the composite's id."""
    def go(con: sqlite3.Connection) -> int:
        if "absorbed_message_uids" not in {r[1] for r in con.execute("PRAGMA table_info(messages)")}:
            con.execute("ALTER TABLE messages ADD COLUMN absorbed_message_uids TEXT")
        live = con.execute("SELECT id, role, content, message_uid, tool_call_id, tool_name FROM messages "
                           "WHERE session_id = ? AND active = 1 ORDER BY id", (sid,)).fetchall()
        con.execute("UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ? AND active = 1", (sid,))
        ins = ("INSERT INTO messages (session_id, role, content, message_uid, tool_call_id, tool_name, timestamp, "
               "absorbed_message_uids) VALUES (?,?,?,?,?,?,?,?)")
        by_id = {r[0]: r for r in live}
        composite = 0
        for r in live:
            if r[0] == second:
                continue
            if r[0] == first:
                b = by_id[second]
                composite = con.execute(ins, (sid, r[1], r[2] + "\n\n" + b[2], r[3], None, None, ts,
                                              json.dumps([b[3]]))).lastrowid
                continue
            con.execute(ins, (sid, r[1], r[2], r[3], r[4], r[5], ts, None))
        return composite
    return bj._write_db(db, go)


END_MARKER = "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"


# 0.6.2 core-1: the "released" variant is retired (a release followed a renumbered live backup through the content
# binding plan 1.2 deletes; a backup other than the decision backup is a later backup, held unless a safe copy: 5.3).
@pytest.mark.parametrize("where", ["no_uids", "cli_branch_child"])
async def test_c3_rewound_original_rollback_copy_loses_the_text_at_withhold(client, tmp_path, where):
    """(Attack finding 3, x6.) An in-place compaction rewinds the protected tail; the next import deletes the rewound
    original (its rollback copy keeps the text) and stores the live copy, which the admin then withholds. The
    importer's twin link (from stored data) blanks that rollback copy at once: nothing at rest, and a rollback of
    that run brings nothing back."""
    mark = "zqc3%s62" % where[:5].replace("_", "")
    fx = bj.mini(tmp_path / "fx")
    turns = [("user", "Hello."), ("assistant", "Hi."), ("user", "Give me the headline."), ("assistant", secret(mark))]
    sid = "c3r"
    if where == "no_uids":
        conversation(fx["db"], sid, T, turns, uid=False)
    else:
        conversation(fx["db"], "c3r_parent", T, turns)
        bj.cli_branch(fx["db"], "c3r_parent", sid, T + DAY)
        bj.add_row(fx["db"], sid, "user", "Continue in the branch.", T + DAY + 10)
    await run(fx)
    bj.hermes_compact(fx["db"], sid, head_n=1, tail_n=3, ts=T + 2 * DAY)
    second = await run(fx)
    [copy] = [r for r in bj.rows(fx["db"], sid) if mark in r["content"] and r["active"] == 1]
    [stored] = await message_of(sid, copy["id"])
    await withhold_api(client, stored.id)
    assert await bj.nowhere(mark) == []
    await imp.rollback(second["run_id"], actor=ADMIN[0], confirm=True)
    after = await who_reads(client, mark)
    assert after["readable_rows"] == [] and after["api"] == [] and after["columns"] == [], after
    assert await readable_with("Give me the headline.")  # the other turns stay


@pytest.mark.parametrize("order", ["branch_before_withhold", "branch_after_withhold"])
async def test_c4_gateway_branch_of_a_donor_timestamp_copy_stays_readable(client, fx, order):
    """(Attack finding 4, x2.) The rotation's copy of readable row 5 got row 1's timestamp (a donor timestamp); a
    gateway branch of the continuation copies it with a fresh uid and that time. Withholding row 1 does not take
    the branch's copy: the nearest level it was copied from is the continuation's copy of row 5, which its uid
    says is row 5's (readable). Row 1 stays withheld."""
    yes = "Yes, go ahead. zqc4donor%s62" % order[7:10]
    ids = add_turns(fx["db"], "c4d", T, _family(yes))
    rotation_tail(fx["db"], "c4d", "c4d_child", T + DAY, tail_n=4)
    if order == "branch_before_withhold":
        bj.gateway_branch(fx["db"], "c4d_child", "c4d_gw", T + 2 * DAY)
    await run(fx)
    # 0.6.2 plan 5.3 rv3-fb-4/rv3-ov-2: never certain (the parent has two rows with that text), listed pre-ticked;
    # the admin's 'left readable' choice stands, and the later branch's copy is a safe copy of a readable row.
    await withhold_leaving(client, (await message_of("c4d", ids[1]))[0].id, yes)
    if order == "branch_after_withhold":
        bj.gateway_branch(fx["db"], "c4d_child", "c4d_gw", T + 2 * DAY)
    await run(fx)
    await run(fx)
    [gw] = [r for r in bj.rows(fx["db"], "c4d_gw") if r["content"] == yes]
    assert gw["timestamp"] == bj.rows(fx["db"], "c4d")[1]["timestamp"]  # row 1's (donor) time
    assert (await _state("c4d_gw", gw["id"]))[:2] == (False, yes)
    assert (await _state("c4d", ids[5]))[:2] == (False, yes)
    assert (await _state("c4d", ids[1]))[0] is True
    assert await holds_listed(client) == []


@pytest.mark.parametrize("order", ["fold_before_withhold", "fold_after_withhold"])
async def test_c5_a_fold_survivors_uid_never_takes_the_other_constituent(client, fx, order):
    """(Attack finding 5, x1.) Micro-compaction folds turn B into A's composite (it keeps A's uid, absorbs B's). The
    admin withholds B: the composite is a member (the fold records B's uid); A's own archived original (A's uid,
    A's text) is another message: readable, never held, promotable, and no release or audit entry was needed."""
    mark = "zqc5fold%s62" % order[5:8]
    a_text = "Please book the dentist for Tuesday at nine in the morning."
    bj.add_session(fx["db"], "c5f", started=T)
    bj.add_row(fx["db"], "c5f", "assistant", "How can I help today?", T + 1)
    a = bj.add_row(fx["db"], "c5f", "user", a_text, T + 2)
    b = bj.add_row(fx["db"], "c5f", "user", secret(mark), T + 3)
    if order == "fold_before_withhold":
        composite = _micro_merge(fx["db"], "c5f", a, b, T + 10)
        await run(fx)
        await withhold(client, (await message_of("c5f", b))[0].id)
        assert (await _state("c5f", a))[:2] == (False, a_text)  # right after the withhold
        # 0.6.2 plan 4.5: at withhold time the composite (it contains B) is only listed pre-ticked; Settle decides
        # it certain by its absorbed uid (4.2 rule 3)
        await bj.settle(fx)
    else:
        await run(fx)
        await withhold(client, (await message_of("c5f", b))[0].id)
        composite = _micro_merge(fx["db"], "c5f", a, b, T + 10)
    await run(fx)
    await run(fx)
    assert (await _state("c5f", composite))[2:] == (identity.FOLD, identity.CERTAIN)
    assert (await _state("c5f", a))[:2] == (False, a_text)
    assert await holds_listed(client) == []
    assert (await who_reads(client, mark))["readable_rows"] == []
    [orig_a] = await message_of("c5f", a)
    assert await promote_status(client, orig_a.id) == 201
    async with session_scope() as s:
        assert (await s.scalars(select(BrainWithholdExemption))).all() == []


async def test_c7_withhold_time_evidence_matches_the_import_and_a_release_sticks(client, fx):
    """(Attack finding 7, x3b.) Micro-compaction folds the short request M and the next turn X into a composite that
    KEEPS M's uid (M's text first). The withhold holds it as 'contains the withheld text' (never 'different text'),
    the next import agrees (a hold, not a member), and the admin's release on that evidence sticks: M's own row
    stays withheld."""
    mark = "zqc7pay"
    short = "Pay %s now." % mark
    bj.add_session(fx["db"], "c7c", started=T)
    bj.add_row(fx["db"], "c7c", "assistant", "How can I help today?", T + 1)
    m = bj.add_row(fx["db"], "c7c", "user", short, T + 2)
    x = bj.add_row(fx["db"], "c7c", "user", "Also the rent for March, please, by Friday.", T + 3)
    composite = _micro_merge(fx["db"], "c7c", m, x, T + 10)
    await run(fx)
    await withhold(client, (await message_of("c7c", m))[0].id)
    [comp] = await message_of("c7c", composite)
    # 0.6.2 plan 4.6: the computed relations (rv3-fb-5/R7), never 'different text'
    at_withhold = [(h["rule"], h["compare"], h["relations"]) for h in await _holds_full(client)
                   if h["message"] == comp.id]
    assert at_withhold == [(identity.UID_HOLD, "contains the withheld text", [identity.CONTAINS, identity.SAME_UID])]
    await run(fx)
    at_import = [(h["rule"], h["compare"], h["relations"]) for h in await _holds_full(client)
                 if h["message"] == comp.id]
    assert at_import == at_withhold
    await as_(client, "admin")
    r = await client.post("/api/brain/holds/%d/release" % comp.id)
    assert r.status_code == 400 and "RELEASE" in r.text  # it contains the withheld text: a typed confirmation
    assert (await client.post("/api/brain/holds/%d/release" % comp.id,
                              json={"confirm": identity.CONFIRM_WORD})).status_code == 200
    await run(fx)
    await run(fx)
    assert (await _state("c7c", composite))[0] is False
    assert (await _state("c7c", m))[0] is True
    assert await holds_listed(client) == []


# ======================= The lead's decision on the prune twin (deviation 61 of 2026-10-21) =========================

