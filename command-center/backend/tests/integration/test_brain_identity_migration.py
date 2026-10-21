"""0008_brain_withhold_identity.sql on the databases it meets (spec 3): a fresh one, a 0.6.1 database holding every
confirmed leak state (the design's Appendix B fixture, as SQL inserts), a lineage deeper than any limit, and the
restore of a 0.6.1 or a 0.6.2 dump (scripts/restore.ps1's pg_restore flags). The real migration runner applies it.

Every row is synthetic. Row numbers are the fixture's: 158-248 as the design's tables name them.

The restore tests need docker (pg_dump and pg_restore in the cc-test-pg container) and are skipped without it;
REQUIRE_DOCKER_TESTS=1 makes a missing docker or container a failure instead."""

from __future__ import annotations

import hashlib
import json
import os

import asyncpg
import pytest
from sqlalchemy import text

from app import db
from app.brain import identity
from app.db import session_scope

from .test_brain_schema import _container, _migrate, _pg, _version_row, needs_docker

SCRATCH = os.environ["TEST_DATABASE_URL"].rsplit("/", 1)[1] + "_identity"
DUMP = "/tmp/%s_061.dump" % SCRATCH
DUMP_062 = "/tmp/%s_062.dump" % SCRATCH
WITHHELD_BY_REPAIR = [163, 177, 179, 181, 191, 200, 211, 240, 242, 245, 247, 248]
READABLE = [176, 178, 182, 209, 231, 241, 243]
HELD = [177, 179, 240, 242]

