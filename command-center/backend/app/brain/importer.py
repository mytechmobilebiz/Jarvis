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
    AuditLog,
    BrainDecisionBackup,
    BrainFrozenSession,
    BrainMaintenance,
    BrainPurgedContent,
    BrainPurgedItem,
    BrainSessionLink,
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
from . import IMPORTER_VERSION, identity, later
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
from .later import joined_families, session_families
from .quotes import refresh_quotes, scrub_quotes
from .policy import LEGACY_LOCK, RISK_FLAGS, SENSITIVE_AGENT

LOCK_CLASS = 7_314_002_213
LOCK_KEY = ((LOCK_CLASS + 2 ** 31) % 2 ** 32) - 2 ** 31  # the same number as a signed 32-bit lock key
GLOBAL_LOCK = 0  # (LOCK_KEY, 0): one import, rollback or rescan at a time
# Who can hold GLOBAL_LOCK: an import, a rollback, a rescan, decisions-apply, and an admin's withhold, keep, release,
# purge or redaction through the API (api/brain._no_import_running). The refusal names them all (review 2, S24); it
# keeps "is running" (restore.ps1 tries decisions-apply again on it).
LOCK_BUSY = ("another import, rollback or rescan is running, or an admin's withhold, release, keep, purge or "
             "redaction is in progress: try again when it has finished")
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
    """The JARVIS sources, open read-only for one command. With files=False (Settle: conversations only) the profile
    and content folders are not needed and not read."""

    def __init__(self, paths: SourcePaths, tz: ZoneInfo, tz_name: str, *, files: bool = True) -> None:
        if files and (not paths.profile or not os.path.isdir(paths.profile) or os.path.islink(paths.profile)):
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
            if root and files:
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
        # Hermes's time of the backup's newest message (its own clock): a decision backup records it
        # (brain_decision_backups.newest_at).
        newest = self.backup.newest_at
        self.db_info["newest_at"] = newest.isoformat() if newest else None
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


# 0.6.2 plan 8.1: what the importer (and the withhold API: api/brain._no_legacy_data) says while 0.6.0/0.6.1 Brain data
# is locked (brain_maintenance 'legacy_brain_data', set by migration 0008).
LEGACY_REFUSAL = ("this database holds JARVIS Brain data that Command Center 0.6.0 or 0.6.1 imported (legacy): it is "
                  "locked, and hidden from every reader, until it is imported again. Run the legacy reset first: "
                  "brain-import legacy-reset --export FILE --yes (jarvis-import -Mode LegacyReset); then import the "
                  "same JARVIS backup again and finish the decisions on it (brain-import settle)")


async def legacy_locked(session: AsyncSession) -> bool:
    """Whether the legacy lock is set (0.6.2 plan 8.1)."""
    return await session.scalar(select(BrainMaintenance.name).where(BrainMaintenance.name == LEGACY_LOCK)) is not None


