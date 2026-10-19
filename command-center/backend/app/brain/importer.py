"""The JARVIS import (spec section 2): plan, run, resume, verify, rollback, status, rescan and review decisions.

Imported text is reference data. Nothing here approves, enables or schedules anything: memory items arrive pending
review (or quarantined), workflow drafts never run, and an existing Command Center record is never overwritten: a
clash becomes a memory_conflicts row for a person to decide. Nothing calls the network or an AI model.

How a run stays safe
- One source record, one import_records row per run (spec 2.6), so every record is accounted for and a run can be
  verified and rolled back exactly.
- plan reads the target database in a read-only transaction and writes nothing.
- run, resume and rollback hold the advisory lock (7314002213, run id) for their whole life, and only one of them runs
  at a time: the lock (7314002213, 0), plus the unique index on running rows. PostgreSQL's two-key advisory locks
  take 32-bit keys, so 7314002213 is folded into that range (LOCK_KEY). A run is alive only while its lock is held;
  the heartbeat (every 30 seconds) is shown to people but never used to decide. A running row whose lock is free is
  marked interrupted by whoever notices.
- Every status change is conditional (UPDATE ... WHERE id = :id AND status = :expected); zero rows ends the command.
- Batches are bounded (25 sessions, 2,000 messages or 20 MB of text; 200 file or job records). A batch is one
  transaction with its target rows, its import_records and the checkpoint {phase, index}. A single session larger
  than the bounds is written alone, in a batch of its own, so a conversation is never split.
- Each source record is written inside a SAVEPOINT (a session's new messages share one, and are written again one
  savepoint each if the database refuses any of them). A BrainError, DataError, IntegrityError or a cleaning failure
  excludes that record ("invalid record: <class>") and the run continues; any other error fails the run with
  "<class> in <phase> at <location>", where the location is a source key only when it holds no name or personal
  data (otherwise "<area> #<n>").
- Console output, errors, audit entries and reports carry counts, ids, kinds and safe labels only.

Conflict effects (memory_conflicts.effects): what a person's resolution changed in rows that already existed, so a
rollback of the run that raised the conflict can put them back. The API writes it when a conflict is resolved;
rollback_run() reverts it. The format is exactly:

    {"items":  [{"id": int, "status_before": str, "version_before": int, "version_after": int}],
     "sites":  [{"id": int, "updated_at_after": "<ISO 8601 time>"}],
     "drafts": [{"id": int, "before": {<workflow_drafts fields before the change>}, "fp_after": str}],
     "purged_items": [{"id": int, "source_key": str | null}]}

- items: the item's version after the resolution (version_after) and before it (version_before). Rollback reverts an
  item only while its current version is still version_after: it restores status_before and, when the content
  differs, the title, body, tags, kind and agent of version_before (and, for a source_changed conflict, the
  source_sha256 and source_at the importer recorded in the conflict's "existing"), as a new version with change
  "reverted".
- sites: a site the resolution registered. Rollback deletes it only while its updated_at still equals
  updated_at_after.
- drafts: a draft the resolution replaced. Rollback restores "before" only while the draft's source_fp still equals
  fp_after.
- purged_items: incoming items the resolution purged; they are reported, nothing is restored.
If any target moved on since, the rollback refuses and lists the conflicts. Effects on rows the rolled-back run
created itself are skipped: those rows are deleted anyway.

Items promoted from archive messages (origin "promoted") have no import run and survive a rollback; when their
message is deleted, their source_ref is marked "source removed". They are matched by flags.message_id, or by the
source_ref "JARVIS session <source id> · message <position>".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg
from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy import text as sql
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..config import get_settings
from ..db import sessionmaker
from ..models import (
    ArchiveMessage,
    ArchiveSession,
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
from . import IMPORTER_VERSION
from . import jarvis_kit as kit
from . import mapping
from . import source as src
from . import store
from . import text as brain_text
from .archive import recompute_session
from .policy import RISK_FLAGS, SENSITIVE_AGENT

LOCK_CLASS = 7_314_002_213
LOCK_KEY = ((LOCK_CLASS + 2 ** 31) % 2 ** 32) - 2 ** 31  # the same number as a signed 32-bit lock key
GLOBAL_LOCK = 0  # (LOCK_KEY, 0): one import, rollback or rescan at a time
HEARTBEAT_SECONDS = 30.0
BATCH_SESSIONS = 25
BATCH_MESSAGES = 2_000
BATCH_TEXT = 20_000_000
BATCH_RECORDS = 200
DEFAULT_ACTOR = "importer (cli)"
PHASES = ("files", "jobs", "sessions", "orphans")
CHAT_IN, CHAT_OUT = 200_000, 50_000
TOOL_IN, TOOL_OUT = 20_000, 4_000
ARG_OUT = 500
CARRIED = "carried-on copy of a compacted message"
REWOUND = "rewound or superseded"
DECISION_NOTE = "decision carried over from the test copy"
BRAIN_TABLES = ("memory_items", "archive_sessions", "archive_messages", "workflow_drafts")
SESSION_SOURCE_FIELDS = ("title", "started_at", "ended_at", "end_reason", "source_message_count", "hermes_archived",
                         "channel", "lineage", "parent_source_session_id", "staff", "agent", "origin", "job_id",
                         "job_name", "flags")
MESSAGE_FIELDS = ("source_message_id", "source_fp", "position", "role", "content", "content_chars", "truncated",
                  "tool_name", "tool_call_id", "tool_calls", "at", "state", "sensitivity", "withheld", "redactions",
                  "labels", "flags")
DATETIME_FIELDS = {"at", "started_at", "ended_at", "created_at", "updated_at"}
MESSAGE_COLUMNS = [c for c in ArchiveMessage.__table__.c if c.name != "search"]


class Refused(Exception):
    """Refused before any change (exit code 2). The message and ids are safe to show."""

    def __init__(self, message: str, ids: list[int] | tuple[int, ...] = ()) -> None:
        super().__init__(message)
        self.ids = list(ids)


class ImportFailed(Exception):
    """The run failed or was interrupted (exit code 1); its row says why. The message is safe to show."""


class RunLost(Exception):
    """The run's status changed under it (a conditional update matched no row)."""


def now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def jsonable(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items() if k != "search"}


def unjson(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (datetime.fromisoformat(v) if k in DATETIME_FIELDS and isinstance(v, str) else v)
            for k, v in row.items()}


# --- What is selected --------------------------------------------------------------------------------------------


@dataclass
class Selection:
    sample: int | None = None
    files_sample: int | None = None
    all: bool = False
    since: str | None = None
    until: str | None = None
    no_files: bool = False

    def validate(self) -> None:
        if self.all and (self.sample or self.since or self.until):
            raise Refused("--all selects everything: leave out --sample, --since and --until")
        if self.all and self.files_sample is not None:
            raise Refused("--all imports every file: leave out --files-sample")
        if not (self.all or self.sample or self.since or self.until):
            raise Refused("choose what to import: --sample N, --all, or --since DATE --until DATE")
        if self.sample is not None and self.sample < 1:
            raise Refused("--sample must be at least 1")
        if self.files_sample is not None and self.files_sample < 0:
            raise Refused("--files-sample must be 0 or more")
        for name in ("since", "until"):
            value = getattr(self, name)
            if value is not None:
                try:
                    date.fromisoformat(value)
                except ValueError:
                    raise Refused("--%s must be a date (YYYY-MM-DD)" % name) from None

    @property
    def files_limit(self) -> int | None:
        """None: every file; 0: none; n: a sample of n files (memory entries always come with a sample)."""
        if self.no_files:
            return 0
        if self.files_sample is not None:
            return self.files_sample
        if self.sample:
            return 10
        return None

    @property
    def mode(self) -> str:
        return "all" if self.all else "sample" if self.sample else "range"

    def to_json(self) -> dict[str, Any]:
        return {"mode": self.mode, "sample": self.sample, "files_sample": self.files_sample, "all": self.all,
                "since": self.since, "until": self.until, "no_files": self.no_files}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Selection:
        return cls(sample=data.get("sample"), files_sample=data.get("files_sample"), all=bool(data.get("all")),
                   since=data.get("since"), until=data.get("until"), no_files=bool(data.get("no_files")))


@dataclass
class SourcePaths:
    db: str
    profile: str
    content: str | None = None
    discovery: str | None = None


class Sources:
    """The JARVIS sources, open read-only for one command."""

    def __init__(self, paths: SourcePaths, tz: ZoneInfo, tz_name: str) -> None:
        if not paths.profile or not os.path.isdir(paths.profile) or os.path.islink(paths.profile):
            raise Refused("the profile folder does not exist")
        if paths.content and (not os.path.isdir(paths.content) or os.path.islink(paths.content)):
            raise Refused("the content folder does not exist")
        self.paths = paths
        self.tz, self.tz_name = tz, tz_name
        try:
            self.db_info = src.check_backup(paths.db)
        except src.SourceRefused as exc:
            raise Refused(str(exc)) from None
        self.templates = kit.load_templates()
        self.files, self.skipped = [], []
        for area, root in (("profile", paths.profile), ("content", paths.content)):
            if root:
                found, skipped = src.walk_area(area, root, self.templates)
                self.files += found
                self.skipped += [dict(s, area=area) for s in skipped]
        self.files.sort(key=lambda f: (f.area, f.rel))
        self.manifest = src.manifest_sha256(self.files, self.skipped)
        profile = {f.rel: f for f in self.files if f.area == "profile"}
        template_jobs = self.templates.get("cron_jobs", {})
        jobs_file = profile.get("cron/jobs.json")
        self.jobs = src.read_jobs(jobs_file.text if jobs_file else None, template_jobs)
        marker = profile.get("cron/jarvis-setup-registered.json")
        self.job_names = src.job_names_by_id(self.jobs, marker.text if marker else None)
        config = profile.get("config.yaml")
        self.cron_mode = ""
        if config and config.text:
            self.cron_mode = kit.yaml_keys(config.text)["values"].get("approvals.cron_mode", "")
        self.skill_texts = {f.rel.split("/")[1]: f.text for f in profile.values()
                            if f.category == "skill" and f.text is not None and f.rel.count("/") == 2
                            and f.rel.lower().endswith("/skill.md")}
        self.watchdog = profile.get(mapping.WATCHDOG_SCRIPT)
        try:
            self.backup = src.Backup(paths.db, tz, self.job_names, template_jobs)
        except src.SourceRefused as exc:
            raise Refused(str(exc)) from None
        self.counts = self.backup.counts()
        self.discovery = self._discovery_check(paths.discovery)

    def close(self) -> None:
        self.backup.close()

    def _discovery_check(self, path: str | None) -> dict[str, Any]:
        if not path:
            return {"checked": False}
        try:
            with open(path, "rb") as f:
                data = json.load(f)
        except (OSError, ValueError):
            raise Refused("the discovery summary could not be read as JSON") from None
        db = (data.get("database_backup") or {}) if isinstance(data, dict) else {}
        tables = db.get("tables") or {}
        expected = {
            "sessions": (db.get("sessions") or {}).get("total", (tables.get("sessions") or {}).get("rows")),
            "messages": (db.get("messages") or {}).get("total", (tables.get("messages") or {}).get("rows")),
        }
        result = {"checked": True, **{k: {"backup": self.counts[k], "discovery": expected[k]} for k in expected}}
        if any(expected[k] != self.counts[k] for k in expected):
            raise Refused("the backup holds %d sessions and %d messages, the discovery summary %s and %s: this is not "
                          "the backup the kit analysed" % (self.counts["sessions"], self.counts["messages"],
                                                           expected["sessions"], expected["messages"]))
        result["ok"] = True
        return result

    def source_info(self, pre_backup_sha256: str = "") -> dict[str, Any]:
        if pre_backup_sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", pre_backup_sha256):
            raise Refused("--pre-backup-sha256 must be a SHA-256 (64 hexadecimal characters)")
        return {"db": self.db_info, "profile": {"manifest_sha256": self.manifest, "files": len(self.files),
                                                "content_folder": bool(self.paths.content)},
                "columns_read": self.backup.columns_read, "pre_backup_sha256": pre_backup_sha256.lower()}

    def same_source(self, recorded: dict[str, Any]) -> bool:
        return ((recorded.get("db") or {}).get("sha256") == self.db_info["sha256"]
                and (recorded.get("profile") or {}).get("manifest_sha256") == self.manifest)


@dataclass
class Work:
    sessions: list[src.SourceSession]
    files: list[src.SourceFile]
    jobs: bool
    orphans: bool
    totals: dict[str, int] = field(default_factory=dict)


IMPORTABLE_SAMPLE = ("knowledge", "instructions", "skill", "script", "plugin_or_hook", "workspace")


