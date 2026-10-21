"""0.6.2 review round 2: the identity-rule findings (stage 1: S1-S14 of findings-062-round2.md), the durability
findings and the remaining readers findings (stage 2: S15-S24), as regressions. Stage 2 runs scripts/restore.ps1 and
scripts/start.ps1 themselves under PowerShell with a stand-in docker (as tests/unit/test_restore_ps1.py), the real 0.6.1
release where a finding needs it, and restore.ps1's own steps against a real pg_dump of the test database.

Ported from the attackers' and the skeptics' probes (rv2/<lens> and rv2/c_<lens>): Hermes-shaped backups (gateway and
CLI /branch, single-session delete that orphans children, in-place compaction, gateway /retry, uid backfill, prune
rewrite, rotation with donor timestamps), the real 0.6.1 importer where a finding needs it. Synthetic data only, a
unique marker per test.

Every test asserts the SAFE outcome under the lead's rule principle: certain (withheld for good, never listed) only
by provenance; everything uncertain is a listed, releasable hold with honest evidence; a release covers only the
content it released; never an unrelated message withheld because its text matches. Each test names its finding.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import subprocess
import sys
from typing import Any

import asyncpg
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.brain import identity, review, store
from app.brain import importer as imp
from app.db import session_scope
from app.models import ArchiveMessage, ArchiveSession, BrainTombstone, BrainWithholdExemption, ImportRecord

from .. import brain_jarvis as bj
from ..conftest import ADMIN
from .test_brain_agents import allow_ai_context, ask
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_rules_062r import BACKEND, HEADER, _env, _legacy, rel061, scratch  # noqa: F401 - fixtures
from .test_brain_schema import _migrate
from .test_brain_withhold_identity import DAY, _fresh_production, message_of, run, stones, withhold

pytestmark = pytest.mark.usefixtures("clean_db")

ALL = imp.Selection(all=True)
T = 1796000000.0


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


def secret(mark: str) -> str:
    return "Payroll note: the bonus pool code is %s and must stay private." % mark


def show(label: str, value: Any) -> None:
    print("PROBE %s: %s" % (label, json.dumps(value, default=str, sort_keys=True)))


# --- Hermes models (from hermes-src, as the skeptics wrote them) -------------------------------------------------


def db_do(db: str, fn: Any) -> Any:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        out = fn(con)
        con.commit()
        con.execute("PRAGMA journal_mode = DELETE")
        return out
    finally:
        con.close()


def hermes_delete_session(db: str, sid: str) -> None:
    """hermes_state_sessions.delete_session: children orphaned (parent NULL; model_config keeps _branched_from),
    the session's messages and row deleted."""
    def go(con):
        con.execute("UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id = ?", (sid,))
        con.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
        con.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    db_do(db, go)


def gw_branch(db: str, parent: str, child: str, started: float, title: str) -> list[int]:
    """gateway /branch (_BRANCH_COPIED_FIELDS): content, call id, tool name and timestamp copied, a fresh uid."""
    bj.add_session(db, child, started=started, parent=parent, model_config={"_branched_from": parent}, title=title)

    def go(con):
        out = []
        for r in con.execute("SELECT role, content, tool_call_id, tool_name, timestamp FROM messages "
                             "WHERE session_id = ? AND active = 1 AND role != 'system' ORDER BY id",
                             (parent,)).fetchall():
            out.append(con.execute(
                "INSERT INTO messages (session_id, role, content, tool_call_id, tool_name, timestamp, message_uid) "
                "VALUES (?,?,?,?,?,?,?)", (child, r[0], r[1], r[2], r[3], r[4], bj.new_uid())).lastrowid)
        return out
    return db_do(db, go)


def cli_branch(db: str, parent: str, child: str, started: float, title: str) -> list[int]:
    """CLI /branch: the parent ends 'branched'; the child keeps the rows with their uids."""
    bj.change(db, "UPDATE sessions SET end_reason = 'branched' WHERE id = ?", parent)
    bj.add_session(db, child, started=started, parent=parent, model_config={"_branched_from": parent}, title=title)
    return _carry(db, parent, child)


def _carry(db: str, parent: str, child: str) -> list[int]:
    def go(con):
        out = []
        for r in con.execute("SELECT role, content, tool_call_id, tool_name, timestamp, message_uid FROM messages "
                             "WHERE session_id = ? AND active = 1 AND role != 'system' ORDER BY id",
                             (parent,)).fetchall():
            out.append(con.execute(
                "INSERT INTO messages (session_id, role, content, tool_call_id, tool_name, timestamp, message_uid) "
                "VALUES (?,?,?,?,?,?,?)", (child, *tuple(r))).lastrowid)
        return out
    return db_do(db, go)


def rotate_with_title(db: str, parent: str, child: str, started: float) -> list[int]:
    """Rotating compaction: the parent ends 'compression', the continuation carries the rows (same uids), and
    _carry_session_state_to_child moves the title (the parent's is cleared)."""
    title = db_do(db, lambda con: con.execute("SELECT title FROM sessions WHERE id = ?", (parent,)).fetchone()[0])
    bj.change(db, "UPDATE sessions SET end_reason = 'compression', title = NULL WHERE id = ?", parent)
    bj.add_session(db, child, started=started, parent=parent, title=title)
    return _carry(db, parent, child)


def rotate_plain(db: str, parent: str, child: str, started: float) -> list[int]:
    bj.change(db, "UPDATE sessions SET end_reason = 'compression' WHERE id = ?", parent)
    bj.add_session(db, child, started=started, parent=parent, title="Continuation")
    return _carry(db, parent, child)


def hermes_backfill(db: str) -> None:
    """hermes_state_schema._advance_message_uid_backfill (one full pass)."""
    db_do(db, lambda con: con.execute(
        "UPDATE messages SET message_uid = lower(hex(randomblob(16))) WHERE message_uid IS NULL"))


def hermes_compact_in_place(db: str, sid: str, head_n: int, tail_n: int, now: float) -> list[int]:
    """_archive_and_compact_on_conn: the last tail_n active rows rewound, the rest archived; head copies, a summary and
    tail copies inserted, each keeping its uid and its timestamp."""
    def go(con):
        live = [dict(r) for r in con.execute(
            "SELECT * FROM messages WHERE session_id = ? AND active = 1 AND role != 'system' ORDER BY id", (sid,))]
        head, tail = live[:head_n], live[len(live) - tail_n:]
        for r in tail:
            con.execute("UPDATE messages SET active = 0, compacted = 0 WHERE id = ?", (r["id"],))
        con.execute("UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ? AND active = 1", (sid,))
        out = []

        def ins(r):
            return con.execute(
                "INSERT INTO messages (session_id, role, content, tool_call_id, tool_name, timestamp, message_uid) "
                "VALUES (?,?,?,?,?,?,?)", (sid, r["role"], r["content"], r["tool_call_id"], r["tool_name"],
                                           r["timestamp"], r["message_uid"])).lastrowid
        for r in head:
            out.append(ins(r))
        out.append(ins({"role": "assistant", "content": "[CONTEXT COMPACTION] earlier turns summarised.",
                        "tool_call_id": None, "tool_name": None, "timestamp": now, "message_uid": bj.new_uid()}))
        for r in tail:
            out.append(ins(r))
        return out
    return db_do(db, go)


def hermes_gateway_retry(db: str, sid: str, reply: str = "Retried answer.", ts: float = 0.0) -> dict[int, int]:
    """gateway /retry (replace_messages_in_transaction(truncated, active_only=True)): every active row deleted, the
    history before the last user turn re-inserted with its uid and timestamp under new ids, then the re-sent user turn
    and a new reply (fresh uids). Returns old id -> new id."""
    def go(con):
        live = [dict(r) for r in con.execute(
            "SELECT * FROM messages WHERE session_id = ? AND active = 1 ORDER BY id", (sid,))]
        last_user = max(i for i, r in enumerate(live) if r["role"] == "user")
        truncated, resent = live[:last_user], live[last_user]
        con.execute("DELETE FROM messages WHERE session_id = ? AND active = 1", (sid,))
        out = {}
        for r in truncated:
            out[r["id"]] = con.execute(
                "INSERT INTO messages (session_id, role, content, tool_call_id, tool_name, timestamp, message_uid) "
                "VALUES (?,?,?,?,?,?,?)", (sid, r["role"], r["content"], r["tool_call_id"], r["tool_name"],
                                           r["timestamp"], r["message_uid"])).lastrowid
        con.execute("INSERT INTO messages (session_id, role, content, timestamp, message_uid) VALUES (?,?,?,?,?)",
                    (sid, "user", resent["content"], ts, bj.new_uid()))
        con.execute("INSERT INTO messages (session_id, role, content, timestamp, message_uid) VALUES (?,?,?,?,?)",
                    (sid, "assistant", reply, ts + 1, bj.new_uid()))
        return out
    return db_do(db, go)


def hermes_prune(db: str, row: int, stub: str = "[Old tool output cleared to save context space]") -> int:
    """hermes_state_prune_rewrite.rewrite_pruned_rows: INSERT a twin of every column (active 0, compacted 1), then
    UPDATE the row's content in place. Returns the twin's id."""
    def go(con):
        cols = [r[1] for r in con.execute("PRAGMA table_info(messages)") if r[1] not in ("id", "active", "compacted")]
        twin = con.execute("INSERT INTO messages (%s, active, compacted) SELECT %s, 0, 1 FROM messages WHERE id = ?"
                           % (", ".join(cols), ", ".join(cols)), (row,)).lastrowid
        con.execute("UPDATE messages SET content = ? WHERE id = ?", (stub, row))
        return twin
    return db_do(db, go)


def legacy_rotation_with_donor(db: str, parent: str, child: str, started_at: float, carried_ids: list[int]) -> None:
    """Hermes rotation of legacy (uid-less) rows: the child gets the carried tail, each with a fresh uid and the
    timestamp of the FIRST active parent row with the same content (_carry_parent_timestamps, first match wins)."""
    bj.change(db, "UPDATE sessions SET end_reason = 'compression' WHERE id = ?", parent)
    bj.add_session(db, child, started=started_at, parent=parent, title="Continuation")
    con = sqlite3.connect(db)
    donors: dict = {}
    for _rid, role, content, ts in con.execute("SELECT id, role, content, timestamp FROM messages "
                                               "WHERE session_id = ? AND active = 1 ORDER BY id", (parent,)).fetchall():
        donors.setdefault((role, content), []).append(ts)
    for rid in carried_ids:
        role, content = con.execute("SELECT role, content FROM messages WHERE id = ?", (rid,)).fetchone()
        ts = donors[(role, content)].pop(0)
        con.execute("INSERT INTO messages (session_id, role, content, message_uid, timestamp) VALUES (?,?,?,?,?)",
                    (child, role, content, bj.new_uid(), ts))
    con.commit()
    con.close()


# --- Readers ---------------------------------------------------------------------------------------------------


async def stored_at(sid: str, row: Any) -> list[ArchiveMessage]:
    async with session_scope() as s:
        return list((await s.scalars(select(ArchiveMessage).join(
            ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id).where(
            ArchiveSession.source_session_id == sid, ArchiveMessage.source_message_id == str(row))
            .order_by(ArchiveMessage.id))).all())


async def rows_of(sid: str) -> list[ArchiveMessage]:
    async with session_scope() as s:
        return list((await s.scalars(select(ArchiveMessage).join(
            ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id).where(
            ArchiveSession.source_session_id == sid).order_by(ArchiveMessage.position, ArchiveMessage.id))).all())


async def archive_rows(mark: str) -> list[dict[str, Any]]:
    async with session_scope() as s:
        out = []
        for m, sid in (await s.execute(select(ArchiveMessage, ArchiveSession.source_session_id).join(
                ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id).order_by(ArchiveMessage.id))).all():
            w = (m.flags or {}).get("withheld_by_admin") or {}
            st = await s.get(BrainTombstone, m.source_key)
            out.append({"id": m.id, "sid": sid, "row": m.source_message_id, "key": m.source_key, "role": m.role,
                        "withheld": m.withheld, "has_mark": mark in (m.content or ""), "uid": m.source_uid,
                        "via": st.via if st else w.get("via"), "review": st.review if st else w.get("review"),
                        "released": bool(w.get("released_at"))})
        return out


async def api_withhold(client, mid: int, **body) -> dict:
    await as_(client, "admin")
    r = await client.post("/api/brain/messages/%d/withhold" % mid, json=body or None)
    assert r.status_code == 200, r.text
    return r.json()


async def holds_of(client) -> list[dict]:
    await as_(client, "admin+")
    r = await client.get("/api/brain/holds")
    assert r.status_code == 200, r.text
    return r.json()["holds"]


async def who_reads(client, mark: str) -> dict:
    """Where the marker is readable: database columns, search and session detail per role, the Supervisor's history
    and recall; with status and the holds listed."""
    out: dict = {"db_columns": await bj.nowhere(mark)}
    async with session_scope() as s:
        sids = list((await s.scalars(select(ArchiveSession.id))).all())
    for who in ("viewer", "operator", "admin", "admin+"):
        await as_(client, who)
        r = await client.get("/api/brain/search", params={"q": mark})
        out["search:" + who] = mark in r.text
        if who != "viewer":
            out["detail:" + who] = any([mark in (await client.get("/api/brain/sessions/%d" % i)).text for i in sids])
    async with session_scope() as s:
        hits, _ = await store.AgentMemory(s, "supervisor").history(mark)
        out["supervisor_history"] = any(mark in (h.message.content or "") for h in hits)
    rec = await ask("what was the bonus pool code %s" % mark)
    out["supervisor_recall"] = mark in json.dumps(rec.data, default=str) or mark in (rec.summary or "")
    st = await imp.status()
    out["status_readable_matching"] = st.get("readable_rows_matching_a_withhold")
    out["holds"] = [(h["message"], h.get("rule")) for h in await holds_of(client)]
    return out


def leaked(out: dict) -> bool:
    return bool(out["db_columns"]) or any(v is True for k, v in out.items()
                                          if k.startswith(("search:", "detail:", "supervisor_")))


# ======================= S1: a gateway /branch copy whose link to the withheld row is broken =======================


def s1_root(fx: dict[str, str], sid: str, mark: str) -> list[int]:
    """A conversation; the withheld message is its SECOND user message (not the title's row)."""
    bj.add_session(fx["db"], sid, started=T, title="Quarterly planning")
    return [bj.add_row(fx["db"], sid, role, content, T + 10 * n + 1) for n, (role, content) in enumerate(
        [("user", "Let us plan the quarter."), ("assistant", "Sure, where to start?"), ("user", secret(mark)),
         ("assistant", "Understood, noted."), ("user", "Now the hiring plan."), ("assistant", "Here it is.")])]