async def refuse_legacy(session: AsyncSession) -> None:
    """Refused (before any change) while legacy 0.6.0/0.6.1 Brain data is locked: an import, Settle, a resume and a
    decisions file (decisions-apply after an import, and its export) wait for the legacy reset (plan 8.1)."""
    if await legacy_locked(session):
        raise Refused(LEGACY_REFUSAL)


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
class SettleScope:
    """What a Settle run finishes (0.6.2 plan 4.1): the decisions (their admin's own stones' keys) whose newest backup
    is this one, and the JARVIS sessions of their conversation families in this backup (also sessions a -Sample run
    skipped); and the decisions that wait on another backup (its SHA-256 or label -> how many)."""
    groups: list[str]
    family: set[str]
    waiting: dict[str, int] = field(default_factory=dict)
    families: dict[str, list[str]] = field(default_factory=dict)  # each decision's own family (Settle holds only there)


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
                 actor: str = DEFAULT_ACTOR, settle_scope: SettleScope | None = None) -> None:
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
        # replay_rows(): holds an admin released on this very backup (key, stone, reason), dropped by apply_releases().
        self.auto_release: list[tuple[str, identity.Stone, str]] = []
        self._hidden_titles: set[str] | None = None
        self._title_hidden: dict[str, bool] = {}
        self._by_session_id: dict[str, src.SourceSession] | None = None
        self.current_sess_id: int | None = None  # the archive session held_rows() looks at (None: not known)
        # (JARVIS row id, role) -> the ids of this session's stored rows of it (whatever their keys): _collides()
        self._stored_rows: dict[tuple[str, str], list[int]] = {}
        # key -> the stored row (id) of the very same JARVIS row under an earlier key (identity.exact), in a withheld
        # family of a later backup: never stored twice, never re-keyed (plan 5.2; write_messages records a duplicate)
        self.same_stored: dict[str, int] = {}
        # (JARVIS row id, role) -> (stored row id, its exact identity) of this session's stored rows: _stored_twin()
        self._stored_exact: dict[tuple[str, str], list[tuple[int, identity.Exact]]] = {}
        self._stored_withheld: set[int] = set()  # this session's stored rows that are withheld (load_stored)
        # stored row id -> (its exact identity, the uids it absorbed): later_rows judges a stored row by what it holds
        self._stored_by_id: dict[int, tuple[identity.Exact, tuple[str, ...]]] = {}
        self._bound_rows: dict[tuple[str, str], src.SourceMessage | None] = {}
        self._uid_backfill: list[tuple[int, str, list[str]]] = []
        # Texts and contents of the rows matched as members, per group: in memory for this run only.
        self.group_texts: dict[str, set[tuple[str, str, str]]] = {}
        self.group_contents: dict[str, list[str]] = {}
        self._readable: dict[str, tuple[str, str, str, str]] = {}  # key -> its readable text (in memory only)
        self._sid = ""  # the JARVIS session held_rows() looks at
        # The JARVIS backup in hand (its SHA-256): a decision taken on it is replayed word for word (do_session), and a
        # decision this import makes is taken on it (decided_on).
        self.backup_sha = identity.run_sha256({"db": sources.db_info})
        # JARVIS sessions this import replays (load(): their family holds a decision taken on this very backup), and
        # the family of every session (union over parent links and markers of the backup, the recorded links and the
        # archive's lineage).
        self.replay_sessions: set[str] = set()
        self._family: dict[str, str] = {}
        # family root -> the decisions (admin's own stones' keys) bound in it, and per decision the backup it was taken
        # on: a session of such a family in another backup is a later backup there (later_groups)
        self._decision_roots: dict[str, list[str]] = {}
        self._joined: dict[str, list[str]] = {}  # family root -> decisions it joins (joined_families)
        self._joins: list[later.Join] = []  # the sessions that joined a family that way (links to record: plan 5.1)
        self._readable_uids: set[str] = set()  # certain stone uids a readable or released stored row carries
        self._later_session = False  # do_session: a session of a withheld family in a later backup (plan 5.2)
        # (role, tool call, time) -> a later family's readable stored rows (later.load_family_readable: the safe
        # copies of plan 5.3), and the withheld texts a later backup holds (_reply_quotes)
        self._family_readable: dict[tuple[str, str, Any], list[later.Readable]] = {}
        self._later_texts: dict[str, str] = {}
        # Plan 5.5: every decision's withheld text this backup holds (in memory for this run only), the tripwire's
        # counts and locations (kept in the tally, so a resumed run reports the whole run), and this session's rows
        # it found (key -> inside a family or not; their locations are known once they are stored).
        self.w_texts: dict[str, str] = {}
        self.tripwire = later.Tripwire.from_tally(tally.d.get("tripwire"))
        self._tripwire_keys: dict[str, bool] = {}
        self.frozen: set[str] = set()  # JARVIS sessions an admin stopped importing (plan 5.3)
        self._decisions: dict[str, later.Decision | None] = {}  # group -> the decision as this backup shows it
        self._conversation: dict[str, str] = {}  # group -> its conversation in the plan report (5.6)
        # the later classifier's outcome for this session (later_rows): rows readable as safe copies or released,
        # stored rows of the very same JARVIS row to withhold now (blanking is always allowed: plan 5.2), and why a
        # key was not stored twice
        self._readable_new: dict[str, str] = {}
        self._blank_twins: dict[int, identity.Match] = {}
        self._same_reason: dict[str, str] = {}
        # Settle (plan 4): the decisions this run finishes and their outcomes (settle.Engine), the evidence of each hold
        # it writes, in memory until inherit() stores it
        self.settle_scope = settle_scope
        self.settle_engine: Any = None
        self.relations: dict[str, tuple[str, ...]] = {}
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
        await self.prepare_identity()
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

    async def prepare_identity(self) -> None:
        """The decisions' families (session_families), which decision each family holds, the sessions this import
        replays (_replay_scope), and for a Settle run the outcomes of the decisions it finishes (settle.Engine). The
        stones are loaded (self.ids)."""
        self._family = await self._families()
        self._decision_roots = {}
        for st in self.ids.by_key.values():
            if st.session and st.certain and not st.anchor:  # an admin's own decision, and the family it is bound in
                root = self._family.get(st.session, st.session)
                if st.key not in self._decision_roots.setdefault(root, []):
                    self._decision_roots[root].append(st.key)
        for st in self.ids.by_key.values():  # a decision's other stones bind its family too
            if st.session and st.group in self.ids.by_key and st.anchor:
                root = self._family.get(st.session, st.session)
                if st.group not in self._decision_roots.setdefault(root, []):
                    self._decision_roots[root].append(st.group)
        # A continuation or branch whose link to its family is lost (a deleted middle branch, an orphaned rotation
        # without its marker): its copied history holds a row at the very role, call and time of a decision's stone
        # (Hermes keeps a copied row's time), or a row of it carries a certain stone's uid, so it is that family's
        # (plan 5.1; recorded as a 'copied_rows' or 'shared_uid' link by record_links, never forgotten).
        self._joins = []
        self._joined = await joined_families(self.s, self.src.backup, self.ids, self._family, self._decision_roots,
                                             joins=self._joins)
        self.frozen = await later.frozen_sessions(self.s)
        self._decisions = {}
        # The uids of certain stones that a readable stored row or a released one carries: never a copy identity
        # (plan 5.3: the uid is shared with a readable or released stored row).
        uids = sorted({st.uid for st in self.ids.by_key.values()
                       if st.uid and st.certain and st.via not in identity.FOLD_VIAS})
        self._readable_uids = set()
        for i in range(0, len(uids), 1000):
            self._readable_uids |= set((await self.s.scalars(select(ArchiveMessage.source_uid).where(
                ArchiveMessage.withheld.is_(False), ArchiveMessage.source_uid.in_(uids[i:i + 1000])).distinct()))
                .all())
            self._readable_uids |= set((await self.s.scalars(select(BrainWithholdExemption.source_uid).where(
                BrainWithholdExemption.source_uid.in_(uids[i:i + 1000])).distinct())).all())
        await self._replay_scope()
        self._later_texts = {}
        self.w_texts = {}
        for group in sorted({g for gs in self._decision_roots.values() for g in gs}):
            st = self.ids.by_key.get(group)
            if st is None:
                continue
            text = self._w_text(group)
            if text:
                self.w_texts[group] = text  # the tripwire's (plan 5.5): in memory only
                if st.decided_on != self.backup_sha:
                    self._later_texts[group] = text
        self.tripwire.texts = max(self.tripwire.texts, len(self.w_texts))
        if self.settle_scope is not None:
            from . import settle as settle_engine

            sessions = self._source_sessions()
            self.settle_engine = settle_engine.Engine(
                self.src.backup, sessions, self.ids, self.settle_scope.groups, self.settle_scope.family,
                stored=lambda m: not self.exclusion(m), families=self.settle_scope.families)
            self.settle_engine.decide()

    # Decide once, replay exactly (0.6.2 plan 3.2) ------------------------------------------------------------------

    async def _families(self) -> dict[str, str]:
        return await session_families(self.s, self.src.backup)

    async def _replay_scope(self) -> None:
        """The JARVIS sessions this import replays: a decision (a stone, a release) was taken on this very backup
        (decided_on is its SHA-256) for the session; once the decisions on this backup are finished (Settle:
        brain_decision_backups.settled_run_id), for every session of such a session's family. Replayed, a session's
        rows are matched with the stored decisions by exact identity only, and the rules never run (replay_rows)."""
        self.replay_sessions = set()
        if not self.backup_sha:
            return
        decided = set((await self.s.scalars(select(BrainTombstone.source_session_id).where(
            BrainTombstone.reason == WITHHELD_BY_ADMIN, BrainTombstone.decided_on == self.backup_sha,
            BrainTombstone.source_session_id != "").distinct())).all())
        decided |= set((await self.s.scalars(select(BrainWithholdExemption.source_session_id).where(
            BrainWithholdExemption.decided_on == self.backup_sha).distinct())).all())
        # A family whose admin's decision was taken on another backup is a later backup here (its holds of this backup
        # included): the later-backup path decides it again, the same way each time (plan 5.2, 5.3; B7).
        decided = {sid for sid in decided if not self.later_groups(sid)}
        if not decided:
            return
        self.replay_sessions = set(decided)
        settled = await self.s.scalar(select(BrainDecisionBackup.settled_run_id).where(
            BrainDecisionBackup.sha256 == self.backup_sha))
        if settled is not None:
            roots = {self._family.get(sid, sid) for sid in decided}
            self.replay_sessions |= {ss.id[:200] for ss in self.src.backup.sessions()
                                     if self._family.get(ss.id[:200], ss.id[:200]) in roots
                                     and not self.later_groups(ss.id)}

    def replay_rows(self, ss: src.SourceSession, msgs: list[src.SourceMessage]) -> dict[str, identity.Match]:
        """held_rows() of a replayed session: the stored decisions by exact identity only (a stone's key, or the exact
        JARVIS row it is bound to; a release of that exact row frees a hold). Nothing else is evaluated: the decision
        was taken on this very backup, so its result is applied word for word (0.6.2 plan 3.2, C1)."""
        held: dict[str, identity.Match] = {}
        self.moved_keys = set()
        self.auto_release = []
        self._sid = ss.id
        for m in msgs:
            here = identity.exact_of_source(ss.id, m)
            mt: identity.Match | None = None
            st = self.ids.by_key.get(m.key)
            if st is not None:
                review = identity.PENDING if st.review == identity.PENDING else identity.CERTAIN
                if not st.row or not st.session or (st.session, st.row) == (here.session, here.row):
                    mt = identity.Match(st, "key", review)
                elif not self.src.backup.has_row(st.session, st.row) and st.role in ("", here.role):
                    mt = identity.Match(st, "key", review)  # the bound row is gone: the uid key passed to this row
                else:
                    self.moved_keys.add(m.key)  # the key's stone is bound to another row of this backup
                    continue
            if mt is None or mt.review == identity.PENDING:
                found = self.ids.row_stones(here)
                if found and (mt is None or found[0].certain):
                    mt = identity.Match(found[0], "same JARVIS row",
                                        identity.PENDING if found[0].review == identity.PENDING else identity.CERTAIN)
            if mt is not None and mt.review == identity.PENDING and self.ids.released(mt.group, here,
                                                                                      backup=self.backup_sha):
                # an admin released this exact row, decided on this very backup: a hold left on it (another backup's
                # import held it again) goes, and its text comes back (apply_releases)
                self.auto_release.append((m.key, mt.stone, "released by an admin (the same message)"))
                mt = None
            if mt is not None:
                held[m.key] = mt
        return held

    async def record_links(self, ss: src.SourceSession) -> None:
        """The family links this session's own lineage gives, recorded the first time an import sees them and never
        forgotten (brain_session_links, append-only): its parent (a continuation or a branch), and the parents its
        _branched_from and _compressed_from markers name. A reset and a delegated sub-agent are not recorded."""
        if not self.write:
            return
        links: set[tuple[str, str]] = set()
        if ss.lineage in identity.LINEAGE and ss.hermes_parent:
            links.add((ss.hermes_parent[:200], "parent"))
        for marker, how in (("_branched_from", "branched_from"), ("_compressed_from", "compressed_from")):
            if ss.markers.get(marker):
                links.add((ss.markers[marker][:200], how))
        for a, how in sorted(links):
            if a and a != ss.id[:200]:
                await self.s.execute(pg_insert(BrainSessionLink).values(
                    a=a, b=ss.id[:200], how=how, first_run=self.run_id).on_conflict_do_nothing())
        # a session that joined a decision's family through a shared uid or copied history (plan 5.1)
        await later.record_joins(self.s, self._joins, self.ids, self.run_id, ss.id)

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
            holder = await self.s.scalar(select(MemoryItem.id).where(MemoryItem.source_key == p.key).limit(1))
            if holder is not None:
                # Purged bytes under a key that still has its own item (an incoming version of a changed file an admin
                # purged, or a purged file's text copied over another file): this version stays out, and the item and
                # its key stay as they are. A tombstone here would keep every later version out and make decisions-
                # apply (a restore) purge that item, which nobody purged (review 2, S16).
                self.t.bump("purged_same_text")
                return rec("duplicate", "incoming version purged by an admin", holder)
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
        says the same thing again ("Sent the invoice.", "yes") is never a copy.

        Provenance first: a copy keeps its original's uid, so a row is paired with the compacted row of its own uid;
        a row whose uid another row of the session carries (its own rewound or archived original: Hermes's protected
        tail) is the copy of that row, never of a compacted row that only has the same text. Without uids the same
        holds by the importer's twin link: past the summary, a row that still has a rewound row with its text left
        (Hermes rewinds the protected tail and writes it again right after the summary) is that row's copy, never a
        compacted row's. A row with no uid, or a fresh one, and no rewound twin left, is paired by text as before."""
        waiting: dict[tuple[str, str], list[src.SourceMessage]] = {}
        copies: dict[str, str] = {}
        uids = Counter(m.uid for m in msgs if m.uid)
        rewound = Counter((self.dup_fp(m), m.tool_call_id) for m in msgs if self.exclusion(m) == REWOUND)
        block = ""  # "open" right after compacted rows, "summary" once the summary was passed, "" outside a block
        for m in msgs:
            if self.exclusion(m):
                continue
            key = (self.dup_fp(m), m.tool_call_id)
            found = None
            twin_left = block == "summary" and m.compacted != 1 and rewound[key] > 0
            if twin_left:
                rewound[key] -= 1
            if m.compacted != 1 and block and waiting.get(key):
                cands = waiting[key]
                found = next((i for i, w in enumerate(cands) if m.uid and w.uid == m.uid), None)
                if found is None and not (m.uid and uids[m.uid] > 1) and not twin_left:
                    found = 0  # no uid, or a fresh one: the first compacted row with this text
            if m.compacted == 1:
                waiting.setdefault(key, []).append(m)
                block = "open"
            elif found is not None:
                copies[m.key] = waiting[key].pop(found).key
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
        # the twin's action is decided as for any row. A row is the twin of the rewound row with its own uid; else of
        # the first one whose uid no live row carries (a rewound row whose copy is there is that copy's original).
        rewound: dict[tuple[str, str], list[src.SourceMessage]] = {}
        live_uids = Counter(m.uid for m in msgs if m.uid and not self.exclusion(m))
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
                    rewound.setdefault((self.dup_fp(m), m.tool_call_id), []).append(m)
                plans.append(MessagePlan(m, "excluded", reason))
                continue
            fp = self.dup_fp(m)
            twin = rewound.get((fp, m.tool_call_id)) or []
            pick = next((i for i, r in enumerate(twin) if m.uid and r.uid == m.uid), None)
            if pick is None:
                pick = next((i for i, r in enumerate(twin) if not r.uid or live_uids[r.uid] == 0), None)
            twin_of = twin.pop(pick).key if pick is not None else ""
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
            "source_uid": m.uid[:200], "absorbed_uids": [u[:200] for u in m.absorbed],
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
        elif mt.stone.review == identity.KEPT:
            # a hold an admin kept: the row says so, and who kept it (review 3, rv3-dur-6)
            flag["review"] = identity.KEPT
            if mt.stone.kept_by:
                flag["kept_by"] = mt.stone.kept_by
            if mt.stone.kept_at is not None:
                flag["kept_at"] = mt.stone.kept_at.isoformat()
        return flag

    # The identity of withheld messages (spec 2.9) ----------------------------------------------------------------

    def held_rows(self, ss: src.SourceSession, msgs: list[src.SourceMessage], plans: list[MessagePlan],
                  rows: dict[str, dict[str, Any]]) -> dict[str, identity.Match]:
        """The source rows of this session that are the same message as a withheld one (key -> Match: stored without
        text, members inherit a stone, holds are listed for an admin), for a session that is neither replayed (a
        decision taken on this very backup and finished: replay_rows), being finished (Settle: settle_rows) nor of a
        withheld family in a later backup (later_rows, plan 5.3): a session outside every family (plan 5.4: nothing
        here can name its rows but the database guard's own rules), or of a family whose decisions were taken on this
        very backup and are not finished yet (the test copy between a withhold and Settle: best effort, Settle
        decides). Also sets self.moved_keys (keys whose stone names another JARVIS row that is still in the backup:
        the row is excluded as a key collision) and self.auto_release (holds an admin released on this exact row).

        Provenance only (0.6.2 plan step 6 deleted every rule that inferred identity from text, time, copy position,
        release digests or the admin's view):
        - R-key, R-row, R-fold (identity.Identities.certain; excluded rows too: a rewound original keeps its stone);
        - the importer's own links (R-link: a carried-on copy, an adjacent repeat, a rewound twin of a held row);
        - the shared uid (R-uid): a member when its text is the withheld text as this backup holds it (the rows the
          stones are bound to, the members found this run), a hold otherwise ('same message id, other text');
        - a hold stays a hold unless an admin released that exact row (decided on this backup, or the stored row
          already shows that very content): its stale hold goes (apply_releases).
        Owner question 8 (a): a later backup's reply quoting a withheld text is held (_reply_quotes)."""
        backup = self.src.backup
        self._sid = ss.id
        by_key = {m.key: m for m in msgs}
        held: dict[str, identity.Match] = {}
        pend: dict[str, identity.Match] = {}
        self.moved_keys = set()
        self.auto_release = []
        for m in msgs:
            mt = self.ids.certain(ss.id, m, backup.has_row)
            if mt is identity.MOVED:
                self.moved_keys.add(m.key)
            elif isinstance(mt, identity.Match) and mt.review == identity.PENDING:
                pend[m.key] = mt  # the hold itself, unless a certain rule matches it below
            elif isinstance(mt, identity.Match):
                held[m.key] = mt
        self._links(plans, held, by_key)
        for m in msgs:  # R-uid: provenance (the uid) and the text; without the text to agree, an admin decides
            if m.key in held or not m.uid or m.key in self.moved_keys:
                continue
            folds = [st for st in self.ids.uid_stones(m.uid, m.role, folds=True) if st.via in identity.FOLD_VIAS]
            fold = next((st for st in folds if identity.text_key(m) in self.anchor_texts(st.group, held, by_key)[0]),
                        None)
            if fold is not None:  # a copy of a withheld fold (its own uid and its very text)
                held[m.key] = identity.Match(fold, identity.FOLD, identity.CERTAIN)
                continue
            cands = self.ids.uid_stones(m.uid, m.role)
            if not cands:
                continue  # at most a fold's uid: the survivor's own message, which says nothing of the withheld one
            st = cands[0]
            texts, contents = self.anchor_texts(st.group, held, by_key)
            if identity.text_key(m) in texts:
                held[m.key] = identity.Match(st, "same message id", identity.CERTAIN)
            elif self._released_here(st.group, ss, m, rows.get(m.key)) or m.key in pend:
                # an admin released this exact row; a hold stays the hold it is until an admin decides
                continue
            else:  # an archived generation with other text, a pruned stub, a fold that kept this uid: a hold
                rel = {identity.SAME_UID}
                for c in contents:
                    rel |= identity.text_relations(m.content, c)
                if not texts:
                    rel.add(identity.NOT_IN_BACKUP)
                held[m.key] = identity.Match(st, identity.UID_HOLD, identity.PENDING)
                self.relations[m.key] = tuple(identity.relation_order(rel))
        self._links(plans, held, by_key)
        for k, mt in pend.items():
            if k in held:
                continue
            if self._released_here(mt.group, ss, by_key[k], rows.get(k)):
                # an admin released this exact row: the hold left on it goes, and its text comes back
                self.auto_release.append((k, mt.stone, "released by an admin (the same message)"))
                continue
            held[k] = mt
        if self.current_sess_id is not None:
            self._reply_quotes(ss, plans, rows, held)
        self._links(plans, held, by_key)  # the importer's duplicates of a row held here are held with it
        return held

    def _released_here(self, group: str, ss: src.SourceSession, m: src.SourceMessage, row: dict[str, Any] | None
                       ) -> bool:
        """Whether an admin's release (or 'left readable') for this group covers this exact JARVIS row and the content
        this backup holds: decided on this very backup, or the stored row already shows that content readable (the
        same source row, or the same text). A release never follows another row or another version (0.6.2 plan 1.2,
        step 6)."""
        shown = row is not None and not row["withheld"] and (
            row.get("source_fp") == m.fp or self.stored_text(row) == self.readable_text(m, None))
        return self.ids.released(group, identity.exact_of_source(ss.id, m), backup=self.backup_sha,
                                 same_content=shown)

    # A later backup in a withheld family (0.6.2 plan step 5: app/brain/later.py) ----------------------------------

    def family_groups(self, session_id: str) -> list[str]:
        """Every decision whose conversation family holds this JARVIS session (plan 5.1: the sessions bound by its
        stones, the links and markers, and the sessions that joined it through a shared uid or copied history)."""
        root = self._family.get(session_id[:200], session_id[:200])
        return sorted(set(self._decision_roots.get(root, ())) | set(self._joined.get(root, ())))

    def later_groups(self, session_id: str) -> list[str]:
        """The decisions whose conversation family holds this JARVIS session and that were taken on another backup
        than this one (or on none known: a version 4 file, a legacy withhold): for them this import is a later backup
        (plan 3.3: fail-safe)."""
        return [g for g in self.family_groups(session_id)
                if (st := self.ids.by_key.get(g)) is not None and st.decided_on != self.backup_sha]

    def _family_of_groups(self, groups: list[str] | set[str]) -> set[str]:
        """The JARVIS sessions of the conversation families of these decisions (their roots, the sessions their
        stones are bound to, and the families that joined them)."""
        want = set(groups)
        roots = {r for r, gs in self._decision_roots.items() if want & set(gs)}
        roots |= {r for r, gs in self._joined.items() if want & set(gs)}
        sids = {sid for sid, r in self._family.items() if r in roots} | roots
        for st in self.ids.by_key.values():
            if st.group in want and st.session:
                sids.add(st.session)
                sids |= {sid for sid, r in self._family.items() if r == self._family.get(st.session, st.session)}
        return sids

    def _w_text(self, group: str) -> str | None:
        """The withheld text of a decision as this backup holds it (in memory only; plan 5.5): the exact JARVIS row
        (identity.exact) of the admin's own stone, or of a member that is the same text (the same JARVIS row, the same
        uid with that text, a duplicate, a copy at a copy position or its origin, a kept hold whose text was
        identical); else a row of the family with the copy identity of one of them (the same uid, time, role and
        call); None when this backup holds none of them (its text is never stored: owner question 6)."""
        own = self.ids.by_key.get(group)
        if own is None:
            return None
        same_text = ("key", "same JARVIS row", "same message id", "duplicate", "copied by JARVIS",
                     "the original JARVIS copied", "same tool call")
        cands = [own] + sorted((x for x in self.ids.groups.get(group, ()) if x is not own and x.certain
                                and (x.via in same_text or identity.IDENTICAL_REL in x.relations)),
                               key=lambda x: (x.via not in same_text, x.key))
        for st in cands:
            if not st.session or not st.row:
                continue
            row = self._bound_row(st.session, st.row)
            if row is not None and identity.same_row(identity.exact_of_stone(st),
                                                     identity.exact_of_source(st.session, row)):
                return row.content
        copies = {(st.uid, st.role, st.tool_call_id, st.at) for st in cands
                  if st.uid and st.at is not None and st.via not in identity.FOLD_VIAS}
        if not copies:
            return None
        sessions = self._source_sessions()
        first = [own.session] if own.session else []
        for sid in first + sorted(self._family_of_groups([group]) - set(first)):
            if sid not in sessions:
                continue
            for row in self.src.backup.messages(sid):
                here = identity.exact_of_source(sid, row)
                if (here.uid, here.role, here.tool_call_id, here.at) in copies:
                    return row.content
        return None

    def _decision(self, group: str) -> later.Decision | None:
        if group not in self._decisions:
            self._decisions[group] = later.decision_of(self.ids, group, self.w_texts.get(group), self.backup_sha)
        return self._decisions[group]

    def later_rows(self, ss: src.SourceSession, msgs: list[src.SourceMessage], plans: list[MessagePlan],
                   rows: dict[str, dict[str, Any]]) -> dict[str, identity.Match]:
        """held_rows() of a session of a withheld conversation family in a later backup: the later-backup classifier
        (app/brain/later.py; plan 5.2, 5.3, 5.5). Every row gets one outcome:
        - a stored row (its key) is kept as stored (_reconcile): only blanked, by a certain match or a hold; a row an
          admin released whose release this backup does not cover is listed again (a hold with its evidence here);
        - the very JARVIS row of a stored row under an earlier key (Hermes's uid backfill) is not stored twice;
        - CERTAIN (later.certain): the exact identity of a stone, the copy identity of a certain stone, an absorbed
          uid; MOVED: its key's stone is another message (left out as a key collision);
        - a pending stone stays the hold it is, unless an admin released that exact row on this backup;
        - an admin's release of this exact row decided on this backup: readable;
        - SAFE COPY (later.safe_copy): readable; in this very session an alias of the stored row. The tripwire (5.5)
          holds one that is, or contains, a withheld text, unless its stored row is an admin's decision;
        - HELD FOR REVIEW: everything else ('new in a later backup', its computed relations to the family's withheld
          texts: later.strongest);
        - the importer's duplicate of a held row follows it: certain only when both carry the same uid (plan 4.2 (5)).
        Nothing here makes a row certain by its text, its time alone, or a link the importer drew."""
        backup = self.src.backup
        self._sid = ss.id
        self.moved_keys = set()
        self.auto_release = []
        self._readable_new = {}
        self._blank_twins = {}
        held: dict[str, identity.Match] = {}
        pend: dict[str, identity.Match] = {}
        by_key = {m.key: m for m in msgs}
        decisions = [d for g in self.family_groups(ss.id) if (d := self._decision(g)) is not None]
        for m in msgs:
            # A row that is a stored row of this conversation (its key, or its exact JARVIS row under an earlier key)
            # is judged by the stored row's identity and the uids it absorbed when it was stored (plan 5.2: the stored
            # row keeps what it holds; a fold or a renumbering JARVIS made since is not followed: owner question 7).
            # Its uid, when it had none stored, is the backup's (the very same JARVIS row: Hermes only fills a NULL).
            row = rows.get(m.key)
            # its key's stored row of this conversation is another JARVIS row (renumbered by replace_messages, or a uid
            # JARVIS gave to another row): this row is never stored under that key, and nothing it is gives that key
            # a stone (it would withhold the stored row for words that are not its own)
            other_row = (row is not None and row["session_id"] == self.current_sess_id
                         and row["source_message_id"] != str(m.id)[:200])
            stored = None if other_row else self._stored_identity(ss, m, row)
            if stored is not None:
                mt = later.certain(self.ids, ss.id, m, backup.has_row, self._readable_uids, here=stored[0],
                                   absorbed=stored[1])
            else:
                mt = later.certain(self.ids, ss.id, m, backup.has_row, self._readable_uids)
            if other_row and isinstance(mt, identity.Match) and m.key not in self.ids.by_key:
                continue  # kept as stored (_reconcile): the stored row under its key stays as it is
            if mt is later.MOVED:
                self.moved_keys.add(m.key)
            elif isinstance(mt, identity.Match) and mt.review == identity.PENDING:
                pend[m.key] = mt
            elif isinstance(mt, identity.Match):
                held[m.key] = mt
        for k, mt in pend.items():
            if self._released_here(mt.group, ss, by_key[k], rows.get(k)):
                # an admin released this exact row, decided on this backup: the hold left on it goes (apply_releases)
                self.auto_release.append((k, mt.stone, "released by an admin (the same message)"))
                continue
            held[k] = mt
        agent, staff = ss.attribution.agent, ss.attribution.staff
        for p in plans:
            m = p.m
            k = m.key
            if k in self.moved_keys:
                continue
            row = rows.get(k)
            if row is not None and row["session_id"] != self.current_sess_id:
                row = None  # its key's stored row is of another conversation: this row may be stored here already
            if row is not None:
                if (k not in held and row["withheld"] and k not in self.ids.by_key
                        and ((row["flags"] or {}).get("withheld_by_admin") or {}).get("released_at")
                        and not self._release_covers(m)):
                    # Released by an admin, but this later backup is not the one the release was decided on (the
                    # release covers the exact row and its content, plan 1.2): listed again with its evidence on this
                    # backup, never left withheld with nothing to review
                    self._hold_later(held, ss, m, decisions)
                continue  # a stored row of the family: kept as stored, or blanked (plan 5.2)
            if p.action != "import":
                continue  # excluded (not stored), or the importer's duplicate (below)
            twin = self._stored_twin(ss, m)
            if twin is not None:
                # the very JARVIS row of a stored row under its earlier key: never stored twice, never re-keyed (5.2);
                # its key gets the stone of a match, and a readable stored row of a certain message is withheld
                mt = held.get(k)
                if mt is not None and mt.review == identity.CERTAIN and twin not in self._stored_withheld:
                    self._blank_twins[twin] = mt
                self.same_stored[k] = twin
                self._same_reason[k] = later.SAME_ROW
                continue
            if k in held:
                continue
            if self._release_covers(m):
                self._readable_new[k] = "released"  # an admin released this exact row, decided on this backup
                if later.tripwire_relations(m.content, self.w_texts.values()):
                    self.tripwire.admin_decided += 1
                continue
            here = identity.exact_of_source(ss.id, m)
            sensitivity = p.cleaned.sensitivity if p.cleaned is not None else "restricted"
            safe = later.safe_copy(here, self.readable_text(m, None), agent, staff, sensitivity,
                                   self._family_readable)
            if safe is not None:
                if later.tripwire_relations(m.content, self.w_texts.values()):
                    if safe.admin_decided:
                        self.tripwire.admin_decided += 1
                    else:
                        # the tripwire (5.5): a safe copy that is, or holds, a withheld text this backup holds and no
                        # admin left readable: held, and the run's verify fails
                        self._hold_later(held, ss, m, decisions)
                        self._tripwire_keys[k] = True
                        continue
                self._readable_new[k] = "safe copy"
                if safe.session_id == self.current_sess_id:
                    self.same_stored[k] = safe.row_id  # an alias of the stored row of this conversation
                    self._same_reason[k] = later.SAFE_ALIAS
                continue
            self._hold_later(held, ss, m, decisions)
        for p in plans:  # the importer's duplicates follow the row they repeat; certain only with its uid (4.2 (5))
            m = p.m
            if (m.key in held or m.key in self.moved_keys or p.action != "duplicate"
                    or p.reason not in identity.PROVENANCE_REASONS or p.duplicate_of not in held):
                continue
            mt, target = held[p.duplicate_of], by_key.get(p.duplicate_of)
            ok = mt.review == identity.CERTAIN and target is not None and bool(m.uid) and m.uid == target.uid
            held[m.key] = identity.Match(mt.stone, "duplicate", identity.CERTAIN if ok else identity.PENDING)
            if not ok and p.duplicate_of in self.relations:
                self.relations[m.key] = self.relations[p.duplicate_of]
        return held

    def _stored_identity(self, ss: src.SourceSession, m: src.SourceMessage, row: dict[str, Any] | None
                         ) -> tuple[identity.Exact, tuple[str, ...]] | None:
        """(exact identity, absorbed uids) of the stored row of this session that a backup row is: the one under its
        key, else its very JARVIS row under an earlier key (_stored_twin); None when it is new here. A stored row
        without a uid takes the backup row's when they are the same JARVIS row."""
        found: tuple[identity.Exact, tuple[str, ...]] | None = None
        if row is not None and row["session_id"] == self.current_sess_id:
            found = self._stored_by_id.get(int(row["id"]))
        elif row is None or row["session_id"] != self.current_sess_id:
            twin = self._stored_twin(ss, m)
            found = self._stored_by_id.get(twin) if twin is not None else None
        if found is None:
            return None
        ex, absorbed = found
        here = identity.exact_of_source(ss.id, m)
        if not ex.uid and here.uid and identity.same_row(ex, here):
            ex = ex._replace(uid=here.uid)
        return ex, absorbed

    def _hold_later(self, held: dict[str, identity.Match], ss: src.SourceSession, m: src.SourceMessage,
                    decisions: list[later.Decision]) -> None:
        """A row held for review, 'new in a later backup' (plan 5.3), under the family's decision it relates to most,
        with its computed relations (later.strongest)."""
        if not decisions:  # every family here holds a decision (later_groups): never reached, held all the same
            group = self.later_groups(ss.id)[0]
            held[m.key] = identity.Match(self.ids.by_key[group], identity.LATER_HOLD, identity.PENDING)
            self.relations[m.key] = (identity.NOT_IN_BACKUP,)
            return
        d, rel = later.strongest(m, identity.exact_of_source(ss.id, m), decisions)
        held[m.key] = identity.Match(d.stone, identity.LATER_HOLD, identity.PENDING)
        self.relations[m.key] = rel

    async def load_family_readable(self, ss: src.SourceSession) -> None:
        """For a session of a withheld family in a later backup: the family's readable stored rows by (role, tool call,
        time), with their readable text and readers (later.safe_copy). In memory only."""
        self._family_readable = {}
        groups = self.family_groups(ss.id)
        if not self.later_groups(ss.id):
            return
        sids = self._family_of_groups(groups) | {ss.id[:200]}
        self._family_readable = await later.load_family_readable(self.s, sids)

    def _tripwire_outside(self, plans: list[MessagePlan], rows: dict[str, dict[str, Any]]) -> None:
        """Plan 5.5 outside every family: a row this run writes readable that is, or contains, a withheld text this
        backup holds is listed to admins by location once it is stored (_tripwire_locations), never hidden (owner
        question 8 (b)). Compared in memory only."""
        if not self.w_texts:
            return
        for p in plans:
            m = p.m
            if p.action != "import" or m.key in self.held or m.key in self.moved_keys:
                continue
            row = rows.get(m.key)
            if row is not None and (row["withheld"] or identity.same_source(row, m)):
                continue  # not written again, or it stays without text
            if later.tripwire_relations(m.content, self.w_texts.values()):
                self._tripwire_keys[m.key] = False

    async def _tripwire_locations(self, sess_id: int) -> None:
        """This session's tripwire rows (plan 5.5) by location, once stored: inside a family each was held instead of
        written readable (the run's verify fails); outside, each was written readable and is listed to admins. Archive
        ids and counts only, never text or a fingerprint; kept in the tally (a resumed run reports the whole run)."""
        found, self._tripwire_keys = self._tripwire_keys, {}
        if not found:
            return
        ids: dict[str, int] = {}
        if self.write and sess_id > 0:
            ids = dict((await self.s.execute(select(ArchiveMessage.source_key, ArchiveMessage.id).where(
                ArchiveMessage.session_id == sess_id, ArchiveMessage.source_key.in_(sorted(found))))).all())
        for key in sorted(found):
            where = {"session": sess_id if sess_id > 0 else None, "message": ids.get(key)}
            if found[key]:
                self.tripwire.inside += 1
                if len(self.tripwire.inside_at) < later.TRIPWIRE_LIST_CAP:
                    self.tripwire.inside_at.append(where)
            else:
                self.tripwire.outside += 1
                if len(self.tripwire.outside_at) < later.TRIPWIRE_LIST_CAP:
                    self.tripwire.outside_at.append(where)
        self.t.d["tripwire"] = self.tripwire.to_tally()

    async def _blank_stored_twins(self) -> int:
        """Plan 5.2 (blanking is always allowed): a readable stored row of a withheld family that is the very JARVIS
        row, under its earlier key, of a row this later backup shows is the withheld message (later_rows) is withheld
        in place. Its key is not this backup's, so no import record names it (the identity repair would withhold it
        at the next import); what was copied from it goes as for a withhold (withhold_batch)."""
        twins, self._blank_twins = self._blank_twins, {}
        if not twins or not self.write:
            return 0
        n = 0
        for row_id, mt in sorted(twins.items()):
            flag = {"note": WITHHELD_BY_ADMIN, "via": mt.via, "at": now().isoformat()}
            done = await self.s.execute(sql(
                "UPDATE archive_messages SET content = '', truncated = false, tool_calls = '[]'::jsonb, labels = '{}', "
                "withheld = true, source_fp = 'w1:' || state, "
                "flags = flags || jsonb_build_object('withheld_by_admin', CAST(:f AS jsonb)) "
                "WHERE id = :i AND NOT withheld"), {"i": row_id, "f": json.dumps(flag)})
            if done.rowcount:
                n += 1
                self.withheld_now.append(row_id)
        if n:
            self.t.bump("stored_rows_withheld_as_their_jarvis_row", n)
        return n

    async def _conversation_label(self, group: str) -> str:
        """Plan 5.6: the name of a withheld conversation in the report: the archive session id of the decision's own
        row's conversation (an id, never a title), or 'decision <n>' while it is not stored here."""
        if group not in self._conversation:
            st = self.ids.by_key.get(group)
            found = await self.s.scalar(select(ArchiveSession.id).where(
                ArchiveSession.source_session_id == st.session)) if st is not None and st.session else None
            self._conversation[group] = (str(found) if found is not None
                                         else "decision %d" % (len(self._conversation) + 1))
        return self._conversation[group]

    async def _plan_count(self, session_id: str, name: str, n: int = 1) -> None:
        groups = self.later_groups(session_id) or self.family_groups(session_id)
        if groups and n:
            later.plan_entry(self.t.d, await self._conversation_label(groups[0]), name, n)

    async def _plan_counts(self, ss: src.SourceSession, plans: list[MessagePlan], rows: dict[str, dict[str, Any]],
                           created: bool) -> None:
        """Plan 5.6 (jarvis-import -Mode Plan, and every run's report): what this later backup does in a withheld
        conversation, counted per conversation (later_backup: conversation -> counts; no text, no titles)."""
        new = [p for p in plans if p.action == "import" and p.m.key not in rows and p.m.key not in self.same_stored
               and p.m.key not in self.moved_keys]
        held = sum(1 for p in new if (mt := self.held.get(p.m.key)) is not None and mt.review == identity.PENDING)
        certain = sum(1 for p in new if (mt := self.held.get(p.m.key)) is not None and mt.review == identity.CERTAIN)
        relisted = sum(1 for k, r in rows.items() if r["withheld"] and (mt := self.held.get(k)) is not None
                       and mt.via == identity.LATER_HOLD and k in self.relations)
        counts = {"rows_held_for_review": held + relisted, "rows_withheld_for_good": certain,
                  "safe_copies": sum(1 for v in self._readable_new.values() if v == "safe copy"),
                  "rows_an_admin_released": sum(1 for v in self._readable_new.values() if v == "released"),
                  "new_conversations": int(created)}
        for name, n in counts.items():
            await self._plan_count(ss.id, name, n)

    async def _release_held_title(self, ss: src.SourceSession, sess: ArchiveSession | None, sess_id: int,
                                  title_c: brain_text.Cleaned, msgs: list[src.SourceMessage]) -> bool:
        """Plan 5.3: a conversation a later backup brought new into a withheld family keeps its title hidden while a
        row of it is withheld by an admin (held for review, or withheld for good); once none is, its title comes back
        from the source (no other rule hides it: the caller checked). Whether it came back now."""
        if not self.write or sess is None or self.held:
            return False
        await self.s.flush()
        still = await self.s.scalar(select(ArchiveMessage.id).where(
            ArchiveMessage.session_id == sess_id, ArchiveMessage.withheld.is_(True),
            ArchiveMessage.flags.has_key("withheld_by_admin")).limit(1))
        if still is not None:
            return False
        sess.title = brain_text.one_line(title_c.text, 300)
        sess.flags = {k: v for k, v in (sess.flags or {}).items() if k not in ("title_withheld", "title_held")}
        await self.s.flush()
        self.t.bump("titles_released")
        return True

    def _reply_quotes(self, ss: src.SourceSession, plans: list[MessagePlan], rows: dict[str, dict[str, Any]],
                      held: dict[str, identity.Match]) -> None:
        """Owner question 8 (a), its default: outside every withheld family, a later backup's reply that quotes a
        withheld text through JARVIS's reply pointer (at least identity.MIN_RELATED characters of it) is held for
        review ('part_of'). The withheld text is in memory only when this backup holds the admin's own row of it."""
        if not self._later_texts:
            return
        for p in plans:
            m = p.m
            if p.action != "import" or m.key in held or m.key in self.moved_keys or m.key in rows:
                continue
            if not (m.content.startswith("[Replying to") or m.content.startswith("[Triggering message id")):
                continue
            for group, text in self._later_texts.items():
                rel = identity.text_relations(m.content, text)
                if identity.PART_OF in rel:
                    held[m.key] = identity.Match(self.ids.by_key[group], identity.LATER_HOLD, identity.PENDING)
                    self.relations[m.key] = tuple(identity.relation_order(rel))
                    break

    def _stored_twin(self, ss: src.SourceSession, m: src.SourceMessage) -> int | None:
        """The stored row of this session that is the very same JARVIS row as m under another key (identity.exact),
        or None."""
        here = identity.exact_of_source(ss.id, m)
        for row_id, stored in self._stored_exact.get((here.row, here.role), ()):
            if identity.same_row(stored, here):
                return row_id
        return None

    # Settle: the decision backup decided once (plan 4) -----------------------------------------------------------

    async def finish_settle(self) -> dict[str, int]:
        """The end of a Settle run (plan 4.3, 4.4): the stale stored rows of each family at its withheld row's role and
        time that this backup no longer holds (a renumbered or deleted copy: rv3-res-7) are held with their evidence;
        every stone of the finished decisions is decided on this backup (decided_on: a decision taken on an older
        backup is finished on this newer one, so an import of this very file replays it); the family links found
        through a uid are recorded; and the backup is settled (settled_run_id = this run). Counts only."""
        from . import review  # imports the task service and the scheduler: not needed by the import itself

        from . import settle as settle_engine

        scope, eng = self.settle_scope, self.settle_engine
        assert scope is not None and eng is not None
        out: Counter[str] = Counter()
        sessions = dict((await self.s.execute(select(ArchiveSession.id, ArchiveSession.source_session_id).where(
            ArchiveSession.source_session_id.in_(sorted(scope.family))))).all())
        for group in scope.groups:
            found = eng.stale_time(group)
            if found is None or found[2] is None or not sessions:
                continue
            role, call, at, _ = found
            for row in (await self.s.scalars(select(ArchiveMessage).where(
                    ArchiveMessage.session_id.in_(sorted(sessions)), ArchiveMessage.role == role,
                    ArchiveMessage.tool_call_id == call, ArchiveMessage.at == at,
                    ArchiveMessage.withheld.is_(False)).order_by(ArchiveMessage.id).with_for_update())).all():
                sid = sessions.get(row.session_id, "")
                if row.source_key in eng.by_key or self.src.backup.has_row(sid, row.source_message_id):
                    continue  # this backup holds it: Settle decided it with the session
                if await identity.exempt_covers(self.s, group, sid, row):
                    continue  # an admin released it, or left it readable
                rel = settle_engine.stored_relations(row.content, eng.w_text(group), stale=True)
                await review.hold_message(self.s, row, anchor=group, actor=self.actor, cause="settle",
                                          rule=identity.SETTLE_HOLD, decided_on=self.backup_sha, relations=rel)
                out["stale_rows_held"] += 1
        groups = sorted(scope.groups)
        rebased = await self.s.execute(update(BrainTombstone).where(
            BrainTombstone.reason == WITHHELD_BY_ADMIN, BrainTombstone.decided_on != self.backup_sha,
            or_(BrainTombstone.source_key.in_(groups), BrainTombstone.anchor_key.in_(groups)))
            .values(decided_on=self.backup_sha))
        out["stones_decided_on_this_backup"] += rebased.rowcount or 0
        await identity.note_decision_backup(self.s, self.backup_sha)
        await self.s.execute(update(BrainDecisionBackup).where(BrainDecisionBackup.sha256 == self.backup_sha)
                             .values(settled_run_id=self.run_id))
        out["decisions_finished"] = len(groups)
        out["certain_rows"] = sum(1 for o in eng.outcomes.values() if o.review == identity.CERTAIN)
        out["rows_held_for_review"] = sum(1 for o in eng.outcomes.values() if o.review == identity.PENDING)
        out["rows_pre_selected_for_release"] = sum(1 for o in eng.outcomes.values() if o.preselect)
        await self.s.flush()
        return dict(out)


    def settle_rows(self, ss: src.SourceSession, msgs: list[src.SourceMessage], plans: list[MessagePlan],
                    rows: dict[str, dict[str, Any]]) -> dict[str, identity.Match]:
        """held_rows() of a session Settle finishes (app/brain/settle.py, plan 4.2-4.4): every stone by exact identity
        first (replay_rows: other decisions' too), then the Settle outcomes of the decisions it finishes: a certain row
        is a member, a related row a hold with its computed evidence; a hold of those decisions already here gets its
        evidence computed again with this backup. A row an admin released or left readable for that decision stays
        as the admin decided. Rule (5): a carried-on copy or an adjacent repeat of a certain row with that row's own
        uid is certain too."""
        held = self.replay_rows(ss, msgs)
        eng = self.settle_engine
        groups = set(self.settle_scope.groups) if self.settle_scope is not None else set()
        by_key = {m.key: m for m in msgs}
        dups = {p.m.key: p for p in plans if p.action == "duplicate" and p.reason in identity.PROVENANCE_REASONS}
        for m in msgs:
            if m.key in self.moved_keys or m.key in dups:
                continue  # the importer's own duplicates follow the row they repeat (below)
            mt = held.get(m.key)
            o = eng.outcomes.get(m.key)
            if o is not None and (mt is None or mt.review == identity.PENDING):
                if self._released_here(o.group, ss, m, rows.get(m.key)):
                    continue  # the admin's release (or 'left readable') of this exact row stands
                held[m.key] = identity.Match(self.ids.by_key[o.group], o.via, o.review)
                if o.review == identity.PENDING:
                    self.relations[m.key] = (o.relations if mt is None or mt.group != o.group
                                             else eng.evidence(m.key, o.group, mt.stone.relations)[0])
                continue
            if mt is not None and mt.review == identity.PENDING and mt.group in groups:
                rel, _ = eng.evidence(m.key, mt.group, mt.stone.relations)
                held[m.key] = identity.Match(mt.stone, identity.SETTLE_HOLD, identity.PENDING)
                self.relations[m.key] = rel
        for key, p in dups.items():  # rule (5); a duplicate with no row of its own keeps the withheld text out
            if key in held or p.duplicate_of not in held:
                continue
            mt, target = held[p.duplicate_of], by_key.get(p.duplicate_of)
            certain = (mt.review == identity.CERTAIN and target is not None and bool(p.m.uid)
                       and p.m.uid == target.uid)
            held[key] = identity.Match(mt.stone, "duplicate", identity.CERTAIN if certain else identity.PENDING)
        return held

    def settle_later_rows(self, ss: src.SourceSession, msgs: list[src.SourceMessage], plans: list[MessagePlan],
                          rows: dict[str, dict[str, Any]]) -> dict[str, identity.Match]:
        """held_rows() of a session Settle finishes whose conversation family also holds a decision taken on another
        backup: for that decision this backup is a later one (plan 5), so the later-backup classifier applies first
        (later_rows: a stored row of the family keeps what it holds and is only ever blanked, 5.2; every other row is
        certain, a safe copy or held for that decision, 5.3), then the Settle outcomes of the decisions this run
        finishes (settle_rows, plan 4.2-4.4) on top. A row either withholds for good is a member; a row either holds
        is a hold (Settle's outcome and evidence first); a row is readable only when both leave it readable. Without
        this, Settle on the newest backup wrote that backup's text into stored rows of a family whose earlier decision
        it does not finish (round-3 replay seed 322: a fold JARVIS made in place, holding the earlier withheld text)."""
        found = self.later_rows(ss, msgs, plans, rows)
        later_rel, moved, released = dict(self.relations), set(self.moved_keys), list(self.auto_release)
        self.relations = {}
        settled = self.settle_rows(ss, msgs, plans, rows)
        settle_rel = self.relations
        self.moved_keys |= moved
        held: dict[str, identity.Match] = {}
        relations: dict[str, tuple[str, ...]] = {}
        for k in sorted(set(found) | set(settled)):
            if k in self.moved_keys:
                continue
            a, b = settled.get(k), found.get(k)
            if a is not None and (b is None or a.review == identity.CERTAIN or b.review != identity.CERTAIN):
                held[k], rel = a, settle_rel.get(k)
            else:
                held[k], rel = b, later_rel.get(k)
            if held[k].review == identity.PENDING and rel is not None:
                relations[k] = rel
        self.relations = relations
        # A row held here is neither readable nor an alias of a readable stored row. The very JARVIS row of a stored row
        # under an earlier key withholds that stored row too (blanking: plan 5.2); a hold is stored under its own key,
        # so that an admin can review it (a hold needs a row to be listed).
        for k, mt in held.items():
            self._readable_new.pop(k, None)
            reason = self._same_reason.get(k)
            if reason == later.SAFE_ALIAS or (reason == later.SAME_ROW and mt.review == identity.PENDING):
                twin = self.same_stored.pop(k, None)
                self._same_reason.pop(k, None)
                if reason == later.SAME_ROW and twin not in self._stored_withheld and twin not in self._blank_twins:
                    self._blank_twins[twin] = mt
            elif reason == later.SAME_ROW and self.same_stored.get(k) not in self._stored_withheld:
                self._blank_twins[self.same_stored[k]] = mt
        seen: set[str] = set()
        self.auto_release = [r for r in released + self.auto_release
                             if r[0] not in held and not (r[0] in seen or seen.add(r[0]))]
        return held

    @staticmethod
    def _links(plans: list[MessagePlan], held: dict[str, identity.Match], by_key: dict[str, src.SourceMessage],
               exempt: Callable[[str, src.SourceMessage], bool] = lambda group, m: False) -> None:
        """R-link, in id order: a carried-on copy or an adjacent repeat the importer linked to a held row, and a twin
        of a held rewound row, are held as that row is. A twin whose uid differs from the rewound row's (Hermes's
        /retry re-sends the text as a new turn with a new uid; a user may also type it again) is a hold, unless an
        admin released it."""
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
            elif p.twin_of in held and p.action == "import" and not exempt(held[p.twin_of].group, m):
                held[m.key] = identity.Match(held[p.twin_of].stone, "same text", identity.PENDING)

    def _bound_row(self, session_id: str, row_id: str) -> src.SourceMessage | None:
        if (session_id, row_id) not in self._bound_rows:
            backup = self.src.backup
            found = backup.rows(session_id, [row_id]) if backup.has_row(session_id, row_id) else []
            self._bound_rows[(session_id, row_id)] = found[0] if found else None
        return self._bound_rows[(session_id, row_id)]

    def anchor_texts(self, group: str, held: dict[str, identity.Match], by_key: dict[str, src.SourceMessage]
                     ) -> tuple[set[tuple[str, str, str]], list[str]]:
        """The texts (and normalised contents) a group's withheld message has in this backup: rows matched as members
        in this run, and the rows the group's certain stones are bound to (their exact JARVIS rows). In memory only."""
        texts = set(self.group_texts.get(group, ()))
        contents = list(self.group_contents.get(group, ()))
        for k, mt in held.items():
            if mt.review == identity.CERTAIN and mt.group == group:
                texts.add(identity.text_key(by_key[k]))
                contents.append(identity.norm(by_key[k].content))
        for st in self.ids.groups.get(group, ()):
            if st.certain and st.row and st.session and st.via not in identity.FOLD_VIAS:
                row = self._bound_row(st.session, st.row)
                if row is not None and identity.same_row(identity.exact_of_stone(st),
                                                         identity.exact_of_source(st.session, row)):
                    texts.add(identity.text_key(row))
                    contents.append(identity.norm(row.content))
        return texts, contents

    def _source_sessions(self) -> dict[str, src.SourceSession]:
        if self._by_session_id is None:
            self._by_session_id = self.src.backup.sessions_by_id()
        return self._by_session_id

    def _collides(self, ss: src.SourceSession, m: src.SourceMessage, row: dict[str, Any] | None, sess_id: int
                  ) -> bool:
        """The stored row under m's key is another JARVIS row that is still in the backup (or in another session); or
        m's own JARVIS row already has a stored row of its own in this session (under its earlier key): the stored row
        of a JARVIS row that left the backup is never rewritten into a second copy of another one (review 2, S5)."""
        if row is None:
            return False
        if row["session_id"] != sess_id:
            return True
        if row.get("adopted_from") or row["source_message_id"] == str(m.id)[:200]:
            return False
        if self.src.backup.has_row(ss.id, row["source_message_id"]):
            return True
        own = self._stored_rows.get((str(m.id)[:200], brain_text.sanitize(m.role)[:40]), ())
        return any(i != row["id"] for i in own)

    async def inherit(self, ss: src.SourceSession, msgs: list[src.SourceMessage], plans: list[MessagePlan],
                      rows: dict[str, dict[str, Any]], sess_id: int) -> None:
        """Every held row's key gets a stone (its group, the rule, the review, the JARVIS row it is now; a hold its
        computed evidence), so later imports, rollbacks and every key reader keep it withheld; never a key that names
        another JARVIS row, and never a hold on a key with no row of its own (an excluded row: nothing an admin could
        review), except the importer's own duplicate of a held row. A hold Settle computed again keeps its stone with
        the new evidence (identity.update_hold)."""
        by_key = {m.key: m for m in msgs}
        stored_plan = {p.m.key for p in plans if p.action == "import"}
        # the importer's own duplicates (a carried-on copy, an adjacent repeat) of a held row keep a stone as that row
        # does (verify: duplicates_of_withheld_rows_without_a_stone), though they have no row of their own
        linked = {p.m.key for p in plans if p.action == "duplicate" and p.reason in identity.PROVENANCE_REASONS}
        for key, mt in self.held.items():
            m = by_key[key]
            if key in self.moved_keys or self._collides(ss, m, rows.get(key), sess_id):
                continue
            if (mt.review == identity.PENDING and key not in stored_plan and key not in rows
                    and not (key in linked and mt.via == "duplicate")):
                continue  # no row to review: a rowless hold would never be listed (nor let an export through)
            group = mt.group
            existing = self.ids.by_key.get(key)
            relations = self.relations.get(key, ())
            if existing is None or (existing.review == identity.PENDING and mt.review == identity.CERTAIN) or (
                    not existing.bound):
                here = identity.exact_of_source(ss.id, m)
                binding = identity.Stone(
                    key=key, anchor="" if key == group else group, session=here.session, row=here.row,
                    role=here.role, tool_call_id=here.tool_call_id, uid=here.uid, uid_known=False,
                    via=mt.via, review=mt.review, bound=True, after_run=mt.stone.after_run, at=m.at,
                    decided_on=self.backup_sha, relations=relations if mt.review == identity.PENDING else ())
                if self.write:
                    await identity.put_stone(self.s, binding, actor=self.actor)
                self.ids.add(binding)
                if existing is None:
                    self.t.bump("stones_added")
            elif (existing.review == identity.PENDING and mt.review == identity.PENDING and key in self.relations
                  and (existing.via, existing.relations) != (mt.via, tuple(identity.relation_order(relations)))):
                if self.write:
                    await identity.update_hold(self.s, key, via=mt.via, relations=relations,
                                               decided_on=self.backup_sha)
                updated = identity.Stone(**{**existing.__dict__, "via": mt.via,
                                            "relations": tuple(identity.relation_order(relations))})
                self.ids._unindex(existing)
                self.ids._index(updated)
            if mt.review == identity.CERTAIN:
                self.group_texts.setdefault(group, set()).add(identity.text_key(m))
                self.group_contents.setdefault(group, []).append(identity.norm(m.content))
            if mt.via != "key" or mt.stone.anchor:
                self.t.held(mt.via, mt.review)

    async def apply_releases(self, ss: src.SourceSession, msgs: list[src.SourceMessage], plans: list[MessagePlan],
                             rows: dict[str, dict[str, Any]], sess_id: int, customer: bool) -> None:
        """A hold left on a JARVIS row an admin released (that exact row, decided on this backup or showing that very
        content: held_rows, replay_rows) goes as review.release_hold() releases it: its holds in that group go, it is
        marked released and its text comes back from the source at this import (unless its conversation is
        withheld). Audited (archive.hold_released, via import). A release never follows another row (plan step 6)."""
        if not self.auto_release:
            return
        by_key = {m.key: m for m in msgs}
        for key, st, reason in self.auto_release:
            m = by_key[key]
            group = st.group
            here = identity.exact_of_source(ss.id, m)
            gone = {x.key: x for x in [self.ids.by_key.get(key), *self.ids.row_stones(here)]
                    if x is not None and x.review == identity.PENDING and x.group == group}
            for x in gone.values():
                self.ids._unindex(x)
            row = rows.get(key)
            if self.write:
                await self.s.execute(delete(BrainTombstone).where(
                    BrainTombstone.reason == WITHHELD_BY_ADMIN, BrainTombstone.review == identity.PENDING,
                    func.coalesce(func.nullif(BrainTombstone.anchor_key, ""), BrainTombstone.source_key) == group,
                    BrainTombstone.source_key.in_(sorted({key, *gone}))))
                await audit.record(self.s, self.actor, "archive.hold_released", "archive_message",
                                   row["id"] if row is not None else "", {
                                       "message": row["id"] if row is not None else None,
                                       "session": sess_id if sess_id > 0 else None, "anchor": group,
                                       "rule": st.via, "via": "import"})
            self.t.bump("holds_released")
            if row is None or not row["withheld"] or row["session_id"] != sess_id:
                continue
            flag = dict((row["flags"] or {}).get("withheld_by_admin") or {})
            flag.update(released_at=now().isoformat(), released_by=reason)
            row["flags"] = {**(row["flags"] or {}), "withheld_by_admin": flag}
            row["source_fp"] = ""  # the release marker: _reconcile restores the text from the source
            if self.write:
                await self.s.execute(update(ArchiveMessage).where(ArchiveMessage.id == row["id"]).values(
                    flags=row["flags"], source_fp=""))
            for i, p in enumerate(plans):
                if p.m.key == key and p.action == "import" and p.cleaned is None:
                    plans[i] = MessagePlan(p.m, "import", cleaned=self.clean_message(p.m, customer),
                                           twin_of=p.twin_of)

    @staticmethod
    def stored_text(row: dict[str, Any]) -> tuple[str, str, str, str]:
        """identity.message_text() of a stored row read as a dict."""
        return (row["role"] or "", row["tool_call_id"] or "", identity.norm(row["content"]),
                json.dumps(row["tool_calls"] or [], sort_keys=True))

    def readable_text(self, m: src.SourceMessage, row: dict[str, Any] | None) -> tuple[str, str, str, str]:
        """What identity.message_text() reads from the stored row this source row is (or would be) stored as while
        readable: its role, call id, normalised cleaned content and cleaned tool calls, compared in memory only (a safe
        copy, plan 5.2)."""
        if row is not None and not row["withheld"] and row.get("source_fp") == m.fp:
            return (row["role"] or "", row["tool_call_id"] or "", identity.norm(row["content"]),
                    json.dumps(row["tool_calls"] or [], sort_keys=True))
        if m.key not in self._readable:
            try:
                c = self.clean_message(m, False)
            except Exception:  # an uncleanable row is never stored with text: nothing a release covers
                return ("", "", "\x00", "")
            self._readable[m.key] = (brain_text.sanitize(m.role)[:40], brain_text.sanitize(m.tool_call_id)[:200],
                                     identity.norm(c.content), json.dumps(c.calls or [], sort_keys=True))
        return self._readable[m.key]

    async def _title_hidden_for(self, ss: src.SourceSession) -> bool:
        """Whether an admin hid this conversation's title: a withhold that hid it is bound to this conversation (it
        comes back after a rollback or a restore), or a continuation or branch ancestor's title was hidden and this
        conversation carries that title (identity.title_follows: Hermes moves the title to a continuation and names a
        branch '<title> #N'). A conversation a later backup brings new into a withheld family keeps its title hidden
        until it is released (do_session; plan 5.3)."""
        if ss.id in self._title_hidden:
            return self._title_hidden[ss.id]
        if self._hidden_titles is None:  # an import never adds such a stone: read once
            self._hidden_titles = {st.session for st in self.ids.by_key.values()
                                   if st.title_hidden and st.certain and st.session}
        hidden = self._hidden_titles
        out = ss.id in hidden
        sessions = self._source_sessions()
        seen, cur = {ss.id}, ss
        while (not out and cur is not None and cur.lineage in identity.LINEAGE and cur.parent_id
               and cur.parent_id not in seen):
            seen.add(cur.parent_id)
            parent = sessions.get(cur.parent_id)
            if parent is None:
                break  # JARVIS deleted it: a new conversation of a withheld family keeps its title hidden (do_session)
            flagged = cur.parent_id in hidden or await self.s.scalar(select(ArchiveSession.id).where(
                ArchiveSession.source_session_id == cur.parent_id[:200],
                ArchiveSession.flags["title_withheld"].astext == "true").limit(1)) is not None
            if flagged and identity.title_follows(ss.title, ss.lineage, parent.title):
                out = True
            cur = parent
        self._title_hidden[ss.id] = out
        return out

    def _first_user_withheld_titled(self, msgs: list[src.SourceMessage]) -> bool:
        """Whether this conversation's first user row is withheld in a withhold whose admin hid its conversation's
        title (title_hidden on the admin's own stone): a member, or a hold whose evidence is its text (identical,
        restated, containing or quoting the withheld text) or that has nothing to compare it with. The title Hermes
        derived from that message, or carried on with it (a rotation moves the title to the continuation), is hidden
        too (review 2, S3): the title only, never another message."""
        first = next((m for m in msgs if m.role == "user" and not self.exclusion(m)), None)
        mt = self.held.get(first.key) if first is not None else None
        if mt is None:
            return False
        if mt.review == identity.PENDING:
            rel = set(self.relations.get(first.key, ()) or mt.stone.relations)
            if not rel & {*identity.TEXT_RELATIONS, identity.NOT_IN_BACKUP}:
                return False
        return any(st.title_hidden and not st.anchor and st.certain for st in self.ids.groups.get(mt.group, ()))

    async def load_stored(self, ss: src.SourceSession, sess_id: int | None) -> None:
        """This session's stored rows by (JARVIS row id, role): their ids (_collides) and exact identities
        (_stored_twin)."""
        self._stored_rows = {}
        self._stored_exact = {}
        self._stored_withheld = set()
        self._stored_by_id = {}
        if sess_id is None:
            return
        for rid, role, mid, call, at, suid, skey, withheld, absorbed in (await self.s.execute(select(
                ArchiveMessage.source_message_id, ArchiveMessage.role, ArchiveMessage.id, ArchiveMessage.tool_call_id,
                ArchiveMessage.at, ArchiveMessage.source_uid, ArchiveMessage.source_key, ArchiveMessage.withheld,
                ArchiveMessage.absorbed_uids).where(ArchiveMessage.session_id == sess_id))).all():
            self._stored_rows.setdefault((rid or "", role or ""), []).append(int(mid))
            if withheld:
                self._stored_withheld.add(int(mid))
            ex = identity.exact(ss.id, rid, role, call, at, suid or identity.parse_key(skey).uid)
            self._stored_exact.setdefault((rid or "", role or ""), []).append((int(mid), ex))
            self._stored_by_id[int(mid)] = (ex, tuple(absorbed or ()))

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
        # A later backup's import never reaches a conversation an admin stopped importing (plan 5.3; the decision
        # backup's replay and Settle still read it): counted, its rows left out, its stored rows kept as they are.
        later_now = (self.settle_scope is None and ss.id[:200] not in self.replay_sessions
                     and bool(self.later_groups(ss.id)))
        if ss.id[:200] in self.frozen and later_now:
            await self.record_links(ss)
            self.record(ss.key, "session", "excluded", later.FROZEN)
            for m in msgs:
                self.record(m.key, "message", "excluded", later.FROZEN)
            self.t.bump("frozen_sessions_skipped")
            await self._plan_count(ss.id, "frozen_conversations_skipped")
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
        # Key adoption (a stored row re-keyed in place) only outside every withheld family: a stored row of a family
        # is never re-keyed, deleted or adopted (0.6.2 plan 5.2, step 6); a row of it under a new key is not stored
        # twice (later_rows: identity.exact).
        root = self._family.get(ss.id[:200], ss.id[:200])
        in_family = root in self._decision_roots or root in self._joined
        if not in_family:
            await self.adopt_rows(ss, msgs, rows, sess)
        self.same_stored = {}
        self._same_reason = {}
        self.relations = {}
        self._later_session = False
        self._tripwire_keys = {}
        await self.load_stored(ss, sess.id if sess is not None else None)
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
        # An admin who withheld the first user message hid the title too (title_withheld): it can paraphrase it. The
        # choice is bound to the withhold, so it follows the conversation's continuations and branches and comes back
        # with a conversation an import creates again.
        # A conversation a later backup brings new into a withheld family keeps its title hidden while a row of it is
        # withheld by an admin (plan 5.3: until it is released; flags.title_held says why it is hidden).
        title_held = (sess is None and later_now) or (sess is not None and bool((sess.flags or {}).get("title_held")))
        title_hidden_other = (sess is not None and bool((sess.flags or {}).get("title_withheld"))
                              and not (sess.flags or {}).get("title_held")) or await self._title_hidden_for(ss)
        title_hidden = title_hidden_other or title_held
        title = "" if withheld or title_hidden else brain_text.one_line(title_c.text, 300)
        flags: dict[str, Any] = {"attribution": ss.attribution.how, "inferred": ["staff", "labels"]}
        if ss.time_assumed_local:
            flags["time_assumed_local"] = True
        if withheld:
            flags["withheld_by_import"] = True
        if title_hidden:
            flags["title_withheld"] = True
        if title_held:
            flags["title_held"] = later.TITLE_HELD
        if title_c.masked and not withheld:
            flags["title_masked"] = True
        if customer_channel:
            flags["customer_channel"] = True  # customer data by default, the session itself included
        if title_c.secret_types:
            flags["title_redactions"] = dict(title_c.secret_types)
        if ss.hermes_counts:
            flags["hermes_counts"] = ss.hermes_counts
        # A conversation's lineage and parent are never forgotten (0.6.2 plan 1.4; migration 0008 keeps them too):
        # a later backup whose Hermes deleted the parent leaves them '' there, never here.
        lineage = ss.lineage or (sess.lineage if sess is not None else "") or ""
        parent_id = ss.parent_id[:200] or (sess.parent_source_session_id if sess is not None else "") or ""
        fields = {"title": title, "started_at": ss.started_at, "ended_at": ss.ended_at, "end_reason": ss.end_reason,
                  "source_message_count": len(msgs), "hermes_archived": ss.hermes_archived, "channel": ss.channel,
                  "lineage": lineage, "parent_source_session_id": parent_id, "staff": staff,
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
        self.current_sess_id = sess_id
        await self.record_links(ss)
        if self.settle_scope is not None and ss.id[:200] in self.settle_scope.family:
            # Settle: the decisions on this backup decided once (plan 4.2-4.4); in a family that also holds a decision
            # taken on another backup, this backup is a later one for that decision (plan 5.2, 5.3): both apply
            if self.later_groups(ss.id):
                self._later_session = True
                await self.load_family_readable(ss)
                self.held = self.settle_later_rows(ss, msgs, plans, rows)
            else:
                self.held = self.settle_rows(ss, msgs, plans, rows)
            self.t.bump("sessions_settled")
        elif ss.id[:200] in self.replay_sessions:
            # A decision was taken on this very backup: replayed word for word, the rules never run (plan 3.2). A
            # family whose admin's decision was taken on another backup (holds of a later backup decided on this one)
            # keeps its stored rows as they are (plan 5.2).
            self._later_session = bool(self.later_groups(ss.id))
            self.held = self.replay_rows(ss, msgs)
            self.t.bump("sessions_replayed")
        elif later_now:
            # A withheld conversation family in a later backup: the later-backup classifier (plan 5.2, 5.3, 5.5)
            self._later_session = True
            await self.load_family_readable(ss)
            self.held = self.later_rows(ss, msgs, plans, rows)
            self.t.bump("sessions_of_withheld_families_in_a_later_backup")
        else:
            # Outside every family (plan 5.4), or a family whose decisions on this very backup are not finished yet
            self.held = self.held_rows(ss, msgs, plans, rows)
            if not self.family_groups(ss.id) and not withheld:
                self._tripwire_outside(plans, rows)
        member_titled = False
        if not title_hidden and not withheld and self._first_user_withheld_titled(msgs):
            # This conversation's first user message is a member of a withhold that hid its conversation's title (a
            # continuation or branch the parent's deletion orphaned: no lineage left to follow): its title hides too.
            title_hidden = member_titled = True
            flags["title_withheld"] = True
            if sess is not None and (sess.title or not (sess.flags or {}).get("title_withheld")):
                source_changed = source_changed or not created
                if self.write:
                    sess.title = ""
                    sess.flags = {**(sess.flags or {}), "title_withheld": True}
                    await self.s.flush()
        await self.apply_releases(ss, msgs, plans, rows, sess_id, staff == "sarah" or customer_channel)
        await self.inherit(ss, msgs, plans, rows, sess_id)
        changed = await self.write_messages(plans, rows, sess_id, withheld, why, ss)
        changed += await self._blank_stored_twins()
        await self._tripwire_locations(sess_id)
        if self._later_session and ss.id[:200] not in self.replay_sessions:
            await self._plan_counts(ss, plans, rows, created)
        if title_held and not title_hidden_other and not withheld and not member_titled:
            if await self._release_held_title(ss, sess, sess_id, title_c, msgs):
                title_hidden = False
                source_changed = source_changed or not created
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
            if title_hidden and before.get("title"):
                before = {**before, "title": ""}  # a hidden title is not kept for a rollback either
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
            if (m.key in self.same_stored and p.action == "import" and row is not None
                    and row["session_id"] != sess_id):
                # its key names a stored row of another conversation, and this very JARVIS row is stored here under
                # an earlier key (a withheld family of a later backup: later_rows): never stored twice
                self.record(m.key, "message", "duplicate", self._same_reason.get(m.key, later.SAME_ROW),
                            "archive_messages", self.same_stored[m.key])
                continue
            if row is not None and row["session_id"] != sess_id:
                self.record(m.key, "message", "excluded", "key collision")
                continue
            if self._collides(ss, m, row, sess_id):
                # A stored row is never overwritten with another JARVIS row's text (a uid that stopped being shared).
                self.record(m.key, "message", "excluded", "key collision: JARVIS gave this key to another row")
                continue
            if m.key in self.moved_keys and (row is None or row.get("adopted_from") or self._later_session):
                # The key of a withheld message now names another row: that row is neither stored under it nor
                # withheld by it (in a later backup's withheld family the stored row under that key stays as it is).
                self.t.bump("key_reassigned")
                self.record(m.key, "message", "excluded",
                            "key collision: JARVIS gave the key of a withheld message to another row")
                continue
            if row is not None:
                changed += await self._reconcile(p, row, withheld, why)
                continue
            if m.key in self.same_stored and p.action == "import":
                # the very JARVIS row of a stored row under its earlier key, or a safe copy of a readable stored row of
                # this conversation (a withheld family of a later backup): never stored twice, never re-keyed (plan
                # 5.2, 5.3)
                self.record(m.key, "message", "duplicate", self._same_reason.get(m.key, later.SAME_ROW),
                            "archive_messages", self.same_stored[m.key])
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
        if released and self._later_session and not self._release_covers(p.m):
            released = False  # a release covers the content it was decided on, never a later backup's (plan 1.2)
        if (self._later_session and not session_withheld and not released and held is not None
                and not row["withheld"] and (p.action in ("excluded", "duplicate") or not same or old_key)):
            # A readable stored row of a withheld family that a later backup shows is the withheld message (or holds
            # for review): blanked in place, the only change a later backup makes to a stored row (plan 5.2). It is
            # never deleted, re-keyed or given the backup's other fields.
            # (its own state: the stored row keeps no fingerprint of its text, and is not given the backup's state)
            await self._update_row(row, {**held_now, "source_fp": "w1:%s" % (row["state"] or "live")})
            self.record(m.key, "message", "updated", "withheld: an admin withheld it" if held.via == "key"
                        else "withheld: the same message as one an admin withheld (%s)" % held.via
                        if held.review != identity.PENDING else "withheld: held for an admin's review (%s)" % held.via,
                        "archive_messages", row["id"], blank_before(jsonable({k: row[k] for k in MESSAGE_FIELDS})))
            self.withheld_now.append(row["id"])
            return 1
        if (self._later_session and not session_withheld and (not released or p.action in ("excluded", "duplicate"))
                and (held is None or row["withheld"]) and (p.action in ("excluded", "duplicate") or not same or old_key)):
            # A stored row of a withheld conversation family is never rewritten, re-keyed or deleted by a later
            # backup: it keeps the text it had or stays without it (0.6.2 plan 5.2; JARVIS's edits there are not
            # followed: owner question 7). Blanking it (a withhold, a restriction) is always allowed.
            self.record(m.key, "message", "duplicate", "kept as stored: a withheld conversation's rows never change",
                        "archive_messages", row["id"])
            return 0
        if same and not old_key and not released:
            # Withheld now: the rollback copy keeps no text either (it is never stored to be scrubbed later).
            if withheld and not row["withheld"]:
                await self._update_row(row, {**restricted_now, "source_uid": m.uid[:200],
                                             "absorbed_uids": [u[:200] for u in m.absorbed]})
                self.record(m.key, "message", "updated", "withheld: " + why, "archive_messages", row["id"],
                            blank_before(jsonable({k: row[k] for k in MESSAGE_FIELDS})))
                self.withheld_now.append(row["id"])
                return 1
            if held is not None and not row["withheld"]:
                # The same message as one an admin withheld, and its row still has its text: an import that ran during
                # the withhold, a withhold decisions-apply could not make, or a copy found since. The conversation
                # stays as it is.
                await self._update_row(row, {**held_now, "source_uid": m.uid[:200],
                                             "absorbed_uids": [u[:200] for u in m.absorbed]})
                self.record(m.key, "message", "updated", "withheld: an admin withheld it" if held.via == "key"
                            else "withheld: the same message as one an admin withheld (%s)" % held.via,
                            "archive_messages", row["id"], blank_before(jsonable({k: row[k] for k in MESSAGE_FIELDS})))
                self.withheld_now.append(row["id"])
                return 1
            absorbed = [u[:200] for u in m.absorbed]
            if (row.get("source_uid") or "") != m.uid[:200] or list(row.get("absorbed_uids") or []) != absorbed:
                self._uid_backfill.append((int(row["id"]), m.uid[:200], absorbed))
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
        values["absorbed_uids"] = [u[:200] for u in m.absorbed]
        if old_key:  # the JARVIS row as it is now (replace_messages renumbers it)
            values.update(source_key=m.key, source_message_id=str(m.id)[:200], position=m.position)
        ok, result = await self.guarded(lambda: self._update_row(row, values))
        if not ok:
            self.record(m.key, "message", "excluded", "invalid record: %s" % result)
            return 0
        if "state" in values:
            self._count_message(p, withheld and not released)
        before = jsonable({k: row[k] for k in MESSAGE_FIELDS})
        # the uids a fold absorbed go back with the text: a rollback that kept a later fold's absorbed uids on the row
        # it restored would make that row the withheld message by provenance (round-3 replay seed 322)
        before["absorbed_uids"] = list(row.get("absorbed_uids") or [])
        if old_key:  # a rollback puts the old key back
            before.update(source_key=old_key, source_uid=row.get("source_uid") or "")
            self.t.bump("keys_adopted")
        if values.get("withheld") and not row["withheld"]:  # withheld now (its session, or the same message)
            before = blank_before(before)
            self.withheld_now.append(row["id"])
        self.record(m.key, "message", "updated", reason, "archive_messages", row["id"], before)
        return 1

    def _release_covers(self, m: src.SourceMessage) -> bool:
        """Whether an admin's release of this JARVIS row (any group) covers the content this backup holds: decided on
        this very backup (identity.Identities.released)."""
        here = identity.exact_of_source(self._sid, m)
        return any(self.ids.released(g, here, backup=self.backup_sha)
                   for g, sid, rid in self.ids.exempt if (sid, rid) == (here.session, here.row))

    async def _backfill_uids(self) -> None:
        """The JARVIS uid, and the uids a fold absorbed, of rows this import left as they were (identity metadata,
        not a change: no record)."""
        pending, self._uid_backfill = self._uid_backfill, []
        if not pending or not self.write:
            return
        for i in range(0, len(pending), 1000):
            chunk = pending[i:i + 1000]
            params: dict[str, Any] = {}
            values = []
            for n, (row_id, uid, absorbed) in enumerate(chunk):
                params["i%d" % n], params["u%d" % n], params["a%d" % n] = row_id, uid, list(absorbed)
                values.append("(CAST(:i%d AS bigint), CAST(:u%d AS text), CAST(:a%d AS text[]))" % (n, n, n))
            await self.s.execute(sql("UPDATE archive_messages AS a SET source_uid = v.u, absorbed_uids = v.a "
                                     "FROM (VALUES %s) AS v(id, u, a) WHERE a.id = v.id" % ", ".join(values)), params)

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
        await refuse_legacy(s)
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
              heartbeat_seconds: float = HEARTBEAT_SECONDS, decisions: str | None = None) -> dict[str, Any]:
    """Import the selection. Refusals (Refused) come before any change; a failure or an interruption raises
    ImportFailed after the run's row says what happened.

    With decisions (a version 5 decisions file from the test copy; actor: an active admin), its withholds, releases
    and purges are applied first, under the import's lock, then the import runs (it replays the decisions taken on
    this very backup), then the file's review decisions on imported memory items apply to this run (0.6.2 plan
    3.3). A file of another backup or importer version is applied all the same, with a warning: those conversations
    are a later backup here (fail-safe)."""
    selection.validate()
    data = read_decisions(decisions) if decisions else None
    async with sessionmaker()() as s:
        await require_schema(s)
        await refuse_legacy(s)
        await purge_key_check(s)
        tz_name, tz = await customer_timezone(s)
        decider: _Decider | None = None
        if data is not None:
            user = await s.scalar(select(User).where(func.lower(User.email) == (actor or "").lower(),
                                                     User.role == "admin", User.is_active.is_(True)))
            if user is None:
                raise Refused("--actor must be the e-mail address of an active admin: decisions that need an admin "
                              "keep needing one")
            decider = _Decider(user.email, bool(user.can_access_sensitive))
            _check_v5(data)
    sources = Sources(paths, tz, tz_name)
    try:
        source = sources.source_info(pre_backup_sha256)
        locks = await LockConnection.open()
        try:
            if not await locks.try_lock(GLOBAL_LOCK):
                raise Refused(LOCK_BUSY)
            async with sessionmaker()() as s:
                await mark_dead_runs(s)
            applied: dict[str, Any] = {}
            warnings: list[str] = []
            if data is not None and decider is not None:
                warnings = decisions_warnings(data, sources.db_info["sha256"])
                async with sessionmaker()() as s:
                    applied = await decisions_before_import(s, data, decider)
                    await audit.record(s, decider.email, "brain.decisions_applied", "brain", "", {
                        "via": "import", "before_import": applied, "warnings": len(warnings),
                        "same_backup": not any("SHA-256" in w for w in warnings)})
                    await s.commit()
            scope = {"selection": selection.to_json(), "timezone": tz_name}
            run_id = await locks.start_run(label=brain_text.one_line(label, 200), actor=actor, scope=scope,
                                           source=source)
            locks.start_heartbeat(run_id, heartbeat_seconds)
            result = await _execute(run_id, sources, selection, {}, actor, report_file, sources.discovery,
                                    resumed=False)
            if data is not None and decider is not None:
                result["decisions"] = {"before_import": applied, "warnings": warnings,
                                       **await _item_decisions(data, run_id, decider)}
            return result
        finally:
            await locks.close()
    finally:
        sources.close()


