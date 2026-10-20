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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import ColumnElement, and_, delete, func, not_, or_, select, true
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import licensing
from .. import tasks as task_service
from ..config import Settings
from ..models import (
    ArchiveMessage,
    ArchiveSession,
    BrainTombstone,
    BrainWithholdExemption,
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
from .archive import WITHHELD_BY_ADMIN, promoted_ref, recompute_session, withhold_copies
from .quotes import scrub_quotes
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
    """MemoryItemOut (spec 3)."""
    out = {k: _iso(getattr(item, k)) for k in ITEM_FIELDS}
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
    held_for_review: list[int] = field(default_factory=list)  # rows withheld as holds (same uid, other text)
    identical_withheld: int = 0  # rows with identical text the admin chose to withhold too
    keys_tombstoned: int = 0  # stones written (the message's own, its rows', its copies' without a row)
    title_hidden: bool = False


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
                           hide_title: bool | None = None, via: str = "admin") -> Withheld:
    """Removes a message's text (and tool calls and labels) for good, with every other stored row that is the same
    JARVIS message (identity.group_rows: the same JARVIS row, the same uid, the importer's duplicates; a row with the
    same uid but other text is held for an admin's review) and, when the admin asks for it (include_identical), the
    readable rows with exactly this text in the same conversation and its continuations and branches. Each gets a
    stone (identity.put_stone) in the admin's group, as do the keys of its copies that have no row, so a later import
    brings none of them back with text. Then everything that copied them (archive.withhold_copies: rollback copies,
    the memory items promoted from them, emptied, and the answers and notes quoting either) goes, and their sessions
    are recomputed. The conversation's title is hidden too (hide_title; by default when this is the conversation's
    first user message, which a title can paraphrase). All or nothing in the caller's transaction."""
    if message.withheld:
        raise BrainError("this message is already withheld")
    sess = await session.get(ArchiveSession, message.session_id)
    assert sess is not None
    text = identity.message_text(message)  # before blanking; in memory only
    group = await identity.group_rows(session, message, sess, text)
    same = await identity.identical_rows(session, message, sess, text, exclude=[g.row.id for g in group.rows]
                                         ) if include_identical else []
    after = await identity.current_after_run(session)
    uid = identity.row_uid(message)
    written = 0
    if message.source_key:  # a row an import stored always has one
        written = int(await identity.put_stone(session, identity.binding_of(
            message, sess.source_session_id, via=via, after_run=after, uid_known=bool(uid)), actor=actor))
    at = now().isoformat()
    out = Withheld(None, bool(written))
    blanked: list[ArchiveMessage] = []
    names = dict((await session.execute(select(ArchiveSession.id, ArchiveSession.source_session_id).where(
        ArchiveSession.id.in_(sorted({r.session_id for r in same}))))).all()) if same else {}
    chosen = [identity.GroupRow(r, names.get(r.session_id, ""), identity.IDENTICAL, identity.CERTAIN) for r in same]
    for g in group.rows + chosen:
        member = g.review == identity.CERTAIN
        if g.row.source_key:
            written += int(await identity.put_stone(session, identity.binding_of(
                g.row, g.source_session_id, anchor=message.source_key, via=g.rule, review=g.review, after_run=after,
                uid_known=member and bool(identity.row_uid(g.row))), actor=actor))
        if g.row.withheld:
            continue  # withheld already (no text): its key joins the group, the row stays as it is
        flag: dict[str, Any] = {"at": at, "note": identity.NOTE, "via": g.rule}
        if not member:
            flag["review"] = identity.PENDING
        _blank(g.row, flag)
        blanked.append(g.row)
        if g.rule == identity.IDENTICAL:
            out.identical_withheld += 1
        elif member:
            out.also_withheld.append(g.row.id)
        else:
            out.held_for_review.append(g.row.id)
    for st in group.rowless:
        written += int(await identity.put_stone(session, identity.Stone(**{
            **st.__dict__, "anchor": message.source_key, "after_run": after}), actor=actor))
    _blank(message, {"at": at, "note": "withheld by an admin on the JARVIS Brain page; the text was removed"})
    if hide_title is None:
        hide_title = await identity.first_user_message(session, message)
    if hide_title:
        sess.title = ""
        sess.flags = {**(sess.flags or {}), "title_withheld": True}
        out.title_hidden = True
    await session.flush()
    done = await withhold_copies(session, messages=[message, *blanked], source_keys=[st.key for st in group.rowless],
                                 actor=actor, cause=cause, ip=ip)
    for sid in sorted({r.session_id for r in blanked} - {message.session_id}):
        await recompute_session(session, sid)
    out.session = await recompute_session(session, message.session_id)
    out.copies, out.versions_blanked = done.copies, done.versions_blanked
    out.rollback_copies_blanked, out.answers_scrubbed = done.rollback_copies_blanked, done.answers_scrubbed
    out.keys_tombstoned = written
    return out


