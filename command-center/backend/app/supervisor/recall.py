"""Answering questions about JARVIS's history from the imported archive and approved memory (supervisor.recall).

Local only: a database search through the Supervisor's memory policy, general data only. No AI model and no network
call is made, and the answer is never sent to one (summarize_request uses the template when a task's result is
`local_only`). Customer conversations and items, withheld (restricted) conversations and Credit Repair material are
never read here, so they are never quoted.

What it reads:
- archived JARVIS messages labelled accomplishment, problem or decision that match the question's topic words (any of
  them); when the topic words match nothing, the newest labelled messages, and the answer says so;
- approved memory items of the kinds decision, accomplishment, problem, project and business that match them.

The labels come from keyword rules at import (app/brain/text.py `labels_for`) and staff names from JARVIS's job ids:
the answer says both are inferred and gives each line's source, so a person can check it.

A period in the question ("last month", "in March 2026", "the past 30 days", "yesterday") limits the search to
that period, in the customer's time zone; the answer states the dates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field
from sqlalchemy import and_, func, select

from .. import customer_settings
from ..agents.base import AgentContext, AgentResult, Params
from ..brain import text as brain_text
from ..brain.policy import agent_archive, agent_items, agent_messages
from ..brain.store import AgentMemory, MessageHit, search_archive, search_terms, search_was_filtered
from ..models import ArchiveSession, MemoryItem

MAX_LINES = 15
EXCERPT = 240
LABELS: tuple[tuple[str, str], ...] = (("accomplishment", "Accomplishments"), ("problem", "Problems"),
                                        ("decision", "Decisions"))
# The approved item kinds listed under each section: project context (JARVIS's reports and briefs) is what was done;
# business memory (policies, prices, rules the owner set) is what was decided.
SECTION_KINDS: dict[str, tuple[str, ...]] = {"accomplishment": ("accomplishment", "project"), "problem": ("problem",),
                                             "decision": ("decision", "business")}
# Sections the question asks about come first.
FOCUS = {
    "accomplishment": re.compile(r"(?i)\b(accomplish\w*|achiev\w*|done|complet\w*|finish\w*|deliver\w*)\b"
                                r"|\bwhat\s+did\s+(?:\w+\s+){0,3}do\b"),
    "problem": re.compile(r"(?i)\b(problems?|issues?|errors?|fail\w*|wrong|broke\w*|bugs?|outages?|trouble)\b"),
    "decision": re.compile(r"(?i)\b(decisions?|decided?|decide|agreed|rules?|polic(?:y|ies)|chose|choices?)\b"),
}
# Words that say what kind of answer is wanted, on top of store.QUESTION_WORDS.
RECALL_WORDS = {
    "accomplishments", "achievements", "achieve", "achieved", "decide", "went", "wrong", "go", "get", "got", "doing",
    "hermes", "assistant", "staff", "happen", "month", "months", "week", "weeks", "year", "years", "day", "days",
    "today", "yesterday", "during", "period",
}

NOTHING_IMPORTED = ("Nothing has been imported from JARVIS yet: there are no archived conversations or approved "
                    "JARVIS records to answer from. Ask again once the JARVIS import has run.")
FALLBACK_NOTE = "Nothing matched the topic words; showing the newest."
FALLBACK_ITEMS_NOTE = "No archived conversation matched the topic words; showing the newest conversations."
NO_TOPIC_NOTE = "The question has no topic words; showing the newest."
LABEL_NOTE = ("Accomplishment, problem and decision labels are matched by keywords, and staff names are inferred from "
              "JARVIS's job ids: check the source before relying on a line.")


class RecallParams(Params):
    question: str = Field(min_length=1, max_length=500, description="The question about JARVIS's history")
    limit: int = Field(8, ge=1, le=30, description="How many matches to read per section")


# --- Periods ---------------------------------------------------------------------------------------------------

MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"), start=1)}
_MONTH_RE = "|".join(MONTHS)
_ROLLING = re.compile(r"(?i)\b(?:(?:in|during|over|for|from)\s+)?(?:the\s+)?(?:last|past|previous)\s+(\d{1,3})\s+"
                      r"(day|week|month|year)s?\b")
_UNIT = re.compile(r"(?i)\b(?:(?:in|during|over|for|from)\s+)?(?:the\s+)?(this|last|previous|past)\s+(week|month|year)\b")
_DAY = re.compile(r"(?i)\b(today|yesterday)\b")
_MONTH = re.compile(rf"(?i)\b(?:(?:in|during)\s+({_MONTH_RE})(?:,?\s+(20\d\d))?|({_MONTH_RE}),?\s+(20\d\d))\b")
_YEAR = re.compile(r"(?i)\b(?:in|during)\s+(20\d\d)\b")


@dataclass
class Period:
    start: date  # first day, inclusive
    end: date  # last day, inclusive
    words: str  # the words of the question that asked for it

    def bounds(self, tz: ZoneInfo) -> tuple[datetime, datetime]:
        """UTC [since, until) for the local days start..end."""
        since = datetime.combine(self.start, time(), tzinfo=tz).astimezone(UTC)
        until = datetime.combine(self.end + timedelta(days=1), time(), tzinfo=tz).astimezone(UTC)
        return since, until


def _month_start(d: date, back: int = 0) -> date:
    index = d.year * 12 + (d.month - 1) - back
    return date(index // 12, index % 12 + 1, 1)


def _months_back(d: date, n: int) -> date:
    first = _month_start(d, n)
    next_first = _month_start(first, -1)
    return first.replace(day=min(d.day, (next_first - timedelta(days=1)).day))


def find_period(question: str, today: date) -> Period | None:
    """The period a question asks about, or None. "last month" is the previous calendar month; "the past month"
    and "the last 30 days" count back from today."""
    m = _ROLLING.search(question)
    if m:
        n, unit = max(1, int(m.group(1))), m.group(2).lower()
        start = {"day": lambda: today - timedelta(days=n - 1), "week": lambda: today - timedelta(weeks=n),
                 "month": lambda: _months_back(today, n), "year": lambda: _months_back(today, 12 * n)}[unit]()
        return Period(start, today, m.group(0).strip())
    m = _UNIT.search(question)
    if m:
        which, unit = m.group(1).lower(), m.group(2).lower()
        if which == "past":  # rolling: the past week is the last 7 days
            start = {"week": today - timedelta(weeks=1), "month": _months_back(today, 1),
                     "year": _months_back(today, 12)}[unit]
            return Period(start, today, m.group(0).strip())
        if unit == "week":
            monday = today - timedelta(days=today.weekday())
            start = monday if which == "this" else monday - timedelta(weeks=1)
            end = today if which == "this" else monday - timedelta(days=1)
        elif unit == "month":
            start = _month_start(today, 0 if which == "this" else 1)
            end = today if which == "this" else _month_start(today) - timedelta(days=1)
        else:
            start = date(today.year - (0 if which == "this" else 1), 1, 1)
            end = today if which == "this" else date(today.year - 1, 12, 31)
        return Period(start, end, m.group(0).strip())
    m = _DAY.search(question)
    if m:
        day = today if m.group(1).lower() == "today" else today - timedelta(days=1)
        return Period(day, day, m.group(0))
    m = _MONTH.search(question)
    if m:
        month = MONTHS[(m.group(1) or m.group(3)).lower()]
        year_text = m.group(2) or m.group(4)
        # Without a year: the most recent such month that has begun.
        year = int(year_text) if year_text else (today.year if month <= today.month else today.year - 1)
        start = date(year, month, 1)
        return Period(start, _month_start(start, -1) - timedelta(days=1), m.group(0).strip())
    m = _YEAR.search(question)
    if m:
        year = int(m.group(1))
        return Period(date(year, 1, 1), date(year, 12, 31), m.group(0).strip())
    return None


def topic_terms(question: str, period: Period | None) -> list[str]:
    """The question's topic words: no question words, no period words."""
    text = question.replace(period.words, " ") if period else question
    return [w for w in search_terms(text) if w.lower() not in RECALL_WORDS]


