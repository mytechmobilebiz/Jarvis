"""JARVIS Brain 0.6.2, "decide once, replay exactly" (scratchpad conv/RECOMMENDATION.md, plan steps 1-3): the schema
(0008: decision backups, sticky family links, exact releases, the guard's exact identity, the refusal of older code's
rows), the source's lineage markers, exact identity everywhere a 'same JARVIS row' rule runs (review 3, rv3-ov-0), the
replay of a decision on the very backup it was taken on, and jarvis-import -Mode Import -Decisions (the decisions
applied before the import, so no withheld text is ever readable on live: C1). Synthetic data only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import asyncpg
import pytest
from sqlalchemy import select, text

from app.brain import identity
from app.brain import importer as imp
from app.brain import source as src
from app.db import session_scope
from app.models import (
    ArchiveMessage,
    ArchiveSession,
    BrainDecisionBackup,
    BrainSessionLink,
    BrainTombstone,
    ImportRecord,
)

from .. import brain_jarvis as bj
from ..conftest import ADMIN
from .test_brain_api import as_, client  # noqa: F401 - client is a fixture
from .test_brain_withhold_identity import (
    DAY,
    T,
    conversation,
    message_of,
    run,
    secret,
    stones,
    withhold,
)

pytestmark = pytest.mark.usefixtures("clean_db")

ALL = imp.Selection(all=True)
NEW_TABLES = ("brain_decision_backups", "brain_session_links", "brain_frozen_sessions")


@pytest.fixture
def fx(tmp_path) -> dict[str, str]:
    return bj.mini(tmp_path / "fx")


def _raw_url() -> str:
    from app.config import get_settings

    return get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


async def _fresh_live() -> None:
    """The live installation's database: neither decision, nothing imported (TRUNCATE fires no row trigger, so the
    append-only family links go too, as on a database that never imported)."""
    async with session_scope() as s:
        for table in ("import_records", "archive_messages", "archive_sessions", "brain_tombstones",
                      "brain_withhold_exemptions", "brain_purged_content", "brain_maintenance", "memory_items",
                      "import_runs", *NEW_TABLES):
            await s.execute(text("TRUNCATE %s RESTART IDENTITY CASCADE" % table))


async def _row_states() -> dict[str, tuple]:
    """Every stored row's state, by key: (withheld, content, the review of the stone its key has, or None)."""
    async with session_scope() as s:
        reviews = {t.source_key: t.review for t in (await s.scalars(select(BrainTombstone).where(
            BrainTombstone.reason == identity.WITHHELD_BY_ADMIN))).all()}
        return {m.source_key: (m.withheld, m.content, reviews.get(m.source_key))
                for m in (await s.scalars(select(ArchiveMessage).order_by(ArchiveMessage.id))).all()}


def _reuse_row_id(db: str, sid: str, row_id: int) -> None:
    """Hermes's state.db came back from a snapshot (hermes backup import, the snapshot `hermes update` takes):
    sqlite_sequence with it, so the next message reuses this row id."""
    bj.change(db, "DELETE FROM messages WHERE id = ?", row_id)
    bj.change(db, "UPDATE sqlite_sequence SET seq = ? WHERE name = 'messages'", row_id - 1)


# --- Exact identity: the database guard and the repair (plan 1.5, 1.6; review 3 rv3-ov-0 B2, B3) ----------------------


