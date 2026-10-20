"""Screening a research query before a task holds it (plan section 3.13).

A task's parameters are written when the task is created, before the gateway runs. So a direct run or a retry
(app/api/work.py) and a step the Supervisor planned (app/supervisor/agent.py) are checked here first, with the
gateway's own canonical form, screen and agent policy: research switched on, the edition, the agent's policy (Credit
Repair never), then the query. A refused query never reaches a task row, a plan, a note or a log line: the caller
gets the status and a reason code, and the attempt is recorded in research_queries with the code only (plus the
lead or ticket id that caused a block). The gateway checks everything again when the task runs.

A refusal says no more than it must: a name in the leads, the tickets or the Credit Repair index is refused with one
code (personal_record), at the same step and in the same words; the message never names the lead or ticket, and the
Research Center shows its id only to people with the sensitive-data permission. Whether a name is refused at all still
says that it is in this business's records, and every signed-in user can read the leads and tickets: a name refused as
personal_record that is in neither is in the Credit Repair records. No limit can hide that one answer, so it is kept
small and visible: after PERSONAL_RECORD_DAILY personal_record refusals in one day (UTC), nothing more of that
person's is screened that day (refused_budget, too_many_blocked), here and in the gateway, whatever their search cap,
and the first time each day an audit entry (research.personal_record_limit: the person and the count) shows it to the
admins. Apart from that, after as many refusals as their daily search cap, nothing more of theirs is screened that day.
One person's queries are counted and screened one at a time (lock_person), here and in the gateway: requests sent at the
same moment cannot all read a count below the limit. A request that cannot have the lock within LOCK_WAIT_SECONDS is
refused unscreened (refused_budget, busy), so a burst of one person's requests never holds the connection pool. A screen
refusal whose row could not be recorded answers screen_error, never its own code: it would not be counted.

Any error refuses (screen_error, policy_error): nothing that could not be checked is queued.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, licensing
from .. import tasks as task_service
from ..config import Settings
from ..credit_repair import blind_index
from ..db import session_scope
from ..models import AuditLog, ResearchQuery
from . import canon, policy, screen, store

if TYPE_CHECKING:
    from ..agents.research import SearchParams

log = logging.getLogger(__name__)

FEATURE = "web_research"
# A person's personal_record refusals per day (UTC) before nothing more of theirs is screened: each may say that a name
# is in the Credit Repair records (see above), so the number is fixed and not tied to the search caps.
PERSONAL_RECORD_DAILY = 5
LIMIT_ACTION = "research.personal_record_limit"
# pg_try_advisory_xact_lock(namespace, hashtext(person)): one person's screening at a time (budget.py uses 7316,
# cache.py 7315).
LOCK_NAMESPACE = 7_317
# How long a request waits for that lock. Each waiting request holds a pooled connection, and the holder needs one more
# to record its refusal: waiting longer than this would let a burst use up the pool (10 + 10 overflow) until its 30 s
# timeout, when the holder's refusal is answered without its row.
LOCK_WAIT_SECONDS = 3.0
LOCK_POLL_SECONDS = 0.05

# What each code means, for the person who typed the query (fixed texts: never the query itself).
EXPLANATIONS = {
    "research_off": "web research is switched off (an admin switches it on in the Research Center)",
    "settings_unreadable": "the research settings cannot be read (an admin saves them again)",
    "not_entitled": "this edition does not include web research",
    "credit_repair": "Credit Repair never searches the web",
    "no_policy": "this agent has no research policy",
    "search_disabled": "web search is switched off for this agent",
    "policy_unreadable": "this agent's research policy cannot be read",
    "policy_error": "the research policy could not be checked",
    "not_a_research_capability": "this capability does not search the web",
    "empty_query": "the query is empty",
    "too_long": "the query is longer than 128 characters (it is never cut)",
    "mixed_script": "a word mixes alphabets",
    "non_ascii_digits": "it contains digits that are not 0-9",
    "search_syntax": "a word starts with !, ?, : or < (search-engine commands)",
    "encoded_data": "it contains encoded data or a long random-looking token",
    "restricted_data": "it contains restricted data (an SSN, card or bank number, a date of birth)",
    "credit_topic": "credit topics are never searched",
    "secret": "it contains a password, key or token",
    "contact_detail": "it contains an e-mail address or a phone number",
    "personal_record": "it names a person or business from this business's own records",
    "too_many_blocked": "too many of your searches were refused today; try again tomorrow",
    "busy": "another search of yours is being checked; try again in a moment",
    "credit_repair_index_incomplete": "the Credit Repair check is not ready",
    "matches_private_text": "it repeats private text from the JARVIS archive or memory",
    "internal_host": "it names an internal address, host or server",
    "file_path": "it contains a file path",
    "client_name": "it names a client's business or website",
    "owner_name": "it names this business (not allowed for this agent)",
    "software_version": "it contains a version number or CVE id (not allowed for this agent)",
    "class_not_allowed": "this agent only searches advisories for a software name with its version",
    "blocked_domain": "it names a domain this agent's policy blocks",
    "screen_error": "the query could not be checked",
}


class PersonBusy(Exception):
    """The person's screening lock did not come free within LOCK_WAIT_SECONDS."""