async def _item_decisions(data: dict[str, Any], run_id: int, decider: _Decider) -> dict[str, Any]:
    """After an import with -Decisions: the file's review decisions on imported memory items, applied to this run
    where the item is the very same (decisions-apply's rule). The caller holds the import lock."""
    listed: dict[str, list[int]] = {}
    applied = 0
    async with sessionmaker()() as s:
        user = await s.scalar(select(User).where(User.email == decider.email))
        for n, d in enumerate(data.get("decisions") or [] if isinstance(data.get("decisions"), list) else [], 1):
            reason = await _apply_decision(s, d, run_id, user, True)
            if reason:
                listed.setdefault(reason, []).append(n)
            else:
                applied += 1
        await audit.record(s, decider.email, "brain.decisions_applied", "import_run", run_id,
                           {"run": run_id, "applied": applied, "via": "import",
                            "not_applied": {k: len(v) for k, v in listed.items()}})
        await s.commit()
    return {"items_applied": applied, "items_not_applied": listed}


# --- Settle: finishing decisions on the decision backup (0.6.2 plan 4) ---------------------------------------------


async def settle_scope(s: AsyncSession, sources: Sources) -> SettleScope:
    """The decisions a Settle of this backup finishes (plan 4.1): every admin's own withhold whose decision backup is
    not finished yet and whose conversation family's newest import (not rolled back) is of this very backup (its
    SHA-256): Settle runs on the newest backup that wrote rows of the family. Their families in this backup: the
    sessions linked to theirs (session_families), and the sessions with a row that carries or absorbs one of their
    certain uids (plan 5.1). Decisions whose family a newer backup wrote wait for that one."""
    sha = identity.run_sha256({"db": sources.db_info})
    settled = set((await s.scalars(select(BrainDecisionBackup.sha256).where(
        BrainDecisionBackup.settled_run_id.is_not(None)))).all())
    labels = dict((await s.execute(select(BrainDecisionBackup.sha256, BrainDecisionBackup.label))).all())
    groups: list[str] = []
    waiting: Counter[str] = Counter()
    bound: dict[str, list[str]] = {}
    for st in (await s.scalars(select(BrainTombstone).where(
            BrainTombstone.reason == WITHHELD_BY_ADMIN, BrainTombstone.anchor_key == "",
            BrainTombstone.review != identity.PENDING).order_by(BrainTombstone.source_key))).all():
        if st.decided_on and st.decided_on in settled:
            continue
        sids = sorted(set((await s.scalars(select(BrainTombstone.source_session_id).where(
            BrainTombstone.reason == WITHHELD_BY_ADMIN, BrainTombstone.source_session_id != "",
            or_(BrainTombstone.source_key == st.source_key, BrainTombstone.anchor_key == st.source_key)))).all()))
        newest = await identity.decision_backup(s, sids) if sids else ""
        if newest and newest == sha:
            groups.append(st.source_key)
            bound[st.source_key] = sids
        else:
            on = newest or st.decided_on
            waiting[(labels.get(on) or on[:12]) if on else "no backup known"] += 1
    if not groups:
        return SettleScope([], set(), dict(waiting))
    fam = await session_families(s, sources.backup)
    roots_of: dict[str, set[str]] = {g: {fam.get(sid, sid) for sid in bound[g]} for g in groups}
    for g in groups:  # the sessions with a row that carries or absorbs one of the decision's certain uids
        uids = set((await s.scalars(select(BrainTombstone.message_uid).where(
            BrainTombstone.reason == WITHHELD_BY_ADMIN, BrainTombstone.review != identity.PENDING,
            BrainTombstone.message_uid != "", BrainTombstone.via.not_in(identity.FOLD_VIAS),
            or_(BrainTombstone.source_key == g, BrainTombstone.anchor_key == g)))).all())
        roots_of[g] |= {fam.get(sid[:200], sid[:200]) for sid in sources.backup.sessions_with_uids(uids)}
    # a continuation or branch whose link is lost, whose copied history holds a row at a stone's role, call and time
    ids = await identity.Identities.load(s)
    ids = identity.Identities([st for st in ids.by_key.values() if st.group in set(groups)])
    decision_roots: dict[str, list[str]] = {}
    for g in groups:
        for root in roots_of[g]:
            decision_roots.setdefault(root, []).append(g)
    for root, joined in (await joined_families(s, sources.backup, ids, fam, decision_roots)).items():
        for g in joined:
            if g in roots_of:
                roots_of[g].add(root)
    families = {g: sorted(ss.id[:200] for ss in sources.backup.sessions()
                          if fam.get(ss.id[:200], ss.id[:200]) in roots_of[g]) for g in groups}
    family = {sid for sids in families.values() for sid in sids}
    return SettleScope(groups, family, dict(waiting), families)