async def test_the_guard_blanks_only_the_exact_jarvis_row():
    """The guard's 'same JARVIS row' branch: the same session and row id, the same role and tool_call_id, the same time
    when both have one, never two different uids. Another message at a reused row id (another time, its own uid) is
    stored readable."""
    t1 = datetime(2026, 11, 18, 10, 0, 1, tzinfo=UTC)
    later = datetime(2026, 11, 19, 9, 0, tzinfo=UTC)
    conn = await asyncpg.connect(_raw_url(), server_settings={"cc.brain_purge_ledger": "on"})
    try:
        sess = await conn.fetchval("INSERT INTO archive_sessions (source_key, source_session_id) VALUES "
                                   "('jarvis:session:G1', 'G1') RETURNING id")
        await conn.execute(
            "INSERT INTO brain_tombstones (source_key, reason, source_session_id, source_row_id, role, tool_call_id, "
            "message_uid, source_at, bound) VALUES ('jarvis:message:uid:old3', 'withheld by an admin', 'G1', '3', "
            "'user', '', 'old3', $1, true)", t1)

        async def insert(key: str, role: str, at: datetime | None, uid: str, call: str = "") -> bool:
            return await conn.fetchval(
                "INSERT INTO archive_messages (session_id, source_key, source_message_id, source_fp, position, role, "
                "content, tool_call_id, at, source_uid) VALUES ($1, $2, '3', 'fp', 0, $3, 'words', $4, "
                "$5, $6) RETURNING withheld", sess, key, role, call, at, uid)

        assert await insert("jarvis:message:uid:new3", "user", later, "new3") is False  # reuse
        assert await insert("jarvis:message:uid:other", "user", t1, "other") is False  # another uid, same time
        assert await insert("jarvis:message:G1:3:a", "user", later, "") is False  # another time
        assert await insert("jarvis:message:G1:3:b", "tool", t1, "", "call_1") is False  # another role
        assert await insert("jarvis:message:G1:3:c", "user", t1, "") is True  # no uid on the row: the same row
        assert await insert("jarvis:message:G1:3:d", "user", None, "old3") is True  # no time on the row
    finally:
        await conn.close()


async def test_the_repair_withholds_only_the_exact_jarvis_row():
    """R4b (plan 1.6): a readable row at the stone's JARVIS row id with another uid is another message, left readable
    with no stone; the exact row is withheld (a hold's own row comes back held, never a member)."""
    async with session_scope() as s:
        sess = (await s.execute(text("INSERT INTO archive_sessions (source_key, source_session_id) VALUES "
                                     "('jarvis:session:R1', 'R1') RETURNING id"))).scalar()
        await s.execute(text(
            "INSERT INTO archive_messages (session_id, source_key, source_message_id, source_fp, position, role, "
            "content, source_uid, at) VALUES (:s, 'jarvis:message:uid:new5', '5', 'fp', 0, 'user', 'other words', "
            "'new5', now()), (:s, 'jarvis:message:uid:mine7', '7', 'fp', 1, 'user', 'held words', 'mine7', now())"),
            {"s": sess})
        await s.execute(text(
            "INSERT INTO brain_tombstones (source_key, reason, source_session_id, source_row_id, role, message_uid, "
            "bound, review) VALUES ('jarvis:message:uid:old5', 'withheld by an admin', 'R1', '5', 'user', 'old5', "
            "true, ''), ('jarvis:message:R1:7', 'withheld by an admin', 'R1', '7', 'user', 'mine7', true, 'pending')"))
        out = await identity.repair_rows(s, cause="test")
        rows = {k: (w, c) for k, w, c in (await s.execute(text(
            "SELECT source_key, withheld, content FROM archive_messages ORDER BY id"))).all()}
        assert rows["jarvis:message:uid:new5"] == (False, "other words")
        assert rows["jarvis:message:uid:mine7"] == (True, "")
        assert out["withheld_by_jarvis_row"] == 1
        made = (await s.execute(text("SELECT review FROM brain_tombstones WHERE source_key = "
                                     "'jarvis:message:uid:mine7'"))).scalar()
        assert made == "pending"
        assert (await s.execute(text("SELECT count(*) FROM brain_tombstones WHERE source_key = "
                                     "'jarvis:message:uid:new5'"))).scalar() == 0
        await s.rollback()


