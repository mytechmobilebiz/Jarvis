"""Copies of JARVIS Brain text outside the brain's tables, and their removal.

A history answer (supervisor.recall: tasks.result.data.answer, appended as it is to supervisor_requests.summary)
quotes archived messages and approved items; reference notes (tasks.result.data.memory_notes) name approved items.
When a source is withheld, deleted, purged, redacted, re-cleaned by a rescan or removed by a rollback, its copies go
too: each line that quotes it becomes REMOVED, in the task's result and in the request's summary, and a note naming a
removed item is dropped. An answer records its sources (data.sources, one per line; None for headings and notes).
After a rescan, refresh_quotes() re-cleans every quoting line with the current detectors as well.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ArchiveMessage, MemoryItem, SupervisorRequest, Task
from . import text as brain_text

REMOVED = "- [removed: its source was withheld, changed or deleted in the JARVIS Brain]"


async def scrub_quotes(session: AsyncSession, *, message_ids: Iterable[int] = (), item_ids: Iterable[int] = ()) -> int:
    """Removes every copy of these archived messages and memory items from task results and request summaries.
    Returns how many task results changed."""
    messages, items = {int(m) for m in message_ids}, {int(i) for i in item_ids}
    if not messages and not items:
        return 0
    data_of = Task.result["data"]
    tasks = (await session.scalars(select(Task).where(or_(data_of.has_key("sources"),
                                                         data_of.has_key("memory_notes"))))).all()
    changed = 0
    for task in tasks:
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
    """After a rescan: a quoting line that the current detectors would change, or whose source is gone, withheld or
    deleted, is removed as scrub_quotes() removes it. Returns how many task results changed."""
    data_of = Task.result["data"]
    messages: set[int] = set()
    items: set[int] = set()
    stale_messages: set[int] = set()
    stale_items: set[int] = set()
    for task in (await session.scalars(select(Task).where(data_of.has_key("sources")))).all():
        data = (task.result or {}).get("data") or {}
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
            MemoryItem.id.in_(items), MemoryItem.deleted_at.is_(None)))).all())
        stale_items |= items - live
    return await scrub_quotes(session, message_ids=stale_messages, item_ids=stale_items)