async def copy_state(sid: str, mark: str) -> list[tuple]:
    return [(r["row"], r["withheld"], r["via"], r["review"]) for r in await archive_rows(mark)
            if r["sid"] == sid and (r["has_mark"] or r["withheld"])]


async def test_s1_branch_of_a_branch_with_the_middle_branch_deleted(client, fx):
    """S1 (a): the user branches, branches the branch, and deletes the first branch between two imports. The second
    branch's copy of the withheld row (fresh uid, copied timestamp) has no link to it any more: a listed hold
    ('copied by JARVIS, nothing to compare'), never readable; verify and status agree."""
    mark = "qrv2s1a91"
    ids = s1_root(fx, "s1a_p", mark)
    await run(fx)
    [w] = await stored_at("s1a_p", ids[2])
    await api_withhold(client, w.id)
    gw_branch(fx["db"], "s1a_p", "s1a_b1", T + 500, "Quarterly planning #2")
    gw_branch(fx["db"], "s1a_b1", "s1a_b2", T + 600, "Quarterly planning #3")
    hermes_delete_session(fx["db"], "s1a_b1")
    r = (await run(fx))["run_id"]
    out = await who_reads(client, mark)
    state = await copy_state("s1a_b2", mark)
    v = await imp.verify(r, bj.paths(fx))
    show("S1a", {**out, "b2": state, "verify": v["passed"]})
    assert not leaked(out), out
    assert [s[1:] for s in state] == [(True, identity.COPY_UNCOMPARED, identity.PENDING)]
    assert any(rule == identity.COPY_UNCOMPARED for _, rule in out["holds"])
    assert out["status_readable_matching"] == 0 and v["passed"]


async def test_s1_parent_deleted_then_the_creating_run_rolled_back(client, fx):
    """S1 (b): the parent is deleted in JARVIS and the archive lost the withheld row (the run that stored it was
    rolled back): the stone keeps the withheld row's time, so the branch copy at that time is held."""
    mark = "qrv2s1b94"
    ids = s1_root(fx, "s1b_p", mark)
    r1 = (await run(fx))["run_id"]
    [w] = await stored_at("s1b_p", ids[2])
    await api_withhold(client, w.id)
    gw_branch(fx["db"], "s1b_p", "s1b_b", T + 500, "Quarterly planning #2")
    hermes_delete_session(fx["db"], "s1b_p")
    await imp.rollback(r1, actor="probe", confirm=True)
    await run(fx)
    out = await who_reads(client, mark)
    async with session_scope() as s:
        own = await s.get(BrainTombstone, w.source_key)
    state = await copy_state("s1b_b", mark)
    show("S1b", {**out, "b": state, "stone_at": own.source_at if own else None})
    assert own is not None and own.source_at is not None
    assert not leaked(out), out
    assert [s[1:] for s in state] == [(True, identity.COPY_UNCOMPARED, identity.PENDING)]


async def test_s1_test_copy_to_live_branch_made_after_the_test_copys_backup(client, fx, tmp_path):
    """S1 (c): the decisions file carries the withheld row's time (at_row); on live the parent was deleted before
    live's first import, so only a branch copy (fresh uid) is there: decisions-apply holds it at once, and the next
    import keeps it held."""
    mark = "qrv2s1c95"
    ids = s1_root(fx, "s1c_p", mark)
    await run(fx)
    [w] = await stored_at("s1c_p", ids[2])
    await api_withhold(client, w.id)
    f = tmp_path / "decisions.json"
    await imp.decisions_export(str(f))
    carried = [r for r in json.loads(f.read_text())["removals"] if r["source_key"] == w.source_key]
    assert carried and carried[0].get("at_row"), carried
    await _fresh_production()
    gw_branch(fx["db"], "s1c_p", "s1c_b", T + 500, "Quarterly planning #2")
    hermes_delete_session(fx["db"], "s1c_p")
    live = (await run(fx))["run_id"]
    res = await imp.decisions_apply(str(f), live, actor=ADMIN[0], apply=True)
    after_apply = await who_reads(client, mark)
    await run(fx)
    after_next = await who_reads(client, mark)
    show("S1c", {"apply": res["removals"], "after_apply": after_apply, "after_next": after_next,
                 "b": await copy_state("s1c_b", mark)})
    assert res["removals"] == {"held for review": 1}
    assert not leaked(after_apply) and not leaked(after_next)
    assert after_apply["status_readable_matching"] == 0
    assert [s[1:3] for s in await copy_state("s1c_b", mark)] == [(True, identity.COPY_UNCOMPARED)]


async def test_s1_status_counts_a_copy_left_readable_at_a_withheld_rows_time(client, fx):
    """S1 (3): a grandchild copy stored readable before the withhold, through a branch already deleted, is not a
    direct child, so the withhold cannot reach it; status counts it (the deployment gate is not 0) until the next
    import holds it."""
    mark = "qrv2s1s96"
    ids = s1_root(fx, "s1s_p", mark)
    gw_branch(fx["db"], "s1s_p", "s1s_b1", T + 500, "Quarterly planning #2")
    gw_branch(fx["db"], "s1s_b1", "s1s_b2", T + 600, "Quarterly planning #3")
    hermes_delete_session(fx["db"], "s1s_b1")
    await run(fx)
    [w] = await stored_at("s1s_p", ids[2])
    await api_withhold(client, w.id)
    st = await imp.status()
    assert st["readable_rows_matching_a_withhold"] == 1, st
    await run(fx)
    out = await who_reads(client, mark)
    assert not leaked(out) and out["status_readable_matching"] == 0, out


async def test_s1_inverse_rows_at_other_times_and_uid_keeping_copies_stay_readable(client, fx):
    """S1 inverse: in the broken chain, a copy at another time stays readable; and a uid-keeping copy (CLI branch) of
    an independent identical message whose own row the archive still holds stays readable after its ancestors left
    the backup (rv2 overreach C3: the donor timestamp gives it the withheld row's time)."""
    mark = "qrv2s1i97"
    ids = s1_root(fx, "s1i_p", mark)
    await run(fx)
    [w] = await stored_at("s1i_p", ids[2])
    await api_withhold(client, w.id)
    gw_branch(fx["db"], "s1i_p", "s1i_b1", T + 500, "Quarterly planning #2")
    gw_branch(fx["db"], "s1i_b1", "s1i_b2", T + 600, "Quarterly planning #3")
    hermes_delete_session(fx["db"], "s1i_b1")
    await run(fx)
    readable = [m.content for m in await rows_of("s1i_b2") if not m.withheld]
    assert sorted(readable) == sorted(["Let us plan the quarter.", "Sure, where to start?", "Understood, noted.",
                                       "Now the hiring plan.", "Here it is."]), readable
    # C3: rotation (uids kept; Hermes gives the carried copy of row 5 the time of the identical row 1), CLI branch of
    # the continuation, then the parent and the continuation pruned from JARVIS (the branch orphaned).
    yes = "Yes, share it with her. zqrv2s1c3"
    db = fx["db"]
    bj.add_session(db, "c3", started=T + 2 * DAY)
    turns = [("assistant", "Shall I share the zq file with Anna?"), ("user", yes), ("assistant", "Shared."),
             ("user", "And the budget?"), ("assistant", "Shall I share it with Anna too?"), ("user", yes),
             ("assistant", "Shared the budget."), ("user", "Thanks.")]
    c3 = [bj.add_row(db, "c3", r, c, T + 2 * DAY + n + 1) for n, (r, c) in enumerate(turns)]
    src = {r["id"]: r for r in bj.rows(db, "c3")}
    bj.change(db, "UPDATE sessions SET end_reason = 'compression' WHERE id = 'c3'")
    bj.add_session(db, "c3_child", started=T + 3 * DAY, parent="c3", title="Continuation")
    for rid in c3[4:]:
        r = src[rid]
        ts = src[c3[1]]["timestamp"] if r["content"] == yes else r["timestamp"]
        bj.add_row(db, "c3_child", r["role"], r["content"], ts, uid=r["message_uid"])
    await run(fx)
    [w3] = await stored_at("c3", c3[1])
    await api_withhold(client, w3.id)
    bj.cli_branch(db, "c3_child", "c3_branch", T + 4 * DAY)
    await run(fx)
    bj.change(db, "UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id IN ('c3', 'c3_child')")
    bj.change(db, "DELETE FROM messages WHERE session_id IN ('c3', 'c3_child')")
    bj.change(db, "DELETE FROM sessions WHERE id IN ('c3', 'c3_child')")
    await run(fx)
    await run(fx)
    branch = [m for m in await rows_of("c3_branch") if m.role == "user" and m.at is not None
              and abs(m.at.timestamp() - src[c3[1]]["timestamp"]) < 1e-3]
    show("S1 inverse C3", [(m.source_message_id, m.withheld, m.content) for m in branch])
    assert branch and all(not m.withheld and m.content == yes for m in branch)


# ======================= S2: decisions-apply when live never stored the binding's JARVIS row id =======================


def s2_conv(fx: dict[str, str], sid: str, mark: str) -> list[int]:
    bj.add_session(fx["db"], sid, started=T, title="Ops chat")
    return [bj.add_row(fx["db"], sid, role, content, T + 10 * n + 1, uid=False) for n, (role, content) in enumerate(
        [("user", "Morning, a few ops questions."), ("assistant", "Go ahead."), ("user", secret(mark)),
         ("assistant", "Got it."), ("user", "And the backup schedule?"), ("assistant", "Nightly at two.")])]


@pytest.mark.parametrize("event", ["compact_tail", "gateway_retry"])
async def test_s2_decisions_apply_holds_the_live_copy_of_a_row_live_never_stored(client, fx, tmp_path, event):
    """S2: the test copy's backup predates JARVIS's uid backfill (the binding names row 3, no uid); live was compacted
    in place (the tail copy keeps the uid and time) or /retried (rows re-inserted with new ids, their uids and times)
    before its first import. decisions-apply holds the stored row of that conversation, role and time ('same
    conversation, role and time; nothing to compare'), and every later import keeps it withheld."""
    mark = "qrv2s2%s7" % event[:3]
    sid = "s2" + event[:3]
    ids = s2_conv(fx, sid, mark)
    await run(fx)  # the test copy
    [w] = await stored_at(sid, ids[2])
    await api_withhold(client, w.id)
    f = tmp_path / "decisions.json"
    await imp.decisions_export(str(f))
    await _fresh_production()
    hermes_backfill(fx["db"])
    if event == "compact_tail":
        hermes_compact_in_place(fx["db"], sid, head_n=1, tail_n=4, now=T + 1000)
    else:
        hermes_gateway_retry(fx["db"], sid, ts=T + 1000)
    live = (await run(fx))["run_id"]
    res = await imp.decisions_apply(str(f), live, actor=ADMIN[0], apply=True)
    after_apply = await who_reads(client, mark)
    await run(fx)
    after_next = await who_reads(client, mark)
    await run(fx)
    after_third = await who_reads(client, mark)
    show("S2 %s" % event, {"apply": res["removals"], "after_apply": after_apply, "after_next": after_next,
                           "after_third": after_third})
    assert res["removals"] == {"held for review": 1}, res
    assert after_apply["holds"] and after_apply["status_readable_matching"] == 0
    for out in (after_apply, after_next, after_third):
        assert not leaked(out), out
    others = [m.content for m in await rows_of(sid) if not m.withheld and m.role == "user"]
    assert "And the backup schedule?" in others  # the conversation's other rows stay readable


async def test_s2_inverse_a_release_of_the_same_time_hold_sticks(client, fx, tmp_path):
    """S2 inverse: the same-time hold is listed with honest evidence and releasable; the release sticks, and the
    conversation's other rows were never touched."""
    mark = "qrv2s2inv8"
    sid = "s2inv"
    ids = s2_conv(fx, sid, mark)
    await run(fx)
    [w] = await stored_at(sid, ids[2])
    await api_withhold(client, w.id)
    f = tmp_path / "decisions.json"
    await imp.decisions_export(str(f))
    await _fresh_production()
    hermes_backfill(fx["db"])
    hermes_gateway_retry(fx["db"], sid, ts=T + 1000)
    live = (await run(fx))["run_id"]
    await imp.decisions_apply(str(f), live, actor=ADMIN[0], apply=True)
    await as_(client, "admin")
    listed = (await client.get("/api/brain/holds")).json()["holds"]
    assert [(h["rule"], "time" in h["evidence"]) for h in listed] == [(identity.SAME_TIME, True)], listed
    r = await client.post("/api/brain/holds/%d/release" % listed[0]["message"])
    assert r.status_code == 200, r.text
    await run(fx)
    await run(fx)
    readable = [m.content for m in await rows_of(sid) if not m.withheld]
    assert secret(mark) in readable and not await holds_of(client)


# ======================= S3: the hidden title of a child first imported after its parent was deleted ===============


TITLE = "Bonus pool qzt"


@pytest.mark.parametrize("kind", ["gateway_branch", "cli_branch", "rotation", "control_parent_kept"])
async def test_s3_hidden_title_of_a_child_first_imported_after_the_parent_was_deleted(client, fx, kind):
    """S3: the admin withholds the first user message (the title is hidden by default); JARVIS branches or rotates the
    conversation and deletes the parent before the next import. The child's title (the parent's, or '<title> #2')
    stays hidden for every reader."""
    mark = "qrv2s3%s8" % kind[:4]
    bj.add_session(fx["db"], "s3p", started=T, title=TITLE)
    first = bj.add_row(fx["db"], "s3p", "user", secret(mark), T + 1)
    bj.add_row(fx["db"], "s3p", "assistant", "Noted.", T + 2)
    bj.add_row(fx["db"], "s3p", "user", "Next topic: the office move.", T + 3)
    bj.add_row(fx["db"], "s3p", "assistant", "Sure.", T + 4)
    await run(fx)
    [w] = await stored_at("s3p", first)
    res = await api_withhold(client, w.id)
    assert res["title_hidden"] is True
    if kind in ("gateway_branch", "control_parent_kept"):
        gw_branch(fx["db"], "s3p", "s3k", T + 100, TITLE + " #2")
    elif kind == "cli_branch":
        cli_branch(fx["db"], "s3p", "s3k", T + 100, TITLE + " #2")
    else:
        rotate_with_title(fx["db"], "s3p", "s3k", T + 100)
    if kind != "control_parent_kept":
        hermes_delete_session(fx["db"], "s3p")
    await run(fx)
    await run(fx)
    shown = {}
    for who in ("viewer", "operator", "admin", "admin+"):
        await as_(client, who)
        shown["sessions:" + who] = "qzt" in (await client.get("/api/brain/sessions", params={"limit": 200})).text
        shown["search:" + who] = "qzt" in (await client.get("/api/brain/search", params={"q": "qzt"})).text
    async with session_scope() as s:
        child = await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == "s3k"))
    show("S3 %s" % kind, {"shown": shown, "child_title": child.title, "flags": child.flags,
                          "nowhere": await bj.nowhere(mark)})
    assert not any(shown.values()), shown
    assert child.title == "" and (child.flags or {}).get("title_withheld") is True
    assert await bj.nowhere(mark) == []


