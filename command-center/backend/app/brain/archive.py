"""The derived fields of an archived JARVIS session, recomputed from the rows it still holds; and what a withheld
message leaves elsewhere, removed.

A session's counts, labels, sensitivity and summary are never edited by hand: the importer, a rollback and the
withhold-a-message route (app/api/brain.py) all change messages and then call recompute_session(), so the session
always describes exactly the rows that remain.

Whoever withholds a message (an admin, an import, a rescan, decisions-apply, a rollback that puts a withheld row
back) calls withhold_copies(): its rollback copies are blanked, the memory items promoted from it are emptied, and
the history answers and reference notes quoting either lose those lines. sweep_withheld_copies() finds the promoted
items whose message is withheld but which still hold text (what 0.6.0 left behind) and empties them; it runs at
start-up (app/bootstrap.py) and in a rescan.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Text, and_, cast, exists, func, not_, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..models import ArchiveMessage, ArchiveSession, BrainTombstone, ImportRecord, MemoryItem, MemoryItemVersion
from . import store
from . import text as brain_text
from .policy import OLD_ROW_ACTIONS, PROMOTED_REF, SOURCE_REMOVED, WITHHELD_BY_ADMIN
from .quotes import scrub_quotes

SUMMARY_FIRST = 200
SUMMARY_LAST = 300


def _line(text: str, limit: int) -> str:
    """text.one_line(), never ending inside a redaction marker ("[REDACTED:credential_assi…" would read as a new
    secret-like word when the summary is checked or cleaned again)."""
    out = brain_text.one_line(text, limit)
    start = out.rfind("[REDACTED")
    if start >= 0 and "]" not in out[start:]:
        out = out[:start].rstrip() + "…"
    return out


async def recompute_session(session: AsyncSession, archive_session_id: int) -> ArchiveSession | None:
    """Recompute counts, labels, sensitivity, summary and the secrets flag of one archived session.

    - imported_messages / withheld_messages: rows kept with and without their text;
    - tool_calls: the tool calls of the rows that still have text;
    - labels: how many rows carry each label (matched by keywords);
    - sensitivity: the highest level of any row (restricted > customer > general), at least customer when the title
      itself held a masked contact or the session came from a customer-facing channel (flags.customer_channel);
    - summary: the first user message (one line, 200) and the last assistant message (one line, 300) of the rows
      that still have text; a restricted session has no summary and no title.
    The caller flushes and commits.
    """
    sess = await session.get(ArchiveSession, archive_session_id)
    if sess is None:
        return None
    rows = (await session.execute(
        select(ArchiveMessage.role, ArchiveMessage.content, ArchiveMessage.withheld, ArchiveMessage.sensitivity,
               ArchiveMessage.labels, ArchiveMessage.redactions,
               func.jsonb_array_length(ArchiveMessage.tool_calls).label("calls"))
        .where(ArchiveMessage.session_id == archive_session_id)
        .order_by(ArchiveMessage.position, ArchiveMessage.id))).all()
    kept = [r for r in rows if not r.withheld]
    labels: Counter[str] = Counter()
    secrets: Counter[str] = Counter()
    for r in kept:
        labels.update(r.labels or [])
    for r in rows:
        for k, v in (r.redactions or {}).items():
            if isinstance(v, int):
                secrets[k] += v
    flags: dict[str, Any] = dict(sess.flags or {})
    for k, v in (flags.get("title_redactions") or {}).items():
        if isinstance(v, int):
            secrets[k] += v
    levels = [r.sensitivity for r in rows] + (
        ["customer"] if flags.get("title_masked") or flags.get("customer_channel") else [])
    sensitivity = brain_text.most_sensitive(*levels) if levels else "general"
    if secrets:
        flags["secrets_redacted"] = dict(sorted(secrets.items()))
    else:
        flags.pop("secrets_redacted", None)
    summary = ""
    if sensitivity != "restricted":
        first = next((r.content for r in kept if r.role == "user" and r.content.strip()), "")
        last = next((r.content for r in reversed(kept) if r.role == "assistant" and r.content.strip()), "")
        pieces = [_line(first, SUMMARY_FIRST) if first else "", _line(last, SUMMARY_LAST) if last else ""]
        summary = " → ".join(p for p in pieces if p)
    else:
        sess.title = ""
    sess.imported_messages = len(kept)
    sess.withheld_messages = len(rows) - len(kept)
    sess.tool_calls = sum(int(r.calls or 0) for r in kept)
    sess.labels = dict(sorted(labels.items()))
    sess.sensitivity = sensitivity
    sess.summary = summary
    sess.flags = flags
    sess.updated_at = datetime.now(UTC)
    await session.flush()
    return sess


# --- Withheld rows keep no text, not even in the copies a rollback restores ---------------------------------------

# Who withheld a message or a session: an admin, a rescan or an import that found restricted data. Neither a rollback
# nor a later import ever brings the text back.
WITHHELD_LATER = ("withheld_by_admin", "withheld_by_rescan", "withheld_by_import")
# WITHHELD_BY_ADMIN (policy.py), the brain_tombstones reason of a message an admin withheld: a later import brings it
# back with no text, even after the run that created it was rolled back (an ordinary tombstone, "purged by an admin",
# leaves the record out).
WITHHELD_ROW: dict[str, Any] = {"content": "", "tool_calls": [], "labels": [], "withheld": True,
                                "sensitivity": "restricted", "truncated": False}


def withheld_later(flags: dict[str, Any] | None) -> bool:
    return any(k in (flags or {}) for k in WITHHELD_LATER)


def blank_before(before: dict[str, Any]) -> dict[str, Any]:
    """A rollback copy (import_records.before) without its text; a message's copy is marked withheld and keeps no
    fingerprint of its text either (source_fp 'w1:<state>', as a withheld row)."""
    if "deleted_row" in before:
        return {**before, "deleted_row": blank_before(dict(before["deleted_row"]))}
    out = {**before, **({"title": ""} if "title" in before else {})}
    if "content" in before:
        out.update(WITHHELD_ROW)
        if "source_fp" in before:
            out["source_fp"] = "w1:%s" % (before.get("state") or "live")
    return out


async def scrub_before(session: AsyncSession, *, message_id: int | None = None,
                       archive_session_id: int | None = None, source_key: str | None = None) -> int:
    """Blanks the rollback copies of a withheld message, or of a withheld session and all of its messages. With the
    message's source_key, also the copies kept under an older row id of the same message (a rollback and a new
    import give it a new id; import_records keep the old one)."""
    conds = []
    if message_id is not None:
        conds.append((ImportRecord.target_table == "archive_messages") & (ImportRecord.target_id == message_id))
    if source_key:
        conds.append((ImportRecord.record_type == "message") & (ImportRecord.source_key == source_key))
    if archive_session_id is not None:
        messages = select(ArchiveMessage.id).where(ArchiveMessage.session_id == archive_session_id)
        conds += [(ImportRecord.target_table == "archive_sessions") & (ImportRecord.target_id == archive_session_id),
                  (ImportRecord.target_table == "archive_messages") & ImportRecord.target_id.in_(messages),
                  ImportRecord.before["deleted_row"]["session_id"].as_integer() == archive_session_id]
    return await _blank_records(session, conds)


async def _blank_records(session: AsyncSession, conds: list[Any]) -> int:
    if not conds:
        return 0
    n = 0
    for record in (await session.scalars(select(ImportRecord).where(ImportRecord.before != {}, or_(*conds)))).all():
        blank = blank_before(dict(record.before))
        if blank != record.before:
            record.before = blank
            n += 1
    await session.flush()
    return n


# --- Promoted copies of a withheld message -------------------------------------------------------------------------

# SOURCE_REMOVED and PROMOTED_REF (policy.py): how a promoted item names its message.
BATCH = 500


def promoted_ref(source_session_id: str, position: int) -> str:
    """The source_ref of an item promoted from an archived message (review.promote_message)."""
    return PROMOTED_REF % (source_session_id, position)


def _chunks(rows: Sequence[Any], size: int = BATCH) -> Iterable[Sequence[Any]]:
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


async def _copies(session: AsyncSession, messages: Sequence[Any]) -> list[tuple[MemoryItem, Any]]:
    """(item, the message it was promoted from) for promoted_copies(); messages need id, source_key, session_id and
    position (ORM rows or selected columns)."""
    session_ids = sorted({int(m.session_id) for m in messages})
    sources = dict((await session.execute(select(ArchiveSession.id, ArchiveSession.source_session_id).where(
        ArchiveSession.id.in_(session_ids)))).all()) if session_ids else {}
    message_id = MemoryItem.flags["message_id"].as_integer()
    found: dict[int, tuple[MemoryItem, Any]] = {}
    for chunk in _chunks(messages):
        by_id = {int(m.id): m for m in chunk}
        by_key = {m.source_key: m for m in chunk if m.source_key}
        by_ref = {promoted_ref(sources[m.session_id], m.position): m for m in chunk if m.session_id in sources}
        conds = [message_id.in_(list(by_id))]
        if by_key:
            conds.append(MemoryItem.flags["message_source_key"].astext.in_(list(by_key)))
        if by_ref:
            conds.append(and_(not_(MemoryItem.flags.has_key("message_source_key")),
                              MemoryItem.source_ref.in_([r + s for r in by_ref for s in ("", SOURCE_REMOVED)]),
                              not_(exists().where(ArchiveMessage.id == message_id))))
        for item in (await session.scalars(select(MemoryItem).where(MemoryItem.origin == "promoted", or_(*conds))
                                           )).all():
            flags = item.flags or {}
            source = (by_id.get(flags.get("message_id")) or by_key.get(flags.get("message_source_key"))
                      or by_ref.get(item.source_ref.removesuffix(SOURCE_REMOVED)))
            found.setdefault(item.id, (item, source))
    return [found[k] for k in sorted(found)]


async def promoted_copies(session: AsyncSession, messages: Sequence[Any]) -> list[MemoryItem]:
    """Every memory item promoted from these archived messages, whatever its status, deleted ones too. An item names
    its message by flags.message_id; by flags.message_source_key, which a rollback and a new import keep (the row id
    changes); or, promoted by 0.6.0 (no message_source_key), by its source_ref (promoted_ref(), "(source removed)"
    or not) while its message_id names no row any more, so a later message at that position is never taken for it."""
    return [item for item, _ in await _copies(session, messages)]


@dataclass
class CopiesWithheld:
    copies: list[dict[str, Any]] = field(default_factory=list)  # store.empty_withheld_copy() of each emptied item
    versions_blanked: int = 0
    rollback_copies_blanked: int = 0
    answers_scrubbed: int = 0


async def _audit_copy(session: AsyncSession, actor: str, done: dict[str, Any], message: Any, cause: str,
                      ip: str = "") -> None:
    await audit.record(session, actor, "memory.source_withheld", "memory_item", done["item"], {
        "item": done["item"], "kind": done["kind"], "agent": done["agent"], "status_before": done["status_before"],
        "status": "quarantined", "version": done["version"], "deleted": done["deleted"],
        "versions_blanked": done["versions_blanked"], "message": int(message.id) if message is not None else None,
        "session": int(message.session_id) if message is not None else None, "cause": cause}, ip=ip)


async def withhold_copies(session: AsyncSession, *, messages: Sequence[Any] = (), message_ids: Iterable[int] = (),
                          source_keys: Iterable[str] = (), fresh_ids: Iterable[int] = (), actor: str, cause: str,
                          ip: str = "") -> CopiesWithheld:
    """Removes what withheld messages left elsewhere; the caller withholds the messages themselves.

    - their rollback copies (import_records.before), also those kept under an older row id of the same source key;
    - every memory item promoted from them (promoted_copies), emptied by store.empty_withheld_copy, each with a
      memory.source_withheld audit entry (ids, kinds, statuses, counts and the cause: admin, import, rescan,
      decisions-apply or rollback; never text);
    - the lines of history answers and the reference notes that quote them, an older row of them (the rows
      import_records name for their source keys: a rollback or a rewound import deleted it, and a new import gave the
      message a new id) or those items (quotes.scrub_quotes).
    source_keys: withheld messages this database holds no row of (decisions-apply's withhold of a message a rollback
    or an import removed): their rollback copies, the items promoted from an earlier row of them (found by
    flags.message_source_key; the audit entry names no message) and the quotes of those rows. fresh_ids: rows the
    caller's transaction inserted (an import), which nothing can have quoted yet. Idempotent. In the caller's
    transaction; the caller commits."""
    rows: dict[int, Any] = {int(m.id): m for m in messages}
    wanted = sorted({int(i) for i in message_ids} - set(rows))
    for chunk in _chunks(wanted, 1000):
        for r in (await session.execute(select(ArchiveMessage.id, ArchiveMessage.source_key, ArchiveMessage.session_id,
                                               ArchiveMessage.position).where(ArchiveMessage.id.in_(chunk)))).all():
            rows[int(r.id)] = r
    rowless = sorted(set(source_keys) - {m.source_key for m in rows.values()} - {""})
    out = CopiesWithheld()
    if not rows and not rowless:
        return out
    listed = [rows[k] for k in sorted(rows)]
    for chunk in _chunks(listed):
        conds = [(ImportRecord.target_table == "archive_messages") & ImportRecord.target_id.in_([m.id for m in chunk])]
        keys = [m.source_key for m in chunk if m.source_key]
        if keys:
            conds.append((ImportRecord.record_type == "message") & ImportRecord.source_key.in_(keys))
        out.rollback_copies_blanked += await _blank_records(session, conds)
    copies = await _copies(session, listed)
    for chunk in _chunks(rowless):
        out.rollback_copies_blanked += await _blank_records(session, [
            (ImportRecord.record_type == "message") & ImportRecord.source_key.in_(list(chunk))])
        known = {item.id for item, _ in copies}
        copies += [(item, None) for item in (await session.scalars(select(MemoryItem).where(
            MemoryItem.origin == "promoted", MemoryItem.flags["message_source_key"].astext.in_(list(chunk)))
            .order_by(MemoryItem.id))).all() if item.id not in known]
    older = await _older_rows(session, sorted({m.source_key for m in rows.values() if m.source_key} | set(rowless)))
    # A 0.6.0 copy (no message_source_key) names only the row it was promoted from: also an older row of these keys,
    # whose id the import records keep (rolled back, rewound, or a position that moved since).
    for chunk in _chunks(sorted(older - set(rows))):
        known = {item.id for item, _ in copies}
        copies += [(item, None) for item in (await session.scalars(select(MemoryItem).where(
            MemoryItem.origin == "promoted", not_(MemoryItem.flags.has_key("message_source_key")),
            MemoryItem.flags["message_id"].as_integer().in_(list(chunk))).order_by(MemoryItem.id))).all()
            if item.id not in known]
    for item, message in copies:
        done = await store.empty_withheld_copy(session, item, actor=actor)
        if done is None:
            continue
        out.copies.append(done)
        out.versions_blanked += done["versions_blanked"]
        await _audit_copy(session, actor, done, message, cause, ip)
    quoted = set(rows) | older
    out.answers_scrubbed = await scrub_quotes(session, message_ids=quoted - {int(i) for i in fresh_ids},
                                              item_ids=[i.id for i, _ in copies])
    return out


async def _older_rows(session: AsyncSession, keys: Sequence[str]) -> set[int]:
    """The archive_messages row ids import_records name for these source keys: the rows a message was created,
    withheld or updated as, also those a rollback or a later import deleted since. Never a 'duplicate' record's: it
    names the row of the message it duplicates."""
    ids: set[int] = set()
    for chunk in _chunks(keys):
        ids |= {int(i) for i in (await session.scalars(select(ImportRecord.target_id).where(
            ImportRecord.record_type == "message", ImportRecord.target_table == "archive_messages",
            ImportRecord.action.in_(OLD_ROW_ACTIONS), ImportRecord.target_id.is_not(None),
            ImportRecord.source_key.in_(list(chunk))))).all()}
    return ids


def _holds_text() -> Any:
    """SQL: a memory item that store.empty_withheld_copy() would still change (it, or one of its versions, holds
    text: store.emptied() and store.blank_version())."""
    version_text = exists().where(MemoryItemVersion.item_id == MemoryItem.id, or_(
        MemoryItemVersion.body != "", cast(MemoryItemVersion.tags, Text) != "[]",
        MemoryItemVersion.title.not_in((store.REDACTED_TITLE, store.WITHHELD_COPY_TITLE))))
    return or_(not_(MemoryItem.flags.has_key("source_withheld")), MemoryItem.title != store.WITHHELD_COPY_TITLE,
               MemoryItem.body != "", cast(MemoryItem.tags, Text) != "[]", version_text)


async def withheld_copies(session: AsyncSession) -> list[tuple[MemoryItem, Any]]:
    """Promoted items that still hold text although their message is withheld, with that message (None when no row
    of it is left): flagged source_withheld (0.6.0 quarantined them but kept the text); their message_id row is
    withheld; their message_source_key row is withheld, or has an admin's withhold tombstone; (promoted by 0.6.0)
    their source_ref names a withheld row while their message_id names none, or the import records name the source
    key their gone row held and that key is withheld; or their conversation is withheld for restricted data
    (policy.withheld_source() matches the same items)."""
    items = list((await session.scalars(select(MemoryItem).where(MemoryItem.origin == "promoted", _holds_text())
                                        .order_by(MemoryItem.id))).all())
    if not items:
        return []
    ids = sorted({f["message_id"] for i in items if isinstance((f := i.flags or {}).get("message_id"), int)})
    keys = sorted({f["message_source_key"] for i in items
                   if isinstance((f := i.flags or {}).get("message_source_key"), str)})
    columns = (ArchiveMessage.id, ArchiveMessage.source_key, ArchiveMessage.session_id, ArchiveMessage.withheld)
    by_id = {r.id: r for r in (await session.execute(select(*columns).where(ArchiveMessage.id.in_(ids)))).all()}
    by_key = {r.source_key: r for r in (await session.execute(select(*columns).where(
        ArchiveMessage.source_key.in_(keys)))).all()} if keys else {}
    stones = set((await session.scalars(select(BrainTombstone.source_key).where(
        BrainTombstone.source_key.in_(keys), BrainTombstone.reason == WITHHELD_BY_ADMIN))).all()) if keys else set()
    by_ref: dict[str, Any] = {}
    legacy = sorted({i.source_ref.removesuffix(SOURCE_REMOVED) for i in items if "message_source_key" not in
                     (i.flags or {}) and (i.flags or {}).get("message_id") not in by_id})
    if legacy:
        ref = func.format(PROMOTED_REF, ArchiveSession.source_session_id, ArchiveMessage.position)
        for r in (await session.execute(select(*columns, ref.label("ref"))
                                        .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                                        .where(ArchiveMessage.withheld.is_(True), ref.in_(legacy)))).all():
            by_ref.setdefault(r.ref, r)
    # 0.6.0 copies whose row is gone: the source key the import records name for that row, when it is withheld.
    gone_ids = sorted({f["message_id"] for i in items if "message_source_key" not in (f := i.flags or {})
                       and isinstance(f.get("message_id"), int) and f["message_id"] not in by_id})
    old_key: dict[int, str] = {}
    withheld_keys: set[str] = set()
    for chunk in _chunks(gone_ids):
        old_key.update((int(t), k) for t, k in (await session.execute(select(
            ImportRecord.target_id, ImportRecord.source_key).where(
            ImportRecord.record_type == "message", ImportRecord.target_table == "archive_messages",
            ImportRecord.action.in_(OLD_ROW_ACTIONS), ImportRecord.target_id.in_(list(chunk))))).all())
    for chunk in _chunks(sorted(set(old_key.values()))):
        withheld_keys |= set((await session.scalars(select(ArchiveMessage.source_key).where(
            ArchiveMessage.source_key.in_(list(chunk)), ArchiveMessage.withheld.is_(True)))).all())
        withheld_keys |= set((await session.scalars(select(BrainTombstone.source_key).where(
            BrainTombstone.source_key.in_(list(chunk)), BrainTombstone.reason == WITHHELD_BY_ADMIN))).all())
    # Copies of a conversation withheld for restricted data, whatever became of their row (finding 2).
    sessions = sorted({f["archive_session_id"] for i in items
                       if isinstance((f := i.flags or {}).get("archive_session_id"), int)})
    restricted = set((await session.scalars(select(ArchiveSession.id).where(
        ArchiveSession.id.in_(sessions), ArchiveSession.sensitivity == "restricted"))).all()) if sessions else set()
    found = []
    for item in items:
        flags = item.flags or {}
        row, key = by_id.get(flags.get("message_id")), flags.get("message_source_key")
        keyed = by_key.get(key) if key else None
        if row is not None and row.withheld:
            found.append((item, row))
        elif keyed is not None and keyed.withheld:
            found.append((item, keyed))
        elif key and key in stones:
            found.append((item, keyed))
        elif not key and row is None and item.source_ref.removesuffix(SOURCE_REMOVED) in by_ref:
            found.append((item, by_ref[item.source_ref.removesuffix(SOURCE_REMOVED)]))
        elif not key and row is None and old_key.get(flags.get("message_id")) in withheld_keys:
            found.append((item, None))
        elif flags.get("archive_session_id") in restricted:
            found.append((item, row))
        elif flags.get("source_withheld"):
            found.append((item, row))
    return found


def _withheld_keys() -> list[Any]:
    """SQL: the import_records (rollback copies) of a message whose source key is withheld: a withheld row has it, or
    an admin's withhold tombstone."""
    message = ImportRecord.record_type == "message"
    return [message & ImportRecord.source_key.in_(select(BrainTombstone.source_key).where(
                BrainTombstone.reason == WITHHELD_BY_ADMIN)),
            message & ImportRecord.source_key.in_(select(ArchiveMessage.source_key).where(
                ArchiveMessage.withheld.is_(True), ArchiveMessage.source_key != ""))]


