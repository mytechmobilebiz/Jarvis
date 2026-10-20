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

    {"items":  [{"id": int, "status_before": str, "version_before": int, "version_after": int,
                 "flags_before"?: {<memory_items.flags before the change>}, "rejected_source_sha256"?: str}],
     "sites":  [{"id": int, "updated_at_after": "<ISO 8601 time>"}],
     "drafts": [{"id": int, "before": {<workflow_drafts fields before the change>}, "fp_after": str}],
     "purged_items": [{"id": int, "source_key": str | null}]}

- items: the item's version after the resolution (version_after) and before it (version_before). Rollback reverts an
  item only while its current version is still version_after and version_before was not redacted since: it restores
  status_before, flags_before (recorded when the resolution replaced the flags) and, when the content differs, the
  title, body, tags, kind and agent of version_before (and, for a source_changed conflict, the source_sha256 and
  source_at the importer recorded in the conflict's "existing"), as a new version with change "reverted". A restored
  text gets the risk flags it calls for (text_risk_flags), and it stays approved only when it was approved with every
  risk flag it now has; otherwise it is quarantined.
  An entry with rejected_source_sha256 records only that the resolution added that hash to the item's
  flags.rejected_source_sha256, without a new version: rollback takes the hash out again and changes nothing else,
  whatever happened to the item since.
- sites: a site the resolution registered. Rollback deletes it only while its updated_at still equals
  updated_at_after.
- drafts: a draft the resolution replaced. Rollback restores "before" only while the draft's source_fp still equals
  fp_after.
- purged_items: incoming items the resolution purged; they are reported, nothing is restored.
If any target moved on since, the rollback refuses and lists the conflicts. Effects on rows the rolled-back run
created itself are skipped: those rows are deleted anyway.

Items promoted from archive messages (origin "promoted") have no import run and survive a rollback; when their
message is deleted, their source_ref is marked "source removed". They are matched by flags.message_id, by
flags.message_source_key, or by the source_ref "JARVIS session <source id> · message <position>"
(archive.promoted_ref). A message withheld by an import (every row it stores without its text: a restricted
conversation, new or not, and a row an admin's tombstone names), a rescan, a rollback (a row put back without its text
because an admin withheld it) or decisions-apply (also when no row of it is left) empties its promoted items as an
admin's withhold does (archive.withhold_copies); such an emptied item is never changed by a conflict's revert.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import urllib.parse
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg
from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy import text as sql
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..config import get_settings
from ..db import sessionmaker
from ..models import (
    ArchiveMessage,
    ArchiveSession,
    BrainPurgedContent,
    BrainTombstone,
    BrainWithholdExemption,
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
from . import IMPORTER_VERSION, identity
from . import jarvis_kit as kit
from . import mapping
from . import source as src
from . import store
from . import text as brain_text
from .archive import (
    WITHHELD_BY_ADMIN,
    WITHHELD_ROW,
    blank_before,
    promoted_ref,
    recompute_session,
    scrub_before,
    sweep_withheld_copies,
    sweep_withheld_records,
    withheld_copies,
    withheld_later,
    withheld_rollback_copies,
    withhold_copies,
)
from .quotes import refresh_quotes, scrub_quotes
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
CARRIED = identity.CARRIED
SAME_AS_PREVIOUS = identity.SAME_AS_PREVIOUS
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
                "columns_read": self.backup.columns_read, "pre_backup_sha256": pre_backup_sha256.lower(),
                "discovery_check": self.discovery}  # a resumed run reports it too (resume takes no --discovery)

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
            "time_range": {"first": None, "last": None},
            # 1.1.0: what the withhold identity did (spec 2.9): rows stored without text as the same message as a
            # withheld one, by rule; rows held for an admin's review; keys that named another JARVIS row; stored rows
            # re-keyed in place; items back under another key after a purge.
            "withheld_same_message": {}, "held_for_review": {}, "key_reassigned": 0, "keys_adopted": 0,
            "purged_same_text": 0, "stones_added": 0}


