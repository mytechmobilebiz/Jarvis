"""0008_brain_withhold_identity.sql on the databases it meets (spec 3): a fresh one, a 0.6.1 database holding every
confirmed leak state (the design's Appendix B fixture, as SQL inserts), a lineage deeper than any limit, and the
restore of a 0.6.1 or a 0.6.2 dump (scripts/restore.ps1's pg_restore flags). The real migration runner applies it.

Every row is synthetic. Row numbers are the fixture's: 158-248 as the design's tables name them.

The restore tests need docker (pg_dump and pg_restore in the cc-test-pg container) and are skipped without it;
REQUIRE_DOCKER_TESTS=1 makes a missing docker or container a failure instead."""

from __future__ import annotations

import asyncio
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
# What 0008 still does to the 0.6.1 fixture (0.6.2 plan 1.6): 245 (R5: a restricted conversation's readable row) is
# withheld; every legacy re-derivation (R1-R3, R4c, R4d) is gone, so the rows it found stay as 0.6.1 left them until
# the legacy reset (plan 1.7, 8).
WITHHELD_BY_REPAIR = [245]

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
                           "readable_in_restricted_sessions": 0, "unbound_stones": 0,  # step 6: no time counter
                           "withheld_with_text_fingerprint": 0, "held_for_review": 0, "repair_done": True,
                           # 0.6.2 plan 1.8
                           "unsettled_decisions": 0, "unreviewed_settle_holds": 0, "quarantined_rows": 0,
                           "frozen_sessions": 0, "legacy_lock": False, "last_tripwire": None}
        assert await conn.fetchval("SELECT count(*) FROM brain_maintenance WHERE name = 'legacy_brain_data'") == 0
        await _version_row(conn, "quarantined")  # 0007's CHECK still in force
    finally:
        await conn.close()


async def test_0008_marks_0_6_1_data_legacy_and_never_re_derives_it(scratch):
    """0.6.2 plan 1.6, 1.7: a database 0.6.0 or 0.6.1 imported into (runs by an importer older than 1.1.0 that were not
    rolled back, withholds made before 0008) is marked legacy_brain_data, for the legacy lock and reset; the repair no
    longer binds unbound stones (R1, R2), follows text-only carried links (R3) or holds rows by 0.6.1's fingerprints or
    a shared uid (R4c, R4d). What it still does: the key rule, the exact JARVIS row, folds, restricted conversations
    and fingerprints (R4a, R4b, R4e, R5, R6, R7)."""
    conn = await _fx(scratch)
    try:
        legacy = json.loads(await conn.fetchval(
            "SELECT detail FROM brain_maintenance WHERE name = 'legacy_brain_data'"))
        assert legacy["runs_by_an_older_importer"] == 3  # the fixture's three runs by importer 1.0.1
        assert legacy["withholds_before_0008"] == await conn.fetchval(
            "SELECT count(*) FROM brain_tombstones WHERE reason = 'withheld by an admin' AND via = ''")
        assert (await _state(conn))["withheld_by_repair"] == WITHHELD_BY_REPAIR
        assert await conn.fetchval("SELECT count(*) FROM brain_tombstones WHERE via LIKE 'upgrade: %' OR "
                                   "review = 'pending'") == 0
        assert await conn.fetchval("SELECT count(*) FROM brain_tombstones WHERE reason = 'withheld by an admin' AND "
                                   "bound") == 0  # never bound by the upgrade (R2 is gone)
        pending = json.loads(await conn.fetchval("SELECT brain_withhold_pending()"))
        assert pending["legacy_lock"] is True and pending["unbound_stones"] > 0
    finally:
        await conn.close()


