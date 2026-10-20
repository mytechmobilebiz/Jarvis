"""Reading the JARVIS sources, read-only: the verified database backup, the restored profile and content folders,
and the scheduled jobs.

Nothing here writes anywhere or calls the network.
- The database backup is opened only through jarvis_kit.open_backup: read-only, immutable, query_only, and behind an
  authorizer that allows nothing but reading.
- Columns are always listed explicitly. Reasoning columns, the system prompt, the model and the cost and token
  columns are never selected; columns_read records what was.
- A backup with a non-empty -wal or -journal file next to it is refused: immutable=1 would silently ignore the
  changes that file holds.
- Folders are walked with the kit, which never follows a link. Files are opened only for reading, and files the
  importer never imports (secrets, databases, caches, programs, logs, transcripts) are not opened at all.

Only identifiers, counts, hashes and the text the importer cleans leave this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cached_property
from typing import Any
from zoneinfo import ZoneInfo

from . import STAFF, STAFF_AGENT
from . import jarvis_kit as kit
from . import text as brain_text

# display_name is not read: on a gateway it is the name of the person JARVIS chatted with, never a title.
SESSION_COLUMNS = ("id", "title", "source", "started_at", "ended_at", "end_reason", "parent_session_id",
                   "model_config", "message_count", "tool_call_count", "archived")
MESSAGE_COLUMNS = ("id", "session_id", "role", "content", "tool_name", "tool_call_id", "tool_calls", "timestamp",
                   "active", "compacted", "message_uid", "absorbed_message_uids")
REQUIRED = {"sessions": ("id",), "messages": ("id", "session_id", "role", "content")}
# Never selected, whatever the lists above say (a second guard: the lists are code, but so is this).
FORBIDDEN_COLUMN = re.compile(r"(?i)^(?:system_prompt|model|.*(?:cost|token).*)$")

# Tool names of customer-facing work: their messages are customer data by default (spec 0.5).
CUSTOMER_TOOL = re.compile(r"(?i)mail|smtp|imap|whatsapp|sms|message|lead|crm|contact|proposal|invoice|client|customer")
# The same work done through a generic tool: a tool call's arguments (a terminal command, a script's options) or a
# file path in a tool result that names it, as a word or a path part, or names Sarah's files and scripts.
CUSTOMER_WORK = re.compile(
    r"(?i)(?<![a-z])(?:(?:e-?|g)?mails?|mailbox(?:es)?|smtp|imap|himalaya|whatsapp|sms|leads?|crm|contacts?"
    r"|proposals?|invoices?|clients?|customers?|sarah)(?![a-z])")
PATH = re.compile(r"[^\s\"'`<>|]*[/\\][^\s\"'`<>|]+")
# Gateway channels where JARVIS talked with people outside the business: every message of their sessions is customer
# data by default. The owner's own Slack, the CLI and scheduled runs are not; "(other)" is a source value that held
# personal data (kit.label()).
CUSTOMER_CHANNEL = re.compile(
    r"(?i)^\(other\)$|whatsapp|sms|mms|mail|telegram|messenger|facebook|instagram|signal|imessage|wechat|viber"
    r"|discord|webchat|livechat|twilio|voice|phone")
NUMERIC = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
# Profile categories that are never imported, so never opened (the live state.db, secrets, caches...).
NOT_OPENED = {"secret", "database", "cache", "program", "temporary", "log", "session_transcript"}


class SourceRefused(Exception):
    """A source check failed before anything was written. The message is written here and holds no source data."""


# --- Small helpers ----------------------------------------------------------------------------------------------


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(kit.CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(*parts: object) -> str:
    """A fingerprint of several values (None, numbers and JSON kept distinct)."""
    joined = "\x1f".join(json.dumps(p, sort_keys=True, ensure_ascii=False, default=str) for p in parts)
    return hashlib.sha256(joined.encode("utf-8", "replace")).hexdigest()


def check_backup(path: str) -> dict[str, Any]:
    """The backup's identity for import_runs.source, after the checks that come before any reading."""
    if not path or not os.path.isfile(path) or os.path.islink(path):
        raise SourceRefused("the database backup does not exist (or is a link)")
    for suffix in ("-wal", "-journal"):
        side = path + suffix
        if os.path.exists(side) and os.path.getsize(side) > 0:
            raise SourceRefused(
                "a non-empty %s file sits next to the database backup: the backup may not hold every change, and "
                "reading it read-only would ignore that file. Make the backup again with the kit (it checkpoints the "
                "database first)." % suffix)
    with open(path, "rb") as f:
        if f.read(16) != b"SQLite format 3\x00":
            raise SourceRefused("the database backup is not an SQLite database")
    return {"name": kit.label(os.path.basename(path), "(backup file)"), "bytes": os.path.getsize(path),
            "sha256": sha256_file(path), "wal_present": False}