# The 0.6.1 database after the confirmed re-import leaks (the judge's fixture). Run 1 before the withholds, run 2
# after them. A: 158 withheld, its carried-on copy back as 163. A-tail: 170 withheld then deleted as rewound, its copy
# back as 177; 176 the same words stored before the withhold; 178 in another conversation; 179 in a continuation.
# B: 180 withheld, its repeat back as 181; 182 similar words. C1: 190 withheld under S1:140, back as 191 under its
# uid. C3: 200 overwritten with the withheld 165's text. C2: the key uid:uY moved to 164 (210), 165 back as 211; 209
# the unrelated row 164. 230 withheld "yes" with a known uid; 231 an independent repeat with its own uid.
FX_061 = """
INSERT INTO import_runs (id, status, started_at, importer_version) VALUES
  (1, 'completed', '2026-10-01 10:00+00', '1.0.1'), (2, 'completed', '2026-10-05 10:00+00', '1.0.1');
SELECT setval('import_runs_id_seq', 2);
INSERT INTO archive_sessions (id, source_key, source_session_id, lineage, parent_source_session_id, import_run_id)
VALUES
  (1, 'jarvis:session:S1', 'S1', '', '', 1), (2, 'jarvis:session:S2', 'S2', '', '', 1),
  (3, 'jarvis:session:S3', 'S3', '', '', 1), (4, 'jarvis:session:S4', 'S4', '', '', 1),
  (5, 'jarvis:session:S5', 'S5', 'continuation', 'S1', 2);
SELECT setval('archive_sessions_id_seq', 5);
INSERT INTO archive_messages (id, session_id, source_key, source_message_id, source_fp, position, role, content, state,
                              withheld, flags, import_run_id) VALUES
  (158, 1, 'jarvis:message:S1:101', '101', 'fpA_orig', 0, 'user', '', 'compacted', true,
   '{"withheld_by_admin": {"at": "2026-10-03T10:00:00+00:00", "note": "x"}}', 1),
  (163, 1, 'jarvis:message:S1:103', '103', 'fpA_copy_compacted', 2, 'user', 'LEAK-A Quokkazebrine', 'compacted', false,
   '{}', 2);
INSERT INTO import_records (run_id, source_key, record_type, action, reason, target_table, target_id) VALUES
  (1, 'jarvis:message:S1:101', 'message', 'imported', '', 'archive_messages', 158),
  (1, 'jarvis:message:S1:103', 'message', 'duplicate', 'carried-on copy of a compacted message', 'archive_messages',
   158),
  (2, 'jarvis:message:S1:103', 'message', 'imported', '', 'archive_messages', 163);
INSERT INTO import_records (run_id, source_key, record_type, action, reason, target_table, target_id, before) VALUES
  (1, 'jarvis:message:uid:uD', 'message', 'imported', '', 'archive_messages', 170, '{}'),
  (2, 'jarvis:message:uid:uD', 'message', 'updated', 'rewound since an earlier import', 'archive_messages', 170,
   '{"deleted_row": {"id": 170, "session_id": 1, "source_key": "jarvis:message:uid:uD", "source_message_id": "110",
     "source_fp": "fpTAIL", "position": 9, "role": "user", "content": "", "content_chars": 25, "withheld": true,
     "state": "live", "flags": {"withheld_by_admin": {"note": "x"}}, "import_run_id": 1}}');
INSERT INTO archive_messages (id, session_id, source_key, source_message_id, source_fp, position, role, content, state,
                              withheld, import_run_id) VALUES
  (176, 1, 'jarvis:message:S1:105', '105', 'fpTAIL', 4, 'user', 'Same words, written before the withhold', 'live',
   false, 1),
  (177, 1, 'jarvis:message:S1:120', '120', 'fpTAIL', 19, 'user', 'LEAK-TAIL kcmarker', 'live', false, 2),
  (178, 4, 'jarvis:message:S4:10', '10', 'fpTAIL', 0, 'user', 'Same words in another conversation', 'live', false, 2),
  (179, 5, 'jarvis:message:S5:300', '300', 'fpTAIL', 0, 'user', 'LEAK-TAIL in the continuation', 'live', false, 2);
INSERT INTO import_records (run_id, source_key, record_type, action, reason, target_table, target_id) VALUES
  (1, 'jarvis:message:S1:105', 'message', 'imported', '', 'archive_messages', 176),
  (2, 'jarvis:message:S1:120', 'message', 'imported', '', 'archive_messages', 177),
  (2, 'jarvis:message:S4:10', 'message', 'imported', '', 'archive_messages', 178),
  (2, 'jarvis:message:S5:300', 'message', 'imported', '', 'archive_messages', 179);
INSERT INTO archive_messages (id, session_id, source_key, source_message_id, source_fp, position, role, content, state,
                              withheld, import_run_id) VALUES
  (180, 1, 'jarvis:message:S1:130', '130', 'fpB', 29, 'user', '', 'live', true, 1),
  (181, 1, 'jarvis:message:S1:131', '131', 'fpB2', 30, 'user', 'LEAK-B zebraquokka91', 'live', false, 2),
  (182, 1, 'jarvis:message:S1:133', '133', 'fpB', 32, 'user', 'Similar zebraquokka93', 'live', false, 1);
INSERT INTO import_records (run_id, source_key, record_type, action, reason, target_table, target_id) VALUES
  (1, 'jarvis:message:S1:130', 'message', 'imported', '', 'archive_messages', 180),
  (1, 'jarvis:message:S1:131', 'message', 'duplicate', 'same as the previous message', 'archive_messages', 180),
  (2, 'jarvis:message:S1:131', 'message', 'imported', '', 'archive_messages', 181);
INSERT INTO archive_messages (id, session_id, source_key, source_message_id, source_fp, position, role, content, state,
                              withheld, import_run_id) VALUES
  (190, 1, 'jarvis:message:S1:140', '140', 'fpC1', 39, 'assistant', '', 'live', true, 1),
  (191, 1, 'jarvis:message:uid:u140', '140', 'fpC1', 39, 'assistant', 'LEAK-C1 kcmarkerokapi', 'live', false, 2);
INSERT INTO import_records (run_id, source_key, record_type, action, reason, target_table, target_id) VALUES
  (1, 'jarvis:message:S1:140', 'message', 'imported', '', 'archive_messages', 190),
  (2, 'jarvis:message:uid:u140', 'message', 'imported', '', 'archive_messages', 191);
INSERT INTO archive_messages (id, session_id, source_key, source_message_id, source_fp, position, role, content, state,
                              withheld, import_run_id) VALUES
  (200, 2, 'jarvis:message:uid:uX', '165', 'fpC3m', 1, 'assistant', 'LEAK-C3 marker overwrote row 200', 'live', false,
   1),
  (201, 2, 'jarvis:message:S2:165', '165', 'fpC3m', 1, 'assistant', '', 'live', true, 1);
INSERT INTO import_records (run_id, source_key, record_type, action, reason, target_table, target_id, before) VALUES
  (1, 'jarvis:message:uid:uX', 'message', 'imported', '', 'archive_messages', 200, '{}'),
  (1, 'jarvis:message:S2:165', 'message', 'imported', '', 'archive_messages', 201, '{}'),
  (2, 'jarvis:message:uid:uX', 'message', 'updated', 'changed since an earlier import', 'archive_messages', 200,
   '{"source_message_id": "164", "source_fp": "fpC3u", "position": 0, "role": "user", "content": "Plain user text",
     "withheld": false}');
INSERT INTO archive_messages (id, session_id, source_key, source_message_id, source_fp, position, role, content, state,
                              withheld, flags, import_run_id) VALUES
  (210, 3, 'jarvis:message:uid:uY', '164', 'fpC2u', 0, 'user', '', 'live', true,
   '{"withheld_by_admin": {"note": "x"}}', 1),
  (211, 3, 'jarvis:message:S3:165', '165', 'fpC2m', 1, 'assistant', 'LEAK-C2 marker', 'live', false, '{}', 2);
INSERT INTO import_records (run_id, source_key, record_type, action, reason, target_table, target_id, before) VALUES
  (1, 'jarvis:message:uid:uY', 'message', 'imported', '', 'archive_messages', 210, '{}'),
  (2, 'jarvis:message:uid:uY', 'message', 'updated', 'changed since an earlier import', 'archive_messages', 210,
   '{"source_message_id": "165", "source_fp": "fpC2m", "position": 1, "role": "assistant", "content": "",
     "withheld": true}'),
  (2, 'jarvis:message:S3:165', 'message', 'imported', '', 'archive_messages', 211, '{}');
SELECT setval('archive_messages_id_seq', 300);
INSERT INTO brain_tombstones (source_key, purged_at, purged_by, reason) VALUES
  ('jarvis:message:S1:101', '2026-10-03 10:00+00', 'admin@example.com', 'withheld by an admin'),
  ('jarvis:message:uid:uD', '2026-10-03 10:00+00', 'admin@example.com', 'withheld by an admin'),
  ('jarvis:message:S1:130', '2026-10-03 10:00+00', 'admin@example.com', 'withheld by an admin'),
  ('jarvis:message:S1:140', '2026-10-03 10:00+00', 'admin@example.com', 'withheld by an admin'),
  ('jarvis:message:S2:165', '2026-10-03 10:00+00', 'admin@example.com', 'withheld by an admin'),
  ('jarvis:message:uid:uY', '2026-10-03 10:00+00', 'admin@example.com', 'withheld by an admin'),
  ('jarvis:file:profile/memories/MEMORY.md#entry-0123456789abcdef', '2026-10-03 10:00+00', 'admin@example.com',
   'purged by an admin');
INSERT INTO memory_items (id, kind, title, body, status, origin, flags, source_ref, content_hash) VALUES
  (42, 'decision', 'Legacy zqtleg', 'LEAK legacy copy body', 'approved', 'promoted', '{"message_id": 170}',
   'JARVIS session S1 · message 9', 'h');
INSERT INTO archive_messages (id, session_id, source_key, source_message_id, source_fp, position, role, content, state,
                              withheld, flags, import_run_id) VALUES
  (209, 3, 'jarvis:message:uid:uA', '164', 'fpC2old', 0, 'user', 'C2 UNRELATED user164 text', 'live', false, '{}', 1),
  (230, 1, 'jarvis:message:uid:uQ', '150', 'fpYES', 40, 'user', '', 'live', true,
   '{"withheld_by_admin": {"at": "2026-10-03T10:00:00+00:00", "note": "x"}}', 1),
  (231, 1, 'jarvis:message:uid:uR', '190', 'fpYES', 60, 'user', 'yes INDEPENDENT later repeat own uid', 'live', false,
   '{}', 2);
INSERT INTO import_records (run_id, source_key, record_type, action, reason, target_table, target_id) VALUES
  (1, 'jarvis:message:uid:uA', 'message', 'imported', '', 'archive_messages', 209),
  (1, 'jarvis:message:uid:uQ', 'message', 'imported', '', 'archive_messages', 230),
  (2, 'jarvis:message:uid:uR', 'message', 'imported', '', 'archive_messages', 231);
INSERT INTO brain_tombstones (source_key, purged_at, purged_by, reason) VALUES
  ('jarvis:message:uid:uQ', '2026-10-03 10:00+00', 'admin@example.com', 'withheld by an admin');
"""