async def test_s3_inverse_an_orphaned_branch_of_a_conversation_whose_title_was_not_hidden_keeps_it(client, fx):
    """S3 inverse: the parent's title was not hidden (another message withheld, hide_title false): its orphaned
    branch keeps its own title."""
    mark = "qrv2s3inv"
    bj.add_session(fx["db"], "s3ip", started=T, title="Garden zqtitle plan")
    bj.add_row(fx["db"], "s3ip", "user", "Plan the garden please.", T + 1)
    second = bj.add_row(fx["db"], "s3ip", "assistant", secret(mark), T + 2)
    await run(fx)
    [w] = await stored_at("s3ip", second)
    res = await api_withhold(client, w.id)
    assert res["title_hidden"] is False
    gw_branch(fx["db"], "s3ip", "s3ik", T + 100, "Garden zqtitle plan #2")
    hermes_delete_session(fx["db"], "s3ip")
    await run(fx)
    async with session_scope() as s:
        child = await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == "s3ik"))
    assert child.title == "Garden zqtitle plan #2" and not (child.flags or {}).get("title_withheld")


# ======================= S4: the prune twin of a certain member copy =======================


@pytest.mark.parametrize("variant", ["root_deleted", "control_root_kept", "own_copy_withheld_in_c"])
async def test_s4_prune_twin_of_a_member_copy_is_a_member(client, fx, variant):
    """S4: a continuation carries a withheld tool output as a certain member (same uid and text); Hermes's proactive
    prune in the continuation rewrites that member in place and INSERTs its pre-prune self as an archived twin. With
    the root deleted the twin is still that very row (Hermes provenance): a member, never a releasable hold, so a
    blind release of every hold brings nothing back (deviation 61/83, extended to a member's own row)."""
    mark = "qrv2s4%s9" % variant[:4]
    bj.add_session(fx["db"], "s4p", started=T, title="Report run")
    bj.add_row(fx["db"], "s4p", "user", "Run the payroll report.", T + 1)
    tool = bj.add_row(fx["db"], "s4p", "tool", "REPORT OUTPUT " + secret(mark), T + 2, tool_call_id="call_7",
                      tool_name="terminal")
    bj.add_row(fx["db"], "s4p", "assistant", "The report ran.", T + 3)
    await run(fx)
    if variant == "own_copy_withheld_in_c":
        copies = rotate_plain(fx["db"], "s4p", "s4c", T + 50)
        await run(fx)
        [w] = await stored_at("s4c", copies[1])
        await api_withhold(client, w.id)
    else:
        [w] = await stored_at("s4p", tool)
        await api_withhold(client, w.id)
        copies = rotate_plain(fx["db"], "s4p", "s4c", T + 50)
        await run(fx)
    if variant != "control_root_kept":
        hermes_delete_session(fx["db"], "s4p")
    twin = hermes_prune(fx["db"], copies[1])
    await run(fx)
    hs = await holds_of(client)
    [t] = await stored_at("s4c", twin)
    async with session_scope() as s:
        st = await s.get(BrainTombstone, t.source_key)
    show("S4 %s" % variant, {"holds": [(h["message"], h["rule"]) for h in hs], "twin": (t.withheld, st and st.via,
                                                                                      st and st.review)})
    await as_(client, "admin")
    for h in hs:  # a blind release of every hold listed
        assert (await client.post("/api/brain/holds/%d/release" % h["message"])).status_code == 200
    await run(fx)
    out = await who_reads(client, mark)
    assert not hs, hs
    assert t.withheld and st is not None and st.review == identity.CERTAIN
    assert not leaked(out), out


# ======================= S5: a released stub held again after a gateway /retry renumbers it =======================


@pytest.mark.parametrize("event", ["gateway_retry", "control_no_retry"])
async def test_s5_released_stub_stays_released_after_a_gateway_retry(client, fx, event):
    """S5: after a prune the admin withholds the archived full output; the stub (same uid, other text) is held and the
    admin releases it. A gateway /retry re-inserts the live rows under new ids (same uids and texts) and the archived
    twin takes the uid key: the renumbered stub, the content released, stays released (no hold listed again), the
    withheld output stays withheld, and no two stored rows name one JARVIS row."""
    mark = "qrv2s5%s3" % event[:4]
    sid = "s5"
    bj.add_session(fx["db"], sid, started=T, title="Report run")
    bj.add_row(fx["db"], sid, "user", "Run the payroll report.", T + 1)
    tool = bj.add_row(fx["db"], sid, "tool", "REPORT OUTPUT " + secret(mark), T + 2, tool_call_id="call_3",
                      tool_name="terminal")
    bj.add_row(fx["db"], sid, "assistant", "The report ran.", T + 3)
    bj.add_row(fx["db"], sid, "user", "Thanks. Summarise the trend please.", T + 4)
    bj.add_row(fx["db"], sid, "assistant", "Trend is flat.", T + 5)
    twin = hermes_prune(fx["db"], tool)
    await run(fx)
    [full] = await stored_at(sid, twin)
    await api_withhold(client, full.id)
    [stub] = await stored_at(sid, tool)
    assert [h["message"] for h in await holds_of(client)] == [stub.id]
    await as_(client, "admin")
    assert (await client.post("/api/brain/holds/%d/release" % stub.id)).status_code == 200
    await run(fx)
    moved = hermes_gateway_retry(fx["db"], sid, ts=T + 100) if event == "gateway_retry" else {}
    await run(fx)
    await run(fx)
    hs = await holds_of(client)
    out = await who_reads(client, mark)
    tool_rows = [(m.source_message_id, m.withheld, m.content[:20]) for m in await rows_of(sid) if m.role == "tool"]
    show("S5 %s" % event, {"holds": [(h["message"], h["rule"]) for h in hs], "tool_rows": tool_rows,
                           "moved": moved})
    assert not hs, hs
    assert not leaked(out), out
    named = [r for r, _, _ in tool_rows]
    assert len(named) == len(set(named)), "two stored rows name one JARVIS row: %s" % tool_rows
    assert len([1 for r, w_, _ in tool_rows if r == str(twin) and w_]) == 1
    if moved:
        renumbered = [w_ for r, w_, _ in tool_rows if r == str(moved[tool])]
        assert renumbered == [False], tool_rows  # the content released stays readable


# ======================= S6-S12: over-reach =======================

# Helpers to run 0.6.1 and 0.6.2 steps against a scratch database in subprocesses (rv2/c_overreach).


async def q(url: str, sql_: str, *args: Any) -> list[dict]:
    conn = await asyncpg.connect(url)
    try:
        return [dict(r) for r in await conn.fetch(sql_, *args)]
    finally:
        await conn.close()