@dataclass(frozen=True)
class Admission:
    """The outcome: params to store (the query in its canonical form), or a refusal's status, code and reference."""

    params: dict[str, Any] | None = None
    status: str = ""  # refused_policy or blocked
    code: str = ""
    ref: str = ""  # lead:<id> or ticket:<id> when one caused a block (recorded, never in the message)

    @property
    def refused(self) -> bool:
        return self.params is None

    def message(self) -> str:
        """The same words for a lead's, a ticket's and a Credit Repair client's name: never the record's id."""
        return f"The search was refused ({self.code}): {EXPLANATIONS.get(self.code, 'not allowed')}. Nothing was " \
               "searched or stored."


async def admit(session: AsyncSession, settings: Settings, *, agent: str, capability: str, params: SearchParams,
                trigger: str, person: str, request_id: int | None = None) -> Admission:
    """Checks one research query: params are the capability's parameters, already validated by its own model.
    A refusal is recorded (codes only) in its own transaction."""
    attempt = store.Attempt(agent=agent, capability=capability, request_id=request_id, requested_by=person,
                            trigger_source=trigger)
    mode = "off"
    try:
        if not task_service.uses_research(agent, capability):
            raise policy.PolicyRefused("not_a_research_capability")
        values = await policy.load_settings(session)
        mode = values.mode
        if mode == "off":
            raise policy.PolicyRefused("research_off")
        if not (await licensing.current_entitlements(session, settings)).has(FEATURE):
            raise policy.PolicyRefused("not_entitled")
        rules = await policy.search_policy(session, agent)
        await lock_person(session, person)  # held until the caller's transaction ends, after a refusal's own row
        blocked, named = await refused_today(session, person)
    except policy.PolicyRefused as exc:
        return await _refuse(attempt, "refused_policy", exc.code, mode)
    except policy.SettingsUnreadable:
        return await _refuse(attempt, "refused_policy", "settings_unreadable", "off")
    except PersonBusy:
        # Nothing was screened. Not recorded: the row would take a second pooled connection while this request holds
        # its own, which is the wait lock_person bounds.
        log.info("research refused_budget before queueing for %s: busy", attempt.agent)
        return Admission(status="refused_budget", code="busy")
    except Exception as exc:  # noqa: BLE001 - a policy that cannot be read refuses
        log.warning("research intake policy check failed (%s)", exc.__class__.__name__)
        return await _refuse(attempt, "refused_policy", "policy_error", mode)
    if named >= PERSONAL_RECORD_DAILY:
        await note_limit(person, named)
        return await _refuse(attempt, "refused_budget", "too_many_blocked", mode)
    if blocked and blocked >= values.person_daily_cap:
        return await _refuse(attempt, "refused_budget", "too_many_blocked", mode)
    try:
        c = canon.canonical(params.query_text())
        verdict = await screen.screen(session, settings, c, classes=rules.classes,
                                      blocked_domains=rules.blocked_domains, owner_terms=values.owner_terms)
    except canon.QueryRefused as exc:
        return await _refuse(attempt, "blocked", exc.code, mode)
    except blind_index.IndexIncomplete:
        return await _refuse(attempt, "blocked", "credit_repair_index_incomplete", mode)
    except Exception as exc:  # noqa: BLE001 - a screen that fails refuses
        log.warning("research intake screen failed (%s)", exc.__class__.__name__)
        return await _refuse(attempt, "blocked", "screen_error", mode)
    if verdict.blocked:
        return await _refuse(attempt, "blocked", verdict.code, mode, ref=verdict.ref)
    return Admission(params=params.screened(c.text))