def settle_work(sources: Sources, scope: SettleScope) -> Work:
    """A Settle run's work: every session of the families, in the backup's order; no file, job or orphan."""
    sessions = [ss for ss in sources.backup.sessions() if ss.id[:200] in scope.family]
    counts = sources.backup.message_counts()
    work = Work(sessions=sessions, files=[], jobs=False, orphans=False)
    work.totals = {"sessions": len(sessions), "messages": sum(counts.get(ss.id, 0) for ss in sessions), "orphans": 0,
                   "files": 0, "jobs": 0, "urls": 0}
    return work


def _scope_of_run(run_scope: dict[str, Any]) -> SettleScope | None:
    """A Settle run's recorded scope (import_runs.scope -> settle), for resume and verify."""
    found = (run_scope or {}).get("settle")
    if not isinstance(found, dict):
        return None
    families = found.get("families") if isinstance(found.get("families"), dict) else {}
    return SettleScope([str(g) for g in found.get("groups") or []], {str(x) for x in found.get("sessions") or []},
                       families={str(g): [str(x) for x in v or []] for g, v in families.items()})


async def settle(paths: SourcePaths, *, actor: str = DEFAULT_ACTOR, report_file: str | None = None,
                 heartbeat_seconds: float = HEARTBEAT_SECONDS) -> dict[str, Any]:
    """Finish the decisions taken on this JARVIS backup (brain-import settle --backup FILE, jarvis-import -Mode Settle;
    0.6.2 plan 4): an import run of every session of their conversation families (also sessions a -Sample run
    skipped), with each withheld text in memory, that withholds for good only what provenance says is the withheld
    message (settle.Engine, plan 4.2) and holds every other related row for review with its computed evidence (4.3,
    4.6). Its stones and holds are decided on this backup (decided_on), and the backup is settled
    (brain_decision_backups.settled_run_id): an import of this very file replays the result word for word, here and
    on live with the exported decisions file (C1). Only the conversations are read (no profile folder needed).
    Refusals (Refused) come before any change; with nothing left to finish it changes nothing."""
    async with sessionmaker()() as s:
        await require_schema(s)
        await refuse_legacy(s)
        await purge_key_check(s)
        tz_name, tz = await customer_timezone(s)
    sources = Sources(paths, tz, tz_name, files=False)
    try:
        source = sources.source_info()
        locks = await LockConnection.open()
        try:
            if not await locks.try_lock(GLOBAL_LOCK):
                raise Refused(LOCK_BUSY)
            async with sessionmaker()() as s:
                await mark_dead_runs(s)
                scope = await settle_scope(s, sources)
                await s.commit()
            if not scope.groups:
                if scope.waiting:
                    raise Refused("no decision waits on this JARVIS backup; %d decision(s) wait on another one (%s): "
                                  "finish them with the newest backup that wrote their conversations" % (
                                      sum(scope.waiting.values()),
                                      ", ".join("%s: %d" % kv for kv in sorted(scope.waiting.items()))))
                return {"run_id": None, "status": "nothing to finish", "decisions": 0, "waiting": {}}
            run_scope = {"selection": Selection(all=True).to_json(), "timezone": tz_name,
                         "settle": {"groups": scope.groups, "sessions": sorted(scope.family),
                                    "families": scope.families}}
            run_id = await locks.start_run(label="settle (finishing decisions)", actor=actor, scope=run_scope,
                                           source=source)
            locks.start_heartbeat(run_id, heartbeat_seconds)
            result = await _execute(run_id, sources, Selection(all=True), {}, actor, report_file, sources.discovery,
                                    resumed=False, settle=scope)
            result.update(decisions=len(scope.groups), waiting=scope.waiting)
            return result
        finally:
            await locks.close()
    finally:
        sources.close()