# The design's 15 extra states: a gateway /branch copy (fresh uid key, the parent's timestamp: before the branch
# started), an independent later turn of that branch, a 60-deep continuation chain, a parent cycle, a restricted
# conversation with a readable row (finding 3), a 0.6.0 withhold without a tombstone, a decisions-apply tombstone kept
# for later imports whose live row came back under its uid key, and a rollback copy with the text of row 177.
FX_EXTRA = """
INSERT INTO import_runs (id, status, started_at, importer_version)
VALUES (3, 'completed', '2026-10-06 10:00+00', '1.0.1');
SELECT setval('import_runs_id_seq', 3);
UPDATE archive_messages SET at = '2026-09-01 10:00+00' WHERE id IN (177);
INSERT INTO archive_sessions (id, source_key, source_session_id, lineage, parent_source_session_id, started_at,
                              sensitivity, import_run_id) VALUES
  (6, 'jarvis:session:S6', 'S6', 'branch', 'S1', '2026-10-04 12:00+00', 'general', 2),
  (7, 'jarvis:session:S7', 'S7', '', '', '2026-09-01 00:00+00', 'general', 2),
  (8, 'jarvis:session:S8', 'S8', 'continuation', 'S9', '2026-09-01 00:00+00', 'general', 2),
  (9, 'jarvis:session:S9', 'S9', 'continuation', 'S8', '2026-09-01 00:00+00', 'general', 2),
  (10, 'jarvis:session:S10', 'S10', '', '', '2026-09-01 00:00+00', 'restricted', 1),
  (11, 'jarvis:session:S11', 'S11', '', '', '2026-09-01 00:00+00', 'general', 1);
INSERT INTO archive_sessions (id, source_key, source_session_id, lineage, parent_source_session_id, started_at,
                              import_run_id)
SELECT 100 + g, 'jarvis:session:C' || g, 'C' || g, 'continuation', CASE WHEN g = 1 THEN 'S1' ELSE 'C' || (g - 1) END,
       '2026-10-04 00:00+00'::timestamptz + g * interval '1 minute', 2
  FROM generate_series(1, 60) g;
SELECT setval('archive_sessions_id_seq', 300);
INSERT INTO archive_messages (id, session_id, source_key, source_message_id, source_fp, position, role, content, state,
                              withheld, flags, import_run_id, at) VALUES
  (240, 6, 'jarvis:message:uid:uFRESH1', '400', 'fpTAIL', 0, 'user', 'LEAK-TAIL gateway branch copy', 'live', false,
   '{}', 2, '2026-09-01 10:00+00'),
  (241, 6, 'jarvis:message:uid:uFRESH2', '420', 'fpTAIL', 5, 'user', 'later independent turn same words', 'live',
   false, '{}', 2, '2026-10-05 09:00+00'),
  (242, 160, 'jarvis:message:C60:9', '9', 'fpTAIL', 0, 'user', 'LEAK-TAIL deep in the chain', 'live', false, '{}', 2,
   '2026-09-01 10:00+00'),
  (243, 8, 'jarvis:message:S8:1', '1', 'fpOTHER', 0, 'user', 'cycle session text', 'live', false, '{}', 2, NULL),
  (244, 10, 'jarvis:message:S10:1', '1', 'fpR1', 0, 'user', '', 'live', true,
   '{"withheld": "restricted data in this conversation"}', 1, NULL),
  (245, 10, 'jarvis:message:S10:2', '2', 'fpR2', 1, 'user', 'LEAK-R3 row no longer in the source', 'live', false,
   '{}', 1, NULL),
  (246, 11, 'jarvis:message:S11:7', '7', 'fpR1x', 0, 'assistant', '', 'live', true,
   '{"withheld_by_admin": {"at": "2026-10-02T09:00:00+00:00", "note": "x"}}', 1, NULL),
  (247, 11, 'jarvis:message:uid:uS11b', '7', 'fpR1x', 0, 'assistant', 'LEAK-R1 same JARVIS row uid key', 'live', false,
   '{}', 2, NULL),
  (248, 7, 'jarvis:message:uid:uL', '500', 'fpD', 0, 'assistant', 'LEAK-D live row', 'live', false, '{}', 2, NULL);
INSERT INTO import_records (run_id, source_key, record_type, action, reason, target_table, target_id, before) VALUES
  (2, 'jarvis:message:uid:uFRESH1', 'message', 'imported', '', 'archive_messages', 240, '{}'),
  (2, 'jarvis:message:uid:uFRESH2', 'message', 'imported', '', 'archive_messages', 241, '{}'),
  (2, 'jarvis:message:C60:9', 'message', 'imported', '', 'archive_messages', 242, '{}'),
  (1, 'jarvis:message:S11:7', 'message', 'imported', '', 'archive_messages', 246, '{}'),
  (2, 'jarvis:message:uid:uS11b', 'message', 'imported', '', 'archive_messages', 247, '{}'),
  (2, 'jarvis:message:uid:uL', 'message', 'imported', '', 'archive_messages', 248, '{}'),
  (3, 'jarvis:message:S1:120', 'message', 'updated', 'changed since an earlier import', 'archive_messages', 177,
   '{"source_message_id": "120", "source_fp": "fpTAIL", "position": 19, "role": "user",
     "content": "LEAK-TAIL copy text", "withheld": false, "state": "live"}');
INSERT INTO brain_tombstones (source_key, purged_at, purged_by, reason) VALUES
  ('jarvis:message:S7:500', '2026-10-03 10:00+00', 'admin@example.com', 'withheld by an admin');
"""