class Tally:
    """Counts for the report. Kept in the checkpoint, so a resumed run reports the whole run."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.d = json.loads(json.dumps(data)) if data else _empty_tally()
        for k, v in _empty_tally().items():  # a run resumed from an older checkpoint
            self.d.setdefault(k, v)

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

    def held(self, via: str, review: str) -> None:
        self._inc(self.d["held_for_review" if review == identity.PENDING else "withheld_same_message"], via)

    def bump(self, name: str, n: int = 1) -> None:
        self.d[name] = int(self.d.get(name) or 0) + n

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
        """Refreshes heartbeat_at, and checks that this connection still holds the run's lock. Once it does not (the
        connection was lost), another import could start, so the run is stopped: the calling task is cancelled, which
        marks the run interrupted."""
        main = asyncio.current_task()

        async def beat() -> None:
            while True:
                await asyncio.sleep(seconds)
                try:
                    async with self._io:
                        held = await self.conn.fetchval(
                            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid() "
                            "AND granted AND classid::bigint = $1 AND objid::bigint = $2 AND objsubid = 2",
                            LOCK_KEY % 2 ** 32, run_id)
                        await self.conn.execute(
                            "UPDATE import_runs SET heartbeat_at = now() WHERE id = $1 AND status = 'running'", run_id)
                except Exception:  # the connection is gone, and its locks with it
                    held = 0
                if not held:
                    if main is not None:
                        main.cancel()
                    return
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
    if not await session.scalar(sql(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = 'brain_tombstones' AND column_name = 'anchor_key')")):
        raise Refused("the JARVIS Brain needs migration 0008_brain_withhold_identity: start the Command Center 0.6.2 "
                      "once first")


async def purge_key_check(session: AsyncSession) -> None:
    """Refused (before any change) when stored purge fingerprints were made with another database password."""
    try:
        await identity.PurgeIndex.load(session)
    except identity.PurgeKeyMismatch as exc:
        raise Refused(str(exc)) from None


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
    twin_of: str = ""  # the key of an earlier rewound row with the same text (an identity link only)


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
        self.pending_at: list[str] = []  # the location of each pending record, for an error at the flush
        self._fake = 0
        self.tombstones: set[str] = set()  # purges (and other non-withhold tombstones): the record is left out
        # The identity of every withheld message (its stones, identity.py), and per session the rows that are the same
        # message as a withheld one (key -> Match: stored without text) and the keys JARVIS gave to another row.
        self.ids = identity.Identities()
        self.held: dict[str, identity.Match] = {}
        self.moved_keys: set[str] = set()
        # Texts and contents of the rows matched as members, per group: in memory for this run only.
        self.group_texts: dict[str, set[tuple[str, str, str]]] = {}
        self.group_contents: dict[str, list[str]] = {}
        self._anchor_cache: dict[str, tuple[int, dict[tuple[str, str, str], identity.Stone]]] = {}
        self._bound_rows: dict[tuple[str, str], src.SourceMessage | None] = {}
        self._uid_backfill: list[tuple[int, str]] = []
        self._by_session_id: dict[str, src.SourceSession] | None = None
        self.purge = identity.PurgeIndex()
        self.purged_shas_here: set[str] = set()
        # Rows this batch withheld (for an admin, or by _reconcile; copies go too), inserted (nothing can quote them
        # yet), deleted (their quotes go) and deleted while withheld (their promoted copies go too: finding 2):
        # handled once per batch, by withhold_batch().
        self.withheld_now: list[int] = []
        self.inserted_now: set[int] = set()
        self.deleted_now: list[int] = []
        self.withheld_gone: list[Any] = []
        self.site_urls: dict[str, int] = {}
        self.schedules: list[dict[str, Any]] = []
        self.new_hashes: dict[str, int] = {}
        self.new_titles: dict[tuple[str, str, str], int] = {}
        self.new_conflicts: dict[str, int] = {}
        self.drafts: list[mapping.DraftPlan] = []

    async def load(self, work: Work) -> None:
        rows = (await self.s.execute(select(BrainTombstone.source_key, BrainTombstone.reason))).all()
        self.tombstones = {key for key, reason in rows if reason != WITHHELD_BY_ADMIN}
        self.ids = await identity.Identities.load(self.s)
        try:
            self.purge = await identity.PurgeIndex.load(self.s)
        except identity.PurgeKeyMismatch as exc:
            raise Refused(str(exc)) from None
        self.purged_shas_here = {p.sha256 for f in work.files for p in mapping.file_parts(f, self.tz)
                                 if p.key in self.tombstones and p.sha256}
        await self._rebind()
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

    async def _rebind(self) -> None:
        """A stone bound to a JARVIS row that is in this backup learns that row's uid when it had none (a message
        withheld while its uid was NULL, or by 0.6.x under a row-form key): learned, so uid_known stays false. With
        write, the uid is kept. The uids a withheld fold survivor absorbed are learned in memory."""
        backup = self.src.backup
        for st in sorted(self.ids.by_key.values(), key=lambda st: st.key):
            if not (st.bound and st.row and st.session and st.certain) or not backup.has_row(st.session, st.row):
                continue
            found = [r for r in backup.rows(st.session, [st.row]) if st.role in ("", r.role)]
            if not found:
                continue
            if not st.uid and found[0].uid and self.write:
                await self.s.execute(update(BrainTombstone).where(
                    BrainTombstone.source_key == st.key, BrainTombstone.message_uid == "").values(
                    message_uid=found[0].uid[:200]))
            self.ids.learn_uid(st, found[0].uid[:200], found[0].absorbed)

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
            self.pending_at.append(self.location)

    async def flush(self) -> None:
        """The batch's import records. A source key recorded twice in one run is refused (UNIQUE (run_id,
        source_key)): the run then fails at the record that repeated it, not at the batch's last one."""
        if not self.pending:
            return
        try:
            async with self.s.begin_nested():
                await self.s.execute(insert(ImportRecord), self.pending)
        except IntegrityError:
            self.location = await self._repeated_record() or self.location
            raise
        self.pending, self.pending_at = [], []

    async def _repeated_record(self) -> str:
        """The location of the first pending record whose key this run has recorded already."""
        keys = [r["source_key"] for r in self.pending]
        done: set[str] = set()
        for i in range(0, len(keys), 1000):
            done.update((await self.s.scalars(select(ImportRecord.source_key).where(
                ImportRecord.run_id == self.run_id, ImportRecord.source_key.in_(keys[i:i + 1000])))).all())
        for key, at in zip(keys, self.pending_at, strict=True):
            if key in done:
                return at
            done.add(key)
        return ""

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
            fp = self.purge.fingerprint(p.sha256)
            if self.write and fp is not None and fp not in self.purge.fps:
                # A purge made before 0.6.2 (or on a test copy) learns the keyed fingerprint of the bytes it purged,
                # so a later rename or copy of the same file is recognised too.
                await identity.remember_purge(self.s, source_key=p.key, sha256_hex=p.sha256, kind=p.record_type,
                                              actor=self.actor, via="import", key=self.purge.key)
                self.purge.fps.add(fp)
            return rec("excluded", "purged by an admin")
        if p.excluded:
            if p.restricted:
                self.t.restricted(p.restricted, file=True)
            return rec("excluded", p.excluded)
        if p.duplicate_of_earlier:
            return rec("duplicate", "the same text appears earlier in the file")
        if self.purge.same_content(p, self.purged_shas_here):
            # An item an admin purged, back under another key (moved, copied or renamed): purged as well, by a
            # tombstone of its own (no new data, no text, no plain hash).
            if self.write:
                await self.s.execute(pg_insert(BrainTombstone).values(
                    source_key=p.key, purged_at=now(), purged_by=self.actor[:200], reason=identity.PURGED_BY_ADMIN,
                    via="same text").on_conflict_do_nothing(index_elements=[BrainTombstone.source_key]))
            self.tombstones.add(p.key)
            self.t.bump("purged_same_text")
            return rec("excluded", "purged by an admin: the same text as an item an admin purged")
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
        if d.duplicate_of_earlier:
            return self.record(d.key, d.record_type, "duplicate", "the same job id appears earlier in jobs.json")
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
        return identity.dup_fp(m)

    def clean_message(self, m: src.SourceMessage, customer: bool) -> MessageClean:
        """customer: every message of this session is customer data (Sarah's sessions, customer-facing channels)."""
        tool = m.role == "tool"
        c = mapping.clean(m.content[:TOOL_IN if tool else CHAT_IN], "chat")
        content, cut_ = mapping.cut(c.text, TOOL_OUT if tool else CHAT_OUT)
        truncated = cut_ or len(m.content) > (TOOL_IN if tool else CHAT_IN)
        total = brain_text.Cleaned("", dict(c.secret_types), list(c.restricted), dict(c.masked), c.think_removed)
        calls = []
        # Customer work done through a generic tool: a command or arguments that name it, a file path in a result.
        work = tool and src.customer_path(c.text)
        for call in src.parse_tool_calls(m.tool_calls_raw):
            args = call["arguments"]
            args_text = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            ac = mapping.clean(args_text[:TOOL_IN], "chat")
            total.merge(ac)
            work = work or src.customer_work(ac.text)
            name = brain_text.sanitize(call["name"])[:200]
            calls.append({"name": name, "arguments": mapping.cut(ac.text, ARG_OUT)[0]})
        restricted = sorted(set(total.restricted))
        names = [c["name"] for c in calls]
        if restricted:
            sensitivity = "restricted"
        elif (total.masked or customer or work or src.customer_tool(m.tool_name)
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

    def carried_copies(self, msgs: list[src.SourceMessage]) -> dict[str, str]:
        """Live rows that are carried-on copies of compacted rows: their key -> the key of the compacted row copied.

        Hermes's compaction archives the original turns (compacted = 1), then writes the summary and the turns it
        carries on as new live rows right after them. Only that block is looked at, one to one: a row there whose
        (role, tool_name, tool_call_id, content, tool_calls) equals a compacted row not matched yet is a copy of it;
        the first row without one is the summary, and the next row without one ends the block. A later turn that
        says the same thing again ("Sent the invoice.", "yes") is never a copy."""
        waiting: dict[tuple[str, str], list[str]] = {}
        copies: dict[str, str] = {}
        block = ""  # "open" right after compacted rows, "summary" once the summary was passed, "" outside a block
        for m in msgs:
            if self.exclusion(m):
                continue
            key = (self.dup_fp(m), m.tool_call_id)
            if m.compacted == 1:
                waiting.setdefault(key, []).append(m.key)
                block = "open"
            elif block and waiting.get(key):
                copies[m.key] = waiting[key].pop(0)
            elif block:
                block = "summary" if block == "open" else ""
        return copies

    def plan_messages(self, msgs: list[src.SourceMessage], customer: bool, unchanged: frozenset[str] = frozenset()
                      ) -> tuple[list[MessagePlan], set[str]]:
        """What each source row becomes, from the source alone (so a re-import takes the same decisions).

        customer: every message is customer data (clean_message()).
        unchanged: keys of rows an earlier run stored in this session from the very same source row (same source_fp).
        They are not cleaned again (cleaning is most of an import's time): the stored row already holds what the
        detectors found then, and `rescan` is how stored text meets newer detectors."""
        restricted: set[str] = set()
        copies = self.carried_copies(msgs)
        plans: list[MessagePlan] = []
        prev: tuple[str, str] | None = None
        prev_key = ""
        # Rewound rows (by text): a later row with the same text is their twin (Hermes's in-place compaction rewinds
        # the protected tail and writes it again as new rows; /retry and replace do the same). An identity link only:
        # the twin's action is decided as for any row.
        rewound: dict[tuple[str, str], list[str]] = {}
        for m in msgs:
            reason = self.exclusion(m)
            if reason:
                if m.content.strip() or src.parse_tool_calls(m.tool_calls_raw):
                    # Not stored, but a conversation that held restricted data (in its system prompt, a rewound turn
                    # or a purged row) is withheld whole.
                    try:
                        restricted |= set(self.clean_message(m, customer).restricted)
                    except Exception:  # the row is excluded anyway
                        pass
                if reason == REWOUND:
                    rewound.setdefault((self.dup_fp(m), m.tool_call_id), []).append(m.key)
                plans.append(MessagePlan(m, "excluded", reason))
                continue
            fp = self.dup_fp(m)
            twin = rewound.get((fp, m.tool_call_id))
            twin_of = twin.pop(0) if twin else ""
            same_prev = False
            if m.role in ("user", "assistant"):
                current = (fp, m.tool_call_id)
                same_prev = current == prev
                if not same_prev:
                    prev, prev_key = current, m.key
            if m.key in copies:
                plans.append(MessagePlan(m, "duplicate", CARRIED, duplicate_of=copies[m.key], twin_of=twin_of))
                continue
            if same_prev:
                plans.append(MessagePlan(m, "duplicate", SAME_AS_PREVIOUS, duplicate_of=prev_key, twin_of=twin_of))
                continue
            if m.key in unchanged:
                # reconciled as "already imported"; never cleaned again
                plans.append(MessagePlan(m, "import", twin_of=twin_of))
                continue
            try:
                cleaned = self.clean_message(m, customer)
            except Exception as exc:  # a cleaning failure excludes this record only
                plans.append(MessagePlan(m, "excluded", "invalid record: %s" % exc.__class__.__name__))
                continue
            restricted |= set(cleaned.restricted)
            plans.append(MessagePlan(m, "import", cleaned=cleaned, twin_of=twin_of))
        return plans, restricted

    def message_values(self, p: MessagePlan, withheld: bool) -> dict[str, Any]:
        """A row's values. Withheld (restricted data in the conversation, or the same message as one an admin withheld:
        self.held) it keeps no text and no fingerprint of its text (source_fp 'w1:<state>')."""
        m, c = p.m, p.cleaned
        assert c is not None
        values: dict[str, Any] = {
            "source_message_id": str(m.id)[:200], "source_fp": m.fp, "position": m.position,
            "role": brain_text.sanitize(m.role)[:40], "content_chars": len(m.content),
            "tool_name": brain_text.sanitize(m.tool_name)[:200], "tool_call_id": brain_text.sanitize(m.tool_call_id)[:200],
            "at": m.at, "state": "compacted" if m.compacted == 1 else "live", "redactions": c.redactions,
            "source_uid": m.uid[:200],
        }
        held = self.held.get(m.key)
        if withheld:
            values.update(content="", truncated=False, tool_calls=[], sensitivity="restricted", withheld=True,
                          labels=[], flags={**c.flags, "withheld": "restricted data in this conversation"},
                          source_fp=identity.withheld_fp(m))
        elif held is not None:  # the same message as one an admin withheld: it comes back with no text
            values.update(content="", truncated=False, tool_calls=[], sensitivity=c.sensitivity, withheld=True,
                          labels=[], flags={**c.flags, "withheld_by_admin": self._held_flag(held)},
                          source_fp=identity.withheld_fp(m))
        else:
            values.update(content=c.content, truncated=c.truncated, tool_calls=c.calls, sensitivity=c.sensitivity,
                          withheld=False, labels=c.labels, flags=c.flags)
        return values

    @staticmethod
    def _held_flag(mt: identity.Match) -> dict[str, Any]:
        flag: dict[str, Any] = {"note": WITHHELD_BY_ADMIN}
        if mt.via != "key":
            flag["via"] = mt.via
        if mt.review == identity.PENDING:
            flag["review"] = identity.PENDING
        return flag

    # The identity of withheld messages (spec 2.9) ----------------------------------------------------------------

    def held_rows(self, ss: src.SourceSession, msgs: list[src.SourceMessage], plans: list[MessagePlan],
                  rows: dict[str, dict[str, Any]]) -> dict[str, identity.Match]:
        """The source rows of this session that are the same message as a withheld one (key -> Match): members are
        stored without text and inherit a stone; holds as well, listed for an admin. Also sets self.moved_keys: keys
        whose stone names another JARVIS row that is still in the backup (the row is excluded as a key collision).

        Certain rules first (R-key, R-row, R-fold; excluded rows too: a rewound original is an anchor), then the
        importer's own links (R-link), the shared uid (R-uid: a member when the text agrees, an archived generation,
        nothing to compare, or the withheld text contained; a hold otherwise) and the same text in this session or a
        continuation or branch ancestor (R-text: a member in a copy position, a hold otherwise; never a row the admin
        saw, a released row, or a row with its own uid when the withheld message's uid was known)."""
        backup = self.src.backup
        by_key = {m.key: m for m in msgs}
        held: dict[str, identity.Match] = {}
        pend: dict[str, identity.Match] = {}
        self.moved_keys = set()
        for m in msgs:
            mt = self.ids.certain(ss.id, m, backup.has_row)
            if mt is identity.MOVED:
                self.moved_keys.add(m.key)
            elif isinstance(mt, identity.Match) and mt.review == identity.PENDING:
                pend[m.key] = mt  # the hold itself, unless a certain rule matches it below
            elif isinstance(mt, identity.Match):
                held[m.key] = mt
        self._links(plans, held, by_key)
        for m in msgs:
            if m.key in held or not m.uid or m.key in self.moved_keys:
                continue
            cands = self.ids.uid_stones(m.uid, m.role)
            if not cands:
                continue
            st = cands[0]
            texts, contents = self.anchor_texts(st.group, held, by_key)
            content = identity.norm(m.content)
            if (identity.text_key(m) in texts or m.compacted == 1 or not texts
                    or any(len(a) >= identity.MIN_CONTAINED and a in content for a in contents)):
                held[m.key] = identity.Match(st, "same message id", identity.CERTAIN)
            elif (st.group, ss.id, str(m.id)[:200]) not in self.ids.exempt:
                held[m.key] = identity.Match(st, "same message id, different text", identity.PENDING)
        self._links(plans, held, by_key)
        anchors: dict[tuple[str, str, str], identity.Stone] = {}
        for k, mt in held.items():
            if mt.review == identity.CERTAIN:
                anchors.setdefault(identity.text_key(by_key[k]), mt.stone)
        for t, st in self.ancestor_anchors(ss).items():
            anchors.setdefault(t, st)
        if anchors:
            for p in plans:
                m = p.m
                if m.key in held or p.action != "import" or m.key in self.moved_keys:
                    continue
                found = anchors.get(identity.text_key(m))
                if found is None:
                    continue
                row = rows.get(m.key)
                if row is not None and (row["withheld"] or (row["import_run_id"] or 0) <= found.after_run):
                    continue  # stored before the withhold: the admin saw it
                if (found.group, ss.id, str(m.id)[:200]) in self.ids.exempt:
                    continue
                if identity.copied_by_history(m, ss):
                    held[m.key] = identity.Match(found, "copied by JARVIS", identity.CERTAIN)
                elif not self.ids.independent(found.group, m.uid):
                    held[m.key] = identity.Match(found, "same text", identity.PENDING)
            self._links(plans, held, by_key)
        for k, mt in pend.items():
            held.setdefault(k, mt)
        return held

    @staticmethod
    def _links(plans: list[MessagePlan], held: dict[str, identity.Match], by_key: dict[str, src.SourceMessage]
               ) -> None:
        """R-link, in id order: a carried-on copy or an adjacent repeat the importer linked to a held row, and a twin
        of a held rewound row (unless both carry uids and they differ), are held as that row is."""
        for p in plans:
            m = p.m
            if m.key in held:
                continue
            if p.action == "duplicate" and p.reason in identity.PROVENANCE_REASONS and p.duplicate_of in held:
                mt = held[p.duplicate_of]
                held[m.key] = identity.Match(mt.stone, "duplicate", mt.review)
            elif p.twin_of in held and not identity.uid_conflict(by_key[p.twin_of], m):
                mt = held[p.twin_of]
                held[m.key] = identity.Match(mt.stone, "copied by JARVIS", mt.review)

    def _bound_row(self, session_id: str, row_id: str) -> src.SourceMessage | None:
        if (session_id, row_id) not in self._bound_rows:
            backup = self.src.backup
            found = backup.rows(session_id, [row_id]) if backup.has_row(session_id, row_id) else []
            self._bound_rows[(session_id, row_id)] = found[0] if found else None
        return self._bound_rows[(session_id, row_id)]

    def anchor_texts(self, group: str, held: dict[str, identity.Match], by_key: dict[str, src.SourceMessage]
                     ) -> tuple[set[tuple[str, str, str]], list[str]]:
        """The texts (and normalised contents) a group's withheld message has in this backup: rows matched as members
        in this run, and the rows the group's certain stones are bound to. In memory only."""
        texts = set(self.group_texts.get(group, ()))
        contents = list(self.group_contents.get(group, ()))
        for k, mt in held.items():
            if mt.review == identity.CERTAIN and mt.group == group:
                texts.add(identity.text_key(by_key[k]))
                contents.append(identity.norm(by_key[k].content))
        for st in self.ids.groups.get(group, ()):
            if st.certain and st.row and st.session:
                row = self._bound_row(st.session, st.row)
                if row is not None and st.role in ("", row.role):
                    texts.add(identity.text_key(row))
                    contents.append(identity.norm(row.content))
        return texts, contents

    def ancestor_anchors(self, ss: src.SourceSession) -> dict[tuple[str, str, str], identity.Stone]:
        """The texts of withheld rows in the continuation and branch ancestors of this session (a rotation or a branch
        copied their history): walked up with a visited set and no step limit."""
        out: dict[tuple[str, str, str], identity.Stone] = {}
        if ss.lineage not in identity.LINEAGE or not ss.parent_id:
            return out
        sessions = self._source_sessions()
        seen = {ss.id}
        cur: src.SourceSession | None = ss
        while cur is not None and cur.lineage in identity.LINEAGE and cur.parent_id and cur.parent_id not in seen:
            parent = sessions.get(cur.parent_id)
            if parent is None:
                break
            seen.add(parent.id)
            if parent.id in self.ids.sessions:
                for t, st in self._anchors_of(parent).items():
                    out.setdefault(t, st)
            cur = parent
        return out

    def _source_sessions(self) -> dict[str, src.SourceSession]:
        if self._by_session_id is None:
            self._by_session_id = self.src.backup.sessions_by_id()
        return self._by_session_id

    def _anchors_of(self, a: src.SourceSession) -> dict[tuple[str, str, str], identity.Stone]:
        version = self.ids.version[a.id]
        cached = self._anchor_cache.get(a.id)
        if cached is not None and cached[0] == version:
            return cached[1]
        found: dict[tuple[str, str, str], identity.Stone] = {}
        for m in self.src.backup.messages(a.id):
            row_id = str(m.id)[:200]
            st = self.ids.by_key.get(m.key)
            if st is not None and st.certain and (not st.row or not st.session
                                                  or (st.session, st.row) == (a.id, row_id)):
                found.setdefault(identity.text_key(m), st)
                continue
            for st in self.ids.by_row.get((a.id, row_id), ()):
                if st.certain and st.role in ("", m.role):
                    found.setdefault(identity.text_key(m), st)
                    break
        self._anchor_cache[a.id] = (version, found)
        return found

    def _collides(self, ss: src.SourceSession, m: src.SourceMessage, row: dict[str, Any] | None, sess_id: int
                  ) -> bool:
        """The stored row under m's key is another JARVIS row that is still in the backup (or in another session)."""
        if row is None:
            return False
        if row["session_id"] != sess_id:
            return True
        return (not row.get("adopted_from") and row["source_message_id"] != str(m.id)[:200]
                and self.src.backup.has_row(ss.id, row["source_message_id"]))

    async def inherit(self, ss: src.SourceSession, msgs: list[src.SourceMessage], rows: dict[str, dict[str, Any]],
                      sess_id: int) -> None:
        """Every held row's key gets a stone (its group, the rule, the review, the JARVIS row it is now), so later
        imports, rollbacks and every key reader keep it withheld; never a key that names another JARVIS row."""
        by_key = {m.key: m for m in msgs}
        for key, mt in self.held.items():
            m = by_key[key]
            if key in self.moved_keys or self._collides(ss, m, rows.get(key), sess_id):
                continue
            group = mt.group
            existing = self.ids.by_key.get(key)
            if existing is None or (existing.review == identity.PENDING and mt.review == identity.CERTAIN) or (
                    not existing.bound):
                binding = identity.Stone(
                    key=key, anchor="" if key == group else group, session=ss.id[:200], row=str(m.id)[:200],
                    role=m.role[:40], tool_call_id=m.tool_call_id[:200], uid=m.uid[:200], uid_known=False,
                    via=mt.via, review=mt.review, bound=True, after_run=mt.stone.after_run)
                if self.write:
                    await identity.put_stone(self.s, binding, actor=self.actor)
                self.ids.add(binding)
                if existing is None:
                    self.t.bump("stones_added")
            if mt.review == identity.CERTAIN:
                self.group_texts.setdefault(group, set()).add(identity.text_key(m))
                self.group_contents.setdefault(group, []).append(identity.norm(m.content))
            if mt.via != "key" or mt.stone.anchor:
                self.t.held(mt.via, mt.review)

    async def adopt_rows(self, ss: src.SourceSession, msgs: list[src.SourceMessage], rows: dict[str, dict[str, Any]],
                         sess: ArchiveSession | None) -> None:
        """Key adoption: a stored row of this session whose key the backup no longer has, and that is the same JARVIS
        row as a source row whose key has no stored row (JARVIS gave it a uid, or replace_messages renumbered it with
        its uid), is re-keyed in place (_reconcile) instead of the message being stored twice."""
        if sess is None:
            return
        wanted = [m for m in msgs if m.key not in rows and not self.exclusion(m)]
        if not wanted:
            return
        stale = [dict(r) for r in (await self.s.execute(select(*MESSAGE_COLUMNS).where(
            ArchiveMessage.session_id == sess.id, ArchiveMessage.source_key.not_in([m.key for m in msgs]))
            .order_by(ArchiveMessage.id))).mappings().all()]
        if not stale:
            return
        by_row: dict[tuple[str, str], dict[str, Any]] = {}
        by_uid: dict[tuple[str, str], dict[str, Any]] = {}
        for r in stale:
            by_row.setdefault((r["source_message_id"], r["role"]), r)
            if r["source_uid"] and not self.src.backup.has_row(ss.id, r["source_message_id"]):
                by_uid.setdefault((r["source_uid"], r["role"]), r)
        taken: set[int] = set()
        for m in wanted:
            role = brain_text.sanitize(m.role)[:40]
            r = by_row.get((str(m.id)[:200], role))
            if r is None and m.uid:
                r = by_uid.get((m.uid[:200], role))
            if r is None or r["id"] in taken or (r["source_uid"] and m.uid and r["source_uid"] != m.uid[:200]):
                continue
            taken.add(r["id"])
            rows[m.key] = {**r, "adopted_from": r["source_key"]}

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
        await self.adopt_rows(ss, msgs, rows, sess)
        try:
            title_c = mapping.clean(ss.title, "chat")
        except Exception:  # a title that cannot be cleaned is left out
            title_c = brain_text.Cleaned("")
        # A row stored from this very source row is not cleaned again; a re-keyed (adopted) row and a released hold
        # (source_fp '': its text comes back from the source) are.
        unchanged = frozenset(m.key for m in msgs if sess is not None and m.key in rows
                              and rows[m.key]["session_id"] == sess.id and not rows[m.key].get("adopted_from")
                              and rows[m.key]["source_fp"] and identity.same_source(rows[m.key], m))
        customer_channel = src.customer_channel(ss.channel)
        plans, restricted = self.plan_messages(msgs, staff == "sarah" or customer_channel, unchanged)
        restricted |= set(title_c.restricted)
        withheld = bool(restricted) or (sess is not None and sess.sensitivity == "restricted")
        why = ("restricted data in this conversation: %s" % ", ".join(sorted(restricted)) if restricted
               else "restricted data in this conversation (found by an earlier import)")
        # An admin who withheld the first user message hid the title too (title_withheld): it can paraphrase it.
        title_hidden = sess is not None and bool((sess.flags or {}).get("title_withheld"))
        title = "" if withheld or title_hidden else brain_text.one_line(title_c.text, 300)
        flags: dict[str, Any] = {"attribution": ss.attribution.how, "inferred": ["staff", "labels"]}
        if ss.time_assumed_local:
            flags["time_assumed_local"] = True
        if withheld:
            flags["withheld_by_import"] = True
        if title_c.masked and not withheld:
            flags["title_masked"] = True
        if customer_channel:
            flags["customer_channel"] = True  # customer data by default, the session itself included
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
            for k in ("title_masked", "title_redactions", "time_assumed_local", "customer_channel"):
                if k not in flags:
                    current["flags"].pop(k, None)
            source_changed = jsonable(current) != jsonable({f: getattr(sess, f) for f in SESSION_SOURCE_FIELDS})
            if source_changed and self.write:
                for k, v in current.items():
                    setattr(sess, k, v)
                await self.s.flush()
        self.held = self.held_rows(ss, msgs, plans, rows)
        await self.inherit(ss, msgs, rows, sess_id)
        changed = await self.write_messages(plans, rows, sess_id, withheld, why, ss)
        if withheld and not created and not was_restricted and self.write:
            # Newly restricted: every row keeps no text, also one the source no longer holds (finding 3), and no copy
            # of its earlier text is kept for a rollback either (the source keeps it). The items promoted from its
            # rows go too, also from a row an earlier import deleted (finding 2).
            await self.flush()
            changed += (await self.s.execute(sql(
                "UPDATE archive_messages SET content = '', truncated = false, tool_calls = '[]'::jsonb, labels = '{}', "
                "sensitivity = 'restricted', withheld = true, source_fp = 'w1:' || state, "
                "flags = flags || '{\"withheld\": \"restricted data in this conversation\"}'::jsonb "
                "WHERE session_id = :s AND NOT withheld"), {"s": sess_id})).rowcount or 0
            await scrub_before(self.s, archive_session_id=sess_id)
            self.withheld_now += (await self.s.scalars(select(ArchiveMessage.id).where(
                ArchiveMessage.session_id == sess_id))).all()
            self.withheld_gone += await self._gone_promoted_rows(sess_id)
        await self._backfill_uids()
        level = "general"
        if created or changed or source_changed:
            if self.write:
                after = await recompute_session(self.s, sess_id)
                level = after.sensitivity if after else level
            else:
                levels = [p.cleaned.sensitivity for p in plans if p.cleaned] + (
                    ["customer"] if title_c.masked or customer_channel else [])
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
                        sess_id, blank_before(before) if withheld and not was_restricted else before)
        else:
            self.record(ss.key, "session", "duplicate", "already imported", "archive_sessions", sess_id)
        return len(msgs), size

    async def write_messages(self, plans: list[MessagePlan], rows: dict[str, dict[str, Any]], sess_id: int,
                             withheld: bool, why: str, ss: src.SourceSession) -> int:
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
            if self._collides(ss, m, row, sess_id):
                # A stored row is never overwritten with another JARVIS row's text (a uid that stopped being shared).
                self.record(m.key, "message", "excluded", "key collision: JARVIS gave this key to another row")
                continue
            if m.key in self.moved_keys and (row is None or row.get("adopted_from")):
                # The key of a withheld message now names another row: that row is neither stored under it nor
                # withheld by it.
                self.t.bump("key_reassigned")
                self.record(m.key, "message", "excluded",
                            "key collision: JARVIS gave the key of a withheld message to another row")
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
            self.inserted_now.add(outcome)
            held = self.held.get(p.m.key)
            if withheld or held is not None:  # stored without its text: copies of an earlier row go too
                self.withheld_now.append(outcome)
            self._count_message(p, withheld)
            if withheld:
                self.record(p.m.key, "message", "withheld", why, "archive_messages", outcome)
            elif held is not None:
                self.record(p.m.key, "message", "withheld", "held for an admin's review: %s" % held.via
                            if held.review == identity.PENDING else WITHHELD_BY_ADMIN if held.via == "key"
                            else "%s: %s" % (WITHHELD_BY_ADMIN, held.via), "archive_messages", outcome)
            else:
                self.record(p.m.key, "message", "imported", "", "archive_messages", outcome)
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
        """A message an earlier run imported: unchanged, now withheld, changed, compacted, rewound, re-keyed (key
        adoption) or released. A row withheld before (by an import, an admin or a rescan) stays withheld: a newer copy
        of the source never brings its text back; only an admin's release of a hold does (its stone is gone, and the
        row is marked released_at)."""
        m = p.m
        kept = {k: v for k, v in (row["flags"] or {}).items() if k.startswith("withheld")} if row["withheld"] else {}
        session_withheld, withheld = withheld, withheld or bool(row["withheld"])
        held = self.held.get(m.key)
        old_key = row.get("adopted_from")
        same = identity.same_source(row, m)
        released = (bool(row["withheld"]) and not session_withheld and held is None
                    and bool(((row["flags"] or {}).get("withheld_by_admin") or {}).get("released_at"))
                    and m.key not in self.ids.by_key and (old_key or "") not in self.ids.by_key)
        restricted_now = {"content": "", "truncated": False, "tool_calls": [], "sensitivity": "restricted",
                          "withheld": True, "labels": [], "source_fp": identity.withheld_fp(m),
                          "flags": {**(row["flags"] or {}), "withheld": "restricted data in this conversation"}}
        held_now = {"content": "", "truncated": False, "tool_calls": [], "withheld": True, "labels": [],
                    "source_fp": identity.withheld_fp(m),
                    "flags": {**(row["flags"] or {}), "withheld_by_admin": self._held_flag(held)} if held else {}}
        if same and not old_key and not released:
            # Withheld now: the rollback copy keeps no text either (it is never stored to be scrubbed later).
            if withheld and not row["withheld"]:
                await self._update_row(row, {**restricted_now, "source_uid": m.uid[:200]})
                self.record(m.key, "message", "updated", "withheld: " + why, "archive_messages", row["id"],
                            blank_before(jsonable({k: row[k] for k in MESSAGE_FIELDS})))
                self.withheld_now.append(row["id"])
                return 1
            if held is not None and not row["withheld"]:
                # The same message as one an admin withheld, and its row still has its text: an import that ran during
                # the withhold, a withhold decisions-apply could not make, or a copy found since. The conversation
                # stays as it is.
                await self._update_row(row, {**held_now, "source_uid": m.uid[:200]})
                self.record(m.key, "message", "updated", "withheld: an admin withheld it" if held.via == "key"
                            else "withheld: the same message as one an admin withheld (%s)" % held.via,
                            "archive_messages", row["id"], blank_before(jsonable({k: row[k] for k in MESSAGE_FIELDS})))
                self.withheld_now.append(row["id"])
                return 1
            if (row.get("source_uid") or "") != m.uid[:200]:
                self._uid_backfill.append((int(row["id"]), m.uid[:200]))
            self.record(m.key, "message", "duplicate", "already imported", "archive_messages", row["id"])
            return 0
        if p.action in ("excluded", "duplicate"):
            reason = ("rewound since an earlier import" if p.reason == REWOUND
                      else "%s since an earlier import" % p.reason)
            if self.write:
                await self.s.execute(delete(ArchiveMessage).where(ArchiveMessage.id == row["id"]))
                self.deleted_now.append(row["id"])
            self.record(m.key, "message", "updated", reason, "archive_messages", row["id"],
                        {"deleted_row": jsonable({k: v for k, v in row.items() if k != "adopted_from"})})
            if withheld or held is not None:  # its promoted copies go too, also once no row of it is left
                self.withheld_gone.append(SimpleNamespace(id=row["id"], source_key=row["source_key"],
                                                          session_id=row["session_id"], position=row["position"]))
            return 1
        if released:
            # An admin released this hold: its text comes back from the source (unless the conversation is withheld).
            values = self.message_values(p, False)
            values["flags"] = {k: v for k, v in values["flags"].items() if k != "withheld_by_admin"}
            reason = "released by an admin: restored from the source"
        elif same:  # re-keyed, the source row unchanged
            values = dict(restricted_now if withheld and not row["withheld"] else
                          held_now if held is not None and not row["withheld"] else {})
            reason = "key changed since an earlier import"
        else:
            values = self.message_values(p, withheld)
            if kept:
                values["flags"] = {**values["flags"], **kept}
                if not session_withheld:  # withheld by an admin or a rescan: the conversation itself stays as it was
                    values["sensitivity"] = row["sensitivity"]
            reason = ("key changed since an earlier import" if old_key else
                      "compacted since an earlier import" if values["state"] == "compacted" and row["state"] == "live"
                      else "changed since an earlier import")
        values["source_uid"] = m.uid[:200]
        if old_key:  # the JARVIS row as it is now (replace_messages renumbers it)
            values.update(source_key=m.key, source_message_id=str(m.id)[:200], position=m.position)
        ok, result = await self.guarded(lambda: self._update_row(row, values))
        if not ok:
            self.record(m.key, "message", "excluded", "invalid record: %s" % result)
            return 0
        if "state" in values:
            self._count_message(p, withheld and not released)
        before = jsonable({k: row[k] for k in MESSAGE_FIELDS})
        if old_key:  # a rollback puts the old key back
            before.update(source_key=old_key, source_uid=row.get("source_uid") or "")
            self.t.bump("keys_adopted")
        if values.get("withheld") and not row["withheld"]:  # withheld now (its session, or the same message)
            before = blank_before(before)
            self.withheld_now.append(row["id"])
        self.record(m.key, "message", "updated", reason, "archive_messages", row["id"], before)
        return 1

    async def _backfill_uids(self) -> None:
        """The JARVIS uid of rows this import left as they were (identity metadata, not a change: no record)."""
        pending, self._uid_backfill = self._uid_backfill, []
        if not pending or not self.write:
            return
        for i in range(0, len(pending), 1000):
            chunk = pending[i:i + 1000]
            params: dict[str, Any] = {}
            values = []
            for n, (row_id, uid) in enumerate(chunk):
                params["i%d" % n], params["u%d" % n] = row_id, uid
                values.append("(CAST(:i%d AS bigint), CAST(:u%d AS text))" % (n, n))
            await self.s.execute(sql("UPDATE archive_messages AS a SET source_uid = v.u FROM (VALUES %s) AS v(id, u) "
                                     "WHERE a.id = v.id" % ", ".join(values)), params)

    async def _gone_promoted_rows(self, sess_id: int) -> list[Any]:
        """The rows of this conversation that items were promoted from and that are gone (deleted by an earlier
        import): their copies are emptied once the conversation is withheld (finding 2)."""
        promoted_from = MemoryItem.flags["message_id"].as_integer()
        found = (await self.s.execute(select(promoted_from, MemoryItem.flags["message_source_key"].astext).where(
            MemoryItem.origin == "promoted", MemoryItem.flags["archive_session_id"].as_integer() == sess_id,
            ~select(ArchiveMessage.id).where(ArchiveMessage.id == promoted_from).exists()))).all()
        return [SimpleNamespace(id=int(gone_id), source_key=key or "", session_id=sess_id, position=-1)
                for gone_id, key in found if gone_id is not None]

    async def _update_row(self, row: dict[str, Any], values: dict[str, Any]) -> None:
        if self.write:
            await self.s.execute(update(ArchiveMessage).where(ArchiveMessage.id == row["id"]).values(**values))

    async def do_orphan(self, m: src.SourceMessage, index: int) -> None:
        self.location = safe_location(m.key, "orphan", index)
        self.record(m.key, "orphan", "excluded", "purged by an admin" if m.key in self.tombstones else "no session")

    # Phases -------------------------------------------------------------------------------------------------

    async def run_phases(self, work: Work, start_phase: str | None, start_index: int,
                         checkpoint: Callable[[str, int], Awaitable[None]]) -> None:
        # The checkpoint names the phase to continue with; "finish" comes after every phase (only the report is left).
        order = (*PHASES, "finish")
        for phase in PHASES[order.index(start_phase) if start_phase in order else 0:]:
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
            await self.withhold_batch()
            await self.flush()
            await checkpoint("sessions", i)

    async def withhold_batch(self) -> None:
        """What the batch's sessions stored without their text: the items promoted from those messages are emptied
        and the answers quoting them, or an older row of them, lose those lines (archive.withhold_copies); the quotes
        of the rows it deleted go too (quotes.scrub_quotes). Once per batch, right before its commit: the quote lock
        that removing quotes takes is held only from here (recalls are queued again meanwhile), and the rows the batch
        inserted are never looked for in quotes (nothing can have read them yet)."""
        withheld, inserted, deleted, gone = self.withheld_now, self.inserted_now, self.deleted_now, self.withheld_gone
        self.withheld_now, self.inserted_now, self.deleted_now, self.withheld_gone = [], set(), [], []
        if not self.write:
            return
        if withheld or gone:
            await withhold_copies(self.s, messages=gone, message_ids=withheld, fresh_ids=inserted, actor=self.actor,
                                  cause="import")
        await scrub_quotes(self.s, message_ids=deleted)

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
        await purge_key_check(s)
        tz_name, tz = await customer_timezone(s)
        sources = Sources(paths, tz, tz_name)
        try:
            work = select_work(sources, selection)
            tally = Tally()
            # What the identity repair a run makes first would change (a plan writes nothing, so it decides on the
            # database as it is).
            repair_pending = await identity.pending(s)
            proc = Processor(s, sources, run_id=None, tally=tally, write=False)
            await proc.load(work)

            async def no_checkpoint(phase: str, index: int) -> None:
                return None
            await proc.run_phases(work, None, 0, no_checkpoint)
            report = tally.report(selection, work.totals, sources.discovery, await missing_from_source(s, sources))
            report["repair_pending"] = repair_pending
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
        await purge_key_check(s)
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
            return await _execute(run_id, sources, selection, {}, actor, report_file, sources.discovery,
                                  resumed=False)
        finally:
            await locks.close()
    finally:
        sources.close()