def cli(url: str, *args: str) -> str:
    done = subprocess.run([sys.executable, "-m", "app.cli", *args], cwd=str(BACKEND), env=_env(url),
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def import062(url: str, fx: dict[str, str]) -> str:
    return cli(url, "brain-import", "run", "--db", fx["db"], "--profile", fx["profile"], "--content", fx["content"],
               "--all")


def migrate061(rel: Any, url: str) -> None:
    done = subprocess.run([sys.executable, "-m", "app.cli", "migrate"], cwd=str(rel), env=_env(url),
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr


async def startup(url: str) -> None:
    engine = create_async_engine(url.replace("postgresql://", "postgresql+asyncpg://", 1))
    try:
        async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
            await identity.repair(s, actor="system (start-up check)", cause="startup-check")
            await s.commit()
    finally:
        await engine.dispose()


OPS = r'''
import asyncio, json, sys
from sqlalchemy import select

async def main():
    from app.brain import review
    from app.brain.policy import PersonScope
    from app.db import dispose_engine, session_scope
    from app.models import ArchiveMessage, ArchiveSession
    op, sid, rid = sys.argv[1], sys.argv[2], sys.argv[3]
    async with session_scope() as s:
        if op == "holds":
            out = await review.holds(s, PersonScope(role="admin", sensitive=True))
            print("HOLDS=" + json.dumps(out, default=str))
        else:
            m = (await s.execute(select(ArchiveMessage).join(
                ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id).where(
                ArchiveSession.source_session_id == sid, ArchiveMessage.source_message_id == rid)
                .order_by(ArchiveMessage.id))).scalars().first()
            await review.withhold_message(s, m, actor="admin@test.local")
    await dispose_engine()

asyncio.run(main())
'''


def op062(url: str, op: str, sid: str = "", rid: str = "") -> str:
    done = subprocess.run([sys.executable, "-c", OPS, op, sid, rid], cwd=str(BACKEND), env=_env(url),
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def holds062(url: str) -> list[dict]:
    line = next(x for x in op062(url, "holds").splitlines() if x.startswith("HOLDS="))
    return json.loads(line[len("HOLDS="):])


async def state062(url: str, sid: str) -> dict[str, dict]:
    rows = await q(url, "SELECT m.id, m.source_key, m.source_message_id AS rid, m.role, m.withheld, m.content, "
                        "m.source_uid, t.via, t.review FROM archive_messages m "
                        "JOIN archive_sessions s ON s.id = m.session_id "
                        "LEFT JOIN brain_tombstones t ON t.source_key = m.source_key "
                        "WHERE s.source_session_id = $1 ORDER BY m.id", sid)
    return {r["rid"]: r for r in rows}


async def row_state(sid: str, rid: int) -> dict:
    found = await message_of(sid, rid)
    if not found:
        return {"stored": False}
    m = found[0]
    st = (await stones()).get(m.source_key)
    return {"id": m.id, "withheld": m.withheld, "content": m.content, "uid": (m.source_uid or "")[:8],
            "run": m.import_run_id, "via": st.via if st else None, "review": st.review if st else None}


OK6 = "OK, send it. zqrv2s6"
TURNS6 = [("user", "Hi, zqs6 here, about the invoices."), ("assistant", "Hello. What do you need?"),
          ("assistant", "Shall I email the March invoice to the accountant?"),
          ("user", OK6),                                         # 3: the admin withholds this one
          ("assistant", "Sent the March invoice."), ("user", "And the April one, please."),
          ("assistant", "Shall I email April's to the landlord instead?"),
          ("user", OK6),                                         # 7: an independent answer, its own uid (in the tail)
          ("assistant", "Sent April's invoice to the landlord."), ("user", "Great, thanks.")]


@pytest.mark.parametrize("when", ["on_061", "on_062"])
async def test_s6_a_text_only_carried_record_from_061_is_not_provenance(scratch, rel061, tmp_path, when):
    """S6: 0.6.1's carried_copies paired rows by text alone; with Hermes's in-place compaction it recorded the tail
    copy of an independent later answer (its own uid) as a 'carried-on copy' of the withheld row's stored row. 0.6.2
    neither turns that record into a certain stone at the upgrade (R3) nor follows it at a withhold (group_rows): the
    import compares the uid, and the independent answer stays readable (or at most a listed hold)."""
    migrate061(rel061, scratch)
    fx = bj.mini(tmp_path / "fx")
    db = fx["db"]
    bj.add_session(db, "k0", started=T)
    ids = [bj.add_row(db, "k0", r, c, T + 7 * (n + 1)) for n, (r, c) in enumerate(TURNS6)]
    src0 = {r["id"]: r for r in bj.rows(db, "k0")}
    u3, u7 = src0[ids[3]]["message_uid"], src0[ids[7]]["message_uid"]
    _legacy(rel061, scratch, "run", fx["db"], fx["profile"], fx["content"])
    if when == "on_061":
        _legacy(rel061, scratch, "withhold", OK6)  # the first stored row with this text: row 3
    bj.hermes_compact(db, "k0", head_n=2, tail_n=3, ts=T + DAY)  # tail: rows 7, 8, 9 (copies keep their uids)
    _legacy(rel061, scratch, "run", fx["db"], fx["profile"], fx["content"])
    copy7 = next(r["id"] for r in bj.rows(db, "k0") if r["message_uid"] == u7 and r["active"] == 1)
    recs = await q(scratch, "SELECT r.source_key, r.reason FROM import_records r WHERE r.record_type = 'message' "
                            "AND r.action = 'duplicate' ORDER BY r.id")
    assert any(r["source_key"] == "jarvis:message:k0:%d" % copy7 and r["reason"].startswith("carried")
               for r in recs), "0.6.1 did not pair the independent tail copy by text"
    assert await _migrate(scratch) == ["0008_brain_withhold_identity"]
    await startup(scratch)
    import062(scratch, fx)
    if when == "on_062":
        op062(scratch, "withhold", "k0", str(ids[3]))
    import062(scratch, fx)
    import062(scratch, fx)
    st = await state062(scratch, "k0")
    listed = [(h["message"], h["rule"]) for h in holds062(scratch)]
    show("S6 %s" % when, {"row3": st[str(ids[3])], "copy7": st[str(copy7)], "holds": listed,
                          "uids": (u3[:8], u7[:8])})
    assert st[str(ids[3])]["withheld"] is True
    c7 = st[str(copy7)]
    assert (not c7["withheld"] and c7["content"] == OK6) or c7["review"] == "pending", c7
    stones_ = await q(scratch, "SELECT source_key, review FROM brain_tombstones WHERE message_uid = $1", u7)
    assert all(s_["review"] == "pending" for s_ in stones_), stones_  # never a certain stone on the other uid


REQ7 = "Please transfer 200 dollars to the savings account. zqrv2s7"


def compact_midturn(db: str, sid: str, inflight: int, ts: float) -> int:
    """In-place compaction while `inflight` is unanswered: every active row archived, the summary carrier, then the
    restatement: a copy of the in-flight row with ITS OWN uid (_fresh_compaction_message_copy), header + text."""
    def go(con):
        content, uid = con.execute("SELECT content, message_uid FROM messages WHERE id = ?", (inflight,)).fetchone()
        con.execute("UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ? AND active = 1", (sid,))
        con.execute("INSERT INTO messages (session_id, role, content, message_uid, timestamp) VALUES (?,?,?,?,?)",
                    (sid, "assistant", "[CONTEXT COMPACTION] Earlier: transfers were discussed.", bj.new_uid(), ts))
        return con.execute("INSERT INTO messages (session_id, role, content, message_uid, timestamp) "
                           "VALUES (?,?,?,?,?)", (sid, "user", "%s\n%s" % (HEADER, content), uid, ts + 1)).lastrowid
    return bj._write_db(db, go)


def s7_setup(fx: dict[str, str], sid: str) -> list[int]:
    turns = [("user", REQ7),                                       # 0: the admin withholds this one
             ("assistant", "Transferred 200 dollars to savings."), ("user", "What is the balance now?"),
             ("assistant", "4,300 dollars in checking."),
             ("user", REQ7)]                                       # 4: independent, in flight (unanswered)
    bj.add_session(fx["db"], sid, started=T)
    return [bj.add_row(fx["db"], sid, r, c, T + 60 * (n + 1)) for n, (r, c) in enumerate(turns)]


@pytest.mark.parametrize("order", ["withhold_then_compaction", "compaction_then_withhold"])
async def test_s7_a_restatement_under_the_inflight_rows_own_uid_follows_that_row(client, fx, order):
    """S7: Hermes restates the in-flight request after a mid-turn compaction under that row's OWN uid. When the
    in-flight row is an independent later message whose words equal a withheld one, its restatement follows its own
    row (readable), at import and at withhold time; it is never held as 'restated ... under a new message id'."""
    ids = s7_setup(fx, "k1")
    if order == "withhold_then_compaction":
        await run(fx)
        await withhold(client, (await message_of("k1", ids[0]))[0].id)
        again = compact_midturn(fx["db"], "k1", ids[4], T + DAY)
        await run(fx)
    else:
        again = compact_midturn(fx["db"], "k1", ids[4], T + DAY)
        await run(fx)
        await withhold(client, (await message_of("k1", ids[0]))[0].id)
    await run(fx)
    got = {"row 0": await row_state("k1", ids[0]), "row 4": await row_state("k1", ids[4]),
           "restatement": await row_state("k1", again)}
    show("S7 %s" % order, got)
    assert got["row 0"]["withheld"] is True
    assert got["row 4"]["withheld"] is False
    assert got["restatement"]["withheld"] is False, got


async def test_s7_inverse_the_restatement_of_a_held_inflight_row_is_held_with_it(client, fx):
    """S7 inverse: the in-flight row is itself held ('same text': the withheld message's uid was not known); its
    restatement under its own uid is held with it (never made readable by the owner rule)."""
    db = fx["db"]
    bj.add_session(db, "k1i", started=T)
    first = bj.add_row(db, "k1i", "user", REQ7 + " inv", T + 60, uid=False)
    bj.add_row(db, "k1i", "assistant", "Transferred.", T + 120, uid=False)
    await run(fx)
    await withhold(client, (await message_of("k1i", first))[0].id)
    later = bj.add_row(db, "k1i", "user", REQ7 + " inv", T + DAY)  # its own uid; the withheld one's is unknown
    await run(fx)
    assert (await row_state("k1i", later))["review"] == "pending"
    again = compact_midturn(db, "k1i", later, T + 2 * DAY)
    await run(fx)
    await run(fx)
    got = await row_state("k1i", again)
    show("S7 inverse", got)
    assert got["withheld"] is True and got["review"] == "pending", got


async def test_s8_the_sql_repair_never_spreads_a_holds_own_uid(client, fx):
    """S8: a hold's uid is the held row's own (a restatement held as 'restated ...' under the in-flight row's uid,
    carried as a hold from a test copy). The SQL repair (0008 R4c, every import and start-up) must not hold the
    in-flight row's archived original for it ('upgrade: same message id'), and the release of the hold frees all."""
    ids = s7_setup(fx, "k2")
    await run(fx)
    await withhold(client, (await message_of("k2", ids[0]))[0].id)
    again = compact_midturn(fx["db"], "k2", ids[4], T + DAY)
    await run(fx)
    [w] = await message_of("k2", ids[0])
    [r] = await message_of("k2", again)
    async with session_scope() as s:
        m = await s.get(ArchiveMessage, r.id)
        if not m.withheld:  # the hold a test copy carried (decisions-apply holds it on its own row)
            await review.hold_message(s, m, anchor=w.source_key, actor=ADMIN[0], cause="decisions-apply",
                                      rule=identity.RESTATED)
        await s.commit()
    await run(fx)
    await run(fx)
    before = {"row 4": await row_state("k2", ids[4]), "restatement": await row_state("k2", again)}
    show("S8 before the release", before)
    assert before["restatement"]["review"] == "pending"
    assert before["row 4"]["withheld"] is False, before
    spread = [k for k, s_ in (await stones()).items() if s_.via.startswith("upgrade:")]
    assert not spread, spread
    await as_(client, "admin")
    assert (await client.post("/api/brain/holds/%d/release" % before["restatement"]["id"])).status_code == 200
    await run(fx)
    await run(fx)
    after = {"row 4": await row_state("k2", ids[4]), "restatement": await row_state("k2", again)}
    show("S8 after", after)
    assert after["restatement"]["withheld"] is False and after["row 4"]["withheld"] is False


YES9 = "Sure, go ahead. zqrv2s9"


@pytest.mark.parametrize("history", ["uid_backfill", "uids_from_the_start"])
async def test_s9_an_independent_repeat_typed_after_the_uid_backfill_is_readable(client, fx, history):
    """S9: a JARVIS without message_uid, a withhold, then Hermes's uid backfill (a random uid per legacy row): a message
    the user types weeks later with the same words, under its own fresh uid, is another message. It is readable; a
    repeat from before the backfill (its uid is only a backfilled one) stays a listed hold (inverse)."""
    db = fx["db"]
    if history == "uid_backfill":
        bj.drop_uid_column(db)
    bj.add_session(db, "k3", started=T)
    first = [("assistant", "Shall I cancel the zqs9 gym membership?"), ("user", YES9), ("assistant", "Cancelled it.")]
    ids = [bj.add_row(db, "k3", r, c, T + 30 * (n + 1)) for n, (r, c) in enumerate(first)]
    await run(fx)
    await withhold(client, (await message_of("k3", ids[1]))[0].id)
    # a repeat before the backfill (no uid yet): held for review, nothing can tell it apart
    early = bj.add_row(db, "k3", "user", YES9, T + 2 * DAY, uid=False if history == "uid_backfill" else None)
    await run(fx)
    if history == "uid_backfill":
        bj.backfill_uids(db, "k3")
        await run(fx)
    bj.add_row(db, "k3", "assistant", "Shall I also cancel the zqs9 streaming plan?", T + 20 * DAY)
    later = bj.add_row(db, "k3", "user", YES9, T + 20 * DAY + 5)
    bj.add_row(db, "k3", "assistant", "Cancelled the streaming plan.", T + 20 * DAY + 9)
    await run(fx)
    await run(fx)
    got = {"withheld": await row_state("k3", ids[1]), "early": await row_state("k3", early),
           "later": await row_state("k3", later)}
    show("S9 %s" % history, got)
    assert got["withheld"]["withheld"] is True
    assert got["later"]["withheld"] is False and got["later"]["content"] == YES9, got
    if history == "uid_backfill":
        assert got["early"]["withheld"] is True and got["early"]["review"] == "pending", got


YES10 = "Yes, renew it for another year. zqrv2s10"


@pytest.mark.parametrize("later_in", ["the_same_run", "a_later_run"])
async def test_s10_decisions_apply_twin_rule_takes_only_the_copy_of_that_run(client, tmp_path, later_in):
    """S10: decisions-apply's stored-data twin rule (identity.rewound_twins), in a backup without uids: the copy
    Hermes wrote again in the very run that deleted the rewound original is a member; an independent later answer with
    the same words stored by that run is at most a listed hold, and one stored by a later run stays readable."""
    fx = bj.mini(tmp_path / "fx")
    db = fx["db"]
    bj.add_session(db, "k4", started=T)
    base = [("user", "Hi, zqs10 insurance questions."), ("assistant", "Sure."),
            ("assistant", "Your car insurance expires next week. Renew it?"), ("user", YES10),
            ("assistant", "Renewed the car insurance.")]
    ids = [bj.add_row(db, "k4", r, c, T + 20 * (n + 1), uid=False) for n, (r, c) in enumerate(base)]
    test_copy = bj.copy(fx, tmp_path / "test_copy")
    await run(test_copy)
    await withhold(client, (await message_of("k4", ids[3]))[0].id)
    out = str(tmp_path / "decisions.json")
    await imp.decisions_export(out)
    await _fresh_production()
    await run(fx)                                                # live: run 1
    bj.hermes_compact(db, "k4", head_n=1, tail_n=2, ts=T + DAY)  # rows 3 and 4 rewound and written again

    def add_later() -> int:
        bj.add_row(db, "k4", "assistant", "Your home insurance expires too. Renew it?", T + 9 * DAY, uid=False)
        n = bj.add_row(db, "k4", "user", YES10, T + 9 * DAY + 4, uid=False)
        bj.add_row(db, "k4", "assistant", "Renewed the home insurance.", T + 9 * DAY + 8, uid=False)
        return n
    if later_in == "the_same_run":
        later = add_later()
        last = await run(fx)                                     # live: run 2
    else:
        await run(fx)                                            # live: run 2 (deletes the rewound row 3)
        later = add_later()
        last = await run(fx)                                     # live: run 3
    copy = next(r["id"] for r in bj.rows(db, "k4") if r["content"] == YES10 and r["active"] == 1 and r["id"] != later)
    await imp.decisions_apply(out, last["run_id"], actor=ADMIN[0], apply=True)
    await run(fx)
    await run(fx)
    got = {"copy": await row_state("k4", copy), "later": await row_state("k4", later)}
    show("S10 %s" % later_in, got)
    assert got["copy"]["withheld"] is True and got["copy"]["review"] == identity.CERTAIN
    if later_in == "the_same_run":
        assert got["later"]["withheld"] is True and got["later"]["review"] == "pending", got
    else:
        assert got["later"]["withheld"] is False and got["later"]["content"] == YES10, got


YES11 = "Fine, approve the refund. zqrv2s11"


async def test_s11_a_release_survives_a_database_password_change(client, fx, monkeypatch):
    """S11: a release's digest is keyed by the database password. Under another password (an owner's rotation, or the
    test copy seeded from live) the release still covers the released row (by its uid and length until re-bound
    under the new key), never silently lost."""
    db = fx["db"]
    bj.add_session(db, "k5", started=T)
    turns = [("assistant", "A customer asks for a zqs11 refund of 40 dollars. Approve?"), ("user", YES11),
             ("assistant", "Refund approved.")]
    ids = [bj.add_row(db, "k5", r, c, T + 15 * (n + 1), uid=False) for n, (r, c) in enumerate(turns)]
    await run(fx)
    await withhold(client, (await message_of("k5", ids[1]))[0].id)
    bj.add_row(db, "k5", "assistant", "Another zqs11 refund, 15 dollars. Approve?", T + 3 * DAY, uid=False)
    later = bj.add_row(db, "k5", "user", YES11, T + 3 * DAY + 5, uid=False)
    await run(fx)
    held = await row_state("k5", later)
    assert held["review"] == "pending"
    await as_(client, "admin")
    assert (await client.post("/api/brain/holds/%d/release" % held["id"])).status_code == 200
    await run(fx)
    await run(fx)
    assert (await row_state("k5", later))["content"] == YES11
    other = hmac.new(identity.RELEASE_KEY_LABEL, b"a-rotated-database-password", hashlib.sha256).digest()
    monkeypatch.setattr(identity, "release_key", lambda settings=None: other)
    await run(fx)
    await run(fx)
    after = await row_state("k5", later)
    async with session_scope() as s:
        fps = [e.content_fp for e in (await s.scalars(select(BrainWithholdExemption))).all()]
    show("S11", {"after": after, "fps": fps})
    assert after["withheld"] is False and after["content"] == YES11, after
    assert fps and all(fp.startswith(identity.release_prefix(other)) for fp in fps), fps  # re-bound under the new key


SURE12 = "Sure, book it for Friday. zqrv2s12"


@pytest.mark.parametrize("uids", [False, True])
async def test_s12_the_upgrade_leaves_an_identical_message_the_admin_saw_readable(scratch, rel061, tmp_path, uids):
    """S12: a 0.6.1 database (no uids, or uids), a short message withheld; an identical message in the same
    conversation that the admin saw and left readable (stored before the withhold) is only archived later by a
    compaction (a state change, its text unchanged): the upgrade's R4d does not hold it, and 0.6.2 imports keep it."""
    migrate061(rel061, scratch)
    fx = bj.mini(tmp_path / "fx")
    db = fx["db"]
    bj.add_session(db, "k6", started=T)
    turns = [("assistant", "The zqs12 dentist has a slot on Friday. Book it?"), ("user", SURE12),  # 1: withheld
             ("assistant", "Booked the dentist."),
             ("assistant", "The zqs12 barber also has Friday free. Book it?"), ("user", SURE12),  # 4: the admin saw it
             ("assistant", "Booked the barber."), ("user", "Thanks, that is all for zqs12.")]
    ids = [bj.add_row(db, "k6", r, c, T + 11 * (n + 1), uid=None if uids else False) for n, (r, c) in enumerate(turns)]
    _legacy(rel061, scratch, "run", fx["db"], fx["profile"], fx["content"])
    _legacy(rel061, scratch, "withhold", SURE12)  # the first stored row with it: row 1
    seen = {r["rid"]: r for r in await q(scratch, "SELECT m.source_message_id AS rid, m.withheld, m.content "
                                                  "FROM archive_messages m")}
    assert seen[str(ids[4])]["withheld"] is False
    bj.hermes_compact(db, "k6", head_n=0, tail_n=2, ts=T + 2 * DAY)  # rows 0-4 archived (text unchanged)
    _legacy(rel061, scratch, "run", fx["db"], fx["profile"], fx["content"])
    assert await _migrate(scratch) == ["0008_brain_withhold_identity"]
    await startup(scratch)
    mid = await state062(scratch, "k6")
    import062(scratch, fx)
    import062(scratch, fx)
    end = await state062(scratch, "k6")
    show("S12 uids=%s" % uids, {"after_upgrade": mid[str(ids[4])], "end_row1": end[str(ids[1])],
                                "end_row4": end[str(ids[4])]})
    assert mid[str(ids[4])]["withheld"] is False, mid[str(ids[4])]
    assert end[str(ids[1])]["withheld"] is True
    assert end[str(ids[4])]["withheld"] is False and end[str(ids[4])]["content"] == SURE12


# ======================= S13: a rollback never makes a whole conversation restricted =======================


async def sess_state(sid: str) -> dict:
    async with session_scope() as s:
        sess = await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == sid))
        rows = (await s.scalars(select(ArchiveMessage).where(ArchiveMessage.session_id == sess.id)
                                .order_by(ArchiveMessage.position, ArchiveMessage.id))).all()
        return {"id": sess.id, "sens": sess.sensitivity, "title": sess.title, "flags": sorted(sess.flags or {}),
                "rows": [(m.position, m.role, m.withheld, m.sensitivity, m.content or "") for m in rows]}


async def view(client, sess_id: int, other: str) -> dict:
    out = {}
    for who in ("operator", "admin", "admin+"):
        await as_(client, who)
        out[who + " GET session"] = (await client.get("/api/brain/sessions/%d" % sess_id)).status_code
        out[who + " search"] = other in (await client.get("/api/brain/search", params={"q": other})).text
    async with session_scope() as s:
        hits, _ = await store.AgentMemory(s, "supervisor").history(other)
        out["supervisor history"] = any(other in (h.message.content or "") for h in hits)
    return out


def readable_everywhere(out: dict) -> bool:
    return all(v == 200 for k, v in out.items() if k.endswith("GET session")) and all(
        v is True for k, v in out.items() if not k.endswith("GET session"))


async def test_s13_rolling_back_the_run_that_deleted_a_branch_childs_copy(client, tmp_path):
    """S13 (a): a CLI /branch child's copy of the parent's message was rewound and run 2 deleted it; the admin
    withholds the PARENT's row (its rollback copy is blanked); the admin rolls back run 2. The child conversation comes
    back with that one row withheld, never restricted as a whole: operators, admins and the Supervisor still read its
    other messages, after the rollback and after the next import."""
    mark, other = "zqrv2s13mark", "zqrv2s13other"
    await allow_ai_context()
    fx = bj.mini(tmp_path / "j")
    bj.add_session(fx["db"], "s13_p", started=T, title="Garden plan")
    bj.add_row(fx["db"], "s13_p", "user", "What is the shed lock code?", T + 1)
    bj.add_row(fx["db"], "s13_p", "assistant", "The shed lock code is %s." % mark, T + 2)
    child = bj.cli_branch(fx["db"], "s13_p", "s13_c", T + 10)
    bj.add_row(fx["db"], "s13_c", "user", "Now plan the tomato beds %s." % other, T + 11)
    bj.add_row(fx["db"], "s13_c", "assistant", "Tomato beds planned for %s." % other, T + 12)
    await imp.run(bj.paths(fx), ALL)
    bj.change(fx["db"], "UPDATE messages SET active = 0, compacted = 0 WHERE id = ?", child[1])  # rewound
    second = await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        [parent] = [m for m in (await s.scalars(select(ArchiveMessage))).all() if mark in (m.content or "")]
    await api_withhold(client, parent.id, hide_title=False)
    async with session_scope() as s:
        copies = [(b["deleted_row"].get("sensitivity"), b["deleted_row"].get("content")) for b in (await s.scalars(
            select(ImportRecord.before).where(ImportRecord.before.has_key("deleted_row")))).all()]
    assert copies and all(c == "" for _, c in copies), copies
    assert all(sens != "restricted" for sens, _ in copies), copies  # one message withheld, not restricted data
    await as_(client, "admin")
    rb = await client.post("/api/brain/imports/%d/rollback" % second["run_id"],
                           json={"discard_reviewed": True, "confirm": str(second["run_id"])})
    assert rb.status_code == 200, rb.text
    st2 = await sess_state("s13_c")
    rd2 = await view(client, st2["id"], other)
    await imp.run(bj.paths(fx), ALL)
    st3 = await sess_state("s13_c")
    rd3 = await view(client, st3["id"], other)
    show("S13 branch", {"after_rollback": st2, "readers2": rd2, "after_import": st3, "readers3": rd3,
                        "nowhere": await bj.nowhere(mark)})
    assert st2["sens"] != "restricted" and st3["sens"] != "restricted", (st2, st3)
    assert readable_everywhere(rd2) and readable_everywhere(rd3), (rd2, rd3)
    assert [r for r in st3["rows"] if other in r[4] and not r[2]], st3
    assert await bj.nowhere(mark) == []


@pytest.mark.parametrize("uids,legacy", [(True, False), (False, False), (True, True)])
async def test_s13_rolling_back_the_compaction_run_keeps_the_conversation_readable(client, tmp_path, uids, legacy):
    """S13 (b): Hermes's in-place compaction (head 1, tail 3), the admin withholds the live copy, then rolls back the
    compaction run: the rewound original comes back withheld, and the conversation's other messages stay readable to
    every reader, after the rollback and after the next import. `legacy`: the rollback copy was blanked by an earlier
    0.6.2 build, the 0008 repair or the start-up sweep, which marked it 'restricted': the rollback still puts the row
    back as one withheld message, never restricting the conversation."""
    mark = "zqrv2s13cmp" + ("u" if uids else "n") + ("l" if legacy else "")
    other = "zqrv2s13cmpo" + ("u" if uids else "n") + ("l" if legacy else "")
    await allow_ai_context()
    fx = bj.mini(tmp_path / "j")
    bj.add_session(fx["db"], "s13_r", started=T, title="Shopping list")
    convo = [("user", "List the groceries for %s." % other), ("assistant", "Milk, eggs, %s bread." % other),
             ("user", "Safe code?"), ("assistant", "Safe code is %s." % mark), ("user", "Thanks.")]
    for n, (role, content) in enumerate(convo):
        bj.add_row(fx["db"], "s13_r", role, content, T + n + 1, uid=None if uids else False)
    await imp.run(bj.paths(fx), ALL)
    bj.hermes_compact(fx["db"], "s13_r", head_n=1, tail_n=3, ts=T + 500)
    second = await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        live = [m for m in (await s.scalars(select(ArchiveMessage))).all() if mark in (m.content or "")]
    await api_withhold(client, live[-1].id)
    if legacy:
        async with session_scope() as s:
            n = (await s.execute(text(
                "UPDATE import_records SET before = jsonb_set(before, '{deleted_row,sensitivity}', '\"restricted\"') "
                "WHERE before ? 'deleted_row' AND (before -> 'deleted_row' ->> 'withheld') = 'true'"))).rowcount
            await s.commit()
        assert n >= 1
    await as_(client, "admin")
    rb = await client.post("/api/brain/imports/%d/rollback" % second["run_id"],
                           json={"discard_reviewed": True, "confirm": str(second["run_id"])})
    assert rb.status_code == 200, rb.text
    st2 = await sess_state("s13_r")
    rd2 = await view(client, st2["id"], other)
    await imp.run(bj.paths(fx), ALL)
    st3 = await sess_state("s13_r")
    rd3 = await view(client, st3["id"], other)
    show("S13 compaction uids=%s legacy=%s" % (uids, legacy),
         {"after_rollback": st2, "readers2": rd2, "after_import": st3, "readers3": rd3})
    assert st2["sens"] != "restricted" and st3["sens"] != "restricted", (st2, st3)
    assert readable_everywhere(rd2) and readable_everywhere(rd3), (rd2, rd3)
    assert await bj.nowhere(mark) == []


# ======================= S14: a withhold in a gateway or uid-less branch child reaches the parent's original ========


async def readers14(client, mark: str) -> dict:
    out = {}
    for who in ("viewer", "operator", "admin", "admin+"):
        await as_(client, who)
        out[who + " search"] = mark in (await client.get("/api/brain/search", params={"q": mark})).text
    async with session_scope() as s:
        hits, _ = await store.AgentMemory(s, "supervisor").history(mark)
        out["supervisor history"] = any(mark in (h.message.content or "") for h in hits)
    out["recall"] = mark in json.dumps((await ask("what about the courier %s" % mark)).data, default=str)
    return out


@pytest.mark.parametrize("how", ["gateway_branch", "rotation_no_uids"])
async def test_s14_a_withhold_in_a_child_reaches_the_parents_original(client, tmp_path, how):
    """S14: the admin withholds a message in a gateway /branch child (fresh uid) or a uid-less rotation child. The
    parent's original, the very row JARVIS copied (same role and text, the copy's own time, the copy at or before the
    child's start), is a member ('copied by JARVIS'): unreadable to every reader, not promotable, not listed."""
    mark = "zqrv2s14" + how[:4]
    await allow_ai_context()
    fx = bj.mini(tmp_path / "j")
    db = fx["db"]
    uid = None if how == "gateway_branch" else False
    bj.add_session(db, "s14_p", started=T, title="Courier setup")
    bj.add_row(db, "s14_p", "user", "Set up the courier account please.", T + 1, uid=uid)
    bj.add_row(db, "s14_p", "assistant", "Courier account created, the PIN is %s." % mark, T + 2, uid=uid)
    bj.add_row(db, "s14_p", "user", "Great, thanks.", T + 3, uid=uid)
    if how == "gateway_branch":
        bj.gateway_branch(db, "s14_p", "s14_c", started_at=T + DAY)
    else:
        bj.rotate(db, "s14_p", "s14_c", started_at=T + DAY)
    bj.add_row(db, "s14_c", "user", "Next, the pickup schedule.", T + DAY + 5, uid=uid)
    await imp.run(bj.paths(fx), ALL)
    [copy] = [r for r in await rows_of("s14_c") if mark in (r.content or "")]
    [orig] = [r for r in await rows_of("s14_p") if mark in (r.content or "")]
    await as_(client, "admin")
    pv = (await client.get("/api/brain/messages/%d/withhold-preview" % copy.id)).json()
    assert [r["id"] for r in pv["same_message_rows"]] == [orig.id], pv
    w = await api_withhold(client, copy.id)
    assert w["also_withheld"] == [orig.id], w
    third = await imp.run(bj.paths(fx), ALL)
    [orig2] = [r for r in await rows_of("s14_p") if r.position == orig.position and r.role == "assistant"]
    rd = await readers14(client, mark)
    await as_(client, "operator")
    p = await client.post("/api/brain/messages/%d/promote" % orig2.id, json={"kind": "project", "title": "s14"})
    v = await imp.verify(third["run_id"], bj.paths(fx))
    st = await imp.status()
    show("S14 %s" % how, {"readers": rd, "promote": p.status_code, "verify": v["passed"],
                          "status": st["readable_rows_matching_a_withhold"]})
    assert orig2.withheld and not any(rd.values()), rd
    assert p.status_code == 400
    assert not await holds_of(client)
    assert await bj.nowhere(mark) == []


async def test_s14_inverse_a_donor_timestamp_makes_the_origin_a_hold_never_a_wrong_member(client, tmp_path):
    """S14 inverse: a legacy rotation gives the child's copy of row 5 the time of an identical, independent row 1
    (donor timestamp). The admin withholds the child's copy: which parent row was copied cannot be told, so both
    candidates are listed holds; row 1 never becomes a certain, unlisted member, and the text is readable nowhere."""
    mark = "zqrv2s14donor"
    await allow_ai_context()
    fx = bj.mini(tmp_path / "j")
    db = fx["db"]
    bj.add_session(db, "s14d_p", started=T, title="Legacy")
    text_ = "Confirmed, the gate code stays %s." % mark
    bj.add_row(db, "s14d_p", "user", "Gate code?", T + 1, uid=False)
    r1 = bj.add_row(db, "s14d_p", "assistant", text_, T + 2, uid=False)
    bj.add_row(db, "s14d_p", "user", "Still the same?", T + 3, uid=False)
    r5 = bj.add_row(db, "s14d_p", "assistant", text_, T + 4, uid=False)
    r6 = bj.add_row(db, "s14d_p", "user", "Ok.", T + 5, uid=False)
    legacy_rotation_with_donor(db, "s14d_p", "s14d_c", T + DAY, [r5, r6])
    await imp.run(bj.paths(fx), ALL)
    [copy] = [r for r in await rows_of("s14d_c") if mark in (r.content or "")]
    w = await api_withhold(client, copy.id)
    parent = {r.source_message_id: r for r in await rows_of("s14d_p")}
    async with session_scope() as s:
        st = {k: (await s.get(BrainTombstone, parent[str(k)].source_key)) for k in (r1, r5)}
    show("S14 donor", {"withhold": w, "stones": {k: (v.via, v.review) if v else None for k, v in st.items()}})
    assert all(v is not None and v.review == identity.PENDING for v in st.values()), st
    assert sorted(w["held_for_review"]) == sorted([parent[str(r1)].id, parent[str(r5)].id]), w
    await imp.run(bj.paths(fx), ALL)
    assert len(await holds_of(client)) == 2
    assert await bj.nowhere(mark) == []


# =====================================================================================================================
# Stage 2: the durability findings (S18-S24) and the remaining readers findings (S15-S17)
# =====================================================================================================================

import asyncio  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
from pathlib import Path  # noqa: E402

from app import restore_carry  # noqa: E402
from app.bootstrap import refuse_while_a_restore_is_pending  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog,
    BrainPurgedContent,
    BrainPurgedItem,
    MemoryConflict,
    MemoryItem,
)