def parse_time(value: Any, tz: ZoneInfo) -> tuple[datetime | None, bool]:
    """(UTC time, assumed local) for a JARVIS time value.

    A number (or numeric text) is seconds below 1e11, milliseconds below 1e14 and microseconds below 1e17; anything
    larger is not a time. ISO text without a zone is taken as the customer time zone (assumed_local). Anything else
    is None.
    """
    if value is None or isinstance(value, bool):
        return None, False
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None, False
        if NUMERIC.match(s):
            try:
                value = float(s)
            except ValueError:
                return None, False
        else:
            try:
                parsed = datetime.fromisoformat(s)
            except ValueError:
                return None, False
            if parsed.tzinfo is None:
                try:
                    return parsed.replace(tzinfo=tz).astimezone(UTC), True
                except (OverflowError, ValueError):
                    return None, False
            try:
                return parsed.astimezone(UTC), False
            except (OverflowError, ValueError):
                return None, False
    if isinstance(value, (int, float)):
        v = float(value)
        if v != v or v < 0:
            return None, False
        if v < 1e11:
            seconds = v
        elif v < 1e14:
            seconds = v / 1e3
        elif v < 1e17:
            seconds = v / 1e6
        else:
            return None, False
        try:
            return datetime.fromtimestamp(seconds, UTC), False
        except (OverflowError, OSError, ValueError):
            return None, False
    return None, False


def parse_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """[{name, arguments}] from both shapes Hermes has stored: OpenAI's (function.name / function.arguments) and the
    flat one (name / arguments). Arguments stay as given (text or JSON); the caller cleans them."""
    if raw is None:
        return []
    data = raw
    if isinstance(raw, (bytes, str)):
        s = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        if not s.strip() or s.strip() in ("[]", "null", "{}"):
            return []
        try:
            data = json.loads(s)
        except ValueError:
            return [{"name": "", "arguments": s}]
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    out = []
    for call in data:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else None
        name = (fn or call).get("name")
        args = (fn or call).get("arguments", (fn or call).get("args", ""))
        out.append({"name": name if isinstance(name, str) else "", "arguments": args})
    return out


