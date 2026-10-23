"""Copies of JARVIS Brain text outside the brain's tables, and their removal.

A history answer (supervisor.recall: tasks.result.data.answer, appended as it is to supervisor_requests.summary)
quotes archived messages and approved items; reference notes (tasks.result.data.memory_notes) name approved items.
When a source is withheld, deleted, purged, redacted, re-cleaned by a rescan or removed by a rollback, its copies go
too: each line that quotes it becomes REMOVED, in the task's result and in the request's summary, and a note naming a
removed item is dropped. An answer records its sources (data.sources, one per line; None for headings and notes).
After a rescan, and at start-up, refresh_quotes() re-cleans every quoting line with the current detectors as well,
and removes a request summary's quoting line that no task holds any more (QUOTE_LINE: a line 0.6.0's unlocked
summary copied after the withhold had already removed it from the task).

A quote also names its source: recall writes "(source: <conversation title> · message N)". When an admin later hides
that conversation's title (a withhold's hide_title, down the lineage), the title stays in every quote of the
conversation's other, readable messages. relabel_hidden_titles() rewrites such a line's source as recall writes it for
a conversation without a title ("(source: session <id> · message N)"; the quote itself stays): at the withhold, at
start-up and after a rescan (refresh_quotes), and at read time (hidden_titles(), strip_dead()).

Every reader applies the same rule at read time (dead_sources(), strip_dead(), strip_summary(): app/api/work.py
task_views and app/api/supervisor.py request_view), so a quote a start-up check could not reach yet (it was skipped
while a task held the quote lock) is never served either.

A task that quotes (the Supervisor's recall, a request summary that copies a recall's answer, reference notes) holds
lock_quotes() shared from before its first read until it commits; scrub_quotes() and refresh_quotes() take it
exclusively before they look for quotes. A quote is therefore either committed before a removal looks for it, or read
after the removal committed. Nothing holds it around a call to an AI model (a summary that copies an answer never
calls one), and a recall never waits for it: while a removal holds or waits for it, the worker queues the recall
again (QuotesBusy), so removals wait only for quoting tasks that are already running.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from sqlalchemy import literal_column, not_, or_, select
from sqlalchemy import text as sql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Approval, ArchiveMessage, Artifact, ArchiveSession, MemoryItem, SupervisorRequest, Task
from . import text as brain_text

REMOVED = "- [removed: its source was withheld, changed or deleted in the JARVIS Brain]"
QUOTE_LOCK = -1  # the advisory lock (importer.LOCK_KEY, -1): import runs hold (LOCK_KEY, run id), and ids start at 1
# The shapes of supervisor.recall's quoting lines (_message_line, _item_line), at the end of a line. Only these are
# quotes: a summary's other lines (the digest of other agents' results, which may say "(source: ...)" too) stay.
QUOTE_LINE = re.compile(r"\(source: .+ · message \d+\)$|\(source: .+, item #\d+\)$")
MESSAGE_TAIL = re.compile(r" · message (\d+)\)$")  # the end of a message quote (recall._message_line)
SOURCE_START = " (source: "


# The tasks that can quote a message or an item are found through tasks_quotes_idx (0007_brain_withhold.sql). Its
# expressions name the keys literally, so these do too: with the keys sent as parameters, the index is never used and
# every task is read.
_DATA = Task.result.op("->", return_type=JSONB)(literal_column("'data'"))
_SOURCES = _DATA.op("->", return_type=JSONB)(literal_column("'sources'"))
_NOTES = _DATA.op("->", return_type=JSONB)(literal_column("'memory_notes'"))
QUOTE_BATCH = 500  # lookups per query
# An AI-written draft (app/agents/drafting.write) that had approved memory items in its prompt records them (review 3,
# readers): artifacts.data.memory_items, and the task's result.data.memory_prompt_items. When one of them is withheld,
# purged, deleted, emptied or redacted, the draft's text and title, the task's summary and the approval cards naming
# the draft are replaced: the model may have copied the item's text into it. Drafts written before 0.6.2 recorded
# nothing and are not reached (RELEASE_NOTES_0.6.2.md).
DRAFT_REMOVED = ("[removed: this draft was written with JARVIS Brain memory that was later withheld, changed or "
                 "deleted; draft it again]")
DRAFT_REMOVED_TITLE = "[removed draft]"
DRAFT_TASK_REMOVED = "Draft removed: it was written with JARVIS Brain memory that was later withheld, changed or deleted"
_PROMPT_ITEMS = _DATA.op("->", return_type=JSONB)(literal_column("'memory_prompt_items'"))
_ARTIFACT_ITEMS = Artifact.data.op("->", return_type=JSONB)(literal_column("'memory_items'"))


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
    drafts = await scrub_drafts(session, items) if items else 0
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
    return changed + drafts


def _ids(value: Any) -> set[int]:
    return {int(i) for i in value if isinstance(i, int)} if isinstance(value, list) else set()


async def scrub_drafts(session: AsyncSession, items: Iterable[int]) -> int:
    """The drafts written with one of these memory items in their AI prompt, their tasks' summaries and the approval
    cards naming them: replaced by DRAFT_REMOVED (the stored rows; the caller holds the quote lock, exclusively for a
    removal, shared for the task that is storing the draft). How many drafts and tasks changed."""
    wanted = sorted({int(i) for i in items})
    if not wanted:
        return 0
    changed = 0
    arts: dict[int, Artifact] = {}
    tasks: dict[int, Task] = {}
    for i in range(0, len(wanted), QUOTE_BATCH):
        part = wanted[i:i + QUOTE_BATCH]
        for a in (await session.scalars(select(Artifact).where(or_(*[_ARTIFACT_ITEMS.contains([n]) for n in part]))
                                        )).all():
            arts.setdefault(a.id, a)
        for t in (await session.scalars(select(Task).where(or_(*[_PROMPT_ITEMS.contains([n]) for n in part])))).all():
            tasks.setdefault(t.id, t)
    for a in (arts[k] for k in sorted(arts)):
        if (a.data or {}).get("memory_removed") and a.content == DRAFT_REMOVED:
            continue
        a.title, a.content = DRAFT_REMOVED_TITLE, DRAFT_REMOVED
        a.data = {**(a.data or {}), "memory_removed": True}
        changed += 1
        if a.task_id is not None and a.task_id not in tasks:
            t = await session.get(Task, a.task_id)
            if t is not None:
                tasks[t.id] = t
    for t in (tasks[k] for k in sorted(tasks)):
        result = dict(t.result or {})
        data = dict(result.get("data") or {})
        if data.get("memory_removed") and t.summary == DRAFT_TASK_REMOVED:
            continue
        t.summary = DRAFT_TASK_REMOVED
        t.result = {**result, "summary": DRAFT_TASK_REMOVED, "data": {**data, "memory_removed": True}}
        changed += 1
    if arts:
        ids = sorted(arts)
        for ap in (await session.scalars(select(Approval).where(or_(
                *[Approval.details["draft"]["artifact_id"].astext == str(n) for n in ids],
                *[Approval.details["binding"]["artifact_id"].astext == str(n) for n in ids])))).all():
            details = dict(ap.details or {})
            draft = dict(details.get("draft") or {})
            if draft:
                draft.update(title=DRAFT_REMOVED_TITLE, excerpt=DRAFT_REMOVED)
                details["draft"] = draft
            if details.get("target"):
                details["target"] = "a draft whose text was removed (#%s)" % (
                    (details.get("binding") or {}).get("artifact_id") or draft.get("artifact_id") or "?")
            ap.details = details
    await session.flush()
    return changed


async def dead_items(session: AsyncSession, items: Iterable[int]) -> set[int]:
    """The memory items among these that must not be shown or used: gone, deleted, emptied (source_withheld),
    promoted from a withheld message (policy.withheld_source()), or legacy 0.6.0/0.6.1 data under the lock."""
    from .policy import legacy_hidden_item, withheld_source

    wanted = {int(i) for i in items}
    if not wanted:
        return set()
    return wanted - set((await session.scalars(select(MemoryItem.id).where(
        MemoryItem.id.in_(sorted(wanted)), MemoryItem.deleted_at.is_(None),
        not_(MemoryItem.flags.has_key("source_withheld")), not_(withheld_source()),
        not_(legacy_hidden_item())))).all())


async def guard_drafts(session: AsyncSession, views: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The read-time guard of drafts (GET /api/artifacts, /api/artifacts/{id}): a draft whose prompt memory is dead
    (dead_items) is served as DRAFT_REMOVED, whatever the stored row still holds (a removal that committed while the
    start-up check could not run yet). Changes the views in place and returns them."""
    used = {i for v in views for i in _ids((v.get("data") or {}).get("memory_items"))}
    dead = await dead_items(session, used) if used else set()
    for v in views:
        if _ids((v.get("data") or {}).get("memory_items")) & dead:
            v["title"] = DRAFT_REMOVED_TITLE
            if "content" in v:
                v["content"] = DRAFT_REMOVED
    return views