from ..unit.test_restore_ps1 import _runner  # noqa: E402
from .test_brain_review1_062 import restore_point, seen_by  # noqa: E402, F401 - restore_point is a fixture

ROOT = Path(__file__).resolve().parents[3]


# --- S15: the title of a /branch child's parent, and a rotation's parent after a rollback ---------------------------


def payroll(mark: str) -> str:
    return "Payroll file %s lists the bonus for Dana as nine thousand, do not share." % mark


def derived_title(mark: str) -> str:
    return payroll(mark)[:48].rstrip() + "\u2026"  # Hermes's title from the first user message


async def title_readers(client, mark: str) -> dict:
    """Where the marker shows as a title: the sessions list, search and each session, per role (rv2 c2)."""
    async with session_scope() as s:
        ids = list((await s.scalars(select(ArchiveSession.id))).all())
    out = {}
    for who in ("operator", "admin", "admin+"):
        await as_(client, who)
        seen = []
        if mark in (await client.get("/api/brain/sessions", params={"limit": 200})).text:
            seen.append("GET /sessions")
        if mark in (await client.get("/api/brain/search", params={"q": "Payroll"})).text:
            seen.append("GET /search?q=Payroll")
        for i in ids:
            if mark in (await client.get("/api/brain/sessions/%d" % i)).text:
                seen.append("GET /sessions/%d" % i)
        if seen:
            out[who] = seen
    return out