def _today() -> datetime:
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


async def lock_person(session: AsyncSession, person: str) -> None:
    """Takes this person's screening lock and holds it until the session's transaction ends, waiting at most
    LOCK_WAIT_SECONDS (then PersonBusy). Taken before refused_today(): one person's queries are then counted and
    screened one after the other, and each count sees the refusals before it (a refusal's row is committed in its own
    transaction while the lock is still held). Tried again every LOCK_POLL_SECONDS rather than waited for with a
    lock_timeout: a lock timeout is an error that leaves the caller's transaction unusable, and the Supervisor's
    planning goes on in that transaction after a refused step."""
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while not await session.scalar(text("SELECT pg_try_advisory_xact_lock(:ns, hashtext(:person))"),
                                   {"ns": LOCK_NAMESPACE, "person": person}):
        if time.monotonic() >= deadline:
            raise PersonBusy
        await asyncio.sleep(LOCK_POLL_SECONDS)


async def refused_today(session: AsyncSession, person: str) -> tuple[int, int]:
    """How many of this person's searches the screen refused today (UTC), here or in the gateway: in all, and as
    personal_record."""
    found = (await session.execute(
        select(func.count(), func.count().filter(ResearchQuery.reason_code == screen.PERSONAL_RECORD))
        .select_from(ResearchQuery)
        .where(ResearchQuery.requested_by == person, ResearchQuery.status == "blocked",
               ResearchQuery.created_at >= _today()))).one()
    return found[0] or 0, found[1] or 0


async def note_limit(person: str, count: int) -> None:
    """The first time each day that a person reaches PERSONAL_RECORD_DAILY, one audit entry says so (the person and the
    count, never a query), so the admins see that someone tried names. In its own transaction; a failure is logged."""
    try:
        async with session_scope() as session:
            seen = await session.scalar(select(AuditLog.id).where(
                AuditLog.action == LIMIT_ACTION, AuditLog.target_id == person, AuditLog.at >= _today()).limit(1))
            if seen is None:
                await audit.record(session, "system:research", LIMIT_ACTION, "user", person,
                                   {"person": person, "count": count})
    except Exception as exc:  # noqa: BLE001 - the refusal stands without its entry
        log.warning("research refusal limit not audited (%s)", exc.__class__.__name__)


async def _refuse(attempt: store.Attempt, status: str, code: str, mode: str, *, ref: str = "") -> Admission:
    log.info("research %s before queueing for %s: %s", status, attempt.agent, code)
    # As the gateway records them: a blocked query under the provider it would have gone to.
    provider = {"mock": "mock", "live": "searxng"}.get(mode, "") if status == "blocked" else ""
    try:
        await store.log_attempt(attempt, status=status, reason=code, mode=mode, provider=provider, ref=ref)
    except Exception as exc:  # noqa: BLE001 - the refusal stands without its row
        log.warning("research attempt not recorded (%s)", exc.__class__.__name__)
        if status == "blocked":
            # Not counted by refused_today(), so it never answers with its code: a personal_record answer that was not
            # recorded would get past PERSONAL_RECORD_DAILY.
            return Admission(status=status, code="screen_error")
    return Admission(status=status, code=code, ref=ref)