# --- The search --------------------------------------------------------------------------------------------------


async def _timezone(ctx: AgentContext) -> ZoneInfo:
    name = (await customer_settings.load(ctx.session, ctx.settings)).get("timezone") or "UTC"
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _day(when: datetime | None, tz: ZoneInfo) -> str:
    return when.astimezone(tz).strftime("%Y-%m-%d") if when else "date unknown"


def _message_line(hit: MessageHit, terms: list[str], tz: ZoneInfo) -> str:
    m, s = hit.message, hit.session
    quote = brain_text.one_line(brain_text.excerpt(m.content, terms, EXCERPT - 4), EXCERPT)
    where = brain_text.one_line(s.title, 80) if s.title.strip() else f"session {s.source_session_id}"
    staff = s.staff or "staff unknown"
    return f'- {_day(m.at or s.started_at, tz)} · {staff} · "{quote}" (source: {where} · message {m.position})'


def _item_line(item: MemoryItem, terms: list[str], tz: ZoneInfo) -> str:
    body = brain_text.excerpt(item.body, terms, EXCERPT - 4) if item.body.strip() else ""
    quote = brain_text.one_line(f"{item.title}: {body}" if body else item.title, EXCERPT)
    source = brain_text.one_line(item.source_ref, 120) if item.source_ref.strip() else "Command Center"
    return (f'- {_day(item.source_at or item.created_at, tz)} · approved memory ({item.kind}) · "{quote}" '
            f"(source: {source}, item #{item.id})")