async def titles() -> dict:
    async with session_scope() as s:
        return {x.source_session_id: (x.title, (x.flags or {}).get("title_withheld"))
                for x in (await s.scalars(select(ArchiveSession).order_by(ArchiveSession.id))).all()}


@pytest.mark.parametrize("hide", [None, False])
async def test_s15_a_withhold_in_a_cli_branch_child_hides_the_parents_derived_title(client, tmp_path, hide):
    """S15 case 1 (rv2 c_readers_privacy c2a): the admin withholds the first user message of a CLI /branch child. The
    parent's original is a member, and the parent's title, which Hermes derived from that very message, is hidden too
    (operator, admin and admin+ never read it in a list, search or detail), through a re-import. Inverse: with the
    title choice off (hide_title false) no title is hidden, the parent's neither."""
    mark = "zqs15cli" + ("d" if hide is None else "k")
    fx = bj.mini(tmp_path / "j")
    db = fx["db"]
    bj.add_session(db, "s15_p", started=T, title=derived_title(mark))
    bj.add_row(db, "s15_p", "user", payroll(mark), T + 1)
    bj.add_row(db, "s15_p", "assistant", "Understood, kept private.", T + 2)
    bj.cli_branch(db, "s15_p", "s15_c", started_at=T + DAY)
    bj.change(db, "UPDATE sessions SET title = ? WHERE id = 's15_c'", derived_title(mark) + " #2")
    bj.add_row(db, "s15_c", "user", "Branch: draft the memo.", T + DAY + 3)
    await imp.run(bj.paths(fx), ALL)
    [copy] = [m for m in await rows_of("s15_c") if mark in (m.content or "")]
    w = await api_withhold(client, copy.id, **({} if hide is None else {"hide_title": hide}))
    show("S15 cli withhold", {"title_hidden": w["title_hidden"], "also": w["also_withheld"], "titles": await titles()})
    for when in ("right after the withhold", "after a re-import"):
        if when == "after a re-import":
            await imp.run(bj.paths(fx), ALL)
        got = await titles()
        if hide is False:
            assert got["s15_p"][0].startswith("Payroll file") and not got["s15_p"][1], got  # nothing hidden by choice
            continue
        assert got["s15_p"] == ("", True) and got["s15_c"] == ("", True), (when, got)
        assert await title_readers(client, mark) == {}, when
        assert await bj.nowhere(mark) == [], when


async def test_s15_a_rotation_parents_title_stays_hidden_after_a_rollback(client, tmp_path):
    """S15 case 2 (rv2 c2b): a rotation imported in two runs; the admin withholds the first user message in the
    continuation. The parent's rollback copy no longer holds the title Hermes derived from it, and rolling back the
    rotation's run never puts it back on the parent (readers {} after the withhold, the rollback and the next import)."""
    mark = "zqs15rot"
    fx = bj.mini(tmp_path / "j")
    db = fx["db"]
    bj.add_session(db, "s15r_p", started=T, title=derived_title(mark))
    bj.add_row(db, "s15r_p", "user", payroll(mark), T + 1)
    bj.add_row(db, "s15r_p", "assistant", "Understood, kept private.", T + 2)
    await imp.run(bj.paths(fx), ALL)
    bj.rotate(db, "s15r_p", "s15r_c", started_at=T + DAY)
    bj.change(db, "UPDATE sessions SET title = ? WHERE id = 's15r_c'", derived_title(mark))
    bj.change(db, "UPDATE sessions SET title = NULL WHERE id = 's15r_p'")
    bj.add_row(db, "s15r_c", "user", "Continue with the memo.", T + DAY + 3)
    second = await imp.run(bj.paths(fx), ALL)
    [copy] = [m for m in await rows_of("s15r_c") if mark in (m.content or "")]
    await api_withhold(client, copy.id)
    assert await bj.nowhere(mark) == [], "the parent's rollback copy keeps the hidden title"
    await as_(client, "admin")
    rb = await client.post("/api/brain/imports/%d/rollback" % second["run_id"],
                           json={"discard_reviewed": True, "confirm": str(second["run_id"])})
    assert rb.status_code == 200, rb.text
    show("S15 rotation after the rollback", await titles())
    assert await title_readers(client, mark) == {}
    assert await bj.nowhere(mark) == []
    await imp.run(bj.paths(fx), ALL)
    assert await title_readers(client, mark) == {}
    assert await bj.nowhere(mark) == []


# --- S16: purging an incoming version of a changed file keeps no plain hash ------------------------------------------


ALARM = "knowledge/office-alarm.md"
ALARM_V1 = "# Office alarm\nThe alarm company is SecureCo, call them on weekdays.\n"


def alarm_v2(pin: int) -> str:
    return ALARM_V1 + "Disarm PIN: %04d.\n" % pin


def write_profile(fx: dict[str, str], rel: str, text_: str) -> None:
    path = os.path.join(fx["profile"], rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text_)


async def _incoming_version(client, fx: dict[str, str], pin: int) -> tuple[int, int, str]:
    write_profile(fx, ALARM, ALARM_V1)
    await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        existing = (await s.scalar(select(MemoryItem).where(MemoryItem.source_key == "jarvis:file:profile/" + ALARM))).id
    await as_(client, "admin")
    assert (await client.post("/api/brain/items/%d/review" % existing, json={"decision": "approve"})).status_code == 200
    write_profile(fx, ALARM, alarm_v2(pin))
    await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        incoming = await s.scalar(select(MemoryItem).where(
            MemoryItem.flags["replaces_source_key"].astext == "jarvis:file:profile/" + ALARM))
        return existing, incoming.id, incoming.source_sha256


async def test_s16_purging_an_incoming_version_keeps_no_plain_hash_anywhere(client, tmp_path):
    """S16 (rv2 c3): an admin purges the incoming version of a changed file (JARVIS added an alarm PIN). No plain
    SHA-256 of the purged bytes is kept (the existing item's flags held it, returned to every reader): no reader can
    recover the PIN offline, the export holds no hash, and the keyed fingerprint still keeps the version out."""
    fx = bj.mini(tmp_path / "fx")
    existing, inc, sha = await _incoming_version(client, fx, 7342)
    assert hashlib.sha256(alarm_v2(7342).encode()).hexdigest() == sha
    await as_(client, "admin")
    assert (await client.delete("/api/brain/items/%d" % inc)).status_code == 200
    r = await client.delete("/api/brain/items/%d/purge" % inc)
    assert r.status_code == 200, r.text
    assert await bj.nowhere(sha) == [], "a plain hash of the purged version is kept"
    async with session_scope() as s:
        assert await s.scalar(select(func_count()).select_from(BrainPurgedContent)) == 1  # the keyed fingerprint
    for who in ("viewer", "operator", "admin"):
        await as_(client, who)
        got = await client.get("/api/brain/items/%d" % existing)
        body = got.text + (await client.get("/api/brain/items", params={"q": "alarm"})).text
        hashes = set(re.findall(r"[0-9a-f]{64}", body))
        assert not [n for n in range(10000) if hashlib.sha256(alarm_v2(n).encode()).hexdigest() in hashes], who
        assert "rejected_source_sha256" not in body, who
    await as_(client, "admin")
    assert sha not in (await client.get("/api/brain/export")).text
    await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        assert (await s.scalars(select(MemoryItem.id).where(MemoryItem.source_sha256 == sha))).all() == []


async def test_s16_the_next_import_after_an_incoming_purge_keeps_the_existing_item(client, tmp_path):
    """S16 (rv2 c3n, the skeptic's step 3): the purged version's bytes under the EXISTING item's key give no tombstone
    on that key: JARVIS's next change still arrives for review, and decisions-apply --removals-only (a restore) keeps
    the approved item (it was never purged)."""
    fx = bj.mini(tmp_path / "fx")
    existing, inc, sha = await _incoming_version(client, fx, 5521)
    await as_(client, "admin")
    assert (await client.delete("/api/brain/items/%d" % inc)).status_code == 200
    assert (await client.delete("/api/brain/items/%d/purge" % inc)).status_code == 200
    await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        tomb = [(t.source_key, t.reason) for t in (await s.scalars(select(BrainTombstone))).all()]
    assert tomb == [], tomb
    write_profile(fx, ALARM, ALARM_V1 + "Weekend contact: Robin.\n")
    await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        incoming = [(i.status, i.source_sha256 == sha) for i in (await s.scalars(select(MemoryItem).where(
            MemoryItem.flags["replaces_source_key"].astext == "jarvis:file:profile/" + ALARM))).all()]
    assert incoming == [("pending_review", False)], incoming
    out = tmp_path / "removals.json"
    await imp.decisions_export(str(out), removals_only=True)
    await imp.decisions_apply(str(out), None, actor=ADMIN[0], apply=True, removals_only=True)
    async with session_scope() as s:
        item = await s.get(MemoryItem, existing)
        assert item is not None and item.status == "approved" and item.deleted_at is None


def func_count():
    from sqlalchemy import func
    return func.count()


# --- S17: a purged memory entry's key carries a 64-bit digest of its text: said, not denied --------------------------


async def test_s17_a_purged_entrys_digest_is_named_where_it_travels(client, tmp_path):
    """S17 (rv2 c4): a purged MEMORY.md entry's key carries a 64-bit digest of its text (its key since 0.6.0, kept for
    the purge to hold). It is in the decisions files. The claim 'never text or a fingerprint' is gone: each file says
    what is derived from text and how to keep it, decisions-export prints it, and the documents say it. Nothing else
    derived from the entry is in the file (no plain hash of its bytes, no keyed fingerprint in the test-copy file)."""
    fx = bj.mini(tmp_path / "fx")
    secret_ = "Spare key lockbox combination is 2690, zqs17entry."
    write_profile(fx, "memories/MEMORY.md", "- Payroll runs every second Friday.\n- %s\n" % secret_)
    await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        item = await s.scalar(select(MemoryItem).where(MemoryItem.body.contains("zqs17entry")))
        item_id, sha = item.id, item.source_sha256
    await as_(client, "admin")
    assert (await client.delete("/api/brain/items/%d" % item_id)).status_code == 200
    assert (await client.delete("/api/brain/items/%d/purge" % item_id)).status_code == 200
    for removals_only in (False, True):
        out = tmp_path / ("decisions-%s.json" % removals_only)
        await imp.decisions_export(str(out), removals_only=removals_only)
        raw = out.read_text()
        data = json.loads(raw)
        [key] = [r["source_key"] for r in data["removals"]]
        assert "#entry-" in key and "zqs17entry" not in raw and sha not in raw
        if not removals_only:
            assert "k1:" not in raw  # the test copy's file holds no fingerprint of any text
        assert "64-bit digest" in data["handling"] and "like a backup" in data["handling"]
    assert "64-bit digest" in imp.decisions_export.__doc__
    notes = (ROOT / "docs" / "RELEASE_NOTES_0.6.2.md").read_text()
    assert "never text or a fingerprint." not in notes.replace("\n", " ")
    assert "64-bit digest" in notes[notes.index("**Decisions from the test copy"):notes.index("**Restores.**")]


def test_s17_decisions_export_prints_what_in_the_file_comes_from_text(tmp_path, monkeypatch, capsys):
    """S17: brain-import decisions-export says it when the file holds purged memory entries' keys (a digest of each
    entry's text) and how to keep the file; restore.ps1 prints the helper's lines, so the restore says it too."""
    from app import cli

    async def export(path: str, removals_only: bool = False) -> dict:
        Path(path).write_text(json.dumps({"removals": [
            {"source_key": "jarvis:file:profile/memories/MEMORY.md#entry-0123456789abcdef"},
            {"source_key": "jarvis:file:profile/memories/MEMORY.md#entry-fedcba9876543210"},
            {"source_key": "jarvis:message:s:1"}]}))
        return {"removals": 3, "releases": 0, "purged_items": 0, "purge_fingerprints": 0, "exported": 0,
                "skipped_without_source_key": 0, "edits_not_exported": 0}

    async def keep_engine() -> None:  # the test session's engine stays (the CLI runs in a loop of its own)
        return None

    from app import db as app_db

    monkeypatch.setattr(imp, "decisions_export", export)
    monkeypatch.setattr(app_db, "dispose_engine", keep_engine)
    for extra in ([], ["--removals-only"]):
        out = tmp_path / ("f%d.json" % len(extra))
        assert cli.main(["cli", "brain-import", "decisions-export", "--out", str(out), *extra]) == 0
        printed = capsys.readouterr().out
        assert "2 purged memory entries are in this file: each key carries a 64-bit digest of the entry's text; keep " \
            "the file like a backup" in printed, printed


# --- S18: an interrupted restore is finished before anything serves the restored data --------------------------------


async def write_restore_mark(access: bool, removals: bool) -> None:
    """The mark restore.ps1 writes right after pg_restore (its own SQL, as the script sends it through psql)."""
    script = (ROOT / "scripts" / "restore.ps1").read_text()
    assert "INSERT INTO brain_maintenance (name, detail) VALUES ('restore_reapply_pending', jsonb_build_object(" \
        "$parts))" in script
    async with session_scope() as s:
        await s.execute(text(
            "CREATE TABLE IF NOT EXISTS brain_maintenance (name TEXT PRIMARY KEY, done_at TIMESTAMPTZ NOT NULL DEFAULT "
            "now(), detail JSONB NOT NULL DEFAULT '{}'::jsonb)"))
        await s.execute(text(
            "INSERT INTO brain_maintenance (name, detail) VALUES ('restore_reapply_pending', jsonb_build_object("
            "'access', CAST(:a AS boolean), 'removals', CAST(:r AS boolean), 'access_file', "
            "'access-before-restore-1.json', 'removals_file', 'removals-before-restore-1.json', 'backup', 'old', "
            "'at', now())) ON CONFLICT (name) DO UPDATE SET detail = EXCLUDED.detail"), {"a": access, "r": removals})


async def refused() -> bool:
    try:
        await refuse_while_a_restore_is_pending()
    except restore_carry.RestorePending:
        return True
    return False