def _base(settings) -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)[0]


@pytest.fixture
async def scratch(settings):
    """An empty scratch database of this run's own (its URL), dropped afterwards."""
    admin = await asyncpg.connect(_base(settings) + "/postgres")
    try:
        await admin.execute("DROP DATABASE IF EXISTS %s" % SCRATCH)
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


@pytest.fixture
def dumps():
    written: list[tuple[str, str]] = []
    yield written
    for container, path in written:
        _pg(container, "rm", "-f", path)


async def _install_061(url: str, *fixtures: str) -> None:
    """0001-0007 as 0.6.1 applied them (schema_migrations rows), then the fixture's rows."""
    conn = await asyncpg.connect(url)
    try:
        await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, "
                           "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        for path in db.migration_files():
            if path.stem < "0008":
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", path.stem)
        for sql in fixtures:
            async with conn.transaction():
                await conn.execute(sql)
    finally:
        await conn.close()


async def _guards(conn: asyncpg.Connection) -> int:
    return int(await conn.fetchval("SELECT count(*) FROM pg_trigger WHERE tgname = 'archive_messages_keep_withheld'"))


async def _ids(conn: asyncpg.Connection, where: str) -> list[int]:
    return [r["id"] for r in await conn.fetch("SELECT id FROM archive_messages WHERE %s ORDER BY id" % where)]


async def _state(conn: asyncpg.Connection) -> dict[str, list[int]]:
    return {
        "withheld_by_repair": await _ids(conn, "withheld AND (flags -> 'withheld_by_admin' ? 'repaired' "
                                               "OR flags ? 'withheld_pending_copies')"),
        "readable": await _ids(conn, "NOT withheld"),
        "held": [r["id"] for r in await conn.fetch(
            "SELECT m.id FROM archive_messages m JOIN brain_tombstones t ON t.source_key = m.source_key "
            "WHERE t.review = 'pending' ORDER BY m.id")]}


async def _fx(url: str) -> asyncpg.Connection:
    await _install_061(url, FX_061, FX_EXTRA)
    assert await _migrate(url) == ["0008_brain_withhold_identity"]
    return await asyncpg.connect(url)


async def test_0008_fresh_database_applies_with_0006_0007_and_writes_no_audit(scratch):
    conn = await asyncpg.connect(scratch)
    try:
        await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, "
                           "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        for path in db.migration_files():
            if path.stem < "0006":
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", path.stem)
    finally:
        await conn.close()
    assert await _migrate(scratch) == ["0006_brain", "0007_brain_withhold", "0008_brain_withhold_identity"]
    assert await _migrate(scratch) == []
    conn = await asyncpg.connect(scratch)
    try:
        assert await conn.fetchval("SELECT count(*) FROM audit_log") == 0
        assert await _guards(conn) == 1
        done = json.loads(await conn.fetchval("SELECT detail FROM brain_maintenance WHERE name = 'withhold_identity'"))
        assert done["cause"] == "migration"
        pending = json.loads(await conn.fetchval("SELECT brain_withhold_pending()"))
        assert pending == {"readable_by_key": 0, "readable_by_jarvis_row": 0, "readable_by_fold": 0,
                           "readable_in_restricted_sessions": 0, "readable_copies_at_a_withheld_time": 0,
                           "unbound_stones": 0,
                           "withheld_with_text_fingerprint": 0, "held_for_review": 0, "repair_done": True}
        await _version_row(conn, "quarantined")  # 0007's CHECK still in force
    finally:
        await conn.close()


async def test_0008_repairs_every_0_6_1_leak_state(scratch):
    conn = await _fx(scratch)
    try:
        state = await _state(conn)
        assert state["withheld_by_repair"] == WITHHELD_BY_REPAIR
        for row in WITHHELD_BY_REPAIR:
            content, withheld, fp = await conn.fetchrow(
                "SELECT content, withheld, source_fp FROM archive_messages WHERE id = $1", row)
            assert (content, withheld) == ("", True) and fp.startswith("w1:"), row
        leaks = await conn.fetchval("SELECT count(*) FROM archive_messages WHERE content LIKE 'LEAK%'")
        assert leaks == 0
        # Their sessions show no summary until the start-up step recomputes them.
        pending = await conn.fetch("SELECT id, summary FROM archive_sessions WHERE flags ? 'withhold_repair_pending'")
        assert {r["id"] for r in pending} >= {1, 2, 3, 5, 6, 7, 10, 11, 160} and all(not r["summary"] for r in pending)
        # Every key of a row it withheld has a stone now, with the binding of that row.
        unstoned = await conn.fetchval(
            "SELECT count(*) FROM archive_messages m WHERE m.id = ANY($1::bigint[]) AND m.id <> 245 AND NOT EXISTS "
            "(SELECT 1 FROM brain_tombstones t WHERE t.source_key = m.source_key)", WITHHELD_BY_REPAIR)
        assert unstoned == 0
    finally:
        await conn.close()


async def test_0008_keeps_inverse_rows_readable(scratch):
    """O1 another conversation (178), O2 similar text (182), O3 stored before the withhold (176), O4/O10 an independent
    repeat with its own uid (231, 241), O6 the unrelated row a key moved to (209), and a cycle session's row (243)."""
    conn = await _fx(scratch)
    try:
        assert (await _state(conn))["readable"] == READABLE
        texts = {r["id"]: r["content"] for r in await conn.fetch(
            "SELECT id, content FROM archive_messages WHERE id = ANY($1::bigint[])", READABLE)}
        assert texts[209] == "C2 UNRELATED user164 text" and texts[231].startswith("yes INDEPENDENT")
        stones = await conn.fetchval("SELECT count(*) FROM brain_tombstones t JOIN archive_messages m ON "
                                     "m.source_key = t.source_key WHERE m.id = ANY($1::bigint[])", READABLE)
        assert stones == 0
    finally:
        await conn.close()