async def resume(run_id: int, paths: SourcePaths, *, report_file: str | None = None, actor: str = DEFAULT_ACTOR,
                 heartbeat_seconds: float = HEARTBEAT_SECONDS) -> dict[str, Any]:
    """Continue an interrupted or failed run after its checkpoint, from the very same sources."""
    async with sessionmaker()() as s:
        await require_schema(s)
        await purge_key_check(s)
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
                discovery = sources.discovery if sources.discovery.get("checked") else (
                    (run_row.source or {}).get("discovery_check") or sources.discovery)
                try:
                    await set_status(s, run_id, run_row.status, status="running", error="", finished_at=None,
                                     heartbeat_at=now())
                    await s.commit()
                except (RunLost, IntegrityError):
                    raise Refused("run %d could not be resumed: another import is running" % run_id) from None
            locks.start_heartbeat(run_id, heartbeat_seconds)
            return await _execute(run_id, sources, selection, checkpoint, actor, report_file, discovery, resumed=True)
        finally:
            await locks.close()
    finally:
        sources.close()


async def _execute(run_id: int, sources: Sources, selection: Selection, checkpoint: dict[str, Any], actor: str,
                   report_file: str | None, discovery: dict[str, Any], *, resumed: bool) -> dict[str, Any]:
    tally = Tally(checkpoint.get("tally"))
    work = select_work(sources, selection)
    async with sessionmaker()() as s:
        proc = Processor(s, sources, run_id=run_id, tally=tally, write=True, actor=actor)
        try:
            await audit.record(s, actor, "brain.import_started", "import_run", run_id,
                               {"run": run_id, "resumed": resumed, "selection": selection.mode,
                                "totals": work.totals})
            await s.commit()
            # Under the run's lock, before anything is imported: every readable row that is the same message as a
            # withheld one is withheld first (idempotent; a resume repairs again).
            tally.d["repair"] = await identity.repair(s, actor=actor, cause="import", refresh_quotes=False)
            await s.commit()
            await proc.load(work)

            async def save(phase: str, index: int) -> None:
                await set_status(s, run_id, "running", checkpoint={"phase": phase, "index": index,
                                                                    "tally": tally.d})
                await s.commit()
                s.expunge_all()
            await proc.run_phases(work, checkpoint.get("phase"), int(checkpoint.get("index") or 0), save)
            report = tally.report(selection, work.totals, discovery, await missing_from_source(s, sources))
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
              "run_id", "item_id", "existing_id", "incoming_item_id", "schedule_id", "existing_table", "source_system",
              "source_uid"}