async def test_rows_of_code_older_than_0_6_2_are_refused():
    """rv3-dur-2: a connection that does not say it is 0.6.2 or later (an older importer's run still going, a 0.6.1
    rollback) cannot write an archived message; 0.6.2's own connections can, but never for a running older run."""
    conn = await asyncpg.connect(_raw_url())
    try:
        sess = await conn.fetchval("INSERT INTO archive_sessions (source_key, source_session_id) VALUES "
                                   "('jarvis:session:O1', 'O1') RETURNING id")
        with pytest.raises(asyncpg.RaiseError, match="code older than 0.6.2"):
            await conn.execute("INSERT INTO archive_messages (session_id, source_key, source_message_id, position, "
                               "role, content) VALUES ($1, 'jarvis:message:O1:1', '1', 0, 'user', 'x')", sess)
        await conn.execute("SET cc.brain_purge_ledger = 'on'")
        await conn.execute("INSERT INTO archive_messages (session_id, source_key, source_message_id, position, role, "
                           "content) VALUES ($1, 'jarvis:message:O1:1', '1', 0, 'user', 'x')", sess)
        await conn.execute("RESET cc.brain_purge_ledger")
        with pytest.raises(asyncpg.RaiseError, match="code older than 0.6.2"):
            await conn.execute("UPDATE archive_messages SET content = 'y' WHERE session_id = $1", sess)
        # A message of a run by an older importer that is still running is refused on any connection (its run row
        # predates 0008, so it was never refused itself); a finished older run's row (a 0.6.2 rollback puts it back)
        # and a running 0.6.2 run's rows are stored.
        await conn.execute("SET cc.brain_purge_ledger = 'on'")
        await conn.execute("SET session_replication_role = replica")
        runs = {name: await conn.fetchval("INSERT INTO import_runs (status, importer_version) VALUES ($1, $2) "
                                          "RETURNING id", status, version)
                for name, status, version in (("old_running", "running", "1.0.4"), ("old_done", "completed", "1.0.1"))}
        await conn.execute("SET session_replication_role = origin")

        async def store(row: str, run_id: int) -> None:
            await conn.execute("INSERT INTO archive_messages (session_id, source_key, source_message_id, position, "
                               "role, content, import_run_id) VALUES ($1, $2, $3, 0, 'user', 'x', $4)",
                               sess, "jarvis:message:O1:%s" % row, row, run_id)

        with pytest.raises(asyncpg.RaiseError, match="importer older than 1.1.0"):
            await store("2", runs["old_running"])
        await store("3", runs["old_done"])
        with pytest.raises(asyncpg.RaiseError, match="importer older than 1.1.0"):
            await conn.execute("UPDATE archive_messages SET import_run_id = $1 WHERE source_message_id = '3'",
                               runs["old_running"])
        # the older run stops (one run at a time); a running 0.6.2 run's rows are stored
        await conn.execute("UPDATE import_runs SET status = 'failed' WHERE id = $1", runs["old_running"])
        new_run = await conn.fetchval("INSERT INTO import_runs (status, importer_version) VALUES ('running', '1.1.0') "
                                      "RETURNING id")
        await store("4", new_run)
    finally:
        await conn.close()


async def test_family_links_are_append_only_and_lineage_is_never_forgotten():
    """Plan 1.3, 1.4: a recorded family link is never changed or deleted; a conversation's lineage and parent are
    never overwritten with '' (another value replaces them)."""
    async with session_scope() as s:
        await s.execute(text("INSERT INTO brain_session_links (a, b, how) VALUES ('P', 'C', 'parent')"))
        await s.commit()
    for statement in ("DELETE FROM brain_session_links", "UPDATE brain_session_links SET how = 'shared_uid'"):
        with pytest.raises(Exception, match="append-only"):
            async with session_scope() as s:
                await s.execute(text(statement))
    async with session_scope() as s:
        assert (await s.execute(text("SELECT count(*) FROM brain_session_links"))).scalar() == 1
        sid = (await s.execute(text(
            "INSERT INTO archive_sessions (source_key, source_session_id, lineage, parent_source_session_id) VALUES "
            "('jarvis:session:C', 'C', 'continuation', 'P') RETURNING id"))).scalar()
        await s.execute(text("UPDATE archive_sessions SET lineage = '', parent_source_session_id = '' WHERE id = :i"),
                        {"i": sid})
        assert (await s.execute(text("SELECT lineage, parent_source_session_id FROM archive_sessions WHERE id = :i"),
                                {"i": sid})).one() == ("continuation", "P")
        await s.execute(text("UPDATE archive_sessions SET lineage = 'branch' WHERE id = :i"), {"i": sid})
        assert (await s.execute(text("SELECT lineage FROM archive_sessions WHERE id = :i"), {"i": sid})).scalar() == (
            "branch")


async def test_pending_has_the_new_counters():
    """Plan 1.8: status and precheck read these."""
    async with session_scope() as s:
        pending = await identity.pending(s)
    for name in ("unsettled_decisions", "unreviewed_settle_holds", "quarantined_rows", "frozen_sessions",
                 "legacy_lock", "last_tripwire"):
        assert name in pending, name
    assert pending["legacy_lock"] is False and pending["last_tripwire"] is None