async def test_0008_holds_heuristic_hits_for_review_and_members_the_rest(scratch):
    conn = await _fx(scratch)
    try:
        assert (await _state(conn))["held"] == HELD
        reviews = {r["id"]: (r["review"], r["via"]) for r in await conn.fetch(
            "SELECT m.id, t.review, t.via FROM archive_messages m JOIN brain_tombstones t "
            "ON t.source_key = m.source_key WHERE m.id = ANY($1::bigint[])", WITHHELD_BY_REPAIR)}
        assert {i: r for i, (r, _) in reviews.items() if r} == {i: "pending" for i in HELD}
        assert all(via == "upgrade: same text" for i, (_, via) in reviews.items() if i in HELD)
        # The certain ones: by key (163, 181: the importer's own duplicate records), by JARVIS row (191, 200, 211, 247,
        # 248), a restricted conversation (245, no stone of its own). 163's record is 0.6.1's carried-on link (importer
        # 1.0.1 paired it by its text alone): still certain, labelled so; the next import compares its uid (review 2,
        # S6). 181 is an adjacent repeat.
        assert reviews[163] == ("", "upgrade: carried copy, text only") and reviews[181] == ("", "duplicate")
        for row in (191, 200, 211, 247, 248):
            assert reviews[row] == ("", "upgrade: same JARVIS row"), row
        assert 245 not in reviews
        held = await conn.fetchval("SELECT brain_withhold_pending() ->> 'held_for_review'")
        assert int(held) == len(HELD)
    finally:
        await conn.close()


async def test_0008_keeps_the_rollback_copy_of_an_unrelated_row(scratch):
    """O16: 0.6.1's C3 overwrite kept row 164's own text as the rollback copy of row 200: it is readable in its own
    row, so it stays; the copy of the resurrected row 177's text goes."""
    conn = await _fx(scratch)
    try:
        assert await conn.fetchval(
            "SELECT count(*) FROM import_records WHERE before ->> 'content' = 'Plain user text'") == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM import_records WHERE before::text LIKE '%LEAK-TAIL copy text%'") == 0
    finally:
        await conn.close()


async def test_0008_binds_c2_to_the_row_withheld_not_the_row_the_key_moved_to(scratch):
    conn = await _fx(scratch)
    try:
        stone = await conn.fetchrow("SELECT * FROM brain_tombstones WHERE source_key = 'jarvis:message:uid:uY'")
        assert (stone["source_session_id"], stone["source_row_id"], stone["role"]) == ("S3", "165", "assistant")
        assert stone["uid_known"] and stone["bound"] and stone["anchor_key"] == ""
        assert await conn.fetchval("SELECT withheld FROM archive_messages WHERE id = 211")
        assert not await conn.fetchval("SELECT withheld FROM archive_messages WHERE id = 209")
        # 0.6.1's own victim (the key moved onto row 164, 0.6.1 kept it withheld) stays withheld: fail-closed (e).
        assert await conn.fetchval("SELECT withheld FROM archive_messages WHERE id = 210")
        # The stones of the other withholds name the rows the admin saw: A-tail from the deleted row's copy, D and the
        # 0.6.0 withhold from their keys.
        rows = {r["source_key"]: (r["source_session_id"], r["source_row_id"]) for r in await conn.fetch(
            "SELECT source_key, source_session_id, source_row_id FROM brain_tombstones WHERE anchor_key = ''")}
        assert rows["jarvis:message:uid:uD"] == ("S1", "110") and rows["jarvis:message:S7:500"] == ("S7", "500")
        assert rows["jarvis:message:S11:7"] == ("S11", "7")
    finally:
        await conn.close()


async def test_0008_walks_lineage_without_depth_limit_and_survives_a_parent_cycle(scratch):
    """The fixture's 60-deep chain, a 600-deep one, and two sessions that name each other as parent (a row with the
    withheld message's fingerprint there stays readable: it is not in the withheld conversation's lineage)."""
    deep = """
    INSERT INTO archive_sessions (id, source_key, source_session_id, lineage, parent_source_session_id, started_at,
                                  import_run_id)
    SELECT 1000 + g, 'jarvis:session:D' || g, 'D' || g, 'continuation',
           CASE WHEN g = 1 THEN 'S1' ELSE 'D' || (g - 1) END,
           '2026-10-04 00:00+00'::timestamptz + g * interval '1 second', 2 FROM generate_series(1, 600) g;
    INSERT INTO archive_messages (id, session_id, source_key, source_message_id, source_fp, position, role, content,
                                  withheld, import_run_id, at)
    VALUES (500, 1600, 'jarvis:message:D600:1', '1', 'fpTAIL', 0, 'user', 'LEAK-TAIL 600 deep', false, 2,
            '2026-09-01 10:00+00'),
           (501, 1600, 'jarvis:message:D600:2', '2', 'fpOTHER2', 1, 'user', 'other words 600 deep', false, 2, NULL),
           (502, 8, 'jarvis:message:S8:2', '2', 'fpTAIL', 1, 'user', 'same words in the cycle', false, 2, NULL);
    INSERT INTO import_records (run_id, source_key, record_type, action, reason, target_table, target_id) VALUES
      (2, 'jarvis:message:D600:1', 'message', 'imported', '', 'archive_messages', 500),
      (2, 'jarvis:message:D600:2', 'message', 'imported', '', 'archive_messages', 501),
      (2, 'jarvis:message:S8:2', 'message', 'imported', '', 'archive_messages', 502);
    """
    await _install_061(scratch, FX_061, FX_EXTRA, deep)
    assert await _migrate(scratch) == ["0008_brain_withhold_identity"]
    conn = await asyncpg.connect(scratch)
    try:
        # The cycle (S8 and S9 name each other) ends the walk without reaching the withheld conversation.
        withheld = await _ids(conn, "withheld AND id IN (242, 500, 501, 243, 502)")
        assert withheld == [242, 500]
        assert (await _state(conn))["held"] == sorted(HELD + [500])
    finally:
        await conn.close()


