"""The JARVIS import (app/brain/importer.py, source.py, mapping.py and the brain-import CLI): spec section 2.7.

The sources are the kit's fixture plus the brain-specific planted set (tests/brain_jarvis.py), built once per module;
a test that changes its sources works on its own copy.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select, text, update

from app.brain import importer as imp
from app.brain import jarvis_kit as kit
from app.brain import mapping
from app.brain import source as src
from app.brain import store
from app.db import session_scope
from app.models import (
    ArchiveMessage,
    ArchiveSession,
    AuditLog,
    BrainTombstone,
    ImportRecord,
    ImportRun,
    MemoryConflict,
    MemoryItem,
    MemoryItemVersion,
    Schedule,
    Site,
    User,
    WorkflowDraft,
)

from .. import brain_jarvis as bj

pytestmark = pytest.mark.usefixtures("clean_db")
BACKEND = Path(__file__).resolve().parents[2]
ALL = imp.Selection(all=True)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return bj.build(tmp_path_factory.mktemp("jarvis") / "fx")


@pytest.fixture
def fx(built, tmp_path):
    """A private copy, for a test that changes its sources."""
    return bj.copy(built, tmp_path / "fx")


async def records(run_id: int) -> dict[str, ImportRecord]:
    async with session_scope() as s:
        return {r.source_key: r for r in (await s.scalars(select(ImportRecord).where(ImportRecord.run_id == run_id)))}


async def run_row(run_id: int) -> ImportRun:
    async with session_scope() as s:
        row = await s.get(ImportRun, run_id)
        assert row is not None
        return row


async def session_of(source_id: str) -> ArchiveSession:
    async with session_scope() as s:
        found = await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == source_id))
        assert found is not None, source_id
        return found


async def messages_of(source_id: str) -> list[ArchiveMessage]:
    async with session_scope() as s:
        return list((await s.scalars(select(ArchiveMessage).join(
            ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id).where(
            ArchiveSession.source_session_id == source_id).order_by(ArchiveMessage.position))).all())


def source_messages(fx: dict[str, str], session_id: str) -> list[src.SourceMessage]:
    backup = src.Backup(fx["db"], src.ZoneInfo("UTC"), {}, {})
    try:
        return backup.messages(session_id)
    finally:
        backup.close()


def kinds(report: dict) -> dict[str, int]:
    return {k: v for k, v in report["by_action"].items() if v}


# --- plan, run, re-run --------------------------------------------------------------------------------------------


async def test_plan_writes_nothing_and_decides_as_run_does(built):
    before = await bj.table_counts()
    planned = await imp.plan(bj.paths(built), ALL)
    assert await bj.table_counts() == before
    assert planned["plan"] is True and planned["totals"]["sessions"] == 35
    done = await imp.run(bj.paths(built), ALL)
    for key in ("by_action", "by_reason", "by_type", "by_agent", "by_staff", "by_kind", "secrets", "restricted",
                "masked", "conflicts", "drafts", "time_range", "totals"):
        assert done["report"][key] == planned[key], key
    # A plan after the run sees only what is already there.
    again = await imp.plan(bj.paths(built), ALL)
    assert not {"imported", "updated", "conflict", "withheld"} & set(kinds(again))


async def test_a_sample_run_then_a_rerun_that_adds_nothing(built):
    first = await imp.run(bj.paths(built), imp.Selection(sample=5), label="first look")
    assert first["report"]["totals"]["sessions"] == 5
    assert first["report"]["by_type"]["file"] <= 10 + 2  # the file sample plus the two memory files
    assert (await imp.verify(first["run_id"], bj.paths(built)))["passed"]
    rows = await bj.table_counts(exclude=bj.HISTORY_TABLES)
    second = await imp.run(bj.paths(built), imp.Selection(sample=5))
    assert set(kinds(second["report"])) <= {"duplicate", "excluded"}
    assert await bj.table_counts(exclude=bj.HISTORY_TABLES) == rows
    first_keys = set(await records(first["run_id"]))
    assert set(await records(second["run_id"])) == first_keys  # the same sample, every record accounted for again
    assert (await imp.verify(second["run_id"], bj.paths(built)))["passed"]
    assert (await run_row(first["run_id"])).label == "first look"


async def test_the_full_import_of_the_planted_fixture(built, tmp_path):
    discovery = tmp_path / "discovery"
    discovery.mkdir()
    kit.cmd_discover(argparse.Namespace(profile=built["profile"], content=built["content"], db=built["db"],
                                        out=str(discovery), sample=50, full=False, check=False, templates=None))
    paths = bj.paths(built, discovery=str(discovery / "discovery-summary.json"))
    result = await imp.run(paths, ALL, actor="admin@test.local")
    report, run_id = result["report"], result["run_id"]
    assert report["discovery_check"]["ok"] is True
    verification = await imp.verify(run_id, paths)
    assert verification["passed"], verification
    assert verification["verification"]["checks"]["all_messages"]["ok"]

    # No planted value anywhere in the brain tables; none at all in the report, the error or the audit log.
    stored = await bj.stored_text()
    for table, dump in stored.items():
        assert bj.planted_in(dump, bj.never_stored()) == [], table
    row = await run_row(run_id)
    assert row.error == "" and row.status == "completed"
    assert bj.planted_in(json.dumps(row.report) + row.error + row.label, bj.never_shown()) == []
    audit = (await bj.stored_text(("audit_log",)))["audit_log"]
    assert bj.planted_in(audit, bj.never_shown()) == []
    # The report names no file (template paths aside) and no title.
    flat = json.dumps(report)
    for name in ("smtp-notes", "big-notes", "websites", "scout-2026", "Planted brain session", "Inbox triage"):
        assert name not in flat
    assert {d["name"] for d in report["drafts"]} == {"morning-brief", "custom job 2", "triage-inbox",
                                                     "website watchdog"}
    assert row.source["db"]["wal_present"] is False and row.source["profile"]["manifest_sha256"]
    assert not any(kit.REASONING_COLUMN.search(c) or c in ("system_prompt", "model")
                   for cols in row.source["columns_read"].values() for c in cols)

    recs = await records(run_id)

    # Withheld sessions hold no text at all.
    for sid, kind in (("20261012_0012", "us_ssn"), (bj.S_RESTRICTED_ARG, "date_of_birth")):
        sess = await session_of(sid)
        assert (sess.sensitivity, sess.title, sess.summary, sess.labels, sess.imported_messages) == (
            "restricted", "", "", {}, 0)
        msgs = await messages_of(sid)
        assert msgs and all(m.withheld and m.content == "" and m.tool_calls == [] and m.labels == []
                            and m.sensitivity == "restricted" for m in msgs)
        assert recs[sess.source_key].action == "withheld" and kind in recs[sess.source_key].reason
    assert report["restricted"]["sessions_withheld"] == 2 and report["restricted"]["files_excluded"] == 1

    # Masking and customer data by default.
    leaks = await messages_of(bj.S_LEAKS)
    contact = next(m for m in leaks if "wrote on WhatsApp" in m.content)
    assert contact.content == "Lead [email] wrote on WhatsApp from [phone] and called [phone]"
    assert contact.sensitivity == "customer" and contact.flags["masked"] == {"email_address": 1, "phone_number": 2}
    assert {m.sensitivity for m in leaks if m.tool_name == "gmail_send_message"
            or any(c["name"] == "gmail_send_message" for c in m.tool_calls)} == {"customer"}
    assert {m.sensitivity for m in await messages_of(bj.S_SARAH)} == {"customer"}
    assert (await session_of(bj.S_LEAKS)).sensitivity == "customer"
    async with session_scope() as s:
        faq = await s.scalar(select(MemoryItem).where(MemoryItem.source_key == "jarvis:file:profile/knowledge/sarah/faq.md"))
        assert "[email]" in faq.body and "[phone]" in faq.body and faq.sensitivity == "customer"
        assert (faq.kind, faq.agent) == ("business", "communications")
        users = (await s.scalars(select(MemoryItem).where(MemoryItem.source_ref == "memories/USER.md#entry"))).all()
        assert users and all((u.kind, u.sensitivity, u.flags.get("owner_profile"), u.status)
                             == ("business", "customer", True, "quarantined") for u in users)
        items = (await s.scalars(select(MemoryItem).where(MemoryItem.import_run_id == run_id))).all()
        assert {i.origin for i in items} == {"import"} and {i.source_system for i in items} == {"jarvis"}
        assert all(i.status in ("pending_review", "quarantined") for i in items)
        assert all(i.status == "quarantined" for i in items if i.flags.get("secrets_redacted"))
        big = next(i for i in items if i.source_key == "jarvis:file:profile/" + bj.BIG_FILE)
        assert big.flags["truncated"] is True and len(big.body) <= store.MAX_BODY
        assert any(i.title == "Invoices go out on the first working day." for i in items)
        scout = next(i for i in items if i.source_key == "jarvis:file:profile/workspace/reports/scout-2026-10-05.md")
        assert (scout.kind, scout.agent) == ("project", "seo_content") and scout.source_at.date().isoformat() == (
            "2026-10-05")
        role = next(i for i in items if i.source_key == "jarvis:file:profile/operations.md#role-scout")
        assert (role.kind, role.agent, role.source_ref) == ("instruction", "seo_content", "operations.md#role scout")
        proposals = next(i for i in items if i.title == "Proposals JARVIS sent")
        assert "2026-10" in proposals.body and "[email]" not in proposals.body
        notes = next(i for i in items if i.source_ref == "jarvis-content/notes.md")
        assert (notes.kind, notes.agent, notes.flags["from_content_folder"]) == ("knowledge", "", True)
        unknown = next(i for i in items if i.kind == "skill" and i.flags.get("custom"))
        assert (unknown.agent, unknown.flags["compatibility"]) == ("supervisor", "reference_only")
        assert recs["jarvis:file:profile/knowledge/clients/acme-widgets-planted.md"].reason == "restricted data"

    # Staff attribution (inferred), lineage and times.
    sarah = await session_of(bj.S_SARAH)
    assert (sarah.staff, sarah.agent, sarah.origin, sarah.job_id, sarah.job_name, sarah.flags["attribution"]) == (
        "sarah", "communications", "scheduled", "s4r4h1", "triage-inbox", "job id")
    brief = await session_of("cron_a1b2c3_20261001_080000")
    assert (brief.staff, brief.origin, brief.job_name, brief.flags["attribution"]) == (
        "jarvis", "scheduled", "morning-brief", "job id")
    custom = await session_of("cron_d4e5f6_20261007_080000")
    assert (custom.staff, custom.flags["attribution"]) == ("jarvis", "default")
    assert (await session_of("20261010_0010")).flags["attribution"] == "default"
    assert report["by_staff"] == {"jarvis": 34, "sarah": 1}
    assert [(s.lineage, s.parent_source_session_id) for s in [
        await session_of("20261020_0020"), await session_of("20261021_0021"), await session_of("20261022_0022")]] == [
        ("continuation", "20261019_0019"), ("branch", "20261018_0018"), ("delegated", "20261017_0017")]
    assert (await session_of("20261023_0023")).hermes_archived is True
    times = await session_of(bj.S_TIMES)
    assert times.started_at == datetime(2026, 11, 5, 14, 30, tzinfo=UTC) and times.flags["time_assumed_local"]
    at = [(m.at, m.flags.get("time_assumed_local", False)) for m in await messages_of(bj.S_TIMES)]
    assert at == [(datetime(2026, 11, 5, 14, 31, tzinfo=UTC), True), (datetime(2026, 11, 5, 8, 0, tzinfo=UTC), False),
                  (datetime(2026, 11, 5, 8, 1, tzinfo=UTC), False), (None, False)]

    # Compaction, rewinds, duplicates, exclusions, a NUL, keys.
    compacted = await messages_of("20261013_0013")
    assert [m.state for m in compacted] == ["compacted"] * 4 + ["live"]
    rewound = source_messages(built, "20261013_0013")[-1]
    assert recs[rewound.key].action == "excluded" and recs[rewound.key].reason == "rewound or superseded"
    carried = source_messages(built, bj.S_CARRIED)
    assert recs[carried[2].key].action == "duplicate"
    assert recs[carried[2].key].reason == "carried-on copy of a compacted message"
    assert recs[carried[2].key].target_id == next(m.id for m in await messages_of(bj.S_CARRIED) if m.position == 0)
    raw = source_messages(built, bj.S_LEAKS)
    by_reason = {recs[m.key].reason for m in raw if recs[m.key].action in ("duplicate", "excluded")}
    assert by_reason == {"same as the previous message", "system prompt", "empty"}
    assert sum(1 for m in leaks if m.content == "HTTP 200 OK") == 2  # parallel results are never merged
    nul = next(m for m in leaks if m.tool_name == "terminal")
    assert nul.content == bj.BRAIN_CONTENT["nul_message"] + bj.BRAIN_CONTENT["nul_tail"]
    assert all(k.startswith("jarvis:message:uid:") for k in recs if k.startswith("jarvis:message:") and
               not k.startswith("jarvis:message:gone"))
    orphan = next(r for k, r in recs.items() if r.record_type == "orphan")
    assert orphan.reason == "no session" and report["totals"]["orphans"] == 1
    assert {m.position for m in leaks} == set(range(len(raw))) - {
        m.position for m in raw if recs[m.key].action != "imported"}

    # Drafts never run and nothing is scheduled.
    async with session_scope() as s:
        drafts = (await s.scalars(select(WorkflowDraft).order_by(WorkflowDraft.id))).all()
        assert len(drafts) == 4 and {d.status for d in drafts} == {"draft"} and not any(d.schedule_id for d in drafts)
        assert await s.scalar(select(func.count()).select_from(Schedule)) == 0
        brief_draft = next(d for d in drafts if d.source_key == "jarvis:job:a1b2c3")
        assert (brief_draft.cron, brief_draft.enabled_in_source, brief_draft.deliver, brief_draft.timezone) == (
            "0 8 * * *", True, "slack", "America/New_York")
        assert brief_draft.history["runs"] == 6 and brief_draft.approval["jarvis"] == "deny"
        assert brief_draft.approval["command_center"].startswith("runs without approval")
        custom_draft = next(d for d in drafts if d.source_key == "jarvis:job:d4e5f6")
        assert custom_draft.deliver == "whatsapp" and custom_draft.compatibility == "reference_only"
        watchdog = next(d for d in drafts if d.source_key == "jarvis:watchdog:check_websites")
        assert (watchdog.agent, watchdog.capability, watchdog.compatibility) == (
            "monitoring", "check_all_sites", "mapped")
        conflicts = (await s.scalars(select(MemoryConflict))).all()
        assert {c.kind for c in conflicts} == {"instruction", "schedule", "site"}
        assert {c.title for c in conflicts if c.kind == "site"} == {
            "JARVIS watched www.planted-site-one.example", "JARVIS watched planted-site-two.example"}


async def test_cli_exit_codes_and_console_output(fx, tmp_path):
    env = dict(os.environ)

    def cli(*args: str) -> tuple[int, str]:
        done = subprocess.run([sys.executable, "-m", "app.cli", *args], cwd=BACKEND, env=env, capture_output=True,
                              text=True, timeout=120)
        out = done.stdout + done.stderr
        assert bj.planted_in(out, bj.never_shown()) == [], (args, out)
        return done.returncode, out

    source = ["--db", fx["db"], "--profile", fx["profile"], "--content", fx["content"]]
    assert cli("help")[0] == 0 and cli("bogus")[0] == 2
    assert cli("brain-import", "--help")[0] == 0
    assert cli("brain-import", "bogus")[0] == 2
    assert cli("brain-import", "plan", "--all")[0] == 2  # no sources
    assert cli("brain-import", "plan", *source)[0] == 2  # no selection
    code, out = cli("brain-import", "plan", *source, "--all", "--report", str(tmp_path / "plan.json"))
    assert code == 0 and "nothing was written" in out and json.loads((tmp_path / "plan.json").read_text())["plan"]
    code, out = cli("brain-import", "run", *source, "--all", "--pre-backup-sha256", "not-a-hash")
    assert code == 2 and "refused" in out
    code, out = cli("brain-import", "run", *source, "--all", "--label", "cli run")
    assert code == 0 and "run 1 completed" in out and "rotate these kinds of credential" in out
    code, out = cli("brain-import", "verify", "--run", "1", *source)
    assert code == 0 and "passed" in out
    code, out = cli("brain-import", "status")
    assert code == 0 and "run 1: completed" in out
    assert cli("brain-import", "rollback", "--run", "1")[0] == 2  # no --yes: counts only
    assert (await run_row(1)).status == "completed"
    # verify fails (exit 1) once a secret is planted in a stored message
    async with session_scope() as s:
        m = await s.scalar(select(ArchiveMessage).where(ArchiveMessage.content != "").limit(1))
        original = m.content
        m.content = original + " password: " + bj.BRAIN_PLANTED["root_password"]
    code, out = cli("brain-import", "verify", "--run", "1", *source)
    assert code == 1 and "FAILED" in out
    async with session_scope() as s:
        await s.execute(update(ArchiveMessage).where(ArchiveMessage.id == m.id).values(content=original))
    code, out = cli("brain-import", "rollback", "--run", "1", "--yes")
    assert code == 0 and "rolled back" in out
    with open(fx["db"] + "-wal", "wb") as f:
        f.write(b"pending")
    code, out = cli("brain-import", "plan", *source, "--sample", "2")
    assert code == 2 and "-wal" in out
    assert cli("brain-import", "rescan")[0] == 0


# --- Re-import from a newer backup -------------------------------------------------------------------------------


def modify_copy(fx: dict[str, str]) -> dict[str, str]:
    """A newer JARVIS: a grown session, compacted, rewound and changed rows, a changed file and job, a deleted session,
    restricted data in a conversation that was general, and a new website."""
    con = bj.sqlite(fx["db"])
    try:
        con.execute("INSERT INTO messages (session_id, role, content, timestamp, active, compacted, message_uid) "
                    "VALUES (?, 'user', 'And the November one?', 1793600000, 1, 0, 'grown-1')", (bj.S_CARRIED,))
        con.execute("INSERT INTO messages (session_id, role, content, timestamp, active, compacted, message_uid) "
                    "VALUES (?, 'assistant', 'I drafted the November newsletter too.', 1793600001, 1, 0, 'grown-2')",
                    (bj.S_CARRIED,))
        first = lambda sid, role: con.execute(  # noqa: E731
            "SELECT min(id) FROM messages WHERE session_id = ? AND role = ?", (sid, role)).fetchone()[0]
        con.execute("UPDATE messages SET active = 0, compacted = 1 WHERE id = ?", (first("20261010_0010", "user"),))
        con.execute("UPDATE messages SET active = 0, compacted = 0 WHERE id = ?",
                    (first("20261011_0011", "assistant"),))
        con.execute("UPDATE messages SET content = 'ls output, changed' WHERE id = ?", (first("20261014_0014", "tool"),))
        con.execute("DELETE FROM messages WHERE session_id = '20261015_0015'")
        con.execute("DELETE FROM sessions WHERE id = '20261015_0015'")
        con.execute("INSERT INTO messages (session_id, role, content, timestamp, active, compacted, message_uid) "
                    "VALUES ('20261016_0016', 'user', 'Her SSN is 219-09-9999', 1793600002, 1, 0, 'grown-3')")
        con.commit()
    finally:
        con.close()
    with open(os.path.join(fx["profile"], "knowledge", "sarah", "faq.md"), "a", encoding="utf-8") as f:
        f.write("\nWe now also answer on Saturdays.\n")
    jobs_path = os.path.join(fx["profile"], "cron", "jobs.json")
    with open(jobs_path, encoding="utf-8") as f:
        jobs = json.load(f)
    jobs["jobs"][0]["prompt"] = "Compose the brief, shorter."
    with open(jobs_path, "w", encoding="utf-8") as f:
        json.dump(jobs, f)
    with open(os.path.join(fx["profile"], "knowledge", "websites.md"), "a", encoding="utf-8") as f:
        f.write("- https://planted-site-three.example/\n")
    return fx


async def test_an_incremental_reimport_from_a_modified_copy(fx):
    first = await imp.run(bj.paths(fx), ALL)
    grown_before = await session_of(bj.S_CARRIED)
    modify_copy(fx)
    second = await imp.run(bj.paths(fx), ALL)
    report, run_id = second["report"], second["run_id"]
    assert (await imp.verify(run_id, bj.paths(fx)))["passed"]
    recs = await records(run_id)
    assert report["missing_from_source"] == 1
    assert (await session_of("20261015_0015")).imported_messages == 4  # never deleted

    grown = await session_of(bj.S_CARRIED)
    assert recs[grown.source_key].action == "updated" and recs[grown.source_key].before["source_message_count"] == 4
    assert grown.imported_messages == grown_before.imported_messages + 2 and grown.source_message_count == 6
    assert grown.summary.endswith("I drafted the November newsletter too.")
    assert recs["jarvis:message:uid:grown-1"].action == "imported"

    def key(sid: str, role: str, n: int = 0) -> str:
        return [m.key for m in source_messages(fx, sid) if m.role == role][n]
    compacted = recs[key("20261010_0010", "user")]
    assert (compacted.action, compacted.reason, compacted.before["state"]) == (
        "updated", "compacted since an earlier import", "live")
    rewound = recs[key("20261011_0011", "assistant")]
    assert (rewound.action, rewound.reason) == ("updated", "rewound since an earlier import")
    assert rewound.before["deleted_row"]["import_run_id"] == first["run_id"]
    assert len(await messages_of("20261011_0011")) == 3
    changed = recs[key("20261014_0014", "tool")]
    assert (changed.action, changed.reason, changed.before["content"]) == (
        "updated", "changed since an earlier import", "ls output")
    assert any(m.content == "ls output, changed" for m in await messages_of("20261014_0014"))
    # A conversation that now holds restricted data is withheld whole, its earlier rows included.
    now_restricted = await session_of("20261016_0016")
    assert (now_restricted.sensitivity, now_restricted.title, now_restricted.summary) == ("restricted", "", "")
    assert all(m.withheld and m.content == "" for m in await messages_of("20261016_0016"))
    assert recs[key("20261016_0016", "user")].reason.startswith("withheld: restricted data")

    async with session_scope() as s:
        incoming = await s.scalar(select(MemoryItem).where(
            MemoryItem.flags["replaces_source_key"].astext == "jarvis:file:profile/knowledge/sarah/faq.md"))
        assert incoming is not None and incoming.source_key is None and "Saturdays" in incoming.body
        assert incoming.status in ("pending_review", "quarantined") and incoming.import_run_id == run_id
        kinds_ = {c.kind for c in (await s.scalars(select(MemoryConflict).where(MemoryConflict.run_id == run_id)))}
        assert kinds_ == {"source_changed", "workflow", "site"}
        draft = await s.scalar(select(WorkflowDraft).where(WorkflowDraft.source_key == "jarvis:job:a1b2c3"))
        assert draft.import_run_id == first["run_id"]  # never overwritten
        assert "Compose the brief." in draft.spec and "shorter" not in draft.spec

    # The same newer backup again adds nothing.
    rows = await bj.table_counts(exclude=bj.HISTORY_TABLES)
    third = await imp.run(bj.paths(fx), ALL)
    assert set(kinds(third["report"])) <= {"duplicate", "excluded"} and not third["report"]["conflicts"]
    assert await bj.table_counts(exclude=bj.HISTORY_TABLES) == rows


# --- Sessions and files the planted set does not hold -----------------------------------------------------------


def add_session(fx: dict[str, str], sid: str, rows: list[dict], *, source: str = "cli", title: str = "",
                display_name: str | None = None) -> None:
    """A session added to this copy of the backup; each row is {role, content, tool_calls, tool_name, tool_call_id,
    active, compacted}."""
    con = bj.sqlite(fx["db"])
    try:
        con.execute("INSERT INTO sessions (id, source, started_at, end_reason, title, display_name) "
                    "VALUES (?, ?, 1793900000, 'completed', ?, ?)", (sid, source, title, display_name))
        for n, r in enumerate(rows):
            con.execute("INSERT INTO messages (session_id, role, content, tool_calls, tool_name, tool_call_id, "
                        "timestamp, active, compacted, message_uid) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (sid, r["role"], r.get("content", ""), r.get("tool_calls"), r.get("tool_name"),
                         r.get("tool_call_id"), 1793900000 + n, r.get("active", 1), r.get("compacted", 0),
                         "%s-%d" % (sid, n)))
        con.commit()
    finally:
        con.close()


def call(call_id: str, name: str, arguments: dict) -> str:
    return json.dumps([{"id": call_id, "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)}}])


async def test_only_the_rows_hermes_carried_on_are_copies_of_compacted_ones(fx):
    """A later turn that says what a compacted one said is kept: copies are matched one to one, by tool call id
    too, and only in the block Hermes wrote right after the compaction."""
    gone = {"active": 0, "compacted": 1}
    add_session(fx, "20261106_0106", [
        dict(role="user", content="Send the invoice to the first client.", **gone),
        dict(role="assistant", tool_calls=call("call_a", "terminal", {"command": "send-invoice --to first"}), **gone),
        dict(role="tool", content="sent", tool_name="terminal", tool_call_id="call_a", **gone),
        dict(role="assistant", content="Sent the invoice.", **gone),
        dict(role="user", content="yes", **gone),
        dict(role="assistant", content="Summary of earlier turns: the first client's invoice was sent."),
        dict(role="assistant", tool_calls=call("call_b", "terminal", {"command": "send-invoice --to second"})),
        dict(role="tool", content="sent", tool_name="terminal", tool_call_id="call_b"),
        dict(role="assistant", content="Sent the invoice."),
        dict(role="user", content="yes")])
    # Hermes's documented order: the summary, then the turns it carries on, then new turns.
    add_session(fx, "20261107_0107", [
        dict(role="user", content="Plan the week.", **gone),
        dict(role="assistant", content="Here is the plan.", **gone),
        dict(role="assistant", content="Summary of earlier turns: a plan for the week."),
        dict(role="user", content="Plan the week."),
        dict(role="assistant", content="Here is the plan."),
        dict(role="user", content="Plan the week."),
        dict(role="assistant", content="Here is the plan.")])
    done = await imp.run(bj.paths(fx), ALL)
    recs = await records(done["run_id"])
    invoices = source_messages(fx, "20261106_0106")
    assert [recs[m.key].action for m in invoices] == ["imported"] * 10
    assert [m.state for m in await messages_of("20261106_0106")] == ["compacted"] * 5 + ["live"] * 5
    week = source_messages(fx, "20261107_0107")
    assert [(recs[m.key].action, recs[m.key].reason) for m in week[2:]] == [
        ("imported", ""), ("duplicate", imp.CARRIED), ("duplicate", imp.CARRIED), ("imported", ""), ("imported", "")]
    stored = {m.position: m.id for m in await messages_of("20261107_0107")}
    assert [recs[m.key].target_id for m in week[3:5]] == [stored[0], stored[1]]
    assert (await imp.verify(done["run_id"], bj.paths(fx)))["passed"]


async def test_restricted_data_in_a_row_that_is_not_stored_withholds_the_conversation(fx):
    ssn = "219-09-9999"
    add_session(fx, "20261108_0108", [
        dict(role="system", content="Client file: SSN %s, disputing the Capital One collection." % ssn),
        dict(role="user", content="Where are we with her file?"),
        dict(role="assistant", content="The letters went out on Monday.")])
    add_session(fx, "20261109_0109", [
        dict(role="user", content="Fill in the onboarding form."),
        dict(role="assistant", active=0, tool_calls=call("call_r", "fill_form", {
            "field": "date of birth", "value": bj.BRAIN_PLANTED["dob"]})),
        dict(role="assistant", content="Done, the form is in.")])
    done = await imp.run(bj.paths(fx), ALL)
    recs = await records(done["run_id"])
    for sid, kind in (("20261108_0108", "us_ssn"), ("20261109_0109", "date_of_birth")):
        sess = await session_of(sid)
        assert (sess.sensitivity, sess.title, sess.summary, sess.imported_messages) == ("restricted", "", "", 0)
        assert recs[sess.source_key].action == "withheld" and kind in recs[sess.source_key].reason
        msgs = await messages_of(sid)
        assert len(msgs) == 2 and all(m.withheld and m.content == "" and m.tool_calls == [] for m in msgs)
    excluded = [recs[m.key].reason for m in source_messages(fx, "20261108_0108") + source_messages(fx, "20261109_0109")
                if recs[m.key].action == "excluded"]
    assert excluded == ["system prompt", imp.REWOUND]
    dump = "\n".join((await bj.stored_text()).values())
    assert ssn not in dump and "Capital One" not in dump and "letters went out" not in dump
    assert (await imp.verify(done["run_id"], bj.paths(fx)))["passed"]


async def test_customer_work_through_a_generic_tool_or_a_customer_channel_is_customer_data(fx):
    lead = "Jane Roe, Roe Dental, 12 Main St"
    add_session(fx, "20261110_0110", source="slack", rows=[
        dict(role="user", content="Send the website proposal to the dentist who wrote in."),
        dict(role="assistant", tool_calls=call("call_p", "terminal", {
            "command": "python scripts/send_proposal.py --lead \"%s\"" % lead})),
        dict(role="tool", tool_name="terminal", tool_call_id="call_p",
             content=json.dumps({"status": "sent", "pdf": "workspace/proposals/Roe-Dental.pdf"})),
        dict(role="assistant", content="Done: Roe Dental got the five-page package at 2,400.")])
    add_session(fx, "20261111_0111", source="whatsapp", display_name="Jane Roe", rows=[
        dict(role="user", content="Hi, how much is a five-page website?"),
        dict(role="assistant", content="Our five-page package is 2,400.")])
    planned = await imp.plan(bj.paths(fx), ALL)
    done = await imp.run(bj.paths(fx), ALL)
    assert done["report"]["by_sensitivity"] == planned["by_sensitivity"]
    slack = await session_of("20261110_0110")
    assert slack.sensitivity == "customer" and "customer_channel" not in slack.flags
    by_role = {(m.role, bool(m.tool_calls)): m.sensitivity for m in await messages_of("20261110_0110")}
    assert by_role[("assistant", True)] == "customer"  # the command names a proposal and a lead
    assert by_role[("tool", False)] == "customer"  # the result names a proposal's file
    gateway = await session_of("20261111_0111")
    assert (gateway.channel, gateway.sensitivity, gateway.title, gateway.flags["customer_channel"]) == (
        "whatsapp", "customer", "", True)
    assert {m.sensitivity for m in await messages_of("20261111_0111")} == {"customer"}
    assert "Jane Roe" not in (await bj.stored_text(("archive_sessions",)))["archive_sessions"]
    assert (await session_of("20261011_0011")).sensitivity == "general"  # the owner's own Slack stays general


async def test_a_file_path_that_names_a_person_or_holds_restricted_data(fx):
    email, ssn_name = "jane.roe@example.com", "jane-roe-ssn-078-05-1120"
    os.makedirs(os.path.join(fx["content"], "leads"), exist_ok=True)
    os.makedirs(os.path.join(fx["content"], "clients"), exist_ok=True)
    with open(os.path.join(fx["content"], "leads", email + ".md"), "w", encoding="utf-8") as f:
        f.write("---\nstatus: new\n---\n")
    with open(os.path.join(fx["content"], "clients", ssn_name + ".md"), "w", encoding="utf-8") as f:
        f.write("---\nstatus: new\n---\n")
    done = await imp.run(bj.paths(fx), ALL)
    report, recs = done["report"], await records(done["run_id"])
    async with session_scope() as s:
        item = await s.scalar(select(MemoryItem).where(MemoryItem.source_ref == "jarvis-content/leads/[email]"))
    assert item is not None and (item.title, item.sensitivity) == ("[email]", "customer")
    assert item.source_key.startswith("jarvis:file:content/path-sha256:")
    restricted = [r for r in recs.values() if r.reason == "restricted data in its path"]
    assert len(restricted) == 1 and restricted[0].action == "excluded"
    assert report["restricted"]["files_excluded"] == 2 and report["restricted"]["types"]["us_ssn"] >= 1
    for table, dump in (await bj.stored_text()).items():
        assert email not in dump and ssn_name not in dump and "078-05-1120" not in dump, table
    assert (await imp.verify(done["run_id"], bj.paths(fx)))["passed"]


# --- Failures, interruptions, liveness -------------------------------------------------------------------------


async def test_resume_after_a_failure_in_the_middle_of_a_batch(built, monkeypatch):
    planned = await imp.plan(bj.paths(built), ALL)
    monkeypatch.setattr(imp, "BATCH_SESSIONS", 4)
    original = imp.Processor.do_session
    secret = bj.BRAIN_PLANTED["root_password"]

    async def failing(self, ss, index):
        if index == 6:
            raise RuntimeError("could not store " + secret)  # its text must never be stored or shown
        return await original(self, ss, index)

    monkeypatch.setattr(imp.Processor, "do_session", failing)
    with pytest.raises(imp.ImportFailed) as failed:
        await imp.run(bj.paths(built), ALL)
    assert secret not in str(failed.value)
    async with session_scope() as s:
        row = await s.scalar(select(ImportRun))
        sixth = sorted(src.Backup(built["db"], src.ZoneInfo("UTC"), {}, {}).sessions(), key=src.SourceSession.sort_key)
        assert row.status == "failed" and secret not in row.error
        assert row.error == "RuntimeError in sessions at jarvis:session:%s" % sixth[6].id
        assert (row.checkpoint["phase"], row.checkpoint["index"]) == ("sessions", 4)
        # The failed batch left nothing behind: sessions 4 and 5 were rolled back with it.
        assert await s.scalar(select(func.count()).select_from(ArchiveSession)) == 4
        assert await s.scalar(select(func.count()).select_from(ImportRecord).where(
            ImportRecord.record_type == "session")) == 4
        failed_audit = await s.scalar(select(AuditLog).where(AuditLog.action == "brain.import_failed"))
        assert failed_audit.detail["error"] == row.error
    monkeypatch.setattr(imp.Processor, "do_session", original)
    with pytest.raises(imp.Refused, match="same sources"):
        await imp.resume(row.id, bj.paths({**built, "content": None}))
    done = await imp.resume(row.id, bj.paths(built))
    assert done["status"] == "completed"
    assert done["report"]["by_action"] == planned["by_action"] and done["report"]["by_reason"] == planned["by_reason"]
    assert (await imp.verify(row.id, bj.paths(built)))["passed"]
    with pytest.raises(imp.Refused, match="completed"):
        await imp.resume(row.id, bj.paths(built))


async def test_a_job_id_used_twice_in_jobs_json_is_a_duplicate(fx):
    jobs_path = os.path.join(fx["profile"], "cron", "jobs.json")
    with open(jobs_path, encoding="utf-8") as f:
        jobs = json.load(f)
    jobs["jobs"].append(dict(jobs["jobs"][0], name="own-job", prompt="Another job that reuses the id."))
    with open(jobs_path, "w", encoding="utf-8") as f:
        json.dump(jobs, f)
    done = await imp.run(bj.paths(fx), ALL)
    recs = await records(done["run_id"])
    assert recs["jarvis:job:a1b2c3"].action == "imported"
    assert (recs["jarvis:job:a1b2c3#2"].action, recs["jarvis:job:a1b2c3#2"].reason) == (
        "duplicate", "the same job id appears earlier in jobs.json")
    async with session_scope() as s:
        draft = await s.scalar(select(WorkflowDraft).where(WorkflowDraft.source_key == "jarvis:job:a1b2c3"))
        assert draft.name == "morning-brief" and "reuses the id" not in draft.spec
        assert await s.scalar(select(func.count()).select_from(WorkflowDraft)) == 4
    assert (await session_of("cron_a1b2c3_20261001_080000")).job_name == "morning-brief"
    assert (await imp.verify(done["run_id"], bj.paths(fx)))["passed"]


async def test_a_source_key_recorded_twice_fails_the_run_at_that_record(built, monkeypatch):
    original = imp.Processor.do_draft

    async def twice(self, d, index):
        await original(self, d, index)
        if d.key == "jarvis:job:a1b2c3":
            self.record(d.key, d.record_type, "duplicate", "recorded twice")  # refused when the batch is flushed

    monkeypatch.setattr(imp.Processor, "do_draft", twice)
    with pytest.raises(imp.ImportFailed):
        await imp.run(bj.paths(built), ALL)
    async with session_scope() as s:
        row = await s.scalar(select(ImportRun))
    # The batch's last record is the watchdog's; the error names the record that was recorded twice.
    assert row.status == "failed" and row.error == "IntegrityError in jobs at jarvis:job:a1b2c3"
    monkeypatch.setattr(imp.Processor, "do_draft", original)
    assert (await imp.resume(row.id, bj.paths(built)))["status"] == "completed"


async def test_a_run_that_stopped_after_its_last_checkpoint_resumes_to_its_report(built, tmp_path, monkeypatch):
    """The finish checkpoint is past every phase: resume only writes the report, with the discovery check the run
    started with (resume takes no --discovery)."""
    backup = src.Backup(built["db"], src.ZoneInfo("UTC"), {}, {})
    try:
        counts = backup.counts()
    finally:
        backup.close()
    discovery = tmp_path / "discovery-summary.json"
    discovery.write_text(json.dumps({"database_backup": {"sessions": {"total": counts["sessions"]},
                                                         "messages": {"total": counts["messages"]}}}))
    original, calls = imp.missing_from_source, []

    async def fails_once(session, sources):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("after the finish checkpoint")
        return await original(session, sources)

    monkeypatch.setattr(imp, "missing_from_source", fails_once)
    with pytest.raises(imp.ImportFailed):
        await imp.run(bj.paths(built, discovery=str(discovery)), ALL)
    row = (await _all_runs())[0]
    assert row.status == "failed" and row.checkpoint["phase"] == "finish"
    done = await imp.resume(row.id, bj.paths(built))
    assert done["status"] == "completed" and done["report"]["discovery_check"]["ok"] is True
    assert (await imp.verify(row.id, bj.paths(built)))["passed"]


async def _paused_run(built, monkeypatch, at_index: int, selection=ALL):
    """A run in a task, held inside do_session(at_index) until the returned gate is set."""
    gate, entered = asyncio.Event(), asyncio.Event()
    original = imp.Processor.do_session

    async def paused(self, ss, index):
        if index == at_index and not gate.is_set():
            entered.set()
            await gate.wait()
        return await original(self, ss, index)

    monkeypatch.setattr(imp.Processor, "do_session", paused)
    task = asyncio.create_task(imp.run(bj.paths(built), selection, heartbeat_seconds=0.05))
    await asyncio.wait_for(entered.wait(), 60)
    return task, gate


async def test_an_interrupted_run_is_marked_and_can_be_resumed(built, monkeypatch):
    monkeypatch.setattr(imp, "BATCH_SESSIONS", 4)
    task, gate = await _paused_run(built, monkeypatch, 5)
    task.cancel()  # what Ctrl+C and SIGTERM do (the CLI turns SIGTERM into a cancellation)
    with pytest.raises(imp.ImportFailed, match="interrupted"):
        await task
    gate.set()
    row = await run_row(1)
    assert row.status == "interrupted" and row.checkpoint["phase"] == "sessions" and row.checkpoint["index"] == 4
    done = await imp.resume(1, bj.paths(built))
    assert done["status"] == "completed" and (await imp.verify(1, bj.paths(built)))["passed"]


async def test_one_import_at_a_time_and_rollback_waits_for_the_lock(built, monkeypatch):
    task, gate = await _paused_run(built, monkeypatch, 1, imp.Selection(sample=3))
    try:
        started = (await run_row(1)).started_at
        await asyncio.sleep(0.3)
        status = await imp.status()
        assert status["runs"][0]["status"] == "running" and not status["marked_interrupted"]
        assert (await run_row(1)).heartbeat_at > started  # the background heartbeat
        assert await imp.run_alive(1)
        with pytest.raises(imp.Refused, match="another import"):
            await imp.run(bj.paths(built), imp.Selection(sample=3))
        with pytest.raises(imp.Refused, match="running"):
            await imp.resume(1, bj.paths(built))
        with pytest.raises(imp.Refused, match="running"):
            await imp.rollback(1, confirm=True)
        with pytest.raises(imp.Refused, match="running"):
            await imp.rescan(apply=False)
    finally:
        gate.set()
    assert (await task)["status"] == "completed"
    assert not await imp.run_alive(1)
    assert [r.id for r in await _all_runs()] == [1]


async def test_a_run_whose_lock_connection_is_lost_stops_and_is_marked_interrupted(built, monkeypatch):
    task, gate = await _paused_run(built, monkeypatch, 1, imp.Selection(sample=3))
    try:
        async with session_scope() as s:  # the run's own lock connection goes away (a network or server fault)
            killed = await s.scalar(text(
                "SELECT count(pg_terminate_backend(pid)) FROM pg_locks WHERE locktype = 'advisory' AND database = "
                "(SELECT oid FROM pg_database WHERE datname = current_database()) AND classid::bigint = :k "
                "AND objid::bigint = 1 AND objsubid = 2"), {"k": imp.LOCK_KEY % 2 ** 32})
        assert killed == 1
        # The heartbeat notices: the run stops (another import could start now) instead of writing on. The run is
        # still held in do_session, so nothing else could end it.
        await asyncio.wait({task}, timeout=30)
        assert task.done()
        with pytest.raises(imp.ImportFailed, match="interrupted"):
            await task
    finally:
        gate.set()
    assert (await run_row(1)).status == "interrupted" and not await imp.run_alive(1)
    assert (await imp.resume(1, bj.paths(built)))["status"] == "completed"


async def _all_runs() -> list[ImportRun]:
    async with session_scope() as s:
        return list((await s.scalars(select(ImportRun).order_by(ImportRun.id))).all())


async def test_a_running_row_whose_lock_is_free_is_marked_interrupted_by_whoever_notices(built):
    async with session_scope() as s:
        s.add(ImportRun(status="running", scope={"selection": ALL.to_json(), "timezone": "UTC"}))
    status = await imp.status()
    assert status["marked_interrupted"] == [1] and status["runs"][0]["status"] == "interrupted"
    async with session_scope() as s:
        s.add(ImportRun(status="running"))
    # A new run notices too, and is not blocked by the dead row's place in the one-running-run index.
    done = await imp.run(bj.paths(built), imp.Selection(sample=2))
    assert done["run_id"] == 3 and (await run_row(2)).status == "interrupted"


async def test_a_record_the_database_refuses_is_excluded_and_the_run_continues(built, monkeypatch):
    original_values = imp.Processor.message_values
    original_clean = imp.mapping.clean

    def refused_by_the_database(self, p, withheld):
        values = original_values(self, p, withheld)
        if p.m.session_id == bj.S_TIMES and p.m.position == 1:
            values["state"] = "neither"  # violates a CHECK constraint: the database refuses this record alone
        return values

    def cleaning_fails(raw, kind="chat"):
        if raw.startswith("Keyword sweep done"):
            raise ValueError("a cleaning failure")
        return original_clean(raw, kind)

    monkeypatch.setattr(imp.Processor, "message_values", refused_by_the_database)
    monkeypatch.setattr(imp.mapping, "clean", cleaning_fails)
    done = await imp.run(bj.paths(built), ALL)
    assert done["status"] == "completed"
    recs = await records(done["run_id"])
    refused = source_messages(built, bj.S_TIMES)[1]
    assert (recs[refused.key].action, recs[refused.key].reason) == ("excluded", "invalid record: IntegrityError")
    scout = recs["jarvis:file:profile/workspace/reports/scout-2026-10-05.md"]
    assert (scout.action, scout.reason) == ("excluded", "invalid record: ValueError")
    times = await session_of(bj.S_TIMES)
    assert times.imported_messages == 3 and [m.position for m in await messages_of(bj.S_TIMES)] == [0, 2, 3]
    monkeypatch.setattr(imp.mapping, "clean", original_clean)
    assert (await imp.verify(done["run_id"], bj.paths(built)))["passed"]


# --- verify ------------------------------------------------------------------------------------------------------


async def test_verify_fails_on_a_planted_secret_an_orphan_row_and_a_count_mismatch(built):
    done = await imp.run(bj.paths(built), ALL)
    run_id = done["run_id"]
    assert (await imp.verify(run_id, bj.paths(built)))["passed"]

    async def failed_checks() -> set[str]:
        result = await imp.verify(run_id, bj.paths(built))
        assert (await run_row(run_id)).report["verification"]["passed"] is result["passed"]
        return {k for k, c in result["verification"]["checks"].items() if not c["ok"]}

    async with session_scope() as s:
        m = await s.scalar(select(ArchiveMessage).where(ArchiveMessage.import_run_id == run_id,
                                                        ArchiveMessage.content != "").order_by(ArchiveMessage.id))
        original = m.content
        m.content = original + " the root password is " + bj.BRAIN_PLANTED["root_password"]
    assert await failed_checks() == {"no_secrets_or_restricted_data"}
    async with session_scope() as s:
        await s.execute(update(ArchiveMessage).where(ArchiveMessage.id == m.id).values(content=original))
        item = await s.scalar(select(MemoryItem).where(MemoryItem.import_run_id == run_id).order_by(MemoryItem.id))
        item_body = item.body
        item.body = item_body + "\nSSN 219-09-9999"
    assert await failed_checks() == {"no_secrets_or_restricted_data"}
    async with session_scope() as s:
        await s.execute(update(MemoryItem).where(MemoryItem.id == item.id).values(body=item_body))
        orphan = ArchiveMessage(session_id=m.session_id, source_key="jarvis:message:planted-orphan", source_message_id="x",
                                position=999, role="user", content="hello", import_run_id=run_id)
        s.add(orphan)
    assert "no_unrecorded_rows" in await failed_checks()
    async with session_scope() as s:
        await s.delete(await s.get(ArchiveMessage, orphan.id))
        await s.execute(update(ArchiveSession).where(ArchiveSession.id == m.session_id)
                        .values(imported_messages=ArchiveSession.imported_messages + 1))
    assert await failed_checks() == {"session_counts"}
    async with session_scope() as s:
        await s.execute(update(ArchiveSession).where(ArchiveSession.id == m.session_id)
                        .values(imported_messages=ArchiveSession.imported_messages - 1))
        await s.execute(text("DELETE FROM import_records WHERE id = (SELECT min(id) FROM import_records "
                             "WHERE record_type = 'message')"))
    assert await failed_checks() == {"every_record_once", "no_unrecorded_rows", "session_counts", "all_messages"}


async def test_a_rollback_while_verify_scans_keeps_its_report_section(built, monkeypatch):
    done = await imp.run(bj.paths(built), imp.Selection(sample=2))
    original = imp._search_check

    async def rolled_back_meanwhile(s, run_id):
        result = await original(s, run_id)
        await imp.rollback(run_id, confirm=True)
        return result

    monkeypatch.setattr(imp, "_search_check", rolled_back_meanwhile)
    with pytest.raises(imp.Refused, match="rolled back while it was being verified"):
        await imp.verify(done["run_id"], bj.paths(built))
    row = await run_row(done["run_id"])
    assert row.status == "rolled_back" and row.report["rollback"] and "verification" not in row.report


# --- Conflicts, tombstones, refusals ----------------------------------------------------------------------------


async def test_every_conflict_kind_and_its_dedupe(fx):
    async with session_scope() as s:
        s.add(Site(name="Main", url=bj.SITE_REGISTERED + "/"))
        s.add(Schedule(name="cc-daily-briefing", agent="supervisor", capability="daily_briefing", cron="52 7 * * 1-5"))
        same_title = await store.create_item(s, kind="knowledge", title="Notes the owner added.",
                                             body="Something else entirely.", actor="admin@test.local")
    first = await imp.run(bj.paths(fx), ALL)
    recs = await records(first["run_id"])
    assert recs[mapping.url_key(bj.SITE_REGISTERED)].action == "duplicate"
    assert recs[mapping.url_key(bj.SITE_NEW)].action == "conflict"
    modify_copy(fx)
    second = await imp.run(bj.paths(fx), ALL)
    async with session_scope() as s:
        conflicts = (await s.scalars(select(MemoryConflict).order_by(MemoryConflict.id))).all()
        by_kind = {}
        for c in conflicts:
            by_kind.setdefault(c.kind, []).append(c)
        assert set(by_kind) == {"memory_item", "source_changed", "instruction", "schedule", "site", "workflow"}
        clash = by_kind["memory_item"][0]
        assert clash.existing_id == same_title.id and clash.incoming_item_id
        incoming = await s.get(MemoryItem, clash.incoming_item_id)
        assert incoming.flags["conflict"] is True and incoming.title == "Notes the owner added."
        schedule = next(c for c in by_kind["schedule"] if c.existing_table == "schedules")
        assert "cc-daily-briefing" in schedule.existing["automation"]
        instruction = by_kind["instruction"][0]
        assert instruction.title.startswith("JARVIS rule that bypasses approval (")
        assert (await s.get(MemoryItem, instruction.incoming_item_id)).flags["approval_conflict"] is True
        assert by_kind["workflow"][0].incoming["source_fp"] != by_kind["workflow"][0].existing["source_fp"]
        assert by_kind["source_changed"][0].existing["id"] and by_kind["source_changed"][0].incoming_item_id
        assert {c.title for c in by_kind["site"]} == {"JARVIS watched planted-site-two.example",
                                                      "JARVIS watched planted-site-three.example"}
        assert all(c.dedupe_key for c in conflicts) and len({c.dedupe_key for c in conflicts}) == len(conflicts)
        total = len(conflicts)
        # A decided conflict is not asked again either.
        for c in by_kind["source_changed"]:
            c.status = "keep_existing"
    # source_changed twice: faq.md and websites.md (a new URL) both changed.
    assert second["report"]["conflicts"] == {"source_changed": 2, "workflow": 1, "site": 1}
    third = await imp.run(bj.paths(fx), ALL)
    assert third["report"]["conflicts"] == {}
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(MemoryConflict)) == total
        # The incoming version waiting for review is not created twice.
        assert await s.scalar(select(func.count()).select_from(MemoryItem).where(
            MemoryItem.flags["replaces_source_key"].astext == "jarvis:file:profile/knowledge/sarah/faq.md")) == 1
        # Nothing existing was changed by any import.
        assert (await s.get(MemoryItem, same_title.id)).version == 1
        assert await s.scalar(select(func.count()).select_from(Site)) == 1
        assert (await s.scalar(select(Schedule))).enabled is True


async def test_tombstoned_records_are_not_imported_again(built):
    times = source_messages(built, bj.S_TIMES)
    purged = ["jarvis:session:" + bj.S_CARRIED, times[1].key, "jarvis:file:profile/workspace/reports/scout-2026-10-05.md",
              "jarvis:job:a1b2c3", mapping.url_key(bj.SITE_NEW)]
    async with session_scope() as s:
        for key in purged:
            s.add(BrainTombstone(source_key=key, purged_by="admin@test.local"))
    done = await imp.run(bj.paths(built), ALL)
    recs = await records(done["run_id"])
    for key in purged:
        assert (recs[key].action, recs[key].reason) == ("excluded", "purged by an admin"), key
    for m in source_messages(built, bj.S_CARRIED):
        assert recs[m.key].reason == "purged by an admin"
    async with session_scope() as s:
        assert await s.scalar(select(ArchiveSession).where(ArchiveSession.source_session_id == bj.S_CARRIED)) is None
        assert await s.scalar(select(WorkflowDraft).where(WorkflowDraft.source_key == "jarvis:job:a1b2c3")) is None
        assert not (await s.scalars(select(MemoryConflict).where(
            MemoryConflict.source_key == mapping.url_key(bj.SITE_NEW)))).all()
    assert [m.position for m in await messages_of(bj.S_TIMES)] == [0, 2, 3]
    assert (await imp.verify(done["run_id"], bj.paths(built)))["passed"]


async def test_source_keys_and_reasons_never_hold_a_secret_or_contact_data(fx):
    """Keys are stored unmasked (records, conflicts, tombstones): a website line, a file name, a role id or a job name
    holding a webhook secret or contact data is keyed by a digest. verify reads keys and reasons too."""
    planted = {"hook": "PlantedHook" + "Secret42", "email": "jane.planted" + "@planted-client.example",
               "phone": "555-765-" + "4321"}
    hook = "https://hooks.slack.com/services/" + "/".join(("T0" + "PLANTED9", "B0" + "PLANTED9", planted["hook"]))
    prof = fx["profile"]
    with open(os.path.join(prof, "knowledge", "websites.md"), "a", encoding="utf-8") as f:
        f.write("- %s\n- https://planted-site-four.example/u/%s\n" % (hook, planted["email"]))
    with open(os.path.join(prof, "knowledge", "%s.md" % planted["email"]), "w", encoding="utf-8") as f:
        f.write("Notes about the customer's website.\n")
    with open(os.path.join(prof, "operations.md"), "a", encoding="utf-8") as f:
        f.write("<!-- jarvis-setup:role:%s:start -->\nCall them back on Mondays.\n<!-- jarvis-setup:role:%s:end -->\n"
                % (planted["phone"], planted["phone"]))
    jobs_path = os.path.join(prof, "cron", "jobs.json")
    with open(jobs_path, encoding="utf-8") as f:
        jobs = json.load(f)
    jobs["jobs"].append({"name": "watch the inbox of " + planted["email"], "prompt": "Check the inbox.",
                         "schedule": {"kind": "cron", "expr": "0 9 * * *"}, "enabled": False})
    with open(jobs_path, "w", encoding="utf-8") as f:
        json.dump(jobs, f)

    done = await imp.run(bj.paths(fx), ALL)
    assert (await imp.verify(done["run_id"], bj.paths(fx)))["passed"]
    recs = await records(done["run_id"])
    for url in (hook, "https://planted-site-four.example/u/" + planted["email"]):
        assert (recs[mapping.url_key(url)].action, recs[mapping.url_key(url)].reason) == (
            "excluded", "held a secret or personal data")
    assert any(k.startswith("jarvis:file:profile/path-sha256:") for k in recs)  # a path naming a person (source.py)
    assert any(k.startswith("jarvis:file:profile/operations.md#role-sha256-") for k in recs)
    assert any(k.startswith("jarvis:job:name:sha256-") for k in recs)
    for table, dump in (await bj.stored_text(bj.BRAIN_TABLES + ("audit_log",))).items():
        assert bj.planted_in(dump, planted) == [], table
    async with session_scope() as s:
        item = await s.scalar(select(MemoryItem).where(MemoryItem.title == "Notes about the customer's website."))
        assert item.source_ref == "knowledge/[email]"  # the address runs to the end of the name (.md is a TLD)

    # A key or a reason that holds one fails verify.
    async with session_scope() as s:
        record = await s.scalar(select(ImportRecord).where(ImportRecord.run_id == done["run_id"],
                                                           ImportRecord.record_type == "file").limit(1))
        reason = record.reason
        record.reason = "invalid record: " + planted["email"]
    result = await imp.verify(done["run_id"], bj.paths(fx))
    assert {k for k, c in result["verification"]["checks"].items() if not c["ok"]} == {"no_secrets_or_restricted_data"}
    async with session_scope() as s:
        await s.execute(update(ImportRecord).where(ImportRecord.id == record.id).values(
            reason=reason, source_key="jarvis:url:" + hook))
    where = (await imp.verify(done["run_id"], bj.paths(fx)))["verification"]["checks"][
        "no_secrets_or_restricted_data"]["where"]
    assert where == [{"table": "import_records", "id": record.id, "column": "source_key", "types": ["webhook_url"]}]


async def test_a_wal_file_or_a_different_discovery_count_is_refused_before_any_change(fx, tmp_path):
    before = await bj.table_counts()
    discovery = tmp_path / "discovery-summary.json"
    discovery.write_text(json.dumps({"database_backup": {"sessions": {"total": 35}, "messages": {"total": 1}}}))
    for call in (imp.plan(bj.paths(fx, discovery=str(discovery)), ALL),
                 imp.run(bj.paths(fx, discovery=str(discovery)), ALL)):
        with pytest.raises(imp.Refused, match="not the backup the kit analysed"):
            await call
    with open(fx["db"] + "-wal", "wb") as f:
        f.write(b"changes not yet in the backup")
    for call in (imp.plan(bj.paths(fx), ALL), imp.run(bj.paths(fx), ALL)):
        with pytest.raises(imp.Refused, match="-wal"):
            await call
    os.remove(fx["db"] + "-wal")
    with pytest.raises(imp.Refused, match="--all"):
        await imp.run(bj.paths(fx), imp.Selection(all=True, sample=3))
    with pytest.raises(imp.Refused, match="profile folder"):
        await imp.run(bj.paths({**fx, "profile": fx["profile"] + "-missing"}), ALL)
    assert await bj.table_counts() == before


async def test_the_sources_are_unchanged(built):
    before = bj.snapshot(built["root"])
    planned = await imp.plan(bj.paths(built), imp.Selection(sample=4))
    done = await imp.run(bj.paths(built), ALL)
    await imp.verify(done["run_id"], bj.paths(built))
    await imp.rollback(done["run_id"], confirm=True)
    assert planned and bj.snapshot(built["root"]) == before


async def test_a_date_range_selects_whole_sessions(built):
    planned = await imp.plan(bj.paths(built), imp.Selection(since="2026-11-01", until="2026-11-04", no_files=True))
    assert planned["totals"]["sessions"] == 4 and planned["totals"]["files"] == 0 and planned["totals"]["jobs"] == 0
    assert "file" not in planned["by_type"]


# --- rescan and review decisions ---------------------------------------------------------------------------------


async def test_rescan_recleans_stored_text_with_the_current_detectors(built):
    done = await imp.run(bj.paths(built), ALL)
    secret = bj.BRAIN_PLANTED["as_password"]
    async with session_scope() as s:  # what older detectors could have let through
        m = await s.scalar(select(ArchiveMessage).where(ArchiveMessage.content == "Please send the weekly report."))
        m.content = "use %s as the new password" % secret
        item = await s.scalar(select(MemoryItem).where(MemoryItem.import_run_id == done["run_id"],
                                                       MemoryItem.status == "pending_review").order_by(MemoryItem.id))
        item.body = item.body + "\nthe root password is " + secret
        await s.execute(update(MemoryItemVersion).where(MemoryItemVersion.item_id == item.id)
                        .values(body=item.body))
        restricted = await s.scalar(select(ArchiveMessage).where(ArchiveMessage.content == "Thanks."))
        restricted.content = "Thanks. Her SSN is 219-09-9999"
    dry = await imp.rescan(apply=False)
    assert dry["applied"] is False and dry["messages_changed"] == 1 and dry["items_changed"] == 1
    assert dry["sessions_withheld"] == 1 and dry["versions_changed"] >= 1
    assert secret in (await bj.stored_text(("archive_messages",)))["archive_messages"]
    applied = await imp.rescan(apply=True, actor="admin@test.local")
    assert applied["messages_changed"] == 1 and applied["sessions_withheld"] == 1
    for table, dump in (await bj.stored_text()).items():
        assert secret not in dump and "219-09-9999" not in dump, table
    async with session_scope() as s:
        item = await s.get(MemoryItem, item.id)
        assert item.status == "quarantined" and item.version == 2 and item.flags["secrets_redacted"]
        assert [v.change for v in await store.versions(s, item.id)] == ["redacted", "imported"]
        assert await s.scalar(select(AuditLog).where(AuditLog.action == "brain.rescanned")) is not None
    times = await session_of(bj.S_TIMES)
    assert (times.sensitivity, times.title, times.summary, times.imported_messages) == ("restricted", "", "", 0)
    again = await imp.rescan(apply=True)
    assert {k: v for k, v in again.items() if k != "applied" and v} == {}


async def test_review_decisions_carry_over_from_the_test_copy(built, tmp_path):
    test_copy = await imp.run(bj.paths(built), ALL)
    async with session_scope() as s:
        items = list((await s.scalars(select(MemoryItem).where(MemoryItem.import_run_id == test_copy["run_id"],
                                                               MemoryItem.source_key.is_not(None))
                                      .order_by(MemoryItem.id))).all())
        plain = [i for i in items if i.status == "pending_review" and i.sensitivity == "general"]
        quarantined = next(i for i in items if i.status == "quarantined")
        customer = next(i for i in items if i.status == "pending_review" and i.sensitivity == "customer")
        unedited_text = [i.body for i in (plain[0], plain[2], quarantined, customer)]
        await store.review_item(s, plain[0], decision="approve", actor="admin@test.local")
        await store.edit_item(s, plain[1], actor="admin@test.local", title="Edited on the test copy")
        await store.review_item(s, plain[1], decision="approve", actor="admin@test.local")
        await store.review_item(s, plain[2], decision="reject", actor="admin@test.local", note="not ours")
        await store.delete_item(s, plain[3], actor="admin@test.local")
        await store.review_item(s, quarantined, decision="approve", actor="admin@test.local")
        await store.edit_item(s, customer, actor="admin@test.local", body="Customer text edited on the test copy.")
        await store.review_item(s, customer, decision="approve", actor="admin@test.local")
        decided = {i.source_key: i.status for i in (plain[0], plain[1], plain[2], quarantined)}
        edited_key, deleted_key, customer_key = plain[1].source_key, plain[3].source_key, customer.source_key
    out = tmp_path / "decisions.json"
    assert await imp.decisions_export(str(out)) == {"exported": 6, "skipped_without_source_key": 0,
                                                    "edits_not_exported": 1, "removals": 0, "releases": 0}
    exported = json.loads(out.read_text())
    assert exported["format"] == "jarvis-brain-decisions" and len(exported["decisions"]) == 6
    # Decisions, not memory: only the text a person edited travels, and never a customer item's.
    decision = {d["source_key"]: d for d in exported["decisions"]}
    assert decision[edited_key]["title"] == "Edited on the test copy" and decision[edited_key]["edited"] is True
    assert {k for k, d in decision.items() if "body" in d or "title" in d} == {edited_key}
    assert decision[customer_key]["edited"] is True and decision[customer_key]["edit_not_exported"]
    flat = out.read_text()
    assert "Customer text edited" not in flat and not [t for t in unedited_text if json.dumps(t)[1:-1] in flat]
    with pytest.raises(imp.Refused, match="exists"):
        await imp.decisions_export(str(out))

    # Production: the same import into a database that has none of those decisions.
    await imp.rollback(test_copy["run_id"], discard_reviewed=True, confirm=True)
    production = await imp.run(bj.paths(built), ALL)
    run_id = production["run_id"]
    async with session_scope() as s:
        s.add(User(email="operator@test.local", password_hash="x", role="operator"))
        changed_since = await s.scalar(select(MemoryItem).where(MemoryItem.source_key == next(iter(decided))))
        await store.edit_item(s, changed_since, actor="admin@test.local", body=changed_since.body + "\nlocal change")
    with pytest.raises(imp.Refused, match="admin"):
        await imp.decisions_apply(str(out), run_id, actor="operator@test.local", apply=True)
    dry = await imp.decisions_apply(str(out), run_id, actor="admin@test.local", apply=False)
    assert dry["would_apply"] == 4
    result = await imp.decisions_apply(str(out), run_id, actor="admin@test.local", apply=True)
    assert result["applied"] == 4
    number = {d["source_key"]: n for n, d in enumerate(exported["decisions"], 1)}
    assert result["not_applied"] == {
        "the item differs from the test copy's import, or was changed since": [number[next(iter(decided))]],
        "edited on the test copy, but customer and Credit Repair text is never exported: edit and review it by hand":
            [number[customer_key]]}
    async with session_scope() as s:
        by_key = {i.source_key: i for i in (await s.scalars(select(MemoryItem).where(
            MemoryItem.import_run_id == run_id, MemoryItem.source_key.is_not(None)))).all()}
        for key, status in list(decided.items())[1:]:
            assert by_key[key].status == status, key
            notes = {v.note for v in await store.versions(s, by_key[key].id) if v.version > 1}
            assert notes == {imp.DECISION_NOTE}
        assert by_key[edited_key].title == "Edited on the test copy"
        assert by_key[deleted_key].deleted_at is not None
        assert (by_key[customer_key].status, by_key[customer_key].version) == ("pending_review", 1)
        assert await s.scalar(select(AuditLog).where(AuditLog.action == "brain.decisions_applied")) is not None
    again = await imp.decisions_apply(str(out), run_id, actor="admin@test.local", apply=True)
    assert again["applied"] == 0


async def test_withheld_messages_and_purged_items_carry_over_from_the_test_copy(built, tmp_path):
    """A message an admin withheld on the test copy, and an item an admin purged there, are withheld and purged in
    production too (whose database has never seen either decision), and stay so for later imports."""
    from app.brain import review

    test_copy = await imp.run(bj.paths(built), ALL)
    async with session_scope() as s:
        message = await s.scalar(
            select(ArchiveMessage).join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
            .where(ArchiveSession.sensitivity == "general", ArchiveMessage.withheld.is_(False),
                   ArchiveMessage.content != "", ArchiveMessage.source_key.is_not(None))
            .order_by(ArchiveMessage.id).limit(1))
        message_key, message_text = message.source_key, message.content
        await review.withhold_message(s, message, actor="admin@test.local")
        item = await s.scalar(select(MemoryItem).where(MemoryItem.import_run_id == test_copy["run_id"],
                                                       MemoryItem.status == "pending_review",
                                                       MemoryItem.sensitivity == "general",
                                                       MemoryItem.source_key.is_not(None)).order_by(MemoryItem.id)
                              .limit(1))
        item_key, item_body = item.source_key, item.body
        await store.delete_item(s, item, actor="admin@test.local")
        await review.purge(s, item, actor="admin@test.local")
    out = tmp_path / "decisions.json"
    assert (await imp.decisions_export(str(out)))["removals"] == 2
    exported = json.loads(out.read_text())
    assert exported["version"] == 4  # 0.6.2: a withhold carries the JARVIS row it names (keys and ids, no text)
    assert {(r["source_key"], r["reason"]) for r in exported["removals"]} == {
        (message_key, "withheld by an admin"), (item_key, imp.PURGED_BY_ADMIN)}
    flat = out.read_text()
    assert json.dumps(message_text)[1:-1] not in flat and json.dumps(item_body)[1:-1] not in flat  # keys, no text
    exported["removals"] += [{"source_key": "jarvis:message:never-imported:1", "reason": "withheld by an admin"},
                             {"source_key": "", "reason": "withheld by an admin"},
                             {"source_key": "jarvis:file:x", "reason": "something else"}]
    out.write_text(json.dumps(exported))

    # Production: a database that has neither decision (no tombstones), then the same import.
    await imp.rollback(test_copy["run_id"], discard_reviewed=True, confirm=True)
    async with session_scope() as s:
        await s.execute(BrainTombstone.__table__.delete())
    production = await imp.run(bj.paths(built), ALL)
    async with session_scope() as s:
        again = await s.scalar(select(ArchiveMessage).where(ArchiveMessage.source_key == message_key))
        assert (again.withheld, again.content) == (False, message_text)
        assert await s.scalar(select(MemoryItem).where(MemoryItem.source_key == item_key)) is not None
    dry = await imp.decisions_apply(str(out), production["run_id"], actor="admin@test.local", apply=False)
    assert dry["removals"] == {"withheld": 1, "purged": 1, "kept for later imports": 1}
    assert dry["removals_not_applied"] == {"not a removal": [4, 5]}
    async with session_scope() as s:  # the dry run changed nothing
        assert (await s.scalar(select(ArchiveMessage).where(ArchiveMessage.source_key == message_key))).content
        assert await s.scalar(select(func.count()).select_from(BrainTombstone)) == 0

    result = await imp.decisions_apply(str(out), production["run_id"], actor="admin@test.local", apply=True)
    assert result["removals"] == {"withheld": 1, "purged": 1, "kept for later imports": 1}
    async with session_scope() as s:
        withheld = await s.scalar(select(ArchiveMessage).where(ArchiveMessage.source_key == message_key))
        assert (withheld.withheld, withheld.content) == (True, "")
        assert "withheld_by_admin" in withheld.flags
        assert await s.scalar(select(MemoryItem).where(MemoryItem.source_key == item_key)) is None
        reasons = dict((await s.execute(select(BrainTombstone.source_key, BrainTombstone.reason))).all())
        assert reasons == {message_key: "withheld by an admin", item_key: imp.PURGED_BY_ADMIN,
                           "jarvis:message:never-imported:1": "withheld by an admin"}
        logged = await s.scalar(select(AuditLog).where(AuditLog.action == "brain.decisions_applied"))
        assert logged.detail["removals"] == {"withheld": 1, "purged": 1, "kept for later imports": 1}
    repeat = await imp.decisions_apply(str(out), production["run_id"], actor="admin@test.local", apply=True)
    assert repeat["removals"] == {"already done": 3}

    # A later import into production keeps both out.
    await imp.rollback(production["run_id"], discard_reviewed=True, confirm=True)
    later = await imp.run(bj.paths(built), ALL)
    async with session_scope() as s:
        back = await s.scalar(select(ArchiveMessage).where(ArchiveMessage.source_key == message_key))
        assert (back.withheld, back.content) == (True, "")
        assert await s.scalar(select(MemoryItem).where(MemoryItem.source_key == item_key)) is None
    assert later["run_id"] > production["run_id"]


async def test_a_heartbeat_is_never_used_to_decide_liveness(built):
    """An old heartbeat with the lock held is alive; a fresh heartbeat without the lock is dead."""
    holder = await imp.LockConnection.open()
    try:
        run_id = await holder.start_run(label="", actor="t", scope={}, source={})
        async with session_scope() as s:
            await s.execute(update(ImportRun).where(ImportRun.id == run_id)
                            .values(heartbeat_at=datetime(2020, 1, 1, tzinfo=UTC)))
        assert (await imp.status())["runs"][0]["status"] == "running"
        assert await imp.run_alive(run_id)
    finally:
        await holder.close()
    async with session_scope() as s:
        await s.execute(update(ImportRun).where(ImportRun.id == run_id).values(heartbeat_at=func.now()))
    assert (await imp.status())["marked_interrupted"] == [run_id]


def test_the_lock_key_is_the_spec_number_in_postgres_int_range():
    assert imp.LOCK_KEY % 2 ** 32 == imp.LOCK_CLASS % 2 ** 32 and -2 ** 31 <= imp.LOCK_KEY < 2 ** 31
    assert sqlite3.sqlite_version  # the backups are read with the standard library's SQLite