async def _dead_draft_items(session: AsyncSession) -> set[int]:
    """The dead memory items (dead_items) that a stored draft or its task still holds unremoved (refresh_quotes)."""
    used: set[int] = set()
    for value in (await session.scalars(select(_ARTIFACT_ITEMS).where(
            Artifact.data.has_key("memory_items"), not_(Artifact.data.has_key("memory_removed"))))).all():
        used |= _ids(value)
    for value in (await session.scalars(select(_PROMPT_ITEMS).where(
            Task.result["data"].has_key("memory_prompt_items"),
            not_(Task.result["data"].has_key("memory_removed"))))).all():
        used |= _ids(value)
    return await dead_items(session, used) if used else set()


def hidden_label(line: str, source_session_id: str) -> str:
    """A message quote line naming its conversation as recall names one without a title ("session <id>"), for a
    conversation whose title an admin hid. Everything from the line's first " (source: " on is replaced, so nothing of
    the hidden title stays even when the title or the quote holds that text itself (the quote may then lose its end:
    fail closed). REMOVED when the line is not a message quote. Unchanged when it already names the session."""
    end = MESSAGE_TAIL.search(line.rstrip())
    start = line.find(SOURCE_START)
    if end is None or start < 0 or start > end.start():
        return REMOVED
    return f"{line[:start]}{SOURCE_START}session {source_session_id} · message {end.group(1)})"