async def test_0008_waits_for_an_older_importer_that_is_still_running(scratch):
    """Plan 1.7 (review 3, rv3-dur-2): an import by an importer older than 1.1.0 that holds its run's lock while 0.6.2
    starts: 0008 is refused (nothing changed), so that import never finishes on this schema. Once it has ended (its
    lock is free), 0008 applies and marks its data legacy."""
    from app.brain import importer as imp

    await _install_061(scratch, "INSERT INTO import_runs (id, status, started_at, importer_version) VALUES "
                                "(7, 'running', now(), '1.0.1');")
    holder = await asyncpg.connect(scratch)
    holder_pid = await holder.fetchval("SELECT pg_backend_pid()")
    try:
        assert await holder.fetchval("SELECT pg_try_advisory_lock($1::int, 7)", imp.LOCK_KEY)
        with pytest.raises(Exception, match="still running"):
            await _migrate(scratch)
        assert await holder.fetchval("SELECT count(*) FROM schema_migrations WHERE version = "
                                     "'0008_brain_withhold_identity'") == 0
    finally:
        await holder.close()
    # close() returns before the server has ended that session and freed its lock (under load the next migration
    # could still find it held): wait until the old importer's session is gone (integrator, deviation 257)
    probe = await asyncpg.connect(scratch)
    try:
        for _ in range(200):
            if not await probe.fetchval("SELECT count(*) FROM pg_stat_activity WHERE pid = $1", holder_pid):
                break
            await asyncio.sleep(0.05)
    finally:
        await probe.close()
    assert await _migrate(scratch) == ["0008_brain_withhold_identity"]
    conn = await asyncpg.connect(scratch)
    try:
        legacy = json.loads(await conn.fetchval("SELECT detail FROM brain_maintenance WHERE name = "
                                                "'legacy_brain_data'"))
        assert legacy["runs_by_an_older_importer"] == 1
    finally:
        await conn.close()


async def test_0008_keeps_the_rollback_copy_of_an_unrelated_row(scratch):
    """O16: 0.6.1's C3 overwrite kept row 164's own text as the rollback copy of row 200: it is readable in its own
    row, so it stays. (Legacy rows are not re-derived any more, 0.6.2 plan 1.6: the legacy lock and reset, plan 8.)"""
    conn = await _fx(scratch)
    try:
        assert await conn.fetchval(
            "SELECT count(*) FROM import_records WHERE before ->> 'content' = 'Plain user text'") == 1
    finally:
        await conn.close()


async def test_0008_twice_changes_nothing_and_audits_once(scratch):
    conn = await _fx(scratch)
    try:
        before = await conn.fetchval("SELECT count(*) FROM audit_log")
        audit = json.loads(await conn.fetchval(
            "SELECT detail FROM audit_log WHERE action = 'brain.withhold_identity_repaired'"))
        assert before == 1 and audit["full"] is True and "held_by_same_text" not in audit
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
    # 0.6.2 code says so on its connections (app/db.py); without it 0008 refuses any archived message (rv3-dur-2)
    conn = await asyncpg.connect(scratch, server_settings={"cc.brain_purge_ledger": "on"})
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
        # 0.6.1 data is legacy (plan 1.7): marked, never re-derived into holds
        assert pending["held_for_review"] == 0 and pending["legacy_lock"] is True
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
        assert json.loads(await conn.fetchval(
            "SELECT detail FROM brain_maintenance WHERE name = 'withhold_identity'"))["cause"] == "migration"
        # The restored 0.6.1 data is legacy again (0.6.2 plan 1.7): marked for the legacy lock and reset, its rows
        # never re-derived (three runs by importer 1.0.1, and withholds made before 0008).
        legacy = json.loads(await conn.fetchval(
            "SELECT detail FROM brain_maintenance WHERE name = 'legacy_brain_data'"))
        assert legacy["runs_by_an_older_importer"] == 3 and legacy["withholds_before_0008"] >= 1
    finally:
        await conn.close()


@needs_docker
async def test_restore_of_a_0_6_2_dump_keeps_functions_and_trigger(scratch, dumps):
    container = _container()
    dumps.append((container, DUMP_062))
    conn = await _fx(scratch)
    stones = await conn.fetch("SELECT to_jsonb(t) AS r FROM brain_tombstones t ORDER BY source_key")
    await conn.close()
    dump = _pg(container, "pg_dump", "-U", "cc", "-Fc", "-f", DUMP_062, SCRATCH)
    assert dump.returncode == 0, dump.stderr
    conn = await asyncpg.connect(scratch)
    try:  # a change after the dump that the restore undoes
        await conn.execute("DELETE FROM brain_tombstones WHERE reason = 'withheld by an admin'")
    finally:
        await conn.close()
    restore = _pg(container, "pg_restore", "-U", "cc", "-d", SCRATCH, "--clean", "--if-exists",
                  "--single-transaction", "--exit-on-error", DUMP_062)
    assert restore.returncode == 0, restore.stderr
    assert await _migrate(scratch) == []  # 0008 is recorded in the dump
    conn = await asyncpg.connect(scratch)
    try:
        assert await _guards(conn) == 1
        for fn in ("brain_withhold_repair(text)", "brain_withhold_pending()", "archive_messages_keep_withheld()",
                   "brain_exact(text,text,timestamp with time zone,text,text,text,timestamp with time zone,text)",
                   "brain_refuse_old_rows()", "archive_sessions_keep_lineage()"):
            assert await conn.fetchval("SELECT to_regprocedure($1) IS NOT NULL", fn), fn
        assert await conn.fetch("SELECT to_jsonb(t) AS r FROM brain_tombstones t ORDER BY source_key") == stones
        # code older than 0.6.2 cannot write an archived message (rv3-dur-2); 0.6.2's own write meets the guard
        with pytest.raises(asyncpg.RaiseError, match="code older than 0.6.2"):
            await conn.execute("UPDATE archive_messages SET withheld = false, content = 'LEAK restored' "
                               "WHERE id = 158")
        await conn.execute("SET cc.brain_purge_ledger = 'on'")
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