# --- The source's lineage (plan 2.1) ---------------------------------------------------------------------------------


async def test_an_orphaned_rotation_keeps_its_lineage_from_the_marker_and_later_backups_never_erase_it(fx):
    """rv3-res-4: JARVIS deleted a rotation's parent (delete_session sets parent_session_id NULL); the child's
    _compressed_from marker still names it, so the archive stores it as a continuation of that parent and records the
    family link. A later backup without the marker (an older Hermes) never erases either."""
    db = fx["db"]
    bj.add_session(db, "rot_child", started=T + DAY, model_config={"_compressed_from": "rot_parent_gone"})
    bj.add_row(db, "rot_child", "user", "Carried on.", T + 1)
    bj.add_session(db, "reset_child", started=T + DAY, parent="rot_child", model_config={"_reset_from": "rot_child"})
    bj.add_row(db, "reset_child", "user", "Fresh start.", T + DAY + 1)
    await run(fx)
    async with session_scope() as s:
        lineage = {r.source_session_id: (r.lineage, r.parent_source_session_id)
                   for r in (await s.scalars(select(ArchiveSession))).all()}
        links = {(r.a, r.b, r.how) for r in (await s.scalars(select(BrainSessionLink))).all()}
    assert lineage["rot_child"] == ("continuation", "rot_parent_gone")
    assert lineage["reset_child"] == ("reset", "rot_child")  # recorded, never a family link
    assert links == {("rot_parent_gone", "rot_child", "compressed_from")}
    bj.change(db, "UPDATE sessions SET model_config = '{}' WHERE id = 'rot_child'")
    later = await run(fx)
    async with session_scope() as s:
        row = (await s.execute(select(ArchiveSession.lineage, ArchiveSession.parent_source_session_id).where(
            ArchiveSession.source_session_id == "rot_child"))).one()
        record = await s.scalar(select(ImportRecord).where(ImportRecord.run_id == later["run_id"],
                                                           ImportRecord.source_key == "jarvis:session:rot_child"))
    assert tuple(row) == ("continuation", "rot_parent_gone")
    assert (record.action, record.reason) == ("duplicate", "already imported")  # nothing to change


# --- Exact identity in the importer and at withhold time (plan 3.1; rv3-ov-0 B1) ------------------------------------


def _ov0_backup(fx: dict[str, str], mark: str) -> list[int]:
    return conversation(fx["db"], "ov0", T, [("user", "Hello there."), ("assistant", "Hi."),
                                             ("user", "My card PIN is %s, keep it safe." % mark)])


async def test_a_new_message_at_a_reused_row_id_is_never_withheld_by_the_old_withhold(client, fx):
    """rv3-ov-0 (withhold, then restore): the admin withholds row 3; Hermes's state.db comes back from a snapshot and
    the user writes a new, unrelated message, which gets row id 3 again with a fresh uid and a later time. The trigger
    and exact identity reject the match: never withheld for good. 0.6.2 plan 5.3 (rv3-ov-0): in the withheld family it
    is held and listed ('a reused row number'), and a release brings it back; the withheld message stays withheld."""
    mark, other = "zqov0pin62", "What is the weather tomorrow? zqov0other62"
    ids = _ov0_backup(fx, mark)
    await run(fx)
    [w] = await message_of("ov0", ids[2])
    await withhold(client, w.id)
    _reuse_row_id(fx["db"], "ov0", ids[2])
    assert bj.add_row(fx["db"], "ov0", "user", other, T + DAY) == ids[2]
    await run(fx)
    rows = await message_of("ov0", ids[2])
    assert {(m.withheld, m.content) for m in rows} == {(True, "")}
    new = next(m for m in rows if m.source_key != w.source_key)
    assert (await stones())[new.source_key].review == identity.PENDING
    await as_(client, "admin")
    [hold] = (await client.get("/api/brain/holds")).json()["holds"]
    assert hold["message"] == new.id and identity.REUSED_ROW in hold["relations"], hold
    assert (await client.post("/api/brain/holds/%d/release" % new.id)).status_code == 200
    await run(fx)
    assert {(m.withheld, m.content) for m in await message_of("ov0", ids[2])} == {(True, ""), (False, other)}
    assert await bj.nowhere(mark) == []