# Records keyed by the backup's own row identifiers (read like the ID columns), and the digest that ends a memory
# entry's key: neither is text from the sources.
IDENTIFIER_RECORDS = ("session", "message", "orphan")
ENTRY_DIGEST = re.compile(r"#entry-[0-9a-f]{16}(?:-[0-9]+)?$")


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


def unmasked_leak_types(value: str) -> list[str]:
    """leak_types() for text stored unmasked (a source key, a reason): an e-mail address or a phone number counts."""
    cleaned = mapping.clean(value, "chat")
    return sorted(set(cleaned.secret_types) | set(cleaned.restricted) | set(cleaned.masked))


def _without_ids(before: Any) -> Any:
    """A rollback copy (import_records.before) without its identifier fields, at its top level and in its deleted_row,
    as _row_leaks skips them in a row: keys and fingerprints are not text from the sources, and a sha256 can hold a
    run of digits the card detector accepts (key adoption, 0.6.2, writes a copy of every row it re-keys)."""
    if not isinstance(before, dict):
        return before
    return {k: _without_ids(v) if k == "deleted_row" else v for k, v in before.items() if k not in ID_COLUMNS}


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
    """Recompute the selection from the sources and check the run against it (spec 2.1, twelve checks)."""
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
            checks.update(await _identity_checks(s, run_id, sources, work))
            passed = all(c["ok"] for c in checks.values())
            verification = {"passed": passed, "checked_at": now().isoformat(), "checks": checks}
            report["verification"] = verification
            # Only report.verification is written, and only while the run is still completed: a rollback that
            # happened while verify was scanning keeps its own section (and a rolled-back run gets no verification).
            written = (await s.execute(sql(
                "UPDATE import_runs SET report = jsonb_set(COALESCE(report, '{}'::jsonb), '{verification}', "
                "CAST(:v AS jsonb)) WHERE id = :r AND status = 'completed'"),
                {"v": json.dumps(verification), "r": run_id})).rowcount
            await s.commit()
            if not written:
                raise Refused("run %d was rolled back while it was being verified: the verification was not written"
                              % run_id)
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
    for row in await rows(select(ImportRecord.id, ImportRecord.before).where(ImportRecord.run_id == run_id,
                                                                              ImportRecord.before != {})):
        leaks += _row_leaks("import_records", {**row, "before": _without_ids(row["before"])})
    # Keys and reasons are shown unmasked wherever records are listed: they hold no contact data either.
    for name in ("source_key", "reason"):
        column = getattr(ImportRecord, name)
        query = select(func.min(ImportRecord.id), column).where(ImportRecord.run_id == run_id, column != "")
        if name == "source_key":
            query = query.where(ImportRecord.record_type.not_in(IDENTIFIER_RECORDS))
        for record_id, value in (await s.execute(query.group_by(column))).all():
            found = unmasked_leak_types(ENTRY_DIGEST.sub("", value) if name == "source_key" else value)
            if found:
                leaks.append({"table": "import_records", "id": record_id, "column": name, "types": found})
    leaks += _row_leaks("import_runs", {"id": run_id, "report": report})
    return leaks