async def hidden_titles(session: AsyncSession, messages: Iterable[int],
                        session_ids: Iterable[int] | None = None) -> dict[int, str]:
    """{message id: source session id} of these quoted messages whose conversation's title an admin hid
    (flags.title_withheld); with session_ids, only messages of those conversations."""
    wanted = sorted({int(m) for m in messages})
    out: dict[int, str] = {}
    only = None if session_ids is None else sorted({int(i) for i in session_ids})
    for i in range(0, len(wanted), 1000):
        query = (select(ArchiveMessage.id, ArchiveSession.source_session_id)
                 .join(ArchiveSession, ArchiveSession.id == ArchiveMessage.session_id)
                 .where(ArchiveMessage.id.in_(wanted[i:i + 1000]),
                        ArchiveSession.flags["title_withheld"].astext == "true"))
        if only is not None:
            query = query.where(ArchiveSession.id.in_(only))
        out.update({int(m): str(sid) for m, sid in (await session.execute(query)).all()})
    return out


def relabel(data: dict[str, Any], hidden: dict[int, str]) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """A task result's data whose quote lines of the `hidden` messages name the conversation without its hidden title
    (hidden_label()), and the (old line, new line) pairs it changed. A copy; the data given is not changed."""
    answer, sources = list(data.get("answer") or []), list(data.get("sources") or [])
    changes: list[tuple[str, str]] = []
    for i, source in enumerate(sources):
        if i >= len(answer) or not isinstance(source, dict) or not isinstance(source.get("message"), int):
            continue
        sid = hidden.get(source["message"])
        if sid is None:
            continue
        line = str(answer[i])
        new = hidden_label(line, sid)
        if new != line:
            changes.append((line, new))
            answer[i] = new
            if new == REMOVED:
                sources[i] = None
    if not changes:
        return data, []
    return {**data, "answer": answer, "sources": sources}, changes


def replace_lines(summary: str, changes: Iterable[tuple[str, str]]) -> str:
    """A request summary with each of its lines that equals an old line replaced by the new one."""
    swap = dict(changes)
    if not swap or not summary:
        return summary
    return "\n".join(swap.get(line, line) for line in summary.split("\n"))


async def _quoting_tasks(session: AsyncSession, messages: set[int] | None) -> list[Task]:
    """The tasks whose answer quotes one of these messages (every task with an answer's sources for None)."""
    if messages is None:
        return list((await session.scalars(select(Task).where(Task.result["data"].has_key("sources"))
                                           .order_by(Task.id))).all())
    found: dict[int, Task] = {}
    for conds in _quoting(messages, set()):
        for task in (await session.scalars(select(Task).where(or_(*conds)))).all():
            found.setdefault(task.id, task)
    return [found[k] for k in sorted(found)]