def select_work(sources: Sources, selection: Selection) -> Work:
    sessions = sources.backup.sessions()
    if selection.since or selection.until:
        lo = datetime.combine(date.fromisoformat(selection.since), time.min, sources.tz) if selection.since else None
        hi = (datetime.combine(date.fromisoformat(selection.until) + timedelta(days=1), time.min, sources.tz)
              if selection.until else None)
        sessions = [s for s in sessions if s.started_at is not None and (lo is None or s.started_at >= lo)
                    and (hi is None or s.started_at < hi)]
    if selection.sample:
        n, k = len(sessions), min(selection.sample, len(sessions))
        sessions = [sessions[i * n // k] for i in range(k)] if k else []
    limit = selection.files_limit
    if limit == 0:
        files: list[src.SourceFile] = []
    elif limit is None:
        files = list(sources.files)
    else:
        profile = [f for f in sources.files if f.area == "profile"]
        memory = [f for f in profile if f.category == "memory"]
        by_category: dict[str, list[src.SourceFile]] = {}
        for f in profile:
            if f.category in IMPORTABLE_SAMPLE and any(not p.excluded for p in mapping.file_parts(f, sources.tz)):
                by_category.setdefault(f.category, []).append(f)
        picked: list[src.SourceFile] = []
        while len(picked) < limit and any(by_category.values()):
            for cat in sorted(by_category):
                if by_category[cat] and len(picked) < limit:
                    picked.append(by_category[cat].pop(0))
        files = sorted(memory + picked, key=lambda f: (f.area, f.rel))
    counts = sources.backup.message_counts()
    work = Work(sessions=sessions, files=files, jobs=limit != 0, orphans=selection.all)
    known = {s.id for s in sources.backup.sessions()}
    orphans = sum(n for sid, n in counts.items() if sid not in known) if selection.all else 0
    urls = sum(1 for f in files if f.rel.lower() == "knowledge/websites.md"
               for p in mapping.file_parts(f, sources.tz) if p.record_type == "url")
    jobs = (len(sources.jobs) + (1 if sources.watchdog else 0)) if work.jobs else 0
    work.totals = {"sessions": len(sessions), "messages": sum(counts.get(s.id, 0) for s in sessions) + orphans,
                   "orphans": orphans, "files": len(files), "jobs": jobs, "urls": urls}
    return work


# --- The report (spec 2.8) ---------------------------------------------------------------------------------------


def _empty_tally() -> dict[str, Any]:
    return {"by_action": {}, "by_reason": {}, "by_type": {}, "by_agent": {}, "by_staff": {}, "by_kind": {},
            "by_sensitivity": {"messages": {}, "items": {}, "sessions": {}},
            "secrets": {"types": {}, "where": {"messages": 0, "files": 0, "drafts": 0}},
            "restricted": {"types": {}, "sessions_withheld": 0, "files_excluded": 0},
            "masked": {"email_address": 0, "phone_number": 0}, "conflicts": {}, "drafts": [],
            "time_range": {"first": None, "last": None}}


class Tally:
    """Counts for the report. Kept in the checkpoint, so a resumed run reports the whole run."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.d = json.loads(json.dumps(data)) if data else _empty_tally()

    @staticmethod
    def _inc(bucket: dict[str, int], key: str, n: int = 1) -> None:
        bucket[key] = bucket.get(key, 0) + n

    def record(self, record_type: str, action: str, reason: str) -> None:
        self._inc(self.d["by_action"], action)
        self._inc(self.d["by_type"], record_type)
        if reason:
            self._inc(self.d["by_reason"], reason)

    def secrets(self, types: dict[str, int], where: str) -> None:
        if types:
            self.d["secrets"]["where"][where] += 1
            for k, v in types.items():
                self._inc(self.d["secrets"]["types"], k, v)

    def masked(self, masked: dict[str, int]) -> None:
        for k, v in masked.items():
            self._inc(self.d["masked"], k, v)

    def restricted(self, types: list[str] | set[str], *, session: bool = False, file: bool = False) -> None:
        for t in types:
            self._inc(self.d["restricted"]["types"], t)
        if session:
            self.d["restricted"]["sessions_withheld"] += 1
        if file:
            self.d["restricted"]["files_excluded"] += 1

    def created(self, *, agent: str | None = None, staff: str | None = None, kind: str | None = None,
                sensitivity: tuple[str, str] | None = None) -> None:
        if agent:
            self._inc(self.d["by_agent"], agent)
        if staff:
            self._inc(self.d["by_staff"], staff)
        if kind:
            self._inc(self.d["by_kind"], kind)
        if sensitivity:
            self._inc(self.d["by_sensitivity"][sensitivity[0]], sensitivity[1])

    def conflict(self, kind: str) -> None:
        self._inc(self.d["conflicts"], kind)

    def time(self, at: datetime | None) -> None:
        if at is None:
            return
        value = at.isoformat()
        tr = self.d["time_range"]
        if tr["first"] is None or value < tr["first"]:
            tr["first"] = value
        if tr["last"] is None or value > tr["last"]:
            tr["last"] = value

    def report(self, selection: Selection, totals: dict[str, int], discovery: dict[str, Any],
               missing_from_source: int) -> dict[str, Any]:
        return {"selection": selection.to_json(), "totals": totals, **json.loads(json.dumps(self.d)),
                "missing_from_source": missing_from_source, "discovery_check": discovery,
                "importer_version": IMPORTER_VERSION}


def write_report(path: str | None, report: dict[str, Any]) -> None:
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, sort_keys=True)


# --- Locks and liveness ------------------------------------------------------------------------------------------


def _dsn() -> str:
    return get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


class LockConnection:
    """A connection of its own, outside the pool, that holds the advisory locks: closing it releases them, and a
    pooled connection can never be handed on with a lock still held."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn
        self._io = asyncio.Lock()
        self._heartbeat: asyncio.Task[None] | None = None

    @classmethod
    async def open(cls) -> LockConnection:
        return cls(await asyncpg.connect(_dsn()))

    async def try_lock(self, objid: int) -> bool:
        async with self._io:
            return bool(await self.conn.fetchval("SELECT pg_try_advisory_lock($1::int, $2::int)", LOCK_KEY, objid))

    async def start_run(self, *, label: str, actor: str, scope: dict[str, Any], source: dict[str, Any]) -> int:
        """The new run's row and its lock, in one transaction: by the time others can see the running row, its lock
        is already held."""
        async with self._io, self.conn.transaction():
            try:
                run_id = await self.conn.fetchval(
                    "INSERT INTO import_runs (source_system, label, status, scope, source, importer_version, "
                    "created_by) VALUES ('jarvis', $1, 'running', $2::jsonb, $3::jsonb, $4, $5) RETURNING id",
                    label, json.dumps(scope), json.dumps(source), IMPORTER_VERSION, actor)
            except asyncpg.UniqueViolationError:
                raise Refused("another import is running") from None
            await self.conn.execute("SELECT pg_advisory_lock($1::int, $2::int)", LOCK_KEY, run_id)
        return int(run_id)

    def start_heartbeat(self, run_id: int, seconds: float) -> None:
        async def beat() -> None:
            while True:
                await asyncio.sleep(seconds)
                async with self._io:
                    await self.conn.execute(
                        "UPDATE import_runs SET heartbeat_at = now() WHERE id = $1 AND status = 'running'", run_id)
        self._heartbeat = asyncio.ensure_future(beat())

    async def close(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            try:
                await self._heartbeat
            except (asyncio.CancelledError, Exception):  # the heartbeat's own end, or a lost connection
                pass
        try:
            await self.conn.close()
        except Exception:  # the locks go with the connection either way
            self.conn.terminate()


async def try_xact_lock(session: AsyncSession, objid: int) -> bool:
    """The advisory lock (LOCK_KEY, objid) until the session's transaction ends, if nobody holds it."""
    return bool(await session.scalar(sql("SELECT pg_try_advisory_xact_lock(CAST(:k AS int), CAST(:i AS int))"),
                                     {"k": LOCK_KEY, "i": objid}))


async def mark_dead_runs(session: AsyncSession) -> list[int]:
    """Running rows whose lock is free are interrupted: whoever notices marks them. Commits."""
    marked = []
    for run_id in (await session.scalars(select(ImportRun.id).where(ImportRun.status == "running"))).all():
        if await try_xact_lock(session, run_id):
            result = await session.execute(update(ImportRun).where(ImportRun.id == run_id,
                                                                    ImportRun.status == "running")
                                           .values(status="interrupted", finished_at=now()))
            if result.rowcount:
                marked.append(run_id)
    await session.commit()
    return marked


async def set_status(session: AsyncSession, run_id: int, expected: str | tuple[str, ...], **values: Any) -> None:
    allowed = (expected,) if isinstance(expected, str) else expected
    result = await session.execute(update(ImportRun).where(ImportRun.id == run_id, ImportRun.status.in_(allowed))
                                   .values(**values))
    if not result.rowcount:
        raise RunLost()


async def run_alive(run_id: int) -> bool:
    """Whether a run's lock is held right now (by its own process)."""
    conn = await asyncpg.connect(_dsn())
    try:
        free = await conn.fetchval("SELECT pg_try_advisory_lock($1::int, $2::int)", LOCK_KEY, run_id)
        if free:
            await conn.execute("SELECT pg_advisory_unlock($1::int, $2::int)", LOCK_KEY, run_id)
        return not free
    finally:
        await conn.close()


async def require_schema(session: AsyncSession) -> None:
    if not await session.scalar(sql("SELECT to_regclass('public.import_runs') IS NOT NULL")):
        raise Refused("the JARVIS Brain tables are not installed (migration 0006_brain): run migrate first")


async def customer_timezone(session: AsyncSession) -> tuple[str, ZoneInfo]:
    from .. import customer_settings

    name = (await customer_settings.load(session, get_settings())).get("timezone") or "America/New_York"
    try:
        return str(name), ZoneInfo(str(name))
    except Exception:  # an unknown zone name: UTC, and the report says so through the scope
        return "UTC", ZoneInfo("UTC")


# --- Processing (shared by plan and run) -------------------------------------------------------------------------


SAFE_KEY = re.compile(r"^[A-Za-z0-9_.:\-]{1,120}$")


def safe_location(key: str, record_type: str, index: int) -> str:
    """A source key for an error message, unless it could name a customer or hold personal data."""
    if record_type in ("session", "message", "orphan", "job", "watchdog") and SAFE_KEY.match(key):
        personal = any(True for name in ("email_address", "phone_number")
                       for _ in kit.matches(kit.PERSONAL_PATTERNS[name], name, key))
        restricted = any(True for name, rx in brain_text.RESTRICTED_PATTERNS.items()
                         for _ in kit.matches(rx, name, key))
        e164 = re.search(r"\+?[0-9]{10,15}", key.rsplit(":", 1)[-1]) and record_type != "job"
        if not personal and not restricted and not kit.count(kit.SECRET_PATTERNS, key) and not e164:
            return key
    return "%s #%d" % (record_type, index + 1)


@dataclass
class MessageClean:
    content: str
    truncated: bool
    calls: list[dict[str, str]]
    sensitivity: str
    redactions: dict[str, int]
    masked: dict[str, int]
    flags: dict[str, Any]
    labels: list[str]
    restricted: list[str]


@dataclass
class MessagePlan:
    m: src.SourceMessage
    action: str  # import, duplicate, excluded
    reason: str = ""
    cleaned: MessageClean | None = None
    duplicate_of: str = ""


class Processor:
    """Turns selected source records into target rows and import records. With write=False (plan) it changes
    nothing and only counts; both modes take the same decisions."""

    def __init__(self, session: AsyncSession, sources: Sources, *, run_id: int | None, tally: Tally, write: bool,
                 actor: str = DEFAULT_ACTOR) -> None:
        self.s = session
        self.src = sources
        self.run_id = run_id
        self.t = tally
        self.write = write
        self.actor = actor
        self.tz = sources.tz
        self.location = ""
        self.phase = ""
        self.pending: list[dict[str, Any]] = []
        self._fake = 0
        self.tombstones: set[str] = set()
        self.site_urls: dict[str, int] = {}
        self.schedules: list[dict[str, Any]] = []
        self.new_hashes: dict[str, int] = {}
        self.new_titles: dict[tuple[str, str, str], int] = {}
        self.new_conflicts: dict[str, int] = {}
        self.drafts: list[mapping.DraftPlan] = []

    async def load(self, work: Work) -> None:
        self.tombstones = set((await self.s.scalars(select(BrainTombstone.source_key))).all())
        for site_id, url in (await self.s.execute(select(Site.id, Site.url))).all():
            normalized = mapping.normalize_url(url or "")
            if normalized:
                self.site_urls.setdefault(normalized, site_id)
        self.schedules = [{"id": r.id, "name": r.name, "agent": r.agent, "capability": r.capability, "cron": r.cron}
                          for r in (await self.s.execute(select(Schedule.id, Schedule.name, Schedule.agent,
                                                                Schedule.capability, Schedule.cron))).all()]
        if work.jobs:
            for job in self.src.jobs:
                history = self.src.backup.job_history(job.id) if job.id else {"runs": 0, "from": "the JARVIS backup"}
                self.drafts.append(mapping.draft_for_job(
                    job, skill_texts=self.src.skill_texts, cron_mode=self.src.cron_mode, tz_name=self.src.tz_name,
                    history=history, schedules=self.schedules))
            if self.src.watchdog is not None:
                self.drafts.append(mapping.draft_for_watchdog(self.src.watchdog, cron_mode=self.src.cron_mode,
                                                              tz_name=self.src.tz_name, schedules=self.schedules))

    # Records ------------------------------------------------------------------------------------------------

    def fake_id(self) -> int:
        self._fake -= 1
        return self._fake

    def record(self, key: str, record_type: str, action: str, reason: str = "", table: str = "",
               target: int | None = None, before: dict[str, Any] | None = None) -> None:
        self.t.record(record_type, action, reason)
        if self.write:
            self.pending.append({"run_id": self.run_id, "source_key": key, "record_type": record_type,
                                 "action": action, "reason": reason[:500], "target_table": table,
                                 "target_id": target if target and target > 0 else None, "before": before or {}})

    async def flush(self) -> None:
        if self.pending:
            await self.s.execute(insert(ImportRecord), self.pending)
            self.pending = []

    async def guarded(self, fn: Callable[[], Awaitable[Any]]) -> tuple[bool, Any]:
        """fn inside a SAVEPOINT: a record the database or the memory service refuses is rolled back alone."""
        try:
            if not self.write:
                return True, await fn()
            async with self.s.begin_nested():
                return True, await fn()
        except (store.BrainError, DataError, IntegrityError) as exc:
            return False, exc.__class__.__name__

    async def conflict(self, kind: str, title: str, source_key: str, incoming_sha: str, *, existing_table: str = "",
                       existing_id: int | None = None, incoming_item_id: int | None = None,
                       existing: dict[str, Any] | None = None, incoming: dict[str, Any] | None = None
                       ) -> tuple[int | None, bool]:
        """(conflict id, created). The same question (kind, source, incoming content) is asked once, whatever
        happened to the earlier one."""
        dedupe = hashlib.sha256("\x1f".join((kind, source_key, incoming_sha)).encode("utf-8")).hexdigest()
        if dedupe in self.new_conflicts:
            return self.new_conflicts[dedupe], False
        found = await self.s.scalar(select(MemoryConflict.id).where(MemoryConflict.dedupe_key == dedupe))
        if found:
            return found, False
        title = brain_text.one_line(mapping.clean(title, "chat").text, 300) or kind
        if self.write:
            row = MemoryConflict(run_id=self.run_id, kind=kind, title=title, source_key=source_key, dedupe_key=dedupe,
                                 existing_table=existing_table, existing_id=existing_id,
                                 incoming_item_id=incoming_item_id if incoming_item_id and incoming_item_id > 0
                                 else None, existing=existing or {}, incoming=incoming or {})
            self.s.add(row)
            await self.s.flush()
            conflict_id = row.id
        else:
            conflict_id = self.fake_id()
        self.new_conflicts[dedupe] = conflict_id
        self.t.conflict(kind)
        return conflict_id, True

    # Files --------------------------------------------------------------------------------------------------

    async def do_file(self, f: src.SourceFile, index: int) -> int:
        parts = mapping.file_parts(f, self.tz)
        for p in parts:
            self.location = safe_location(p.key, p.record_type, index)
            if p.record_type == "url":
                await self.do_url(p)
            else:
                await self.do_part(p)
        return len(parts)

    async def _hash_owner(self, content_hash: str) -> int | None:
        if content_hash in self.new_hashes:
            return self.new_hashes[content_hash]
        return await self.s.scalar(select(MemoryItem.id).where(MemoryItem.content_hash == content_hash,
                                                               MemoryItem.deleted_at.is_(None)).limit(1))

    async def _title_owner(self, kind: str, agent: str, title: str, content_hash: str) -> int | None:
        key = (kind, agent, title.lower())
        if key in self.new_titles:
            return self.new_titles[key]
        return await self.s.scalar(select(MemoryItem.id).where(
            MemoryItem.kind == kind, MemoryItem.agent == agent, func.lower(MemoryItem.title) == title.lower(),
            MemoryItem.content_hash != content_hash, MemoryItem.deleted_at.is_(None)).limit(1))

    async def do_part(self, p: mapping.Part) -> None:
        def rec(action: str, reason: str = "", target: int | None = None) -> None:
            self.record(p.key, p.record_type, action, reason, "memory_items" if target else "", target)

        if p.key in self.tombstones:
            return rec("excluded", "purged by an admin")
        if p.excluded:
            return rec("excluded", p.excluded)
        if p.duplicate_of_earlier:
            return rec("duplicate", "the same text appears earlier in the file")
        try:
            cleaned = mapping.clean(p.raw, p.text_kind)
        except Exception as exc:  # a cleaning failure excludes this record only
            return rec("excluded", "invalid record: %s" % exc.__class__.__name__)
        if cleaned.withheld:
            self.t.restricted(cleaned.restricted, file=True)
            return rec("excluded", "restricted data")
        body, truncated = mapping.cut(cleaned.text, store.MAX_BODY)
        if not body.strip():
            return rec("excluded", "empty")
        title = p.title or mapping.title_from(body, p.fallback_title)
        flags: dict[str, Any] = {**p.flags, **{k: v for k, v in cleaned.flags().items() if k != "restricted"},
                                 "text_kind": p.text_kind}
        if truncated:
            flags["truncated"] = True
        instr = brain_text.instruction_flags(title + "\n" + body)
        if instr:
            flags["instruction_like"] = instr
        approval_conflict = p.kind == "instruction" and bool({"approval_bypass", "autonomous_action"} & set(instr))
        if approval_conflict:
            flags["approval_conflict"] = True
        sensitivity = "customer" if cleaned.masked or p.customer else "general"
        status = "quarantined" if cleaned.secret_types or any(flags.get(f) for f in RISK_FLAGS) else "pending_review"
        content_hash = brain_text.content_hash(p.kind, p.agent, title, body)
        source_key: str | None = p.key
        clash: tuple[str, MemoryItem | int] | None = None
        existing = await self.s.scalar(select(MemoryItem).where(MemoryItem.source_key == p.key))
        if existing is not None:
            if existing.source_sha256 == p.sha256:
                return rec("duplicate", "already imported", existing.id)
            if p.sha256 in ((existing.flags or {}).get("rejected_source_sha256") or []):
                return rec("duplicate", "incoming version rejected before", existing.id)
            waiting = await self.s.scalar(select(MemoryItem.id).where(
                MemoryItem.source_key.is_(None), MemoryItem.source_sha256 == p.sha256,
                MemoryItem.flags["replaces_source_key"].astext == p.key).limit(1))
            same = waiting or await self._hash_owner(content_hash)
            if same:
                return rec("duplicate", "incoming version already waiting for review", same)
            source_key = None
            flags["replaces_source_key"] = p.key
            clash = ("source_changed", existing)
        else:
            same = await self._hash_owner(content_hash)
            if same:
                return rec("duplicate", "same content as an existing item", same)
            other = await self._title_owner(p.kind, p.agent, title, content_hash)
            if other:
                flags["conflict"] = True
                clash = ("memory_item", other)
        try:
            store._check(p.kind, p.agent, title, body, [])
        except store.BrainError:
            return rec("excluded", "invalid record: BrainError")

        async def write() -> int:
            if self.write:
                item = await store.create_item(
                    self.s, kind=p.kind, title=title, body=body, actor=self.actor, agent=p.agent, status=status,
                    origin="import", flags=flags, sensitivity=sensitivity, source_system="jarvis",
                    source_ref=p.source_ref, source_key=source_key, source_sha256=p.sha256, source_at=p.source_at,
                    import_run_id=self.run_id, change="imported", note="imported from JARVIS", already_clean=True)
                item_id = item.id
            else:
                item_id = self.fake_id()
            if clash and clash[0] == "source_changed":
                old = clash[1]
                assert isinstance(old, MemoryItem)
                await self.conflict(
                    "source_changed", "JARVIS changed %s since it was imported" % p.source_ref, p.key, p.sha256,
                    existing_table="memory_items", existing_id=old.id, incoming_item_id=item_id,
                    existing={"id": old.id, "title": old.title, "version": old.version,
                              "source_sha256": old.source_sha256, "source_at": _iso(old.source_at)},
                    incoming={"id": item_id, "title": title, "source_sha256": p.sha256})
            elif clash:
                await self.conflict(
                    "memory_item", "Same title as an existing item: %s" % title, p.key, p.sha256,
                    existing_table="memory_items", existing_id=int(clash[1]),  # type: ignore[arg-type]
                    incoming_item_id=item_id, existing={"id": clash[1]}, incoming={"id": item_id, "title": title})
            if approval_conflict:
                await self.conflict(
                    "instruction", "JARVIS rule that bypasses approval (%s)" % p.source_ref, p.key, p.sha256,
                    existing_table="", incoming_item_id=item_id, incoming={"id": item_id, "rules": instr})
            return item_id

        ok, result = await self.guarded(write)
        if not ok:
            return rec("excluded", "invalid record: %s" % result)
        item_id = int(result)
        self.new_hashes[content_hash] = item_id
        self.new_titles[(p.kind, p.agent, title.lower())] = item_id
        self.t.created(agent=p.agent or "(shared)", kind=p.kind, sensitivity=("items", sensitivity))
        self.t.secrets(cleaned.secret_types, "files")
        self.t.masked(cleaned.masked)
        if clash and clash[0] == "source_changed":
            rec("conflict", "changed since an earlier import (the new version waits for review)", item_id)
        elif clash:
            rec("conflict", "same title as an existing item (both kept for review)", item_id)
        else:
            rec("imported", "quarantined (held a secret or a risky rule)" if status == "quarantined" else "", item_id)

    async def do_url(self, p: mapping.Part) -> None:
        if p.key in self.tombstones:
            return self.record(p.key, "url", "excluded", "purged by an admin")
        if p.excluded:
            return self.record(p.key, "url", "excluded", p.excluded)
        if p.url in self.site_urls:
            return self.record(p.key, "url", "duplicate", "site already registered", "sites", self.site_urls[p.url])
        host = urllib.parse.urlsplit(p.url).hostname or ""

        async def write() -> tuple[int | None, bool]:
            return await self.conflict("site", "JARVIS watched %s" % host, p.key, p.sha256, existing_table="sites",
                                       incoming={"url": p.url, "host": host})
        ok, result = await self.guarded(write)
        if not ok:
            return self.record(p.key, "url", "excluded", "invalid record: %s" % result)
        conflict_id, created = result
        if created:
            self.record(p.key, "url", "conflict", "not a registered site (a person decides)", "memory_conflicts",
                        conflict_id)
        else:
            self.record(p.key, "url", "duplicate", "already waiting for a decision", "memory_conflicts", conflict_id)

    # Jobs ---------------------------------------------------------------------------------------------------

    async def do_draft(self, d: mapping.DraftPlan, index: int) -> None:
        self.location = safe_location(d.key, d.record_type, index)
        if d.key in self.tombstones:
            return self.record(d.key, d.record_type, "excluded", "purged by an admin")
        existing = await self.s.scalar(select(WorkflowDraft).where(WorkflowDraft.source_key == d.key))
        fp = d.fields["source_fp"]
        if existing is not None:
            if existing.source_fp == fp:
                return self.record(d.key, d.record_type, "duplicate", "already imported", "workflow_drafts",
                                   existing.id)

            async def ask() -> tuple[int | None, bool]:
                return await self.conflict(
                    "workflow", "JARVIS job %s changed since it was imported" % d.fields["name"], d.key, fp,
                    existing_table="workflow_drafts", existing_id=existing.id,
                    existing={"id": existing.id, "source_fp": existing.source_fp, "status": existing.status},
                    incoming=d.fields)
            ok, result = await self.guarded(ask)
            if not ok:
                return self.record(d.key, d.record_type, "excluded", "invalid record: %s" % result)
            conflict_id, created = result
            return self.record(d.key, d.record_type, "conflict" if created else "duplicate",
                               "changed since an earlier import (a person decides)" if created
                               else "already waiting for a decision", "memory_conflicts", conflict_id)

        async def write() -> int:
            if self.write:
                draft = WorkflowDraft(source_system="jarvis", source_key=d.key, status="draft",
                                      import_run_id=self.run_id, **d.fields)
                self.s.add(draft)
                await self.s.flush()
                draft_id = draft.id
            else:
                draft_id = self.fake_id()
            if d.overlaps:
                same = next((s for s in self.schedules if d.fields["capability"]
                             and s["agent"] == d.fields["agent"] and s["capability"] == d.fields["capability"]), None)
                await self.conflict(
                    "schedule", "JARVIS job %s overlaps a Command Center automation" % d.fields["name"], d.key, fp,
                    existing_table="schedules" if same else "", existing_id=same["id"] if same else None,
                    existing={"automation": d.overlaps},
                    incoming={"draft_id": draft_id, "agent": d.fields["agent"], "capability": d.fields["capability"]})
            return draft_id

        ok, result = await self.guarded(write)
        if not ok:
            return self.record(d.key, d.record_type, "excluded", "invalid record: %s" % result)
        self.t.created(agent=d.fields["agent"])
        self.t.secrets(d.secret_types, "drafts")
        if d.restricted:
            self.t.restricted(d.restricted)
        self.t.d["drafts"].append({"name": d.report_name, "compatibility": d.fields["compatibility"],
                                   "enabled_in_source": d.fields["enabled_in_source"]})
        self.record(d.key, d.record_type, "imported", "disabled draft", "workflow_drafts", result)

    # Sessions -----------------------------------------------------------------------------------------------

    def exclusion(self, m: src.SourceMessage) -> str:
        if m.key in self.tombstones:
            return "purged by an admin"
        if m.role == "system":
            return "system prompt"
        if m.active == 0 and m.compacted in (0, None):
            return REWOUND
        if not m.content.strip() and not src.parse_tool_calls(m.tool_calls_raw) and not m.tool_name:
            return "empty"
        return ""

    @staticmethod
    def dup_fp(m: src.SourceMessage) -> str:
        raw = m.tool_calls_raw
        try:
            calls = json.dumps(json.loads(raw), sort_keys=True) if isinstance(raw, str) and raw.strip() else raw
        except ValueError:
            calls = raw
        return src.sha256_text(m.role, m.tool_name, brain_text.content_hash(m.content), calls)

    def clean_message(self, m: src.SourceMessage, staff: str) -> MessageClean:
        tool = m.role == "tool"
        c = mapping.clean(m.content[:TOOL_IN if tool else CHAT_IN], "chat")
        content, cut_ = mapping.cut(c.text, TOOL_OUT if tool else CHAT_OUT)
        truncated = cut_ or len(m.content) > (TOOL_IN if tool else CHAT_IN)
        total = brain_text.Cleaned("", dict(c.secret_types), list(c.restricted), dict(c.masked), c.think_removed)
        calls = []
        for call in src.parse_tool_calls(m.tool_calls_raw):
            args = call["arguments"]
            args_text = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            ac = mapping.clean(args_text[:TOOL_IN], "chat")
            total.merge(ac)
            name = brain_text.sanitize(call["name"])[:200]
            calls.append({"name": name, "arguments": mapping.cut(ac.text, ARG_OUT)[0]})
        restricted = sorted(set(total.restricted))
        names = [c["name"] for c in calls]
        if restricted:
            sensitivity = "restricted"
        elif (total.masked or staff == "sarah" or src.customer_tool(m.tool_name)
              or any(src.customer_tool(n) for n in names)):
            sensitivity = "customer"
        else:
            sensitivity = "general"
        flags: dict[str, Any] = {}
        if total.masked:
            flags["masked"] = dict(total.masked)
        if total.think_removed:
            flags["reasoning_removed"] = total.think_removed
        if m.time_assumed_local:
            flags["time_assumed_local"] = True
        labels = [] if restricted else brain_text.labels_for(m.role, content, tool_names=names)
        return MessageClean(content=content, truncated=truncated, calls=calls, sensitivity=sensitivity,
                            redactions=dict(total.secret_types), masked=dict(total.masked), flags=flags,
                            labels=labels, restricted=restricted)

    def plan_messages(self, msgs: list[src.SourceMessage], staff: str, unchanged: frozenset[str] = frozenset()
                      ) -> tuple[list[MessagePlan], set[str]]:
        """What each source row becomes, from the source alone (so a re-import takes the same decisions).

        unchanged: keys of rows an earlier run stored in this session from the very same source row (same source_fp).
        They are not cleaned again (cleaning is most of an import's time): the stored row already holds what the
        detectors found then, and `rescan` is how stored text meets newer detectors."""
        restricted: set[str] = set()
        compacted: dict[str, str] = {}
        for m in msgs:
            if m.compacted == 1 and not self.exclusion(m):
                compacted.setdefault(self.dup_fp(m), m.key)
        plans: list[MessagePlan] = []
        prev: tuple[str, str] | None = None
        prev_key = ""
        for m in msgs:
            reason = self.exclusion(m)
            if reason:
                if reason == REWOUND and m.content.strip():
                    # Not stored, but a conversation that held restricted data is withheld whole.
                    try:
                        restricted |= set(brain_text.clean(m.content[:CHAT_IN], "chat").restricted)
                    except Exception:  # the row is excluded anyway
                        pass
                plans.append(MessagePlan(m, "excluded", reason))
                continue
            fp = self.dup_fp(m)
            same_prev = False
            if m.role in ("user", "assistant"):
                current = (fp, m.tool_call_id)
                same_prev = current == prev
                if not same_prev:
                    prev, prev_key = current, m.key
            if m.compacted != 1 and fp in compacted:
                plans.append(MessagePlan(m, "duplicate", CARRIED, duplicate_of=compacted[fp]))
                continue
            if same_prev:
                plans.append(MessagePlan(m, "duplicate", "same as the previous message", duplicate_of=prev_key))
                continue
            if m.key in unchanged:
                plans.append(MessagePlan(m, "import"))  # reconciled as "already imported"; never cleaned again
                continue
            try:
                cleaned = self.clean_message(m, staff)
            except Exception as exc:  # a cleaning failure excludes this record only
                plans.append(MessagePlan(m, "excluded", "invalid record: %s" % exc.__class__.__name__))
                continue
            restricted |= set(cleaned.restricted)
            plans.append(MessagePlan(m, "import", cleaned=cleaned))
        return plans, restricted

    def message_values(self, p: MessagePlan, withheld: bool) -> dict[str, Any]:
        m, c = p.m, p.cleaned
        assert c is not None
        values: dict[str, Any] = {
            "source_message_id": str(m.id)[:200], "source_fp": m.fp, "position": m.position,
            "role": brain_text.sanitize(m.role)[:40], "content_chars": len(m.content),
            "tool_name": brain_text.sanitize(m.tool_name)[:200], "tool_call_id": brain_text.sanitize(m.tool_call_id)[:200],
            "at": m.at, "state": "compacted" if m.compacted == 1 else "live", "redactions": c.redactions,
        }
        if withheld:
            values.update(content="", truncated=False, tool_calls=[], sensitivity="restricted", withheld=True,
                          labels=[], flags={**c.flags, "withheld": "restricted data in this conversation"})
        else:
            values.update(content=c.content, truncated=c.truncated, tool_calls=c.calls, sensitivity=c.sensitivity,
                          withheld=False, labels=c.labels, flags=c.flags)
        return values

    async def do_session(self, ss: src.SourceSession, index: int) -> tuple[int, int]:
        self.location = safe_location(ss.key, "session", index)
        msgs = self.src.backup.messages(ss.id)
        size = sum(m.text_size for m in msgs)
        if ss.key in self.tombstones:
            self.record(ss.key, "session", "excluded", "purged by an admin")
            for m in msgs:
                self.record(m.key, "message", "excluded", "purged by an admin")
            return len(msgs), size
        self.t.time(ss.started_at)
        staff = ss.attribution.staff
        sess = await self.s.scalar(select(ArchiveSession).where(ArchiveSession.source_key == ss.key))
        rows: dict[str, dict[str, Any]] = {}
        keys = [m.key for m in msgs]
        for i in range(0, len(keys), 1000):
            for r in (await self.s.execute(select(*MESSAGE_COLUMNS).where(
                    ArchiveMessage.source_key.in_(keys[i:i + 1000])))).mappings().all():
                rows[r["source_key"]] = dict(r)
        try:
            title_c = mapping.clean(ss.title, "chat")
        except Exception:  # a title that cannot be cleaned is left out
            title_c = brain_text.Cleaned("")
        unchanged = frozenset(m.key for m in msgs if sess is not None and m.key in rows
                              and rows[m.key]["session_id"] == sess.id and rows[m.key]["source_fp"] == m.fp)
        plans, restricted = self.plan_messages(msgs, staff, unchanged)
        restricted |= set(title_c.restricted)
        withheld = bool(restricted) or (sess is not None and sess.sensitivity == "restricted")
        why = ("restricted data in this conversation: %s" % ", ".join(sorted(restricted)) if restricted
               else "restricted data in this conversation (found by an earlier import)")
        title = "" if withheld else brain_text.one_line(title_c.text, 300)
        flags: dict[str, Any] = {"attribution": ss.attribution.how, "inferred": ["staff", "labels"]}
        if ss.time_assumed_local:
            flags["time_assumed_local"] = True
        if title_c.masked and not withheld:
            flags["title_masked"] = True
        if title_c.secret_types:
            flags["title_redactions"] = dict(title_c.secret_types)
        if ss.hermes_counts:
            flags["hermes_counts"] = ss.hermes_counts
        fields = {"title": title, "started_at": ss.started_at, "ended_at": ss.ended_at, "end_reason": ss.end_reason,
                  "source_message_count": len(msgs), "hermes_archived": ss.hermes_archived, "channel": ss.channel,
                  "lineage": ss.lineage, "parent_source_session_id": ss.parent_id[:200], "staff": staff,
                  "agent": ss.attribution.agent, "origin": ss.attribution.origin, "job_id": ss.attribution.job_id,
                  "job_name": brain_text.one_line(mapping.clean(ss.attribution.job_name, "chat").text, 200),
                  "flags": flags}
        created = sess is None
        was_restricted = sess is not None and sess.sensitivity == "restricted"
        before: dict[str, Any] = {}
        source_changed = False
        if created:
            if self.write:
                async def create() -> ArchiveSession:
                    obj = ArchiveSession(source_system="jarvis", source_key=ss.key, source_session_id=ss.id[:200],
                                         import_run_id=self.run_id, **fields)
                    self.s.add(obj)
                    await self.s.flush()
                    return obj
                ok, result = await self.guarded(create)
                if not ok:
                    self.record(ss.key, "session", "excluded", "invalid record: %s" % result)
                    for m in msgs:
                        self.record(m.key, "message", "excluded", "invalid record: the session could not be stored")
                    return len(msgs), size
                sess = result
                sess_id = sess.id
            else:
                sess_id = self.fake_id()
        else:
            assert sess is not None
            sess_id = sess.id
            before = jsonable({f: getattr(sess, f) for f in SESSION_SOURCE_FIELDS})
            current = {**fields, "flags": {**(sess.flags or {}), **flags}}
            for k in ("title_masked", "title_redactions", "time_assumed_local"):
                if k not in flags:
                    current["flags"].pop(k, None)
            source_changed = jsonable(current) != jsonable({f: getattr(sess, f) for f in SESSION_SOURCE_FIELDS})
            if source_changed and self.write:
                for k, v in current.items():
                    setattr(sess, k, v)
                await self.s.flush()
        changed = await self.write_messages(plans, rows, sess_id, withheld, why)
        level = "general"
        if created or changed or source_changed:
            if self.write:
                after = await recompute_session(self.s, sess_id)
                level = after.sensitivity if after else level
            else:
                levels = [p.cleaned.sensitivity for p in plans if p.cleaned] + (
                    ["customer"] if title_c.masked else [])
                level = "restricted" if withheld else brain_text.most_sensitive(*levels) if levels else "general"
        if created:
            self.t.created(agent=ss.attribution.agent, staff=staff, sensitivity=("sessions", level))
            if withheld:
                self.t.restricted(restricted, session=True)
                self.record(ss.key, "session", "withheld", why, "archive_sessions", sess_id)
            else:
                self.record(ss.key, "session", "imported", "", "archive_sessions", sess_id)
        elif changed or source_changed:
            if withheld and not was_restricted:
                self.t.restricted(restricted, session=True)
            self.record(ss.key, "session", "updated", "grew or changed since an earlier import", "archive_sessions",
                        sess_id, before)
        else:
            self.record(ss.key, "session", "duplicate", "already imported", "archive_sessions", sess_id)
        return len(msgs), size

    async def write_messages(self, plans: list[MessagePlan], rows: dict[str, dict[str, Any]], sess_id: int,
                             withheld: bool, why: str) -> int:
        changed = 0
        ids = {k: r["id"] for k, r in rows.items() if r["session_id"] == sess_id}
        duplicates: list[MessagePlan] = []
        new: list[tuple[MessagePlan, dict[str, Any]]] = []
        for p in plans:
            m = p.m
            row = rows.get(m.key)
            if row is not None and row["session_id"] != sess_id:
                self.record(m.key, "message", "excluded", "key collision")
                continue
            if row is not None:
                changed += await self._reconcile(p, row, withheld, why)
                continue
            if p.action == "excluded":
                self.record(m.key, "message", "excluded", p.reason)
                continue
            if p.action == "duplicate":
                duplicates.append(p)
                continue
            new.append((p, {"session_id": sess_id, "source_key": m.key, "import_run_id": self.run_id,
                            **self.message_values(p, withheld)}))
        for p, outcome in await self._insert_messages(new):
            if isinstance(outcome, str):
                self.record(p.m.key, "message", "excluded", "invalid record: %s" % outcome)
                continue
            ids[p.m.key] = outcome
            changed += 1
            self._count_message(p, withheld)
            self.record(p.m.key, "message", "withheld" if withheld else "imported", why if withheld else "",
                        "archive_messages", outcome)
        for p in duplicates:
            target = ids.get(p.duplicate_of)
            self.record(p.m.key, "message", "duplicate", p.reason, "archive_messages" if target else "", target)
        return changed

    async def _insert_messages(self, new: list[tuple[MessagePlan, dict[str, Any]]]
                               ) -> list[tuple[MessagePlan, int | str]]:
        """(plan, new row id or the class of the error that excluded it) for a session's new messages.

        All of them go in with one statement inside one SAVEPOINT (a conversation's rows are written together; one
        savepoint per row made the import several times slower). If the database refuses that statement, the
        savepoint is rolled back and every row is written again inside its own SAVEPOINT, so only the row it refuses
        is excluded: the outcome is the same as one savepoint per record.
        """
        if not new:
            return []
        if not self.write:
            return [(p, self.fake_id()) for p, _ in new]
        if len(new) > 1:
            async def together() -> list[int]:
                result = await self.s.execute(
                    insert(ArchiveMessage).returning(ArchiveMessage.id, sort_by_parameter_order=True),
                    [values for _, values in new])
                return [int(r[0]) for r in result.all()]
            ok, result = await self.guarded(together)
            if ok:
                return list(zip((p for p, _ in new), result, strict=True))
        out: list[tuple[MessagePlan, int | str]] = []
        for p, values in new:
            async def one(values: dict[str, Any] = values) -> int:
                return int(await self.s.scalar(insert(ArchiveMessage).values(**values).returning(ArchiveMessage.id)))
            ok, result = await self.guarded(one)
            out.append((p, result if ok else str(result)))
        return out

    def _count_message(self, p: MessagePlan, withheld: bool) -> None:
        c = p.cleaned
        if c is None:
            return
        self.t.created(sensitivity=("messages", "restricted" if withheld else c.sensitivity))
        self.t.secrets(c.redactions, "messages")
        self.t.masked(c.masked)

    async def _reconcile(self, p: MessagePlan, row: dict[str, Any], withheld: bool, why: str) -> int:
        """A message an earlier run imported: unchanged, now withheld, changed, compacted or rewound."""
        m = p.m
        if row["source_fp"] == m.fp:
            if withheld and not row["withheld"]:
                values = {"content": "", "truncated": False, "tool_calls": [], "sensitivity": "restricted",
                          "withheld": True, "labels": [],
                          "flags": {**(row["flags"] or {}), "withheld": "restricted data in this conversation"}}
                await self._update_row(row, values)
                self.record(m.key, "message", "updated", "withheld: " + why, "archive_messages", row["id"],
                            jsonable({k: row[k] for k in MESSAGE_FIELDS}))
                return 1
            self.record(m.key, "message", "duplicate", "already imported", "archive_messages", row["id"])
            return 0
        if p.action in ("excluded", "duplicate"):
            reason = ("rewound since an earlier import" if p.reason == REWOUND
                      else "%s since an earlier import" % p.reason)
            if self.write:
                await self.s.execute(delete(ArchiveMessage).where(ArchiveMessage.id == row["id"]))
            self.record(m.key, "message", "updated", reason, "archive_messages", row["id"],
                        {"deleted_row": jsonable(row)})
            return 1
        values = self.message_values(p, withheld)
        ok, result = await self.guarded(lambda: self._update_row(row, values))
        if not ok:
            self.record(m.key, "message", "excluded", "invalid record: %s" % result)
            return 0
        reason = ("compacted since an earlier import" if values["state"] == "compacted" and row["state"] == "live"
                  else "changed since an earlier import")
        self._count_message(p, withheld)
        self.record(m.key, "message", "updated", reason, "archive_messages", row["id"],
                    jsonable({k: row[k] for k in MESSAGE_FIELDS}))
        return 1

    async def _update_row(self, row: dict[str, Any], values: dict[str, Any]) -> None:
        if self.write:
            await self.s.execute(update(ArchiveMessage).where(ArchiveMessage.id == row["id"]).values(**values))

    async def do_orphan(self, m: src.SourceMessage, index: int) -> None:
        self.location = safe_location(m.key, "orphan", index)
        self.record(m.key, "orphan", "excluded", "purged by an admin" if m.key in self.tombstones else "no session")

    # Phases -------------------------------------------------------------------------------------------------

    async def run_phases(self, work: Work, start_phase: str | None, start_index: int,
                         checkpoint: Callable[[str, int], Awaitable[None]]) -> None:
        for phase in PHASES:
            if start_phase and start_phase in PHASES and PHASES.index(phase) < PHASES.index(start_phase):
                continue
            start = start_index if phase == start_phase else 0
            self.phase = phase
            if phase == "files":
                await self._files(work.files, start, checkpoint)
            elif phase == "jobs" and work.jobs:
                await self._batched(self.drafts, start, BATCH_RECORDS, self.do_draft, "jobs", checkpoint)
            elif phase == "sessions":
                await self._sessions(work.sessions, start, checkpoint)
            elif phase == "orphans" and work.orphans:
                await self._orphans(start, checkpoint)
            nxt = PHASES[PHASES.index(phase) + 1] if phase != PHASES[-1] else "finish"
            await checkpoint(nxt, 0)
        self.phase = "finish"

    async def _files(self, files: list[src.SourceFile], start: int,
                     checkpoint: Callable[[str, int], Awaitable[None]]) -> None:
        i = start
        while i < len(files):
            n = 0
            while i < len(files) and (n == 0 or n < BATCH_RECORDS):
                self.location = safe_location(files[i].key, "file", i)
                n += await self.do_file(files[i], i)
                i += 1
            await self.flush()
            await checkpoint("files", i)

    async def _batched(self, units: list[Any], start: int, size: int, fn: Callable[[Any, int], Awaitable[None]],
                       phase: str, checkpoint: Callable[[str, int], Awaitable[None]]) -> None:
        i = start
        while i < len(units):
            for j in range(i, min(i + size, len(units))):
                self.location = "%s #%d" % (phase, j + 1)
                await fn(units[j], j)
            i = min(i + size, len(units))
            await self.flush()
            await checkpoint(phase, i)

    async def _sessions(self, sessions: list[src.SourceSession], start: int,
                        checkpoint: Callable[[str, int], Awaitable[None]]) -> None:
        counts = self.src.backup.message_counts()
        i = start
        while i < len(sessions):
            n_sessions = n_messages = n_text = 0
            while i < len(sessions) and n_sessions < BATCH_SESSIONS and n_text < BATCH_TEXT:
                upcoming = counts.get(sessions[i].id, 0)
                if n_sessions and n_messages + upcoming > BATCH_MESSAGES:
                    break
                self.location = safe_location(sessions[i].key, "session", i)
                msgs, text_size = await self.do_session(sessions[i], i)
                n_sessions, n_messages, n_text = n_sessions + 1, n_messages + msgs, n_text + text_size
                i += 1
            await self.flush()
            await checkpoint("sessions", i)

    async def _orphans(self, start: int, checkpoint: Callable[[str, int], Awaitable[None]]) -> None:
        i = 0
        batch = 0
        for m in self.src.backup.orphans():
            if i >= start:
                await self.do_orphan(m, i)
                batch += 1
            i += 1
            if batch >= BATCH_MESSAGES:
                await self.flush()
                await checkpoint("orphans", i)
                batch = 0
        await self.flush()
        await checkpoint("orphans", i)


async def missing_from_source(session: AsyncSession, sources: Sources) -> int:
    present = {s.id for s in sources.backup.sessions()}
    stored = (await session.scalars(select(ArchiveSession.source_session_id)
                                    .where(ArchiveSession.source_system == "jarvis"))).all()
    return sum(1 for sid in stored if sid not in present)


# --- plan, run, resume -----------------------------------------------------------------------------------------


async def plan(paths: SourcePaths, selection: Selection, *, report_file: str | None = None) -> dict[str, Any]:
    """What a run with this selection would do. Reads the target database in a read-only transaction."""
    selection.validate()
    async with sessionmaker()() as s:
        await s.execute(sql("SET TRANSACTION READ ONLY"))
        await require_schema(s)
        tz_name, tz = await customer_timezone(s)
        sources = Sources(paths, tz, tz_name)
        try:
            work = select_work(sources, selection)
            tally = Tally()
            proc = Processor(s, sources, run_id=None, tally=tally, write=False)
            await proc.load(work)

            async def no_checkpoint(phase: str, index: int) -> None:
                return None
            await proc.run_phases(work, None, 0, no_checkpoint)
            report = tally.report(selection, work.totals, sources.discovery, await missing_from_source(s, sources))
        finally:
            sources.close()
            await s.rollback()
    report["plan"] = True
    write_report(report_file, report)
    return report


async def run(paths: SourcePaths, selection: Selection, *, label: str = "", actor: str = DEFAULT_ACTOR,
              report_file: str | None = None, pre_backup_sha256: str = "",
              heartbeat_seconds: float = HEARTBEAT_SECONDS) -> dict[str, Any]:
    """Import the selection. Refusals (Refused) come before any change; a failure or an interruption raises
    ImportFailed after the run's row says what happened."""
    selection.validate()
    async with sessionmaker()() as s:
        await require_schema(s)
        tz_name, tz = await customer_timezone(s)
    sources = Sources(paths, tz, tz_name)
    try:
        source = sources.source_info(pre_backup_sha256)
        locks = await LockConnection.open()
        try:
            if not await locks.try_lock(GLOBAL_LOCK):
                raise Refused("another import, rollback or rescan is running")
            async with sessionmaker()() as s:
                await mark_dead_runs(s)
            scope = {"selection": selection.to_json(), "timezone": tz_name}
            run_id = await locks.start_run(label=brain_text.one_line(label, 200), actor=actor, scope=scope,
                                           source=source)
            locks.start_heartbeat(run_id, heartbeat_seconds)
            return await _execute(run_id, sources, selection, {}, actor, report_file, resumed=False)
        finally:
            await locks.close()
    finally:
        sources.close()


async def resume(run_id: int, paths: SourcePaths, *, report_file: str | None = None, actor: str = DEFAULT_ACTOR,
                 heartbeat_seconds: float = HEARTBEAT_SECONDS) -> dict[str, Any]:
    """Continue an interrupted or failed run after its checkpoint, from the very same sources."""
    async with sessionmaker()() as s:
        await require_schema(s)
        found = await s.get(ImportRun, run_id)
        if found is None:
            raise Refused("there is no import run %d" % run_id)
        tz_name = (found.scope or {}).get("timezone") or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz_name, tz = "UTC", ZoneInfo("UTC")
    sources = Sources(paths, tz, tz_name)
    try:
        locks = await LockConnection.open()
        try:
            if not await locks.try_lock(GLOBAL_LOCK):
                raise Refused("another import, rollback or rescan is running")
            if not await locks.try_lock(run_id):
                raise Refused("run %d is still running" % run_id)
            async with sessionmaker()() as s:
                await mark_dead_runs(s)
                run_row = await s.get(ImportRun, run_id)
                assert run_row is not None
                if run_row.status not in ("interrupted", "failed", "running"):  # running: we hold its lock, so dead
                    raise Refused("run %d is %s: only an interrupted or failed run can be resumed"
                                  % (run_id, run_row.status.replace("_", " ")))
                if not sources.same_source(run_row.source or {}):
                    raise Refused("the backup or the restored profile differs from the one run %d started with: "
                                  "resume needs the very same sources" % run_id)
                selection = Selection.from_json((run_row.scope or {}).get("selection") or {})
                checkpoint = dict(run_row.checkpoint or {})
                try:
                    await set_status(s, run_id, run_row.status, status="running", error="", finished_at=None,
                                     heartbeat_at=now())
                    await s.commit()
                except (RunLost, IntegrityError):
                    raise Refused("run %d could not be resumed: another import is running" % run_id) from None
            locks.start_heartbeat(run_id, heartbeat_seconds)
            return await _execute(run_id, sources, selection, checkpoint, actor, report_file, resumed=True)
        finally:
            await locks.close()
    finally:
        sources.close()


async def _execute(run_id: int, sources: Sources, selection: Selection, checkpoint: dict[str, Any], actor: str,
                   report_file: str | None, *, resumed: bool) -> dict[str, Any]:
    tally = Tally(checkpoint.get("tally"))
    work = select_work(sources, selection)
    async with sessionmaker()() as s:
        proc = Processor(s, sources, run_id=run_id, tally=tally, write=True, actor=actor)
        try:
            await audit.record(s, actor, "brain.import_started", "import_run", run_id,
                               {"run": run_id, "resumed": resumed, "selection": selection.mode,
                                "totals": work.totals})
            await s.commit()
            await proc.load(work)

            async def save(phase: str, index: int) -> None:
                await set_status(s, run_id, "running", checkpoint={"phase": phase, "index": index,
                                                                    "tally": tally.d})
                await s.commit()
                s.expunge_all()
            await proc.run_phases(work, checkpoint.get("phase"), int(checkpoint.get("index") or 0), save)
            report = tally.report(selection, work.totals, sources.discovery, await missing_from_source(s, sources))
            counts = {"records": sum(report["by_action"].values()), "by_action": report["by_action"],
                      "by_type": report["by_type"]}
            await set_status(s, run_id, "running", status="completed", finished_at=now(), counts=counts,
                             report=report, checkpoint={"phase": "finish", "index": 0, "tally": tally.d})
            await audit.record(s, actor, "brain.import_completed", "import_run", run_id,
                               {"run": run_id, "records": counts["records"], "by_action": counts["by_action"]})
            await s.commit()
        except asyncio.CancelledError:
            await _stop(run_id, "interrupted", "", actor, s)
            raise ImportFailed("run %d was interrupted; resume it with: brain-import resume --run %d"
                               % (run_id, run_id)) from None
        except RunLost:
            await _safe_rollback(s)
            raise ImportFailed("run %d was stopped from outside (its status changed)" % run_id) from None
        except Exception as exc:
            error = "%s in %s at %s" % (exc.__class__.__name__, proc.phase or "start", proc.location or "start")
            await _stop(run_id, "failed", error, actor, s)
            raise ImportFailed("run %d failed: %s" % (run_id, error)) from None
    write_report(report_file, report)
    return {"run_id": run_id, "status": "completed", "report": report}


async def _safe_rollback(s: AsyncSession) -> None:
    try:
        await s.rollback()
    except Exception:  # the connection may be gone; the new session below reports the outcome
        pass


async def _stop(run_id: int, status: str, error: str, actor: str, s: AsyncSession) -> None:
    """Record the end of a run that did not finish. The checkpoint stays, so resume continues after it."""
    await _safe_rollback(s)
    async with sessionmaker()() as fresh:
        try:
            await set_status(fresh, run_id, "running", status=status, error=error, finished_at=now())
        except RunLost:
            return
        await audit.record(fresh, actor, "brain.import_failed", "import_run", run_id,
                           {"run": run_id, "status": status, "error": error})
        await fresh.commit()


# --- verify ------------------------------------------------------------------------------------------------------


ID_COLUMNS = {"id", "source_key", "source_message_id", "source_session_id", "parent_source_session_id", "source_fp",
              "content_hash", "source_sha256", "dedupe_key", "tool_call_id", "job_id", "import_run_id", "session_id",
              "run_id", "item_id", "existing_id", "incoming_item_id", "schedule_id", "existing_table", "source_system"}


def leak_types(value: Any, kind: str = "chat") -> list[str]:
    """What the import's detectors would still find in a stored value (secrets, restricted data, long tokens)."""
    if value is None or isinstance(value, (bool, int, float, datetime)):
        return []
    if isinstance(value, (list, dict)):
        if not value:
            return []
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    if not text.strip():
        return []
    cleaned = mapping.clean(text, kind)
    return sorted(set(cleaned.secret_types) | set(cleaned.restricted))


def _row_leaks(table: str, row: dict[str, Any], kinds: dict[str, str] | None = None) -> list[dict[str, Any]]:
    out = []
    for column, value in row.items():
        if column in ID_COLUMNS or column == "search":
            continue
        found = leak_types(value, (kinds or {}).get(column, "chat"))
        if found:
            out.append({"table": table, "id": row.get("id"), "column": column, "types": found})
    return out


def expected_keys(sources: Sources, work: Work) -> tuple[set[str], dict[str, list[str]]]:
    keys: set[str] = set()
    per_session: dict[str, list[str]] = {}
    for f in work.files:
        keys.update(p.key for p in mapping.file_parts(f, sources.tz))
    if work.jobs:
        keys.update(j.key for j in sources.jobs)
        if sources.watchdog is not None:
            keys.add(mapping.WATCHDOG_KEY)
    for ss in work.sessions:
        keys.add(ss.key)
        per_session[ss.key] = [m.key for m in sources.backup.messages(ss.id)]
        keys.update(per_session[ss.key])
    if work.orphans:
        keys.update(m.key for m in sources.backup.orphans())
    return keys, per_session


async def _search_word(s: AsyncSession, text: str) -> str | None:
    for word in re.findall(r"[A-Za-z]{4,30}", text)[:40]:
        if await s.scalar(select(func.numnode(func.websearch_to_tsquery("english", word)))):
            return word
    return None


async def verify(run_id: int, paths: SourcePaths, *, report_file: str | None = None) -> dict[str, Any]:
    """Recompute the selection from the sources and check the run against it (spec 2.1, eight checks)."""
    async with sessionmaker()() as s:
        await require_schema(s)
        run_row = await s.get(ImportRun, run_id)
        if run_row is None:
            raise Refused("there is no import run %d" % run_id)
        if run_row.status != "completed":
            raise Refused("run %d is %s: only a completed run can be verified" % (run_id,
                                                                                  run_row.status.replace("_", " ")))
        tz_name = (run_row.scope or {}).get("timezone") or "UTC"
        selection = Selection.from_json((run_row.scope or {}).get("selection") or {})
        recorded_source = dict(run_row.source or {})
        report = dict(run_row.report or {})
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    sources = Sources(paths, tz, tz_name)
    try:
        if not sources.same_source(recorded_source):
            raise Refused("the backup or the restored profile differs from the one run %d used: verify needs the "
                          "very same sources" % run_id)
        work = select_work(sources, selection)
        expected, per_session = expected_keys(sources, work)
        checks: dict[str, dict[str, Any]] = {}
        async with sessionmaker()() as s:
            records = {r.source_key: r for r in (await s.execute(
                select(ImportRecord.source_key, ImportRecord.action, ImportRecord.record_type,
                       ImportRecord.target_table, ImportRecord.target_id)
                .where(ImportRecord.run_id == run_id))).all()}
            missing, extra = expected - set(records), set(records) - expected
            checks["every_record_once"] = {"ok": not missing and not extra, "expected": len(expected),
                                           "recorded": len(records), "missing": len(missing), "unexpected": len(extra)}
            unrecorded = {}
            for table in BRAIN_TABLES:
                unrecorded[table] = int(await s.scalar(sql(
                    "SELECT count(*) FROM %s t WHERE t.import_run_id = :r AND NOT EXISTS (SELECT 1 FROM "
                    "import_records x WHERE x.run_id = :r AND x.target_table = :t AND x.target_id = t.id)" % table),
                    {"r": run_id, "t": table}) or 0)
            checks["no_unrecorded_rows"] = {"ok": not any(unrecorded.values()), "rows": unrecorded}
            mismatched = []
            for skey, mkeys in per_session.items():
                accounted = sum(1 for k in mkeys if k in records)
                if accounted != len(mkeys) or (len(set(mkeys)) != len(mkeys)):
                    mismatched.append(skey)
            touched = set((await s.scalars(select(ImportRecord.target_id).where(
                ImportRecord.run_id == run_id, ImportRecord.target_table == "archive_sessions",
                ImportRecord.target_id.is_not(None)))).all())
            bad_counts = []
            for sid in sorted(touched):
                row = (await s.execute(sql(
                    "SELECT s.imported_messages, s.withheld_messages, "
                    "(SELECT count(*) FROM archive_messages m WHERE m.session_id = s.id AND NOT m.withheld), "
                    "(SELECT count(*) FROM archive_messages m WHERE m.session_id = s.id AND m.withheld) "
                    "FROM archive_sessions s WHERE s.id = :id"), {"id": sid})).first()
                if row is not None and (row[0] != row[2] or row[1] != row[3]):
                    bad_counts.append(sid)
            checks["session_counts"] = {"ok": not mismatched and not bad_counts, "sessions": len(per_session),
                                        "source_rows_unaccounted": len(mismatched),
                                        "archive_count_mismatch_ids": bad_counts[:50]}
            if selection.all:
                backup_keys = [k for k in expected if k.startswith("jarvis:message:")]
                covered = 0
                for i in range(0, len(backup_keys), 1000):
                    covered += int(await s.scalar(
                        select(func.count(func.distinct(ImportRecord.source_key)))
                        .join(ImportRun, ImportRun.id == ImportRecord.run_id)
                        .where(ImportRecord.source_key.in_(backup_keys[i:i + 1000]),
                               ImportRun.status != "rolled_back")) or 0)
                checks["all_messages"] = {"ok": covered == sources.counts["messages"],
                                          "backup_messages": sources.counts["messages"], "recorded": covered}
            leaks = await _scan_leaks(s, run_id, report)
            checks["no_secrets_or_restricted_data"] = {"ok": not leaks, "found": len(leaks), "where": leaks[:50]}
            withheld_text = int(await s.scalar(sql(
                "SELECT count(*) FROM archive_messages WHERE withheld AND (content <> '' OR tool_calls <> '[]'::jsonb "
                "OR cardinality(labels) > 0)")) or 0)
            restricted_text = int(await s.scalar(sql(
                "SELECT count(*) FROM archive_sessions WHERE sensitivity = 'restricted' AND (title <> '' OR "
                "summary <> '')")) or 0)
            checks["withheld_hold_no_text"] = {"ok": not withheld_text and not restricted_text,
                                               "messages": withheld_text, "sessions": restricted_text}
            columns = (recorded_source.get("columns_read") or {})
            bad_columns = [c for cols in columns.values() for c in cols
                           if kit.REASONING_COLUMN.search(c) or src.FORBIDDEN_COLUMN.match(c)]
            checks["no_reasoning_columns_read"] = {"ok": not bad_columns, "columns": bad_columns}
            checks["search_finds_imported_text"] = await _search_check(s, run_id)
            passed = all(c["ok"] for c in checks.values())
            verification = {"passed": passed, "checked_at": now().isoformat(), "checks": checks}
            report["verification"] = verification
            await s.execute(update(ImportRun).where(ImportRun.id == run_id).values(report=report))
            await s.commit()
    finally:
        sources.close()
    write_report(report_file, report)
    return {"run_id": run_id, "passed": passed, "verification": verification}


async def _scan_leaks(s: AsyncSession, run_id: int, report: dict[str, Any]) -> list[dict[str, Any]]:
    leaks: list[dict[str, Any]] = []

    async def rows(query: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in (await s.execute(query)).mappings().all()]

    item_cols = [c for c in MemoryItem.__table__.c if c.name != "search"]
    for item in await rows(select(*item_cols).where(MemoryItem.import_run_id == run_id)):
        kind = (item.get("flags") or {}).get("text_kind", "chat")
        leaks += _row_leaks("memory_items", item, {"body": kind})
        for v in await rows(select(MemoryItemVersion.__table__).where(MemoryItemVersion.item_id == item["id"])):
            leaks += _row_leaks("memory_item_versions", v, {"body": kind})
    updated = {t: set((await s.scalars(select(ImportRecord.target_id).where(
        ImportRecord.run_id == run_id, ImportRecord.target_table == t, ImportRecord.action == "updated",
        ImportRecord.target_id.is_not(None)))).all()) for t in ("archive_sessions", "archive_messages")}
    session_cols = [c for c in ArchiveSession.__table__.c if c.name != "search"]
    for row in await rows(select(*session_cols).where(or_(ArchiveSession.import_run_id == run_id,
                                                          ArchiveSession.id.in_(updated["archive_sessions"])))):
        leaks += _row_leaks("archive_sessions", row)
    last = 0
    while True:
        batch = await rows(select(*MESSAGE_COLUMNS).where(
            or_(ArchiveMessage.import_run_id == run_id, ArchiveMessage.id.in_(updated["archive_messages"])),
            ArchiveMessage.id > last).order_by(ArchiveMessage.id).limit(500))
        if not batch:
            break
        for row in batch:
            leaks += _row_leaks("archive_messages", row)
        last = batch[-1]["id"]
    for row in await rows(select(MemoryConflict.__table__).where(MemoryConflict.run_id == run_id)):
        leaks += _row_leaks("memory_conflicts", row)
    for row in await rows(select(WorkflowDraft.__table__).where(WorkflowDraft.import_run_id == run_id)):
        leaks += _row_leaks("workflow_drafts", row)
    leaks += _row_leaks("import_runs", {"id": run_id, "report": report})
    return leaks


async def _search_check(s: AsyncSession, run_id: int) -> dict[str, Any]:
    sampled = found = 0
    messages = (await s.execute(select(ArchiveMessage.id, ArchiveMessage.content).where(
        ArchiveMessage.import_run_id == run_id, ArchiveMessage.withheld.is_(False), ArchiveMessage.content != "")
        .order_by(ArchiveMessage.id).limit(20))).all()
    for mid, content in messages:
        word = await _search_word(s, content)
        if word is None:
            continue
        sampled += 1
        if await s.scalar(select(ArchiveMessage.id).where(
                ArchiveMessage.id == mid, ArchiveMessage.search.op("@@")(func.websearch_to_tsquery("english", word)))):
            found += 1
    items = (await s.execute(select(MemoryItem.id, MemoryItem.title, MemoryItem.body).where(
        MemoryItem.import_run_id == run_id).order_by(MemoryItem.id).limit(20))).all()
    for iid, title, body in items:
        word = await _search_word(s, title + " " + body[:2000])
        if word is None:
            continue
        sampled += 1
        if await s.scalar(select(MemoryItem.id).where(
                MemoryItem.id == iid, MemoryItem.search.op("@@")(func.websearch_to_tsquery("english", word)))):
            found += 1
    return {"ok": found == sampled, "sampled": sampled, "found": found}


# --- rollback ----------------------------------------------------------------------------------------------------


async def rollback_run(session: AsyncSession, run_id: int, actor: str, discard_reviewed: bool = False
                       ) -> dict[str, Any]:
    """Undo one import run exactly (spec 2.1), in the caller's transaction; the caller commits.

    Refuses (Refused, with the ids) while the run or another import is running, when a later run that is not
    rolled back depends on this one, when any of the run's items was reviewed or edited (unless discard_reviewed),
    and when a conflict resolution's target was changed again since. Also called by the API.

    The locks are transaction-level advisory locks taken in the caller's transaction: the one-at-a-time lock
    (LOCK_KEY, 0) and the run's own (LOCK_KEY, run id). They conflict with the session-level locks a running import
    holds on its own connection, and they end with the caller's commit or rollback, so a rollback, which is one
    transaction, holds them for its whole life. Refused and ImportFailed (the result was not exact) both mean the
    caller must roll its transaction back: nothing is changed then.
    """
    await require_schema(session)
    # The one-import-at-a-time lock and the run's own lock, held until the caller's transaction ends.
    if not await try_xact_lock(session, GLOBAL_LOCK):
        raise Refused("an import, rollback or rescan is running: try again when it has finished")
    if not await try_xact_lock(session, run_id):
        raise Refused("run %d is still running" % run_id)
    run_row = await session.scalar(select(ImportRun).where(ImportRun.id == run_id).with_for_update())
    if run_row is None:
        raise Refused("there is no import run %d" % run_id)
    if run_row.status == "running":  # its lock is free (we hold it): the process is gone
        await set_status(session, run_id, "running", status="interrupted", finished_at=now())
        run_row.status = "interrupted"
    if run_row.status == "rolled_back":
        raise Refused("run %d is already rolled back" % run_id)
    expected_status = run_row.status

    params = {"r": run_id}
    later = sql("SELECT id FROM import_runs WHERE id > :r AND status <> 'rolled_back'")
    dependent = set((await session.scalars(sql(
        "SELECT DISTINCT x.run_id FROM import_records x WHERE x.run_id IN (%s) AND ("
        " (x.target_table = 'memory_items' AND x.target_id IN (SELECT id FROM memory_items WHERE import_run_id = :r))"
        " OR (x.target_table = 'archive_sessions' AND x.target_id IN "
        "     (SELECT id FROM archive_sessions WHERE import_run_id = :r))"
        " OR (x.target_table = 'archive_messages' AND x.target_id IN "
        "     (SELECT id FROM archive_messages WHERE import_run_id = :r))"
        " OR (x.target_table = 'workflow_drafts' AND x.target_id IN "
        "     (SELECT id FROM workflow_drafts WHERE import_run_id = :r))"
        " OR (x.target_table = 'memory_conflicts' AND x.target_id IN "
        "     (SELECT id FROM memory_conflicts WHERE run_id = :r))"
        " OR (x.before -> 'deleted_row' ->> 'import_run_id') = CAST(:r AS text))" % later.text), params)).all())
    dependent |= set((await session.scalars(sql(
        "SELECT DISTINCT import_run_id FROM archive_messages WHERE import_run_id IN (%s) AND session_id IN "
        "(SELECT id FROM archive_sessions WHERE import_run_id = :r)" % later.text), params)).all())
    dependent |= set((await session.scalars(sql(
        "SELECT DISTINCT run_id FROM memory_conflicts WHERE run_id IN (%s) AND ("
        " (existing_table = 'memory_items' AND existing_id IN (SELECT id FROM memory_items WHERE import_run_id = :r))"
        " OR (existing_table = 'workflow_drafts' AND existing_id IN "
        "     (SELECT id FROM workflow_drafts WHERE import_run_id = :r)))" % later.text), params)).all())
    dependent.discard(None)
    if dependent:
        raise Refused("later import runs depend on run %d: roll them back first, newest first" % run_id,
                      sorted(dependent))
    reviewed = list((await session.scalars(select(MemoryItem.id).where(
        MemoryItem.import_run_id == run_id,
        or_(MemoryItem.version > 1, MemoryItem.status.not_in(("pending_review", "quarantined")),
            MemoryItem.deleted_at.is_not(None))).order_by(MemoryItem.id))).all())
    if reviewed and not discard_reviewed:
        raise Refused("%d item(s) of run %d were reviewed or edited since the import; roll back with "
                      "discard_reviewed to delete them too" % (len(reviewed), run_id), reviewed)
    conflicts = list((await session.scalars(select(MemoryConflict).where(MemoryConflict.run_id == run_id)
                                            .order_by(MemoryConflict.id))).all())
    blocked = await _blocked_effects(session, run_id, conflicts)
    if blocked:
        raise Refused("conflict decisions of run %d changed rows that were changed again since: revert those by "
                      "hand first" % run_id, blocked)

    reverted = await _revert_effects(session, run_id, conflicts, actor)
    restored = {"archive_messages": 0, "archive_sessions": 0}
    touched: set[int] = set()
    created_sessions = set((await session.scalars(select(ArchiveSession.id).where(
        ArchiveSession.import_run_id == run_id))).all())
    updates = (await session.execute(select(ImportRecord.target_table, ImportRecord.target_id, ImportRecord.before)
                                     .where(ImportRecord.run_id == run_id, ImportRecord.action == "updated")
                                     .order_by(ImportRecord.id.desc()))).all()
    for table, target, before in updates:
        if table == "archive_messages" and before:
            if "deleted_row" in before:
                row = unjson(before["deleted_row"])
                await session.execute(insert(ArchiveMessage).values(**row))
                touched.add(int(row["session_id"]))
            else:
                await session.execute(update(ArchiveMessage).where(ArchiveMessage.id == target)
                                      .values(**unjson(before)))
                sid = await session.scalar(select(ArchiveMessage.session_id).where(ArchiveMessage.id == target))
                if sid:
                    touched.add(sid)
            restored["archive_messages"] += 1
        elif table == "archive_sessions" and before and target:
            await session.execute(update(ArchiveSession).where(ArchiveSession.id == target).values(**unjson(before)))
            touched.add(target)
            restored["archive_sessions"] += 1
    touched |= set((await session.scalars(select(ArchiveMessage.session_id).where(
        ArchiveMessage.import_run_id == run_id).distinct())).all())
    touched -= created_sessions
    promoted = await _mark_promoted(session, run_id)
    report = dict(run_row.report or {})
    deleted = {}
    deleted["conflicts"] = (await session.execute(delete(MemoryConflict).where(MemoryConflict.run_id == run_id))
                            ).rowcount
    kept = list((await session.scalars(select(WorkflowDraft.id).where(
        WorkflowDraft.import_run_id == run_id, WorkflowDraft.status == "converted"))).all())
    deleted["drafts"] = (await session.execute(delete(WorkflowDraft).where(
        WorkflowDraft.import_run_id == run_id, WorkflowDraft.status != "converted"))).rowcount
    deleted["items"] = (await session.execute(delete(MemoryItem).where(MemoryItem.import_run_id == run_id))).rowcount
    deleted["messages"] = (await session.execute(delete(ArchiveMessage).where(
        ArchiveMessage.import_run_id == run_id))).rowcount
    deleted["sessions"] = (await session.execute(delete(ArchiveSession).where(
        ArchiveSession.import_run_id == run_id))).rowcount
    session.expire_all()
    for sid in sorted(touched):
        await recompute_session(session, sid)
    left = {t: int(await session.scalar(sql("SELECT count(*) FROM %s WHERE import_run_id = :r" % t), params) or 0)
            for t in BRAIN_TABLES}
    left["workflow_drafts"] -= len(kept)
    off = []
    for sid in sorted(touched):
        row = (await session.execute(sql(
            "SELECT s.imported_messages, s.withheld_messages, "
            "(SELECT count(*) FROM archive_messages m WHERE m.session_id = s.id AND NOT m.withheld), "
            "(SELECT count(*) FROM archive_messages m WHERE m.session_id = s.id AND m.withheld) "
            "FROM archive_sessions s WHERE s.id = :id"), {"id": sid})).first()
        if row is not None and (row[0] != row[2] or row[1] != row[3]):
            off.append(sid)
    if any(left.values()) or off:
        raise ImportFailed("rollback of run %d did not come out exact (rows left: %s; sessions off: %s); nothing "
                           "was changed" % (run_id, left, off))
    result = {"run_id": run_id, "status": "rolled_back", "deleted": deleted, "restored": restored,
              "reverted": reverted, "recomputed_sessions": len(touched), "kept_converted_drafts": kept,
              "discarded_reviewed_items": len(reviewed) if discard_reviewed else 0,
              "promoted_items_marked": promoted, "rolled_back_at": now().isoformat()}
    report["rollback"] = {k: v for k, v in result.items() if k not in ("run_id", "status")}
    await set_status(session, run_id, expected_status, status="rolled_back", rolled_back_at=now(),
                     rolled_back_by=actor[:200], report=report)
    await audit.record(session, actor, "brain.import_rolled_back", "import_run", run_id,
                       {"run": run_id, "deleted": deleted, "restored": restored, "reverted": reverted,
                        "kept_converted_drafts": kept, "discarded_reviewed_items": result["discarded_reviewed_items"]})
    await session.flush()
    return result


async def _blocked_effects(session: AsyncSession, run_id: int, conflicts: list[MemoryConflict]) -> list[int]:
    """Conflicts whose effects can no longer be reverted: a target was changed again (or removed) since."""
    blocked = []
    for c in conflicts:
        effects = c.effects or {}
        ok = True
        for e in effects.get("items") or []:
            item = await session.get(MemoryItem, int(e["id"]))
            if item is None:
                ok = False
            elif item.import_run_id != run_id and item.version != int(e["version_after"]):
                ok = False
        for e in effects.get("sites") or []:
            site = await session.get(Site, int(e["id"]))
            if site is not None and site.updated_at != datetime.fromisoformat(e["updated_at_after"]):
                ok = False
        for e in effects.get("drafts") or []:
            draft = await session.get(WorkflowDraft, int(e["id"]))
            if draft is None:
                ok = False
            elif draft.import_run_id != run_id and draft.source_fp != e["fp_after"]:
                ok = False
        if not ok:
            blocked.append(c.id)
    return blocked


async def _revert_effects(session: AsyncSession, run_id: int, conflicts: list[MemoryConflict],
                          actor: str) -> dict[str, int]:
    counts = {"items": 0, "sites": 0, "drafts": 0, "purged_items_reported": 0}
    for c in conflicts:
        effects = c.effects or {}
        for e in effects.get("items") or []:
            item = await session.get(MemoryItem, int(e["id"]))
            if item is None or item.import_run_id == run_id:
                continue
            snap = await session.scalar(select(MemoryItemVersion).where(
                MemoryItemVersion.item_id == item.id, MemoryItemVersion.version == int(e["version_before"])))
            if snap is not None and (snap.title, snap.body, list(snap.tags or []), snap.kind, snap.agent) != (
                    item.title, item.body, list(item.tags or []), item.kind, item.agent):
                item.title, item.body, item.tags = snap.title, snap.body, list(snap.tags or [])
                item.kind, item.agent = snap.kind, snap.agent
                item.content_hash = brain_text.content_hash(item.kind, item.agent, item.title, item.body)
                if c.kind == "source_changed" and (c.existing or {}).get("source_sha256"):
                    item.source_sha256 = c.existing["source_sha256"]
                    at = c.existing.get("source_at")
                    item.source_at = datetime.fromisoformat(at) if at else None
            item.status = str(e["status_before"])
            item.version += 1
            item.updated_at, item.updated_by = now(), actor[:200]
            session.add(store._snapshot(item, "reverted", actor, "import run %d rolled back: conflict %d reverted"
                                        % (run_id, c.id)))
            counts["items"] += 1
        for e in effects.get("sites") or []:
            site = await session.get(Site, int(e["id"]))
            if site is not None:
                await session.delete(site)
                counts["sites"] += 1
        for e in effects.get("drafts") or []:
            draft = await session.get(WorkflowDraft, int(e["id"]))
            if draft is None or draft.import_run_id == run_id:
                continue
            for k, v in (e.get("before") or {}).items():
                if k in WorkflowDraft.__table__.c and k not in ("id", "source_key", "import_run_id", "created_at"):
                    setattr(draft, k, datetime.fromisoformat(v) if k in DATETIME_FIELDS | {"reviewed_at"}
                            and isinstance(v, str) else v)
            draft.updated_at = now()
            counts["drafts"] += 1
        counts["purged_items_reported"] += len(effects.get("purged_items") or [])
    await session.flush()
    return counts


async def _mark_promoted(session: AsyncSession, run_id: int) -> int:
    """Items promoted from messages this rollback deletes keep their text; their source reads 'source removed'."""
    gone = (await session.execute(
        select(ArchiveMessage.id, ArchiveMessage.position, ArchiveSession.source_session_id)
        .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
        .where(ArchiveMessage.import_run_id == run_id))).all()
    if not gone:
        return 0
    ids = {g.id for g in gone}
    refs = {"JARVIS session %s · message %s" % (g.source_session_id, g.position) for g in gone}
    marked = 0
    for item in (await session.scalars(select(MemoryItem).where(MemoryItem.origin == "promoted"))).all():
        message_id = (item.flags or {}).get("message_id")
        if (isinstance(message_id, int) and message_id in ids) or item.source_ref in refs:
            if not item.source_ref.endswith("(source removed)"):
                item.source_ref = (item.source_ref + " (source removed)").strip()
                marked += 1
    await session.flush()
    return marked


async def rollback(run_id: int, *, actor: str = DEFAULT_ACTOR, discard_reviewed: bool = False,
                   confirm: bool = False) -> dict[str, Any]:
    """The CLI's rollback: without confirm it only says what would be deleted (and refuses)."""
    async with sessionmaker()() as s:
        await require_schema(s)
        run_row = await s.get(ImportRun, run_id)
        if run_row is None:
            raise Refused("there is no import run %d" % run_id)
        if not confirm:
            counts = {t: int(await s.scalar(sql("SELECT count(*) FROM %s WHERE import_run_id = :r" % t),
                                            {"r": run_id}) or 0) for t in BRAIN_TABLES}
            reviewed = int(await s.scalar(select(func.count()).select_from(MemoryItem).where(
                MemoryItem.import_run_id == run_id,
                or_(MemoryItem.version > 1, MemoryItem.status.not_in(("pending_review", "quarantined")),
                    MemoryItem.deleted_at.is_not(None)))) or 0)
            raise Refused("nothing was changed: run %d created %s; %d of its items were reviewed or edited. Add --yes "
                          "to roll it back" % (run_id, ", ".join("%d %s" % (n, t) for t, n in counts.items()),
                                               reviewed))
        result = await rollback_run(s, run_id, actor, discard_reviewed)
        await s.commit()
    return result


# --- status ------------------------------------------------------------------------------------------------------


async def status(run_id: int | None = None) -> dict[str, Any]:
    async with sessionmaker()() as s:
        installed = bool(await s.scalar(sql("SELECT to_regclass('public.import_runs') IS NOT NULL")))
        migration = bool(await s.scalar(sql(
            "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE version = '0006_brain')")))
        out: dict[str, Any] = {"importer_version": IMPORTER_VERSION, "installed": installed,
                               "migration_0006_brain": migration, "runs": []}
        if not installed:
            return out
        out["marked_interrupted"] = await mark_dead_runs(s)
        query = select(ImportRun).order_by(ImportRun.id.desc())
        query = query.where(ImportRun.id == run_id) if run_id is not None else query.limit(20)
        for r in (await s.scalars(query)).all():
            out["runs"].append({
                "id": r.id, "status": r.status, "label": kit.label(r.label, "(label)") if r.label else "",
                "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at),
                "heartbeat_at": _iso(r.heartbeat_at), "checkpoint": {k: v for k, v in (r.checkpoint or {}).items()
                                                                      if k in ("phase", "index")},
                "records": (r.counts or {}).get("records"), "by_action": (r.counts or {}).get("by_action", {}),
                "error": r.error, "verified": ((r.report or {}).get("verification") or {}).get("passed"),
                "rolled_back_at": _iso(r.rolled_back_at)})
        if run_id is not None and not out["runs"]:
            raise Refused("there is no import run %d" % run_id)
    return out


# --- rescan ------------------------------------------------------------------------------------------------------


def _reclean_json(value: Any) -> tuple[Any, bool, list[str]]:
    """(cleaned value, changed, restricted types) for a JSONB value."""
    if value in (None, {}, []) or isinstance(value, (bool, int, float)):
        return value, False, []
    cleaned = brain_text.clean(json.dumps(value, ensure_ascii=False), "chat")
    if cleaned.withheld:
        return ({} if isinstance(value, dict) else [] if isinstance(value, list) else ""), True, cleaned.restricted
    try:
        new = json.loads(cleaned.text)
    except ValueError:
        new = value
    return new, new != value, []


async def rescan(*, apply: bool, actor: str = DEFAULT_ACTOR) -> dict[str, Any]:
    """Re-clean every stored brain text and JSONB value with the current detectors (spec 2.1)."""
    counts = {"items_changed": 0, "items_withheld": 0, "versions_changed": 0, "messages_changed": 0,
              "sessions_changed": 0, "sessions_withheld": 0, "conflicts_changed": 0, "drafts_changed": 0,
              "records_changed": 0}
    locks = await LockConnection.open()
    try:
        if not await locks.try_lock(GLOBAL_LOCK):
            raise Refused("an import, rollback or rescan is running")
        async with sessionmaker()() as s:
            await require_schema(s)
            await _rescan_items(s, counts, apply, actor)
            withhold, touched = await _rescan_messages(s, counts, apply)
            await _rescan_sessions(s, counts, apply, withhold, touched)
            await _rescan_json(s, counts, apply)
            if apply:
                await audit.record(s, actor, "brain.rescanned", "brain", "", dict(counts))
                await s.commit()
            else:
                await s.rollback()
    finally:
        await locks.close()
    return {"applied": apply, **counts}


async def _rescan_items(s: AsyncSession, counts: dict[str, int], apply: bool, actor: str) -> None:
    last = 0
    while True:
        items = list((await s.scalars(select(MemoryItem).where(MemoryItem.id > last).order_by(MemoryItem.id)
                                      .limit(200))).all())
        if not items:
            break
        for item in items:
            last = item.id
            kind = (item.flags or {}).get("text_kind", "chat")
            t, b = mapping.clean(item.title, "chat"), mapping.clean(item.body, kind)
            if t.withheld or b.withheld:
                counts["items_withheld"] += 1
                if apply:
                    flags = dict(item.flags or {})
                    flags["restricted"] = sorted(set(t.restricted) | set(b.restricted))
                    item.title, item.body, item.flags = "Withheld: a later scan found restricted data", "", flags
                    item.status = "quarantined" if item.status != "rejected" else item.status
                    _bump(s, item, actor, "restricted data found by a rescan; the text was removed")
                continue
            new_title = brain_text.one_line(t.text, store.MAX_TITLE) or "untitled"
            raises = (t.masked or b.masked) and item.sensitivity == "general"
            if new_title != item.title or b.text != item.body or raises:
                counts["items_changed"] += 1
                if apply:
                    flags = dict(item.flags or {})
                    secrets = dict(flags.get("secrets_redacted") or {})
                    for k, v in {**t.secret_types, **b.secret_types}.items():
                        secrets[k] = secrets.get(k, 0) + v
                    if secrets:
                        flags["secrets_redacted"] = secrets
                    item.title, item.body, item.flags = new_title, b.text, flags
                    if raises:
                        item.sensitivity = "customer"
                    if (t.secret_types or b.secret_types) and item.status != "rejected":
                        item.status = "quarantined"
                    _bump(s, item, actor, "re-cleaned by a rescan with the current detectors")
        await s.flush()
    last = 0
    while True:
        versions = (await s.execute(select(MemoryItemVersion, MemoryItem.flags).join(
            MemoryItem, MemoryItem.id == MemoryItemVersion.item_id).where(MemoryItemVersion.id > last)
            .order_by(MemoryItemVersion.id).limit(500))).all()
        if not versions:
            break
        for v, flags in versions:
            last = v.id
            kind = (flags or {}).get("text_kind", "chat")
            t, b = mapping.clean(v.title, "chat"), mapping.clean(v.body, kind)
            title = "[withheld]" if t.withheld or b.withheld else t.text
            body = "" if t.withheld or b.withheld else b.text
            if title != v.title or body != v.body:
                counts["versions_changed"] += 1
                if apply:
                    v.title, v.body = title, body
        await s.flush()


def _bump(s: AsyncSession, item: MemoryItem, actor: str, note: str) -> None:
    """A rescan's change to an item, as a new 'redacted' version."""
    item.content_hash = brain_text.content_hash(item.kind, item.agent, item.title, item.body)
    item.version += 1
    item.updated_at, item.updated_by = now(), actor[:200]
    s.add(store._snapshot(item, "redacted", actor, note))


async def _rescan_messages(s: AsyncSession, counts: dict[str, int], apply: bool) -> tuple[set[int], set[int]]:
    """(sessions to withhold, sessions whose messages changed)."""
    withhold: set[int] = set()
    touched: set[int] = set()
    last = 0
    while True:
        rows = (await s.execute(select(ArchiveMessage.id, ArchiveMessage.session_id, ArchiveMessage.content,
                                       ArchiveMessage.tool_calls, ArchiveMessage.sensitivity,
                                       ArchiveMessage.redactions, ArchiveMessage.flags)
                                .where(ArchiveMessage.id > last, ArchiveMessage.withheld.is_(False))
                                .order_by(ArchiveMessage.id).limit(1000))).all()
        if not rows:
            break
        for r in rows:
            last = r.id
            c = mapping.clean(r.content, "chat")
            calls, restricted, secrets, masked = [], list(c.restricted), dict(c.secret_types), dict(c.masked)
            for call in r.tool_calls or []:
                ac = mapping.clean(str(call.get("arguments", "")), "chat")
                restricted += ac.restricted
                for k, v in ac.secret_types.items():
                    secrets[k] = secrets.get(k, 0) + v
                for k, v in ac.masked.items():
                    masked[k] = masked.get(k, 0) + v
                calls.append({**call, "arguments": ac.text})
            if restricted:
                withhold.add(r.session_id)
                continue
            raises = bool(masked) and r.sensitivity == "general"
            if c.text != r.content or calls != (r.tool_calls or []) or raises:
                counts["messages_changed"] += 1
                touched.add(r.session_id)
                if apply:
                    redactions = dict(r.redactions or {})
                    for k, v in secrets.items():
                        redactions[k] = redactions.get(k, 0) + v
                    await s.execute(update(ArchiveMessage).where(ArchiveMessage.id == r.id).values(
                        content=c.text, tool_calls=calls, redactions=redactions,
                        sensitivity="customer" if raises else r.sensitivity))
    return withhold, touched


async def _rescan_sessions(s: AsyncSession, counts: dict[str, int], apply: bool, withhold: set[int],
                           touched: set[int]) -> None:
    changed: set[int] = set()
    for sid, title in (await s.execute(select(ArchiveSession.id, ArchiveSession.title))).all():
        c = mapping.clean(title or "", "chat")
        if c.withheld:
            withhold.add(sid)
        elif c.text != title:
            changed.add(sid)
            if apply:
                await s.execute(update(ArchiveSession).where(ArchiveSession.id == sid).values(title=c.text))
    for sid in sorted(withhold):
        counts["sessions_withheld"] += 1
        if apply:
            await s.execute(update(ArchiveMessage).where(ArchiveMessage.session_id == sid).values(
                content="", tool_calls=[], labels=[], sensitivity="restricted", withheld=True))
            await s.execute(update(ArchiveSession).where(ArchiveSession.id == sid).values(title="", summary=""))
    counts["sessions_changed"] = len((changed | touched) - withhold)
    if apply:
        s.expire_all()
        for sid in sorted(changed | touched | withhold):
            await recompute_session(s, sid)


async def _rescan_json(s: AsyncSession, counts: dict[str, int], apply: bool) -> None:
    for c in (await s.scalars(select(MemoryConflict))).all():
        changed = False
        title = mapping.clean(c.title, "chat")
        new_title = "[withheld]" if title.withheld else title.text
        if new_title != c.title:
            changed = True
            if apply:
                c.title = new_title
        for column in ("existing", "incoming"):
            value, diff, _ = _reclean_json(getattr(c, column))
            if diff:
                changed = True
                if apply:
                    setattr(c, column, value)
        counts["conflicts_changed"] += int(changed)
    for d in (await s.scalars(select(WorkflowDraft))).all():
        changed = False
        spec = mapping.clean(d.spec, "chat")
        new_spec = "" if spec.withheld else spec.text
        if new_spec != d.spec:
            changed = True
            if apply:
                d.spec = new_spec
        for column in ("approval", "error_handling", "duplicate_guard", "history", "notes", "params"):
            value, diff, _ = _reclean_json(getattr(d, column))
            if diff:
                changed = True
                if apply:
                    setattr(d, column, value)
        counts["drafts_changed"] += int(changed)
    last = 0
    while True:
        records = list((await s.scalars(select(ImportRecord).where(ImportRecord.id > last, ImportRecord.before != {})
                                        .order_by(ImportRecord.id).limit(500))).all())
        if not records:
            break
        for r in records:
            last = r.id
            value, diff, _ = _reclean_json(r.before)
            if diff:
                counts["records_changed"] += 1
                if apply:
                    r.before = value
        await s.flush()
    await s.flush()


# --- review decisions (test copy -> production) ------------------------------------------------------------------


DECISION_STATUS = {"approved": "approve", "rejected": "reject", "archived": "archive"}


async def decisions_export(out_path: str) -> dict[str, Any]:
    """Every imported item a person reviewed or edited in this database, keyed by source_key and by the content hash
    of the imported version (so production applies a decision only to the very same import)."""
    if os.path.exists(out_path):
        raise Refused("the output file already exists; nothing is overwritten")
    decisions, skipped = [], 0
    async with sessionmaker()() as s:
        await require_schema(s)
        items = (await s.scalars(select(MemoryItem).where(
            MemoryItem.origin == "import",
            or_(MemoryItem.version > 1, MemoryItem.reviewed_by != "", MemoryItem.deleted_at.is_not(None)))
            .order_by(MemoryItem.id))).all()
        for item in items:
            if not item.source_key:
                skipped += 1
                continue
            first = await s.scalar(select(MemoryItemVersion).where(MemoryItemVersion.item_id == item.id)
                                   .order_by(MemoryItemVersion.version).limit(1))
            if first is None:
                skipped += 1
                continue
            decisions.append({
                "source_key": item.source_key,
                "content_hash": brain_text.content_hash(first.kind, first.agent, first.title, first.body),
                "status": item.status, "kind": item.kind, "agent": item.agent, "title": item.title,
                "body": item.body, "tags": list(item.tags or []), "reviewed_by": item.reviewed_by,
                "review_note": item.review_note, "deleted": item.deleted_at is not None})
    with open(out_path, "x", encoding="utf-8") as f:
        json.dump({"format": "jarvis-brain-decisions", "version": 1, "exported_at": now().isoformat(),
                   "importer_version": IMPORTER_VERSION, "decisions": decisions}, f, indent=2, sort_keys=True)
    return {"exported": len(decisions), "skipped_without_source_key": skipped}


async def decisions_apply(in_path: str, run_id: int, *, actor: str, apply: bool) -> dict[str, Any]:
    """Apply exported decisions to the production import run where the item is the very same (same source_key and
    content hash, untouched since). Anything else is listed by decision number, not applied."""
    try:
        with open(in_path, "rb") as f:
            data = json.load(f)
    except (OSError, ValueError):
        raise Refused("the decisions file could not be read as JSON") from None
    if not isinstance(data, dict) or data.get("format") != "jarvis-brain-decisions":
        raise Refused("this is not a decisions file exported by brain-import decisions-export")
    listed: dict[str, list[int]] = {}
    applied = 0
    async with sessionmaker()() as s:
        await require_schema(s)
        user = await s.scalar(select(User).where(func.lower(User.email) == actor.lower(), User.role == "admin",
                                                 User.is_active.is_(True)))
        if user is None:
            raise Refused("--actor must be the e-mail address of an active admin: decisions that need an admin keep "
                          "needing one")
        if await s.get(ImportRun, run_id) is None:
            raise Refused("there is no import run %d" % run_id)
        for n, d in enumerate(data.get("decisions") or [], 1):
            reason = await _apply_decision(s, d, run_id, user, apply)
            if reason:
                listed.setdefault(reason, []).append(n)
            else:
                applied += 1
        if apply:
            await audit.record(s, user.email, "brain.decisions_applied", "import_run", run_id,
                               {"run": run_id, "applied": applied,
                                "not_applied": {k: len(v) for k, v in listed.items()}})
            await s.commit()
        else:
            await s.rollback()
    return {"applied" if apply else "would_apply": applied, "not_applied": listed}


async def _apply_decision(s: AsyncSession, d: Any, run_id: int, user: User, apply: bool) -> str:
    if not isinstance(d, dict) or not isinstance(d.get("source_key"), str):
        return "not a decision"
    item = await s.scalar(select(MemoryItem).where(MemoryItem.source_key == d["source_key"],
                                                   MemoryItem.import_run_id == run_id))
    if item is None:
        return "no such item in this run"
    if item.content_hash != d.get("content_hash") or item.version != 1:
        return "the item differs from the test copy's import, or was changed since"
    if SENSITIVE_AGENT in (item.agent, d.get("agent")) and not user.can_access_sensitive:
        return "needs the sensitive-data permission"
    actor = user.email

    async def change() -> None:
        if d.get("deleted"):
            await store.delete_item(s, item, actor=actor, note=DECISION_NOTE)
            return
        edits = {k: d.get(k) for k in ("title", "body", "tags", "kind", "agent")
                 if d.get(k) is not None and d.get(k) != getattr(item, k)}
        if edits:
            await store.edit_item(s, item, actor=actor, note=DECISION_NOTE, **edits)
        decision = DECISION_STATUS.get(str(d.get("status")))
        if decision and item.status != d.get("status"):
            await store.review_item(s, item, decision=decision, actor=actor, note=DECISION_NOTE)

    if not apply:
        return ""
    try:
        async with s.begin_nested():
            await change()
    except (store.BrainError, DataError, IntegrityError):
        return "refused by the memory service"
    return ""