def parse_uid_list(value: Any) -> tuple[str, ...]:
    """The unique non-empty string uids of a JSON list (Hermes absorbed_message_uids), in order; anything else is ()
    (as Hermes's own _uid_list reads it)."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return ()
    if not isinstance(value, list):
        return ()
    return tuple(dict.fromkeys(u for u in value if isinstance(u, str) and u))


def customer_tool(name: str) -> bool:
    return bool(name) and bool(CUSTOMER_TOOL.search(name))


def customer_work(text: str) -> bool:
    """Arguments or a command that name customer work ("scripts/send_proposal.py --lead ...")."""
    return bool(text) and bool(CUSTOMER_WORK.search(text))


def customer_path(text: str) -> bool:
    """A tool result that names a file of customer work ("workspace/proposals/...", "content/leads/...")."""
    return any(CUSTOMER_WORK.search(path) for path in PATH.findall(text or ""))


def customer_channel(channel: str) -> bool:
    return bool(channel) and bool(CUSTOMER_CHANNEL.search(channel))


# --- The scheduled jobs (cron/jobs.json and the setup marker) ----------------------------------------------------


@dataclass
class JobSpec:
    index: int  # position in jobs.json (0-based)
    id: str | None  # a safe job id, or None
    name: str
    prompt: str
    skills: list[str]
    expr: str  # the schedule expression as given ('' if none)
    display: str
    enabled: bool | None
    deliver: str  # platform prefix only
    template_job: bool
    occurrence: int = 1  # the n-th job in jobs.json with this id: a later one is a duplicate, keyed apart

    @property
    def key(self) -> str:
        """By the job's id (a repeated id gets #<n>); a job without a safe id by its name (or a digest of it,
        text.key_part) and position."""
        if not self.id:
            return "jarvis:job:name:%s#%d" % (brain_text.key_part(self.name), self.index)
        return "jarvis:job:%s" % self.id if self.occurrence == 1 else "jarvis:job:%s#%d" % (self.id, self.occurrence)

    @property
    def report_name(self) -> str:
        return self.name if self.template_job else "custom job %d" % (self.index + 1)


def read_jobs(text: str | None, template_jobs: dict[str, Any]) -> list[JobSpec]:
    """The jobs in cron/jobs.json: {"jobs": [...]} or a bare list."""
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return []
    out = []
    seen: dict[str, int] = {}
    for n, job in enumerate(j for j in jobs if isinstance(j, dict)):
        name = job.get("name") if isinstance(job.get("name"), str) else ""
        schedule = job.get("schedule")
        expr, display = "", ""
        if isinstance(schedule, dict):
            expr = schedule.get("expr") or ""
            display = schedule.get("display") or ""
        elif isinstance(schedule, str):
            expr = schedule
        expr = expr or job.get("schedule_expr") or job.get("cron") or ""
        display = display or job.get("schedule_display") or ""
        deliver = job.get("deliver")
        deliver = kit.label(deliver.split(":")[0], "(other)") if isinstance(deliver, str) and deliver else ""
        job_id = job.get("id") if isinstance(job.get("id"), str) and kit.SAFE_JOB_ID.match(job["id"]) else None
        if job_id:
            seen[job_id] = seen.get(job_id, 0) + 1
        out.append(JobSpec(
            index=n, id=job_id, name=name, prompt=job.get("prompt") if isinstance(job.get("prompt"), str) else "",
            skills=[s for s in job.get("skills") or [] if isinstance(s, str)] if isinstance(job.get("skills"), list)
            else [], expr=expr if isinstance(expr, str) else "", display=display if isinstance(display, str) else "",
            enabled=job.get("enabled") if isinstance(job.get("enabled"), bool) else None, deliver=deliver,
            template_job=name in template_jobs, occurrence=seen[job_id] if job_id else 1))
    return out


def job_names_by_id(jobs: list[JobSpec], marker_text: str | None) -> dict[str, str]:
    """Job id -> job name, from jobs.json and from the setup marker (cron/jarvis-setup-registered.json, which maps
    names to ids, or holds {"jobs": [...]}). A job id used twice in jobs.json names its first job."""
    names: dict[str, str] = {}
    for j in jobs:
        if j.id:
            names.setdefault(j.id, j.name)
    if marker_text:
        try:
            marker = json.loads(marker_text)
        except ValueError:
            marker = None
        if isinstance(marker, dict) and isinstance(marker.get("jobs"), list):
            for j in marker["jobs"]:
                if isinstance(j, dict) and isinstance(j.get("id"), str) and isinstance(j.get("name"), str):
                    names.setdefault(j["id"], j["name"])
        elif isinstance(marker, dict):
            for name, job_id in marker.items():
                if isinstance(name, str) and isinstance(job_id, str) and kit.SAFE_JOB_ID.match(job_id):
                    names.setdefault(job_id, name)
    return names


@dataclass
class Attribution:
    staff: str
    agent: str
    origin: str  # "scheduled" or "chat"
    job_id: str
    job_name: str
    how: str  # "job id" or "default"


def attribute(session_id: str, job_names: dict[str, str], template_jobs: dict[str, Any]) -> Attribution:
    """Which JARVIS staff member a session belongs to. Only scheduled-job sessions can be attributed (their id names
    the job); every other session is Jarvis's by default. Shown as inferred."""
    m = kit.CRON_SESSION.match(session_id or "")
    if not m:
        return Attribution("jarvis", STAFF_AGENT["jarvis"], "chat", "", "", "default")
    job_id = m.group(1) if kit.SAFE_JOB_ID.match(m.group(1)) else ""
    name = job_names.get(job_id, "")
    role = (template_jobs.get(name) or {}).get("role") if name else None
    if role in STAFF:
        return Attribution(role, STAFF_AGENT[role], "scheduled", job_id, name, "job id")
    return Attribution("jarvis", STAFF_AGENT["jarvis"], "scheduled", job_id, name, "default")