def _quoted_messages(task: Task) -> set[int]:
    data = (task.result or {}).get("data") or {}
    return {s["message"] for s in data.get("sources") or [] if isinstance(s, dict) and isinstance(s.get("message"), int)}


async def relabel_hidden_titles(session: AsyncSession, *, session_ids: Iterable[int] | None = None,
                                apply: bool = True) -> int:
    """Rewrites every stored quote line that names a conversation by a title an admin has hidden (hidden_label()), in
    task results and in their requests' summaries; with session_ids, only quotes of those conversations' messages (a
    withhold that just hid them). With apply=False only counts (brain-import status: no lock, nothing changed).
    Returns how many task results changed (or would)."""
    if apply:
        await lock_quotes(session)
    only = None if session_ids is None else {int(i) for i in session_ids}
    if only is not None and not only:
        return 0
    messages = None if only is None else set((await session.scalars(select(ArchiveMessage.id).where(
        ArchiveMessage.session_id.in_(sorted(only))))).all())
    if messages is not None and not messages:
        return 0
    tasks = await _quoting_tasks(session, messages)
    quoted = {task.id: _quoted_messages(task) for task in tasks}
    every = await hidden_titles(session, set().union(*quoted.values()) if quoted else set(), only)
    changed = 0
    for task in tasks:
        hidden = {m: every[m] for m in quoted[task.id] if m in every}
        if not hidden:
            continue
        result: dict[str, Any] = dict(task.result or {})
        data, changes = relabel(dict(result.get("data") or {}), hidden)
        if not changes:
            continue
        changed += 1
        if not apply:
            continue
        task.result = {**result, "data": data}
        if task.request_id:
            request = await session.get(SupervisorRequest, task.request_id)
            if request is not None and request.summary:
                request.summary = replace_lines(request.summary, changes)
    if apply:
        await session.flush()
    return changed


async def refresh_quotes(session: AsyncSession) -> int:
    """After a rescan, and at start-up: a quoting line that the current detectors would change, or whose source is
    gone, withheld or deleted, is removed as scrub_quotes() removes it; so is a reference note naming an item that is
    gone, deleted, emptied because its source message was withheld, or promoted from a withheld message that has not
    been emptied yet (policy.withheld_source(); a task that finished during a withhold kept them before 0.6.1's quote
    lock). Then a request summary's quoting line that none of its tasks holds any more becomes REMOVED (0.6.0's
    summary could copy a line after the withhold removed it from the task). Returns how many task results and
    request summaries changed."""
    await lock_quotes(session)
    stale_messages, stale_items = await _stale_sources(session)
    changed = await scrub_quotes(session, message_ids=stale_messages, item_ids=stale_items)
    changed += await scrub_drafts(session, await _dead_draft_items(session))  # drafts written with removed memory
    changed += await relabel_hidden_titles(session)  # quotes naming a conversation by a title an admin hid
    for request in await _orphan_summaries(session):
        request.summary = strip_summary(request.summary, await held_lines(session, request.id))
        changed += 1
    await session.flush()
    return changed


async def removed_sources(session: AsyncSession) -> int:
    """How many task results and request summaries still quote a source refresh_quotes() would remove
    (brain-import status), read-only: no lock, nothing changed."""
    stale_messages, stale_items = await _stale_sources(session)
    found: set[int] = set()
    for conds in _quoting(stale_messages, stale_items):
        found |= set((await session.scalars(select(Task.id).where(or_(*conds)))).all())
    found |= {-i for i in await _dead_draft_items(session)}  # drafts written with removed memory (one per item)
    titled = await relabel_hidden_titles(session, apply=False)  # quotes naming a hidden title
    return len(found) + titled + len(await _orphan_summaries(session))


async def _orphan_summaries(session: AsyncSession) -> list[SupervisorRequest]:
    """The history requests (plan.local_only) whose summary holds a quoting line none of their tasks holds."""
    out = []
    for request in (await session.scalars(select(SupervisorRequest).where(
            SupervisorRequest.plan["local_only"].astext == "true", SupervisorRequest.summary != "")
            .order_by(SupervisorRequest.id))).all():
        if strip_summary(request.summary, await held_lines(session, request.id)) != request.summary:
            out.append(request)
    return out


