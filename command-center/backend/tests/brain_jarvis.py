"""The JARVIS installation the importer's tests read: the kit's fixture plus a brain-specific planted set.

migration/jarvis/tests/make_fixture.py is loaded by path and never changed: its built counts are the kit's own tests'
contract. Everything the importer's tests need beyond it is added here, to the test's own copy of the profile and of the
database backup (spec 2.7): the leaked phrasings the design review found, a NUL in a tool result, a 250 KB knowledge
file, a blank-first-line memory entry, carried-on copies of compacted rows, parallel identical tool results, a naive ISO
time, a job that names a staff member, customer tools and websites.

Values that must never reach a brain table are built at run time, so no token-shaped literal sits in the repository
(GitHub's push protection rejects them).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

COMMAND_CENTER = Path(__file__).resolve().parents[2]
MAKE_FIXTURE = COMMAND_CENTER / "migration" / "jarvis" / "tests" / "make_fixture.py"


def _load_kit_fixture() -> Any:
    spec = importlib.util.spec_from_file_location("jarvis_make_fixture", MAKE_FIXTURE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


KIT_FIXTURE = _load_kit_fixture()
KIT_PLANTED: dict[str, str] = KIT_FIXTURE.PLANTED
# Planted kit values that are ordinary conversation or note text: the brain keeps them (names are not detected,
# spec 0.5; the kit plants them to check that its reports, which hold no text at all, never show them).
KIT_CONTENT = ("title", "message", "memory_text", "customer_name", "bad_utf8")
# Names the owner chose (file, skill and job names, a client's slug): the kit allows them in files that stay on the
# owner's PC, as the Command Center's database does; never in a report or on the console.
KIT_DETAIL_ONLY: tuple[str, ...] = tuple(KIT_FIXTURE.DETAIL_ONLY)

# The brain-specific planted values: none may appear anywhere in the brain tables, a report, the console or an error.
BRAIN_PLANTED: dict[str, str] = {
    "root_password": "Xy7!kLp9" + "Planted",
    "as_password": "Qz8#m" + "Planted42",
    "gmail_spaced": " ".join(("pqrs", "tuvw", "lmno", "abcd")),
    "pin": "48" + "2913",
    "url_token": "PlantedReset" + "Tok99",
    "ps_secure": "PlantedPs" + "#77",
    "py_auth": "PlantedAuth" + "55",
    "long_token": "aB3dE5fG7hJ9" + "kL1mN3pQ5rS7" + "tU9vW1xY3zA5",
    "dump": "PlantedDump" + "1",
    "json_password": "PlantedJson" + "Pw1",
    "json_api_key": "PlantedJson" + "Key2",
    "inline": "PlantedInline" + "9",
    "github_token": "gh" + "p_" + "PlantedGithub" + "Token0123456789abcdefABCD",
    "argument_password": "PlantedArg" + "Pw3",
    "lead_email": "lead.person" + "@planted-lead.example",
    "whatsapp_digits": "1555765" + "4321",
    "e164": "+44 20 " + "7946 0958",
    "think": "PLANTED-THINK-" + "TWO",
    "dob": "04/12/" + "1980",
}
# Planted values that may be stored as conversation text (each sits next to a planted secret in the same message).
BRAIN_CONTENT = {"nul_message": "line one before the NUL", "nul_tail": "after the NUL"}

# Sessions added to the copy of the backup.
S_LEAKS = "20261101_0101"
S_CARRIED = "20261102_0102"
S_SARAH = "cron_s4r4h1_20261103_080000"
S_RESTRICTED_ARG = "20261104_0104"
S_TIMES = "20261105_0105"
SARAH_JOB = {"id": "s4r4h1", "name": "triage-inbox", "prompt": "Triage the support inbox and draft replies.",
             "skills": ["triage-inbox"], "schedule": {"kind": "cron", "expr": "0 * * * *"}, "enabled": False,
             "deliver": "slack:C0PLANTED"}
SITE_REGISTERED = "https://www.planted-site-one.example"
SITE_NEW = "https://planted-site-two.example/shop"
BIG_FILE = "knowledge/big-notes.md"


def never_stored() -> dict[str, str]:
    """Every planted value that must not appear in any brain table (masked or redacted forms do not count)."""
    out = {k: v for k, v in KIT_PLANTED.items() if k not in KIT_CONTENT and k not in KIT_DETAIL_ONLY}
    out.update({"brain_" + k: v for k, v in BRAIN_PLANTED.items()})
    return out


def never_shown() -> dict[str, str]:
    """Every planted value that must not appear on the console, in a report or in an error."""
    out = dict(KIT_PLANTED)
    out.update({"brain_" + k: v for k, v in BRAIN_PLANTED.items()})
    return out


def build(root: str | os.PathLike[str]) -> dict[str, str]:
    """The kit's fixture under root, then the brain-specific additions to this copy."""
    fx = KIT_FIXTURE.build(str(root))
    add_profile_files(fx)
    add_sessions(fx["db"])
    return fx