# --- Review 2 S25 and S26 (tests-integrity #1, #2): the SQL repair's remaining rules and guards ----------------------
# Each plants its rows BEFORE the stone (so the database guard never acts first), runs identity.repair_rows (what the
# migration, the start-up check, every import and rescan --yes run) and checks the rows, the stones and the counters.


async def _row_state(s, i: int) -> tuple:
    return tuple((await s.execute(text("SELECT withheld, content FROM archive_messages WHERE id = :i"),
                                  {"i": i})).one())


async def _stone_of(s, key: str) -> tuple | None:
    r = (await s.execute(text("SELECT review, via, anchor_key FROM brain_tombstones WHERE source_key = :k"),
                         {"k": key})).one_or_none()
    return tuple(r) if r is not None else None


@pytest.mark.usefixtures("clean_db")
async def test_r4e_withholds_a_fold_of_a_certain_uid_and_never_a_fold_of_a_held_rows_uid():
    """S25 (1) and S26 (2): R4e, a stored readable fold (Hermes absorbed_message_uids) that absorbed the withheld
    message's uid (a certain stone, uid known) holds its text verbatim: a member, withheld at once. Its guard: a fold
    that absorbed only a HELD row's own uid (a pending stone) is left alone; that uid says nothing of the withheld
    message, and a member is never listed for an admin's review (owner requirement 2)."""
    async with session_scope() as s:
        sr = await _r_session(s, "R4E")
        await _r_message(s, sr, "jarvis:message:R4E:10", "10", "user", "", uid="u-r4e-w", withheld=True,
                         fp="w1:live", pos=1)
        await _r_message(s, sr, "jarvis:message:R4E:11", "11", "user", "", uid="u-r4e-held", withheld=True,
                         fp="w1:live", pos=2)
        fold_a = await _r_message(s, sr, "jarvis:message:R4E:30", "30", "assistant",
                                  "Summary holding the withheld words zqr4efoldcert.", uid="u-r4e-folda", pos=3)
        fold_b = await _r_message(s, sr, "jarvis:message:R4E:31", "31", "assistant",
                                  "Summary of a held row's own words zqr4efoldheld.", uid="u-r4e-foldb", pos=4)
        await s.execute(text("UPDATE archive_messages SET absorbed_uids = ARRAY['u-r4e-w'] WHERE id = :i"),
                        {"i": fold_a})
        await s.execute(text("UPDATE archive_messages SET absorbed_uids = ARRAY['u-r4e-held'] WHERE id = :i"),
                        {"i": fold_b})
        await _r_stone(s, "jarvis:message:R4E:10", "R4E", "10", "user", "u-r4e-w", uid_known=True)
        await s.execute(text(  # the hold: a pending stone of the held row, anchored on the admin's stone
            "INSERT INTO brain_tombstones (source_key, purged_at, purged_by, reason, source_session_id, source_row_id,"
            " role, message_uid, uid_known, anchor_key, via, review, bound) VALUES ('jarvis:message:R4E:11', now(), "
            "'system', 'withheld by an admin', 'R4E', '11', 'user', 'u-r4e-held', true, 'jarvis:message:R4E:10', "
            "'same message id', 'pending', true)"))
    async with session_scope() as s:
        counts = await identity.repair_rows(s, cause="test")
    async with session_scope() as s:
        a, b = await _row_state(s, fold_a), await _row_state(s, fold_b)
        stone_a, stone_b = await _stone_of(s, "jarvis:message:R4E:30"), await _stone_of(s, "jarvis:message:R4E:31")
    assert a == (True, "") and stone_a == ("", "upgrade: merged into another message", "jarvis:message:R4E:10"), \
        (a, stone_a)
    assert b == (False, "Summary of a held row's own words zqr4efoldheld.") and stone_b is None, (b, stone_b)
    assert counts.get("withheld_by_fold") == 1, counts