async def held_lines(session: AsyncSession, request_id: int) -> set[str]:
    """The answer lines a request's tasks hold now."""
    held: set[str] = set()
    for answer in (await session.scalars(select(Task.result["data"]["answer"]).where(
            Task.request_id == request_id))).all():
        if isinstance(answer, list):
            held |= {str(line) for line in answer}
    return held


def strip_summary(summary: str, held: set[str]) -> str:
    """A summary without the quoting lines (QUOTE_LINE) its tasks no longer hold: each becomes REMOVED. Other lines
    (headings, notes, other agents' digests) stay as they are."""
    lines = summary.split("\n")
    out = [REMOVED if QUOTE_LINE.search(line.rstrip()) and line not in held else line for line in lines]
    return "\n".join(out)


async def dead_sources(session: AsyncSession, datas: Iterable[dict[str, Any]]) -> tuple[set[int], set[int]]:
    """(messages, items) that these task results quote or name and that must not be shown: a message withheld or
    gone; an item gone, deleted, emptied (source_withheld), or promoted from a withheld message
    (policy.withheld_source()); and, while the legacy lock is set, a message or an item of the legacy 0.6.0/0.6.1
    data (policy.legacy_hidden_*: 0.6.2 plan 8.1). The read-time guard of every task and request reader."""
    from .policy import legacy_hidden_item, legacy_hidden_message, withheld_source

    messages: set[int] = set()
    items: set[int] = set()
    for data in datas:
        for source in data.get("sources") or []:
            if isinstance(source, dict):
                if isinstance(source.get("message"), int):
                    messages.add(source["message"])
                if isinstance(source.get("item"), int):
                    items.add(source["item"])
        for note in data.get("memory_notes") or []:
            if isinstance(note, dict) and isinstance(note.get("id"), int):
                items.add(note["id"])
        items |= _ids(data.get("memory_prompt_items"))
    dead_m = messages - set((await session.scalars(select(ArchiveMessage.id).where(
        ArchiveMessage.id.in_(messages), ArchiveMessage.withheld.is_(False),
        not_(legacy_hidden_message())))).all()) if messages else set()
    dead_i = items - set((await session.scalars(select(MemoryItem.id).where(
        MemoryItem.id.in_(items), MemoryItem.deleted_at.is_(None), not_(MemoryItem.flags.has_key("source_withheld")),
        not_(withheld_source()), not_(legacy_hidden_item())))).all()) if items else set()
    return dead_m, dead_i


def strip_dead(data: dict[str, Any], dead_m: set[int], dead_i: set[int],
               hidden: dict[int, str] | None = None) -> dict[str, Any]:
    """A task result's data without the lines quoting a dead source (each becomes REMOVED, as scrub_quotes() writes
    it), without the notes naming a dead item, and with the quote lines of `hidden` messages (hidden_titles(): their
    conversation's title was hidden) naming the conversation without that title. A copy; the stored result is not
    changed."""
    out = dict(data)
    answer, sources = list(data.get("answer") or []), list(data.get("sources") or [])
    if any(isinstance(src, dict) and (src.get("message") in dead_m or src.get("item") in dead_i) for src in sources):
        for i, source in enumerate(sources):
            if isinstance(source, dict) and i < len(answer) and (source.get("message") in dead_m
                                                                  or source.get("item") in dead_i):
                answer[i], sources[i] = REMOVED, None
        out.update(answer=answer, sources=sources)
    notes = data.get("memory_notes")
    if isinstance(notes, list):
        out["memory_notes"] = [n for n in notes if not (isinstance(n, dict) and n.get("id") in dead_i)]
    if hidden:
        out, _ = relabel(out, hidden)
    return out


async def _stale_sources(session: AsyncSession) -> tuple[set[int], set[int]]:
    """(messages, items) that quoting lines or notes name and that are gone, withheld, deleted or emptied, or whose
    quoting line the current detectors would change."""
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
        from .policy import withheld_source

        live = set((await session.scalars(select(MemoryItem.id).where(
            MemoryItem.id.in_(items), MemoryItem.deleted_at.is_(None),
            not_(MemoryItem.flags.has_key("source_withheld")), not_(withheld_source())))).all())
        stale_items |= items - live
    return stale_messages, stale_items