async def test_s18_an_interrupted_restore_serves_nothing_until_its_files_are_applied(client, fx, restore_point):
    """S18 (rv2 durability #0, database side): restore.ps1 was killed after pg_restore, before the re-apply. The
    restored database says so: the api and the worker refuse to start (whoever starts them: start.ps1 has its own
    refusal, a plain 'docker compose up' or another folder's start does not). Applying the saved files clears the mark
    part by part (the access file alone is not enough), and only then does anything start; the decisions made after
    the backup are all there (the withheld message is read by nobody)."""
    mark = "zqs18int"
    bj.add_session(fx["db"], "s18_s", started=T)
    ids = [bj.add_row(fx["db"], "s18_s", role, content, T + n + 1) for n, (role, content) in enumerate(
        [("user", "Hello."), ("assistant", secret(mark)), ("user", "Thanks.")])]
    await run(fx)
    restore_point.take()  # the nightly backup, before the withhold
    [row] = await message_of("s18_s", ids[1])
    await api_withhold(client, row.id)
    files = await restore_point.export()
    await restore_point.restore(None)  # pg_restore, then the console window is closed
    await write_restore_mark(access=True, removals=True)
    assert await refused(), "the api and the worker would serve the restored data without the withhold"
    await restore_carry.apply(files[1], apply=True)
    assert await refused(), "the removals are not applied yet"
    await imp.decisions_apply(files[0], None, actor=None, apply=True, removals_only=True)
    assert not await refused()
    assert await seen_by(client, mark) == {}
    assert await bj.nowhere(mark) == []


async def test_s18_a_removal_not_applied_keeps_the_mark(client, fx, restore_point):
    """S18: decisions-apply that leaves a removal 'not applied' keeps the mark (restore.ps1 then leaves the services
    stopped too): nothing serves the restored data while a decision is missing."""
    bj.add_session(fx["db"], "s18b_s", started=T)
    bj.add_row(fx["db"], "s18b_s", "user", "Hello.", T + 1)
    await run(fx)
    restore_point.take()
    files = await restore_point.export()
    data = json.loads(Path(files[0]).read_text())
    data["removals"].append({"source_key": 7, "reason": "withheld by an admin"})  # not a removal: not applied
    Path(files[0]).write_text(json.dumps(data))
    await restore_point.restore(None)
    await write_restore_mark(access=False, removals=True)
    out = await imp.decisions_apply(files[0], None, actor=None, apply=True, removals_only=True)
    assert out["removals_not_applied"], out
    assert await refused()


# The stand-in docker of tests/unit/test_restore_ps1.py, for several restore.ps1 runs in one PowerShell (the marker
# file and backups\ stay between them, as on the owner's PC). A run is "<scenario>:<switch>,<switch>".
STUB_SEQ = r'''
param([string]$Dir, [string]$Plan)
$global:Calls = New-Object System.Collections.Generic.List[string]
$global:Scenario = ""
$global:Interrupted = $false
function global:Start-Sleep { param([int]$Seconds) $global:Calls.Add("sleep $Seconds") }
function global:docker {
  $line = ($args | ForEach-Object { "$_" }) -join " "
  $global:Calls.Add($line); $global:LASTEXITCODE = 0
  $sc = $global:Scenario
  if ($line -match "version = '0006_brain'") {
    if ($sc -eq "psqlfail") { "Error response from daemon: container 1f0c is restarting"; $global:LASTEXITCODE = 1; return }
    "1"; return
  }
  if ($line -match "FROM pg_locks") { if ($sc -eq "busy") { "1" } else { "0" }; return }
  if ($line -match "name = ''restore_reapply_pending''") {
    if ($sc -eq "dbmark") { '{"access": true, "removals": true, "removals_file": "removals-before-restore-1.json"}' }
    return
  }
  if ($line -match "FILTER \(WHERE reason") { "3 withheld messages and copies, 1 purged items with a source key"; return }
  if ($line -match '^compose run -d --no-deps --name (\S+) api ') { return }
  if ($line -match 'restore-carry export --out') { "access and audit exported: users 4, audit entries 10"; return }
  if ($line -match 'decisions-export --removals-only') {
    "removals exported (withheld messages and purged items, keys and JARVIS ids only): 3; releases: 1; purges by item id: 2; purge fingerprints: 1"; return
  }
  if ($line -match '^cp cc-restore-helper-\S+:(\S+) (.+)$') { Set-Content -LiteralPath $Matches[2] -Value '{}'; return }
  if ($line -match 'restore-carry apply --in') {
    if ($sc -eq "interrupt" -and -not $global:Interrupted) { $global:Interrupted = $true; throw "the console window was closed" }
    "audit entries made after the backup, put back: 2"; "access changes made after the backup, applied again: 0"; return
  }
  if ($line -match 'decisions-apply --removals-only') {
    if ($sc -eq "fails") { "failed: OperationalError"; $global:LASTEXITCODE = 1; return }
    "removals applied: already done 2, purged 1, withheld 2"; return
  }
}
foreach ($run in ($Plan -split ';')) {
  $parts = $run -split ':'
  $global:Scenario = $parts[0]
  $flags = @{}
  foreach ($s in ($parts[1] -split ',')) { if ($s) { $flags[$s] = $true } }
  $global:Calls.Add("=== run $run")
  $global:LASTEXITCODE = 0
  try {
    if ($flags.ContainsKey("FinishPending")) { & (Join-Path $Dir "scripts/restore.ps1") @flags }
    else { & (Join-Path $Dir "scripts/restore.ps1") -BackupFolder (Join-Path $Dir "backups/old") -Yes @flags }
    $code = "$LASTEXITCODE"
  } catch { $code = "killed" }
  $global:Calls.Add("exit=$code")
  $global:Calls.Add("marker=" + (Test-Path -LiteralPath (Join-Path $Dir "backups/restore-pending.json")))
}
"--- calls"; $global:Calls
'''


def restore_runs(tmp_path: Path, plan: str) -> tuple[str, list[list[str]]]:
    """restore.ps1 (the worktree's) run as `plan` says, one PowerShell; each run's calls, ending with its exit code
    and whether the marker file is there."""
    work = tmp_path / "w"
    proj = work / "InfiniteAI-CommandCenter"
    (proj / "scripts").mkdir(parents=True)
    (proj / "backups" / "old").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "restore.ps1", proj / "scripts" / "restore.ps1")
    (proj / "scripts" / "backup.ps1").write_text("$global:Calls.Add('backup.ps1')\r\nexit 0\r\n")
    (proj / "scripts" / "start.ps1").write_text("param([switch]$NoBuild)\r\n$global:Calls.Add('start.ps1')\r\nexit 0\r\n")
    (proj / "backups" / "old" / "command_center.dump").write_bytes(b"")
    stub = work / "stub.ps1"
    stub.write_text(STUB_SEQ)
    done = subprocess.run([*_runner(work), "-NoProfile", "-File", str(stub), str(proj), plan],
                          capture_output=True, text=True, timeout=300)
    out = done.stdout + done.stderr
    assert "--- calls" in done.stdout, out
    runs: list[list[str]] = []
    for line in done.stdout.split("--- calls", 1)[1].strip().splitlines():
        if line.startswith("=== run "):
            runs.append([])
        elif runs:
            runs[-1].append(line)
    return out, runs


def first(calls: list[str], pattern: str) -> int:
    return next((n for n, c in enumerate(calls) if re.search(pattern, c)), -1)


def test_s18_restore_ps1_interrupted_then_run_again_applies_the_saved_files_first(tmp_path):
    """S18 (rv2 durability #0, the script): run 1 is killed after pg_restore, before the re-apply: the marker file
    stays, naming the files it saved. Run 2 with the same folder applies THOSE files first (before its safety backup
    and before any export from the restored data), then restores and re-applies as usual, and ends with no marker."""
    out, (one, two) = restore_runs(tmp_path, "interrupt:;works:")
    assert first(one, r"pg_restore") >= 0 and one[-2:] == ["exit=killed", "marker=True"], one
    assert first(one, r"^start\.ps1$") == -1
    saved = {m.group(1): m.group(2) for c in one if (m := re.match(
        r"^cp cc-restore-helper-\S+:/tmp/(removals|access)-before-restore-\S+ (.+)$", c))}
    assert set(saved) == {"removals", "access"}, one
    pend_a = first(two, r"^cp %s cc-restore-helper-\S+:/tmp/access\.json$" % re.escape(saved["access"]))
    pend_r = first(two, r"^cp %s cc-restore-helper-\S+:/tmp/removals\.json$" % re.escape(saved["removals"]))
    apply_r = first(two, r"decisions-apply --removals-only --in /tmp/removals\.json")
    assert 0 <= pend_a < first(two, "restore-carry apply") < pend_r < apply_r < first(two, r"^backup\.ps1$"), two
    assert apply_r < first(two, r"decisions-export"), "the restored data was exported before the saved files applied"
    assert two[-2:] == ["exit=0", "marker=False"] and first(two, r"^start\.ps1$") >= 0, two
    assert "An earlier restore" in out and "did not finish" in out


def test_s18_a_failed_reapply_then_finish_pending(tmp_path):
    """S18 (rv2 c_durability h-failapply-retry): run 1's re-apply fails: services not started, the marker stays.
    -FinishPending applies the saved files (never exports, never restores again), then starts the services."""
    out, (one, two) = restore_runs(tmp_path, "fails:;works:FinishPending")
    assert one[-2:] == ["exit=1", "marker=True"] and first(one, r"^start\.ps1$") == -1, one
    assert "restore.ps1 -FinishPending" in out
    assert first(two, r"decisions-apply --removals-only") >= 0 and first(two, r"decisions-export") == -1, two
    assert first(two, r"pg_restore") == -1 and first(two, r"^backup\.ps1$") == -1, two
    assert first(two, r"decisions-apply") < first(two, r"^start\.ps1$") and two[-2:] == ["exit=0", "marker=False"], two


def test_s18_the_databases_mark_without_the_marker_file_stops_the_restore(tmp_path):
    """S18: the database says its last restore did not finish, but this folder has no marker file (a restore run from
    another folder): nothing is backed up, exported, stopped or restored; the restore stops and says why."""
    out, (one,) = restore_runs(tmp_path, "dbmark:")
    assert one[-2:] == ["exit=1", "marker=False"], one
    assert first(one, r"^backup\.ps1$") == first(one, r"compose stop") == first(one, r"pg_restore") == -1, one
    assert "did not finish" in out and "-FinishPending" in out


def test_s18_start_ps1_starts_nothing_while_a_restore_is_pending(tmp_path):
    """S18: start.ps1 (the worktree's) refuses while backups\\restore-pending.json is there: no docker call at all,
    exit 1, and it names the files not applied yet and the command that finishes the restore."""
    proj = tmp_path / "InfiniteAI-CommandCenter"
    (proj / "scripts").mkdir(parents=True)
    (proj / "backups").mkdir()
    shutil.copy(ROOT / "scripts" / "start.ps1", proj / "scripts" / "start.ps1")
    (proj / ".env").write_text("DASHBOARD_PORT=8090\r\n")
    (proj / "backups" / "restore-pending.json").write_text(json.dumps(
        {"access": "C:\\x\\backups\\access-before-restore-1.json",
         "removals": "C:\\x\\backups\\removals-before-restore-1.json", "backup": "old", "at": "now"}))
    stub = tmp_path / "stub.ps1"
    stub.write_text("param([string]$Dir)\n$global:Calls = New-Object System.Collections.Generic.List[string]\n"
                    "function global:docker { $global:Calls.Add(($args -join ' ')); $global:LASTEXITCODE = 0 }\n"
                    "& (Join-Path $Dir 'scripts/start.ps1') -NoBuild\n$code = $LASTEXITCODE\n"
                    "'--- calls'; $global:Calls; \"exit=$code\"\n")
    done = subprocess.run([*_runner(tmp_path), "-NoProfile", "-File", str(stub), str(proj)], capture_output=True,
                          text=True, timeout=300)
    out = done.stdout + done.stderr
    tail = done.stdout.split("--- calls", 1)[1].strip().splitlines()
    assert tail == ["exit=1"], out  # no docker call: nothing started, nothing built
    assert "removals-before-restore-1.json" in out and "restore.ps1 -FinishPending" in out


# --- S19: the application is stopped before the decisions are saved --------------------------------------------------


def test_s19_restore_ps1_stops_the_application_before_it_saves_the_decisions(tmp_path):
    """S19 (rv2 durability #1): a withhold, purge, release or keep committed between the export and the stop was in
    neither file. Now: safety backup < no import running < stop < decisions-export < restore-carry export (whose audit
    log then holds the export's own entry) < pg_restore. An import, rollback or rescan holding the import lock stops the
    restore before anything is stopped."""
    out, (one, busy) = restore_runs(tmp_path, "works:;busy:")
    order = [first(one, p) for p in (r"^backup\.ps1$", r"FROM pg_locks", r"^compose stop api worker dashboard n8n",
                                     r"decisions-export --removals-only", r"restore-carry export",
                                     r"pg_restore -U postgres")]
    assert -1 not in order and order == sorted(order), (order, one)
    assert one[-2:] == ["exit=0", "marker=False"], one
    assert first(busy, r"compose stop") == first(busy, r"pg_restore") == -1 and busy[-2:][0] == "exit=1", busy
    assert "nothing was changed" in out


async def test_s19_the_restores_own_export_entry_is_kept(client, fx, restore_point):
    """S19 (the audit part): restore.ps1 exports the removals before the audit log (the order is read from the
    script), so the carried audit log holds the export's own brain.decisions_exported entry: after the restore it is
    there with its own id, and no id names another entry than before."""
    script = (ROOT / "scripts" / "restore.ps1").read_text()
    removals_first = script.index("Invoke-Helper brain-import decisions-export") < script.index(
        "Invoke-Helper restore-carry export")
    bj.add_session(fx["db"], "s19_s", started=T)
    bj.add_row(fx["db"], "s19_s", "user", "Hello.", T + 1)
    await run(fx)
    restore_point.take()
    removals = str(restore_point_dir(fx) / "removals.json")
    access = str(restore_point_dir(fx) / "access.json")
    if removals_first:
        await imp.decisions_export(removals, removals_only=True)
        await restore_carry.export(access)
    else:
        await restore_carry.export(access)
        await imp.decisions_export(removals, removals_only=True)
    async with session_scope() as s:
        before = {a.id: (a.actor, a.action) for a in (await s.scalars(select(AuditLog))).all()}
    await restore_point.restore((removals, access))
    async with session_scope() as s:
        after = {a.id: (a.actor, a.action) for a in (await s.scalars(select(AuditLog))).all()}
    lost = {i: v for i, v in before.items() if after.get(i) != v}
    assert lost == {}, lost
    assert any(v[1] == "brain.decisions_exported" for v in after.values())


def restore_point_dir(fx: dict[str, str]) -> Path:
    out = Path(fx["db"]).parent / "carry"
    out.mkdir(exist_ok=True)
    return out


# --- S20: the probe that decides whether the Brain's removals are saved fails closed ---------------------------------