def copy(fx: dict[str, str], dest: str | os.PathLike[str]) -> dict[str, str]:
    """A separate copy of a built fixture (links kept as links), for tests that change their sources."""
    shutil.copytree(fx["root"], str(dest), symlinks=True)
    return {k: v.replace(fx["root"], str(dest), 1) for k, v in fx.items()}


def _write(path: str, data: str | bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data.encode("utf-8") if isinstance(data, str) else data)


def _append(path: str, data: str) -> None:
    with open(path, "ab") as f:
        f.write(data.encode("utf-8"))


def add_profile_files(fx: dict[str, str]) -> None:
    prof = fx["profile"]
    # A knowledge file larger than an item's body may be (store.MAX_BODY): kept, cut, flagged.
    line = "Routine line about how the agency onboards a new website client, step by step.\n"
    _write(os.path.join(prof, BIG_FILE), "# Big notes\n" + line * (250 * 1024 // len(line)))
    # A memory entry whose first line is blank (its title comes from the first line with text).
    _append(os.path.join(prof, "memories", "MEMORY.md"), "§\n\n\nInvoices go out on the first working day.\n")
    # A role block in the form setup writes (the kit's fixture has an unterminated marker only).
    _append(os.path.join(prof, "operations.md"),
            "<!-- jarvis-setup:role:scout:start -->\nScout researches keywords every Monday.\n"
            "<!-- jarvis-setup:role:scout:end -->\n")
    _write(os.path.join(prof, "knowledge", "websites.md"),
           "# Websites JARVIS watches\n- %s/ (main site)\n- %s?utm_source=x\n" % (SITE_REGISTERED, SITE_NEW))
    _write(os.path.join(prof, "workspace", "reports", "scout-2026-10-05.md"), "Keyword sweep done for October.\n")
    jobs_path = os.path.join(prof, "cron", "jobs.json")
    with open(jobs_path, encoding="utf-8") as f:
        jobs = json.load(f)
    jobs["jobs"].append(SARAH_JOB)
    _write(jobs_path, json.dumps(jobs, indent=2))


def add_sessions(db: str) -> None:
    """Rows added to this copy of the backup (the kit's rows are left as they are)."""
    p = BRAIN_PLANTED
    con = sqlite3.connect(db)
    base = 1793520000.0  # 2026-11-01

    def session(sid: str, started: Any, source: str = "cli", title: str = "Planted brain session") -> None:
        con.execute("INSERT INTO sessions (id, source, model, model_config, system_prompt, started_at, ended_at, "
                    "end_reason, message_count, tool_call_count, title, archived) VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
                    (sid, source, "claude-opus-5", "{}", "PLANTED-SYSTEM-PROMPT", started, None, "completed", 0, 0,
                     title))

    def message(sid: str, role: str, content: Any, ts: Any, *, tool_calls: str | None = None,
                tool_name: str | None = None, tool_call_id: str | None = None, active: int = 1,
                compacted: int = 0) -> None:
        cur = con.execute(
            "INSERT INTO messages (session_id, role, content, tool_calls, tool_name, tool_call_id, timestamp, "
            "reasoning, active, compacted) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, role, content, tool_calls, tool_name, tool_call_id, ts, "PLANTED-REASONING-MARKER", active,
             compacted))
        uid = hashlib.sha256(("%s:%d" % (sid, cur.lastrowid)).encode()).hexdigest()[:32]
        con.execute("UPDATE messages SET message_uid = ? WHERE id = ?", (uid, cur.lastrowid))

    # The review's leaked phrasings, customer data, a NUL, parallel identical tool results, adjacent duplicates.
    session(S_LEAKS, base)
    t = base
    leaked = [
        "the root password is %s for the VPS" % p["root_password"],
        "ok, use %s as the new password" % p["as_password"],
        "Gmail app password: %s" % p["gmail_spaced"],
        "your PIN is %s" % p["pin"],
        "open https://portal.example.test/reset?token=%s now" % p["url_token"],
        "$p = ConvertTo-SecureString '%s' -AsPlainText -Force" % p["ps_secure"],
        "requests.get(url, auth=('admin', '%s'))" % p["py_auth"],
        "the token %s works" % p["long_token"],
        "set DB_PASSWORD=%s in the env file" % p["inline"],
        "the deploy token is %s" % p["github_token"],
        "Lead %s wrote on WhatsApp from %s and called %s"
        % (p["lead_email"], p["whatsapp_digits"] + "@s.whatsapp.net", p["e164"]),
        "<think>%s</think>Let us go ahead and publish it." % p["think"],
    ]
    for n, text in enumerate(leaked):
        t += 1
        message(S_LEAKS, "user" if n % 2 == 0 else "assistant", text, t)
    t += 1
    message(S_LEAKS, "user", "DB_HOST=db.local\nDB_USER=app\nDB_PASSWORD=%s\n" % p["dump"], t)
    calls = json.dumps([
        {"id": "call_1", "type": "function",
         "function": {"name": "http_get", "arguments": json.dumps({"url": "https://example.test/status"})}},
        {"id": "call_2", "type": "function",
         "function": {"name": "http_get", "arguments": json.dumps({"url": "https://example.test/status",
                                                                    "password": p["argument_password"]})}}])
    message(S_LEAKS, "assistant", "", t + 1, tool_calls=calls)
    # Two identical results of two parallel calls: tool results are never de-duplicated.
    message(S_LEAKS, "tool", "HTTP 200 OK", t + 2, tool_name="http_get", tool_call_id="call_1")
    message(S_LEAKS, "tool", "HTTP 200 OK", t + 3, tool_name="http_get", tool_call_id="call_2")
    message(S_LEAKS, "tool", json.dumps({"user": "x", "password": p["json_password"],
                                         "nested": {"apiKey": p["json_api_key"]}}), t + 4, tool_name="read_config")
    message(S_LEAKS, "tool", "%s\x00%s" % (BRAIN_CONTENT["nul_message"], BRAIN_CONTENT["nul_tail"]), t + 5,
            tool_name="terminal")
    # A customer tool (flat tool-call shape) and its result: customer data by default.
    message(S_LEAKS, "assistant", "", t + 6,
            tool_calls=json.dumps([{"name": "gmail_send_message", "arguments": {"subject": "Your quote"}}]))
    message(S_LEAKS, "tool", "sent", t + 7, tool_name="gmail_send_message")
    # Adjacent duplicates of a user row; a system row; an empty row.
    message(S_LEAKS, "user", "Please send the weekly report.", t + 8)
    message(S_LEAKS, "user", "Please send the weekly report.", t + 9)
    message(S_LEAKS, "system", "PLANTED-SYSTEM-PROMPT", t + 10)
    message(S_LEAKS, "assistant", "", t + 11)

    # Hermes compaction: originals archived (active=0, compacted=1), a live carried-on copy of one, the summary.
    session(S_CARRIED, base + 86400)
    t = base + 86400
    message(S_CARRIED, "user", "Draft the October newsletter.", t + 1, active=0, compacted=1)
    message(S_CARRIED, "assistant", "I drafted the October newsletter.", t + 2, active=0, compacted=1)
    message(S_CARRIED, "user", "Draft the October newsletter.", t + 3)
    message(S_CARRIED, "assistant", "Summary of earlier turns: the newsletter was drafted.", t + 4)

    # Sarah's scheduled job (its id names the template job triage-inbox): customer data by default.
    session(S_SARAH, base + 2 * 86400, source="cron", title="Inbox triage")
    t = base + 2 * 86400
    message(S_SARAH, "user", "Triage the support inbox.", t + 1)
    message(S_SARAH, "assistant", "Two new tickets, both answered.", t + 2)

    # A restricted value inside a tool call's arguments withholds the whole session.
    session(S_RESTRICTED_ARG, base + 3 * 86400)
    t = base + 3 * 86400
    message(S_RESTRICTED_ARG, "user", "Fill in the onboarding form for the new client.", t + 1)
    message(S_RESTRICTED_ARG, "assistant", "", t + 2, tool_calls=json.dumps(
        [{"name": "fill_form", "arguments": {"field": "date of birth", "value": p["dob"]}}]))

    # Times as JARVIS has stored them: naive ISO text (customer time zone), milliseconds, microseconds, rubbish.
    session(S_TIMES, "2026-11-05 09:30:00")
    message(S_TIMES, "user", "What changed on the shop site?", "2026-11-05T09:31:00")
    message(S_TIMES, "assistant", "The shop site was updated.", (base + 4 * 86400) * 1000)
    message(S_TIMES, "user", "Thanks.", (base + 4 * 86400 + 60) * 1_000_000)
    message(S_TIMES, "assistant", "You are welcome.", "not a time")
    con.commit()
    con.execute("PRAGMA journal_mode = DELETE")
    con.close()


def sqlite(db: str) -> sqlite3.Connection:
    """The test's own copy of the backup, opened for writing (to change it between two imports)."""
    return sqlite3.connect(db)


# --- Database helpers shared by the importer's integration tests --------------------------------------------------

BRAIN_TABLES = ("import_runs", "import_records", "memory_items", "memory_item_versions", "archive_sessions",
                "archive_messages", "memory_conflicts", "workflow_drafts", "brain_tombstones")
# Kept by a rollback, on purpose (brain_maintenance: the identity repair's done-markers, 0008).
HISTORY_TABLES = ("import_runs", "import_records", "audit_log", "brain_maintenance")


def paths(fx: dict[str, str], **kw: Any) -> Any:
    from app.brain.importer import SourcePaths

    return SourcePaths(db=fx["db"], profile=fx["profile"], content=fx["content"], **kw)


async def settle(db: str | dict[str, str], actor: str = "admin@test.local", *, keep: bool = True) -> dict[str, Any]:
    """brain-import settle --backup <db> (0.6.2 plan 4.1): the withholds taken on this JARVIS backup finished, as the
    test copy does before decisions-export (which refuses until then: plan 4.7). With keep, every hold it found is
    then kept (an admin's review that keeps them all), so an export can follow."""
    from sqlalchemy import select

    from app.brain import identity, review
    from app.brain.importer import SourcePaths, settle as settle_backup
    from app.db import session_scope
    from app.models import ArchiveMessage, BrainTombstone

    out = await settle_backup(SourcePaths(db=db["db"] if isinstance(db, dict) else db, profile=""), actor=actor)
    if keep:
        async with session_scope() as s:
            for message in (await s.scalars(select(ArchiveMessage).join(
                    BrainTombstone, BrainTombstone.source_key == ArchiveMessage.source_key).where(
                    BrainTombstone.review == identity.PENDING, BrainTombstone.via == identity.SETTLE_HOLD,
                    ArchiveMessage.withheld.is_(True)))).all():
                await review.keep_hold(s, message, actor=actor)
            await s.commit()
    return out


async def table_counts(exclude: tuple[str, ...] = ()) -> dict[str, int]:
    """Rows in every table of the Command Center's database."""
    from sqlalchemy import text

    from app.db import session_scope

    async with session_scope() as s:
        tables = [r[0] for r in await s.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"))]
        return {t: int(await s.scalar(text('SELECT count(*) FROM "%s"' % t)) or 0) for t in tables if t not in exclude}


async def stored_text(tables: tuple[str, ...] = BRAIN_TABLES) -> dict[str, str]:
    """Every row of the given tables as JSON text (all text, array and JSONB columns; the search vectors left out)."""
    from sqlalchemy import text

    from app.db import session_scope

    out = {}
    async with session_scope() as s:
        for t in tables:
            rows = (await s.execute(text("SELECT (to_jsonb(x) - 'search')::text FROM %s x ORDER BY 1" % t))).scalars()
            out[t] = "\n".join(rows)
    return out


def planted_in(text: str, values: dict[str, str]) -> list[str]:
    """The names of the planted values found in text, as written or as they read inside JSON."""
    return sorted(k for k, v in values.items() if v in text or json.dumps(v)[1:-1] in text)


def snapshot(root: str) -> dict[str, tuple[Any, ...]]:
    """Every file and link under root: size, modification time and SHA-256 (links: their target)."""
    state: dict[str, tuple[Any, ...]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            if os.path.islink(full):
                state[full] = ("link", os.readlink(full))
            elif os.path.isfile(full):
                with open(full, "rb") as f:
                    state[full] = (st.st_size, st.st_mtime_ns, hashlib.sha256(f.read()).hexdigest())
    return state


# --- Small JARVIS installations in the shapes Hermes writes (0.6.2 withhold identity tests) ------------------------

# The kit's backup schema (migration/jarvis/tests/make_fixture.py), without its full-text index: rows are added and
# changed by the helpers below exactly as Hermes changes them.
JARVIS_SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, user_id TEXT, model TEXT, model_config TEXT,
    system_prompt TEXT, parent_session_id TEXT, started_at REAL, ended_at REAL, end_reason TEXT,
    message_count INTEGER, tool_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,
    title TEXT, display_name TEXT, archived INTEGER DEFAULT 0, estimated_cost_usd REAL, actual_cost_usd REAL);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL,
    token_count INTEGER, finish_reason TEXT, reasoning TEXT, reasoning_details TEXT,
    active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0, message_uid TEXT, tool_call_uids TEXT);
CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
"""


def mini(root: str | os.PathLike[str]) -> dict[str, str]:
    """An installation with no files and an empty backup: the tests add the conversations they need."""
    root = str(root)
    out = {"root": root, "db": os.path.join(root, "state-backup.db"), "profile": os.path.join(root, "profile"),
           "content": os.path.join(root, "content")}
    os.makedirs(out["profile"], exist_ok=True)
    os.makedirs(out["content"], exist_ok=True)
    con = sqlite3.connect(out["db"])
    con.executescript(JARVIS_SCHEMA)
    con.commit()
    con.close()
    return out


def _write_db(db: str, fn: Any) -> Any:
    con = sqlite3.connect(db)
    try:
        result = fn(con)
        con.commit()
        con.execute("PRAGMA journal_mode = DELETE")
        return result
    finally:
        con.close()


def new_uid() -> str:
    """A Hermes message_uid (uuid4 hex)."""
    import uuid

    return uuid.uuid4().hex


def add_session(db: str, sid: str, *, started: float, parent: str | None = None, end_reason: str = "completed",
                model_config: dict[str, Any] | None = None, title: str = "Probe conversation",
                source: str = "cli") -> None:
    _write_db(db, lambda con: con.execute(
        "INSERT INTO sessions (id, source, model, model_config, system_prompt, parent_session_id, started_at, "
        "ended_at, end_reason, message_count, tool_call_count, title, archived) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
        (sid, source, "claude-opus-5", json.dumps(model_config or {}), "sys", parent, started, None, end_reason, 0, 0,
         title)))


def add_row(db: str, sid: str, role: str, content: str, ts: float, *, uid: str | None | bool = None,
            active: int = 1, compacted: int = 0, tool_call_id: str | None = None, tool_name: str | None = None
            ) -> int:
    """One message row; uid None mints one (Hermes stamps every row), False leaves it NULL (or out: a backup of a
    Hermes older than message_uid has no such column)."""
    value = new_uid() if uid is None else (None if uid is False else uid)

    def insert(con: sqlite3.Connection) -> int:
        cols = ["session_id", "role", "content", "tool_call_id", "tool_name", "timestamp", "active", "compacted"]
        values = [sid, role, content, tool_call_id, tool_name, ts, active, compacted]
        if "message_uid" in {r[1] for r in con.execute("PRAGMA table_info(messages)")}:
            cols.append("message_uid")
            values.append(value)
        return con.execute("INSERT INTO messages (%s) VALUES (%s)" % (", ".join(cols), ",".join("?" * len(cols))),
                           values).lastrowid
    return _write_db(db, insert)


def rows(db: str, sid: str) -> list[dict[str, Any]]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
        uid = "message_uid" if "message_uid" in cols else "NULL AS message_uid"
        return [dict(r) for r in con.execute(
            "SELECT id, role, content, tool_call_id, tool_name, timestamp, active, compacted, %s FROM messages "
            "WHERE session_id = ? ORDER BY id" % uid, (sid,))]
    finally:
        con.close()


def change(db: str, sql: str, *args: Any) -> None:
    _write_db(db, lambda con: con.execute(sql, args))


def hermes_compact(db: str, sid: str, head_n: int, tail_n: int, ts: float) -> list[int]:
    """Hermes's in-place compaction (hermes_state_messages._archive_and_compact_on_conn, watermark None): the tail
    originals are rewind-flagged (active 0, compacted 0), the rest archived (compacted 1), then copies of the first
    head_n archived rows, the summary and copies of the tail are inserted as live rows with the originals' uids.
    Returns the new rows' ids."""
    def run(con: sqlite3.Connection) -> list[int]:
        live = con.execute("SELECT id, role, content, message_uid, tool_call_id, tool_name FROM messages "
                           "WHERE session_id = ? AND active = 1 AND role != 'system' ORDER BY id", (sid,)).fetchall()
        head, tail = live[:head_n], (live[-tail_n:] if tail_n else [])
        for r in tail:
            con.execute("UPDATE messages SET active = 0, compacted = 0 WHERE id = ?", (r[0],))
        con.execute("UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ? AND active = 1", (sid,))
        new = []
        insert = ("INSERT INTO messages (session_id, role, content, message_uid, tool_call_id, tool_name, timestamp) "
                  "VALUES (?,?,?,?,?,?,?)")
        for _, role, content, uid, call_id, name in head:
            new.append(con.execute(insert, (sid, role, content, uid, call_id, name, ts)).lastrowid)
        new.append(con.execute(insert, (sid, "assistant", "[CONTEXT COMPACTION] Summary of earlier turns.", new_uid(),
                                        None, None, ts + 1)).lastrowid)
        for _, role, content, uid, call_id, name in tail:
            new.append(con.execute(insert, (sid, role, content, uid, call_id, name, ts + 2)).lastrowid)
        return new
    return _write_db(db, run)


def _copy_into(db: str, parent: str, child: str, *, fresh_uid: bool) -> list[int]:
    def run(con: sqlite3.Connection) -> list[int]:
        out = []
        for role, content, uid, call_id, name, ts in con.execute(
                "SELECT role, content, message_uid, tool_call_id, tool_name, timestamp FROM messages "
                "WHERE session_id = ? AND active = 1 AND role != 'system' ORDER BY id", (parent,)).fetchall():
            out.append(con.execute(
                "INSERT INTO messages (session_id, role, content, message_uid, tool_call_id, tool_name, timestamp) "
                "VALUES (?,?,?,?,?,?,?)",
                (child, role, content, new_uid() if fresh_uid else uid, call_id, name, ts)).lastrowid)
        return out
    return _write_db(db, run)


def gateway_branch(db: str, parent: str, child: str, started_at: float) -> list[int]:
    """The gateway's /branch (gateway/slash_commands_session.py _branch_row): the child copies the parent's live rows
    with their timestamps and a fresh uid each; model_config names the parent."""
    add_session(db, child, started=started_at, parent=parent, model_config={"_branched_from": parent},
                title="Branch of the probe")
    return _copy_into(db, parent, child, fresh_uid=True)


def cli_branch(db: str, parent: str, child: str, started_at: float) -> list[int]:
    """The CLI's /branch: the parent ends 'branched', the child copies its rows keeping their uids."""
    change(db, "UPDATE sessions SET end_reason = 'branched' WHERE id = ?", parent)
    add_session(db, child, started=started_at, parent=parent, model_config={"_branched_from": parent},
                title="CLI branch of the probe")
    return _copy_into(db, parent, child, fresh_uid=False)


def rotate(db: str, parent: str, child: str, started_at: float) -> list[int]:
    """A rotating compaction (in_place False): the parent ends 'compression' and the continuation child carries its
    rows on with their uids."""
    change(db, "UPDATE sessions SET end_reason = 'compression' WHERE id = ?", parent)
    add_session(db, child, started=started_at, parent=parent, title="Continuation of the probe")
    return _copy_into(db, parent, child, fresh_uid=False)


def fold(db: str, sid: str, survivor: int, dropped: list[int]) -> None:
    """Hermes's micro-compaction fold: the survivor's text becomes the composite, it records the uids it absorbed
    (absorbed_message_uids, a JSON list), and the folded rows are removed."""
    def run(con: sqlite3.Connection) -> None:
        if "absorbed_message_uids" not in {r[1] for r in con.execute("PRAGMA table_info(messages)")}:
            con.execute("ALTER TABLE messages ADD COLUMN absorbed_message_uids TEXT")
        parts = [con.execute("SELECT content, message_uid FROM messages WHERE id = ?", (i,)).fetchone()
                 for i in dropped]
        text = con.execute("SELECT content FROM messages WHERE id = ?", (survivor,)).fetchone()[0]
        con.execute("UPDATE messages SET content = ?, absorbed_message_uids = ? WHERE id = ?",
                    ("\n".join([p[0] for p in parts] + [text]), json.dumps([p[1] for p in parts]), survivor))
        con.execute("DELETE FROM messages WHERE id IN (%s)" % ",".join("?" * len(dropped)), dropped)
    _write_db(db, run)


def prune_twin(db: str, sid: str, row: int, stub: str = "[tool output pruned to save context]") -> int:
    """Pruning: the full row is archived as a compacted twin with the same uid, the live row keeps a stub."""
    def run(con: sqlite3.Connection) -> int:
        role, content, uid, call_id, name, ts = con.execute(
            "SELECT role, content, message_uid, tool_call_id, tool_name, timestamp FROM messages WHERE id = ?",
            (row,)).fetchone()
        twin = con.execute("INSERT INTO messages (session_id, role, content, message_uid, tool_call_id, tool_name, "
                           "timestamp, active, compacted) VALUES (?,?,?,?,?,?,?,0,1)",
                           (sid, role, content, uid, call_id, name, ts)).lastrowid
        con.execute("UPDATE messages SET content = ? WHERE id = ?", (stub, row))
        return twin
    return _write_db(db, run)


def replace_renumber(db: str, sid: str) -> dict[int, int]:
    """Hermes's replace_messages: every row of the session deleted and written again, new ids, the same uids.
    Returns old id -> new id."""
    def run(con: sqlite3.Connection) -> dict[int, int]:
        old = con.execute("SELECT id, role, content, message_uid, tool_call_id, tool_name, timestamp, active, "
                          "compacted FROM messages WHERE session_id = ? ORDER BY id", (sid,)).fetchall()
        con.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
        out = {}
        for r in old:
            out[r[0]] = con.execute(
                "INSERT INTO messages (session_id, role, content, message_uid, tool_call_id, tool_name, timestamp, "
                "active, compacted) VALUES (?,?,?,?,?,?,?,?,?)", (sid, *r[1:])).lastrowid
        return out
    return _write_db(db, run)


def backfill_uids(db: str, sid: str, n: int | None = None) -> dict[int, str]:
    """Hermes's uid backfill: rows with no message_uid get one (the first n, or all). Returns id -> uid."""
    def run(con: sqlite3.Connection) -> dict[int, str]:
        cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
        if "message_uid" not in cols:
            con.execute("ALTER TABLE messages ADD COLUMN message_uid TEXT")
        ids = [r[0] for r in con.execute("SELECT id FROM messages WHERE session_id = ? AND message_uid IS NULL "
                                         "ORDER BY id", (sid,))]
        out = {i: new_uid() for i in (ids if n is None else ids[:n])}
        for i, uid in out.items():
            con.execute("UPDATE messages SET message_uid = ? WHERE id = ?", (uid, i))
        return out
    return _write_db(db, run)


def drop_uid_column(db: str) -> None:
    """A backup of a Hermes older than message_uid."""
    _write_db(db, lambda con: con.execute("ALTER TABLE messages DROP COLUMN message_uid"))


async def nowhere(marker: str, allow: tuple[str, ...] = (), caplog: Any = None) -> list[str]:
    """Every table.column (text, varchar, JSONB, arrays and search vectors) holding the marker, except `allow`, and
    "log" when an application log line holds it."""
    from sqlalchemy import text

    from app.db import session_scope

    found = []
    async with session_scope() as s:
        columns = (await s.execute(text(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' AND "
            "data_type IN ('text', 'character varying', 'jsonb', 'json', 'ARRAY', 'tsvector') "
            "ORDER BY table_name, column_name"))).all()
        for table, column in columns:
            where = "%s.%s" % (table, column)
            if where not in allow and await s.scalar(text(
                    'SELECT EXISTS (SELECT 1 FROM "%s" WHERE "%s"::text ILIKE :m)' % (table, column)),
                    {"m": "%" + marker + "%"}):
                found.append(where)
    if caplog is not None and any(marker.lower() in r.getMessage().lower() for r in caplog.records
                                  if not r.name.startswith("httpx")):  # the test client logs its own URLs
        found.append("log")
    return found