async def _identity_checks(s: AsyncSession, run_id: int, sources: Sources, work: Work) -> dict[str, dict[str, Any]]:
    """Checks 9-12 (spec 2.9): nothing that is the same message as a withheld one is readable, no withheld row or
    rollback copy keeps a fingerprint of its text, purged items stay purged, and the database guard never had to
    remove text the import wrote."""
    checks: dict[str, dict[str, Any]] = {}
    pending = dict((await s.execute(sql("SELECT brain_withhold_pending()"))).scalar() or {})
    readable = {k: int(pending.get(k) or 0) for k in
                ("readable_by_key", "readable_by_jarvis_row", "readable_in_restricted_sessions")}
    # The rules again, from the backup, with the stones as they are now (read-only): every row that is the same
    # message as a withheld one (or held for review) is stored without text.
    proc = Processor(s, sources, run_id=None, tally=Tally(), write=False)
    proc.ids = await identity.Identities.load(s)
    await proc._rebind()
    rule_rows: list[int] = []
    for ss in work.sessions:
        sess_id = await s.scalar(select(ArchiveSession.id).where(ArchiveSession.source_key == ss.key))
        if sess_id is None:
            continue
        msgs = sources.backup.messages(ss.id)
        keys = [m.key for m in msgs]
        rows: dict[str, dict[str, Any]] = {}
        for i in range(0, len(keys), 1000):
            for r in (await s.execute(select(*MESSAGE_COLUMNS).where(
                    ArchiveMessage.source_key.in_(keys[i:i + 1000])))).mappings().all():
                rows[r["source_key"]] = dict(r)
        plans, _ = proc.plan_messages(msgs, False, frozenset(keys))  # actions only: nothing is cleaned
        held = proc.held_rows(ss, msgs, plans, rows)
        if not held:
            continue
        by_jarvis_row: dict[tuple[str, str], list[Any]] = {}
        for r in (await s.execute(select(ArchiveMessage.id, ArchiveMessage.source_message_id, ArchiveMessage.role,
                                         ArchiveMessage.withheld).where(ArchiveMessage.session_id == sess_id))).all():
            by_jarvis_row.setdefault((r.source_message_id, r.role), []).append(r)
        by_key = {m.key: m for m in msgs}
        for key, mt in held.items():
            m, row = by_key[key], rows.get(key)
            if row is not None and row["session_id"] == sess_id and row["source_message_id"] == str(m.id)[:200]:
                stored = [row]
            else:  # a re-keyed row, or none stored (excluded, a duplicate)
                stored = by_jarvis_row.get((str(m.id)[:200], brain_text.sanitize(m.role)[:40]), [])
            rule_rows += [int(r["id"] if isinstance(r, dict) else r.id) for r in stored
                          if not (r["withheld"] if isinstance(r, dict) else r.withheld)]
    unstoned = int(await s.scalar(sql(
        "SELECT count(*) FROM import_records d JOIN archive_messages t ON t.id = d.target_id "
        "JOIN archive_sessions sx ON sx.id = t.session_id "
        "JOIN brain_tombstones st ON st.source_key = t.source_key AND st.reason = :wh "
        "WHERE d.run_id = :r AND d.record_type = 'message' AND d.action = 'duplicate' "
        "AND d.target_table = 'archive_messages' AND d.reason IN (:c1, :c2) AND sx.sensitivity <> 'restricted' "
        "AND NOT EXISTS (SELECT 1 FROM brain_tombstones x WHERE x.source_key = d.source_key)"),
        {"r": run_id, "wh": WITHHELD_BY_ADMIN, "c1": identity.CARRIED, "c2": identity.SAME_AS_PREVIOUS}) or 0)
    checks["withheld_identity_holds"] = {
        "ok": not any(readable.values()) and not rule_rows and not unstoned, **readable,
        "readable_rows_the_rules_match": len(rule_rows), "row_ids": sorted(set(rule_rows))[:50],
        "duplicates_of_withheld_rows_without_a_stone": unstoned}
    rows_fp = int(await s.scalar(sql(
        "SELECT count(*) FROM archive_messages WHERE withheld AND source_fp <> '' AND source_fp NOT LIKE 'w1:%'"))
        or 0)
    copies_fp = int(await s.scalar(sql(
        "SELECT count(*) FROM import_records x WHERE x.record_type = 'message' AND x.before <> '{}'::jsonb "
        "AND coalesce(x.before -> 'deleted_row' ->> 'withheld', x.before ->> 'withheld') = 'true' "
        "AND coalesce(x.before -> 'deleted_row' ->> 'source_fp', x.before ->> 'source_fp', '') <> '' "
        "AND coalesce(x.before -> 'deleted_row' ->> 'source_fp', x.before ->> 'source_fp') NOT LIKE 'w1:%'")) or 0)
    checks["withheld_hold_no_fingerprint"] = {"ok": not rows_fp and not copies_fp, "rows": rows_fp,
                                              "rollback_copies": copies_fp}
    finished = await s.scalar(select(ImportRun.finished_at).where(ImportRun.id == run_id)) or now()
    purge = await identity.PurgeIndex.load(s, check=False)
    tombs = {k: at for k, at in (await s.execute(select(BrainTombstone.source_key, BrainTombstone.purged_at).where(
        BrainTombstone.reason == identity.PURGED_BY_ADMIN, BrainTombstone.purged_at <= finished))).all()}
    digests = {found.group(1) for k in tombs if (found := identity.ENTRY.search(k))}
    fps = set((await s.scalars(select(BrainPurgedContent.fp).where(BrainPurgedContent.purged_at <= finished,
                                                                   identity.live_fingerprints()))).all())
    back = []
    for item_id, key, sha in (await s.execute(select(MemoryItem.id, MemoryItem.source_key, MemoryItem.source_sha256)
                                              .where(MemoryItem.import_run_id == run_id))).all():
        found = identity.ENTRY.search(key or "")
        if (key in tombs or (found and found.group(1) in digests)
                or (purge.fingerprint(sha or "") or "-") in fps):
            back.append(item_id)
    checks["purged_stay_purged"] = {"ok": not back, "items": len(back), "item_ids": back[:50]}
    guarded = int(await s.scalar(sql(
        "SELECT count(*) FROM archive_messages m WHERE m.flags -> 'withheld_by_admin' ? 'guard' AND "
        "(m.import_run_id = :r OR m.id IN (SELECT x.target_id FROM import_records x WHERE x.run_id = :r AND "
        "x.target_table = 'archive_messages' AND x.action = 'updated'))"), {"r": run_id}) or 0)
    checks["database_guard_quiet"] = {"ok": not guarded, "rows": guarded}
    return checks


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
    rolled back depends on this one (or recorded a row or source record this run updated), when any of the run's
    items was reviewed or edited (unless discard_reviewed), and when a conflict resolution's target was changed again
    (or its earlier version redacted) since. Also called by the API.

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
    # A later run that recorded a row or a source record this run updated: putting `before` back would undo that
    # run's view of it too (the archive would be in neither run's state).
    dependent |= set((await session.scalars(sql(
        "SELECT x.run_id FROM import_records u JOIN import_records x ON x.target_table = u.target_table "
        "AND x.target_id = u.target_id WHERE u.run_id = :r AND u.action = 'updated' AND x.run_id IN (%s) "
        "UNION SELECT x.run_id FROM import_records u JOIN import_records x ON x.source_key = u.source_key "
        "WHERE u.run_id = :r AND u.action = 'updated' AND x.run_id IN (%s)" % (later.text, later.text)),
        params)).all())
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

    reverted, kept_copies = await _revert_effects(session, run_id, conflicts, actor)
    restored = {"archive_messages": 0, "archive_sessions": 0}
    touched: set[int] = set()
    created_sessions = set((await session.scalars(select(ArchiveSession.id).where(
        ArchiveSession.import_run_id == run_id))).all())
    updates = (await session.execute(select(ImportRecord.target_table, ImportRecord.target_id, ImportRecord.before)
                                     .where(ImportRecord.run_id == run_id, ImportRecord.action == "updated")
                                     .order_by(ImportRecord.id.desc()))).all()
    # A withheld row stays withheld (whoever withheld it, and when): its text is never restored. A row that is the
    # same message as one an admin withheld (its key has a stone, its JARVIS row is a stone's binding) comes back
    # without its text too, keeping its sensitivity; one that only shares a withheld message's uid comes back held for
    # review (a rollback cannot compare the text; the next import does). Every withheld row put back keeps no
    # fingerprint of its text. Stones and exemptions are never deleted: an inherited withhold is as permanent as the
    # admin's own.
    ids = await identity.Identities.load(session)
    jarvis_sessions: dict[int, str] = {}

    async def same_message(values: dict[str, Any]) -> identity.Match | None:
        sid = int(values["session_id"])
        if sid not in jarvis_sessions:
            jarvis_sessions[sid] = await session.scalar(select(ArchiveSession.source_session_id).where(
                ArchiveSession.id == sid)) or ""
        mt = ids.match_row(values.get("source_key") or "", jarvis_sessions[sid],
                           str(values.get("source_message_id") or ""), values.get("role") or "",
                           values.get("source_uid") or "")
        if mt is not None and mt.via == "same message id" and values.get("source_key") not in ids.by_key:
            hold = identity.Stone(
                key=values["source_key"], anchor=mt.group, session=jarvis_sessions[sid],
                row=str(values.get("source_message_id") or ""), role=values.get("role") or "",
                tool_call_id=values.get("tool_call_id") or "", uid=values.get("source_uid") or "",
                via=mt.via, review=mt.review, bound=True, after_run=mt.stone.after_run)
            await identity.put_stone(session, hold, actor=actor)
            ids.add(hold)
        return mt

    def no_fingerprint(values: dict[str, Any], state: str | None) -> None:
        released = ((values.get("flags") or {}).get("withheld_by_admin") or {}).get("released_at")
        values["source_fp"] = "" if released else "w1:%s" % (values.get("state") or state or "live")

    put_back_withheld: list[int] = []
    for table, target, before in updates:
        if table == "archive_messages" and before:
            if "deleted_row" in before:
                row = unjson(before["deleted_row"])
                if withheld_later(await session.scalar(select(ArchiveSession.flags).where(
                        ArchiveSession.id == int(row["session_id"])))):
                    row.update(WITHHELD_ROW)
                elif not row.get("withheld") and (mt := await same_message(row)) is not None:
                    row.update(content="", tool_calls=[], labels=[], withheld=True, truncated=False,
                               flags={**(row.get("flags") or {}), "withheld_by_admin": Processor._held_flag(mt)})
                if row.get("withheld"):
                    no_fingerprint(row, None)
                await session.execute(insert(ArchiveMessage).values(**row))
                touched.add(int(row["session_id"]))
                if row.get("withheld"):
                    put_back_withheld.append(int(row["id"]))
            else:
                values = unjson(before)
                current = (await session.execute(select(
                    ArchiveMessage.session_id, ArchiveMessage.flags, ArchiveMessage.withheld,
                    ArchiveMessage.sensitivity, ArchiveMessage.source_key, ArchiveMessage.source_uid,
                    ArchiveMessage.source_message_id, ArchiveMessage.role, ArchiveMessage.tool_call_id,
                    ArchiveMessage.state).where(ArchiveMessage.id == target))).first()
                mt = None
                if current is not None and not current.withheld:
                    mt = await same_message({**current._asdict(), **values})
                if current is not None and current.withheld:
                    values.update(WITHHELD_ROW, flags=current.flags, sensitivity=current.sensitivity)
                    no_fingerprint(values, current.state)
                elif current is not None and mt is not None:
                    values.update(WITHHELD_ROW, flags={**(current.flags or {}),
                                                       "withheld_by_admin": Processor._held_flag(mt)},
                                  sensitivity=current.sensitivity)
                    no_fingerprint(values, current.state)
                    put_back_withheld.append(int(target))
                await session.execute(update(ArchiveMessage).where(ArchiveMessage.id == target).values(**values))
                if current is not None:
                    touched.add(current.session_id)
            restored["archive_messages"] += 1
        elif table == "archive_sessions" and before and target:
            values = unjson(before)
            flags = await session.scalar(select(ArchiveSession.flags).where(ArchiveSession.id == target))
            if withheld_later(flags):
                values.update(title="", flags={**(values.get("flags") or {}),
                                               **{k: v for k, v in (flags or {}).items() if k.startswith("withheld_by")}})
            await session.execute(update(ArchiveSession).where(ArchiveSession.id == target).values(**values))
            touched.add(target)
            restored["archive_sessions"] += 1
    touched |= set((await session.scalars(select(ArchiveMessage.session_id).where(
        ArchiveMessage.import_run_id == run_id).distinct())).all())
    touched -= created_sessions
    quoted_messages = set((await session.scalars(select(ArchiveMessage.id).where(
        ArchiveMessage.import_run_id == run_id))).all()) | {t for table, t, _ in updates if table == "archive_messages" and t}
    quoted_items = set((await session.scalars(select(MemoryItem.id).where(MemoryItem.import_run_id == run_id))).all())
    quoted_items |= {int(e["id"]) for c in conflicts for e in (c.effects or {}).get("items", []) if "id" in e}
    await scrub_quotes(session, message_ids=quoted_messages, item_ids=quoted_items)
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
    # The rows put back without their text: their promoted items, rollback copies and quotes lose it as well.
    await withhold_copies(session, message_ids=put_back_withheld, actor=actor, cause="rollback")
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
              "promoted_items_marked": promoted, "kept_withheld_copies": kept_copies,
              "withheld_reinserted": len(put_back_withheld), "rolled_back_at": now().isoformat()}
    report["rollback"] = {k: v for k, v in result.items() if k not in ("run_id", "status")}
    await set_status(session, run_id, expected_status, status="rolled_back", rolled_back_at=now(),
                     rolled_back_by=actor[:200], report=report)
    await audit.record(session, actor, "brain.import_rolled_back", "import_run", run_id,
                       {"run": run_id, "deleted": deleted, "restored": restored, "reverted": reverted,
                        "kept_converted_drafts": kept, "discarded_reviewed_items": result["discarded_reviewed_items"],
                        "kept_withheld_copies": kept_copies, "withheld_reinserted": len(put_back_withheld)})
    await session.flush()
    return result