# --- The database backup ----------------------------------------------------------------------------------------


@dataclass
class SourceSession:
    id: str
    title: str
    channel: str
    started_at: datetime | None
    ended_at: datetime | None
    end_reason: str
    parent_id: str
    lineage: str
    hermes_archived: bool
    hermes_counts: dict[str, int]
    attribution: Attribution
    time_assumed_local: bool

    @property
    def key(self) -> str:
        return "jarvis:session:%s" % self.id

    def sort_key(self) -> tuple[Any, ...]:
        return (self.started_at is None, self.started_at or datetime.min.replace(tzinfo=UTC), self.id)


@dataclass
class SourceMessage:
    id: Any
    session_id: str
    role: str
    content: str
    tool_name: str
    tool_call_id: str
    tool_calls_raw: Any
    at: datetime | None
    time_assumed_local: bool
    active: int | None
    compacted: int | None
    key: str
    position: int = 0
    uid: str = ""  # Hermes message_uid ('' when the backup has none): one logical message across its copies
    absorbed: tuple[str, ...] = ()  # uids a fold (micro-compaction) merged into this row

    @property
    def fp(self) -> str:
        """The source fingerprint: a later backup with a different one changed this row."""
        return sha256_text(self.role, self.content, self.tool_name, self.tool_calls_raw, self.active, self.compacted)

    @property
    def text_size(self) -> int:
        """Characters of text (content and tool calls): what the batch bound of 20 MB counts."""
        return len(self.content) + len(str(self.tool_calls_raw or ""))


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _as_flag(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class Backup:
    """The JARVIS database backup, open read-only for one command."""

    def __init__(self, path: str, tz: ZoneInfo, job_names: dict[str, str], template_jobs: dict[str, Any]) -> None:
        self.path = path
        self.tz = tz
        self.job_names = job_names
        self.template_jobs = template_jobs
        try:
            self.con = kit.open_backup(path)
        except kit.Refused as exc:
            raise SourceRefused("the database backup could not be opened read-only") from exc
        try:
            tables = {r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        except sqlite3.Error as exc:
            raise SourceRefused("the database backup could not be read (%s)" % exc.__class__.__name__) from exc
        if "sessions" not in tables or "messages" not in tables:
            raise SourceRefused("the database backup has no sessions or no messages table")
        self.columns = {"sessions": self._columns("sessions", SESSION_COLUMNS),
                        "messages": self._columns("messages", MESSAGE_COLUMNS)}
        self._dup_uid_first: dict[str, Any] | None = None
        self._sessions: list[SourceSession] | None = None
        self._row_ids: dict[str, set[str]] = {}

    def close(self) -> None:
        self.con.close()

    def _columns(self, table: str, wanted: tuple[str, ...]) -> list[str]:
        present = {row[1] for row in self.con.execute("PRAGMA table_info(%s)" % kit.q(table)).fetchall()}
        missing = [c for c in REQUIRED[table] if c not in present]
        if missing:
            raise SourceRefused("the backup's %s table lacks the column(s) %s" % (table, ", ".join(missing)))
        cols = [c for c in wanted if c in present]
        assert not any(kit.REASONING_COLUMN.search(c) or FORBIDDEN_COLUMN.match(c) for c in cols)
        return cols

    @property
    def columns_read(self) -> dict[str, list[str]]:
        return {t: list(c) for t, c in self.columns.items()}

    def _select(self, table: str) -> str:
        return ", ".join(kit.q(c) for c in self.columns[table])

    # Totals ------------------------------------------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        return {"sessions": self.con.execute("SELECT count(*) FROM sessions").fetchone()[0],
                "messages": self.con.execute("SELECT count(*) FROM messages").fetchone()[0]}

    def message_counts(self) -> dict[str, int]:
        return {_as_text(sid): n for sid, n in
                self.con.execute("SELECT session_id, count(*) FROM messages GROUP BY session_id")}

    # Sessions --------------------------------------------------------------------------------------------

    def sessions(self) -> list[SourceSession]:
        """Every session, in time order: (started_at IS NULL, started_at, id)."""
        if self._sessions is not None:
            return self._sessions
        cols = self.columns["sessions"]
        rows = [dict(zip(cols, r, strict=True)) for r in
                self.con.execute("SELECT %s FROM sessions" % self._select("sessions"))]
        end_reasons = {_as_text(r["id"]): _as_text(r.get("end_reason")) for r in rows}
        out = []
        for r in rows:
            sid = _as_text(r["id"])
            started, local_a = parse_time(r.get("started_at"), self.tz)
            ended, local_b = parse_time(r.get("ended_at"), self.tz)
            parent = _as_text(r.get("parent_session_id"))
            lineage = ""
            config = {}
            if r.get("model_config"):
                try:
                    loaded = json.loads(_as_text(r["model_config"]))
                    config = loaded if isinstance(loaded, dict) else {}
                except ValueError:
                    config = {}
            if isinstance(config.get("_delegate_from"), str) and config["_delegate_from"]:
                lineage, parent = "delegated", parent or config["_delegate_from"]
            elif isinstance(config.get("_branched_from"), str) and config["_branched_from"]:
                lineage, parent = "branch", parent or config["_branched_from"]
            elif parent and end_reasons.get(parent) == "branched":
                lineage = "branch"
            elif parent and end_reasons.get(parent) == "compression":
                lineage = "continuation"
            counts = {k: int(r[k]) for k in ("message_count", "tool_call_count")
                      if isinstance(r.get(k), (int, float)) and not isinstance(r.get(k), bool)}
            out.append(SourceSession(
                id=sid, title=_as_text(r.get("title")),
                channel=kit.label(r.get("source"), "(other)") if r.get("source") is not None else "",
                started_at=started, ended_at=ended, end_reason=kit.label(r.get("end_reason"), "(other)")
                if r.get("end_reason") else "", parent_id=parent, lineage=lineage,
                hermes_archived=bool(_as_flag(r.get("archived"))), hermes_counts=counts,
                attribution=attribute(sid, self.job_names, self.template_jobs),
                time_assumed_local=local_a or local_b))
        out.sort(key=SourceSession.sort_key)
        self._sessions = out
        return out

    # Messages --------------------------------------------------------------------------------------------

    def _dup_uids(self) -> dict[str, Any]:
        """message_uid values used by more than one row -> the smallest id using it (that row keeps the uid key)."""
        if self._dup_uid_first is None:
            self._dup_uid_first = {}
            if "message_uid" in self.columns["messages"]:
                for uid, first in self.con.execute(
                        "SELECT message_uid, min(id) FROM messages WHERE message_uid IS NOT NULL AND message_uid != '' "
                        "GROUP BY message_uid HAVING count(*) > 1"):
                    self._dup_uid_first[_as_text(uid)] = first
        return self._dup_uid_first

    def message_key(self, row_id: Any, session_id: str, uid: Any) -> str:
        uid_text = _as_text(uid)
        if uid_text:
            first = self._dup_uids().get(uid_text)
            if first is None or first == row_id:
                return "jarvis:message:uid:%s" % uid_text
        return "jarvis:message:%s:%s" % (session_id, row_id)

    def _message(self, r: dict[str, Any]) -> SourceMessage:
        sid = _as_text(r["session_id"])
        at, local = parse_time(r.get("timestamp"), self.tz)
        return SourceMessage(
            id=r["id"], session_id=sid, role=_as_text(r.get("role")), content=_as_text(r.get("content")),
            tool_name=_as_text(r.get("tool_name")), tool_call_id=_as_text(r.get("tool_call_id")),
            tool_calls_raw=r.get("tool_calls"), at=at, time_assumed_local=local,
            active=_as_flag(r["active"]) if "active" in r else 1,
            compacted=_as_flag(r["compacted"]) if "compacted" in r else None,
            key=self.message_key(r["id"], sid, r.get("message_uid")), uid=_as_text(r.get("message_uid")),
            absorbed=parse_uid_list(r.get("absorbed_message_uids")))

    def _order(self) -> str:
        return "id, timestamp" if "timestamp" in self.columns["messages"] else "id"

    def messages(self, session_id: str) -> list[SourceMessage]:
        """Every source row of one session, ORDER BY id (timestamp only breaks ties), with its 0-based position."""
        cols = self.columns["messages"]
        out = []
        for i, row in enumerate(self.con.execute(
                "SELECT %s FROM messages WHERE session_id = ? ORDER BY %s" % (self._select("messages"), self._order()),
                (session_id,))):
            m = self._message(dict(zip(cols, row, strict=True)))
            m.position = i
            out.append(m)
        return out

    def has_row(self, session_id: str, row_id: str) -> bool:
        """Whether this JARVIS row (session id, row id as text) is in the backup. The ids of a session are read once."""
        ids = self._row_ids.get(session_id)
        if ids is None:
            ids = self._row_ids[session_id] = {_as_text(r[0]) for r in self.con.execute(
                "SELECT id FROM messages WHERE session_id = ?", (session_id,))}
        return str(row_id) in ids

    def rows(self, session_id: str, row_ids: list[str] | tuple[str, ...] | set[str]) -> list[SourceMessage]:
        """These rows of one session (ids as text), with their keys; their positions are not set."""
        wanted = sorted({str(r) for r in row_ids if r})
        out: list[SourceMessage] = []
        cols = self.columns["messages"]
        for i in range(0, len(wanted), 400):
            chunk = wanted[i:i + 400]
            for row in self.con.execute(
                    "SELECT %s FROM messages WHERE session_id = ? AND id IN (%s) ORDER BY %s"
                    % (self._select("messages"), ",".join("?" * len(chunk)), self._order()), (session_id, *chunk)):
                out.append(self._message(dict(zip(cols, row, strict=True))))
        return out

    def sessions_by_id(self) -> dict[str, SourceSession]:
        return {s.id: s for s in self.sessions()}

    def orphans(self) -> Iterator[SourceMessage]:
        """Messages whose session is not in the sessions table, by id."""
        cols = self.columns["messages"]
        for row in self.con.execute(
                "SELECT %s FROM messages m WHERE m.session_id IS NULL OR NOT EXISTS "
                "(SELECT 1 FROM sessions s WHERE s.id = m.session_id) ORDER BY %s"
                % (", ".join("m." + kit.q(c) for c in cols), self._order())):
            yield self._message(dict(zip(cols, row, strict=True)))

    def job_history(self, job_id: str) -> dict[str, Any]:
        """Every session of one scheduled job in the backup, whatever was selected: counts and dates only."""
        runs = [s for s in self.sessions() if s.attribution.job_id == job_id and job_id]
        dates = sorted(s.started_at for s in runs if s.started_at)
        problems = 0
        ids = [s.id for s in runs]
        words = ("error", "failed", "failure", "exception", "traceback", "timed out", "unauthorized", "forbidden",
                 "denied")
        cond = " OR ".join("lower(content) LIKE ?" for _ in words)
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            problems += self.con.execute(
                "SELECT count(DISTINCT session_id) FROM messages WHERE session_id IN (%s) AND role = 'tool' AND (%s)"
                % (",".join("?" * len(chunk)), cond), (*chunk, *("%" + w + "%" for w in words))).fetchone()[0]

        def day(d: datetime) -> str:
            return d.astimezone(self.tz).date().isoformat()
        return {"runs": len(runs), "first": day(dates[0]) if dates else None, "last": day(dates[-1]) if dates else None,
                "with_problems": problems, "last_runs": [day(d) for d in dates[-5:]][::-1],
                "problems_matched_by": "keywords in tool results", "from": "the JARVIS backup"}


# --- The profile and content folders ----------------------------------------------------------------------------


@dataclass
class SourceFile:
    area: str  # "profile" or "content"
    rel: str
    full: str
    size: int
    mtime: float
    mtime_ns: int
    category: str
    sha256: str = ""  # of the raw bytes; "" when the file was not opened
    text: str | None = None
    too_large: bool = False
    unreadable: bool = False
    template: dict[str, Any] = field(default_factory=dict)
    placeholders: int = 0

    @cached_property
    def key(self) -> str:
        """jarvis:file:<area>/<rel>; the path's digest when the path names a person or holds restricted data, or holds
        anything else a key may not (text.key_part), so no record stores it."""
        if self.private_path:
            return "jarvis:file:%s/path-sha256:%s" % (self.area, sha256_text(self.rel))
        return "jarvis:file:%s/%s" % (self.area, brain_text.key_part(self.rel))

    @property
    def name(self) -> str:
        return self.rel.rsplit("/", 1)[-1]

    @cached_property
    def path(self) -> brain_text.Cleaned:
        """The path, cleaned (text.clean_path()): what a source_ref or a title may show of it."""
        return brain_text.clean_path(self.rel)

    @property
    def private_path(self) -> bool:
        return bool(self.path.restricted or self.path.masked)

    @property
    def shown_rel(self) -> str:
        return self.path.text

    @property
    def shown_name(self) -> str:
        return self.path.text.rsplit("/", 1)[-1]

    @property
    def is_text(self) -> bool:
        return self.text is not None


def read_no_follow(full: str, listed: os.stat_result) -> tuple[str, bytes | None]:
    """kit.read_file(), opened so that a file replaced by a link since the folder was listed is never followed
    (O_NOFOLLOW where the system has it), and refused if it is no longer the regular file that was listed."""
    fd = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (listed.st_dev, listed.st_ino):
            raise OSError("the file changed since the folder was listed")
        digest, kept, keep = hashlib.sha256(), bytearray(), st.st_size <= kit.TEXT_SCAN_LIMIT
        for block in iter(lambda: f.read(kit.CHUNK), b""):
            digest.update(block)
            if keep:
                kept += block
    return digest.hexdigest(), (bytes(kept) if keep and len(kept) <= kit.TEXT_SCAN_LIMIT else None)


def walk_area(area: str, root: str, templates: dict[str, Any]) -> tuple[list[SourceFile], list[dict[str, str]]]:
    """Every regular file under root (links are never followed), classified and, when it could be imported, read
    and compared with the JARVIS templates."""
    files, skipped = kit.walk(root)
    out = []
    for rel, full, st in files:
        category = kit.classify(area, rel)
        f = SourceFile(area=area, rel=rel, full=full, size=st.st_size, mtime=st.st_mtime, mtime_ns=st.st_mtime_ns,
                       category=category)
        out.append(f)
        if category in NOT_OPENED:
            continue
        try:
            digest, raw = read_no_follow(full, st)
        except OSError:
            f.unreadable = True
            continue
        f.sha256 = digest
        if raw is None:
            f.too_large = True
            continue
        text = kit.as_text(rel, raw)
        f.text = text
        if category in kit.REVIEW_CATEGORIES or area == "content":
            normalized = hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else digest
            f.template = kit.compare_with_templates(area, rel, normalized, text, templates)
        if text is not None:
            f.placeholders = len(kit.PLACEHOLDER_MARKERS.findall(text))
    return out, skipped


def manifest_sha256(files: list[SourceFile], skipped: list[dict[str, str]]) -> str:
    """One hash for the restored folders: path, size and content hash of every file (size and modification time for
    files that are never opened), and the links that were skipped."""
    lines = sorted("%s\t%s\t%d\t%s" % (f.area, f.rel, f.size, f.sha256 or "mtime:%d" % f.mtime_ns) for f in files)
    lines += sorted("link\t%s\t%s" % (s.get("area", ""), s.get("path", "")) for s in skipped)
    return hashlib.sha256("\n".join(lines).encode("utf-8", "replace")).hexdigest()
