"""Reading the JARVIS sources (app/brain/source.py) and what each record becomes (app/brain/mapping.py): no database."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.brain import mapping, source
from app.brain import jarvis_kit as kit

NY = ZoneInfo("America/New_York")
TEMPLATE_JOBS = kit.load_templates()["cron_jobs"]


# --- Times --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value, expected", [
    (1782864000, datetime(2026, 7, 1, tzinfo=UTC)),                      # seconds
    (1782864000.5, datetime(2026, 7, 1, 0, 0, 0, 500000, tzinfo=UTC)),
    ("1782864000", datetime(2026, 7, 1, tzinfo=UTC)),                    # numeric text
    (1782864000123, datetime(2026, 7, 1, 0, 0, 0, 123000, tzinfo=UTC)),   # milliseconds
    ("1782864000123", datetime(2026, 7, 1, 0, 0, 0, 123000, tzinfo=UTC)),
    (1782864000123456, datetime(2026, 7, 1, 0, 0, 0, 123456, tzinfo=UTC)),  # microseconds
    ("2026-07-01T10:00:00+02:00", datetime(2026, 7, 1, 8, tzinfo=UTC)),  # ISO with a zone
    ("2026-07-01T08:00:00Z", datetime(2026, 7, 1, 8, tzinfo=UTC)),
])
def test_times_are_read_in_every_shape_hermes_stored(value, expected):
    assert source.parse_time(value, NY) == (expected, False)


def test_naive_iso_text_is_the_customer_time_zone_and_marked_assumed():
    assert source.parse_time("2026-11-05 09:30:00", NY) == (datetime(2026, 11, 5, 14, 30, tzinfo=UTC), True)
    assert source.parse_time("2026-07-01T09:30:00", NY) == (datetime(2026, 7, 1, 13, 30, tzinfo=UTC), True)


@pytest.mark.parametrize("value", [None, "", "  ", "not a time", "2026-13-45", True, -5, 1e17, 3e18, "1e20",
                                   float("nan"), b"\xff\xfe", [1], {"t": 1}])
def test_anything_else_is_no_time(value):
    assert source.parse_time(value, NY) == (None, False)


# --- Tool calls ---------------------------------------------------------------------------------------------------


def test_tool_calls_in_the_openai_shape():
    raw = json.dumps([{"id": "call_1", "type": "function",
                       "function": {"name": "web_search", "arguments": "{\"q\": \"pricing\"}"}}])
    assert source.parse_tool_calls(raw) == [{"name": "web_search", "arguments": "{\"q\": \"pricing\"}"}]


def test_tool_calls_in_the_flat_shape_and_a_single_call():
    assert source.parse_tool_calls(json.dumps([{"name": "terminal", "arguments": {"cmd": "ls"}}])) == [
        {"name": "terminal", "arguments": {"cmd": "ls"}}]
    assert source.parse_tool_calls(json.dumps({"name": "terminal", "args": "ls"})) == [
        {"name": "terminal", "arguments": "ls"}]
    assert source.parse_tool_calls(b'[{"name": "x"}]') == [{"name": "x", "arguments": ""}]


@pytest.mark.parametrize("raw", [None, "", "[]", "null", "{}", "  ", "42", json.dumps(["not a call", 3])])
def test_no_tool_calls(raw):
    assert source.parse_tool_calls(raw) == []


def test_tool_calls_that_are_not_json_are_kept_as_text_arguments():
    assert source.parse_tool_calls("terminal(ls -la)") == [{"name": "", "arguments": "terminal(ls -la)"}]
    assert source.parse_tool_calls(json.dumps([{"name": 5, "arguments": "x"}])) == [{"name": "", "arguments": "x"}]


def test_customer_tools():
    for name in ("gmail_send_message", "mcp_whatsapp_send", "smtp", "crm_update_lead", "send_proposal", "SMS"):
        assert source.customer_tool(name)
    for name in ("", "terminal", "web_search", "read_file"):
        assert not source.customer_tool(name)


# --- Jobs and staff attribution -----------------------------------------------------------------------------------


JOBS_JSON = json.dumps({"jobs": [
    {"id": "a1b2c3", "name": "morning-brief", "prompt": "Brief.", "skills": ["morning-brief"],
     "schedule": {"kind": "cron", "expr": "0 8 * * *", "display": "every day at 8"}, "enabled": True,
     "deliver": "whatsapp:+15552013344"},
    {"id": "bad id with spaces", "name": "own-job", "schedule": "*/5 * * * *", "enabled": "yes"},
    "not a job",
    {"name": "triage-inbox", "schedule_expr": "0 * * * *", "deliver": 7},
]})


def test_jobs_are_read_from_both_shapes():
    jobs = source.read_jobs(JOBS_JSON, TEMPLATE_JOBS)
    assert [j.key for j in jobs] == ["jarvis:job:a1b2c3", "jarvis:job:name:own-job#1",
                                     "jarvis:job:name:triage-inbox#2"]
    assert jobs[0].deliver == "whatsapp" and jobs[0].expr == "0 8 * * *" and jobs[0].display == "every day at 8"
    assert jobs[1].id is None and jobs[1].expr == "*/5 * * * *" and jobs[1].enabled is None
    assert [j.report_name for j in jobs] == ["morning-brief", "custom job 2", "triage-inbox"]
    assert source.read_jobs(json.dumps(json.loads(JOBS_JSON)["jobs"]), TEMPLATE_JOBS)[0].key == "jarvis:job:a1b2c3"
    assert source.read_jobs("{not json", TEMPLATE_JOBS) == [] and source.read_jobs(None, TEMPLATE_JOBS) == []


def test_job_names_come_from_jobs_json_and_both_marker_shapes():
    jobs = source.read_jobs(JOBS_JSON, TEMPLATE_JOBS)
    assert source.job_names_by_id(jobs, json.dumps({"weekly-sweep": "w1w2w3"})) == {
        "a1b2c3": "morning-brief", "w1w2w3": "weekly-sweep"}
    marker = json.dumps({"jobs": [{"id": "r1r2r3", "name": "roas-report"}, {"id": "a1b2c3", "name": "other"}]})
    assert source.job_names_by_id(jobs, marker) == {"a1b2c3": "morning-brief", "r1r2r3": "roas-report"}
    assert source.job_names_by_id(jobs, "{broken") == {"a1b2c3": "morning-brief"}


def test_staff_attribution_by_job_id_else_jarvis():
    names = {"s4r4h1": "triage-inbox", "a1b2c3": "morning-brief", "c1c2c3": "own-job"}
    sarah = source.attribute("cron_s4r4h1_20261103_080000", names, TEMPLATE_JOBS)
    assert (sarah.staff, sarah.agent, sarah.origin, sarah.job_id, sarah.job_name, sarah.how) == (
        "sarah", "communications", "scheduled", "s4r4h1", "triage-inbox", "job id")
    jarvis = source.attribute("cron_a1b2c3_20261001_080000", names, TEMPLATE_JOBS)
    assert (jarvis.staff, jarvis.origin, jarvis.how) == ("jarvis", "scheduled", "job id")
    own = source.attribute("cron_c1c2c3_20261001_080000", names, TEMPLATE_JOBS)
    assert (own.staff, own.agent, own.origin, own.how) == ("jarvis", "supervisor", "scheduled", "default")
    chat = source.attribute("20261001_0001", names, TEMPLATE_JOBS)
    assert (chat.staff, chat.origin, chat.job_id, chat.how) == ("jarvis", "chat", "", "default")


# --- The database backup ------------------------------------------------------------------------------------------


def _backup(path, *, uid: bool = True, extra_columns: str = "") -> str:
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, model TEXT, model_config TEXT, system_prompt TEXT,"
        " parent_session_id TEXT, started_at REAL, ended_at REAL, end_reason TEXT, title TEXT, input_tokens INTEGER,"
        " estimated_cost_usd REAL, archived INTEGER);"
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, tool_calls TEXT,"
        " tool_name TEXT, tool_call_id TEXT, timestamp REAL, reasoning TEXT, reasoning_details TEXT, token_count"
        " INTEGER, finish_reason TEXT, active INTEGER, compacted INTEGER%s%s);"
        % (", message_uid TEXT" if uid else "", extra_columns))
    con.executemany("INSERT INTO sessions (id, source, model_config, parent_session_id, started_at, end_reason, title)"
                    " VALUES (?,?,?,?,?,?,?)", [
                        ("p1", "slack", "{}", None, 100, "compression", "Parent"),
                        ("c1", "slack", "{}", "p1", 200, "completed", "Continuation"),
                        ("b0", "cli", "{}", None, 300, "branched", ""),
                        ("b1", "cli", "{}", "b0", 400, "", "Branch"),
                        ("d1", "cli", json.dumps({"_delegate_from": "p1"}), None, None, "", "Delegated"),
                        ("x1", "telegram", json.dumps({"_branched_from": "p1"}), None, "2026-01-01 10:00:00", "", "")])
    rows = [(1, "c1", "user", "hi", None, None, None, 2.0, 1, 0, "u-1"),
            (2, "c1", "assistant", "hello", None, None, None, 1.0, 1, 0, "u-dup"),
            (3, "c1", "tool", "out", None, "terminal", "call_1", 3.0, 1, 0, "u-dup"),
            (4, "gone", "user", "orphan", None, None, None, 4.0, 1, 0, None)]
    for r in rows:
        if uid:
            con.execute("INSERT INTO messages (id, session_id, role, content, tool_calls, tool_name, tool_call_id, "
                        "timestamp, active, compacted, message_uid) VALUES (?,?,?,?,?,?,?,?,?,?,?)", r)
        else:
            con.execute("INSERT INTO messages (id, session_id, role, content, tool_calls, tool_name, tool_call_id, "
                        "timestamp, active, compacted) VALUES (?,?,?,?,?,?,?,?,?,?)", r[:10])
    con.commit()
    con.close()
    return str(path)


def test_backup_columns_are_listed_and_never_reasoning_prompts_models_or_costs(tmp_path):
    b = source.Backup(_backup(tmp_path / "s.db"), NY, {}, TEMPLATE_JOBS)
    try:
        read = b.columns_read
        assert read["messages"] == ["id", "session_id", "role", "content", "tool_name", "tool_call_id", "tool_calls",
                                    "timestamp", "active", "compacted", "message_uid"]
        assert "system_prompt" not in read["sessions"] and "model" not in read["sessions"]
        flat = [c for cols in read.values() for c in cols]
        assert not any(kit.REASONING_COLUMN.search(c) or source.FORBIDDEN_COLUMN.match(c) for c in flat)
        assert not {"input_tokens", "estimated_cost_usd", "token_count", "reasoning", "finish_reason"} & set(flat)
    finally:
        b.close()


def test_message_keys_order_positions_and_orphans(tmp_path):
    b = source.Backup(_backup(tmp_path / "s.db"), NY, {}, TEMPLATE_JOBS)
    try:
        msgs = b.messages("c1")
        # ORDER BY id (the timestamp only breaks ties): row 2's earlier timestamp does not move it.
        assert [(m.id, m.position) for m in msgs] == [(1, 0), (2, 1), (3, 2)]
        # A uid used twice keeps the uid key for its first row only.
        assert [m.key for m in msgs] == ["jarvis:message:uid:u-1", "jarvis:message:uid:u-dup",
                                         "jarvis:message:c1:3"]
        assert msgs[2].tool_name == "terminal" and msgs[2].tool_call_id == "call_1"
        assert [m.key for m in b.orphans()] == ["jarvis:message:gone:4"]
        assert b.counts() == {"sessions": 6, "messages": 4}
    finally:
        b.close()
    plain = source.Backup(_backup(tmp_path / "plain.db", uid=False), NY, {}, TEMPLATE_JOBS)
    try:
        assert [m.key for m in plain.messages("c1")] == ["jarvis:message:c1:1", "jarvis:message:c1:2",
                                                         "jarvis:message:c1:3"]
        assert plain.messages("c1")[0].active == 1 and "message_uid" not in plain.columns_read["messages"]
    finally:
        plain.close()


def test_the_source_fingerprint_changes_with_what_matters_only(tmp_path):
    b = source.Backup(_backup(tmp_path / "s.db"), NY, {}, TEMPLATE_JOBS)
    try:
        m = b.messages("c1")[0]
    finally:
        b.close()
    fp = m.fp
    m.at = datetime(2020, 1, 1, tzinfo=UTC)
    assert m.fp == fp
    m.compacted = 1
    assert m.fp != fp


def test_sessions_lineage_channels_and_time_order(tmp_path):
    b = source.Backup(_backup(tmp_path / "s.db"), NY, {}, TEMPLATE_JOBS)
    try:
        sessions = {s.id: s for s in b.sessions()}
        order = [s.id for s in b.sessions()]
    finally:
        b.close()
    assert sessions["c1"].lineage == "continuation" and sessions["c1"].parent_id == "p1"
    assert sessions["b1"].lineage == "branch"
    assert sessions["d1"].lineage == "delegated" and sessions["d1"].parent_id == "p1"
    assert sessions["x1"].lineage == "branch" and sessions["x1"].time_assumed_local
    assert sessions["p1"].lineage == "" and sessions["p1"].channel == "slack"
    assert sessions["b0"].title == "" and sessions["c1"].title == "Continuation"
    # (started_at IS NULL, started_at, id): the session without a time comes last.
    assert order == ["p1", "c1", "b0", "b1", "x1", "d1"]


def test_a_backup_with_a_wal_or_journal_file_is_refused(tmp_path):
    db = _backup(tmp_path / "s.db")
    assert source.check_backup(db)["wal_present"] is False
    open(db + "-wal", "wb").close()  # an empty -wal file holds nothing: allowed
    source.check_backup(db)
    for suffix in ("-wal", "-journal"):
        with open(db + suffix, "wb") as f:
            f.write(b"pending changes")
        with pytest.raises(source.SourceRefused, match=suffix):
            source.check_backup(db)
        os.remove(db + suffix)
    with open(tmp_path / "not.db", "wb") as f:
        f.write(b"plain text")
    with pytest.raises(source.SourceRefused, match="not an SQLite"):
        source.check_backup(str(tmp_path / "not.db"))
    os.symlink(db, tmp_path / "link.db")
    with pytest.raises(source.SourceRefused, match="link"):
        source.check_backup(str(tmp_path / "link.db"))


def test_a_backup_without_the_required_columns_is_refused(tmp_path):
    path = tmp_path / "odd.db"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE sessions (id TEXT); CREATE TABLE messages (id INTEGER, session_id TEXT, role TEXT);")
    con.close()
    with pytest.raises(source.SourceRefused, match="content"):
        source.Backup(str(path), NY, {}, TEMPLATE_JOBS)


def test_profile_files_are_read_without_following_links(tmp_path):
    root = tmp_path / "profile"
    (root / "knowledge").mkdir(parents=True)
    (root / "knowledge" / "a.md").write_text("Alpha\n")
    (tmp_path / "outside.md").write_text("outside\n")
    os.symlink(tmp_path / "outside.md", root / "knowledge" / "linked.md")
    files, skipped = source.walk_area("profile", str(root), kit.load_templates())
    assert [f.rel for f in files] == ["knowledge/a.md"] and files[0].text == "Alpha\n"
    assert skipped == [{"path": "knowledge/linked.md", "kind": "link (not followed)"}]
    # A file swapped for a link after the folder was listed is not opened through the link.
    st = os.lstat(root / "knowledge" / "a.md")
    os.remove(root / "knowledge" / "a.md")
    os.symlink(tmp_path / "outside.md", root / "knowledge" / "a.md")
    with pytest.raises(OSError):
        source.read_no_follow(str(root / "knowledge" / "a.md"), st)


# --- Mapping -------------------------------------------------------------------------------------------------------


def test_memory_entries_split_on_the_section_sign_else_list_items():
    assert mapping.split_entries("One.\n§\nTwo.\n§\n\n\nThree.\n") == ["One.", "Two.", "\n\nThree.\n"]
    assert mapping.split_entries("# Notes\n- one\n  more\n- two\n") == ["- one\n  more", "- two"]
    assert mapping.split_entries("Intro text\n- one\n") == ["Intro text", "- one"]
    assert mapping.split_entries("Just a paragraph.\n") == ["Just a paragraph.\n"] and mapping.split_entries(" \n") == []


def test_titles_skip_front_matter_blank_lines_and_markers():
    assert mapping.title_from("\n\n\nInvoices go out on the first.\n", "MEMORY.md entry 5") == (
        "Invoices go out on the first.")
    assert mapping.title_from("---\nname: x\n---\n# Heading\nbody", "f") == "Heading"
    assert mapping.title_from("<!-- c -->\n----\n", "notes.md") == "notes.md"


@pytest.mark.parametrize("url, expected", [
    ("https://www.Example.COM/", "https://www.example.com"),
    ("http://user:pw@shop.example.test:8443/path/?q=1#x", "https://shop.example.test/path"),
    ("https://example.test/a/b).", "https://example.test/a/b"),
    ("ftp://example.test", None),
    ("https://exa mple.test", None),
])
def test_urls_are_normalised(url, expected):
    assert mapping.normalize_url(url) == expected


def test_cuts_never_split_a_redaction_marker_and_stay_within_the_limit():
    text = ("word " * 50) + "[REDACTED:credential_assignment] " + ("tail " * 50)
    out, cut = mapping.cut(text, 300)
    assert cut and len(out) <= 300 and "[REDACTED:credential_assi" not in out.replace(
        "[REDACTED:credential_assignment]", "")
    assert mapping.cut("short", 300) == ("short", False)


def test_configuration_files_get_both_rule_sets():
    secret = "Blue" + "Sky99" + "!x"
    cleaned = mapping.clean("smtp:\n  host: mail.example.test\n  password: %s\nnote: the token is %s\n"
                            % (secret, secret), "config")
    assert secret not in cleaned.text and cleaned.secret_types