async def _blocked_effects(session: AsyncSession, run_id: int, conflicts: list[MemoryConflict]) -> list[int]:
    """Conflicts whose effects can no longer be reverted: a target was changed again (or removed) since, or the
    version a revert would bring back was redacted (it would become the item's live text)."""
    from .review import is_redacted

    blocked = []
    for c in conflicts:
        effects = c.effects or {}
        ok = True
        for e in effects.get("items") or []:
            if e.get("rejected_source_sha256"):
                continue  # taking the hash out again is right whatever happened to the item since
            item = await session.get(MemoryItem, int(e["id"]))
            if item is None:
                ok = False
            elif (item.flags or {}).get("source_withheld"):
                continue  # emptied since (its source message was withheld): the revert leaves it as it is
            elif item.import_run_id != run_id:
                snap = await session.scalar(select(MemoryItemVersion).where(
                    MemoryItemVersion.item_id == item.id, MemoryItemVersion.version == int(e["version_before"])))
                if item.version != int(e["version_after"]) or (snap is not None and is_redacted(snap)):
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


REDACTED_MARKER = re.compile(r"\[REDACTED:([A-Za-z0-9_]+)\]")


def text_risk_flags(item: MemoryItem, flags: dict[str, Any]) -> dict[str, Any]:
    """flags, plus the risk flags the item's text calls for (as the import sets them): instruction-like rules, an
    approval conflict, redacted secrets. A text a rollback brings back never comes back without them."""
    text = item.title + "\n" + item.body
    instr = brain_text.instruction_flags(text)
    if instr:
        known = flags.get("instruction_like")
        flags["instruction_like"] = sorted(set(known if isinstance(known, list) else []) | set(instr))
        if item.kind == "instruction" and {"approval_bypass", "autonomous_action"} & set(instr):
            flags["approval_conflict"] = True
    redacted = REDACTED_MARKER.findall(text)
    if redacted and not flags.get("secrets_redacted"):
        flags["secrets_redacted"] = dict(Counter(redacted))
    return flags


async def _revert_effects(session: AsyncSession, run_id: int, conflicts: list[MemoryConflict],
                          actor: str) -> tuple[dict[str, int], list[int]]:
    """(what was reverted, the ids of items left as they are because their source message was withheld since: an
    emptied item never gets its text or its status back)."""
    counts = {"items": 0, "sites": 0, "drafts": 0, "purged_items_reported": 0}
    kept: list[int] = []
    for c in conflicts:
        effects = c.effects or {}
        for e in effects.get("items") or []:
            item = await session.get(MemoryItem, int(e["id"]))
            if item is None or item.import_run_id == run_id:
                continue
            if (item.flags or {}).get("source_withheld"):
                if item.id not in kept:
                    kept.append(item.id)
                continue
            if e.get("rejected_source_sha256"):
                flags = dict(item.flags or {})
                rejected = [h for h in flags.get("rejected_source_sha256") or [] if h != e["rejected_source_sha256"]]
                if rejected:
                    flags["rejected_source_sha256"] = rejected
                else:
                    flags.pop("rejected_source_sha256", None)
                item.flags = flags
                counts["items"] += 1
                continue
            snap = await session.scalar(select(MemoryItemVersion).where(
                MemoryItemVersion.item_id == item.id, MemoryItemVersion.version == int(e["version_before"])))
            changed = snap is not None and (snap.title, snap.body, list(snap.tags or []), snap.kind, snap.agent) != (
                item.title, item.body, list(item.tags or []), item.kind, item.agent)
            if changed:
                assert snap is not None
                item.title, item.body, item.tags = snap.title, snap.body, list(snap.tags or [])
                item.kind, item.agent = snap.kind, snap.agent
                item.content_hash = brain_text.content_hash(item.kind, item.agent, item.title, item.body)
                if c.kind == "source_changed" and (c.existing or {}).get("source_sha256"):
                    item.source_sha256 = c.existing["source_sha256"]
                    at = c.existing.get("source_at")
                    item.source_at = datetime.fromisoformat(at) if at else None
            # The flags go back with the text, and a restored text never lacks the risk flags it calls for.
            flags_before = e.get("flags_before")
            if flags_before is not None:
                item.flags = dict(flags_before)
            if changed:
                item.flags = text_risk_flags(item, dict(item.flags or {}))
            item.status = str(e["status_before"])
            # Approved with a risk flag only when it was approved with that very flag (an effect recorded without
            # flags_before cannot tell).
            if changed and item.status == "approved" and any(
                    item.flags.get(f) and not (flags_before or {}).get(f) for f in RISK_FLAGS):
                item.status = "quarantined"
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
    return counts, kept


async def _mark_promoted(session: AsyncSession, run_id: int) -> int:
    """Items promoted from messages this rollback deletes keep their text; their source reads 'source removed'."""
    gone = (await session.execute(
        select(ArchiveMessage.id, ArchiveMessage.position, ArchiveSession.source_session_id)
        .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
        .where(ArchiveMessage.import_run_id == run_id))).all()
    if not gone:
        return 0
    ids = {g.id for g in gone}
    refs = {promoted_ref(g.source_session_id, g.position) for g in gone}
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
        withhold = bool(await s.scalar(sql(
            "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE version = '0007_brain_withhold')")))
        identity_migration = bool(await s.scalar(sql(
            "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE version = '0008_brain_withhold_identity')")))
        out: dict[str, Any] = {"importer_version": IMPORTER_VERSION, "installed": installed,
                               "migration_0006_brain": migration, "migration_0007_brain_withhold": withhold,
                               "migration_0008_brain_withhold_identity": identity_migration, "runs": []}
        if not installed:
            return out
        # Promoted items whose message is withheld but which still hold text, and rollback copies of withheld messages
        # that still hold it (0.6.0 left them under an older row id): the start-up check empties both.
        out["withheld_copies_with_text"] = len(await withheld_copies(s))
        out["withheld_rollback_copies_with_text"] = await withheld_rollback_copies(s)
        # The withhold identity (0.6.2): whether the repair ran, and what is left for it or for an admin. Counts only.
        pending = await identity.pending(s)
        if pending.get("installed"):
            out.update(
                identity_repair_done=bool(pending.get("repair_done")),
                readable_rows_matching_a_withhold=int(pending["readable_rows_matching_a_withhold"]),
                held_for_review=int(pending.get("held_for_review") or 0),
                withheld_rows_with_a_text_fingerprint=int(pending.get("withheld_with_text_fingerprint") or 0)
                + int(pending["withheld_copies_with_text_fingerprint"]),
                rows_blanked_by_the_database_guard=int(pending["rows_blanked_by_the_database_guard"]),
                quotes_of_removed_sources=int(pending["quotes_of_removed_sources"]),
                purge_fingerprints_made_with_another_key=int(pending["purge_fingerprints_other_key"]))
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