async def _items(memory: AgentMemory, kinds: tuple[str, ...], terms: list[str], filtered: bool,
                 since: datetime | None, until: datetime | None, limit: int) -> tuple[list[MemoryItem], int]:
    """Approved, general items of these kinds in the Supervisor's scope that match any topic word (the newest when
    the question has none), and how many match."""
    session = memory.session
    clauses = [agent_items(memory.policy, general_only=True), MemoryItem.kind.in_(kinds)]
    when = func.coalesce(MemoryItem.source_at, MemoryItem.created_at)
    if since is not None and until is not None:
        clauses += [when >= since, when < until]
    order: tuple[Any, ...] = (when.desc(), MemoryItem.id.desc())
    if filtered:
        query = func.websearch_to_tsquery("english", " OR ".join(terms))
        clauses.append(MemoryItem.search.op("@@")(query))
        order = (func.ts_rank_cd(MemoryItem.search, query).desc(), *order)
    cond = and_(*clauses)
    total = await session.scalar(select(func.count()).select_from(MemoryItem).where(cond)) or 0
    rows = (await session.scalars(select(MemoryItem).where(cond).order_by(*order).limit(limit))).all()
    return list(rows), total


async def _archive(memory: AgentMemory, terms: list[str], since: datetime | None, until: datetime | None,
                   limit: int) -> dict[str, tuple[list[MessageHit], int]]:
    found: dict[str, tuple[list[MessageHit], int]] = {}
    for label, _ in LABELS:
        found[label] = await search_archive(
            memory.session, session_where=agent_archive(memory.policy, general_only=True),
            message_where=agent_messages(memory.policy, general_only=True), q=" ".join(terms), any_terms=True,
            labels=[label], since=since, until=until, limit=limit)
    return found


def _allocate(candidates: dict[str, list[str]], order: list[str], budget: int) -> dict[str, int]:
    """How many lines each section shows: one "nothing found" line for an empty section, the rest round-robin."""
    shown = {label: 0 for label in order}
    budget -= sum(1 for label in order if not candidates[label])
    while budget > 0:
        progressed = False
        for label in order:
            if budget > 0 and shown[label] < len(candidates[label]):
                shown[label] += 1
                budget -= 1
                progressed = True
        if not progressed:
            break
    return shown


def _interleave(items: list[str], messages: list[str]) -> list[str]:
    out: list[str] = []
    for i in range(max(len(items), len(messages))):
        out += items[i:i + 1] + messages[i:i + 1]
    return out