async def resume(run_id: int, paths: SourcePaths, *, report_file: str | None = None, actor: str = DEFAULT_ACTOR,
                 heartbeat_seconds: float = HEARTBEAT_SECONDS) -> dict[str, Any]:
    """Continue an interrupted or failed run after its checkpoint, from the very same sources."""
    async with sessionmaker()() as s:
        await require_schema(s)
        await refuse_legacy(s)
        await purge_key_check(s)
        found = await s.get(ImportRun, run_id)
        if found is None:
            raise Refused("there is no import run %d" % run_id)
        tz_name = (found.scope or {}).get("timezone") or "UTC"
        settle_run = _scope_of_run(found.scope or {})
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz_name, tz = "UTC", ZoneInfo("UTC")
    sources = Sources(paths, tz, tz_name, files=settle_run is None)
    try:
        locks = await LockConnection.open()
        try:
            if not await locks.try_lock(GLOBAL_LOCK):
                raise Refused(LOCK_BUSY)
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
            return await _execute(run_id, sources, selection, checkpoint, actor, report_file, discovery, resumed=True,
                                  settle=settle_run)
        finally:
            await locks.close()
    finally:
        sources.close()


async def _execute(run_id: int, sources: Sources, selection: Selection, checkpoint: dict[str, Any], actor: str,
                   report_file: str | None, discovery: dict[str, Any], *, resumed: bool,
                   settle: SettleScope | None = None) -> dict[str, Any]:
    tally = Tally(checkpoint.get("tally"))
    work = settle_work(sources, settle) if settle is not None else select_work(sources, selection)
    async with sessionmaker()() as s:
        proc = Processor(s, sources, run_id=run_id, tally=tally, write=True, actor=actor, settle_scope=settle)
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
            tally.d["tripwire"] = proc.tripwire.to_tally()
            if proc.tripwire.texts or proc.tripwire.inside or proc.tripwire.outside:
                # plan 5.5: the newest run's tripwire, for admins (counts and archive ids only)
                await later.note_tripwire(s, run_id, tally.d["tripwire"])
            if settle is not None:
                tally.d["settle"] = await proc.finish_settle()
                await audit.record(s, actor, "brain.decisions_settled", "import_run", run_id, {
                    "run": run_id, "decisions": len(settle.groups), "sessions": len(settle.family),
                    **tally.d["settle"]})
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
              "source_uid", "absorbed_uids"}
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
        settle_run = _scope_of_run(run_row.scope or {})
        recorded_source = dict(run_row.source or {})
        report = dict(run_row.report or {})
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    sources = Sources(paths, tz, tz_name, files=settle_run is None)
    try:
        if not sources.same_source(recorded_source):
            raise Refused("the backup or the restored profile differs from the one run %d used: verify needs the "
                          "very same sources" % run_id)
        work = settle_work(sources, settle_run) if settle_run is not None else select_work(sources, selection)
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
            checks.update(await _identity_checks(s, run_id, sources, work, settle_run))
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


async def _identity_checks(s: AsyncSession, run_id: int, sources: Sources, work: Work,
                           settle: SettleScope | None = None) -> dict[str, dict[str, Any]]:
    """Checks 9-12 (spec 2.9): nothing that is the same message as a withheld one is readable, no withheld row or
    rollback copy keeps a fingerprint of its text, purged items stay purged, and the database guard never had to
    remove text the import wrote."""
    checks: dict[str, dict[str, Any]] = {}
    pending = dict((await s.execute(sql("SELECT brain_withhold_pending()"))).scalar() or {})
    readable = {k: int(pending.get(k) or 0) for k in
                ("readable_by_key", "readable_by_jarvis_row", "readable_by_fold", "readable_in_restricted_sessions")}
    # The rules again, from the backup, with the stones as they are now (read-only): every row that is the same
    # message as a withheld one (or held for review) is stored without text.
    proc = Processor(s, sources, run_id=None, tally=Tally(), write=False, settle_scope=settle)
    proc.ids = await identity.Identities.load(s)
    await proc.prepare_identity()
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
        proc.current_sess_id = sess_id
        await proc.load_stored(ss, sess_id)
        if settle is not None and ss.id[:200] in settle.family and proc.later_groups(ss.id):
            await proc.load_family_readable(ss)
            proc._later_session = True
            held = proc.settle_later_rows(ss, msgs, plans, rows)
        elif settle is not None and ss.id[:200] in settle.family:
            held = proc.settle_rows(ss, msgs, plans, rows)
        elif ss.id[:200] in proc.replay_sessions:
            held = proc.replay_rows(ss, msgs)
        elif settle is None and proc.later_groups(ss.id):
            await proc.load_family_readable(ss)
            proc._later_session = True
            held = proc.later_rows(ss, msgs, plans, rows)
        else:
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
    # Plan 5.5: inside a withheld family no readable row this run wrote is, or contains, a withheld text the backup
    # holds; the tripwire held each one it found instead, and a run that found one fails here (the classifier missed
    # it: an admin looks). Outside every family such rows are only listed.
    tw = later.Tripwire.from_tally(((await s.scalar(select(ImportRun.report).where(ImportRun.id == run_id))) or {})
                                   .get("tripwire"))
    checks["tripwire_quiet_in_withheld_families"] = {
        "ok": tw.inside == 0, "rows_held_by_the_tripwire": tw.inside, "where": tw.inside_at[:50],
        "rows_listed_outside_withheld_families": tw.outside}
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
        raise Refused(LOCK_BUSY)
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
    # A Settle run rolled back: the decisions it finished are not finished any more (its stones stay: decisions).
    await session.execute(update(BrainDecisionBackup).where(BrainDecisionBackup.settled_run_id == run_id)
                          .values(settled_run_id=None))

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
                           values.get("source_uid") or identity.parse_key(values.get("source_key")).uid,
                           tool_call_id=values.get("tool_call_id") or "", at=identity.when(values.get("at")))
        if mt is not None and mt.via == "same message id" and values.get("source_key") not in ids.by_key:
            hold = identity.Stone(
                key=values["source_key"], anchor=mt.group, session=jarvis_sessions[sid],
                row=str(values.get("source_message_id") or ""), role=values.get("role") or "",
                tool_call_id=values.get("tool_call_id") or "", uid=values.get("source_uid") or "",
                via=mt.via, review=mt.review, bound=True, after_run=mt.stone.after_run,
                at=identity.when(values.get("at")))
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
                elif (mt := await same_message(row)) is not None and not row.get("withheld"):
                    row.update(content="", tool_calls=[], labels=[], withheld=True, truncated=False,
                               flags={**(row.get("flags") or {}), "withheld_by_admin": Processor._held_flag(mt)})
                elif mt is not None and mt.review == identity.PENDING and not (
                        (row.get("flags") or {}).get("withheld_by_admin")):
                    # Its rollback copy already lost the text (a withhold or the start-up sweep blanked a row that
                    # only shares a withheld message's uid): it comes back as the hold it is (same_message gave its
                    # key a pending stone), listed and releasable, never an unlisted withheld row.
                    row["flags"] = {**(row.get("flags") or {}), "withheld_by_admin": Processor._held_flag(mt)}
                if row.get("withheld"):
                    no_fingerprint(row, None)
                    if row.get("sensitivity") == "restricted" and "withheld" not in (row.get("flags") or {}):
                        # A rollback copy a withhold, the identity repair or the start-up sweep blanked (0.6.2 builds
                        # before review 2 marked it 'restricted', archive.WITHHELD_ROW): one message withheld, not
                        # restricted data in its conversation. It takes the conversation's level, so recompute_session
                        # never makes the whole conversation restricted (review 2, S13). A conversation withheld for
                        # restricted data (withheld_later) keeps 'restricted'; its rows carry flags.withheld.
                        here = (await session.execute(select(ArchiveSession.flags, ArchiveSession.sensitivity).where(
                            ArchiveSession.id == int(row["session_id"])))).first()
                        if here is not None and not withheld_later(here.flags):
                            row["sensitivity"] = (here.sensitivity if here.sensitivity in ("general", "customer")
                                                  else "general")
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
            if (flags or {}).get("title_withheld"):  # an admin hid the title after this run: it stays hidden
                values.update(title="", flags={**(values.get("flags") or {}), "title_withheld": True})
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
                purge_fingerprints_made_with_another_key=int(pending["purge_fingerprints_other_key"]),
                # 0.6.2 plan 5.3, 5.5: conversations an admin stopped importing, and the newest run's tripwire (counts
                # and archive ids only)
                # 0.6.2 plan 1.8, 9.4: withholds not finished on their backup (Settle), Settle's holds waiting for review,
                # and rows a later backup held in a withheld conversation family
                unsettled_decisions=int(pending.get("unsettled_decisions") or 0),
                unreviewed_settle_holds=int(pending.get("unreviewed_settle_holds") or 0),
                later_backup_holds=int(pending.get("quarantined_rows") or 0),
                frozen_conversations=int(pending.get("frozen_sessions") or 0),
                last_tripwire=pending.get("last_tripwire") or None,
                # 0.6.2 plan 8.1: legacy 0.6.0/0.6.1 data, locked until brain-import legacy-reset
                legacy_lock=bool(pending.get("legacy_lock")),
                legacy_runs=[r.id for r in await legacy_runs(s)] if pending.get("legacy_lock") else [])
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
            raise Refused(LOCK_BUSY)
        counts = await identity.repair(s, actor=actor, cause="cli")
        await s.commit()
    return {"applied": True, "repair": counts}