async def test_a_withhold_never_takes_a_new_message_at_a_reused_row_id(client, fx):
    """rv3-ov-0 (restore, then withhold): both messages are stored under row id 3 of the conversation; withholding
    the old one takes only it (the withhold's 'same JARVIS row' step uses exact identity)."""
    mark, other = "zqov0pin62b", "What is the weather tomorrow? zqov0other62b"
    ids = _ov0_backup(fx, mark)
    await run(fx)
    _reuse_row_id(fx["db"], "ov0", ids[2])
    bj.add_row(fx["db"], "ov0", "user", other, T + DAY)
    await run(fx)
    rows = await message_of("ov0", ids[2])
    old = next(m for m in rows if mark in m.content)
    await as_(client, "admin")
    preview = (await client.get("/api/brain/messages/%d/withhold-preview" % old.id)).json()
    assert preview["same_message_rows"] == [] and preview["held_rows"] == [], preview
    await withhold(client, old.id)
    after = {m.id: (m.withheld, m.content) for m in await message_of("ov0", ids[2])}
    assert after[old.id] == (True, "")
    assert (False, other) in after.values()


# --- Decide once, replay exactly (plan 3.2, 3.3; C1) -------------------------------------------------------------------


def _family_backup(fx: dict[str, str], mark: str) -> None:
    conversation(fx["db"], "fam_parent", T, [("user", "What did the vendor offer?"), ("assistant", secret(mark)),
                                             ("user", "Thanks.")])
    bj.gateway_branch(fx["db"], "fam_parent", "fam_branch", started_at=T + DAY)  # a copy with a fresh uid
    bj.add_row(fx["db"], "fam_branch", "user", "A new question in the branch.", T + DAY + 1)
    conversation(fx["db"], "elsewhere", T + 2 * DAY, [("user", "Unrelated."), ("assistant", "Indeed.")])


async def test_a_withhold_is_decided_on_the_backup_the_family_was_imported_from(client, fx):
    """Plan 1.1, 1.3: every stone of the withhold names the JARVIS backup it was decided on (its SHA-256), recorded with
    its label, importer version, newest message time and first run."""
    mark = "zqdecided62"
    _family_backup(fx, mark)
    first = await run(fx)
    sha = src.sha256_file(fx["db"])
    rows = await message_of("fam_parent", 2)
    await withhold(client, rows[0].id)
    group = [st for st in (await stones()).values()]
    assert group and {st.decided_on for st in group} == {sha}
    async with session_scope() as s:
        b = await s.get(BrainDecisionBackup, sha)
        pending = await identity.pending(s)
    assert (b.first_run_id, b.importer_version, b.settled_run_id) == (first["run_id"], imp.IMPORTER_VERSION, None)
    assert b.newest_at is not None
    assert pending["unsettled_decisions"] == 1  # Settle (plan 4) finishes it


async def test_the_same_backup_with_its_decisions_file_replays_them_word_for_word(client, fx, tmp_path):
    """C1: the test copy imports backup B and an admin withholds a message (its gateway-branch copy joins as a member);
    the decisions are exported (version 5). Live imports the very same file with -Decisions: the stones and releases
    apply first, then the import replays them: every row has the test copy's state, every withheld row was stored
    without its text from its very first write (no window), and the rules never ran on the replayed sessions."""
    mark = "zqreplay62"
    _family_backup(fx, mark)
    await run(fx)
    [w] = await message_of("fam_parent", 2)
    await withhold(client, w.id)
    await bj.settle(fx)  # 0.6.2 plan 4.7: decisions-export refuses until the withhold is finished
    test_copy = await _row_states()
    assert sum(1 for state in test_copy.values() if state[0]) >= 2  # the message and its branch copy
    out = tmp_path / "decisions.json"
    await imp.decisions_export(str(out))
    data = json.loads(out.read_text())
    assert data["version"] == 5 and [b["sha256"] for b in data["backups"]] == [src.sha256_file(fx["db"])]
    assert {"a": "fam_parent", "b": "fam_branch", "how": "parent"} in data["links"]
    assert mark not in out.read_text()
    await _fresh_live()
    live = await imp.run(bj.paths(fx), ALL, actor=ADMIN[0], decisions=str(out))
    assert live["decisions"]["warnings"] == []
    assert live["decisions"]["before_import"]["withholds"] == sum(
        1 for r in data["removals"] if r["reason"] == identity.WITHHELD_BY_ADMIN)
    assert await _row_states() == test_copy
    assert live["report"]["sessions_replayed"] == 2  # the message's conversation and its branch: never the rules
    async with session_scope() as s:
        records = {r.source_key: (r.action, r.reason) for r in (await s.scalars(select(ImportRecord).where(
            ImportRecord.run_id == live["run_id"], ImportRecord.record_type == "message"))).all()}
        guarded = (await s.execute(text("SELECT count(*) FROM archive_messages WHERE flags -> 'withheld_by_admin' "
                                        "? 'guard'"))).scalar()
    for key, (withheld, _, _) in test_copy.items():
        if withheld:
            assert records[key][0] == "withheld", (key, records[key])  # stored without its text from the start
    assert guarded == 0  # the importer itself, not the database guard, kept the text out
    assert await bj.nowhere(mark) == []