async def test_0008_twice_changes_nothing_and_audits_once(scratch):
    conn = await _fx(scratch)
    try:
        before = await conn.fetchval("SELECT count(*) FROM audit_log")
        audit = json.loads(await conn.fetchval(
            "SELECT detail FROM audit_log WHERE action = 'brain.withhold_identity_repaired'"))
        assert before == 1 and audit["full"] is True and audit["held_by_same_text"] == len(HELD)
        assert await conn.fetchval("SELECT actor FROM audit_log") == "system (migration)"
        snapshot = await conn.fetch("SELECT to_jsonb(m) - 'search' AS r FROM archive_messages m ORDER BY id")
        stones = await conn.fetch("SELECT to_jsonb(t) - 'purged_at' AS r FROM brain_tombstones t ORDER BY source_key")
        records = await conn.fetch("SELECT before FROM import_records ORDER BY id")
        again = json.loads(await conn.fetchval("SELECT brain_withhold_repair('startup-check')"))
        assert again["full"] is False and not any(v for k, v in again.items() if k != "full")
        async with conn.transaction():
            await conn.execute((db.MIGRATIONS_DIR / "0008_brain_withhold_identity.sql").read_text(encoding="utf-8"))
        assert await conn.fetchval("SELECT count(*) FROM audit_log") == 1
        assert await conn.fetch("SELECT to_jsonb(m) - 'search' AS r FROM archive_messages m ORDER BY id") == snapshot
        assert await conn.fetch("SELECT to_jsonb(t) - 'purged_at' AS r FROM brain_tombstones t "
                                "ORDER BY source_key") == stones
        assert await conn.fetch("SELECT before FROM import_records ORDER BY id") == records
    finally:
        await conn.close()


async def test_0008_a_stone_with_no_evidence_is_not_counted_or_audited_again(scratch):
    """A withhold carried as a key that names no message (decisions-apply accepts any key): it stays unbound, and
    every later repair (each start, each import) finds nothing to do and writes no audit entry."""
    unbound = ("INSERT INTO brain_tombstones (source_key, purged_at, reason) VALUES "
               "('jarvis:file:profile/knowledge/a.md', '2026-10-03 10:00+00', 'withheld by an admin');")
    await _install_061(scratch, unbound)
    assert await _migrate(scratch) == ["0008_brain_withhold_identity"]
    conn = await asyncpg.connect(scratch)
    try:
        for _ in range(2):
            again = json.loads(await conn.fetchval("SELECT brain_withhold_repair('startup-check')"))
            assert not any(v for k, v in again.items() if k != "full"), again
        assert await conn.fetchval("SELECT count(*) FROM audit_log") == 0
        assert json.loads(await conn.fetchval("SELECT brain_withhold_pending()"))["unbound_stones"] == 1
    finally:
        await conn.close()


async def test_0008_converts_every_plain_fingerprint_of_withheld_rows_and_copies(scratch):
    conn = await _fx(scratch)
    try:
        assert await conn.fetchval("SELECT count(*) FROM archive_messages WHERE withheld "
                                   "AND source_fp NOT LIKE 'w1:%'") == 0
        assert await conn.fetchval("SELECT source_fp FROM archive_messages WHERE id = 158") == "w1:compacted"
        plain = await conn.fetchval(
            "SELECT count(*) FROM import_records x WHERE x.record_type = 'message' AND x.before <> '{}'::jsonb "
            "AND coalesce(x.before -> 'deleted_row' ->> 'withheld', x.before ->> 'withheld') = 'true' "
            "AND coalesce(x.before -> 'deleted_row' ->> 'source_fp', x.before ->> 'source_fp', 'w1:') NOT LIKE 'w1:%'")
        assert plain == 0
        deleted = json.loads(await conn.fetchval(
            "SELECT before FROM import_records WHERE before ? 'deleted_row' AND target_id = 170"))
        assert deleted["deleted_row"]["source_fp"] == "w1:live"
        assert json.loads(await conn.fetchval("SELECT brain_withhold_pending()"))["withheld_with_text_fingerprint"] == 0
    finally:
        await conn.close()


async def test_trigger_blanks_stoned_key_and_binding_and_leaves_other_roles_and_rows(scratch):
    await _install_061(scratch)
    assert await _migrate(scratch) == ["0008_brain_withhold_identity"]
    conn = await asyncpg.connect(scratch)
    try:
        await conn.execute("INSERT INTO archive_sessions (id, source_key, source_session_id) VALUES "
                           "(1, 'jarvis:session:T1', 'T1')")
        await conn.execute(
            "INSERT INTO brain_tombstones (source_key, reason, source_session_id, source_row_id, role, bound) VALUES "
            "('jarvis:message:uid:K', 'withheld by an admin', 'T1', '3', 'user', true), "
            "('jarvis:message:T1:7', 'withheld by an admin', 'T1', '7', 'assistant', true)")

        async def row(key: str, row_id: str, role: str, content: str = "trigger words") -> dict:
            r = await conn.fetchrow(
                "INSERT INTO archive_messages (session_id, source_key, source_message_id, source_fp, position, role, "
                "content, tool_calls, labels) VALUES (1, $1, $2, 'plainfp', 0, $3, $4, '[{\"name\": \"x\"}]', "
                "'{accomplishment}') RETURNING content, withheld, source_fp, tool_calls::text AS calls, labels, flags",
                key, row_id, role, content)
            return dict(r)

        by_key = await row("jarvis:message:uid:K", "99", "user")
        assert (by_key["content"], by_key["withheld"], by_key["source_fp"], by_key["calls"], by_key["labels"]) == (
            "", True, "w1:live", "[]", [])
        assert json.loads(by_key["flags"])["withheld_by_admin"]["guard"] == "key"
        by_row = await row("jarvis:message:uid:other", "7", "assistant")
        assert by_row["withheld"] and json.loads(by_row["flags"])["withheld_by_admin"]["guard"] == "same JARVIS row"
        other_role = await row("jarvis:message:uid:tool7", "7", "tool")
        unrelated = await row("jarvis:message:T1:8", "8", "assistant")
        assert not other_role["withheld"] and other_role["content"] == "trigger words"
        assert not unrelated["withheld"] and json.loads(unrelated["flags"]) == {}
        # An update of a readable row is left alone; one that gives it a stoned key is blanked.
        await conn.execute("UPDATE archive_messages SET content = 'edited' WHERE source_key = 'jarvis:message:T1:8'")
        assert await conn.fetchval("SELECT content FROM archive_messages WHERE source_key = 'jarvis:message:T1:8'") == (
            "edited")
        await conn.execute("UPDATE archive_messages SET source_key = 'jarvis:message:T1:7' "
                           "WHERE source_key = 'jarvis:message:uid:tool7'")
        assert await conn.fetchval("SELECT withheld FROM archive_messages WHERE source_key = 'jarvis:message:T1:7'")
        # A released hold (its stone deleted) set readable stays readable.
        await conn.execute("DELETE FROM brain_tombstones WHERE source_key = 'jarvis:message:uid:K'")
        await conn.execute("UPDATE archive_messages SET withheld = false, content = 'released' "
                           "WHERE source_key = 'jarvis:message:uid:K'")
        released = "SELECT content FROM archive_messages WHERE source_key = 'jarvis:message:uid:K'"
        assert await conn.fetchval(released) == "released"
    finally:
        await conn.close()