# --- repair ------------------------------------------------------------------------------------------------------


async def repair(*, apply: bool, actor: str = DEFAULT_ACTOR) -> dict[str, Any]:
    """The identity repair (identity.repair), by hand: without apply, only what is pending (read-only). Refused while an
    import, a rollback or a rescan runs."""
    async with sessionmaker()() as s:
        await require_schema(s)
        if not apply:
            return {"applied": False, "pending": await identity.pending(s)}
        if not await try_xact_lock(s, GLOBAL_LOCK):
            raise Refused("an import, rollback or rescan is running: try again when it has finished")
        counts = await identity.repair(s, actor=actor, cause="cli")
        await s.commit()
    return {"applied": True, "repair": counts}


async def forget_fingerprints(*, apply: bool, actor: str = DEFAULT_ACTOR) -> dict[str, Any]:
    """Purge fingerprints made with another database password, deleted (with apply; audited). The purges stay, by
    their keys. Refused while an import, a rollback or a rescan runs."""
    async with sessionmaker()() as s:
        await require_schema(s)
        if apply and not await try_xact_lock(s, GLOBAL_LOCK):
            raise Refused("an import, rollback or rescan is running: try again when it has finished")
        n = await identity.forget_other_key(s, actor=actor, apply=apply)
        if apply:
            await s.commit()
        else:
            await s.rollback()
    return {"applied": apply, "fingerprints_made_with_another_key": n}


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
              "sessions_changed": 0, "sessions_withheld": 0, "copies_withheld": 0, "copy_versions_blanked": 0,
              "rollback_copies_blanked": 0, "conflicts_changed": 0, "drafts_changed": 0, "records_changed": 0}
    locks = await LockConnection.open()
    try:
        if not await locks.try_lock(GLOBAL_LOCK):
            raise Refused("an import, rollback or rescan is running")
        async with sessionmaker()() as s:
            await require_schema(s)
            # First the identity repair of the rows: every readable row that is the same message as a withheld one is
            # withheld (without --yes it runs too, and is rolled back with the rest: the counts say what --yes would
            # do). Its copies and quotes go in the last pass, with the rescan's own.
            repaired = await identity.repair_rows(s, cause="rescan")
            counts["identity_rows_withheld"] = sum(int(repaired.get(k) or 0) for k in (
                "withheld_by_key", "withheld_by_jarvis_row", "held_by_message_id", "held_by_same_text",
                "restricted_rows_withheld"))
            await _rescan_items(s, counts, apply, actor)
            withhold, touched = await _rescan_messages(s, counts, apply)
            await _rescan_sessions(s, counts, apply, withhold, touched)
            await _rescan_json(s, counts, apply)
            # Last: removing quotes takes the quote lock until the commit (recalls are queued again meanwhile).
            await _rescan_copies(s, counts, withhold, actor)
            if apply:
                counts["answers_changed"] = await refresh_quotes(s)
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
    by_rescan = {"withheld_by_rescan": {"at": datetime.now(UTC).isoformat(),
                                        "note": "restricted data found by a rescan; the text was removed"}}
    for sid in sorted(withhold):
        counts["sessions_withheld"] += 1
        if apply:
            await s.execute(update(ArchiveMessage).where(ArchiveMessage.session_id == sid).values(
                content="", tool_calls=[], labels=[], sensitivity="restricted", withheld=True,
                flags=ArchiveMessage.flags.op("||")(by_rescan)))
            await s.execute(update(ArchiveSession).where(ArchiveSession.id == sid).values(
                title="", summary="", flags=ArchiveSession.flags.op("||")(by_rescan)))
            await scrub_before(s, archive_session_id=sid)  # a rollback must not bring the text back
    counts["sessions_changed"] = len((changed | touched) - withhold)
    if apply:
        s.expire_all()
        for sid in sorted(changed | touched | withhold):
            await recompute_session(s, sid)


async def _rescan_copies(s: AsyncSession, counts: dict[str, int], withhold: set[int], actor: str) -> None:
    """The items promoted from the messages of the sessions this rescan withholds are emptied, and so is every
    promoted item whose message is withheld but which still holds text (archive.sweep_withheld_copies), and every
    rollback copy of a withheld message that still holds it (archive.sweep_withheld_records). Without --yes this runs
    too and is rolled back with the rest: the counts say what --yes would do."""
    ids = list((await s.scalars(select(ArchiveMessage.id).where(ArchiveMessage.session_id.in_(withhold))))
               .all()) if withhold else []
    # The items promoted from rows of those sessions that an earlier import deleted go too (finding 2).
    gone: list[Any] = []
    promoted_from = MemoryItem.flags["message_id"].as_integer()
    for sid in sorted(withhold):
        gone += [SimpleNamespace(id=int(i), source_key=k or "", session_id=sid, position=-1) for i, k in (
            await s.execute(select(promoted_from, MemoryItem.flags["message_source_key"].astext).where(
                MemoryItem.origin == "promoted", MemoryItem.flags["archive_session_id"].as_integer() == sid,
                ~select(ArchiveMessage.id).where(ArchiveMessage.id == promoted_from).exists()))).all()
            if i is not None]
    done = await withhold_copies(s, messages=gone, message_ids=ids, actor=actor, cause="rescan")
    settled = await identity.settle(s, actor=actor, cause="rescan")  # the rows the identity repair withheld
    swept = await sweep_withheld_copies(s, actor=actor, cause="rescan")
    counts["copies_withheld"] += len(done.copies) + len(swept) + settled["copies_emptied"]
    counts["copy_versions_blanked"] += (done.versions_blanked + sum(c["versions_blanked"] for c in swept)
                                        + settled["copy_versions_blanked"])
    counts["rollback_copies_blanked"] = (done.rollback_copies_blanked + await sweep_withheld_records(s)
                                         + settled["rollback_copies_blanked"])
    counts["purged_items_quarantined"] = settled["purged_items_quarantined"]


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
PURGED_BY_ADMIN = "purged by an admin"  # review.purge()'s tombstone reason
DECISIONS_VERSION = 4  # 4 (0.6.2): withholds carry their JARVIS row, and releases travel; 3 (0.6.0, 0.6.1) still read
RESTORE_ACTOR = "system (restore)"  # decisions-apply --removals-only: restore.ps1 applies the removals again
RESTORE_NOTE = "decision applied again after a restore"
# What a withhold carries in a version 4 file: its key and binding (spec 2.1), never text or a fingerprint.
WITHHOLD_FIELDS = ("source_key", "reason", "anchor", "session", "row", "role", "tool_call_id", "uid", "uid_known",
                   "via", "review")


