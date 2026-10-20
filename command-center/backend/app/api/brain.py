"""The JARVIS Brain API (spec section 3): memory items, search, the conversation archive, import conflicts, workflow
drafts, the agents' view, import runs and rollback, and the export.

Who sees what is decided by app/brain/policy.py from the person's role and sensitive-data permission (read from
`users` on every request): viewers read approved, general business knowledge only; operators also see items waiting
for review and the general conversation archive; admins see everything, including customer and quarantined items;
Credit Repair items need the sensitive-data permission whatever the role. A record outside the person's scope is
answered with 404, exactly like a missing one.

Approving needs an admin when policy.needs_admin says so (instruction, skill and workflow kinds, Credit Repair items,
quarantined items and risk flags); so does an edit whose old or new state needs one, and moving an item into or out
of Credit Repair needs an admin with the sensitive-data permission. A refusal by the memory service (BrainError) is a
400 with a message that is safe to show (app/main.py). Every change writes an audit entry carrying ids, kinds,
statuses and counts only, never titles or text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, customer_settings
from ..brain import KINDS, archive, importer, review, store
from ..brain.policy import RISK_FLAGS, SENSITIVE_AGENT, PersonScope, person_items, person_messages, person_sessions
from ..config import Settings, get_settings
from ..models import ArchiveMessage, ArchiveSession, ImportRun, MemoryConflict, MemoryItem, User, WorkflowDraft
from .deps import Principal, client_ip, get_db, require_admin, require_operator, require_user, to_dict

router = APIRouter(prefix="/api/brain", tags=["brain"])

SHARED_ALIASES = ("shared", review.SHARED)


@dataclass(frozen=True)
class Caller:
    name: str
    user_id: int
    scope: PersonScope


async def _caller(principal: Principal, session: AsyncSession) -> Caller:
    """The person and their scope, from `users` (the role and the sensitive-data permission as they are now)."""
    user = await session.get(User, principal.user_id) if principal.user_id else None
    if user is None or not user.is_active:
        raise HTTPException(401, "not signed in")
    return Caller(principal.name, user.id, PersonScope(user.role, bool(user.can_access_sensitive)))


async def any_user(principal: Principal = Depends(require_user), session: AsyncSession = Depends(get_db)) -> Caller:
    return await _caller(principal, session)


async def operator(principal: Principal = Depends(require_operator),
                   session: AsyncSession = Depends(get_db)) -> Caller:
    return await _caller(principal, session)


async def admin(principal: Principal = Depends(require_admin), session: AsyncSession = Depends(get_db)) -> Caller:
    return await _caller(principal, session)


# --- Bodies -----------------------------------------------------------------------------------------------------


class ItemCreate(BaseModel):
    kind: str = Field(max_length=40)
    title: str = Field(min_length=1, max_length=store.MAX_TITLE)
    body: str = Field("", max_length=store.MAX_BODY)
    agent: str = Field("", max_length=40)
    tags: list[str] = Field(default_factory=list, max_length=store.MAX_TAGS)


class ItemPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=store.MAX_TITLE)
    body: str | None = Field(None, max_length=store.MAX_BODY)
    tags: list[str] | None = Field(None, max_length=store.MAX_TAGS)
    kind: str | None = Field(None, max_length=40)
    agent: str | None = Field(None, max_length=40)
    note: str = Field("", max_length=2000)


class ItemReview(BaseModel):
    decision: str = Field(pattern="^(approve|reject|archive|reopen)$")
    note: str = Field("", max_length=2000)


class WithholdBody(BaseModel):
    # Also withhold the readable messages with exactly this text in the conversation and its continuations and
    # branches (never by default: identical words elsewhere are not the same message).
    include_identical: bool = False
    # Hide the conversation's title too; by default only when this is its first user message (null).
    hide_title: bool | None = None


class PromoteBody(BaseModel):
    kind: str = Field(max_length=40)
    title: str = Field(min_length=1, max_length=store.MAX_TITLE)
    agent: str | None = Field(None, max_length=40)


class ResolveBody(BaseModel):
    resolution: str = Field(pattern="^(keep_existing|use_incoming|keep_both|dismissed)$")
    note: str = Field("", max_length=2000)


class DraftPatch(BaseModel):
    params: dict[str, Any] | None = None
    cron: str | None = Field(None, max_length=100)


class DraftReview(BaseModel):
    decision: str = Field(pattern="^(reviewed|rejected|draft)$")
    note: str = Field("", max_length=2000)


class ConvertBody(BaseModel):
    jarvis_job_is_off: bool = False


class RollbackBody(BaseModel):
    confirm: str = Field("", max_length=40)
    discard_reviewed: bool = False


# --- Helpers ----------------------------------------------------------------------------------------------------


def _admin_reasons(item: MemoryItem) -> list[str]:
    reasons = []
    if item.kind in ("instruction", "skill", "workflow"):
        reasons.append("%s items" % item.kind)
    if item.agent == SENSITIVE_AGENT:
        reasons.append("Credit Repair items")
    if item.status == "quarantined":
        reasons.append("quarantined items")
    flagged = [f for f in RISK_FLAGS if (item.flags or {}).get(f)]
    if flagged:
        reasons.append("items flagged %s" % ", ".join(flagged))
    return reasons


def _need_admin(caller: Caller, *items: MemoryItem) -> None:
    if caller.scope.is_admin:
        return
    reasons = [r for i in items for r in _admin_reasons(i)]
    if reasons:
        raise HTTPException(403, "an admin must do this: %s need an admin" % "; ".join(dict.fromkeys(reasons)))


def _need_sensitive_admin(caller: Caller) -> None:
    if not (caller.scope.is_admin and caller.scope.sensitive):
        raise HTTPException(403, "moving an item into or out of Credit Repair needs an admin with the sensitive-data "
                                 "permission")


def _list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _kinds(kind: str | None) -> list[str] | None:
    kinds = _list(kind)
    unknown = [k for k in kinds if k not in KINDS]
    if unknown:
        raise HTTPException(400, "unknown kind: %s" % ", ".join(unknown))
    return kinds or None


def _agent(agent: str | None) -> str | None:
    """An agent filter: none for a missing or empty value (a form's unset field), '' (shared items) for 'shared'."""
    if agent is None or not agent.strip():
        return None
    return "" if agent.strip() in SHARED_ALIASES else agent.strip()


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _when(value: str | None, *, until: bool = False) -> datetime | None:
    """A date filter: a date alone ('2026-03-02', as the dashboard's date fields send it) covers that whole day, so
    'until' a day includes it; a full ISO time is used as it is."""
    if not value:
        return None
    try:
        when = _aware(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
    except ValueError:
        raise HTTPException(422, "dates are YYYY-MM-DD or ISO 8601 times") from None
    assert when is not None
    return when + timedelta(days=1) if until and len(value.strip()) == 10 else when


async def _item(session: AsyncSession, caller: Caller, item_id: int, *, lock: bool = False) -> MemoryItem:
    """An item in the caller's scope (admins also reach deleted ones), else 404."""
    stmt = select(MemoryItem).where(MemoryItem.id == item_id,
                                    person_items(caller.scope, include_deleted=caller.scope.is_admin))
    item = await session.scalar(stmt.with_for_update() if lock else stmt)
    if item is None:
        raise HTTPException(404, "no such memory item")
    return item


def _item_audit(item: MemoryItem, **extra: Any) -> dict[str, Any]:
    return {"item": item.id, "kind": item.kind, "agent": item.agent, "status": item.status, "version": item.version,
            "sensitivity": item.sensitivity, **extra}


async def _message(session: AsyncSession, caller: Caller, message_id: int, *, lock: bool = False
                   ) -> tuple[ArchiveMessage, ArchiveSession]:
    stmt = (select(ArchiveMessage, ArchiveSession).join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
            .where(ArchiveMessage.id == message_id, person_sessions(caller.scope), person_messages(caller.scope)))
    row = (await session.execute(stmt.with_for_update(of=ArchiveMessage) if lock else stmt)).first()
    if row is None:
        raise HTTPException(404, "no such message")
    return row[0], row[1]


async def _draft(session: AsyncSession, draft_id: int, *, lock: bool = False) -> WorkflowDraft:
    stmt = select(WorkflowDraft).where(WorkflowDraft.id == draft_id)
    draft = await session.scalar(stmt.with_for_update() if lock else stmt)
    if draft is None:
        raise HTTPException(404, "no such workflow draft")
    return draft


def _visible_version(caller: Caller, v: Any) -> bool:
    if v.agent == SENSITIVE_AGENT and not caller.scope.sensitive:
        return False
    return caller.scope.sees_customer or v.sensitivity == "general"


# --- Overview, items --------------------------------------------------------------------------------------------


@router.get("/overview")
async def overview(caller: Caller = Depends(any_user), session: AsyncSession = Depends(get_db),
                   settings: Settings = Depends(get_settings)) -> dict:
    await importer.mark_dead_runs(session)  # "Last import: Running" only while its process is alive
    values = await customer_settings.load(session, settings)
    return await review.overview(session, caller.scope, ai_context=str(values.get(store.AI_CONTEXT_KEY) or "off"),
                                 instance_label=str(getattr(settings, "instance_label", "") or ""))


@router.get("/items")
async def list_items(
    caller: Caller = Depends(any_user), session: AsyncSession = Depends(get_db),
    q: str = Query("", max_length=500), kind: str | None = None, agent: str | None = None,
    status: str | None = None, include_deleted: bool = False,
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
) -> dict:
    if include_deleted and not caller.scope.is_admin:
        raise HTTPException(403, "only admins can list deleted items")
    if status and status not in review.ITEM_STATUSES:
        raise HTTPException(400, "unknown status: %s" % status)
    hits, total = await store.search_memory(
        session, where=person_items(caller.scope, include_deleted=include_deleted), q=q, kinds=_kinds(kind),
        agent=_agent(agent), status=status, limit=limit, offset=offset)
    return {"items": [review.item_out(h.item, excerpt=h.excerpt, rank=h.rank) for h in hits], "total": total,
            "filtered": await store.search_was_filtered(session, q)}


@router.get("/items/{item_id}")
async def get_item(item_id: int, caller: Caller = Depends(any_user), session: AsyncSession = Depends(get_db)) -> dict:
    item = await _item(session, caller, item_id)
    versions = []
    if caller.scope.operates:  # viewers get the approved text only, not its history
        versions = [review.version_out(v) for v in await store.versions(session, item.id)
                    if _visible_version(caller, v)]
    return {"item": review.item_out(item), "versions": versions, "versions_visible": caller.scope.operates}


@router.post("/items", status_code=201)
async def create_item(body: ItemCreate, request: Request, caller: Caller = Depends(operator),
                      session: AsyncSession = Depends(get_db)) -> dict:
    if body.agent == SENSITIVE_AGENT and not (caller.scope.is_admin and caller.scope.sensitive):
        raise HTTPException(403, "Credit Repair items are added by an admin with the sensitive-data permission")
    item = await store.create_item(session, kind=body.kind, title=body.title, body=body.body, actor=caller.name,
                                   agent=body.agent, tags=body.tags, origin="manual")
    _need_admin(caller, item)  # judged on the result: cleaning may flag an instruction or a redacted secret
    await audit.record(session, caller.name, "memory.created", "memory_item", item.id, _item_audit(item),
                       ip=client_ip(request))
    await session.commit()
    return {"item": review.item_out(item)}


@router.patch("/items/{item_id}")
async def edit_item(item_id: int, body: ItemPatch, request: Request, caller: Caller = Depends(operator),
                    session: AsyncSession = Depends(get_db)) -> dict:
    item = await _item(session, caller, item_id, lock=True)
    changes = {k: v for k, v in body.model_dump(exclude={"note"}).items() if v is not None}
    if not changes:
        raise HTTPException(400, "nothing to change: send title, body, tags, kind or agent")
    if "agent" in changes and (changes["agent"] == SENSITIVE_AGENT) != (item.agent == SENSITIVE_AGENT):
        _need_sensitive_admin(caller)
    _need_admin(caller, item)  # the old state
    await store.edit_item(session, item, actor=caller.name, note=review.clean_note(body.note), **changes)
    _need_admin(caller, item)  # the new state (refused: nothing is committed)
    await audit.record(session, caller.name, "memory.edited", "memory_item", item.id,
                       _item_audit(item, fields=sorted(changes)), ip=client_ip(request))
    await session.commit()
    return {"item": review.item_out(item)}


@router.post("/items/{item_id}/review")
async def review_item(item_id: int, body: ItemReview, request: Request, caller: Caller = Depends(operator),
                      session: AsyncSession = Depends(get_db)) -> dict:
    item = await _item(session, caller, item_id, lock=True)
    _need_admin(caller, item)
    await store.review_item(session, item, decision=body.decision, actor=caller.name,
                            note=review.clean_note(body.note))
    await audit.record(session, caller.name, "memory.%s" % body.decision, "memory_item", item.id, _item_audit(item),
                       ip=client_ip(request))
    await session.commit()
    return {"item": review.item_out(item)}


@router.delete("/items/{item_id}")
async def delete_item(item_id: int, request: Request, caller: Caller = Depends(admin),
                      session: AsyncSession = Depends(get_db)) -> dict:
    item = await _item(session, caller, item_id, lock=True)
    await store.delete_item(session, item, actor=caller.name)
    await audit.record(session, caller.name, "memory.deleted", "memory_item", item.id, _item_audit(item),
                       ip=client_ip(request))
    await session.commit()
    return {"item": review.item_out(item)}


@router.post("/items/{item_id}/restore")
async def restore_item(item_id: int, request: Request, caller: Caller = Depends(admin),
                       session: AsyncSession = Depends(get_db)) -> dict:
    item = await _item(session, caller, item_id, lock=True)
    await store.restore_item(session, item, actor=caller.name)
    await audit.record(session, caller.name, "memory.restored", "memory_item", item.id, _item_audit(item),
                       ip=client_ip(request))
    await session.commit()
    return {"item": review.item_out(item)}


async def _no_import_running(session: AsyncSession) -> None:
    """The import lock until the request's transaction ends; 409 while an import, a rollback or a rescan holds it (it
    could write back what this request removes: a purged item from its source, a redacted version's old text)."""
    if not await importer.try_xact_lock(session, importer.GLOBAL_LOCK):
        raise HTTPException(409, "an import, rollback or rescan is running: try again when it has finished")


@router.delete("/items/{item_id}/purge")
async def purge_item(item_id: int, request: Request, caller: Caller = Depends(admin),
                     session: AsyncSession = Depends(get_db)) -> dict:
    await _no_import_running(session)
    item = await _item(session, caller, item_id, lock=True)
    detail = {"item": item.id, "kind": item.kind, "agent": item.agent}
    origin = {"origin": item.origin, "message": (item.flags or {}).get("message_id"),
              "source_withheld": bool((item.flags or {}).get("source_withheld"))}
    result = await review.purge(session, item, actor=caller.name)
    answers = result.pop("answers_scrubbed")
    await audit.record(session, caller.name, "memory.purged", "memory_item", item_id,
                       {**detail, **result, **origin, "answers_scrubbed": answers}, ip=client_ip(request))
    await session.commit()
    return {"purged": item_id, **result}


@router.post("/items/{item_id}/versions/{version}/redact")
async def redact_version(item_id: int, version: int, request: Request, caller: Caller = Depends(admin),
                         session: AsyncSession = Depends(get_db)) -> dict:
    await _no_import_running(session)
    item = await _item(session, caller, item_id, lock=True)
    redacted = await review.redact_version(session, item, version)
    if redacted is None:
        raise HTTPException(404, "no such version")
    v, answers = redacted
    await audit.record(session, caller.name, "memory.version_redacted", "memory_item", item.id,
                       {"item": item.id, "version": version, "kind": item.kind, "answers_scrubbed": answers},
                       ip=client_ip(request))
    await session.commit()
    return {"version": review.version_out(v)}


# --- Search and the conversation archive ------------------------------------------------------------------------


@router.get("/search")
async def search(
    caller: Caller = Depends(any_user), session: AsyncSession = Depends(get_db),
    q: str = Query("", max_length=500), search_scope: str = Query("all", alias="scope",
                                                                pattern="^(all|memory|archive)$"),
    kind: str | None = None, agent: str | None = None, staff: str | None = None, label: str | None = None,
    since: str | None = None, until: str | None = None, limit: int = Query(20, ge=1, le=50),
) -> dict:
    since_at, until_at = _when(since), _when(until, until=True)
    agent_filter = _agent(agent)
    memory: dict[str, Any] = {"hits": [], "total": 0}
    if search_scope in ("all", "memory"):
        when = func.coalesce(MemoryItem.source_at, MemoryItem.created_at)
        clauses: list[ColumnElement[bool]] = [person_items(caller.scope)]
        if since_at is not None:
            clauses.append(when >= since_at)
        if until_at is not None:
            clauses.append(when < until_at)
        hits, total = await store.search_memory(session, where=and_(*clauses), q=q, kinds=_kinds(kind),
                                                agent=agent_filter, limit=limit)
        memory = {"hits": [review.item_out(h.item, excerpt=h.excerpt, rank=h.rank) for h in hits], "total": total}
    archive: dict[str, Any] = {"hits": [], "total": 0}
    if search_scope in ("all", "archive") and caller.scope.sees_archive:
        found, n = await store.search_archive(
            session, session_where=person_sessions(caller.scope), message_where=person_messages(caller.scope), q=q,
            labels=_list(label) or None, agent=agent_filter, staff=staff or None, since=since_at, until=until_at,
            limit=limit)
        archive = {"hits": [{"message": review.message_out(h.message),
                             "session": {"id": h.session.id, "title": h.session.title, "staff": h.session.staff,
                                         "agent": h.session.agent, "started_at": review._iso(h.session.started_at)},
                             "excerpt": h.excerpt, "rank": h.rank} for h in found], "total": n}
    return {"memory": memory, "archive": archive, "filtered": await store.search_was_filtered(session, q),
            "archive_visible": caller.scope.sees_archive}


@router.get("/sessions")
async def list_sessions(
    caller: Caller = Depends(operator), session: AsyncSession = Depends(get_db),
    q: str = Query("", max_length=500), staff: str | None = None, agent: str | None = None,
    origin: str | None = Query(None, pattern="^(chat|scheduled)?$"), label: str | None = None,
    since: str | None = None, until: str | None = None,
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
) -> dict:
    clauses: list[ColumnElement[bool]] = [person_sessions(caller.scope)]
    if staff:
        clauses.append(ArchiveSession.staff == staff)
    if agent:
        clauses.append(ArchiveSession.agent == agent)
    if origin:
        clauses.append(ArchiveSession.origin == origin)
    for one in _list(label):
        clauses.append(ArchiveSession.labels.has_key(one))
    since_at, until_at = _when(since), _when(until, until=True)
    if since_at is not None:
        clauses.append(ArchiveSession.started_at >= since_at)
    if until_at is not None:
        clauses.append(ArchiveSession.started_at < until_at)
    if await store.search_was_filtered(session, q):
        clauses.append(ArchiveSession.search.op("@@")(func.websearch_to_tsquery("english", q)))
    where = and_(*clauses)
    total = int(await session.scalar(select(func.count()).select_from(ArchiveSession).where(where)) or 0)
    rows = (await session.scalars(select(ArchiveSession).where(where).order_by(
        ArchiveSession.started_at.desc().nulls_last(), ArchiveSession.id.desc()).limit(limit).offset(offset))).all()
    return {"sessions": [review.session_out(s) for s in rows], "total": total}


@router.get("/sessions/{session_id}")
async def get_session(session_id: int, caller: Caller = Depends(operator), session: AsyncSession = Depends(get_db),
                      limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0)) -> dict:
    sess = await session.scalar(select(ArchiveSession).where(ArchiveSession.id == session_id,
                                                             person_sessions(caller.scope)))
    if sess is None:
        raise HTTPException(404, "no such conversation")
    in_session = ArchiveMessage.session_id == session_id
    where = and_(in_session, person_messages(caller.scope))
    total = int(await session.scalar(select(func.count()).select_from(ArchiveMessage).where(where)) or 0)
    everything = int(await session.scalar(select(func.count()).select_from(ArchiveMessage).where(in_session)) or 0)
    rows = (await session.scalars(select(ArchiveMessage).where(where).order_by(
        ArchiveMessage.position, ArchiveMessage.id).limit(limit).offset(offset))).all()
    return {"session": review.session_out(sess), "messages": [review.message_out(m) for m in rows], "total": total,
            "hidden": everything - total}


@router.get("/messages/{message_id}/promoted")
async def promoted_copies(message_id: int, caller: Caller = Depends(admin),
                          session: AsyncSession = Depends(get_db)) -> dict:
    """How many memory items were promoted from a message (what withholding it empties), counted as the withhold
    finds them; the items listed are those in the admin's own scope."""
    message, _ = await _message(session, caller, message_id)
    copies = await archive.promoted_copies(session, [message])
    visible = set((await session.scalars(select(MemoryItem.id).where(
        MemoryItem.id.in_([c.id for c in copies]), person_items(caller.scope, include_deleted=True)))).all())
    return {"message": message.id, "count": len(copies),
            "items": [{"id": c.id, "status": c.status, "deleted": c.deleted_at is not None}
                      for c in copies if c.id in visible]}


async def _in_scope(session: AsyncSession, caller: Caller, ids: list[int]) -> list[int]:
    """The message ids the caller may see (the others are counted, not named)."""
    if not ids:
        return []
    return sorted((await session.scalars(select(ArchiveMessage.id).join(
        ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id).where(
        ArchiveMessage.id.in_(ids), person_sessions(caller.scope), person_messages(caller.scope)))).all())


@router.get("/messages/{message_id}/withhold-preview")
async def withhold_preview(message_id: int, caller: Caller = Depends(admin),
                           session: AsyncSession = Depends(get_db)) -> dict:
    """What withholding a message would also withhold, hold for review or leave to the admin's choice (ids, positions
    and counts, never text); read-only."""
    message, sess = await _message(session, caller, message_id)
    if message.withheld:
        raise HTTPException(400, "this message is already withheld")
    out = await review.withhold_preview(session, message, sess, caller.scope)
    for key in ("same_message_rows", "held_rows", "identical_text_rows"):
        visible = set(await _in_scope(session, caller, [r["id"] for r in out[key]]))
        out[key + "_hidden"] = sum(1 for r in out[key] if r["id"] not in visible)
        out[key] = [r for r in out[key] if r["id"] in visible]
    return out


@router.post("/messages/{message_id}/withhold")
async def withhold_message(message_id: int, request: Request, body: WithholdBody | None = None,
                           caller: Caller = Depends(admin), session: AsyncSession = Depends(get_db)) -> dict:
    await _no_import_running(session)  # it could write the message back with its text
    body = body or WithholdBody()
    message, _ = await _message(session, caller, message_id, lock=True)
    done = await review.withhold_message(session, message, actor=caller.name, ip=client_ip(request),
                                         include_identical=body.include_identical, hide_title=body.hide_title)
    sess = done.session
    assert sess is not None
    copies = [c["item"] for c in done.copies]
    also = await _in_scope(session, caller, done.also_withheld)
    held = await _in_scope(session, caller, done.held_for_review)
    await audit.record(session, caller.name, "archive.message_withheld", "archive_message", message.id,
                       {"message": message.id, "session": sess.id, "imported_messages": sess.imported_messages,
                        "withheld_messages": sess.withheld_messages, "tombstone": done.tombstone_created,
                        "copies": copies, "versions_blanked": done.versions_blanked,
                        "rollback_copies_blanked": done.rollback_copies_blanked,
                        "answers_scrubbed": done.answers_scrubbed, "via": "api", "also_withheld": also,
                        "held_for_review": held, "identical_withheld": done.identical_withheld,
                        "keys_tombstoned": done.keys_tombstoned, "title_hidden": done.title_hidden},
                       ip=client_ip(request))
    await session.commit()
    return {"message": review.message_out(message), "session": review.session_out(sess),
            "copies": {"items": copies, "versions_blanked": done.versions_blanked},
            "answers_scrubbed": done.answers_scrubbed, "rollback_copies_blanked": done.rollback_copies_blanked,
            "also_withheld": also, "held_for_review": held, "identical_withheld": done.identical_withheld,
            "keys_tombstoned": done.keys_tombstoned, "title_hidden": done.title_hidden}


# --- Holds: rows withheld for an admin's review -----------------------------------------------------------------


@router.get("/holds")
async def list_holds(caller: Caller = Depends(admin), session: AsyncSession = Depends(get_db)) -> dict:
    """Rows withheld because they may be a withheld message (the same uid with other text, the same text outside a
    copy position, an upgrade heuristic), with the evidence; never their text."""
    found = await review.holds(session, caller.scope)
    return {"holds": found, "total": len(found)}


async def _hold_audit(session: AsyncSession, caller: Caller, action: str, message: ArchiveMessage,
                      stone: Any, request: Request) -> None:
    await audit.record(session, caller.name, action, "archive_message", message.id,
                       {"message": message.id, "session": message.session_id,
                        "anchor": stone.anchor_key or stone.source_key, "rule": stone.via}, ip=client_ip(request))


@router.post("/holds/{message_id}/keep")
async def keep_hold(message_id: int, request: Request, caller: Caller = Depends(admin),
                    session: AsyncSession = Depends(get_db)) -> dict:
    await _no_import_running(session)
    message, _ = await _message(session, caller, message_id, lock=True)
    stone = await review.keep_hold(session, message, actor=caller.name)
    await _hold_audit(session, caller, "archive.hold_kept", message, stone, request)
    await session.commit()
    return {"message": review.message_out(message), "review": stone.review}


@router.post("/holds/{message_id}/release")
async def release_hold(message_id: int, request: Request, caller: Caller = Depends(admin),
                       session: AsyncSession = Depends(get_db)) -> dict:
    await _no_import_running(session)  # an import could hold it again before the release is committed
    message, sess = await _message(session, caller, message_id, lock=True)
    stone = await review.release_hold(session, message, sess, actor=caller.name)
    await _hold_audit(session, caller, "archive.hold_released", message, stone, request)
    await session.commit()
    return {"message": review.message_out(message), "released": True,
            "note": "its text comes back at the next import of a backup holding it; promoted copies stay empty"}


@router.post("/messages/{message_id}/promote", status_code=201)
async def promote_message(message_id: int, body: PromoteBody, request: Request, caller: Caller = Depends(operator),
                          session: AsyncSession = Depends(get_db)) -> dict:
    # Locked: a withhold of the same message (the route, or an import's update of the row) waits for this promote and
    # then finds its copy, or this promote waits for the withhold and finds the message withheld.
    message, sess = await _message(session, caller, message_id, lock=True)
    agent = sess.agent if body.agent is None else body.agent
    if agent == SENSITIVE_AGENT and not (caller.scope.is_admin and caller.scope.sensitive):
        raise HTTPException(403, "Credit Repair items are added by an admin with the sensitive-data permission")
    item = await review.promote_message(session, message, sess, kind=body.kind, title=body.title, agent=agent,
                                        actor=caller.name)
    _need_admin(caller, item)  # as for POST /items: judged on the resulting item
    await audit.record(session, caller.name, "memory.promoted", "memory_item", item.id,
                       _item_audit(item, message=message.id, session=sess.id), ip=client_ip(request))
    await session.commit()
    return {"item": review.item_out(item)}


# --- Conflicts --------------------------------------------------------------------------------------------------


@router.get("/conflicts")
async def list_conflicts(
    caller: Caller = Depends(operator), session: AsyncSession = Depends(get_db),
    status: str | None = Query(None, pattern="^(open|keep_existing|use_incoming|keep_both|dismissed)?$"),
    limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
) -> dict:
    clauses: list[ColumnElement[bool]] = [review.conflict_scope(caller.scope)]
    if status:
        clauses.append(MemoryConflict.status == status)
    where = and_(*clauses)
    total = int(await session.scalar(select(func.count()).select_from(MemoryConflict).where(where)) or 0)
    rows = (await session.scalars(select(MemoryConflict).where(where).order_by(
        MemoryConflict.created_at.desc(), MemoryConflict.id.desc()).limit(limit).offset(offset))).all()
    out = []
    for c in rows:
        existing, incoming = await review.conflict_items(session, c)
        out.append(review.conflict_out(c, existing_item=existing, incoming_item=incoming,
                                       admin_needed=await review.conflict_needs_admin(session, c)))
    return {"conflicts": out, "total": total}


@router.post("/conflicts/{conflict_id}/resolve")
async def resolve_conflict(conflict_id: int, body: ResolveBody, request: Request, caller: Caller = Depends(operator),
                           session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    c = await session.scalar(select(MemoryConflict).where(MemoryConflict.id == conflict_id,
                                                          review.conflict_scope(caller.scope)).with_for_update())
    if c is None:
        raise HTTPException(404, "no such conflict")
    if not caller.scope.is_admin and await review.conflict_needs_admin(session, c, body.resolution):
        raise HTTPException(403, "an admin must decide this conflict (instruction, changed-source and workflow "
                                 "conflicts, and decisions that change an item only an admin may approve)")
    effects = await review.resolve_conflict(session, settings, c, body.resolution, actor=caller.name,
                                            note=review.clean_note(body.note))
    await audit.record(session, caller.name, "memory.conflict_resolved", "memory_conflict", c.id,
                       {"conflict": c.id, "kind": c.kind, "resolution": c.status, "run": c.run_id,
                        "effects": {k: len(v) for k, v in effects.items()}}, ip=client_ip(request))
    for site in effects["sites"]:
        await audit.record(session, caller.name, "site.created", "site", site["id"],
                           {"site": site["id"], "enabled": False, "environment": "production", "conflict": c.id},
                           ip=client_ip(request))
    await session.commit()
    existing, incoming = await review.conflict_items(session, c)
    return {"conflict": review.conflict_out(c, existing_item=existing, incoming_item=incoming,
                                            admin_needed=await review.conflict_needs_admin(session, c)),
            "effects": effects}


# --- Workflow drafts --------------------------------------------------------------------------------------------


@router.get("/workflows")
async def list_workflows(caller: Caller = Depends(operator), session: AsyncSession = Depends(get_db)) -> dict:
    drafts = (await session.scalars(select(WorkflowDraft).order_by(WorkflowDraft.name, WorkflowDraft.id))).all()
    return {"drafts": [{**review.draft_out(d), "schedule_exists": await review.schedule_exists(session, d)}
                       for d in drafts]}


@router.get("/workflows/{draft_id}")
async def get_workflow(draft_id: int, caller: Caller = Depends(operator), session: AsyncSession = Depends(get_db)
                       ) -> dict:
    draft = await _draft(session, draft_id)
    return {"draft": review.draft_out(draft), "schedule_exists": await review.schedule_exists(session, draft)}


@router.patch("/workflows/{draft_id}")
async def update_workflow(draft_id: int, body: DraftPatch, request: Request, caller: Caller = Depends(admin),
                          session: AsyncSession = Depends(get_db)) -> dict:
    draft = await _draft(session, draft_id, lock=True)
    changed = await review.update_draft(session, draft, params=body.params, cron=body.cron)
    await audit.record(session, caller.name, "workflow_draft.updated", "workflow_draft", draft.id,
                       {"draft": draft.id, "fields": changed, "status": draft.status}, ip=client_ip(request))
    await session.commit()
    return {"draft": review.draft_out(draft), "schedule_exists": await review.schedule_exists(session, draft)}


@router.post("/workflows/{draft_id}/review")
async def review_workflow(draft_id: int, body: DraftReview, request: Request, caller: Caller = Depends(operator),
                          session: AsyncSession = Depends(get_db)) -> dict:
    draft = await _draft(session, draft_id, lock=True)
    await review.review_draft(session, draft, body.decision, actor=caller.name, note=review.clean_note(body.note))
    await audit.record(session, caller.name, "workflow_draft.reviewed", "workflow_draft", draft.id,
                       {"draft": draft.id, "status": draft.status}, ip=client_ip(request))
    await session.commit()
    return {"draft": review.draft_out(draft), "schedule_exists": await review.schedule_exists(session, draft)}


@router.post("/workflows/{draft_id}/convert")
async def convert_workflow(draft_id: int, body: ConvertBody, request: Request, caller: Caller = Depends(admin),
                           session: AsyncSession = Depends(get_db)) -> dict:
    draft = await _draft(session, draft_id, lock=True)
    schedule = await review.convert_draft(session, draft, jarvis_job_is_off=body.jarvis_job_is_off)
    await audit.record(session, caller.name, "workflow_draft.converted", "workflow_draft", draft.id,
                       {"draft": draft.id, "schedule": schedule.id, "enabled": schedule.enabled},
                       ip=client_ip(request))
    await session.commit()
    await session.refresh(schedule)
    return {"draft": review.draft_out(draft), "schedule": to_dict(schedule)}


# --- Agents and skills ------------------------------------------------------------------------------------------


@router.get("/agents")
async def agents(caller: Caller = Depends(any_user), session: AsyncSession = Depends(get_db)) -> dict:
    return {"agents": await review.agents_view(session, caller.scope)}


@router.get("/skills")
async def skills(caller: Caller = Depends(any_user), session: AsyncSession = Depends(get_db)) -> dict:
    rows = (await session.scalars(select(MemoryItem).where(person_items(caller.scope), MemoryItem.kind == "skill")
                                  .order_by(MemoryItem.agent, MemoryItem.title, MemoryItem.id))).all()
    return {"skills": [review.item_out(i) for i in rows]}


# --- Import runs ------------------------------------------------------------------------------------------------


@router.get("/imports")
async def list_imports(caller: Caller = Depends(operator), session: AsyncSession = Depends(get_db)) -> dict:
    await importer.mark_dead_runs(session)  # an import whose process is gone shows as interrupted, not running
    runs = (await session.scalars(select(ImportRun).order_by(ImportRun.id.desc()))).all()
    return {"runs": [review.run_out(r) for r in runs]}


@router.get("/imports/{run_id}")
async def get_import(run_id: int, caller: Caller = Depends(operator), session: AsyncSession = Depends(get_db)) -> dict:
    await importer.mark_dead_runs(session)
    run = await session.get(ImportRun, run_id)
    if run is None:
        raise HTTPException(404, "no such import run")
    return await review.run_detail(session, run)


@router.post("/imports/{run_id}/rollback")
async def rollback_import(run_id: int, body: RollbackBody, caller: Caller = Depends(admin),
                          session: AsyncSession = Depends(get_db)) -> dict:
    if body.confirm.strip() != str(run_id):
        raise HTTPException(400, "confirm: type the run number")
    if await session.get(ImportRun, run_id) is None:
        raise HTTPException(404, "no such import run")
    try:
        result = await importer.rollback_run(session, run_id, caller.name, body.discard_reviewed)
    except importer.Refused as exc:  # refused before any change
        await session.rollback()
        ids = (" (ids: %s)" % ", ".join(str(i) for i in exc.ids)) if exc.ids else ""
        raise HTTPException(409, "%s%s" % (exc, ids)) from None
    except importer.ImportFailed as exc:  # the rollback did not come out exact: nothing was changed
        await session.rollback()
        raise HTTPException(500, str(exc)) from None
    await session.commit()
    run = await session.get(ImportRun, run_id)
    assert run is not None
    await session.refresh(run)
    return {"run": review.run_out(run, report=True), "rollback": result}


# --- Export -----------------------------------------------------------------------------------------------------


@router.get("/export")
async def export(request: Request, include_customer: bool = False, caller: Caller = Depends(admin),
                 session: AsyncSession = Depends(get_db)) -> JSONResponse:
    data = await review.export_items(session, caller.scope, include_customer=include_customer)
    await audit.record(session, caller.name, "memory.exported", "memory_item", "",
                       {"count": data["count"], "include_customer": include_customer}, ip=client_ip(request))
    await session.commit()
    name = "jarvis-brain-export-%s.json" % datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return JSONResponse(data, headers={"Content-Disposition": 'attachment; filename="%s"' % name})