async def forget_fingerprints(*, apply: bool, actor: str = DEFAULT_ACTOR) -> dict[str, Any]:
    """Purge fingerprints made with another database password, deleted (with apply; audited). The purges stay, by
    their keys. Refused while an import, a rollback or a rescan runs."""
    async with sessionmaker()() as s:
        await require_schema(s)
        if apply and not await try_xact_lock(s, GLOBAL_LOCK):
            raise Refused(LOCK_BUSY)
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
            raise Refused(LOCK_BUSY)
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
    settled = await identity.finish_repair(s, actor=actor, cause="rescan")  # the rows the identity repair withheld
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
# 5 (0.6.2): every stone and release word for word, with its exact JARVIS row and the backup it was decided on, plus
# the decision backups, the family links and the frozen conversations; an import of that backup with the file
# (run --decisions, jarvis-import -Mode Import -Decisions) replays it. 4 (0.6.2 development builds) and 3 (0.6.0,
# 0.6.1) are still read, after an import (decisions-apply --run).
DECISIONS_VERSION = 5
RESTORE_ACTOR = "system (restore)"  # decisions-apply --removals-only: restore.ps1 applies the removals again
RESTORE_NOTE = "decision applied again after a restore"
# What a withhold carries in a version 5 file: its key and exact binding (spec 2.1; at_row: the time of the JARVIS row
# it names, a time, never text), never text or a fingerprint; who decided and when (by, at: a restore puts them back);
# the JARVIS backup it was decided on (decided_on, its SHA-256); kept_by, kept_at: who kept a hold and when ('' unless
# review is 'kept'; S23, rv3-dur-6).
WITHHOLD_FIELDS = ("source_key", "reason", "anchor", "session", "row", "role", "tool_call_id", "uid", "uid_known",
                   "via", "review", "title_hidden", "by", "at", "at_row", "decided_on", "kept_by", "kept_at",
                   "relations")
# What a release (or identical text left readable) carries: its group and exact JARVIS row, the backup it was decided
# on, its kind, and who decided it, when and why.
RELEASE_FIELDS = ("anchor", "session", "row", "role", "tool_call_id", "at_row", "uid", "kind", "decided_on", "reason",
                  "by", "at")
PURGE_FIELDS = ("source_key", "reason", "by", "at")
# Said in every decisions file (review 2, S17): what in it is derived from text, and how to keep it.
DECISIONS_HANDLING = ("Keys and ids only, never text. A purged memory entry's key (#entry-...) carries a 64-bit digest "
                      "of the entry's text, as every memory entry key does since 0.6.0: it confirms a guess of the "
                      "entry offline. Keep this file like a backup and delete it once it is applied.")