async def decisions_export(out_path: str, *, actor: str = DEFAULT_ACTOR, removals_only: bool = False
                           ) -> dict[str, Any]:
    """Every imported item a person reviewed or edited in this database, keyed by source_key and by the content hash
    of the imported version (so production applies a decision only to the very same import).

    The file holds decisions, not memory: an item's text is written only when a person edited it (production needs
    the edit), and never for a customer or Credit Repair item. Such an edit is marked edit_not_exported, and
    decisions-apply lists it to be redone by hand. It also lists the removals (keys and JARVIS ids only, never text
    and never a fingerprint): every withhold that is certain (an admin's own, the rows that are the same message, a
    hold an admin kept), each with the JARVIS row it names (version 4), and the items an admin purged; and the
    releases of held rows. Holds still waiting for review are not exported: an import there finds them again by the
    same rules. With removals_only, only the removals and releases (restore.ps1, before a restore)."""
    if os.path.exists(out_path):
        raise Refused("the output file already exists; nothing is overwritten")
    decisions, skipped, not_exported = [], 0, 0
    async with sessionmaker()() as s:
        await require_schema(s)
        items = [] if removals_only else (await s.scalars(select(MemoryItem).where(
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
            edited = (item.title, item.body, list(item.tags or []), item.kind, item.agent) != (
                first.title, first.body, list(first.tags or []), first.kind, first.agent)
            decision: dict[str, Any] = {
                "source_key": item.source_key,
                "content_hash": brain_text.content_hash(first.kind, first.agent, first.title, first.body),
                "status": item.status, "reviewed_by": item.reviewed_by, "review_note": item.review_note,
                "deleted": item.deleted_at is not None, "edited": edited}
            if edited and item.deleted_at is None:
                if item.sensitivity != "general" or SENSITIVE_AGENT in (item.agent, first.agent):
                    decision["edit_not_exported"] = ("customer or Credit Repair text: redo this edit and its review "
                                                     "by hand")
                    not_exported += 1
                else:
                    decision.update(title=item.title, body=item.body, tags=list(item.tags or []), kind=item.kind,
                                    agent=item.agent)
            decisions.append(decision)
        removals: list[dict[str, Any]] = []
        for t in (await s.scalars(select(BrainTombstone).where(
                BrainTombstone.reason.in_((WITHHELD_BY_ADMIN, PURGED_BY_ADMIN)),
                BrainTombstone.review != identity.PENDING).order_by(BrainTombstone.source_key))).all():
            if t.reason == PURGED_BY_ADMIN:
                removals.append({"source_key": t.source_key, "reason": t.reason})
                continue
            removals.append({"source_key": t.source_key, "reason": t.reason, "anchor": t.anchor_key or "",
                             "session": t.source_session_id or "", "row": t.source_row_id or "", "role": t.role or "",
                             "tool_call_id": t.tool_call_id or "", "uid": t.message_uid or "",
                             "uid_known": bool(t.uid_known), "via": t.via or "", "review": t.review or ""})
        releases = [{"anchor": a, "session": sid, "row": row} for a, sid, row in (await s.execute(select(
            BrainWithholdExemption.anchor_key, BrainWithholdExemption.source_session_id,
            BrainWithholdExemption.source_row_id).order_by(BrainWithholdExemption.anchor_key,
                                                           BrainWithholdExemption.source_session_id,
                                                           BrainWithholdExemption.source_row_id))).all()]
    content: dict[str, Any] = {"format": "jarvis-brain-decisions", "version": DECISIONS_VERSION,
                               "exported_at": now().isoformat(), "importer_version": IMPORTER_VERSION,
                               "removals": removals, "releases": releases}
    if removals_only:
        content["removals_only"] = True
    else:
        content["decisions"] = decisions
    with open(out_path, "x", encoding="utf-8") as f:
        json.dump(content, f, indent=2, sort_keys=True)
    out: dict[str, Any] = {"removals": len(removals), "releases": len(releases)}
    if removals_only:
        out["removals_only"] = True
    else:
        out.update(exported=len(decisions), skipped_without_source_key=skipped, edits_not_exported=not_exported)
    async with sessionmaker()() as s:
        await audit.record(s, actor, "brain.decisions_exported", "brain", "", dict(out))  # no path: it can name a user
        await s.commit()
    return out


@dataclass(frozen=True)
class _Decider:
    """Who applies removals: an active admin (decisions-apply --actor), or restore.ps1 putting back decisions an
    admin already took (--removals-only without --actor: every one was allowed when it was taken)."""
    email: str
    can_access_sensitive: bool


async def decisions_apply(in_path: str, run_id: int | None, *, actor: str | None, apply: bool,
                          removals_only: bool = False) -> dict[str, Any]:
    """Apply exported decisions to the production import run where the item is the very same (same source_key and
    content hash, untouched since). Anything else is listed by decision number, not applied.

    The removals apply to the whole database, not only to the run: a message an admin withheld on the test copy is
    withheld here too (found by its key or by the JARVIS row it names, whatever key this database stored it under) and
    an item an admin purged there is purged here, and their tombstones keep later imports from bringing them back
    (also for what this database has not imported yet). Releases of held rows apply too.

    With removals_only (restore.ps1, after a restore: spec 9.3) only the removals and releases apply, no run is needed
    and the actor is "system (restore)" unless an admin is named."""
    from . import review  # imports the task service and the scheduler: not needed by the import itself
    try:
        with open(in_path, "rb") as f:
            data = json.load(f)
    except (OSError, ValueError):
        raise Refused("the decisions file could not be read as JSON") from None
    if not isinstance(data, dict) or data.get("format") != "jarvis-brain-decisions":
        raise Refused("this is not a decisions file exported by brain-import decisions-export")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version > DECISIONS_VERSION:
        raise Refused("this decisions file was written by a newer Command Center (version %s): apply it with that "
                      "version" % str(version)[:20])
    if not removals_only and run_id is None:
        raise Refused("--run is needed (the production import run the decisions apply to), unless --removals-only")
    listed: dict[str, list[int]] = {}
    applied = 0
    via = "restore" if removals_only else "decisions-apply"
    async with sessionmaker()() as s:
        await require_schema(s)
        if actor or not removals_only:
            user = await s.scalar(select(User).where(func.lower(User.email) == (actor or "").lower(),
                                                     User.role == "admin", User.is_active.is_(True)))
            if user is None:
                raise Refused("--actor must be the e-mail address of an active admin: decisions that need an admin "
                              "keep needing one")
            decider = _Decider(user.email, bool(user.can_access_sensitive))
        else:
            decider = _Decider(RESTORE_ACTOR, True)
        if run_id is not None and await s.get(ImportRun, run_id) is None:
            raise Refused("there is no import run %d" % run_id)
        # Not while an import, a rollback or a rescan runs (its rows could be written back over a withhold).
        if not await try_xact_lock(s, GLOBAL_LOCK):
            raise Refused("an import, rollback or rescan is running: try again when it has finished")
        if not removals_only:
            for n, d in enumerate(data.get("decisions") or [], 1):
                reason = await _apply_decision(s, d, int(run_id or 0), user, apply)
                if reason:
                    listed.setdefault(reason, []).append(n)
                else:
                    applied += 1
        after = await identity.current_after_run(s)
        removed: Counter[str] = Counter()
        removals_listed: dict[str, list[int]] = {}
        removals = data.get("removals") or []
        for n, r in enumerate(removals if isinstance(removals, list) else [], 1):
            outcome = await _apply_removal(s, r, decider, review, via=via, after=after)
            if outcome in REMOVAL_DONE:
                removed[outcome] += 1
            else:
                removals_listed.setdefault(outcome, []).append(n)
        released: Counter[str] = Counter()
        releases_listed: dict[str, list[int]] = {}
        releases = data.get("releases") or []
        for n, r in enumerate(releases if isinstance(releases, list) else [], 1):
            outcome = await _apply_release(s, r, decider, review, via=via)
            if outcome in RELEASE_DONE:
                released[outcome] += 1
            else:
                releases_listed.setdefault(outcome, []).append(n)
        if apply:
            detail: dict[str, Any] = {"run": run_id, "applied": applied,
                                      "not_applied": {k: len(v) for k, v in listed.items()},
                                      "removals": dict(removed),
                                      "removals_not_applied": {k: len(v) for k, v in removals_listed.items()}}
            if releases:
                detail.update(releases=dict(released),
                              releases_not_applied={k: len(v) for k, v in releases_listed.items()})
            if removals_only:
                detail["via"] = "restore"
            await audit.record(s, decider.email, "brain.decisions_applied", "import_run" if run_id else "brain",
                               run_id if run_id else "", detail)
            await s.commit()
        else:
            await s.rollback()  # the removals ran to count them: nothing is kept
    return {"applied" if apply else "would_apply": applied, "not_applied": listed, "removals": dict(removed),
            "removals_not_applied": removals_listed, "releases": dict(released),
            "releases_not_applied": releases_listed}


REMOVAL_DONE = ("withheld", "held for review", "purged", "already done", "kept for later imports")
RELEASE_DONE = ("released", "already done", "kept for later imports")


def _carried_stone(r: dict[str, Any], *, via: str, after: int) -> identity.Stone:
    """The stone a withhold in a decisions file becomes here. Version 4 carries the JARVIS row it named (session, row
    id, role, tool_call_id, uid) and its group; version 3 only the key, whose form gives the row or the uid
    (identity.parse_key; the role unknown)."""
    def text(name: str, limit: int = 200) -> str:
        value = r.get(name)
        return value[:limit] if isinstance(value, str) else ""

    key = r["source_key"]
    session, row, uid = text("session"), text("row"), text("uid")
    uid_known = r.get("uid_known") is True and bool(uid)
    if not (session or row or uid):  # version 3, or a stone that named no row when it was exported
        session, row, uid = identity.parse_key(key)
        uid_known = bool(uid)  # a uid-form key: the uid the admin decided on
    review = identity.KEPT if r.get("review") == identity.KEPT else identity.CERTAIN
    return identity.Stone(key=key, anchor=text("anchor") if text("anchor") != key else "", session=session, row=row,
                          role=text("role", 40), tool_call_id=text("tool_call_id"), uid=uid, uid_known=uid_known,
                          via=via, review=review, bound=bool(row or uid), after_run=after)


async def _apply_removal(s: AsyncSession, r: Any, user: Any, review: Any, *, via: str, after: int) -> str:
    """One removal from the decisions file: the tombstone, then the message withheld or the item purged if this
    database holds it, each audited as the API audits it (ids and counts). Returns what happened, or why it was not
    applied.

    A withhold's stone is written in a savepoint of its own: when the withhold itself is refused here, the stone
    stays, so the next import brings the message back withheld. The rows it names are found by key and by the JARVIS
    row it names (identity.rows_for); a row that only shares its uid is held for review. A purge and its tombstone go
    together: a refusal (the sensitive-data permission) leaves neither."""
    if not isinstance(r, dict) or not isinstance(r.get("source_key"), str) or not r["source_key"] or r.get(
            "reason") not in (WITHHELD_BY_ADMIN, PURGED_BY_ADMIN):
        return "not a removal"
    key, reason = r["source_key"], r["reason"]
    try:
        if reason == WITHHELD_BY_ADMIN:
            binding = _carried_stone(r, via=via, after=after)
            async with s.begin_nested():
                existed = await s.get(BrainTombstone, key) is not None
                await identity.put_stone(s, binding, actor=user.email)
            async with s.begin_nested():
                certain, uid_only = await identity.rows_for(s, binding)
                if not certain and not uid_only:
                    # No row of it here (not imported yet, or removed by a rollback or an import): its rollback
                    # copies, and the items promoted from an earlier row of it, which kept the text, lose it.
                    await withhold_copies(s, source_keys=[key], actor=user.email, cause=via)
                    return "already done" if existed else "kept for later imports"
                outcome = "already done"
                for message in certain:
                    await s.refresh(message)  # a withhold just before may have taken it with its own group
                    if message.withheld:
                        continue
                    done = await review.withhold_message(s, message, actor=user.email, cause=via, via=via)
                    await _withheld_audit(s, user, message, done, tombstone=not existed, via=via)
                    outcome = "withheld"
                for message in uid_only:
                    await s.refresh(message)
                    if message.withheld:
                        continue
                    done = await review.hold_message(s, message, anchor=binding.group, actor=user.email, cause=via)
                    await audit.record(s, user.email, "archive.message_held", "archive_message", message.id, {
                        "message": message.id, "session": message.session_id, "anchor": binding.group,
                        "rule": "same message id", "copies": [c["item"] for c in done.copies],
                        "versions_blanked": done.versions_blanked,
                        "rollback_copies_blanked": done.rollback_copies_blanked,
                        "answers_scrubbed": done.answers_scrubbed, "via": via})
                    if outcome != "withheld":
                        outcome = "held for review"
                return outcome
        async with s.begin_nested():
            existed = await _tombstone(s, key, reason, user)
            item = await s.scalar(select(MemoryItem).where(MemoryItem.source_key == key))
            if item is None:
                return "already done" if existed else "kept for later imports"
            if SENSITIVE_AGENT == item.agent and not user.can_access_sensitive:
                raise store.BrainError("needs the sensitive-data permission")
            if item.deleted_at is None:
                await store.delete_item(s, item, actor=user.email,
                                        note=RESTORE_NOTE if via == "restore" else DECISION_NOTE)
            flags = dict(item.flags or {})
            detail = {"item": item.id, "kind": item.kind, "agent": item.agent}
            origin = {"origin": item.origin, "message": flags.get("message_id"),
                      "source_withheld": bool(flags.get("source_withheld"))}
            result = await review.purge(s, item, actor=user.email)
            answers = result.pop("answers_scrubbed")
            await audit.record(s, user.email, "memory.purged", "memory_item", detail["item"],
                               {**detail, **result, **origin, "answers_scrubbed": answers})
            return "purged"
    except store.BrainError as exc:
        return "%s on the test copy, refused here: %s" % (reason, exc)
    except (DataError, IntegrityError):
        return "%s on the test copy, refused here by the database" % reason


async def _tombstone(s: AsyncSession, key: str, reason: str, user: Any) -> bool:
    """Whether the tombstone was there already; adds it otherwise."""
    if await s.get(BrainTombstone, key) is not None:
        return True
    s.add(BrainTombstone(source_key=key, purged_at=now(), purged_by=user.email[:200], reason=reason))
    await s.flush()
    return False


async def _withheld_audit(s: AsyncSession, user: Any, message: ArchiveMessage, done: Any, *, tombstone: bool,
                          via: str) -> None:
    sess = done.session
    await audit.record(s, user.email, "archive.message_withheld", "archive_message", message.id, {
        "message": message.id, "session": message.session_id,
        "imported_messages": sess.imported_messages if sess is not None else None,
        "withheld_messages": sess.withheld_messages if sess is not None else None,
        "tombstone": tombstone, "copies": [c["item"] for c in done.copies],
        "versions_blanked": done.versions_blanked, "rollback_copies_blanked": done.rollback_copies_blanked,
        "answers_scrubbed": done.answers_scrubbed, "via": via,
        "also_withheld": done.also_withheld, "held_for_review": done.held_for_review,
        "identical_withheld": done.identical_withheld, "keys_tombstoned": done.keys_tombstoned,
        "title_hidden": done.title_hidden})


async def _apply_release(s: AsyncSession, r: Any, user: Any, review: Any, *, via: str) -> str:
    """One release from the decisions file (an admin said a held JARVIS row is not the withheld message): the
    exemption, then a row held here for that group released as the API releases it (review.release_hold: its text
    comes back at the next import). A row a certain rule names stays withheld (the exemption never skips those)."""
    names = ("anchor", "session", "row")
    if not isinstance(r, dict) or not all(isinstance(r.get(k), str) and r[k] for k in names):
        return "not a release"
    anchor, session_id, row_id = (r[k][:200] if k != "anchor" else r[k] for k in names)
    async with s.begin_nested():
        added = (await s.execute(pg_insert(BrainWithholdExemption).values(
            anchor_key=anchor, source_session_id=session_id, source_row_id=row_id, reason="released by an admin",
            decided_by=user.email[:200]).on_conflict_do_nothing())).rowcount
        held = (await s.execute(
            select(ArchiveMessage, ArchiveSession, BrainTombstone)
            .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
            .join(BrainTombstone, BrainTombstone.source_key == ArchiveMessage.source_key)
            .where(ArchiveSession.source_session_id == session_id, ArchiveMessage.source_message_id == row_id,
                   ArchiveMessage.withheld.is_(True), BrainTombstone.reason == WITHHELD_BY_ADMIN,
                   BrainTombstone.review == identity.PENDING,
                   func.coalesce(func.nullif(BrainTombstone.anchor_key, ""), BrainTombstone.source_key) == anchor)
            .order_by(ArchiveMessage.id))).all()
        released = 0
        for message, sess, _ in held:
            try:
                async with s.begin_nested():
                    stone = await review.release_hold(s, message, sess, actor=user.email)
            except store.BrainError:
                continue  # the same message as a withheld one: it stays withheld
            await audit.record(s, user.email, "archive.hold_released", "archive_message", message.id, {
                "message": message.id, "session": message.session_id,
                "anchor": stone.anchor_key or stone.source_key, "rule": stone.via, "via": via})
            released += 1
    if released:
        return "released"
    return "kept for later imports" if added else "already done"


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
    if d.get("edit_not_exported") and not d.get("deleted"):
        # The decision was taken on the edited text, which was not exported: applying it alone would be wrong.
        return ("edited on the test copy, but customer and Credit Repair text is never exported: edit and review it "
                "by hand")
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