def test_s20_a_failed_migrations_probe_stops_the_restore_before_any_change(tmp_path):
    """S20 (rv2 durability #2): the one psql probe for 0006_brain fails (the container is restarting). It used to
    count as 'no JARVIS Brain': nothing exported, nothing refused, 'Restore complete'. Now the restore stops with exit
    1 before anything is stopped or restored."""
    out, (one,) = restore_runs(tmp_path, "psqlfail:")
    assert one[-2:] == ["exit=1", "marker=False"], one
    assert first(one, r"compose stop") == first(one, r"pg_restore") == first(one, r"decisions-export") == -1, one
    assert "could not read the database's migrations" in out and "Restore complete" not in out


# --- S21: identical text the admin saw and left readable stays readable ------------------------------------------------


async def _left_readable_case(client, fx, sid: str, mark: str, restore_point=None) -> tuple[list[int], int]:
    """No uids: row 2 is the secret; JARVIS repeats it verbatim (row 4), imported (run 2) BEFORE the withhold; the
    admin sees the repeat in the preview and leaves it readable (rv2 c_durability cd3)."""
    db_ = fx["db"]
    bj.add_session(db_, sid, started=T)
    ids = [bj.add_row(db_, sid, role, content, T + n + 1, uid=False) for n, (role, content) in enumerate(
        [("user", "Hello."), ("user", secret(mark)), ("assistant", "Noted.")])]
    await run(fx)
    if restore_point is not None:
        restore_point.take()  # the nightly backup: before the repeat is imported
    ids.append(bj.add_row(db_, sid, "user", secret(mark), T + 90, uid=False))
    await run(fx)
    [target] = await message_of(sid, ids[1])
    [repeat] = await message_of(sid, ids[3])
    await as_(client, "admin")
    pv = await client.get("/api/brain/messages/%d/withhold-preview" % target.id)
    assert [x["id"] for x in pv.json()["identical_text_rows"]] == [repeat.id]
    await api_withhold(client, target.id, include_identical=False)
    return ids, repeat.id


async def _repeat_state(sid: str, src_row: int) -> list[tuple]:
    return [(m.withheld, ((m.flags or {}).get("withheld_by_admin") or {}).get("review")) for m in
            await message_of(sid, src_row)]


async def test_s21_identical_text_left_readable_survives_a_restore_and_the_next_import(client, fx, restore_point):
    """S21 (rv2 cd3a): after restore.ps1's steps (the backup predates the repeat's import) and the next import, the
    repeat the admin left readable is readable, not a 'same text' hold."""
    ids, _ = await _left_readable_case(client, fx, "s21a_s", "zqs21a", restore_point)
    files = await restore_point.export()
    await restore_point.restore(files)
    await run(fx)
    assert await _repeat_state("s21a_s", ids[3]) == [(False, None)]
    assert await holds_of(client) == []


async def test_s21_identical_text_left_readable_survives_a_rollback_and_reimport(client, fx):
    """S21 (rv2 cd3b): a rollback of the run that imported the repeat, then a re-import: still readable, no hold."""
    ids, _ = await _left_readable_case(client, fx, "s21b_s", "zqs21b")
    await imp.rollback(2, actor="admin@test.local", confirm=True)
    await run(fx)
    assert await _repeat_state("s21b_s", ids[3]) == [(False, None)]
    assert await holds_of(client) == []


async def test_s21_inverse_a_new_repeat_the_admin_never_saw_is_still_held(client, fx):
    """S21 inverse: the decision covers only the rows the admin saw. A repeat JARVIS stores after the withhold is
    another JARVIS row: held for review ('same text'), listed, never readable unasked."""
    ids, _ = await _left_readable_case(client, fx, "s21c_s", "zqs21c")
    bj.add_row(fx["db"], "s21c_s", "assistant", "Noted again.", T + 150, uid=False)
    later = bj.add_row(fx["db"], "s21c_s", "user", secret("zqs21c"), T + 200, uid=False)
    await run(fx)
    assert await _repeat_state("s21c_s", ids[3]) == [(False, None)]
    assert await _repeat_state("s21c_s", later) == [(True, identity.PENDING)]
    assert [h["rule"] for h in await holds_of(client)] == ["same text"]


# --- S22: a purge by 0.6.1 code on the 0.6.2 schema is refused -----------------------------------------------------------


OLD_PURGE = r'''
"""Runs inside a release's backend: an admin deletes and purges, through THAT release's own API, every item whose text
holds the marker (rv2 c_durability drivers/cd_old_purge.py). Synthetic data only."""
import asyncio
import json
import os
import sys

import httpx


async def main() -> None:
    from app.db import dispose_engine
    from app.main import create_app
    from app.security import CSRF_HEADER, CSRF_VALUE

    app = create_app(run_bootstrap=False)
    out = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cc.test") as c:
        r = await c.post("/api/auth/login", json={"email": os.environ["ADMIN_EMAIL"],
                                                  "password": os.environ["ADMIN_PASSWORD"]})
        assert r.status_code == 200, r.text
        c.headers[CSRF_HEADER] = CSRF_VALUE
        for mark in sys.argv[1:]:
            for item in (await c.get("/api/brain/items", params={"q": mark})).json()["items"]:
                d = await c.delete("/api/brain/items/%d" % item["id"])
                try:
                    p = (await c.delete("/api/brain/items/%d/purge" % item["id"])).status_code
                except Exception as exc:  # the release's app raises what the database refused
                    p = exc.__class__.__name__
                out.append([mark, item["id"], d.status_code, p])
    await dispose_engine()
    print("OLD-PURGE " + json.dumps(out))

asyncio.run(main())
'''


def old_purge(rel: Path, *marks: str) -> list:
    script = rel.parent / "old_purge_061.py"
    script.write_text(OLD_PURGE)
    done = subprocess.run([sys.executable, str(script), *marks], cwd=str(rel),
                          env={**os.environ, "PYTHONPATH": str(rel)}, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads([x for x in done.stdout.splitlines() if x.startswith("OLD-PURGE ")][0][10:])


async def items_with(mark: str) -> list[tuple]:
    async with session_scope() as s:
        return [tuple(r) for r in (await s.execute(text(
            "SELECT id, status, source_key, origin, deleted_at IS NOT NULL FROM memory_items "
            "WHERE body ILIKE :m OR title ILIKE :m ORDER BY id"), {"m": "%" + mark + "%"})).all()]


async def test_s22_a_purge_by_061_code_is_refused_and_a_062_purge_then_holds(client, fx, rel061, restore_point):
    """S22 (rv2 durability #4, cd4a/cd4b): back on 0.6.1 with the data kept, an admin purges a JARVIS file's item and
    a manual note. 0.6.1 writes a tombstone only (no ledger row, no keyed fingerprint), so a rename of the file or a
    restore of an older backup brought the text back. The database now refuses that purge (all or nothing): the items
    stay deleted, nothing half-recorded. Purged again with 0.6.2 after going forward, both purges hold through a rename
    and the next import, and through restore.ps1's steps with a backup from before the purges."""
    mf, mm = "zqs22file", "zqs22manual"
    write_profile(fx, "knowledge/courier.md", "# Courier\nThe courier code is %s and the depot is north.\n" % mf)
    await run(fx)
    await as_(client, "admin")
    r = await client.post("/api/brain/items", json={"kind": "business", "title": "Private note " + mm,
                                                    "body": "Keep this private: " + secret(mm)})
    assert r.status_code == 201, r.text
    for q in (mf, mm):
        item = (await client.get("/api/brain/items", params={"q": q})).json()["items"][0]["id"]
        assert (await client.post("/api/brain/items/%d/review" % item, json={"decision": "approve"})).status_code == 200
    restore_point.take()  # the nightly backup: before the purges
    got = old_purge(rel061, mf, mm)
    show("S22 0.6.1 purges", got)
    assert len(got) == 2 and all(d == 200 and p != 200 for _, _, d, p in got), got
    async with session_scope() as s:
        assert await s.scalar(select(func_count()).select_from(BrainTombstone)) == 0
        assert await s.scalar(select(func_count()).select_from(BrainPurgedItem)) == 0
    assert [x[4] for x in await items_with(mf) + await items_with(mm)] == [True, True]  # deleted, not purged
    await as_(client, "admin")  # forward to 0.6.2: the admin purges again
    for _, item, _, _ in got:
        assert (await client.delete("/api/brain/items/%d/purge" % item)).status_code == 200
    files = await restore_point.export()
    await restore_point.restore(files)
    assert await items_with(mm) == [] and await items_with(mf) == []
    k = os.path.join(fx["profile"], "knowledge")
    os.rename(os.path.join(k, "courier.md"), os.path.join(k, "courier-old.md"))
    await run(fx)
    assert await items_with(mf) == []
    assert await seen_by(client, mf) == {} and await seen_by(client, mm) == {}


async def test_s22_a_connection_that_keeps_no_purge_records_cannot_purge(client):
    """S22, the database rule itself: removing a deleted item for good needs a connection that says it keeps purge
    records (0.6.2's engine sets cc.brain_purge_ledger); a plain connection is refused; a live item and a TRUNCATE
    (the tests' clean-up) are not affected."""
    await as_(client, "admin")
    ids = []
    for n in range(2):
        r = await client.post("/api/brain/items", json={"kind": "business", "title": "Note %d" % n, "body": "Body."})
        assert r.status_code == 201, r.text
        ids.append(r.json()["item"]["id"])
    assert (await client.delete("/api/brain/items/%d" % ids[0])).status_code == 200
    url = os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url)
    try:
        with pytest.raises(asyncpg.RaiseError, match="purge with Command Center 0.6.2 or later"):
            await conn.execute("DELETE FROM memory_items WHERE id = $1", ids[0])
        assert await conn.execute("DELETE FROM memory_items WHERE id = $1", ids[1]) == "DELETE 1"  # not deleted first
    finally:
        await conn.close()
    conn = await asyncpg.connect(url, server_settings={"cc.brain_purge_ledger": "on"})
    try:
        assert await conn.execute("DELETE FROM memory_items WHERE id = $1", ids[0]) == "DELETE 1"
    finally:
        await conn.close()


# --- S23: who released and who kept, after a restore ------------------------------------------------------------------


async def _held_repeat(client, fx, sid: str, mark: str) -> int:
    """No uids: the admin withholds row 2; JARVIS repeats it; the next import holds the repeat ('same text')."""
    db_ = fx["db"]
    bj.add_session(db_, sid, started=T + 1000)
    ids = [bj.add_row(db_, sid, role, content, T + 1000 + n + 1, uid=False) for n, (role, content) in enumerate(
        [("user", "Hi."), ("user", secret(mark)), ("assistant", "Ok.")])]
    await run(fx)
    [row] = await message_of(sid, ids[1])
    await api_withhold(client, row.id)
    rep = bj.add_row(db_, sid, "user", secret(mark), T + 1090, uid=False)
    await run(fx)
    [held] = await message_of(sid, rep)
    assert held.withheld
    return held.id


async def test_s23_a_release_made_after_the_backup_keeps_who_decided_it(client, fx, restore_point):
    """S23 (rv2 cd5a): a release made after the nightly backup is applied again by the restore as 'system (restore)',
    but its exemption keeps the admin and the time of the release (and the row's flag says who released it)."""
    held = await _held_repeat(client, fx, "s23a_s", "zqs23a")
    restore_point.take()
    await asyncio.sleep(1.1)
    await as_(client, "admin")
    assert (await client.post("/api/brain/holds/%d/release" % held)).status_code == 200
    async with session_scope() as s:
        before = [(e.source_row_id, e.decided_by, e.decided_at) for e in
                  (await s.scalars(select(BrainWithholdExemption))).all()]
    files = await restore_point.export()
    await asyncio.sleep(1.1)
    await restore_point.restore(files)
    async with session_scope() as s:
        after = [(e.source_row_id, e.decided_by, e.decided_at) for e in
                 (await s.scalars(select(BrainWithholdExemption))).all()]
        row = await s.get(ArchiveMessage, held)
    assert after == before and before[0][1] == "admin@test.local", (before, after)
    assert (row.flags or {}).get("withheld_by_admin", {}).get("released_by") == "admin@test.local"


async def test_s23_a_keep_made_after_the_backup_says_kept_after_the_restore(client, fx, restore_point):
    """S23 (rv2 cd5b): the backup holds the hold (pending); the admin keeps it afterwards. After the restore the stone
    says kept (not listed), and the row's flag says kept too, by that admin, not 'held for review' (which the
    dashboard showed for a row nobody can keep or release any more)."""
    held = await _held_repeat(client, fx, "s23b_s", "zqs23b")
    restore_point.take()
    await as_(client, "admin")
    assert (await client.post("/api/brain/holds/%d/keep" % held)).status_code == 200
    async with session_scope() as s:
        kept_at = (await s.get(ArchiveMessage, held)).flags["withheld_by_admin"]["kept_at"]
    files = await restore_point.export()
    await restore_point.restore(files)
    async with session_scope() as s:
        row = await s.get(ArchiveMessage, held)
        stone = await s.get(BrainTombstone, row.source_key)
    flag = row.flags["withheld_by_admin"]
    assert row.withheld and stone.review == identity.KEPT
    assert (flag.get("review"), flag.get("kept_by"), flag.get("kept_at")) == ("kept", "admin@test.local", kept_at)
    assert await holds_of(client) == []


# --- S24: the 409 names what can hold the lock --------------------------------------------------------------------------


async def test_s24_a_second_admins_change_while_one_is_in_progress_is_told_so(client, fx):
    """S24 (rv2 cd6): while one admin's withhold holds the import lock (its transaction), a second admin's withhold is
    refused with a 409. The message no longer claims that an import, a rollback or a rescan is running (none is): it
    names an admin's change too. Nothing changes; the second withhold succeeds once the first is done."""
    bj.add_session(fx["db"], "s24_s", started=T)
    ids = [bj.add_row(fx["db"], "s24_s", role, content, T + n + 1) for n, (role, content) in enumerate(
        [("user", "First " + secret("zqs24a")), ("assistant", "Ok.")])]
    await run(fx)
    [row] = await message_of("s24_s", ids[0])
    async with session_scope() as other:  # the first admin's request, in its transaction
        assert await imp.try_xact_lock(other, imp.GLOBAL_LOCK)
        await as_(client, "admin")
        r = await client.post("/api/brain/messages/%d/withhold" % row.id)
        running = await other.scalar(text("SELECT count(*) FROM import_runs WHERE status = 'running'"))
    assert r.status_code == 409 and running == 0, r.text
    assert "an admin's withhold, release, keep, purge or redaction" in r.json()["detail"], r.text
    assert not (await message_of("s24_s", ids[0]))[0].withheld
    await api_withhold(client, row.id)