async def recall_history(ctx: AgentContext, params: RecallParams) -> AgentResult:
    session = ctx.session
    memory = ctx.memory if ctx.memory is not None and ctx.memory.agent == "supervisor" else AgentMemory(session, "supervisor")
    tz = await _timezone(ctx)
    today = datetime.now(UTC).astimezone(tz).date()
    period = find_period(params.question, today)
    since, until = period.bounds(tz) if period else (None, None)
    terms = topic_terms(params.question, period)
    filtered = bool(terms) and await search_was_filtered(session, " OR ".join(terms))

    scope = agent_archive(memory.policy, general_only=True)
    searched, first, last = (await session.execute(
        select(func.count(), func.min(ArchiveSession.started_at), func.max(ArchiveSession.started_at)).where(scope))).one()
    imported_sessions = await session.scalar(select(func.count()).select_from(ArchiveSession)) or 0
    imported_items = await session.scalar(
        select(func.count()).select_from(MemoryItem).where(MemoryItem.origin == "import")) or 0

    archive = await _archive(memory, terms if filtered else [], since, until, params.limit)
    fallback = filtered and not any(total for _, total in archive.values())
    if fallback:
        archive = await _archive(memory, [], since, until, params.limit)
    items = {label: await _items(memory, SECTION_KINDS[label], terms, filtered, since, until, params.limit)
             for label, _ in LABELS}
    items_total = sum(total for _, total in items.values())

    totals = {label: archive[label][1] for label, _ in LABELS}
    coverage: dict[str, Any] = {
        "sessions": searched, "first": _day(first, tz) if first else None, "last": _day(last, tz) if last else None,
        "imported_sessions": imported_sessions,
        "general_only": True, "topic_words": len(terms), "matched_topic_words": filtered and not fallback,
        "newest_shown": fallback or not filtered,
        "period": {"from": period.start.isoformat(), "to": period.end.isoformat(), "timezone": str(tz)} if period else None,
    }

    if imported_sessions == 0 and imported_items == 0 and items_total == 0:
        counts = {**totals, "items": 0, "shown": 0, "sessions_searched": 0}
        return AgentResult(summary="JARVIS history: nothing imported yet (0 sessions, 0 approved items).",
                           data={"local_only": True, "answer": [NOTHING_IMPORTED], "counts": counts, "coverage": coverage})

    # Sections the question asks about come first. Each lists approved items and archived messages alternately; a
    # message with several labels is shown once, in the first section that lists it.
    focused = [label for label, _ in LABELS if FOCUS[label].search(params.question)]
    order = focused + [label for label, _ in LABELS if label not in focused]
    by_section: dict[str, list[str]] = {}
    listed_above: dict[str, int] = {}
    shown_messages: set[int] = set()
    for label in order:
        messages = [h for h in archive[label][0] if h.message.id not in shown_messages]
        listed_above[label] = len(archive[label][0]) - len(messages)
        shown_messages.update(h.message.id for h in messages)
        by_section[label] = _interleave([_item_line(i, terms, tz) for i in items[label][0]],
                                        [_message_line(h, terms, tz) for h in messages])
    if fallback:
        notes = [FALLBACK_ITEMS_NOTE if items_total else FALLBACK_NOTE]
    else:
        notes = [] if filtered else [NO_TOPIC_NOTE]
    if notes and period:
        notes = [notes[0].removesuffix(".") + " from that period."]
    if searched:
        cover = (f"Searched {searched} imported JARVIS session(s) from {coverage['first']} to {coverage['last']} "
                 "(general conversations only; customer and withheld conversations are never quoted).")
    elif imported_sessions:
        cover = ("Searched 0 imported JARVIS sessions: none of them is a general conversation (customer and withheld "
                 "conversations are never quoted).")
    else:
        cover = "No JARVIS conversations have been imported yet; only approved memory was searched."
    if period:
        cover += f" Period asked: {period.start.isoformat()} to {period.end.isoformat()} ({tz})."
    fixed = len(notes) + len(LABELS) + 2  # notes, section headings, coverage and the label note
    shown = _allocate(by_section, order, MAX_LINES - fixed)

    titles = dict(LABELS)
    lines = list(notes)
    for label in order:
        found = totals[label] + items[label][1]
        listed = min(shown[label], len(by_section[label]))
        lines.append(f"### {titles[label]} ({found} found" + (f", {listed} shown)" if 0 < listed < found else ")"))
        if by_section[label]:
            lines += by_section[label][:shown[label]]
        elif listed_above[label]:
            lines.append("- The matching messages are listed above (they carry more than one label).")
        else:
            lines.append("- Nothing found" + (" in that period." if period else "."))
    lines += [cover, LABEL_NOTE]
    lines = lines[:MAX_LINES]

    count_shown = sum(shown.values())
    counts = {**totals, "items": items_total, "shown": count_shown, "sessions_searched": searched}
    summary = (f"JARVIS history: {totals['accomplishment']} accomplishment, {totals['problem']} problem and "
               f"{totals['decision']} decision message(s) in {searched} searched session(s); {items_total} approved "
               f"memory item(s); {count_shown} shown.")
    return AgentResult(summary=summary,
                       data={"local_only": True, "answer": lines, "counts": counts, "coverage": coverage})