def _record_holds_text() -> Any:
    """SQL: a rollback copy blank_before() would still change (a message's copy that is not marked withheld)."""
    row = func.coalesce(ImportRecord.before["deleted_row"], ImportRecord.before, type_=JSONB)
    return and_(ImportRecord.before != {}, row["withheld"].astext.is_distinct_from("true"))


async def withheld_rollback_copies(session: AsyncSession) -> int:
    """How many rollback copies of withheld messages still hold text: those a withhold made by 0.6.0 left under an
    older row id of the message (it blanked them by row id only)."""
    return int(await session.scalar(select(func.count()).select_from(ImportRecord).where(
        _record_holds_text(), or_(*_withheld_keys()))) or 0)


async def sweep_withheld_records(session: AsyncSession) -> int:
    """Blanks the rollback copies withheld_rollback_copies() counts; returns how many. The caller holds the import
    lock and commits."""
    return await _blank_records(session, [and_(_record_holds_text(), or_(*_withheld_keys()))])


async def sweep_withheld_copies(session: AsyncSession, *, actor: str, cause: str = "startup-check"
                                ) -> list[dict[str, Any]]:
    """Empties every item withheld_copies() finds, with an audit entry each, and removes the quotes of them and of
    their messages; quarantines again an emptied copy that 0.6.0 approved or reopened (store.quarantine_emptied_copy).
    Returns store.empty_withheld_copy() (or quarantine_emptied_copy()) of each. The caller holds the import lock and
    commits."""
    found = await withheld_copies(session)
    out = []
    for item, message in found:
        done = await store.empty_withheld_copy(session, item, actor=actor)
        if done is None:
            continue
        out.append(done)
        await _audit_copy(session, actor, done, message, cause)
    for item in (await session.scalars(select(MemoryItem).where(
            MemoryItem.origin == "promoted", MemoryItem.flags.has_key("source_withheld"),
            MemoryItem.status.in_(("approved", "pending_review")), not_(_holds_text())).order_by(MemoryItem.id))).all():
        done = await store.quarantine_emptied_copy(session, item, actor=actor)
        if done is not None:
            out.append(done)
            await _audit_copy(session, actor, done, None, cause)
    if found:
        await scrub_quotes(session, message_ids=[m.id for _, m in found if m is not None],
                           item_ids=[i.id for i, _ in found])
    return out