async def _decisions_content(s: AsyncSession, *, removals_only: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """What a decisions file holds (decisions_export's docstring says what) and its counts, read in the caller's
    transaction: decisions_export, and the legacy reset (legacy_reset), which writes it before its rollbacks."""
    decisions, skipped, not_exported = [], 0, 0
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
    for t in (await s.scalars(select(BrainTombstone).where(BrainTombstone.reason.in_((
            WITHHELD_BY_ADMIN, PURGED_BY_ADMIN))).order_by(BrainTombstone.source_key))).all():
        who = {"by": t.purged_by or "", "at": _iso(t.purged_at)}  # who decided, and when: ids and times only
        if t.reason == PURGED_BY_ADMIN:
            removals.append({"source_key": t.source_key, "reason": t.reason, **who})
            continue
        # title_hidden: whether the admin's own withhold hid the conversation's title (False on other stones)
        removals.append({"source_key": t.source_key, "reason": t.reason, "anchor": t.anchor_key or "",
                         "session": t.source_session_id or "", "row": t.source_row_id or "", "role": t.role or "",
                         "tool_call_id": t.tool_call_id or "", "uid": t.message_uid or "",
                         "uid_known": bool(t.uid_known), "via": t.via or "", "review": t.review or "",
                         "title_hidden": bool(t.title_hidden), "at_row": _iso(t.source_at),
                         "decided_on": t.decided_on or "", "kept_by": "", "kept_at": "",
                         "relations": identity.relation_order(t.relations or []), **who})
        if t.review == identity.KEPT:
            # who kept the hold, and when: the stone says it (rv3-dur-6); a keep written before that, the row's flag
            flag: Any = {}
            if not t.kept_by:
                flag = await s.scalar(select(ArchiveMessage.flags["withheld_by_admin"]).where(
                    ArchiveMessage.source_key == t.source_key).limit(1))
                flag = flag if isinstance(flag, dict) else {}
            removals[-1].update(kept_by=(t.kept_by or str(flag.get("kept_by") or ""))[:200],
                                kept_at=(_iso(t.kept_at) or str(flag.get("kept_at") or ""))[:64])
    purged_items: list[dict[str, Any]] = []
    fingerprints: list[dict[str, Any]] = []
    redactions: list[dict[str, Any]] = []
    if removals_only:
        purged_items = [{"item": p.item_id, "created_at": _iso(p.item_created_at), "by": p.purged_by or "",
                         "at": _iso(p.purged_at)} for p in (await s.scalars(select(BrainPurgedItem).order_by(
                             BrainPurgedItem.item_id, BrainPurgedItem.item_created_at))).all()]
        fingerprints = [{"fp": p.fp, "source_key": p.source_key or "", "kind": p.kind or "", "by": p.purged_by or "",
                         "at": _iso(p.purged_at), "via": p.via or ""} for p in (await s.scalars(
                             select(BrainPurgedContent).where(identity.live_fingerprints())
                             .order_by(BrainPurgedContent.fp))).all()]
        redactions = await _redactions(s)
    # A release travels with the exact JARVIS row it covers (identity.exact; never a digest or a length of any
    # text), the backup it was decided on, its kind, and who decided it, when and why (a release, or identical
    # text an admin left readable: review 2, S21, S23).
    releases = [{"anchor": e.anchor_key, "session": e.source_session_id, "row": e.source_row_id,
                 "role": e.role or "", "tool_call_id": e.tool_call_id or "", "at_row": _iso(e.source_at),
                 "uid": e.source_uid or "", "kind": e.kind or "release", "decided_on": e.decided_on or "",
                 "reason": e.reason or "", "by": e.decided_by or "", "at": _iso(e.decided_at)}
                for e in (await s.scalars(select(BrainWithholdExemption).order_by(
                    BrainWithholdExemption.anchor_key, BrainWithholdExemption.source_session_id,
                    BrainWithholdExemption.source_row_id, BrainWithholdExemption.id))).all()]
    # The JARVIS backups the decisions were taken on (an import of one of them replays its decisions), the
    # family links every import recorded, and the conversations an admin stopped importing.
    backups = [{"sha256": b.sha256, "label": b.label or "", "importer_version": b.importer_version or "",
                "newest_at": _iso(b.newest_at), "settled": b.settled_run_id is not None}
               for b in (await s.scalars(select(BrainDecisionBackup).order_by(BrainDecisionBackup.sha256))).all()]
    links = [{"a": a, "b": b, "how": how} for a, b, how in (await s.execute(select(
        BrainSessionLink.a, BrainSessionLink.b, BrainSessionLink.how).order_by(
        BrainSessionLink.a, BrainSessionLink.b, BrainSessionLink.how))).all()]
    frozen = [{"session": f.source_session_id, "by": f.frozen_by or "", "at": _iso(f.frozen_at)}
              for f in (await s.scalars(select(BrainFrozenSession).order_by(
                  BrainFrozenSession.source_session_id))).all()]
    content: dict[str, Any] = {"format": "jarvis-brain-decisions", "version": DECISIONS_VERSION,
                               "exported_at": now().isoformat(), "importer_version": IMPORTER_VERSION,
                               "handling": DECISIONS_HANDLING, "removals": removals, "releases": releases,
                               "backups": backups, "links": links, "frozen": frozen}
    if removals_only:
        content.update(removals_only=True, purged_items=purged_items, purge_fingerprints=fingerprints,
                       redactions=redactions)
    else:
        content["decisions"] = decisions
    out: dict[str, Any] = {"removals": len(removals), "releases": len(releases)}
    if removals_only:
        out.update(removals_only=True, purged_items=len(purged_items), purge_fingerprints=len(fingerprints),
                   redactions=len(redactions))
    else:
        out.update(exported=len(decisions), skipped_without_source_key=skipped, edits_not_exported=not_exported)
    return content, out


async def decisions_export(out_path: str, *, actor: str = DEFAULT_ACTOR, removals_only: bool = False
                           ) -> dict[str, Any]:
    """Every imported item a person reviewed or edited in this database, keyed by source_key and by the content hash
    of the imported version (so production applies a decision only to the very same import).

    The file holds decisions, not memory: an item's text is written only when a person edited it (production needs
    the edit), and never for a customer or Credit Repair item. Such an edit is marked edit_not_exported, and
    decisions-apply lists it to be redone by hand. It also lists the removals (keys and JARVIS ids only, never text
    and never a fingerprint of a message or a file; but a purged memory entry's key is what it has been since 0.6.0,
    the file and a 64-bit digest of the entry's text, which confirms a guess of the entry offline: the file says so
    in 'handling' and is kept and deleted like a backup, review 2, S17): every withhold (an admin's own, with whether
    it hid the conversation's title; the rows that are the same message; a hold an admin kept, with who kept it and
    when) and every hold still waiting for review (review 'pending': held
    there for review too), each with the JARVIS row it names (version 4), and the items an admin purged; and the
    releases of held rows (and the rows a release follows). Each removal says who decided and when ('by', 'at') and
    how a withhold's stone joined ('via'): a restore puts them back as they were. With removals_only, only the
    removals and releases (restore.ps1, before a restore), plus what only this same installation can use again: every
    purge by item id and creation time (also of items without a source key: manual and promoted items, incoming
    versions), and the keyed purge fingerprints (HMACs keyed by this database's password, never text)."""
    if os.path.exists(out_path):
        raise Refused("the output file already exists; nothing is overwritten")
    async with sessionmaker()() as s:
        await require_schema(s)
        if not removals_only:
            await refuse_legacy(s)  # the legacy reset writes their file (plan 8.2)
            await _refuse_unsettled(s)
        content, out = await _decisions_content(s, removals_only=removals_only)
    with open(out_path, "x", encoding="utf-8") as f:
        json.dump(content, f, indent=2, sort_keys=True)
    async with sessionmaker()() as s:
        await audit.record(s, actor, "brain.decisions_exported", "brain", "", dict(out))  # no path: it can name a user
        await s.commit()
    return out


async def _redactions(s: AsyncSession) -> list[dict[str, Any]]:
    """Every version an admin redacted (review.redact_version: its title REDACTED_TITLE, no text, the note
    REDACTED_NOTE; never a version a withhold emptied), by item id, the item's creation time and version number, with
    who redacted it and when (its memory.version_redacted audit entry): restore.ps1's file (0.6.2 plan 7.2, review 3
    rv3-dur-3). No text, no fingerprint of any."""
    out: list[dict[str, Any]] = []
    for item_id, version, created in (await s.execute(
            select(MemoryItemVersion.item_id, MemoryItemVersion.version, MemoryItem.created_at)
            .join(MemoryItem, MemoryItem.id == MemoryItemVersion.item_id)
            .where(MemoryItemVersion.title == store.REDACTED_TITLE, MemoryItemVersion.body == "",
                   MemoryItemVersion.note == store.REDACTED_NOTE)
            .order_by(MemoryItemVersion.item_id, MemoryItemVersion.version))).all():
        who = (await s.execute(select(AuditLog.actor, AuditLog.at).where(
            AuditLog.action == "memory.version_redacted", AuditLog.target_type == "memory_item",
            AuditLog.target_id == str(item_id), AuditLog.detail["version"].astext == str(version))
            .order_by(AuditLog.id.desc()).limit(1))).first()
        out.append({"item": int(item_id), "created_at": _iso(created), "version": int(version),
                    "by": (who.actor if who is not None else "")[:200],
                    "at": _iso(who.at) if who is not None else None})
    return out


async def _refuse_unsettled(s: AsyncSession) -> None:
    """ExportDecisions refuses while a decision is not finished (0.6.2 plan 4.7): an admin's withhold whose decision
    backup is not settled (Settle: brain-import settle --backup FILE, jarvis-import -Mode Settle), or, with the owner's
    question 5 at its default, a hold that finishing decisions found and an admin has not kept or released yet. Live
    then applies only the admin's own decisions. restore.ps1's --removals-only file never refuses."""
    pending = dict((await s.execute(sql("SELECT brain_withhold_pending()"))).scalar() or {})
    holds = int(pending.get("unreviewed_settle_holds") or 0)
    # A decision on no known backup (a withhold of a version 4 file, of rows no import run recorded) cannot be
    # finished: it travels as it is, and live imports its conversations as a later backup (fail-safe, plan 3.3).
    waiting = (await s.execute(sql(
        "SELECT coalesce(nullif(b.label, ''), left(t.decided_on, 12)), count(*) "
        "FROM brain_tombstones t LEFT JOIN brain_decision_backups b ON b.sha256 = t.decided_on "
        "WHERE t.reason = :wh AND t.anchor_key = '' AND t.review <> 'pending' AND t.decided_on <> '' "
        "AND NOT EXISTS (SELECT 1 FROM brain_decision_backups x WHERE x.sha256 = t.decided_on "
        "AND x.settled_run_id IS NOT NULL) GROUP BY 1 ORDER BY 1"), {"wh": WITHHELD_BY_ADMIN})).all()
    unsettled = sum(int(n) for _, n in waiting)
    if unsettled:
        raise Refused("%d withheld message(s) are not finished yet: run brain-import settle --backup <the JARVIS "
                      "backup> (jarvis-import -Mode Settle) first, then review what it holds (%s)" % (
                          unsettled, ", ".join("%s: %d" % (label, n) for label, n in waiting)))
    if holds:
        raise Refused("%d row(s) found when finishing decisions wait for review: keep or release each one (JARVIS "
                      "Brain page, Held for review) before exporting" % holds)


# 0.6.2 plan 7.1: a decisions file of 0.6.0 or 0.6.1 (version 3 or older) names each withhold by its key only, with no
# JARVIS row, time or decision backup: it applies only through the legacy reset (legacy_reset(decisions=...)), which
# binds what it can exactly and leaves the rest to the re-import and Settle.
LEGACY_DECISIONS_VERSION = 3
V4_RELEASE = ("a version 4 file's release, not applied (it names no JARVIS backup; fail-safe): release the row again "
              "on the JARVIS Brain page")
LEGACY_FILE_REFUSAL = ("this is a version %d decisions file (Command Center 0.6.0 or 0.6.1): its withholds name a key "
                       "only, so it applies only through the legacy reset: brain-import legacy-reset --decisions FILE "
                       "--actor ADMIN_EMAIL --export NEW_FILE --yes (jarvis-import -Mode LegacyReset -Decisions FILE "
                       "-Actor ADMIN_EMAIL)")


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
    and the actor is "system (restore)" unless an admin is named. The admins' own withholds apply first, so a copy's
    stone never takes the place of the message it is a copy of (whatever order the keys sort in). The restore file of
    this same installation also brings back every purge by item id and creation time (an item without a source key
    the restore brought back is purged again), the keyed purge fingerprints made with this database's password, and
    who decided each removal and when."""
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
    if version <= LEGACY_DECISIONS_VERSION:
        raise Refused(LEGACY_FILE_REFUSAL % version)
    if not removals_only and run_id is None:
        raise Refused("--run is needed (the production import run the decisions apply to), unless --removals-only")
    listed: dict[str, list[int]] = {}
    applied = 0
    via = "restore" if removals_only else "decisions-apply"
    async with sessionmaker()() as s:
        await require_schema(s)
        if not removals_only:  # restore.ps1's own file applies whatever is locked: it only removes (plan 8.1)
            await refuse_legacy(s)
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
            raise Refused(LOCK_BUSY)
        if not removals_only:
            for n, d in enumerate(data.get("decisions") or [], 1):
                reason = await _apply_decision(s, d, int(run_id or 0), user, apply)
                if reason:
                    listed.setdefault(reason, []).append(n)
                else:
                    applied += 1
        after = await identity.current_after_run(s)
        removals = data.get("removals") or []
        removals = removals if isinstance(removals, list) else []
        # What this database held before this apply: a removal that adds a stone or a purge writes, with removals_only,
        # who decided it and when (below), never over what was here already.
        had_keys = set((await s.scalars(select(BrainTombstone.source_key))).all()) if removals_only else set()
        had_purges = {(int(i), at) for i, at in (await s.execute(select(
            BrainPurgedItem.item_id, BrainPurgedItem.item_created_at))).all()} if removals_only else set()
        carried_fps: Counter[str] = Counter()
        if removals_only:
            # The purge fingerprints first: a purge applied again below finds its own and keeps the purge's time.
            carried_fps = await _carry_fingerprints(s, data.get("purge_fingerprints"))
        # The releases first, as exemptions only: a withhold below must see them (an admin's release outlives every
        # rule but the withheld message's own row and text). The rows they free are released after the removals.
        releases = data.get("releases") or []
        fresh: set[tuple[str, str, str]] = set()
        # 0.6.2 plan 7.1: a version 4 file (dev only) applies fail-safe. Its releases name no decision backup, so
        # nothing can tell whether the row here holds the content the admin released: none is applied, and the rows of
        # its withholds' conversations stay as the withholds leave them (held for review where another backup stored
        # them: _hold_other_backup). An admin releases them again here.
        legacy_releases = version < DECISIONS_VERSION
        for r in releases if isinstance(releases, list) and not legacy_releases else []:
            if isinstance(r, dict) and all(isinstance(r.get(k), str) and r[k] for k in ("anchor", "session", "row")):
                row = (r["anchor"], r["session"][:200], r["row"][:200])
                async with s.begin_nested():
                    if await _put_release(s, r, decider.email, removals_only):
                        fresh.add(row)
        if data.get("version") == DECISIONS_VERSION:
            await _carry_tables(s, data)
        removed: Counter[str] = Counter()
        removals_listed: dict[str, list[int]] = {}
        # The admins' own withholds first, then the stones that join them, then purges (the file lists them by key, and
        # a copy's row-form key sorts before the uid-form key of the message it copies): numbered as in the file.
        for n, r in sorted(enumerate(removals, 1), key=_removal_order):
            outcome = await _apply_removal(s, r, decider, review, via=via, after=after)
            if outcome in REMOVAL_DONE:
                removed[outcome] += 1
            else:
                removals_listed.setdefault(outcome, []).append(n)
        purges_listed: dict[str, list[int]] = {}
        purged_items: Counter[str] = Counter()
        redacted: Counter[str] = Counter()
        redactions_listed: dict[str, list[int]] = {}
        if removals_only:
            items = data.get("purged_items") or []
            for n, p in enumerate(items if isinstance(items, list) else [], 1):
                outcome = await _apply_purged_item(s, p, decider, review)
                if outcome in PURGE_DONE:
                    purged_items[outcome] += 1
                    if outcome == "purged":  # one more removal applied again (restore.ps1 counts the removals line)
                        removed[outcome] += 1
                else:
                    purges_listed.setdefault(outcome, []).append(n)
            await _carry_provenance(s, removals, data.get("purged_items"), had_keys, had_purges)
            # The version redactions made after the backup (0.6.2 plan 7.2, review 3 rv3-dur-3), after the purges: a
            # purged item has no version left to redact.
            entries = data.get("redactions") or []
            for n, r in enumerate(entries if isinstance(entries, list) else [], 1):
                outcome = await _apply_redaction(s, r, decider, review)
                if outcome in REDACTION_DONE:
                    redacted[outcome] += 1
                    if outcome in REDACTION_APPLIED:  # one more removal applied again (the removals line)
                        removed[outcome] += 1
                else:
                    redactions_listed.setdefault(outcome, []).append(n)
        released: Counter[str] = Counter()
        releases_listed: dict[str, list[int]] = {}
        for n, r in enumerate(releases if isinstance(releases, list) else [], 1):
            if legacy_releases:
                releases_listed.setdefault(V4_RELEASE, []).append(n)
                continue
            outcome = await _apply_release(s, r, decider, review, via=via, fresh=fresh, removals_only=removals_only)
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
                detail.update(via="restore", purge_fingerprints=dict(carried_fps), purged_items=dict(purged_items),
                              purged_items_not_applied={k: len(v) for k, v in purges_listed.items()},
                              redactions=dict(redacted),
                              redactions_not_applied={k: len(v) for k, v in redactions_listed.items()})
            await audit.record(s, decider.email, "brain.decisions_applied", "import_run" if run_id else "brain",
                               run_id if run_id else "", detail)
            if removals_only and not (removals_listed or purges_listed or releases_listed or redactions_listed):
                # restore.ps1's mark (review 2, S18): the removals are applied again, so their part goes with them.
                # Anything not applied keeps it: nothing may serve the restored data until it is.
                from ..restore_carry import finish_part
                await finish_part(s, "removals")
            await s.commit()
        else:
            await s.rollback()  # the removals ran to count them: nothing is kept
    return {"applied" if apply else "would_apply": applied, "not_applied": listed, "removals": dict(removed),
            "removals_not_applied": removals_listed, "releases": dict(released),
            "releases_not_applied": releases_listed, "purged_items": dict(purged_items),
            "purged_items_not_applied": purges_listed, "purge_fingerprints": dict(carried_fps),
            "redactions": dict(redacted), "redactions_not_applied": redactions_listed}


# --- Decisions before an import (0.6.2 plan 3.3: jarvis-import -Mode Import -Decisions) --------------------------------


def read_decisions(in_path: str) -> dict[str, Any]:
    """A decisions file, checked: its format, and a version this importer reads."""
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
    return data


def decisions_warnings(data: dict[str, Any], backup_sha: str) -> list[str]:
    """Why a decisions file's withholds are not replayed by an import of this backup: it is not a backup they were
    decided on (its SHA-256), or the importer version differs. Those conversations are then a later backup here:
    fail-safe (0.6.2 plan 3.3)."""
    out = []
    shas = {b.get("sha256") for b in data.get("backups") or [] if isinstance(b, dict)}
    if backup_sha not in shas:
        out.append("this JARVIS backup (SHA-256 %s) is not the one the decisions were taken on: conversations with "
                   "a withheld message are imported as a later backup (new rows held for review)" % backup_sha[:12])
    if data.get("importer_version") != IMPORTER_VERSION:
        out.append("the decisions file was written by importer %s, this is importer %s: conversations with a withheld "
                   "message are imported as a later backup" % (str(data.get("importer_version"))[:20],
                                                                 IMPORTER_VERSION))
    return out


def _check_v5(data: dict[str, Any]) -> None:
    if data.get("version") != DECISIONS_VERSION:
        raise Refused("-Decisions with an import needs a version %d decisions file (this one is version %s): export "
                      "it again with this version, or apply an older file after the import (decisions-apply --run)"
                      % (DECISIONS_VERSION, str(data.get("version"))[:20]))


async def decisions_before_import(s: AsyncSession, data: dict[str, Any], decider: _Decider) -> dict[str, Any]:
    """A version 5 decisions file, word for word, before an import (the caller holds the import lock): every release
    as an exemption of its exact JARVIS row, every withhold's stone with its exact binding, review, via, anchor,
    title choice, decision backup, keeper and who decided it and when, every purge's tombstone (an item this database
    holds is purged as decisions-apply purges it), and the decision backups, family links and frozen conversations.
    The import that follows replays them: the database guard stores a row a stone names without text as it is
    inserted, so no withheld text is readable at any moment (0.6.2 plan 3.3, C1). Counts only."""
    from . import review  # imports the task service and the scheduler: not needed by the import itself

    _check_v5(data)
    out: Counter[str] = Counter()
    for r in data.get("releases") or [] if isinstance(data.get("releases"), list) else []:
        if isinstance(r, dict) and all(isinstance(r.get(k), str) and r[k] for k in ("anchor", "session", "row")):
            out["releases" if await _put_release(s, r, decider.email, True) else "releases already here"] += 1
    after = await identity.current_after_run(s)
    removals = data.get("removals") if isinstance(data.get("removals"), list) else []
    for _, r in sorted(enumerate(removals, 1), key=_removal_order):
        if not isinstance(r, dict) or not isinstance(r.get("source_key"), str) or not r["source_key"]:
            out["not a removal"] += 1
            continue
        if r.get("reason") == WITHHELD_BY_ADMIN:
            st = _carried_stone(r, via=str(r.get("via") or "decisions")[:200], after=after)
            st = identity.Stone(**{**st.__dict__, "via": str(r.get("via") or st.via)[:200]})
            values = {"source_key": st.key, "purged_at": _when(r.get("at")) or now(),
                      "purged_by": str(r.get("by") or decider.email)[:200], "reason": WITHHELD_BY_ADMIN,
                      "source_session_id": st.session, "source_row_id": st.row, "role": st.role,
                      "tool_call_id": st.tool_call_id, "message_uid": st.uid, "uid_known": st.uid_known,
                      "anchor_key": st.anchor, "via": st.via, "review": st.review, "bound": bool(st.row or st.uid),
                      "after_run": after, "title_hidden": st.title_hidden, "source_at": st.at,
                      "decided_on": st.decided_on, "kept_by": st.kept_by, "kept_at": st.kept_at,
                      "relations": list(st.relations)}
            if (await s.execute(pg_insert(BrainTombstone).values(**values).on_conflict_do_nothing())).rowcount:
                out["withholds"] += 1
            else:  # a stone of that key is here: a hold the file decided on becomes what the file says
                await identity.put_stone(s, st, actor=str(r.get("by") or decider.email))
                out["withholds already here"] += 1
        elif r.get("reason") == PURGED_BY_ADMIN:
            outcome = await _apply_removal(s, r, decider, review, via="decisions", after=after)
            out["purges" if outcome in REMOVAL_DONE else "purges not applied"] += 1
        else:
            out["not a removal"] += 1
    out.update(await _carry_tables(s, data))
    return dict(out)


def _removal_order(nr: tuple[int, Any]) -> tuple[bool, bool, str]:
    r = nr[1] if isinstance(nr[1], dict) else {}
    return (r.get("reason") != WITHHELD_BY_ADMIN, bool(r.get("anchor")) and r.get("anchor") != r.get("source_key"),
            str(r.get("source_key")))


async def _carry_fingerprints(s: AsyncSession, entries: Any) -> Counter[str]:
    """The restore file's keyed purge fingerprints (identity.live_fingerprints of this installation), stored again
    when they were made with this database's password: a renamed or copied purged file stays out after a restore
    that predates its import. A fingerprint made with another password is counted, not stored (it would only make
    the importer refuse: identity.PurgeIndex)."""
    out: Counter[str] = Counter()
    key = identity.purge_key()
    prefix = "k1:%s:" % identity.purge_key_id(key) if key else None
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict) or not isinstance(e.get("fp"), str) or not e["fp"].startswith("k1:"):
            continue
        if prefix is None or not e["fp"].startswith(prefix):
            out["other database password"] += 1
            continue
        values: dict[str, Any] = {"fp": e["fp"][:200], "source_key": str(e.get("source_key") or "")[:2000],
                                  "kind": str(e.get("kind") or "")[:40], "purged_by": str(e.get("by") or "")[:200],
                                  "via": str(e.get("via") or "restore")[:40]}
        at = _when(e.get("at"))
        if at is not None:
            values["purged_at"] = at
        added = (await s.execute(pg_insert(BrainPurgedContent).values(**values).on_conflict_do_nothing(
            index_elements=[BrainPurgedContent.fp]))).rowcount
        out["carried" if added else "already here"] += 1
    return out


def _when(value: Any) -> datetime | None:
    try:
        at = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return at if at is None or at.tzinfo is not None else at.replace(tzinfo=UTC)


async def _apply_purged_item(s: AsyncSession, p: Any, user: Any, review: Any) -> str:
    """One purge from the restore file, by item id and creation time: the very same item, brought back by the restore
    of an older dump, is deleted and purged again (review.purge: its versions and conflicts, its tombstone and
    fingerprint when it has them, an incoming version's hash rejected on the item it would replace). An item of that id
    with another creation time is another item (a restore resets the id sequence): never touched."""
    if not isinstance(p, dict) or not isinstance(p.get("item"), int) or isinstance(p.get("item"), bool):
        return "not a purge"
    created = _when(p.get("created_at"))
    if created is None:
        return "not a purge"
    item = await s.get(MemoryItem, p["item"])
    if item is None or item.created_at != created:
        return "not here"
    try:
        async with s.begin_nested():
            if SENSITIVE_AGENT == item.agent and not user.can_access_sensitive:
                raise store.BrainError("needs the sensitive-data permission")
            if item.deleted_at is None:
                await store.delete_item(s, item, actor=user.email, note=RESTORE_NOTE)
            flags = dict(item.flags or {})
            detail = {"item": item.id, "kind": item.kind, "agent": item.agent}
            origin = {"origin": item.origin, "message": flags.get("message_id"),
                      "source_withheld": bool(flags.get("source_withheld"))}
            result = await review.purge(s, item, actor=user.email, purged_at=_when(p.get("at")))
            answers = result.pop("answers_scrubbed")
            await audit.record(s, user.email, "memory.purged", "memory_item", detail["item"],
                               {**detail, **result, **origin, "answers_scrubbed": answers, "via": "restore"})
    except store.BrainError as exc:
        return "purged before the restore, refused here: %s" % exc
    except (DataError, IntegrityError):
        return "purged before the restore, refused here by the database"
    return "purged"


async def _carry_provenance(s: AsyncSession, removals: list[Any], purged_items: Any, had_keys: set[str],
                            had_purges: set[tuple[int, datetime]]) -> None:
    """After a restore: who decided each removal and when, and how a withhold's stone joined, as the restore file says,
    on every stone, tombstone and purge record this apply wrote (never on one the restored database already held, and
    only where the stone is what the file says: the same group and review). The brain.decisions_applied audit entry
    records the restore itself."""
    for r in removals:
        if not isinstance(r, dict) or not isinstance(r.get("source_key"), str) or r["source_key"] in had_keys:
            continue
        by, at = r.get("by"), _when(r.get("at"))
        if not isinstance(by, str) or not by or at is None:
            continue
        values: dict[str, Any] = {"purged_by": by[:200], "purged_at": at}
        where = [BrainTombstone.source_key == r["source_key"], BrainTombstone.reason == r.get("reason")]
        if r.get("reason") == WITHHELD_BY_ADMIN:
            anchor = r.get("anchor") if isinstance(r.get("anchor"), str) and r.get("anchor") != r["source_key"] else ""
            review_ = {identity.KEPT: identity.KEPT, identity.PENDING: identity.PENDING}.get(r.get("review"),
                                                                                          identity.CERTAIN)
            where += [BrainTombstone.anchor_key == anchor, BrainTombstone.review == review_]
            if isinstance(r.get("via"), str) and r["via"]:
                values["via"] = r["via"][:200]
        elif r.get("reason") != PURGED_BY_ADMIN:
            continue
        await s.execute(update(BrainTombstone).where(*where).values(**values))
    for p in purged_items if isinstance(purged_items, list) else []:
        if not isinstance(p, dict) or not isinstance(p.get("item"), int):
            continue
        created, at, by = _when(p.get("created_at")), _when(p.get("at")), p.get("by")
        if created is None or at is None or not isinstance(by, str) or not by or (p["item"], created) in had_purges:
            continue
        await s.execute(update(BrainPurgedItem).where(BrainPurgedItem.item_id == p["item"],
                                                      BrainPurgedItem.item_created_at == created)
                        .values(purged_by=by[:200], purged_at=at))


REMOVAL_DONE = ("withheld", "held for review", "purged", "already done", "kept for later imports")
PURGE_DONE = ("purged", "not here")  # a purge by item id: the item purged again, or not in this database (any more)
# A version redaction by item id, creation time and version (restore.ps1's file): redacted again; the item's current
# text here (the restored backup predates the edit that made it an older version), emptied and deleted; or nothing to
# do (already redacted; the item or that version is not in this database, any more).
REDACTION_APPLIED = ("version redacted", "current text redacted, item deleted")
REDACTION_DONE = (*REDACTION_APPLIED, "already done", "not here")
REDACTED_CURRENT_NOTE = ("its text was redacted after the restored backup, and it was the item's current text there: "
                         "emptied and deleted (write it again and restore it, or purge it)")


async def _apply_redaction(s: AsyncSession, r: Any, user: Any, review: Any) -> str:
    """One version redaction from restore.ps1's file, applied again to the very same item (its id and creation time:
    a restore resets the id sequence) as the API redacts it (review.redact_version, audited memory.version_redacted
    with via 'restore' and who decided it and when). The redacted version is the item's current text here when the
    restored backup predates the edit that made it an older version: that text is then removed from the item and
    from that version, and the item deleted (soft: an admin restores it with new text, or purges it). Returns what
    happened, or why it was not applied."""
    created = _when(r.get("created_at")) if isinstance(r, dict) else None
    if (not isinstance(r, dict) or created is None or not isinstance(r.get("item"), int)
            or isinstance(r.get("item"), bool) or not isinstance(r.get("version"), int)
            or isinstance(r.get("version"), bool)):
        return "not a redaction"
    item = await s.scalar(select(MemoryItem).where(MemoryItem.id == r["item"], MemoryItem.created_at == created)
                          .with_for_update())
    version = await s.scalar(select(MemoryItemVersion).where(
        MemoryItemVersion.item_id == r["item"], MemoryItemVersion.version == r["version"])) if item else None
    if item is None or version is None:
        return "not here"
    if review.is_redacted(version):
        return "already done"
    detail: dict[str, Any] = {"item": item.id, "version": r["version"], "kind": item.kind, "via": "restore",
                              "decided_by": str(r.get("by") or "")[:200], "decided_at": _iso(_when(r.get("at")))}
    try:
        async with s.begin_nested():
            if r["version"] != item.version:
                _, answers = await review.redact_version(s, item, r["version"])
                await audit.record(s, user.email, "memory.version_redacted", "memory_item", item.id,
                                   {**detail, "answers_scrubbed": answers})
                return "version redacted"
            version.title, version.body, version.tags, version.note = store.REDACTED_TITLE, "", [], store.REDACTED_NOTE
            item.title, item.body, item.tags = store.REDACTED_TITLE, "", []
            item.content_hash = brain_text.content_hash(item.kind, item.agent, item.title, item.body)
            if item.deleted_at is None:
                await store.delete_item(s, item, actor=user.email, note=REDACTED_CURRENT_NOTE)  # scrubs its quotes
            else:
                await s.flush()
                await scrub_quotes(s, item_ids=[item.id])
            await audit.record(s, user.email, "memory.version_redacted", "memory_item", item.id,
                               {**detail, "current_text": True, "deleted": True})
            return "current text redacted, item deleted"
    except store.BrainError as exc:
        return "redaction after the backup, refused here: %s" % exc
    except (DataError, IntegrityError):
        return "redaction after the backup, refused here by the database"


def _release_exact(r: dict[str, Any]) -> identity.Exact:
    """The exact JARVIS row a release in a decisions file covers (identity.exact). A version 4 file's release carries
    only its row id and uid: the role, call and time are not known, and do not narrow it."""
    def text(name: str) -> str:
        return r[name] if isinstance(r.get(name), str) else ""
    return identity.exact(text("session"), text("row"), text("role"), text("tool_call_id"), _when(r.get("at_row")),
                          text("uid"))


def _release_kind(r: dict[str, Any]) -> str:
    kind = r.get("kind")
    if kind in ("release", "left_readable", "quarantine_release"):
        return str(kind)
    return "left_readable" if r.get("reason") == identity.LEFT_READABLE else "release"


async def _put_release(s: AsyncSession, r: dict[str, Any], decider: str, removals_only: bool) -> bool:
    """A release from a decisions file, as an exemption of its exact JARVIS row; whether it was added."""
    who = _release_who(r, decider, removals_only)
    decided_on = r.get("decided_on") if isinstance(r.get("decided_on"), str) else ""
    return await identity.put_exemption(s, _release_exact(r), anchor=r["anchor"], kind=_release_kind(r),
                                        decided_on=decided_on, reason=who["reason"], decided_by=who["decided_by"],
                                        decided_at=who.get("decided_at"))


async def _carry_tables(s: AsyncSession, data: dict[str, Any]) -> Counter[str]:
    """A version 5 file's decision backups, family links and frozen conversations, added here (never one changed, a
    backup settled on the test copy is settled here too: settled_run_id 0, a run of another installation)."""
    out: Counter[str] = Counter()
    for b in data.get("backups") or [] if isinstance(data.get("backups"), list) else []:
        sha = b.get("sha256") if isinstance(b, dict) else None
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            continue
        out["backups"] += (await s.execute(pg_insert(BrainDecisionBackup).values(
            sha256=sha, label=str(b.get("label") or "")[:200], importer_version=str(b.get("importer_version") or "")[:40],
            newest_at=_when(b.get("newest_at")), first_run_id=None,
            settled_run_id=0 if b.get("settled") is True else None).on_conflict_do_nothing())).rowcount or 0
        if b.get("settled") is True:
            await s.execute(update(BrainDecisionBackup).where(BrainDecisionBackup.sha256 == sha,
                                                              BrainDecisionBackup.settled_run_id.is_(None))
                            .values(settled_run_id=0))
    for link in data.get("links") or [] if isinstance(data.get("links"), list) else []:
        if not isinstance(link, dict) or not all(isinstance(link.get(k), str) and link[k] for k in ("a", "b", "how")):
            continue
        if link["how"] not in ("parent", "branched_from", "compressed_from", "shared_uid", "copied_rows"):
            continue
        out["links"] += (await s.execute(pg_insert(BrainSessionLink).values(
            a=link["a"][:200], b=link["b"][:200], how=link["how"]).on_conflict_do_nothing())).rowcount or 0
    for f in data.get("frozen") or [] if isinstance(data.get("frozen"), list) else []:
        if not isinstance(f, dict) or not isinstance(f.get("session"), str) or not f["session"]:
            continue
        values: dict[str, Any] = {"source_session_id": f["session"][:200], "frozen_by": str(f.get("by") or "")[:200]}
        if _when(f.get("at")) is not None:
            values["frozen_at"] = _when(f.get("at"))
        out["frozen"] += (await s.execute(pg_insert(BrainFrozenSession).values(**values)
                                          .on_conflict_do_nothing())).rowcount or 0
    return out


def _release_who(r: dict[str, Any], decider: str, removals_only: bool) -> dict[str, Any]:
    """Who decided a release in a decisions file, when and why, for its exemption here (review 2, S23): with
    removals_only (restore.ps1, the same installation) the file's admin and time; otherwise the admin applying it, now.
    Its reason either way (released, or identical text left readable)."""
    reason = r.get("reason") if isinstance(r.get("reason"), str) and r.get("reason") else "released by an admin"
    out: dict[str, Any] = {"reason": reason[:200], "decided_by": decider[:200]}
    if removals_only:
        if isinstance(r.get("by"), str) and r["by"]:
            out["decided_by"] = r["by"][:200]
        if _when(r.get("at")) is not None:
            out["decided_at"] = _when(r.get("at"))
    return out


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
    review = {identity.KEPT: identity.KEPT, identity.PENDING: identity.PENDING}.get(r.get("review"), identity.CERTAIN)
    if review == identity.PENDING:  # a hold keeps its evidence (the holds list compares by its rule)
        via = text("via") if text("via") in identity.HOLD_VIA else "same message id"
    return identity.Stone(key=key, anchor=text("anchor") if text("anchor") != key else "", session=session, row=row,
                          role=text("role", 40), tool_call_id=text("tool_call_id"), uid=uid, uid_known=uid_known,
                          via=via, review=review, bound=bool(row or uid), after_run=after,
                          title_hidden=r.get("title_hidden") is True, at=_when(r.get("at_row")) if row else None,
                          decided_on=text("decided_on", 64),
                          kept_by=text("kept_by") if review == identity.KEPT else "",
                          kept_at=_when(r.get("kept_at")) if review == identity.KEPT else None,
                          relations=tuple(identity.relation_order(
                              x for x in (r.get("relations") if isinstance(r.get("relations"), list) else [])
                              if isinstance(x, str))))


async def _apply_removal(s: AsyncSession, r: Any, user: Any, review: Any, *, via: str, after: int,
                         hold_other: bool = True) -> str:
    """One removal from the decisions file: the tombstone, then the message withheld or the item purged if this
    database holds it, each audited as the API audits it (ids and counts). Returns what happened, or why it was not
    applied.

    A withhold's stone is written in a savepoint of its own: when the withhold itself is refused here, the stone
    stays, so the next import brings the message back withheld. The rows it names are found by key and by the exact
    JARVIS row it names (identity.exact_rows); nothing else is re-derived from stored data (0.6.2 plan step 6: a
    version 4 file's families are a later backup for the next import, Settle decides the decision backup). A hold
    carried from the test copy is held on its own row. A purge and its tombstone go together: a refusal (the
    sensitive-data permission) leaves neither. hold_other False (the legacy reset under the legacy lock, whose rows are
    rolled back right after, and imported again and finished by Settle) holds no other row of the family."""
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
                certain = await identity.exact_rows(s, binding)
                if binding.review == identity.PENDING:
                    # A hold on the test copy is a hold here: its own row (key or JARVIS row), never a row an admin
                    # released here.
                    outcome = "already done" if existed else "kept for later imports"
                    for message in certain:
                        await s.refresh(message)
                        sid = await s.scalar(select(ArchiveSession.source_session_id).where(
                            ArchiveSession.id == message.session_id))
                        if message.withheld or await identity.exempt_covers(s, binding.group, sid or "", message):
                            continue
                        done = await review.hold_message(s, message, anchor=binding.group, actor=user.email,
                                                         cause=via, rule=binding.via, relations=binding.relations)
                        await audit.record(s, user.email, "archive.message_held", "archive_message", message.id, {
                            "message": message.id, "session": message.session_id, "anchor": binding.group,
                            "rule": binding.via, "copies": [c["item"] for c in done.copies],
                            "versions_blanked": done.versions_blanked,
                            "rollback_copies_blanked": done.rollback_copies_blanked,
                            "answers_scrubbed": done.answers_scrubbed, "via": via})
                        outcome = "held for review"
                    return outcome
                held_here = await _hold_other_backup(s, binding, certain, user, review, via=via) if hold_other else 0
                if not certain:
                    # No row of it here (not imported yet, or removed by a rollback or an import): its rollback
                    # copies, and the items promoted from an earlier row of it, which kept the text, lose it.
                    await withhold_copies(s, source_keys=[key], actor=user.email, cause=via)
                    return "held for review" if held_here else "already done" if existed else "kept for later imports"
                outcome = "held for review" if held_here else "already done"
                # The admin's own title choice (0.6.x withholds, carried as 'upgrade', made none: the default applies)
                hide = (r["title_hidden"] if isinstance(r.get("title_hidden"), bool) and not binding.anchor
                        and r.get("review") != identity.PENDING and r.get("via") != "upgrade" else None)
                for message in certain:
                    await s.refresh(message)  # a withhold just before may have taken it with its own group
                    if message.withheld:
                        if binding.review == identity.KEPT:
                            _carry_keep(message, r, user, removals_only=via == "restore")
                        continue
                    done = await review.withhold_message(s, message, actor=user.email, cause=via, via=via,
                                                         hide_title=hide)
                    await _withheld_audit(s, user, message, done, tombstone=not existed, via=via)
                    outcome = "withheld"
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


async def _hold_other_backup(s: AsyncSession, binding: identity.Stone, certain: list[ArchiveMessage], user: Any,
                             review: Any, *, via: str) -> int:
    """0.6.2 plan 5.3 (R5/S2/rv3-res-3, S1 (c)): a decision carried to a database whose newest import of the
    conversation family is of another JARVIS backup than the one it was decided on is not replayed there (3.3), and
    that database stored the family's rows before the decision arrived. The readable rows of the family that an import
    of another backup stored are held for review at once (fail-safe; identity.other_backup_rows: nothing can compare
    their text), unless an admin's release or 'left readable' choice covers that exact row. A restore (the same
    installation, whose imports already decided these rows) holds nothing. Returns how many rows it held."""
    if via == "restore" or not binding.session:
        return 0
    here_on = await identity.decision_backup(s, [binding.session])
    if not here_on or here_on == binding.decided_on:
        return 0
    held = 0
    for message, sid, rel in await identity.other_backup_rows(s, binding, exclude=[m.id for m in certain]):
        if await identity.exempt_covers(s, binding.group, sid, message):
            continue
        done = await review.hold_message(s, message, anchor=binding.group, actor=user.email, cause=via,
                                         rule=identity.OTHER_BACKUP_HOLD, relations=rel)
        await audit.record(s, user.email, "archive.message_held", "archive_message", message.id, {
            "message": message.id, "session": message.session_id, "anchor": binding.group,
            "rule": identity.OTHER_BACKUP_HOLD, "copies": [c["item"] for c in done.copies],
            "versions_blanked": done.versions_blanked, "rollback_copies_blanked": done.rollback_copies_blanked,
            "answers_scrubbed": done.answers_scrubbed, "via": via})
        held += 1
    return held


def _carry_keep(message: ArchiveMessage, r: dict[str, Any], user: Any, *, removals_only: bool) -> None:
    """A hold an admin kept (the file's stone says 'kept') whose row here is still flagged as held for review (the
    restored backup held it before the keep): the row says kept, by whom and when, as review.keep_hold writes it
    (review 2, S23). Its text stays withheld either way."""
    flag = dict((message.flags or {}).get("withheld_by_admin") or {})
    if flag.get("review") != identity.PENDING:
        return
    by = r.get("kept_by") if removals_only and isinstance(r.get("kept_by"), str) and r.get("kept_by") else user.email
    at = r.get("kept_at") if removals_only and _when(r.get("kept_at")) is not None else now().isoformat()
    flag.update(review=identity.KEPT, kept_by=str(by)[:200], kept_at=str(at)[:64])
    message.flags = {**(message.flags or {}), "withheld_by_admin": flag}


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


async def _apply_release(s: AsyncSession, r: Any, user: Any, review: Any, *, via: str,
                         fresh: set[tuple[str, str, str]] = frozenset(), removals_only: bool = False) -> str:
    """One release from the decisions file (an admin said a held JARVIS row is not the withheld message): the
    exemption, then a row held here for that group released as the API releases it (review.release_hold: its text
    comes back at the next import). A row a certain rule names stays withheld (the exemption never skips those)."""
    names = ("anchor", "session", "row")
    if not isinstance(r, dict) or not all(isinstance(r.get(k), str) and r[k] for k in names):
        return "not a release"
    anchor, session_id, row_id = (r[k][:200] if k != "anchor" else r[k] for k in names)
    who = _release_who(r, user.email, removals_only)
    async with s.begin_nested():
        added = await _put_release(s, r, user.email, removals_only) or (anchor, session_id, row_id) in fresh
        held = (await s.execute(
            select(ArchiveMessage, ArchiveSession, BrainTombstone)
            .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
            .join(BrainTombstone, BrainTombstone.source_key == ArchiveMessage.source_key)
            .where(ArchiveSession.source_session_id == session_id,
                   ArchiveMessage.source_message_id == row_id,
                   ArchiveMessage.withheld.is_(True), BrainTombstone.reason == WITHHELD_BY_ADMIN,
                   BrainTombstone.review == identity.PENDING,
                   func.coalesce(func.nullif(BrainTombstone.anchor_key, ""), BrainTombstone.source_key) == anchor)
            .order_by(ArchiveMessage.id))).all()
        released = 0
        for message, sess, _ in held:
            if not await identity.exempt_covers(s, anchor, session_id, message):
                continue  # another version of the row (other content): the release does not cover it
            try:
                async with s.begin_nested():
                    stone = await review.release_hold(s, message, sess, actor=user.email, confirmed=True,
                                                      decided_by=who["decided_by"], decided_at=who.get("decided_at"),
                                                      reason=who["reason"])
            except store.BrainError:
                continue  # the same message as a withheld one: it stays withheld
            await audit.record(s, user.email, "archive.hold_released", "archive_message", message.id, {
                "message": message.id, "session": message.session_id,
                "anchor": stone.anchor_key or stone.source_key, "rule": stone.via, "via": via})
            released += 1
        if not released and (anchor, session_id, row_id) in fresh:
            # Recorded before the removals, the release kept the row readable here: no withhold above held it.
            for message in (await s.scalars(select(ArchiveMessage).join(
                    ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id).where(
                    ArchiveSession.source_session_id == session_id, ArchiveMessage.source_message_id == row_id,
                    ArchiveMessage.withheld.is_(False)).order_by(ArchiveMessage.id))).all():
                await audit.record(s, user.email, "archive.hold_released", "archive_message", message.id, {
                    "message": message.id, "session": message.session_id, "anchor": anchor, "rule": "",
                    "via": via})
                released += 1
    if released:
        return "released"
    return "kept for later imports" if added else "already done"


# --- The legacy reset (0.6.2 plan 8.2) ----------------------------------------------------------------------------------


async def legacy_runs(s: AsyncSession) -> list[ImportRun]:
    """The legacy import runs, newest first: by an importer older than 1.1.0 (0.6.0, 0.6.1; 0008's
    brain_importer_older) and not rolled back."""
    return list((await s.scalars(select(ImportRun).where(
        ImportRun.status != "rolled_back", func.brain_importer_older(ImportRun.importer_version))
        .order_by(ImportRun.id.desc()))).all())


async def _bind_legacy(s: AsyncSession) -> Counter[str]:
    """Every admin's own withhold that names no JARVIS row yet (0.6.0 and 0.6.1 kept a key only, and migration 0008
    never re-derives one: plan 1.6, 1.7) is bound to the exact JARVIS row its key names in the stored data: that row's
    JARVIS session, row id, role, tool_call_id, time and uid (identity.exact), and the backup it was decided on (the
    newest import of its conversation family, identity.decision_backup). A row-form key names its JARVIS row
    (session:row) and a uid-form key its Hermes message id, so the stored row of the key is the message. A key with no
    stored row (rolled back, deleted by an import, or never imported) cannot be bound exactly: its stone keeps
    withholding that key, as it did (fail-closed), and it is counted. Counts only."""
    out: Counter[str] = Counter()
    for st in (await s.scalars(select(BrainTombstone).where(
            BrainTombstone.reason == WITHHELD_BY_ADMIN, BrainTombstone.anchor_key == "",
            BrainTombstone.review != identity.PENDING, BrainTombstone.source_row_id == "",
            BrainTombstone.message_uid == "").order_by(BrainTombstone.source_key).with_for_update())).all():
        found = (await s.execute(select(ArchiveMessage, ArchiveSession.source_session_id).join(
            ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id).where(
            ArchiveMessage.source_key == st.source_key).limit(2))).all()
        if len(found) != 1 or not found[0][1] or not found[0][0].source_message_id:
            out["withholds kept by their key only (no stored row to bind)"] += 1
            continue
        m, sid = found[0]
        uid = identity.row_uid(m)
        st.source_session_id, st.source_row_id = sid[:200], str(m.source_message_id)[:200]
        st.role, st.tool_call_id = (m.role or "")[:40], (m.tool_call_id or "")[:200]
        st.message_uid, st.uid_known = uid[:200], bool(uid) and st.source_key.startswith(identity.UID_KEY)
        st.source_at, st.bound = m.at, True
        if not st.decided_on:
            st.decided_on = (await identity.decision_backup(s, [sid]))[:64]
        out["withholds bound to their exact JARVIS row"] += 1
    await s.flush()
    return out


def _add_counts(total: dict[str, Any], part: Any) -> None:
    for key, value in (part or {}).items():
        total[key] = total.get(key, 0) + int(value or 0)


async def legacy_reset(out_path: str, *, actor: str = DEFAULT_ACTOR, confirm: bool = False,
                       decisions: str | None = None) -> dict[str, Any]:
    """brain-import legacy-reset --export FILE --yes (jarvis-import -Mode LegacyReset; 0.6.2 plan 8.2).

    JARVIS Brain data that Command Center 0.6.0 or 0.6.1 imported (legacy: brain_maintenance 'legacy_brain_data', set
    by migration 0008) is locked and hidden from every reader. The reset, in one transaction under the import lock:
    1. with decisions (a version 3 file of 0.6.0 or 0.6.1: keys only, which decisions-apply refuses; plan 7.1, and an
       active admin as actor), its withholds and purges are applied first, as decisions-apply applied them;
    2. each admin's own withhold that names no JARVIS row is bound to the exact JARVIS row its key names (_bind_legacy);
    3. a version 5 decisions file of every decision is written to FILE (withholds and holds with their exact binding,
       releases, purges, review decisions on imported items, decision backups, family links, stopped conversations):
       nothing an admin decided is lost, and the file can go to the live installation;
    4. the legacy runs are rolled back, newest first (their reviewed items too: their review decisions are in the
       file);
    5. the lock is lifted.
    The owner then imports the same JARVIS backup again (with --decisions FILE to apply the review decisions) and
    finishes the decisions on it (brain-import settle). The stones and purges stay in the database throughout: a row
    of a withheld message comes back without its text. Without confirm it changes nothing and says what it would do
    (Refused). Refusals come before any change; audited with counts only."""
    from . import review  # imports the task service and the scheduler: not needed by the import itself

    if os.path.exists(out_path):
        raise Refused("the output file already exists; nothing is overwritten")
    data = read_decisions(decisions) if decisions else None
    if data is not None and int(data.get("version") or 0) > LEGACY_DECISIONS_VERSION:
        raise Refused("--decisions takes a decisions file of Command Center 0.6.0 or 0.6.1 (version 3 or older); "
                      "this one is version %s: apply a version 4 file with decisions-apply, a version 5 file with the "
                      "import itself (run --decisions)" % str(data.get("version"))[:20])
    async with sessionmaker()() as s:
        await require_schema(s)
        locked = await legacy_locked(s)
        if not locked and data is None:
            raise Refused("there is no legacy JARVIS Brain data (0.6.0 or 0.6.1) to reset, and no version 3 decisions "
                          "file was given (--decisions)")
        decider: _Decider | None = None
        if data is not None:
            user = await s.scalar(select(User).where(func.lower(User.email) == (actor or "").lower(),
                                                     User.role == "admin", User.is_active.is_(True)))
            if user is None:
                raise Refused("--decisions needs --actor, the e-mail address of an active admin: decisions that need "
                              "an admin keep needing one")
            decider = _Decider(user.email, bool(user.can_access_sensitive))
        await mark_dead_runs(s)  # commits: before the lock below, which the commit would end
        if not await try_xact_lock(s, GLOBAL_LOCK):
            raise Refused(LOCK_BUSY)
        runs = [(r.id, r.status) for r in await legacy_runs(s)]  # ids: a rollback below expires the rows
        running = [run_id for run_id, status in runs if status == "running"]
        if running:
            raise Refused("an import by an importer older than 1.1.0 is still running: let it finish or stop it first",
                          running)
        carried: Counter[str] = Counter()
        if data is not None and decider is not None:
            after = await identity.current_after_run(s)
            removals = data.get("removals") if isinstance(data.get("removals"), list) else []
            for _, r in sorted(enumerate(removals, 1), key=_removal_order):
                carried[await _apply_removal(s, r, decider, review, via="legacy-reset", after=after,
                                             hold_other=not locked)] += 1
        bound = await _bind_legacy(s)
        # a purge 0.6.0/0.6.1 made of an incoming version (a plain hash in the item's flags) survives the rollback that
        # deletes the item: as a keyed fingerprint (review 3, readers)
        await identity.legacy_rejected_hashes(s, actor=actor)
        content, counts = await _decisions_content(s, removals_only=False)
        rolled: dict[str, Any] = {"deleted": {}, "restored": {}, "reverted": {}}
        discarded = 0
        for run_id, _ in runs:
            result = await rollback_run(s, run_id, actor, discard_reviewed=True)
            for key in rolled:
                _add_counts(rolled[key], result.get(key))
            discarded += int(result.get("discarded_reviewed_items") or 0)
        await s.execute(delete(BrainMaintenance).where(BrainMaintenance.name == LEGACY_LOCK))
        out: dict[str, Any] = {
            "runs_rolled_back": [run_id for run_id, _ in runs], "lock_lifted": locked, **dict(bound),
            "decisions_file": {k: v for k, v in counts.items() if isinstance(v, int)},
            "version_3_file": dict(carried) if data is not None else None,
            "rolled_back": rolled, "reviewed_items_deleted": discarded}
        content["legacy_reset"] = {"runs_rolled_back": out["runs_rolled_back"], **dict(bound),
                                   "version_3_file": out["version_3_file"]}
        if not confirm:
            await s.rollback()
            raise Refused("nothing was changed: the legacy reset would roll back %d run(s) (%s), bind %d withhold(s) "
                          "to their exact JARVIS row (%d kept by their key only) and write %d removal(s), %d "
                          "release(s) and %d review decision(s) to the file. Add --yes to reset" % (
                              len(runs), ", ".join(str(run_id) for run_id, _ in runs) or "none",
                              bound["withholds bound to their exact JARVIS row"],
                              bound["withholds kept by their key only (no stored row to bind)"],
                              counts.get("removals", 0), counts.get("releases", 0), counts.get("exported", 0)))
        await audit.record(s, actor, "brain.legacy_reset", "brain", "",
                           {k: v for k, v in out.items() if k != "rolled_back"})
        with open(out_path, "x", encoding="utf-8") as f:
            json.dump(content, f, indent=2, sort_keys=True)
        try:
            await s.commit()
        except BaseException:
            os.remove(out_path)  # nothing was reset: the file must not say otherwise
            raise
    return out


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