async def test_brain_withhold_pending_is_read_only(scratch):
    conn = await _fx(scratch)
    try:
        # A leak 0.6.1 code could still write with the guard off (a session_replication_role replica, as pg_restore's
        # data load runs): a readable row under a stoned key.
        await conn.execute("SET session_replication_role = replica")
        await conn.execute("UPDATE archive_messages SET withheld = false, content = 'LEAK-again' WHERE id = 158")
        await conn.execute("SET session_replication_role = origin")
        snapshot = await conn.fetch("SELECT to_jsonb(m) - 'search' AS r FROM archive_messages m ORDER BY id")
        audits = await conn.fetchval("SELECT count(*) FROM audit_log")
        async with conn.transaction(readonly=True):
            pending = json.loads(await conn.fetchval("SELECT brain_withhold_pending()"))
        assert pending["readable_by_key"] == 1 and pending["repair_done"] is True
        assert pending["held_for_review"] == len(HELD) and pending["unbound_stones"] == 0
        assert await conn.fetch("SELECT to_jsonb(m) - 'search' AS r FROM archive_messages m ORDER BY id") == snapshot
        assert await conn.fetchval("SELECT count(*) FROM audit_log") == audits
        repaired = json.loads(await conn.fetchval("SELECT brain_withhold_repair('cli')"))
        assert repaired["withheld_by_key"] == 1
        assert await conn.fetchval("SELECT content FROM archive_messages WHERE id = 158") == ""
    finally:
        await conn.close()


@needs_docker
async def test_restore_of_a_0_6_1_dump_over_0_6_2_reruns_0008_and_restores_the_trigger(scratch, dumps):
    container = _container()
    dumps.append((container, DUMP))
    await _install_061(scratch, FX_061, FX_EXTRA)
    dump = _pg(container, "pg_dump", "-U", "cc", "-Fc", "-f", DUMP, SCRATCH)
    assert dump.returncode == 0, dump.stderr
    assert await _migrate(scratch) == ["0008_brain_withhold_identity"]  # 0.6.2 installed
    conn = await asyncpg.connect(scratch)
    try:  # a purge made on 0.6.2 after the dump: the restore undoes it
        await conn.execute("INSERT INTO brain_purged_content (fp, source_key) VALUES ('k1:x:1', "
                           "'jarvis:file:profile/knowledge/after-dump.md')")
        await conn.execute("INSERT INTO brain_tombstones (source_key, reason) VALUES "
                           "('jarvis:file:profile/knowledge/after-dump.md', 'purged by an admin')")
    finally:
        await conn.close()
    restore = _pg(container, "pg_restore", "-U", "cc", "-d", SCRATCH, "--clean", "--if-exists",
                  "--single-transaction", "--exit-on-error", DUMP)
    assert restore.returncode == 0, restore.stderr
    conn = await asyncpg.connect(scratch)
    try:  # the dump's leaks are back, and so is a schema without the guard
        assert await _guards(conn) == 0
        assert await conn.fetchval("SELECT count(*) FROM archive_messages WHERE content LIKE 'LEAK%'") > 0
    finally:
        await conn.close()
    assert await _migrate(scratch) == ["0008_brain_withhold_identity"]  # the next start
    conn = await asyncpg.connect(scratch)
    try:
        assert await _guards(conn) == 1
        assert await conn.fetchval("SELECT count(*) FROM brain_purged_content") == 0  # the undone purge's fingerprint
        assert (await _state(conn))["readable"] == READABLE
        assert await conn.fetchval("SELECT count(*) FROM archive_messages WHERE content LIKE 'LEAK%'") == 0
        assert json.loads(await conn.fetchval(
            "SELECT detail FROM brain_maintenance WHERE name = 'withhold_identity'"))["cause"] == "migration"
    finally:
        await conn.close()


@needs_docker
async def test_restore_of_a_0_6_2_dump_keeps_functions_and_trigger(scratch, dumps):
    container = _container()
    dumps.append((container, DUMP_062))
    conn = await _fx(scratch)
    await conn.close()
    dump = _pg(container, "pg_dump", "-U", "cc", "-Fc", "-f", DUMP_062, SCRATCH)
    assert dump.returncode == 0, dump.stderr
    conn = await asyncpg.connect(scratch)
    try:  # a change after the dump that the restore undoes
        await conn.execute("DELETE FROM brain_tombstones WHERE review = 'pending'")
    finally:
        await conn.close()
    restore = _pg(container, "pg_restore", "-U", "cc", "-d", SCRATCH, "--clean", "--if-exists",
                  "--single-transaction", "--exit-on-error", DUMP_062)
    assert restore.returncode == 0, restore.stderr
    assert await _migrate(scratch) == []  # 0008 is recorded in the dump
    conn = await asyncpg.connect(scratch)
    try:
        assert await _guards(conn) == 1
        for fn in ("brain_withhold_repair(text)", "brain_withhold_pending()", "archive_messages_keep_withheld()"):
            assert await conn.fetchval("SELECT to_regprocedure($1) IS NOT NULL", fn), fn
        assert (await _state(conn))["held"] == HELD
        await conn.execute("UPDATE archive_messages SET withheld = false, content = 'LEAK restored' WHERE id = 158")
        assert await conn.fetchval("SELECT content FROM archive_messages WHERE id = 158") == ""
    finally:
        await conn.close()


