"""Builds tests/fixtures/golden_state.db: a small JARVIS state.db in the shapes Hermes writes, whose message keys,
lineage and markers tests/fixtures/golden_keys.json records (0.6.2 plan 2.3: key derivation is a tested contract).

Synthetic data only. Deterministic: fixed uids, times and ids. Run it only to change the contract on purpose:

    python tests/fixtures/make_golden_state.py tests/fixtures/golden_state.db tests/fixtures/golden_keys.json

then review the diff of golden_keys.json (tests/unit/test_brain_golden_keys.py compares both files with the importer).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

# Hermes's columns as the kit's backup schema lists them (tests/brain_jarvis.JARVIS_SCHEMA), with
# absorbed_message_uids (a fold's absorbed uids) and no full-text index.
SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, user_id TEXT, model TEXT, model_config TEXT,
    system_prompt TEXT, parent_session_id TEXT, started_at REAL, ended_at REAL, end_reason TEXT,
    message_count INTEGER, tool_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,
    title TEXT, display_name TEXT, archived INTEGER DEFAULT 0, estimated_cost_usd REAL, actual_cost_usd REAL);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL,
    token_count INTEGER, finish_reason TEXT, reasoning TEXT, reasoning_details TEXT,
    active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0, message_uid TEXT, tool_call_uids TEXT,
    absorbed_message_uids TEXT);
"""

T0 = 1782864000.0  # 2026-07-01T00:00:00Z


def uid(n: int) -> str:
    return "%032x" % (0x60200000000000000000000000000000 + n)


SESSIONS = [
    # id, parent, end_reason, model_config, started offset, title, source
    ("root", None, "compression", {}, 0, "Golden root", "cli"),
    ("rot", None, "completed", {"_compressed_from": "root-deleted"}, 100, "Golden root", "cli"),  # orphaned rotation
    ("cont", "root", "completed", {}, 200, "Golden root", "cli"),  # an old continuation: the parent's end_reason only
    ("gwb", "root", "completed", {"_branched_from": "root"}, 300, "Golden root #2", "slack"),
    ("reset", "root", "completed", {"_reset_from": "root"}, 400, "Golden reset", "telegram"),
    ("deleg", "root", "completed", {"_delegate_from": "root"}, 500, "Golden helper", "cli"),
    ("slack:C123:thread:9", None, "completed", {}, 600, "Golden thread", "slack"),
]

MESSAGES = [
    # session, role, content, tool_call_id, tool_name, timestamp, active, compacted, uid, absorbed
    ("root", "user", "golden question one", None, None, T0 + 1, 1, 0, uid(1), None),
    ("root", "assistant", "golden answer one", None, None, T0 + 2, 1, 0, uid(2), None),
    ("root", "assistant", "", "call_g1", None, T0 + 3, 1, 0, uid(3), None),
    ("root", "tool", "golden tool result", "call_g1", "terminal", T0 + 4, 1, 0, uid(4), None),
    ("root", "user", "golden row without a uid", None, None, T0 + 5, 1, 0, None, None),
    ("root", "user", "golden row with an empty uid", None, None, T0 + 6, 1, 0, "", None),
    ("root", "assistant", "golden archived generation", None, None, T0 + 2, 0, 1, uid(2), None),  # same uid as row 2
    ("root", "assistant", "golden fold survivor", None, None, T0 + 7, 1, 0, uid(7),
     json.dumps([uid(8), uid(9)])),
    ("root", "user", "golden millisecond time", None, None, (T0 + 8) * 1000, 1, 0, uid(10), None),
    ("rot", "user", "golden question one", None, None, T0 + 1, 1, 0, uid(1), None),  # a rotation keeps uid and time
    ("rot", "user", "golden rotation turn", None, None, T0 + 101, 1, 0, uid(11), None),
    ("cont", "user", "golden continuation turn", None, None, T0 + 201, 1, 0, None, None),
    ("gwb", "user", "golden question one", None, None, T0 + 1, 1, 0, uid(12), None),  # a gateway branch: fresh uid
    ("gwb", "assistant", "golden branch reply", None, None, T0 + 301, 1, 0, uid(13), None),
    ("reset", "user", "golden fresh start", None, None, T0 + 401, 1, 0, uid(14), None),
    ("deleg", "user", "golden delegated task", None, None, T0 + 501, 1, 0, uid(15), None),
    ("slack:C123:thread:9", "user", "golden thread message", None, None, T0 + 601, 1, 0, None, None),
    ("slack:C123:thread:9", "assistant", "golden thread reply", None, None, T0 + 602, 1, 0, uid(16), None),
    ("gone", "user", "golden orphan row", None, None, T0 + 700, 1, 0, uid(17), None),  # its session is not here
]


def build(db: str) -> None:
    if os.path.exists(db):
        os.remove(db)
    con = sqlite3.connect(db)
    try:
        con.executescript(SCHEMA)
        for sid, parent, end_reason, config, offset, title, source in SESSIONS:
            con.execute("INSERT INTO sessions (id, source, model, model_config, system_prompt, parent_session_id, "
                        "started_at, ended_at, end_reason, message_count, tool_call_count, title, archived) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
                        (sid, source, "golden-model", json.dumps(config), "sys", parent, T0 + offset, None,
                         end_reason, 0, 0, title))
        for sid, role, content, call, tool, ts, active, compacted, u, absorbed in MESSAGES:
            con.execute("INSERT INTO messages (session_id, role, content, tool_call_id, tool_name, timestamp, active, "
                        "compacted, message_uid, absorbed_message_uids) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (sid, role, content, call, tool, ts, active, compacted, u, absorbed))
        con.commit()
        con.execute("PRAGMA journal_mode = DELETE")
    finally:
        con.close()


def derive(db: str) -> dict:
    """What the importer reads from the backup: every session's lineage, parent and markers, and every row's key,
    uid, absorbed uids, role, tool_call_id and time."""
    from zoneinfo import ZoneInfo

    from app.brain import jarvis_kit as kit
    from app.brain import source

    backup = source.Backup(db, ZoneInfo("UTC"), {}, kit.load_templates()["cron_jobs"])
    try:
        sessions = {s.id: {"lineage": s.lineage, "parent": s.parent_id, "markers": s.markers,
                           "hermes_parent": s.hermes_parent}
                    for s in backup.sessions()}
        messages = []
        for s in backup.sessions():
            for m in backup.messages(s.id):
                messages.append({"session": s.id, "row": str(m.id), "key": m.key, "uid": m.uid,
                                 "absorbed": list(m.absorbed), "role": m.role, "tool_call_id": m.tool_call_id,
                                 "at": m.at.isoformat() if m.at else None})
        orphans = [{"session": m.session_id, "row": str(m.id), "key": m.key} for m in backup.orphans()]
        newest = backup.newest_at
    finally:
        backup.close()
    return {"sessions": sessions, "messages": messages, "orphans": orphans,
            "newest_at": newest.isoformat() if newest else None}


if __name__ == "__main__":
    db_path, keys_path = sys.argv[1], sys.argv[2]
    build(db_path)
    with open(keys_path, "w", encoding="utf-8") as f:
        json.dump(derive(db_path), f, indent=2, sort_keys=True)
        f.write("\n")
