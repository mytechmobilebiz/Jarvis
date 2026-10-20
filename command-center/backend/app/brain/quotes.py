"""Copies of JARVIS Brain text outside the brain's tables, and their removal.

A history answer (supervisor.recall: tasks.result.data.answer, appended as it is to supervisor_requests.summary)
quotes archived messages and approved items; reference notes (tasks.result.data.memory_notes) name approved items.
When a source is withheld, deleted, purged, redacted, re-cleaned by a rescan or removed by a rollback, its copies go
too: each line that quotes it becomes REMOVED, in the task's result and in the request's summary, and a note naming a
removed item is dropped. An answer records its sources (data.sources, one per line; None for headings and notes).
After a rescan, and at start-up, refresh_quotes() re-cleans every quoting line with the current detectors as well.

A task that quotes (the Supervisor's recall, a request summary that copies a recall's answer, reference notes) holds
lock_quotes() shared from before its first read until it commits; scrub_quotes() and refresh_quotes() take it
exclusively before they look for quotes. A quote is therefore either committed before a removal looks for it, or read
after the removal committed. Nothing holds it around a call to an AI model (a summary that copies an answer never
calls one), and a recall never waits for it: while a removal holds or waits for it, the worker queues the recall
again (QuotesBusy), so removals wait only for quoting tasks that are already running.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import literal_column, not_, or_, select
from sqlalchemy import text as sql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ArchiveMessage, MemoryItem, SupervisorRequest, Task
from . import text as brain_text

REMOVED = "- [removed: its source was withheld, changed or deleted in the JARVIS Brain]"
QUOTE_LOCK = -1  # the advisory lock (importer.LOCK_KEY, -1): import runs hold (LOCK_KEY, run id), and ids start at 1


# The tasks that can quote a message or an item are found through tasks_quotes_idx (0007_brain_withhold.sql). Its
# expressions name the keys literally, so these do too: with the keys sent as parameters, the index is never used and
# every task is read.
_DATA = Task.result.op("->", return_type=JSONB)(literal_column("'data'"))
_SOURCES = _DATA.op("->", return_type=JSONB)(literal_column("'sources'"))
_NOTES = _DATA.op("->", return_type=JSONB)(literal_column("'memory_notes'"))
QUOTE_BATCH = 500  # lookups per query


def _quoting(messages: set[int], items: set[int]) -> Iterable[list[Any]]:
    """Conditions, QUOTE_BATCH at a time: a task whose answer's sources name one of these messages or items, or whose
    reference notes name one of these items."""
    conds = ([_SOURCES.contains([{"message": m}]) for m in sorted(messages)]
             + [_SOURCES.contains([{"item": i}]) for i in sorted(items)]
             + [_NOTES.contains([{"id": i}]) for i in sorted(items)])
    for i in range(0, len(conds), QUOTE_BATCH):
        yield conds[i:i + QUOTE_BATCH]


class QuotesBusy(Exception):
    """A JARVIS Brain removal holds (or waits for) the quote lock; the worker queues the quoting task again."""


async def lock_quotes(session: AsyncSession, *, shared: bool = False, wait: bool = True) -> bool:
    """The quote lock until the session's transaction ends: shared by the tasks that quote, exclusive for a removal.
    With wait=False, False when a removal holds it."""
    from .importer import LOCK_KEY  # the importer imports this module

    name = "pg_advisory_xact_lock" if wait else "pg_try_advisory_xact_lock"
    query = sql("SELECT %s%s(CAST(:k AS int), CAST(:i AS int))" % (name, "_shared" if shared else ""))
    got = await session.scalar(query, {"k": LOCK_KEY, "i": QUOTE_LOCK})
    return True if wait else bool(got)


async def scrub_quotes(session: AsyncSession, *, message_ids: Iterable[int] = (), item_ids: Iterable[int] = ()) -> int:
    """Removes every copy of these archived messages and memory items from task results and request summaries.
    Returns how many task results changed."""
    messages, items = {int(m) for m in message_ids}, {int(i) for i in item_ids}
    if not messages and not items:
        return 0
    await lock_quotes(session)
    found: dict[int, Task] = {}
    for conds in _quoting(messages, items):
        for task in (await session.scalars(select(Task).where(or_(*conds)))).all():
            found.setdefault(task.id, task)
    changed = 0
    for task in (found[k] for k in sorted(found)):
        result: dict[str, Any] = dict(task.result or {})
        data: dict[str, Any] = dict(result.get("data") or {})
        answer, sources = list(data.get("answer") or []), list(data.get("sources") or [])
        removed: list[str] = []
        for i, source in enumerate(sources):
            if (isinstance(source, dict) and i < len(answer)
                    and (source.get("message") in messages or source.get("item") in items)):
                removed.append(str(answer[i]))
                answer[i], sources[i] = REMOVED, None
        notes = data.get("memory_notes")
        kept = [n for n in notes if not (isinstance(n, dict) and n.get("id") in items)] if isinstance(notes, list) else notes
        if not removed and kept == notes:
            continue
        if removed:
            data.update(answer=answer, sources=sources)
        if isinstance(notes, list):
            data["memory_notes"] = kept
        task.result = {**result, "data": data}
        changed += 1
        if removed and task.request_id:
            request = await session.get(SupervisorRequest, task.request_id)
            if request is not None and request.summary:
                summary = request.summary
                for line in removed:
                    summary = summary.replace(line, REMOVED)
                request.summary = summary
    await session.flush()
    return changed


async def refresh_quotes(session: AsyncSession) -> int:
    """After a rescan, and at start-up: a quoting line that the current detectors would change, or whose source is
    gone, withheld or deleted, is removed as scrub_quotes() removes it; so is a reference note naming an item that is
    gone, deleted or emptied because its source message was withheld (a task that finished during a withhold kept
    them before 0.6.1's quote lock). Returns how many task results changed."""
    await lock_quotes(session)
    data_of = Task.result["data"]
    messages: set[int] = set()
    items: set[int] = set()
    stale_messages: set[int] = set()
    stale_items: set[int] = set()
    for task in (await session.scalars(select(Task).where(or_(data_of.has_key("sources"),
                                                              data_of.has_key("memory_notes"))))).all():
        data = (task.result or {}).get("data") or {}
        notes = data.get("memory_notes")
        if isinstance(notes, list):
            items.update(n["id"] for n in notes if isinstance(n, dict) and isinstance(n.get("id"), int))
        for line, source in zip(data.get("answer") or [], data.get("sources") or []):
            if not isinstance(source, dict):
                continue
            cleaned = brain_text.clean(str(line), "chat")
            stale = cleaned.withheld or cleaned.text != line
            if "message" in source:
                messages.add(source["message"])
                if stale:
                    stale_messages.add(source["message"])
            elif "item" in source:
                items.add(source["item"])
                if stale:
                    stale_items.add(source["item"])
    if messages:
        live = set((await session.scalars(select(ArchiveMessage.id).where(
            ArchiveMessage.id.in_(messages), ArchiveMessage.withheld.is_(False)))).all())
        stale_messages |= messages - live
    if items:
        live = set((await session.scalars(select(MemoryItem.id).where(
            MemoryItem.id.in_(items), MemoryItem.deleted_at.is_(None),
            not_(MemoryItem.flags.has_key("source_withheld"))))).all())
        stale_items |= items - live
    return await scrub_quotes(session, message_ids=stale_messages, item_ids=stale_items)