# --- Review 1 R34 and R35: the SQL repair's uid rule holds, and its lineage guard (brain_withhold_repair) ----------


async def _r_session(s, sid: str, *, lineage: str = "", parent: str = "") -> int:
    return (await s.execute(text(
        "INSERT INTO archive_sessions (source_system, source_key, source_session_id, lineage, parent_source_session_id,"
        " started_at, sensitivity, labels, flags) VALUES ('jarvis', :k, :sid, :lin, :par, now() - interval '1 day', "
        "'general', '{}'::jsonb, '{}'::jsonb) RETURNING id"),
        {"k": "jarvis:session:" + sid, "sid": sid, "lin": lineage, "par": parent})).scalar()


async def _r_message(s, sess: int, key: str, row: str, role: str, content: str, *, uid: str = "", fp: str = "",
                     withheld: bool = False, run: int | None = None, pos: int = 1) -> int:
    return (await s.execute(text(
        "INSERT INTO archive_messages (session_id, source_key, source_message_id, source_fp, position, role, content,"
        " withheld, source_uid, import_run_id, tool_calls, labels, flags, redactions, at) VALUES (:s, :k, :r, :fp, :p,"
        " :role, :c, :w, :u, :run, '[]'::jsonb, '{}', '{}'::jsonb, '{}'::jsonb, now()) RETURNING id"),
        {"s": sess, "k": key, "r": row, "fp": fp, "p": pos, "role": role, "c": content, "w": withheld, "u": uid,
         "run": run})).scalar()


async def _r_stone(s, key: str, sid: str, row: str, role: str, uid: str, *, after: int = 0,
                   uid_known: bool = False) -> None:
    await s.execute(text(
        "INSERT INTO brain_tombstones (source_key, purged_at, purged_by, reason, source_session_id, source_row_id, "
        "role, tool_call_id, message_uid, uid_known, anchor_key, via, review, bound, after_run) VALUES (:k, "
        "now() - interval '2 hours', 'admin', 'withheld by an admin', :sid, :row, :role, '', :uid, :uk, '', 'admin',"
        " '', true, :after)"),
        {"k": key, "sid": sid, "row": row, "role": role, "uid": uid, "uk": uid_known, "after": after})


@pytest.mark.usefixtures("clean_db")
async def test_r34_the_repair_holds_a_row_that_only_shares_a_withheld_uid_for_review():
    """Review 1 R34 (tests-integrity #5): a readable row under another key and another JARVIS row that shares a
    certain stone's uid is caught by the SQL repair's uid rule (R4c) alone, and as a hold for an admin's review, never
    as a member: the row is blanked, its key gets a pending stone ('upgrade: same message id'), counted once."""
    mark = "zqr34uidrow"
    async with session_scope() as s:
        sr = await _r_session(s, "R34")
        await _r_message(s, sr, "jarvis:message:R34:101", "101", "assistant", "", uid="u-r34-probe", withheld=True,
                         fp="w1:live", pos=1)
        other = await _r_message(s, sr, "jarvis:message:uid:u-r34-probe", "205", "assistant",
                                 "The pin is %s." % mark, uid="u-r34-probe", pos=2)
        await _r_stone(s, "jarvis:message:R34:101", "R34", "101", "assistant", "u-r34-probe", uid_known=True)
    async with session_scope() as s:
        counts = await identity.repair_rows(s, cause="test")
    async with session_scope() as s:
        row = (await s.execute(text("SELECT withheld, content FROM archive_messages WHERE id = :i"),
                               {"i": other})).one()
        stone = (await s.execute(text("SELECT review, via FROM brain_tombstones WHERE source_key = :k"),
                                 {"k": "jarvis:message:uid:u-r34-probe"})).one_or_none()
    assert (row.withheld, row.content) == (True, ""), tuple(row)
    assert stone is not None and (stone.review, stone.via) == ("pending", "upgrade: same message id"), stone
    assert counts.get("held_by_message_id") == 1, counts


@pytest.mark.usefixtures("clean_db")
async def test_r35_the_repair_follows_a_continuation_never_a_delegated_child():
    """Review 1 R35 (tests-integrity #6): 0.6.1's plain same-text fingerprint (R4d) holds a continuation's copy of a
    withheld row for review, but never a delegated child's row: a sub-agent's conversation is not a copy of its
    parent's history (owner requirement 2). Fails both without the lineage guard and without R4d."""
    words = "The fence gate closes at nine and opens at seven every single day of the week."
    fp = hashlib.sha256(words.encode()).hexdigest()  # a 0.6.1-shaped plain fingerprint
    async with session_scope() as s:
        run1 = (await s.execute(text("INSERT INTO import_runs (status, started_at, finished_at) VALUES "
                                     "('completed', now() - interval '3 hours', now() - interval '3 hours') "
                                     "RETURNING id"))).scalar()
        run2 = (await s.execute(text("INSERT INTO import_runs (status, started_at, finished_at) VALUES "
                                     "('completed', now() - interval '1 hour', now() - interval '1 hour') "
                                     "RETURNING id"))).scalar()
        parent = await _r_session(s, "R35P")
        await _r_message(s, parent, "jarvis:message:R35P:7", "7", "assistant", "", fp=fp, withheld=True, run=run1)
        await _r_stone(s, "jarvis:message:R35P:7", "R35P", "7", "assistant", "", after=run1)
        rows = {}
        for lineage in ("delegated", "continuation"):
            child = await _r_session(s, "R35" + lineage[:3].upper(), lineage=lineage, parent="R35P")
            rows[lineage] = await _r_message(s, child, "jarvis:message:R35%s:3" % lineage[:3].upper(), "3",
                                             "assistant", words, fp=fp, run=run2)
    async with session_scope() as s:
        counts = await identity.repair_rows(s, cause="test")
    async with session_scope() as s:
        got = {k: tuple((await s.execute(text("SELECT withheld, content = '' FROM archive_messages WHERE id = :i"),
                                         {"i": i})).one()) for k, i in rows.items()}
    assert got["delegated"] == (False, False), got  # a sub-agent's own row: never held for the parent's text
    assert got["continuation"] == (True, True), got  # the control: a continuation's copy is held
    assert counts.get("held_by_same_text") == 1, counts
