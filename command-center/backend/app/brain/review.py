"""The JARVIS Brain's review actions (spec section 3), behind app/api/brain.py.

What lives here: resolving import conflicts (recording `effects` so a rollback can revert them), reviewing,
editing and converting workflow drafts (a converted draft becomes a *disabled* schedule), promoting an archived
message to a memory item, withholding a message, redacting an old version, purging an item (with a tombstone),
the overview counts for one person's scope, what each agent reads, and the export of approved items.

The API checks roles and scope, writes the audit entry and commits; the functions here only flush. A request they
refuse raises store.BrainError, whose message is safe to show. Nothing here calls the network or an AI model, and
nothing imported can approve, enable or run anything: approvals stay with app/tasks.py.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import ColumnElement, and_, delete, func, not_, or_, select, true, update
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import licensing
from .. import tasks as task_service
from ..config import Settings
from ..models import (
    ArchiveMessage,
    ArchiveSession,
    BrainFrozenSession,
    BrainPurgedItem,
    BrainTombstone,
    ImportRecord,
    ImportRun,
    MemoryConflict,
    MemoryItem,
    MemoryItemVersion,
    Schedule,
    Site,
    WorkflowDraft,
)
from ..worker.scheduler import valid_cron
from . import AGENTS, KINDS, identity, store
from . import text as brain_text
from .archive import WITHHELD_BY_ADMIN, _blank_records, promoted_ref, recompute_session, withhold_copies
from .quotes import relabel_hidden_titles, scrub_quotes
from .policy import (
    POLICIES,
    SENSITIVE_AGENT,
    PersonScope,
    agent_archive,
    agent_items,
    needs_admin,
    person_items,
    person_messages,
    person_sessions,
    withheld_source,
)
from .store import REDACTED_NOTE, REDACTED_TITLE, BrainError

ITEM_STATUSES = ("pending_review", "quarantined", "approved", "rejected", "archived")
RESOLUTIONS = ("keep_existing", "use_incoming", "keep_both", "dismissed")
# Conflicts only an admin may decide, whatever the resolution.
ADMIN_CONFLICTS = ("instruction", "source_changed", "workflow")
DRAFT_DECISIONS = ("reviewed", "rejected", "draft")
# The draft fields an import writes (mapping.draft_for_job): what a 'workflow' conflict's incoming JSON may replace.
DRAFT_FIELDS = ("name", "staff", "agent", "capability", "params", "compatibility", "trigger", "cron", "schedule_display",
                "timezone", "enabled_in_source", "deliver", "approval", "error_handling", "duplicate_guard", "spec",
                "history", "notes", "source_fp")
COMPATIBILITY = ("mapped", "partial", "reference_only")
TRIGGERS = ("schedule", "manual", "event")
SHARED = "(shared)"

ITEM_FIELDS = ("id", "kind", "agent", "title", "body", "tags", "sensitivity", "status", "origin", "flags",
               "source_system", "source_ref", "source_at", "import_run_id", "version", "created_at", "created_by",
               "updated_at", "updated_by", "reviewed_by", "reviewed_at", "review_note", "deleted_at")
MESSAGE_FIELDS = ("id", "session_id", "position", "role", "content", "truncated", "tool_name", "tool_calls", "at",
                  "state", "sensitivity", "withheld", "labels", "redactions", "flags")
VERSION_FIELDS = ("id", "item_id", "version", "change", "kind", "agent", "title", "body", "tags", "sensitivity",
                  "status", "changed_by", "changed_at", "note")
SESSION_FIELDS = tuple(c.key for c in ArchiveSession.__table__.columns if c.key != "search")
RUN_FIELDS = tuple(c.key for c in ImportRun.__table__.columns if c.key != "report")
DRAFT_COLUMNS = tuple(c.key for c in WorkflowDraft.__table__.columns)
CONFLICT_COLUMNS = tuple(c.key for c in MemoryConflict.__table__.columns)


def now() -> datetime:
    return datetime.now(UTC)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


# --- Output shapes ----------------------------------------------------------------------------------------------


def item_needs_admin(item: MemoryItem) -> bool:
    return needs_admin(item.kind, item.agent, item.status, item.flags)


def item_out(item: MemoryItem, **extra: Any) -> dict[str, Any]:
    """MemoryItemOut (spec 3). Never a hash of a version's bytes: the rejected versions are counted (review 2, S16: a
    plain SHA-256 of a short file confirms guesses of its text offline)."""
    out = {k: _iso(getattr(item, k)) for k in ITEM_FIELDS}
    if isinstance(out.get("flags"), dict) and "rejected_source_sha256" in out["flags"]:
        flags = dict(out["flags"])
        rejected = flags.pop("rejected_source_sha256")
        flags["rejected_versions"] = len(rejected) if isinstance(rejected, list) else 1
        out["flags"] = flags
    out["kind_label"] = KINDS.get(item.kind, item.kind)
    out["needs_admin"] = item_needs_admin(item)
    out.update(extra)
    return out


def is_redacted(v: MemoryItemVersion) -> bool:
    return v.title == REDACTED_TITLE and v.body == ""


def version_out(v: MemoryItemVersion) -> dict[str, Any]:
    out = {k: _iso(getattr(v, k)) for k in VERSION_FIELDS}
    out["redacted"] = is_redacted(v)
    return out


def session_out(s: ArchiveSession) -> dict[str, Any]:
    """ArchiveSessionOut: every column except the search vector."""
    return {k: _iso(getattr(s, k)) for k in SESSION_FIELDS}


def message_out(m: ArchiveMessage) -> dict[str, Any]:
    return {k: _iso(getattr(m, k)) for k in MESSAGE_FIELDS}


def run_out(r: ImportRun, *, report: bool = False) -> dict[str, Any]:
    """ImportRunOut: every column except the report, which only the single run carries."""
    out = {k: _iso(getattr(r, k)) for k in RUN_FIELDS}
    if report:
        out["report"] = r.report or {}
    return out


def run_brief(r: ImportRun) -> dict[str, Any]:
    """What a viewer sees of the latest import: when and how it ended, nothing about the sources."""
    return {"id": r.id, "status": r.status, "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at),
            "rolled_back_at": _iso(r.rolled_back_at), "counts": r.counts or {}}


def draft_out(d: WorkflowDraft) -> dict[str, Any]:
    return {k: _iso(getattr(d, k)) for k in DRAFT_COLUMNS}


def conflict_out(c: MemoryConflict, *, existing_item: MemoryItem | None = None,
                 incoming_item: MemoryItem | None = None, admin_needed: bool = False) -> dict[str, Any]:
    out = {k: _iso(getattr(c, k)) for k in CONFLICT_COLUMNS}
    # Never a version's plain SHA-256 (review 3, readers: an old version's PIN is confirmable offline from it; the
    # dashboard never read it). The stored conflict keeps it for a rollback until that version is redacted.
    for side in ("existing", "incoming"):
        if isinstance(out.get(side), dict):
            out[side] = {k: v for k, v in out[side].items() if k != "source_sha256"}
    out["existing_item"] = item_out(existing_item) if existing_item is not None else None
    out["incoming_item"] = item_out(incoming_item) if incoming_item is not None else None
    out["needs_admin"] = admin_needed
    return out


def clean_note(note: str) -> str:
    """A note a person typed, cleaned like any other memory text (secrets redacted, contacts masked)."""
    if not note:
        return ""
    cleaned = brain_text.clean(note, "chat")
    if cleaned.withheld:
        raise BrainError("the note holds restricted data (" + ", ".join(cleaned.restricted) + "); leave it out")
    return cleaned.text[:2000]


# --- Scope helpers ----------------------------------------------------------------------------------------------


def conflict_scope(scope: PersonScope) -> ColumnElement[bool]:
    """Conflicts a person may see: every memory item they point to is in the person's scope (an admin also sees
    deleted ones). A conflict about a customer or quarantined item is therefore admin-only, and one about a Credit
    Repair item needs the sensitive-data permission."""
    visible = select(MemoryItem.id).where(person_items(scope, include_deleted=True)).scalar_subquery()
    return and_(
        or_(MemoryConflict.incoming_item_id.is_(None), MemoryConflict.incoming_item_id.in_(visible)),
        or_(MemoryConflict.existing_table != "memory_items", MemoryConflict.existing_id.is_(None),
            MemoryConflict.existing_id.in_(visible)))


async def _counts(session: AsyncSession, column: Any, where: ColumnElement[bool], model: Any, *, empty: str = ""
                  ) -> dict[str, int]:
    rows = (await session.execute(select(column, func.count()).select_from(model).where(where).group_by(column))).all()
    return {(str(k) if k not in ("", None) else empty): int(n) for k, n in rows}


# --- Overview ---------------------------------------------------------------------------------------------------


async def overview(session: AsyncSession, scope: PersonScope, *, ai_context: str, instance_label: str
                   ) -> dict[str, Any]:
    """Counts in the person's scope. A viewer gets the totals of the general archive (what an operator could list),
    without the per-staff breakdown, and nothing else about conversations."""
    items = person_items(scope)
    memory = {
        "total": int(await session.scalar(select(func.count()).select_from(MemoryItem).where(items)) or 0),
        "by_status": await _counts(session, MemoryItem.status, items, MemoryItem),
        "by_kind": await _counts(session, MemoryItem.kind, items, MemoryItem),
        "by_agent": await _counts(session, MemoryItem.agent, items, MemoryItem, empty=SHARED),
    }
    archive_scope = scope if scope.sees_archive else PersonScope("operator", False)
    sessions_where = person_sessions(archive_scope)
    span = (await session.execute(select(func.count(), func.min(ArchiveSession.started_at),
                                         func.max(ArchiveSession.started_at)).where(sessions_where))).one()
    by_withheld = dict((await session.execute(
        select(ArchiveMessage.withheld, func.count()).join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
        .where(sessions_where, person_messages(archive_scope)).group_by(ArchiveMessage.withheld))).all())
    archive = {
        "sessions": int(span[0] or 0), "messages": int(by_withheld.get(False, 0)),
        "withheld_messages": int(by_withheld.get(True, 0)), "first_at": _iso(span[1]), "last_at": _iso(span[2]),
        "by_staff": await _counts(session, ArchiveSession.staff, sessions_where, ArchiveSession)
        if scope.sees_archive else {},
    }
    review = {
        "pending_items": int(await session.scalar(select(func.count()).select_from(MemoryItem).where(
            items, MemoryItem.status == "pending_review")) or 0),
        "quarantined_items": int(await session.scalar(select(func.count()).select_from(MemoryItem).where(
            items, MemoryItem.status == "quarantined")) or 0),
        "open_conflicts": int(await session.scalar(select(func.count()).select_from(MemoryConflict).where(
            MemoryConflict.status == "open", conflict_scope(scope))) or 0) if scope.operates else 0,
    }
    workflows = {
        "drafts": int(await session.scalar(select(func.count()).select_from(WorkflowDraft)) or 0),
        "by_status": await _counts(session, WorkflowDraft.status, true(), WorkflowDraft),
    }
    latest = await session.scalar(select(ImportRun).order_by(ImportRun.id.desc()).limit(1))
    imports = {
        "runs": int(await session.scalar(select(func.count()).select_from(ImportRun)) or 0),
        "latest": (run_out(latest) if scope.operates else run_brief(latest)) if latest is not None else None,
    }
    return {"memory": memory, "archive": archive, "review": review, "workflows": workflows, "imports": imports,
            "ai_context": ai_context, "instance_label": instance_label,
            "scope": {"role": scope.role, "sensitive": scope.sensitive, "sees_customer": scope.sees_customer,
                      "sees_quarantined": scope.is_admin, "sees_archive": scope.sees_archive}}


# --- Agents -----------------------------------------------------------------------------------------------------


def used_by(agent: str) -> list[str]:
    """Where an agent actually reads memory (spec 3 and 4)."""
    out = []
    if agent != SENSITIVE_AGENT:
        out.append("task results (reference notes)")
    if agent in ("seo_content", "social_marketing", "communications"):
        out.append("AI drafting prompts (when allowed)")
    if agent == "supervisor":
        out.append("answers about JARVIS's history")
    return out


async def agents_view(session: AsyncSession, scope: PersonScope) -> list[dict[str, Any]]:
    """policy.describe() for each agent, with where it reads memory and how many approved items and archived
    sessions it reads (counted within the person's own scope; sessions are not counted for viewers)."""
    out = []
    visible = person_items(scope)
    for key in AGENTS:
        policy = POLICIES.get(key)
        if policy is None:
            continue
        where = and_(agent_items(policy), visible)
        by_kind = {k: int(n) for k, n in (await session.execute(
            select(MemoryItem.kind, func.count()).where(where).group_by(MemoryItem.kind))).all()}
        sessions = None
        if scope.sees_archive:
            sessions = int(await session.scalar(select(func.count()).select_from(ArchiveSession).where(
                agent_archive(policy), person_sessions(scope))) or 0)
        out.append({**policy.describe(), "used_by": used_by(key),
                    "items": {"approved": sum(by_kind.values()), "by_kind": by_kind}, "sessions": sessions})
    return out


# --- Messages ---------------------------------------------------------------------------------------------------


async def promote_message(session: AsyncSession, message: ArchiveMessage, sess: ArchiveSession, *, kind: str,
                          title: str, agent: str, actor: str) -> MemoryItem:
    """A pending memory item from an archived message (quarantined when a secret was redacted from it). It keeps the
    highest sensitivity of the message and its session, has no import run (so it survives a rollback) and names its
    source. Refused for a withheld message, and for one an admin withheld whose row has not been reached yet (its
    tombstone). The caller locks the message's row, so a withhold of it waits for the new item, or this waits for it."""
    if message.withheld or "restricted" in (message.sensitivity, sess.sensitivity):
        raise BrainError("this message is withheld: its text was never stored, so it cannot be promoted")
    stone = await session.get(BrainTombstone, message.source_key) if message.source_key else None
    if stone is not None and stone.reason == WITHHELD_BY_ADMIN:  # its row still has the text until the next import
        raise BrainError("an admin withheld this message: it cannot be promoted")
    if await identity.matches_withheld(session, message, sess):  # another stored row of a withheld JARVIS message
        raise BrainError("this message is the same JARVIS message as one an admin withheld: it cannot be promoted")
    if sess.agent == SENSITIVE_AGENT and agent != SENSITIVE_AGENT:
        raise BrainError("text from a Credit Repair conversation stays in Credit Repair: promote it as a Credit Repair "
                         "item")
    if not (message.content or "").strip():
        raise BrainError("this message has no text to promote (it holds tool calls only)")
    sensitivity = brain_text.most_sensitive(message.sensitivity, sess.sensitivity)
    flags: dict[str, Any] = {"message_id": message.id, "archive_session_id": sess.id}
    if message.source_key:  # names the message after a rollback and a new import too (its row id changes)
        flags["message_source_key"] = message.source_key
    if message.redactions:  # the text held a secret: an admin reviews it, and it never reaches a prompt (RISK_FLAGS)
        flags["secrets_redacted"] = dict(message.redactions)
    return await store.create_item(
        session, kind=kind, title=title, body=message.content, actor=actor, agent=agent,
        status="quarantined" if message.redactions else "pending_review", origin="promoted", flags=flags,
        sensitivity=sensitivity,
        source_system=sess.source_system or "jarvis",
        source_ref=promoted_ref(sess.source_session_id, message.position),
        source_at=message.at, import_run_id=None, note="promoted from an archived JARVIS message")


@dataclass
class Withheld:
    """What withhold_message() did (ids and counts only)."""
    session: ArchiveSession | None
    tombstone_created: bool
    copies: list[dict[str, Any]] = field(default_factory=list)  # store.empty_withheld_copy() of each emptied item
    versions_blanked: int = 0
    rollback_copies_blanked: int = 0
    answers_scrubbed: int = 0
    also_withheld: list[int] = field(default_factory=list)  # other stored rows of the same message (members)
    held_for_review: list[int] = field(default_factory=list)  # rows withheld as holds (same uid other text; matching)
    identical_withheld: int = 0  # rows matching the withheld text held for review (the dialog's pre-ticked rows)
    left_readable: int = 0  # matching rows the admin left readable (unticked)
    keys_tombstoned: int = 0  # stones written (the message's own, its rows', its copies' without a row)
    title_hidden: bool = False


LEFT_READABLE = identity.LEFT_READABLE  # an exemption's reason (S21)


def _blank(row: ArchiveMessage, flag: dict[str, Any]) -> None:
    """A stored row without its text and without a fingerprint of it (source_fp 'w1:<state>')."""
    row.content = ""
    row.tool_calls = []
    row.labels = []
    row.truncated = False
    row.withheld = True
    row.source_fp = "w1:%s" % (row.state or "live")
    row.flags = {**(row.flags or {}), "withheld_by_admin": flag}


async def withhold_message(session: AsyncSession, message: ArchiveMessage, *, actor: str = "admin",
                           cause: str = "admin", ip: str = "", include_identical: bool = False,
                           hide_title: bool | None = None, via: str = "admin",
                           keep_identical_readable: bool = False, leave_readable: Iterable[int] = ()) -> Withheld:
    """Removes a message's text (and tool calls and labels) for good, with every other stored row that is the same
    JARVIS message by provenance (identity.group_rows, 0.6.2 plan 4.5: the same JARVIS row, the same uid with the same
    text, the importer's recorded duplicates; a row with the same uid but other text is held for review), and holds
    for review the stored rows of the conversation family that equal, contain, restate or quote the withheld text
    (identity.matching_rows: the withhold dialog's pre-ticked rows, include_identical), except those the admin unticked
    (leave_readable: ids) or all of them with keep_identical_readable and no include_identical (the admin left them
    readable): each such choice is kept as a 'left readable' exemption of the group, so Settle and every import leave
    that exact row readable. decisions-apply passes neither (nobody saw those rows here: the test copy's choices travel
    in the file). Each row withheld gets a stone (identity.put_stone) in the admin's group, as do the keys of its
    copies that have no row, so a later import brings none of them back with text. Then everything that copied them
    (archive.withhold_copies: rollback copies, the memory items promoted from them, emptied, and the answers and notes
    quoting either) goes, and their sessions are recomputed. The conversation's title is hidden too (hide_title; by
    default when this is the conversation's first user message, which a title can paraphrase). It never releases
    anything; Settle (brain-import settle) decides the decision backup with the withheld text in memory and is
    authoritative. All or nothing in the caller's transaction."""
    if message.withheld:
        raise BrainError("this message is already withheld")
    sess = await session.get(ArchiveSession, message.session_id)
    assert sess is not None
    text = identity.message_text(message)  # before blanking; in memory only
    group = await identity.group_rows(session, message, sess, text)
    # The rollback copies of the rewound originals Hermes wrote these rows again from (the importer's twin link, from
    # stored data: an in-place compaction's protected tail, a /retry), read while the texts are still in memory: a
    # rollback of that run must not put an original back with its text (no uid key to follow without uids, or in a
    # branch whose parent holds the uid key).
    rewound: set[int] = set(await identity.rewound_originals(session, message, text))
    for g in group.rows:
        if not g.row.withheld:
            rewound |= set(await identity.rewound_originals(session, g.row, identity.message_text(g.row)))
    matching = await identity.matching_rows(session, message, sess, exclude=[g.row.id for g in group.rows]) if (
        include_identical or keep_identical_readable) else []
    unticked = {int(i) for i in leave_readable}
    after = await identity.current_after_run(session)
    # The JARVIS backup this decision is taken on: the newest that wrote the conversation's family here. An import of
    # that very file replays it word for word once Settle finished it (0.6.2 plan 3.2, 4.4); a new decision on a
    # backup already finished needs finishing again (plan 4.7).
    decided_on = await identity.decision_backup(session, [sess.source_session_id])
    await identity.reopen_decision_backup(session, decided_on)
    uid = identity.row_uid(message)
    if hide_title is None:
        hide_title = await identity.first_user_message(session, message)
    written = 0
    if message.source_key:  # a row an import stored always has one; it keeps the title choice (later imports)
        written = int(await identity.put_stone(session, replace(identity.binding_of(
            message, sess.source_session_id, via=via, after_run=after, uid_known=bool(uid), decided_on=decided_on),
            title_hidden=bool(hide_title)), actor=actor))
    # The group's root: this message's own key, unless this row is already a stone that joined another admin's
    # withhold (decisions-apply after a restore, or an import, reached it first as a copy): then that withhold's key,
    # so a copy never becomes the root of the message it copies, and the rows below join the admin's own group.
    st0 = await session.get(BrainTombstone, message.source_key) if message.source_key else None
    root = (st0.anchor_key if st0 is not None and st0.reason == WITHHELD_BY_ADMIN and st0.anchor_key
            and st0.anchor_key != message.source_key else message.source_key)
    chosen: list[identity.GroupRow] = []
    out = Withheld(None, bool(written))
    for r, sid, rel in matching:
        if include_identical and r.id not in unticked:
            chosen.append(identity.GroupRow(r, sid, identity.MATCHING_HOLD, identity.PENDING, rel))
        elif root and r.source_message_id and sid:
            # The admin left this row readable (unticked, or every matching row): a decision that outlives the
            # stored rows (review 2, S21), an exemption of the group bound to its exact JARVIS row and the backup.
            await identity.put_exemption(session, identity.exact_of_row(r, sid), anchor=root, kind="left_readable",
                                         decided_on=decided_on, reason=LEFT_READABLE, decided_by=actor)
            out.left_readable += 1
    at = now().isoformat()
    blanked: list[ArchiveMessage] = []
    for g in group.rows + chosen:
        member = g.review == identity.CERTAIN
        if g.row.source_key:
            written += int(await identity.put_stone(session, replace(identity.binding_of(
                g.row, g.source_session_id, anchor=root if root != g.row.source_key else "", via=g.rule,
                review=g.review, after_run=after, uid_known=member and bool(identity.row_uid(g.row)),
                decided_on=decided_on), relations=() if member else g.relations), actor=actor))
        if g.row.withheld:
            continue  # withheld already (no text): its key joins the group, the row stays as it is
        flag: dict[str, Any] = {"at": at, "note": identity.NOTE, "via": g.rule}
        if not member:
            flag["review"] = identity.PENDING
        _blank(g.row, flag)
        blanked.append(g.row)
        if g.rule == identity.MATCHING_HOLD:
            out.identical_withheld += 1
            out.held_for_review.append(g.row.id)
        elif member:
            out.also_withheld.append(g.row.id)
        else:
            out.held_for_review.append(g.row.id)
    for st in group.rowless:
        written += int(await identity.put_stone(session, identity.Stone(**{
            **st.__dict__, "anchor": root if root != st.key else "", "after_run": after, "decided_on": decided_on}),
            actor=actor))
    _blank(message, {"at": at, "note": "withheld by an admin on the JARVIS Brain page; the text was removed"})
    hidden_ids: list[int] = []
    if hide_title:
        old_title = sess.title or ""
        sess.title = ""
        sess.flags = {**(sess.flags or {}), "title_withheld": True}
        out.title_hidden = True
        # Hermes carries the title on: to a continuation (clearing the parent's) and, as '<title> #N', to a branch;
        # each child that carries its parent's title is hidden too, down the lineage (a visited set ends a cycle).
        seen, frontier = {sess.source_session_id}, [(sess.source_session_id, old_title)]
        hidden_ids.append(sess.id)
        # A member that is ITS conversation's first user message (a branch parent's original, a rotation's parent)
        # names a title that can repeat the text too (Hermes derives a title from the first user message): hidden,
        # with that conversation's own title-carrying children, and kept by its stone, so a rollback, a restore or a
        # re-import hides it again (importer._title_hidden_for; review 2, S15).
        for g in group.members:
            other = await session.get(ArchiveSession, g.row.session_id)
            if (other is None or other.source_session_id in seen
                    or not await identity.first_user_message(session, g.row)):
                continue
            seen.add(other.source_session_id)
            frontier.append((other.source_session_id, other.title or ""))
            hidden_ids.append(other.id)
            other.title = ""
            other.flags = {**(other.flags or {}), "title_withheld": True}
            if g.row.source_key:
                await session.execute(update(BrainTombstone).where(BrainTombstone.source_key == g.row.source_key)
                                      .values(title_hidden=True))
        while frontier:
            nxt: list[tuple[str, str]] = []
            for parent_sid, parent_title in frontier:
                for child in (await session.scalars(select(ArchiveSession).where(
                        ArchiveSession.parent_source_session_id == parent_sid,
                        ArchiveSession.lineage.in_(identity.LINEAGE)).order_by(ArchiveSession.id))).all():
                    if child.source_session_id in seen:
                        continue
                    seen.add(child.source_session_id)
                    if identity.title_follows(child.title, child.lineage, parent_title):
                        nxt.append((child.source_session_id, child.title or ""))
                        hidden_ids.append(child.id)
                        child.title = ""
                        child.flags = {**(child.flags or {}), "title_withheld": True}
            frontier = nxt
        # The rollback copies of those conversations keep no title either (a rollback never brings it back).
        await session.execute(update(ImportRecord).where(
            ImportRecord.target_table == "archive_sessions", ImportRecord.target_id.in_(hidden_ids),
            ImportRecord.before.has_key("title"), ImportRecord.before["title"].astext != "")
            .values(before=ImportRecord.before.op("||")(sql_text("'{\"title\": \"\"}'::jsonb"))))
    await session.flush()
    # The rollback copies of an earlier row of it that no stored row holds any more (an import deleted a rewound
    # original kept under its uid key) lose the text too.
    uid_keys = sorted({identity.UID_KEY + u for u in {uid, *(identity.row_uid(g.row) for g in group.members
                                                            if g.rule not in identity.FOLD_VIAS)} if u})
    stored = set((await session.scalars(select(ArchiveMessage.source_key).where(
        ArchiveMessage.source_key.in_(uid_keys)))).all()) if uid_keys else set()
    rowless = [st.key for st in group.rowless] + [k for k in uid_keys if k not in stored]
    done = await withhold_copies(session, messages=[message, *blanked], source_keys=rowless, actor=actor, cause=cause,
                                 ip=ip)
    if hidden_ids:  # a history answer quoting another message of these conversations names them by the hidden title
        await relabel_hidden_titles(session, session_ids=hidden_ids)
    if rewound:
        done.rollback_copies_blanked += await _blank_records(session, [ImportRecord.id.in_(sorted(rewound))])
    for key, u in group.uid_records:  # deleted rows that only share the uid: the copy loses the text, no stone
        done.rollback_copies_blanked += await _blank_records(session, [and_(
            identity.DELETED_COPY, ImportRecord.source_key == key,
            or_(ImportRecord.source_key == identity.UID_KEY + u,
                identity.deleted_field("source_uid") == u))])
    for sid in sorted({r.session_id for r in blanked} - {message.session_id}):
        await recompute_session(session, sid)
    out.session = await recompute_session(session, message.session_id)
    out.copies, out.versions_blanked = done.copies, done.versions_blanked
    out.rollback_copies_blanked, out.answers_scrubbed = done.rollback_copies_blanked, done.answers_scrubbed
    out.keys_tombstoned = written
    return out


async def hold_message(session: AsyncSession, message: ArchiveMessage, *, anchor: str, actor: str, cause: str,
                       ip: str = "", rule: str = "same message id", review: str = identity.PENDING,
                       decided_on: str = "", relations: Iterable[str] = ()) -> Withheld:
    """Withholds a readable row for an admin's review in the group of the withhold `anchor` (decisions-apply, spec
    9.2): it shares the uid of a message withheld in a decisions file but is another JARVIS row, and nothing here can
    compare the texts; or the test copy held it for review (`rule`, the hold's own evidence). Its key gets a pending
    stone in that group, so the holds list shows it and the next import compares the text (a member, or a hold an
    admin keeps or releases). With review CERTAIN it joins the group as a member instead (a fold that absorbed the
    withheld message: it holds the text). What copied it goes as for a withhold."""
    sess = await session.get(ArchiveSession, message.session_id)
    assert sess is not None
    member = review != identity.PENDING
    written = 0
    if message.source_key:
        written = int(await identity.put_stone(session, replace(identity.binding_of(
            message, sess.source_session_id, anchor=anchor, via=rule, review=identity.CERTAIN if member
            else identity.PENDING, after_run=await identity.current_after_run(session), decided_on=decided_on),
            relations=() if member else tuple(relations)), actor=actor))
    flag: dict[str, Any] = {"at": now().isoformat(), "note": identity.NOTE, "via": rule}
    if not member:
        flag["review"] = identity.PENDING
    _blank(message, flag)
    await session.flush()
    done = await withhold_copies(session, messages=[message], actor=actor, cause=cause, ip=ip)
    return Withheld(await recompute_session(session, message.session_id), bool(written), done.copies,
                    done.versions_blanked, done.rollback_copies_blanked, done.answers_scrubbed,
                    also_withheld=[message.id] if member else [], held_for_review=[] if member else [message.id],
                    keys_tombstoned=written)


async def withhold_preview(session: AsyncSession, message: ArchiveMessage, sess: ArchiveSession,
                           scope: PersonScope) -> dict[str, Any]:
    """What withholding this message would do, read-only (spec 8.3, 0.6.2 plan 4.5): the other stored rows that are
    the same JARVIS message by provenance (withheld too), those held for review (the same uid, other text), the stored
    rows of its conversation family that equal, contain, restate or quote it (identity.matching_rows: listed
    pre-ticked with their evidence; unticking one leaves it readable as the admin's decision), how many copies have no
    row, the imported memory items in the admin's scope whose text contains it (to purge by hand; nothing is
    automatic), whether it is the conversation's first user message (its title is hidden by default), and the JARVIS
    backup the decision is taken on (Settle finishes it there). Ids, positions, relation codes and counts only, never
    text."""
    text = identity.message_text(message)
    group = await identity.group_rows(session, message, sess, text, lock=False)
    matching = await identity.matching_rows(session, message, sess, exclude=[g.row.id for g in group.rows])
    decided_on = await _decision_backup_read_only(session, sess)

    def brief(row: ArchiveMessage, relations: Iterable[str] = ()) -> dict[str, Any]:
        out = {"id": row.id, "session": row.session_id, "position": row.position, "role": row.role,
               "at": _iso(row.at), "state": row.state, "withheld": row.withheld}
        if relations:
            out["relations"] = identity.relation_order(relations)
            out["evidence"] = identity.evidence_text(relations)
        return out

    return {"message": message.id,
            "same_message_rows": [brief(g.row) for g in group.members],
            "held_rows": [brief(g.row, g.relations) for g in group.holds if not g.row.withheld],
            # pre-ticked (plan 4.5): withheld as holds unless the admin unticks them (leave_readable)
            "identical_text_rows": [{**brief(r, rel), "preselected": True} for r, _, rel in matching],
            "rowless_copies": len(group.rowless),
            "items_with_this_text": await identity.items_with_text(session, message, person_items(scope)),
            "first_user_message": await identity.first_user_message(session, message),
            "decision_backup": decided_on}


async def _decision_backup_read_only(session: AsyncSession, sess: ArchiveSession) -> dict[str, Any] | None:
    """The JARVIS backup a withhold of this conversation is decided on (its label and the start of its SHA-256), read
    only (identity.decision_backup without writing its record)."""
    family = await identity.lineage(session, [sess.source_session_id])
    ids = list((await session.scalars(select(ArchiveSession.id).where(
        ArchiveSession.source_session_id.in_(sorted(family))))).all())
    if not ids:
        return None
    run = (await session.execute(select(ImportRun.label, ImportRun.source).join(
        ImportRecord, ImportRecord.run_id == ImportRun.id).where(
        ImportRecord.record_type == "session", ImportRecord.target_table == "archive_sessions",
        ImportRecord.target_id.in_(ids), ImportRun.status != "rolled_back").order_by(ImportRun.id.desc()).limit(1))
           ).first()
    if run is None:
        return None
    sha = identity.run_sha256(run.source)
    return {"label": run.label or "", "sha256": sha[:12]} if sha else None


# --- Holds: rows withheld for an admin's review (spec 8.2) ------------------------------------------------------

# The holds groups of the Review tab (0.6.2 plan 9.2, 9.3): by the engine that held the row.
HOLD_GROUPS = {identity.SETTLE_HOLD: "settle", identity.LATER_HOLD: "later"}


async def decision_status(session: AsyncSession) -> dict[str, Any]:
    """The withhold decisions' counters (plan 1.8, 9.4) for admins: withholds whose decision backup is not finished
    yet (Settle), holds Settle found that wait for review, rows a later backup held, every held row, conversations an
    admin stopped importing, the legacy lock, and the newest import's tripwire as counts (its locations stay in
    `brain-import status`). Counts only; brain_withhold_pending() (0008)."""
    if not await session.scalar(sql_text("SELECT to_regprocedure('brain_withhold_pending()') IS NOT NULL")):
        return {}
    p = dict((await session.execute(sql_text("SELECT brain_withhold_pending()"))).scalar() or {})
    tw = p.get("last_tripwire") if isinstance(p.get("last_tripwire"), dict) else None
    return {"unsettled_decisions": int(p.get("unsettled_decisions") or 0),
            "unreviewed_settle_holds": int(p.get("unreviewed_settle_holds") or 0),
            "later_backup_holds": int(p.get("quarantined_rows") or 0),
            "held_for_review": int(p.get("held_for_review") or 0),
            "frozen_conversations": int(p.get("frozen_sessions") or 0),
            "legacy_lock": bool(p.get("legacy_lock")),
            "tripwire": {"run": tw.get("run"), "inside_family": int(tw.get("inside_family") or 0),
                         "outside_family": int(tw.get("outside_family") or 0)} if tw else None}

# A hold written before its evidence was computed (a development build's via): what that via says, as relation codes.
LEGACY_RELATIONS = {"same text": (identity.IDENTICAL_REL,), "upgrade: same text": (identity.IDENTICAL_REL,),
                    "same message id, contains the withheld text": (identity.SAME_UID, identity.CONTAINS),
                    "restated after a compaction, new message id": (identity.RESTATED_REL,),
                    "copied by JARVIS, nothing to compare": (identity.SAME_TIME_REL, identity.NOT_IN_BACKUP),
                    "same conversation, role and time; nothing to compare": (identity.SAME_TIME_REL,
                                                                            identity.NOT_IN_BACKUP),
                    "same message id, nothing to compare": (identity.SAME_UID, identity.NOT_IN_BACKUP)}
CONFIRM_NEEDED = ("Releasing this row needs a typed confirmation: its text is identical to the withheld message, "
                  "contains it or quotes part of it. To release it, send '%s' in the field 'confirm'."
                  % identity.CONFIRM_WORD)


def hold_relations(stone: BrainTombstone) -> list[str]:
    """A hold's computed evidence (relation codes, plan 4.6); for a hold of a development build, what its via says."""
    found = identity.relation_order(stone.relations or [])
    if found:
        return found
    if stone.via in LEGACY_RELATIONS:
        return list(LEGACY_RELATIONS[stone.via])
    if (stone.via or "").startswith(("same message id", "upgrade: same message id")):
        return [identity.SAME_UID]
    return []


async def holds(session: AsyncSession, scope: PersonScope) -> list[dict[str, Any]]:
    """Every withheld row held for an admin's review (its stone is pending) in the admin's scope, with the evidence:
    where it is, its length and state, the rule that held it, its computed relations to the withheld message (plan
    4.6: identical, contains, part of a reply quote, restated, the same message id, tool call or time with other text,
    a reused row number, a stale stored row, not in this backup, none) and their words, whether a release needs a
    typed confirmation (its text is, holds or quotes the withheld text), whether it looks independent (pre-selected
    for release: its own uid, another time, no copy position), the withheld message it may be (its position, role,
    time and length), and the readable messages around it. Never text."""
    found = (await session.execute(
        select(ArchiveMessage, ArchiveSession, BrainTombstone)
        .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
        .join(BrainTombstone, BrainTombstone.source_key == ArchiveMessage.source_key)
        .where(BrainTombstone.reason == WITHHELD_BY_ADMIN, BrainTombstone.review == identity.PENDING,
               ArchiveMessage.withheld.is_(True), person_sessions(scope), person_messages(scope))
        .order_by(ArchiveSession.started_at.desc().nulls_last(), ArchiveSession.id, ArchiveMessage.position,
                  ArchiveMessage.id))).all()
    frozen = set((await session.scalars(select(BrainFrozenSession.source_session_id))).all())
    out = []
    for row, sess, stone in found:
        anchor = stone.anchor_key or stone.source_key
        # The withheld message is described only when the admin may open it (an admin without the sensitive-data
        # permission never learns the id, position, time or length of a Credit Repair or restricted row).
        original = await session.scalar(select(ArchiveMessage).join(
            ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id).where(
            ArchiveMessage.source_key == anchor, person_sessions(scope), person_messages(scope)))
        anchor_stone = await session.get(BrainTombstone, anchor)
        readable = and_(ArchiveMessage.session_id == row.session_id, ArchiveMessage.withheld.is_(False),
                        person_messages(scope))
        before = await session.scalar(select(ArchiveMessage.id).where(readable, or_(
            ArchiveMessage.position < row.position, and_(ArchiveMessage.position == row.position,
                                                         ArchiveMessage.id < row.id)))
            .order_by(ArchiveMessage.position.desc(), ArchiveMessage.id.desc()).limit(1))
        after = await session.scalar(select(ArchiveMessage.id).where(readable, or_(
            ArchiveMessage.position > row.position, and_(ArchiveMessage.position == row.position,
                                                         ArchiveMessage.id > row.id)))
            .order_by(ArchiveMessage.position, ArchiveMessage.id).limit(1))
        hidden = bool((sess.flags or {}).get("title_withheld"))
        relations = hold_relations(stone)
        own_uid = identity.row_uid(row)
        copy_position = (sess.lineage in identity.LINEAGE and sess.started_at is not None and row.at is not None
                         and row.at <= sess.started_at)
        w_uid = anchor_stone.message_uid if anchor_stone is not None else ""
        w_at = anchor_stone.source_at if anchor_stone is not None else None
        out.append({
            "message": row.id, "session": sess.id, "title": None if hidden else sess.title,
            "position": row.position, "role": row.role, "at": _iso(row.at), "content_chars": row.content_chars,
            "state": row.state, "rule": stone.via,
            "withheld_message": {
                "id": original.id if original is not None else None,
                "position": original.position if original is not None else None,
                "role": original.role if original is not None else (anchor_stone.role if anchor_stone else ""),
                "at": _iso(original.at) if original is not None else None,
                "content_chars": original.content_chars if original is not None else None},
            "relations": relations,
            "compare": identity.compare_text(relations),
            "evidence": identity.evidence_text(relations) if relations else "the text could not be compared",
            "confirm_required": identity.needs_confirmation(relations),
            "preselected": bool(own_uid) and own_uid != w_uid and (w_at is None or row.at != w_at)
                           and not copy_position,
            "decided_on": (stone.decided_on or "")[:12],
            # the Review tab's group (plan 9.2, 9.3) and whether later backups still import this conversation
            "group": HOLD_GROUPS.get(stone.via, "other"),
            "frozen": sess.source_session_id in frozen,
            "neighbours": [i for i in (before, after) if i is not None]})
    return out


async def _hold(session: AsyncSession, message: ArchiveMessage) -> BrainTombstone:
    stone = await session.get(BrainTombstone, message.source_key, with_for_update=True) if message.source_key else None
    if (not message.withheld or stone is None or stone.reason != WITHHELD_BY_ADMIN
            or stone.review != identity.PENDING):
        raise BrainError("this message is not held for review")
    return stone


async def keep_hold(session: AsyncSession, message: ArchiveMessage, *, actor: str) -> BrainTombstone:
    """An admin keeps a held row withheld: its stone becomes permanent (review 'kept', a member from then on)."""
    stone = await _hold(session, message)
    stone.review = identity.KEPT
    at = now()
    # who kept it, and when, on the stone itself: a row an import stores again later keeps it (review 3, rv3-dur-6)
    stone.kept_by, stone.kept_at = actor[:200], at
    flag = dict((message.flags or {}).get("withheld_by_admin") or {})
    flag.update(review=identity.KEPT, kept_at=at.isoformat(), kept_by=actor[:200])
    message.flags = {**(message.flags or {}), "withheld_by_admin": flag}
    await session.flush()
    return stone


async def release_hold(session: AsyncSession, message: ArchiveMessage, sess: ArchiveSession, *,
                       actor: str, decided_by: str | None = None, decided_at: datetime | None = None,
                       reason: str = "released by an admin", kind: str = "release",
                       confirm: str | None = None, confirmed: bool = False) -> BrainTombstone:
    """An admin releases a held row: its pending stone goes and an exemption names its JARVIS row for that group, so
    the hold rules (the same uid with other text, the same text, the upgrade heuristics) skip it; the next import of
    a backup holding that row brings its text back (importer release branch). Refused for a row a certain rule
    names (a stone that is not a hold binds its JARVIS row): it is the withheld message itself. Promoted copies
    emptied while it was held stay empty."""
    stone = await _hold(session, message)
    if (identity.needs_confirmation(hold_relations(stone)) and not confirmed
            and (confirm or "").strip() != identity.CONFIRM_WORD):
        # plan 4.6: a hold whose text is, holds or quotes the withheld text is released only on a typed confirmation
        raise BrainError(CONFIRM_NEEDED)
    here = identity.exact_of_row(message, sess.source_session_id)
    stones = [identity._stone(r) for r in (await session.scalars(select(BrainTombstone).where(
        BrainTombstone.reason == WITHHELD_BY_ADMIN, BrainTombstone.source_session_id == here.session,
        BrainTombstone.source_row_id == here.row))).all()]
    exact_stones = [st for st in stones if identity.same_row(identity.exact_of_stone(st), here)]
    if any(st.certain for st in exact_stones):
        raise BrainError("it is the same message as a withheld one: it cannot be released")
    anchor = stone.anchor_key or stone.source_key
    # Every hold of this exact JARVIS row in this group (a key adoption can leave the old key's hold binding it too).
    gone = sorted({message.source_key} | {st.key for st in exact_stones if st.group == anchor})
    await session.execute(delete(BrainTombstone).where(
        BrainTombstone.reason == WITHHELD_BY_ADMIN, BrainTombstone.review == identity.PENDING,
        func.coalesce(func.nullif(BrainTombstone.anchor_key, ""), BrainTombstone.source_key) == anchor,
        BrainTombstone.source_key.in_(gone)))
    # Bound to this exact JARVIS row (identity.exact: session, row id, role, tool_call_id, time, uid) and to a backup:
    # another message at a reused row id is never covered. An admin's release here is decided on the newest backup
    # that wrote the conversation's family (the content the next import of it restores; a hold found in an older
    # backup is reviewed now); decisions-apply keeps the backup the file names. Who decided it and when: the admin
    # now, or (decisions-apply after a restore) the admin and time the restore file names (review 2, S23).
    by, when = (decided_by or actor)[:200], decided_at or now()
    newest = await identity.decision_backup(session, [sess.source_session_id]) if decided_by is None else ""
    await identity.put_exemption(session, here, anchor=anchor, kind=kind,
                                 decided_on=newest or stone.decided_on or await identity.decision_backup(
                                     session, [sess.source_session_id]),
                                 reason=reason, decided_by=by, decided_at=when)
    flag = dict((message.flags or {}).get("withheld_by_admin") or {})
    flag.update(released_at=when.isoformat(), released_by=by)
    message.flags = {**(message.flags or {}), "withheld_by_admin": flag}
    message.source_fp = ""  # its release marker: the next import restores it from the source
    await session.flush()
    return stone


NOT_COMPARABLE = ("this conversation has a hold whose withheld text was not in the JARVIS backup it came from ('the "
                  "withheld text is not in this backup'): nothing can say which rows have no relation to it, so the "
                  "one-click release is not offered; review its rows one by one, or stop importing it")


async def release_unrelated(session: AsyncSession, sess: ArchiveSession, scope: PersonScope, *,
                            actor: str) -> list[int]:
    """'Release this conversation's rows with no relation' (0.6.2 plan 9.3, section 2 correction 4): every hold of the
    conversation (in the admin's scope) whose computed evidence is exactly 'none' (no relation to the withheld text it
    is listed under, which the backup held: later.strongest lists a row under a withheld text the backup lacked
    first) is released as release_hold() releases it. Offered only when the backup held every withheld text of the
    family: refused (BrainError, nothing changed) while a hold of this conversation says 'not in this backup'. A hold
    with any relation, or none computed (an older hold), stays for the admin. The released message ids."""
    found = (await session.execute(
        select(ArchiveMessage, BrainTombstone)
        .join(BrainTombstone, BrainTombstone.source_key == ArchiveMessage.source_key)
        .where(ArchiveMessage.session_id == sess.id, ArchiveMessage.withheld.is_(True),
               BrainTombstone.reason == WITHHELD_BY_ADMIN, BrainTombstone.review == identity.PENDING,
               person_messages(scope))
        .order_by(ArchiveMessage.position, ArchiveMessage.id).with_for_update(of=ArchiveMessage))).all()
    if any(identity.NOT_IN_BACKUP in hold_relations(stone) for _, stone in found):
        raise BrainError(NOT_COMPARABLE)
    out = []
    for message, stone in found:
        if hold_relations(stone) != [identity.NO_RELATION]:
            continue
        await release_hold(session, message, sess, actor=actor,
                           reason="released by an admin (no relation to the withheld text)")
        out.append(message.id)
    return out


async def stop_importing(session: AsyncSession, sess: ArchiveSession, *, actor: str) -> bool:
    """'Stop importing this conversation' (0.6.2 plan 9.3, 5.3): a later backup's import leaves this JARVIS
    conversation out (counted), and its stored rows stay as they are. The decision backup's replay and Settle still
    read it. Kept by brain_frozen_sessions (a decisions file carries it). Whether it was added now."""
    return bool((await session.execute(pg_insert(BrainFrozenSession).values(
        source_session_id=sess.source_session_id[:200], frozen_by=actor[:200], frozen_at=now())
        .on_conflict_do_nothing())).rowcount)


# --- Items ------------------------------------------------------------------------------------------------------


async def redact_version(session: AsyncSession, item: MemoryItem, version: int
                         ) -> tuple[MemoryItemVersion, int] | None:
    """Blanks an older version's title, body, tags and note: (the version, how many answers quoting the item were
    changed). None when the version does not exist."""
    v = await session.scalar(select(MemoryItemVersion).where(MemoryItemVersion.item_id == item.id,
                                                             MemoryItemVersion.version == version))
    if v is None:
        return None
    if version == item.version:
        raise BrainError("this is the item's current text: edit (or delete) the item first; the edit keeps this text "
                         "as an older version, which can then be redacted")
    if is_redacted(v):
        raise BrainError("this version is already redacted")
    v.title, v.body, v.tags, v.note = REDACTED_TITLE, "", [], REDACTED_NOTE
    await redact_conflict_copies(session, item.id, {version})
    await session.flush()
    return v, await scrub_quotes(session, item_ids=[item.id])  # a history answer may quote the old text


def _conflict_versions(c: MemoryConflict, item_id: int) -> tuple[int | None, set[int]]:
    """The item versions a source_changed conflict copied: the one it replaced (existing.version) and, once resolved
    with use_incoming, the one it brought (its effects' version_after)."""
    ex = c.existing or {}
    before = ex.get("version") if isinstance(ex.get("version"), int) else None
    after = {int(e["version_after"]) for e in (c.effects or {}).get("items") or []
             if isinstance(e, dict) and e.get("id") == item_id and isinstance(e.get("version_after"), int)}
    return before, after


async def redact_conflict_copies(session: AsyncSession, item_id: int, versions: set[int]) -> int:
    """A source_changed conflict keeps the title and the plain SHA-256 of the version it replaced (existing) and of
    the version it brought (incoming). When an admin redacts that version, both go (review 3, readers: the redacted
    bytes stayed confirmable offline from the conflict, which GET /api/brain/conflicts returned). A rollback of that
    conflict is refused once its version_before is redacted, so nothing needs them any more. How many conflicts
    changed."""
    changed = 0
    for c in (await session.scalars(select(MemoryConflict).where(
            MemoryConflict.kind == "source_changed", MemoryConflict.existing_table == "memory_items",
            MemoryConflict.existing_id == item_id).order_by(MemoryConflict.id))).all():
        before, after = _conflict_versions(c, item_id)
        hit = False
        for side, take in (("existing", before in versions), ("incoming", bool(after & versions))):
            data = dict(getattr(c, side) or {})
            if take and (data.get("source_sha256") or data.get("title", REDACTED_TITLE) != REDACTED_TITLE):
                data["source_sha256"] = ""
                if "title" in data:
                    data["title"] = REDACTED_TITLE
                setattr(c, side, data)
                hit = True
        changed += hit
    if changed:
        await session.flush()
    return changed


async def redact_all_conflict_copies(session: AsyncSession) -> int:
    """redact_conflict_copies() for every version redacted before it existed (the start-up check). Counts only."""
    changed = 0
    items = (await session.scalars(select(MemoryConflict.existing_id).where(
        MemoryConflict.kind == "source_changed", MemoryConflict.existing_table == "memory_items",
        MemoryConflict.existing_id.is_not(None)).distinct())).all()
    for item_id in sorted(int(i) for i in items):
        redacted = {int(v) for v in (await session.scalars(select(MemoryItemVersion.version).where(
            MemoryItemVersion.item_id == item_id, MemoryItemVersion.title == REDACTED_TITLE,
            MemoryItemVersion.body == ""))).all()}
        if redacted:
            changed += await redact_conflict_copies(session, item_id, redacted)
    return changed


def _blocks_purge(effects: dict[str, Any], item: MemoryItem, own: set[int]) -> bool:
    """Whether a decided conflict's effects stop the purge of an item it names: purging deletes the conflict, and
    with it what a rollback of its import run reverts. A decision that changed nothing (dismissed, keep_both) never
    blocks; neither does one that changed only this item once its source message was withheld (a rollback leaves an
    emptied item as it is), nor an effect on an item the conflict's own run created (`own`: keep_existing rejects the
    incoming item; a rollback skips that effect and deletes the item anyway)."""
    if effects.get("sites") or effects.get("drafts"):
        return True
    ids = {e.get("id") for e in effects.get("items") or [] if isinstance(e, dict)} - own
    return bool(ids - {item.id}) or (item.id in ids and not (item.flags or {}).get("source_withheld"))


def _effect_items(effects: dict[str, Any]) -> list[int]:
    return [e["id"] for e in effects.get("items") or [] if isinstance(e, dict) and isinstance(e.get("id"), int)]


async def purge(session: AsyncSession, item: MemoryItem, *, actor: str, purged_at: datetime | None = None
                ) -> dict[str, Any]:
    """Deletes a soft-deleted item, its versions and its conflicts, and tombstones its source key so a later import
    does not bring it back. An item promoted from a message has no source key, so no tombstone: the audit entry
    records the purge, and the message itself is withheld separately (never by a purge). Every purge, with or without
    a source key, is kept in brain_purged_items (the item's id and creation time, no text): restore.ps1 carries it, so
    a restore of an older dump that brings the item back purges it again (decisions-apply --removals-only, which
    passes the purge's own time as purged_at)."""
    if item.deleted_at is None:
        raise BrainError("delete the item first; only deleted items can be purged")
    names = or_(MemoryConflict.incoming_item_id == item.id,
                and_(MemoryConflict.existing_table == "memory_items", MemoryConflict.existing_id == item.id))
    decided: set[int] = set()
    for c in (await session.scalars(select(MemoryConflict).join(ImportRun, ImportRun.id == MemoryConflict.run_id)
                                    .where(names, MemoryConflict.status != "open",
                                           ImportRun.status != "rolled_back"))).all():
        effects = c.effects or {}
        ids = _effect_items(effects)
        own = set((await session.scalars(select(MemoryItem.id).where(
            MemoryItem.id.in_(ids), MemoryItem.import_run_id == c.run_id))).all()) if ids else set()
        if _blocks_purge(effects, item, own):
            decided.add(c.run_id)
    if decided:  # the decision's effects are what a rollback of that import reverts: they must stay
        raise BrainError("a conflict decision of import run %s names this item; roll that import back first, or keep "
                         "the item deleted" % ", ".join(str(r) for r in sorted(decided)))
    versions = int(await session.scalar(select(func.count()).select_from(MemoryItemVersion).where(
        MemoryItemVersion.item_id == item.id)) or 0)
    conflicts = (await session.execute(delete(MemoryConflict).where(names))).rowcount
    replaces = (item.flags or {}).get("replaces_source_key")
    if replaces and item.source_sha256:  # a newer copy of a changed file: the next import must not offer it again
        existing = await session.scalar(select(MemoryItem).where(MemoryItem.source_key == replaces))
        if existing is not None:
            flags = dict(existing.flags or {})
            rejected = list(flags.get("rejected_source_sha256") or [])
            if item.origin == "import" and identity.purge_key() is not None:
                # Its keyed fingerprint (remember_purge, below) keeps it out at the next import (the existing item and
                # its key stay as they are): no plain hash of purged bytes is kept, also not one an earlier
                # keep_existing put there (review 2, S16).
                if item.source_sha256 in rejected:
                    rejected = [h for h in rejected if h != item.source_sha256]
                    if rejected:
                        flags["rejected_source_sha256"] = rejected
                    else:
                        flags.pop("rejected_source_sha256", None)
                    existing.flags = flags
            elif item.source_sha256 not in rejected:  # no database password: no key, the plain hash keeps it out
                flags["rejected_source_sha256"] = rejected + [item.source_sha256]
                existing.flags = flags
    tombstone = False
    if item.source_key:
        if await session.get(BrainTombstone, item.source_key) is None:
            session.add(BrainTombstone(source_key=item.source_key, purged_at=purged_at or now(),
                                       purged_by=actor[:200], reason="purged by an admin"))
        tombstone = True
    if item.origin == "import" and item.source_sha256:
        # The keyed fingerprint of its bytes (never a plain hash): a later import recognises the same text under
        # another key (a renamed or copied file, an incoming version) and leaves it out too.
        await identity.remember_purge(session, source_key=item.source_key or "", sha256_hex=item.source_sha256,
                                      kind=identity.purge_kind(item.source_key or replaces or ""), actor=actor,
                                      via="purge")
    await session.execute(pg_insert(BrainPurgedItem).values(
        item_id=item.id, item_created_at=item.created_at, origin=(item.origin or "")[:40],
        replaces_source_key=str(replaces or "")[:500], purged_at=purged_at or now(), purged_by=actor[:200])
        .on_conflict_do_nothing())
    await store.purge_item(session, item)
    answers = await scrub_quotes(session, item_ids=[item.id])
    return {"versions_removed": versions, "conflicts_removed": int(conflicts or 0), "tombstone": tombstone,
            "answers_scrubbed": answers}


async def export_items(session: AsyncSession, scope: PersonScope, *, include_customer: bool) -> dict[str, Any]:
    """Approved, non-deleted items in the admin's scope: general ones only unless customer items are asked for;
    Credit Repair items only with the sensitive-data permission; never one promoted from a withheld message."""
    where = and_(person_items(scope), MemoryItem.status == "approved",
                 not_(MemoryItem.flags.has_key("source_withheld")), not_(withheld_source()))
    if not include_customer:
        where = and_(where, MemoryItem.sensitivity == "general")
    items = (await session.scalars(select(MemoryItem).where(where).order_by(MemoryItem.id))).all()
    return {"format": "jarvis-brain-memory-export", "version": 1, "exported_at": now().isoformat(),
            "include_customer": include_customer, "count": len(items), "items": [item_out(i) for i in items]}


# --- Conflicts --------------------------------------------------------------------------------------------------


async def conflict_items(session: AsyncSession, c: MemoryConflict) -> tuple[MemoryItem | None, MemoryItem | None]:
    existing = (await session.get(MemoryItem, c.existing_id)
                if c.existing_table == "memory_items" and c.existing_id else None)
    incoming = await session.get(MemoryItem, c.incoming_item_id) if c.incoming_item_id else None
    return existing, incoming


async def conflict_needs_admin(session: AsyncSession, c: MemoryConflict, resolution: str | None = None) -> bool:
    """instruction, source_changed and workflow conflicts need an admin. So does a resolution that changes an item
    whose approval needs an admin (rejecting the incoming item, or archiving the existing one)."""
    if c.kind in ADMIN_CONFLICTS:
        return True
    if c.kind == "memory_item":
        existing, incoming = await conflict_items(session, c)
        if resolution in (None, "keep_existing") and incoming is not None and item_needs_admin(incoming):
            return True
        if resolution in (None, "use_incoming") and existing is not None and item_needs_admin(existing):
            return True
    return False


def _empty_effects() -> dict[str, list[dict[str, Any]]]:
    return {"items": [], "sites": [], "drafts": [], "purged_items": []}


def _item_effect(effects: dict[str, list[dict[str, Any]]], item: MemoryItem, status_before: str,
                 version_before: int, **extra: Any) -> None:
    effects["items"].append({"id": item.id, "status_before": status_before, "version_before": version_before,
                             "version_after": item.version, **extra})


async def _set_item_status(session: AsyncSession, effects: dict[str, list[dict[str, Any]]], item: MemoryItem | None,
                           decision: str, *, actor: str, note: str) -> None:
    """Reject or archive an item (a new version), recording the effect. A deleted item, or one already in that
    state, is left alone."""
    if item is None or item.deleted_at is not None or item.status == store.REVIEW[decision][0]:
        return
    status_before, version_before = item.status, item.version
    await store.review_item(session, item, decision=decision, actor=actor, note=note)
    _item_effect(effects, item, status_before, version_before)


def _draft_before(draft: WorkflowDraft, fields: tuple[str, ...]) -> dict[str, Any]:
    return {k: _iso(getattr(draft, k)) for k in fields}


async def resolve_conflict(session: AsyncSession, settings: Settings, c: MemoryConflict, resolution: str, *,
                           actor: str, note: str = "") -> dict[str, list[dict[str, Any]]]:
    """Applies a person's decision on an open conflict and records what it changed in `effects` (the format in
    importer.py's docstring), so rolling back the import run that raised the conflict can revert it."""
    if resolution not in RESOLUTIONS:
        raise BrainError("resolution must be keep_existing, use_incoming, keep_both or dismissed")
    if c.status != "open":
        raise BrainError("only open conflicts can be resolved; this one was already decided (%s)" % c.status)
    effects = _empty_effects()
    why = "conflict %d: %s" % (c.id, resolution.replace("_", " "))
    if resolution in ("keep_existing", "use_incoming"):
        handler = {"memory_item": _memory_item, "source_changed": _source_changed, "instruction": _instruction,
                   "site": _site, "schedule": _schedule, "workflow": _workflow}.get(c.kind)
        if handler is None:
            raise BrainError("unknown conflict kind '%s'" % c.kind)
        await handler(session, settings, c, resolution, effects, actor, why)
    c.status = resolution
    c.decided_by, c.decided_at, c.note = actor[:200], now(), note
    c.effects = effects
    await session.flush()
    return effects


async def _memory_item(session: AsyncSession, settings: Settings, c: MemoryConflict, resolution: str,
                       effects: dict[str, list[dict[str, Any]]], actor: str, why: str) -> None:
    existing, incoming = await conflict_items(session, c)
    if resolution == "keep_existing":
        await _set_item_status(session, effects, incoming, "reject", actor=actor, note=why)
    else:  # the existing item is archived; the incoming one stays pending
        await _set_item_status(session, effects, existing, "archive", actor=actor, note=why)


async def _instruction(session: AsyncSession, settings: Settings, c: MemoryConflict, resolution: str,
                       effects: dict[str, list[dict[str, Any]]], actor: str, why: str) -> None:
    if resolution == "keep_existing":
        _, incoming = await conflict_items(session, c)
        await _set_item_status(session, effects, incoming, "reject", actor=actor, note=why)
    # use_incoming: the item stays a pending reference; approving it is a separate, admin-only review.


async def _source_changed(session: AsyncSession, settings: Settings, c: MemoryConflict, resolution: str,
                          effects: dict[str, list[dict[str, Any]]], actor: str, why: str) -> None:
    existing, incoming = await conflict_items(session, c)
    if resolution == "keep_existing":
        await _set_item_status(session, effects, incoming, "reject", actor=actor, note=why)
        sha = str((c.incoming or {}).get("source_sha256") or (incoming.source_sha256 if incoming else ""))
        if existing is not None and sha:
            flags = dict(existing.flags or {})
            rejected = list(flags.get("rejected_source_sha256") or [])
            if sha not in rejected:
                flags["rejected_source_sha256"] = rejected + [sha]
                existing.flags = flags
                # No new version: a rollback only takes the hash out again.
                _item_effect(effects, existing, existing.status, existing.version, rejected_source_sha256=sha)
        return
    if existing is None or existing.deleted_at is not None:
        raise BrainError("the existing item was deleted: restore it first, or resolve with keep_both")
    if incoming is None:
        raise BrainError("the incoming version is no longer there: resolve with keep_existing or dismissed")
    status_before, version_before, flags_before = existing.status, existing.version, dict(existing.flags or {})
    flags = {k: v for k, v in (incoming.flags or {}).items() if k not in ("replaces_source_key", "conflict")}
    for k in ("rejected_source_sha256", "conflict"):
        if k in (existing.flags or {}):
            flags[k] = existing.flags[k]
    existing.title, existing.body, existing.flags = incoming.title, incoming.body, flags
    # Never lower: a rollback restores the old text but not the sensitivity.
    existing.sensitivity = brain_text.most_sensitive(existing.sensitivity, incoming.sensitivity)
    existing.source_sha256, existing.source_at = incoming.source_sha256, incoming.source_at
    existing.content_hash = brain_text.content_hash(existing.kind, existing.agent, existing.title, existing.body)
    existing.status = "quarantined" if incoming.status == "quarantined" else "pending_review"
    existing.version += 1
    existing.updated_at, existing.updated_by = now(), actor[:200]
    session.add(store._snapshot(existing, "edited", actor, why + ": took the changed JARVIS version"))
    # The flags go with the text: a rollback that brings the old text back brings its risk flags back too.
    _item_effect(effects, existing, status_before, version_before, flags_before=flags_before)
    # The conflict outlives the incoming item (its foreign key would delete it), so a rollback still finds it.
    c.incoming_item_id = None
    await session.flush()
    effects["purged_items"].append({"id": incoming.id, "source_key": incoming.source_key})
    await session.delete(incoming)
    await session.flush()


async def _site(session: AsyncSession, settings: Settings, c: MemoryConflict, resolution: str,
                effects: dict[str, list[dict[str, Any]]], actor: str, why: str) -> None:
    if resolution == "keep_existing":
        return  # nothing is registered
    url = str((c.incoming or {}).get("url") or "")
    host = str((c.incoming or {}).get("host") or urllib.parse.urlsplit(url).hostname or "")
    if not url.startswith("https://") or not host:
        raise BrainError("this conflict holds no usable website address")
    found = await session.scalar(select(Site.id).where(Site.url == url))
    if found is not None:
        raise BrainError("that website is already registered (site %d): resolve with keep_existing or dismissed"
                         % found)
    ent = await licensing.current_entitlements(session, settings)
    count = int(await session.scalar(select(func.count()).select_from(Site)) or 0)
    if ent.max_sites is not None and count >= ent.max_sites:
        raise BrainError("the %s edition monitors up to %d sites: the site was not registered"
                         % (ent.edition.title, ent.max_sites))
    at = now()
    site = Site(name=host[:200], url=url, enabled=False, environment="production", created_at=at, updated_at=at,
                notes="Registered paused from JARVIS's website list (%s). Enable it on the Websites page when it "
                      "should be monitored." % why)
    try:
        async with session.begin_nested():
            session.add(site)
            await session.flush()
    except IntegrityError:
        raise BrainError("that website is already registered: resolve with keep_existing or dismissed") from None
    effects["sites"].append({"id": site.id, "updated_at_after": at.isoformat()})


async def _schedule(session: AsyncSession, settings: Settings, c: MemoryConflict, resolution: str,
                    effects: dict[str, list[dict[str, Any]]], actor: str, why: str) -> None:
    if resolution != "keep_existing":
        return  # the draft stays a draft; converting it is a separate admin action
    draft_id = (c.incoming or {}).get("draft_id")
    draft = await session.get(WorkflowDraft, int(draft_id)) if isinstance(draft_id, int) else None
    if draft is None or draft.status in ("converted", "rejected"):
        return
    before = _draft_before(draft, ("status", "reviewed_by", "reviewed_at"))
    draft.status, draft.reviewed_by, draft.reviewed_at, draft.updated_at = "rejected", actor[:200], now(), now()
    effects["drafts"].append({"id": draft.id, "before": before, "fp_after": draft.source_fp})


async def _workflow(session: AsyncSession, settings: Settings, c: MemoryConflict, resolution: str,
                    effects: dict[str, list[dict[str, Any]]], actor: str, why: str) -> None:
    if resolution == "keep_existing":
        return  # the draft keeps its content; the incoming JSON is not applied
    draft = await session.get(WorkflowDraft, c.existing_id) if c.existing_id else None
    if draft is None:
        raise BrainError("the draft is no longer there: resolve with dismissed")
    if draft.status == "converted":
        raise BrainError("the draft was converted to a schedule, and a converted draft is never changed: resolve with "
                         "keep_existing or dismissed, and change the schedule itself if needed")
    incoming = {k: v for k, v in (c.incoming or {}).items() if k in DRAFT_FIELDS}
    if not incoming.get("name") or not incoming.get("source_fp"):
        raise BrainError("this conflict holds no usable draft")
    if incoming.get("compatibility", draft.compatibility) not in COMPATIBILITY or \
            incoming.get("trigger", draft.trigger) not in TRIGGERS:
        raise BrainError("this conflict's draft has an unknown compatibility or trigger")
    before = _draft_before(draft, DRAFT_FIELDS + ("status", "reviewed_by", "reviewed_at"))
    for k, v in incoming.items():
        setattr(draft, k, v)
    # New content is reviewed again before anyone converts it.
    draft.status, draft.reviewed_by, draft.reviewed_at, draft.updated_at = "draft", "", None, now()
    effects["drafts"].append({"id": draft.id, "before": before, "fp_after": draft.source_fp})


# --- Workflow drafts --------------------------------------------------------------------------------------------


def _cron_ok(cron: str) -> bool:
    try:
        return bool(cron) and len(cron.split()) == 5 and valid_cron(cron)
    except Exception:  # croniter raises several types for odd input
        return False


def validate_params(agent: str, capability: str, params: dict[str, Any] | None) -> dict[str, Any]:
    """The parameters as the capability's own model accepts them (the same check app/tasks.py makes)."""
    try:
        cap = task_service.resolve_capability(agent, capability)
    except task_service.TaskError as exc:
        raise BrainError(str(exc)) from None
    try:
        validated = cap.params_model.model_validate(params or {})
    except ValidationError as exc:
        problems = "; ".join("%s: %s" % (".".join(str(p) for p in e.get("loc", ())) or "parameters", e.get("msg", ""))
                             for e in exc.errors(include_url=False))
        raise BrainError("the parameters do not suit %s.%s: %s" % (agent, capability, problems)) from None
    return validated.model_dump(mode="json")


async def schedule_exists(session: AsyncSession, draft: WorkflowDraft) -> bool:
    return draft.schedule_id is not None and await session.get(Schedule, draft.schedule_id) is not None


async def review_draft(session: AsyncSession, draft: WorkflowDraft, decision: str, *, actor: str,
                       note: str = "") -> WorkflowDraft:
    if decision not in DRAFT_DECISIONS:
        raise BrainError("decision must be reviewed, rejected or draft")
    if draft.status == "converted":
        raise BrainError("this draft was converted to a schedule; it is not reviewed again")
    if draft.status == decision:
        raise BrainError("the draft is already %s" % decision)
    draft.status = decision
    draft.reviewed_by, draft.reviewed_at, draft.updated_at = actor[:200], now(), now()
    if note:
        draft.notes = list(draft.notes or []) + ["Review note (%s, %s): %s" % (decision, now().date().isoformat(),
                                                                               brain_text.one_line(note, 500))]
    await session.flush()
    return draft


async def update_draft(session: AsyncSession, draft: WorkflowDraft, *, params: dict[str, Any] | None,
                       cron: str | None) -> list[str]:
    """Changes a draft's parameters (checked by the capability's model) or cron (checked by valid_cron; '' clears
    it). The draft never runs either way."""
    if draft.status == "converted":
        raise BrainError("a converted draft is never changed: change its schedule on the Schedules page")
    changed = []
    if params is not None:
        if draft.compatibility == "reference_only" or not draft.capability:
            raise BrainError("this draft is a reference only: it has no Command Center capability to take parameters")
        draft.params = validate_params(draft.agent, draft.capability, params)
        changed.append("params")
    if cron is not None:
        cron = " ".join(cron.split())
        if cron and not _cron_ok(cron):
            raise BrainError("invalid cron expression: five fields, minute hour day month weekday (e.g. 0 8 * * 1-5)")
        draft.cron = cron
        changed.append("cron")
    if not changed:
        raise BrainError("nothing to change: send params or cron")
    draft.updated_at = now()
    await session.flush()
    return changed


async def convert_draft(session: AsyncSession, draft: WorkflowDraft, *, jarvis_job_is_off: bool) -> Schedule:
    """A reviewed draft becomes a schedule, created DISABLED: the scheduler never runs it until a person enables it
    on the Schedules page (and every run still goes through the capability's approval rules)."""
    if draft.status == "converted":
        raise BrainError("this draft was already converted (schedule %s)" % draft.schedule_id)
    if draft.compatibility == "reference_only" or not draft.capability:
        raise BrainError("a reference-only draft has no Command Center capability to schedule")
    if draft.status != "reviewed":
        raise BrainError("only a reviewed draft can be converted (this one is %s)" % draft.status)
    if not _cron_ok(draft.cron or ""):
        raise BrainError("the draft has no valid cron schedule: set one first")
    params = validate_params(draft.agent, draft.capability, draft.params)
    name = "jarvis-%s" % draft.name
    if await session.scalar(select(Schedule.id).where(Schedule.name == name)) is not None:
        raise BrainError("a schedule named %s already exists" % name)
    if draft.enabled_in_source and not jarvis_job_is_off:
        raise BrainError("this job is still switched on in JARVIS: switch it off there first and confirm that it is "
                         "off, so the work is not done twice")
    at = now()
    schedule = Schedule(name=name, agent=draft.agent, capability=draft.capability, params=params, cron=draft.cron,
                        enabled=False, next_run_at=None, created_at=at, updated_at=at,
                        description="From JARVIS job %s (draft %d); created disabled" % (draft.name, draft.id))
    try:
        async with session.begin_nested():
            session.add(schedule)
            await session.flush()
    except IntegrityError:
        raise BrainError("a schedule named %s already exists" % name) from None
    draft.status, draft.schedule_id, draft.updated_at = "converted", schedule.id, at
    await session.flush()
    return schedule


# --- Import runs ------------------------------------------------------------------------------------------------


REVIEWED_SINCE_IMPORT = (MemoryItem.version > 1, MemoryItem.status.not_in(("pending_review", "quarantined")),
                         MemoryItem.deleted_at.is_not(None))


async def run_detail(session: AsyncSession, run: ImportRun) -> dict[str, Any]:
    by_action = {k: int(n) for k, n in (await session.execute(
        select(ImportRecord.action, func.count()).where(ImportRecord.run_id == run.id)
        .group_by(ImportRecord.action))).all()}
    by_type = {k: int(n) for k, n in (await session.execute(
        select(ImportRecord.record_type, func.count()).where(ImportRecord.run_id == run.id)
        .group_by(ImportRecord.record_type))).all()}
    reviewed = int(await session.scalar(select(func.count()).select_from(MemoryItem).where(
        MemoryItem.import_run_id == run.id, or_(*REVIEWED_SINCE_IMPORT))) or 0)
    return {"run": run_out(run, report=True), "records_by_action": by_action, "records_by_type": by_type,
            # Whether a rollback may be tried: it still refuses (and says why) when a later run depends on this one,
            # when reviewed items are not to be discarded, or when a conflict decision's target changed since.
            "can_roll_back": run.status in ("completed", "failed", "interrupted"),
            "reviewed_items": reviewed}