async def hold_message(session: AsyncSession, message: ArchiveMessage, *, anchor: str, actor: str, cause: str,
                       ip: str = "") -> Withheld:
    """Withholds a readable row for an admin's review: it shares the uid of a message withheld in a decisions file
    but is another JARVIS row, and nothing here can compare the texts (decisions-apply, spec 9.2). Its key gets a
    pending stone in that withhold's group (`anchor`), so the holds list shows it and the next import compares the
    text (a member, or a hold an admin keeps or releases). What copied it goes as for a withhold."""
    sess = await session.get(ArchiveSession, message.session_id)
    assert sess is not None
    rule = "same message id"
    written = 0
    if message.source_key:
        written = int(await identity.put_stone(session, identity.binding_of(
            message, sess.source_session_id, anchor=anchor, via=rule, review=identity.PENDING,
            after_run=await identity.current_after_run(session)), actor=actor))
    _blank(message, {"at": now().isoformat(), "note": identity.NOTE, "via": rule, "review": identity.PENDING})
    await session.flush()
    done = await withhold_copies(session, messages=[message], actor=actor, cause=cause, ip=ip)
    return Withheld(await recompute_session(session, message.session_id), bool(written), done.copies,
                    done.versions_blanked, done.rollback_copies_blanked, done.answers_scrubbed,
                    held_for_review=[message.id], keys_tombstoned=written)


async def withhold_preview(session: AsyncSession, message: ArchiveMessage, sess: ArchiveSession,
                           scope: PersonScope) -> dict[str, Any]:
    """What withholding this message would do, read-only (spec 8.3): the other stored rows that are the same JARVIS
    message (withheld too), those held for review (the same uid, other text), the readable rows with exactly this
    text in the conversation and its continuations and branches (withheld only when the admin ticks the box), how
    many copies have no row, the imported memory items in the admin's scope whose text contains it (to purge by hand;
    nothing is automatic), and whether it is the conversation's first user message (its title is hidden by default).
    Ids, positions and counts only, never text."""
    text = identity.message_text(message)
    group = await identity.group_rows(session, message, sess, text, lock=False)
    same = await identity.identical_rows(session, message, sess, text, exclude=[g.row.id for g in group.rows])

    def brief(row: ArchiveMessage) -> dict[str, Any]:
        return {"id": row.id, "session": row.session_id, "position": row.position, "role": row.role,
                "at": _iso(row.at), "state": row.state, "withheld": row.withheld}

    return {"message": message.id,
            "same_message_rows": [brief(g.row) for g in group.members],
            "held_rows": [brief(g.row) for g in group.holds if not g.row.withheld],
            "identical_text_rows": [brief(r) for r in same],
            "rowless_copies": len(group.rowless),
            "items_with_this_text": await identity.items_with_text(session, message, person_items(scope)),
            "first_user_message": await identity.first_user_message(session, message)}


# --- Holds: rows withheld for an admin's review (spec 8.2) ------------------------------------------------------

COMPARE = {"same text": "identical text", "upgrade: same text": "identical text",
           "same message id, different text": "different text"}