@pytest.mark.usefixtures("clean_db")
async def test_r5_blanks_the_rollback_copies_of_rows_it_withholds_for_restricted_data():
    """S25 (2): R5 withholds a readable row left in a conversation withheld for restricted data, and blanks its
    rollback copy in the same step (import_records.before), before any settle() or sweep runs: right after the
    migration's own repair no copy holds the text."""
    mark = "zqr5copy"
    async with session_scope() as s:
        sr = await _r_session(s, "R5C")
        await s.execute(text("UPDATE archive_sessions SET sensitivity = 'restricted' WHERE id = :i"), {"i": sr})
        row = await _r_message(s, sr, "jarvis:message:R5C:4", "4", "user", "Now readable %s new." % mark, pos=1)
        before = {"source_message_id": "4", "source_fp": "", "position": 1, "role": "user",
                  "content": "The earlier text %s old." % mark, "withheld": False, "state": "live"}
        run = (await s.execute(text("INSERT INTO import_runs (status, started_at, finished_at) VALUES ('completed', "
                                    "now() - interval '1 hour', now() - interval '1 hour') RETURNING id"))).scalar()
        await s.execute(text(
            "INSERT INTO import_records (run_id, source_key, record_type, action, reason, target_table, target_id, "
            "before) VALUES (:r, 'jarvis:message:R5C:4', 'message', 'updated', 'changed since an earlier import', "
            "'archive_messages', :t, CAST(:b AS jsonb))"), {"r": run, "t": row, "b": json.dumps(before)})
    async with session_scope() as s:
        counts = await identity.repair_rows(s, cause="test")
    async with session_scope() as s:
        got = await _row_state(s, row)
        copies = (await s.execute(text("SELECT count(*) FROM import_records WHERE before::text LIKE :m"),
                                  {"m": "%" + mark + "%"})).scalar()
        copy = (await s.execute(text("SELECT before FROM import_records WHERE target_id = :t"), {"t": row})).scalar()
    assert got == (True, "") and counts.get("restricted_rows_withheld") == 1, (got, counts)
    assert copies == 0, copies
    copy = json.loads(copy) if isinstance(copy, str) else copy
    assert (copy["content"], copy["withheld"], copy["sensitivity"]) == ("", True, "restricted"), copy


@pytest.mark.usefixtures("clean_db")
async def test_r4b_withholds_the_same_role_and_never_another_role_at_the_row_id():
    """S26 (1): R4b's role check. Under the withheld row's JARVIS row id (session + row 5), a row of the same role is
    the message (a member, withheld at once); a row of another role is another message (a state.db from an older
    backup resets Hermes's id sequence) and stays readable, never a member nobody reviews (owner requirement 2)."""
    async with session_scope() as s:
        sr = await _r_session(s, "R4B")
        await _r_message(s, sr, "jarvis:message:R4B:5", "5", "assistant", "", withheld=True, fp="w1:live", pos=1)
        other = await _r_message(s, sr, "jarvis:message:uid:u-r4b-user", "5", "user",
                                 "A new question zqr4brolefive after a restore.", uid="u-r4b-user", pos=2)
        same = await _r_message(s, sr, "jarvis:message:uid:u-r4b-asst", "5", "assistant",
                                "The withheld answer again zqr4brolecopy.", uid="u-r4b-asst", pos=3)
        await _r_stone(s, "jarvis:message:R4B:5", "R4B", "5", "assistant", "")
    async with session_scope() as s:
        counts = await identity.repair_rows(s, cause="test")
    async with session_scope() as s:
        o, m = await _row_state(s, other), await _row_state(s, same)
        stone_o = await _stone_of(s, "jarvis:message:uid:u-r4b-user")
    assert m == (True, "") and counts.get("withheld_by_jarvis_row") == 1, (m, counts)
    assert o == (False, "A new question zqr4brolefive after a restore.") and stone_o is None, (o, stone_o)