async def test_another_backup_with_the_decisions_file_warns_and_is_a_later_backup(client, fx, tmp_path):
    """Plan 3.3: a backup other than the one the decisions were taken on is imported all the same, with a warning;
    the withholds apply (fail-safe), nothing is replayed."""
    mark = "zqlater62"
    _family_backup(fx, mark)
    await run(fx)
    [w] = await message_of("fam_parent", 2)
    await withhold(client, w.id)
    await bj.settle(fx)  # 0.6.2 plan 4.7: decisions-export refuses until the withhold is finished
    out = tmp_path / "decisions.json"
    await imp.decisions_export(str(out))
    await _fresh_live()
    bj.add_row(fx["db"], "fam_parent", "user", "One more turn on live.", T + 3 * DAY)  # another file now
    live = await imp.run(bj.paths(fx), ALL, actor=ADMIN[0], decisions=str(out))
    assert any("not the one the decisions were taken on" in w for w in live["decisions"]["warnings"])
    assert "sessions_replayed" not in live["report"]
    [stored] = await message_of("fam_parent", 2)
    assert (stored.withheld, stored.content) == (True, "")
    assert await bj.nowhere(mark) == []


async def test_decisions_with_an_import_need_a_version_5_file_and_an_admin(fx, tmp_path):
    """Refused before any change: an older file (it applies after an import, -Mode ApplyDecisions), and an actor who
    is not an active admin."""
    _family_backup(fx, "zqrefuse62")
    old = tmp_path / "v4.json"
    old.write_text(json.dumps({"format": "jarvis-brain-decisions", "version": 4, "removals": []}))
    with pytest.raises(imp.Refused, match="version 5"):
        await imp.run(bj.paths(fx), ALL, actor=ADMIN[0], decisions=str(old))
    new = tmp_path / "v5.json"
    new.write_text(json.dumps({"format": "jarvis-brain-decisions", "version": 5, "removals": []}))
    with pytest.raises(imp.Refused, match="active admin"):
        await imp.run(bj.paths(fx), ALL, actor="nobody@example.com", decisions=str(new))
    async with session_scope() as s:
        assert (await s.execute(text("SELECT count(*) FROM import_runs"))).scalar() == 0


async def test_a_keep_names_its_keeper_on_the_stone(client, fx, tmp_path):
    """Plan 1.1 (rv3-dur-6): a keep writes who kept the hold and when on the stone, which the decisions file carries
    (a release's exact binding: test_brain_holds.test_release_restores_text_at_next_import_and_audits)."""
    from .test_brain_holds import held_stub

    _, stub = await held_stub(client, fx, "zqkeeper62")
    await as_(client, "admin")
    assert (await client.post("/api/brain/holds/%d/keep" % stub.id)).status_code == 200
    stone = (await stones())[stub.source_key]
    assert stone.review == identity.KEPT and stone.kept_by == ADMIN[0] and stone.kept_at is not None
    await bj.settle(fx)  # 0.6.2 plan 4.7: decisions-export refuses until the withhold is finished
    out = tmp_path / "decisions.json"
    await imp.decisions_export(str(out))
    [removal] = [r for r in json.loads(out.read_text())["removals"] if r["source_key"] == stub.source_key]
    assert (removal["review"], removal["kept_by"]) == (identity.KEPT, ADMIN[0]) and removal["kept_at"]