async def holds(session: AsyncSession, scope: PersonScope) -> list[dict[str, Any]]:
    """Every withheld row held for an admin's review (its stone is pending) in the admin's scope, with the evidence:
    where it is, its length and state, the rule that held it (what a release brings back), the withheld message it
    may be (its position, role, time and length), whether the texts were compared, and the readable messages around
    it. Never text."""
    found = (await session.execute(
        select(ArchiveMessage, ArchiveSession, BrainTombstone)
        .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
        .join(BrainTombstone, BrainTombstone.source_key == ArchiveMessage.source_key)
        .where(BrainTombstone.reason == WITHHELD_BY_ADMIN, BrainTombstone.review == identity.PENDING,
               ArchiveMessage.withheld.is_(True), person_sessions(scope), person_messages(scope))
        .order_by(ArchiveSession.started_at.desc().nulls_last(), ArchiveSession.id, ArchiveMessage.position,
                  ArchiveMessage.id))).all()
    out = []
    for row, sess, stone in found:
        anchor = stone.anchor_key or stone.source_key
        original = await session.scalar(select(ArchiveMessage).where(ArchiveMessage.source_key == anchor))
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
            "compare": COMPARE.get(stone.via, "nothing to compare"),
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
    flag = dict((message.flags or {}).get("withheld_by_admin") or {})
    flag.update(review=identity.KEPT, kept_at=now().isoformat(), kept_by=actor[:200])
    message.flags = {**(message.flags or {}), "withheld_by_admin": flag}
    await session.flush()
    return stone


async def release_hold(session: AsyncSession, message: ArchiveMessage, sess: ArchiveSession, *,
                       actor: str) -> BrainTombstone:
    """An admin releases a held row: its pending stone goes and an exemption names its JARVIS row for that group, so
    the hold rules (the same uid with other text, the same text, the upgrade heuristics) skip it; the next import of
    a backup holding that row brings its text back (importer release branch). Refused for a row a certain rule
    names (a stone that is not a hold binds its JARVIS row): it is the withheld message itself. Promoted copies
    emptied while it was held stay empty."""
    stone = await _hold(session, message)
    certain = await session.scalar(select(BrainTombstone.source_key).where(
        BrainTombstone.reason == WITHHELD_BY_ADMIN, BrainTombstone.review != identity.PENDING,
        BrainTombstone.source_session_id == sess.source_session_id,
        BrainTombstone.source_row_id == (message.source_message_id or "")[:200],
        BrainTombstone.role.in_(("", message.role))).limit(1))
    if certain is not None:
        raise BrainError("it is the same message as a withheld one: it cannot be released")
    anchor = stone.anchor_key or stone.source_key
    await session.execute(delete(BrainTombstone).where(BrainTombstone.source_key == message.source_key,
                                                       BrainTombstone.review == identity.PENDING))
    await session.execute(pg_insert(BrainWithholdExemption).values(
        anchor_key=anchor, source_session_id=sess.source_session_id,
        source_row_id=(message.source_message_id or "")[:200], reason="released by an admin",
        decided_by=actor[:200]).on_conflict_do_nothing())
    flag = dict((message.flags or {}).get("withheld_by_admin") or {})
    flag.update(released_at=now().isoformat(), released_by=actor[:200])
    message.flags = {**(message.flags or {}), "withheld_by_admin": flag}
    message.source_fp = ""  # its release marker: the next import restores it from the source
    await session.flush()
    return stone


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
    await session.flush()
    return v, await scrub_quotes(session, item_ids=[item.id])  # a history answer may quote the old text


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


async def purge(session: AsyncSession, item: MemoryItem, *, actor: str) -> dict[str, Any]:
    """Deletes a soft-deleted item, its versions and its conflicts, and tombstones its source key so a later import
    does not bring it back. An item promoted from a message has no source key, so no tombstone: the audit entry
    records the purge, and the message itself is withheld separately (never by a purge)."""
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
            if item.source_sha256 not in rejected:
                flags["rejected_source_sha256"] = rejected + [item.source_sha256]
                existing.flags = flags
    tombstone = False
    if item.source_key:
        if await session.get(BrainTombstone, item.source_key) is None:
            session.add(BrainTombstone(source_key=item.source_key, purged_at=now(), purged_by=actor[:200],
                                       reason="purged by an admin"))
        tombstone = True
    if item.origin == "import" and item.source_sha256:
        # The keyed fingerprint of its bytes (never a plain hash): a later import recognises the same text under
        # another key (a renamed or copied file, an incoming version) and leaves it out too.
        await identity.remember_purge(session, source_key=item.source_key or "", sha256_hex=item.source_sha256,
                                      kind=identity.purge_kind(item.source_key or replaces or ""), actor=actor,
                                      via="purge")
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
