"""The Research Gateway: the only code that searches the web for the agents (plan section 1.2, steps 1-7).

Every step can refuse, and every error refuses: nothing reaches a provider unless every check before it passed, and
no call is retried beyond plan section 3.8. A refusal is an answer with a status and a fixed reason code, never an
exception, so a capability can report it and the task's log stays intact.

 1. Research is on (mode mock or live) and the settings can be read; the edition has web_research; the agent may
    search (Credit Repair never, whatever its policy row says) and is switched on; the task is this agent's, running,
    read-only and carries no approval; its trigger, read from the database and never from parameters, is a person's
    run on the dashboard, a Supervisor request a person typed on the dashboard, a person's retry, or an admin's
    connectivity test. Schedules, n8n and the service token are refused, and so is anyone but an admin or operator.
 2. Not for a person who reached today's limit of personal_record refusals (app/research/intake.py); then the
    canonical form and the screen (app/research/screen.py), under the person's screening lock (intake.lock_person)
    until a refusal is recorded (a lock that is not free within intake.LOCK_WAIT_SECONDS refuses: busy). A blocked
    query keeps only its reason code, and its text is removed from the task's parameters and from the stored plan of
    the Supervisor request behind it (a lead, a client or archive text added after the task was queued). A blocked
    query whose row could not be recorded answers screen_error, never its own code.
 3. The cache: a hit is answered from it (not for the admin's connectivity test, which always asks the provider);
    an identical search in flight is waited for.
 4. Engines that are cooling down are left out (all of them: provider_down, nothing sent); then a budget and
    spacing slot is taken in its own transaction.
 5. The provider: the mock, or SearXNG.
 6. The answer is cut to its caps, normalized, cleaned and flagged (app/research/normalize.py); results from domains
    the agent's policy blocks are dropped.
 7. The query, its results and sources, usage, SearXNG's health and the cache are stored, each in its own short
    transaction.

Nothing here logs a query or any web text: log lines carry reason codes only.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import licensing
from .. import tasks as task_service
from ..config import Settings
from ..credit_repair import blind_index
from ..db import session_scope
from ..models import Approval, SupervisorRequest, Task, User
from . import budget, cache, canon, health, intake, normalize, policy, screen, store
from .normalize import ResearchResponse
from .providers import ResearchProvider, SearchRequest, for_mode
from .providers.searxng import BASE_URL

log = logging.getLogger(__name__)

FEATURE = "web_research"
TEST_SOURCE = task_service.RESEARCH_TEST_SOURCE  # tasks queued by the admin's connectivity test (POST /api/research/test)
PERSON_ROLES = ("admin", "operator")
LANGUAGE = re.compile(r"all|auto|[a-z]{2,3}(?:-[A-Za-z]{2})?")
TIME_RANGES = (None, "day", "week", "month", "year")
MAX_PAGE = 5


@dataclass(frozen=True)
class Origin:
    """Where a task came from, read from the database; refusal is "" when it may search."""

    trigger: str
    person: str = ""
    capability: str = ""
    request_id: int | None = None
    refusal: str = ""


async def resolve_origin(session: AsyncSession, task: Task) -> Origin:
    """The task's trigger and the person behind it (plan section 5, T2)."""
    base = {"capability": task.capability, "request_id": task.request_id}
    created_by = task.created_by or ""
    if task.schedule_id is not None or task.source == "schedule" or created_by.startswith("schedule:"):
        return Origin("schedule", refusal="trigger_not_allowed", **base)
    if created_by.startswith("service:"):
        return Origin("service", refusal="trigger_not_allowed", **base)
    if task.source == "supervisor":
        request = await session.get(SupervisorRequest, task.request_id) if task.request_id is not None else None
        if request is None:
            return Origin("unknown", refusal="trigger_not_allowed", **base)
        if request.source != "dashboard" or request.requested_by.startswith("service:"):
            trigger = "n8n" if request.source == "n8n" else "service" if request.requested_by.startswith("service:") \
                else "unknown"
            return Origin(trigger, refusal="trigger_not_allowed", **base)
        trigger, person = "supervisor", request.requested_by
    elif task.source in ("dashboard", "retry"):
        trigger, person = task.source, created_by
    elif task.source == TEST_SOURCE:
        trigger, person = "test", created_by
    else:
        return Origin("unknown", refusal="trigger_not_allowed", **base)
    user = await session.scalar(select(User).where(User.email == person.lower()))
    roles = ("admin",) if trigger == "test" else PERSON_ROLES
    if user is None or not user.is_active or user.role not in roles:
        return Origin(trigger, person, refusal="person_not_allowed", **base)
    return Origin(trigger, person, **base)


def check_options(count: object, language: object, time_range: object, page: object) -> dict[str, Any]:
    def whole(v: object, low: int, high: int) -> bool:
        return isinstance(v, int) and not isinstance(v, bool) and low <= v <= high

    if not (whole(count, 1, normalize.MAX_RESULTS) and whole(page, 1, MAX_PAGE) and time_range in TIME_RANGES
            and isinstance(language, str) and LANGUAGE.fullmatch(language)):
        raise ValueError("invalid_options")
    return {"count": count, "language": language, "time_range": time_range, "page": page}


async def _abandon(session: AsyncSession) -> None:
    """Ends a transaction that a failed statement may have left unusable (its locks go with it)."""
    try:
        await session.rollback()
    except Exception as exc:  # noqa: BLE001 - the refusal stands
        log.warning("research screening transaction not rolled back (%s)", exc.__class__.__name__)


class _Stop(Exception):
    """Ends the pipeline with this answer (already recorded)."""

    def __init__(self, response: ResearchResponse) -> None:
        super().__init__(response.status)
        self.response = response


class Gateway:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None,
                 providers: dict[str, ResearchProvider] | None = None, spacing: budget.Spacing = budget.SEARXNG_SPACING,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.settings = settings
        self.transport = transport  # tests: an httpx.MockTransport standing in for SearXNG
        self.providers = providers or {}
        self.spacing = spacing
        self.clock = clock or (lambda: datetime.now(UTC))

    def client(self, *, agent: str, task_id: int | None):
        from . import ResearchClient

        return ResearchClient(self, agent=agent, task_id=task_id)

    def _provider(self, mode: str) -> ResearchProvider:
        if mode not in self.providers:
            self.providers[mode] = for_mode(mode, transport=self.transport)
        return self.providers[mode]

    async def aclose(self) -> None:
        for provider in self.providers.values():
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()
        self.providers.clear()

    async def _refuse(self, attempt: store.Attempt, status: str, reason: str, *, mode: str, provider: str = "",
                      ref: str = "", query_text: str | None = None, options: dict[str, Any] | None = None,
                      retry_after: int | None = None) -> ResearchResponse:
        """Records a refused or failed attempt (codes only) and answers with it. Recording failures are logged; a
        blocked attempt that could not be recorded answers screen_error."""
        log.info("research %s for %s: %s", status, attempt.agent, reason)
        query_id = None
        try:
            query_id = await store.log_attempt(attempt, status=status, reason=reason, mode=mode, provider=provider,
                                               ref=ref, query_text=query_text, options=options, at=self.clock())
        except Exception as exc:  # noqa: BLE001 - the refusal stands without its row
            log.warning("research attempt not recorded (%s)", exc.__class__.__name__)
            if status == "blocked":
                # Not counted by intake.refused_today(), so it never answers with its code (see intake._refuse).
                reason = "screen_error"
        return ResearchResponse(status=status, reason=reason, query_id=query_id, provider=provider,
                                query=query_text or "", options=(options or {}) if query_text else {},
                                retry_after=retry_after)

    async def _blocked(self, attempt: store.Attempt, reason: str, *, mode: str, provider: str,
                       ref: str = "") -> ResearchResponse:
        """A query the screen refused when its task ran: recorded with its code, and its text removed from the task's
        parameters and from the stored plan of the Supervisor request that planned it (the plan's web-search steps
        lose their parameters and rationale, as a purge leaves them), each in its own transaction. Failures are
        logged by class; the refusal stands."""
        response = await self._refuse(attempt, "blocked", reason, mode=mode, provider=provider, ref=ref)
        if attempt.task_id is not None:
            try:
                async with session_scope() as session:
                    await session.execute(update(Task).where(Task.id == attempt.task_id)
                                          .values(params={}, updated_at=func.now()))
                if attempt.request_id is not None:
                    await store.hide_plan_queries(attempt.request_id, task_service.research_capabilities())
            except Exception as exc:  # noqa: BLE001
                log.warning("refused research query not removed from its task (%s)", exc.__class__.__name__)
        return response

    # --- Step 1 ----------------------------------------------------------------------------------------------------

    async def _allowed(self, agent: str, task_id: int | None) -> tuple[store.Attempt, policy.ResearchSettings,
                                                                       policy.AgentPolicy]:
        attempt = store.Attempt(agent=agent, task_id=task_id)
        mode = "off"
        try:
            if agent == policy.SENSITIVE_AGENT:
                raise policy.PolicyRefused("credit_repair")
            async with session_scope() as session:
                task = await session.get(Task, task_id) if task_id is not None else None
                origin = await resolve_origin(session, task) if task is not None else Origin("unknown")
                attempt = store.Attempt(agent=agent, capability=origin.capability, task_id=task_id,
                                        request_id=origin.request_id, requested_by=origin.person,
                                        trigger_source=origin.trigger)
                values = await policy.load_settings(session)
                mode = values.mode
                if mode == "off":
                    raise policy.PolicyRefused("research_off")
                if not (await licensing.current_entitlements(session, self.settings)).has(FEATURE):
                    raise policy.PolicyRefused("not_entitled")
                rules = await policy.search_policy(session, agent)
                if await task_service.agent_unavailable(session, self.settings, agent):
                    raise policy.PolicyRefused("agent_unavailable")
                if task is None:
                    raise policy.PolicyRefused("no_task")
                if task.agent != agent or task.status != "running":
                    raise policy.PolicyRefused("task_mismatch")
                if task.risk != "read" or await session.scalar(select(Approval.id).where(Approval.task_id == task.id)):
                    raise policy.PolicyRefused("task_has_approval")
                if origin.refusal:
                    raise policy.PolicyRefused(origin.refusal)
            return attempt, values, rules
        except policy.PolicyRefused as exc:
            refused = await self._refuse(attempt, "refused_policy", exc.code, mode=mode)
        except policy.SettingsUnreadable:
            refused = await self._refuse(attempt, "refused_policy", "settings_unreadable", mode="off")
        except Exception as exc:  # noqa: BLE001 - any error refuses
            log.warning("research policy check failed (%s)", exc.__class__.__name__)
            refused = await self._refuse(attempt, "refused_policy", "policy_error", mode=mode)
        raise _Stop(refused)

    # --- The pipeline ----------------------------------------------------------------------------------------------

    async def search(self, *, agent: str, task_id: int | None, query: object, count: int = 10,
                     language: str = "en-US", time_range: str | None = None, page: int = 1) -> ResearchResponse:
        try:
            attempt, values, rules = await self._allowed(agent, task_id)
            mode = values.mode
            provider = "mock" if mode == "mock" else "searxng"
            try:
                options = check_options(count, language, time_range, page)
            except ValueError:
                return await self._refuse(attempt, "bad_request", "invalid_options", mode=mode, provider=provider)

            # 2. The person's personal_record refusals today, the canonical form and the screen, under the person's
            # screening lock: a refusal is recorded (in its own transaction) before the lock is released.
            screened = None
            try:
                async with session_scope() as session:
                    screened = await self._screen(session, attempt, query, values, rules, mode=mode,
                                                  provider=provider)
            except Exception as exc:  # noqa: BLE001 - a screening transaction that fails refuses
                log.warning("research screening failed (%s)", exc.__class__.__name__)
                if not isinstance(screened, ResearchResponse):
                    return await self._refuse(attempt, "refused_policy", "policy_error", mode=mode)
            if isinstance(screened, ResearchResponse):
                return screened
            c, verdict = screened

            # 3. The cache, with identical searches merged. Mock answers are kept apart from real ones.
            key = cache.cache_key("search" if mode == "live" else "search:mock", c.text, language=language,
                                  time_range=time_range, page=page, count=count)
            try:
                async with cache.merged(key):
                    hit = await cache.lookup(key, self.clock()) if attempt.trigger_source != "test" else None
                    if hit is not None:
                        return await self._from_cache(attempt, c, verdict, hit, key, options, mode, rules)
                    return await self._send(attempt, c, verdict, key, options, mode, provider, values, rules)
            except _Stop:
                raise
            except Exception as exc:  # noqa: BLE001 - the cache (or its lock) failing refuses
                log.warning("research cache failed (%s)", exc.__class__.__name__)
                return await self._refuse(attempt, "failed", "cache_error", mode=mode, provider=provider,
                                          query_text=c.text, options=options)
        except _Stop as stop:
            return stop.response

    async def _screen(self, session: AsyncSession, attempt: store.Attempt, query: object,
                      values: policy.ResearchSettings, rules: policy.AgentPolicy, *, mode: str,
                      provider: str) -> ResearchResponse | tuple[canon.Canonical, screen.Verdict]:
        """Step 2 in the caller's transaction, which holds the person's screening lock until it ends: the canonical
        query and the screen's verdict, or the refusal (already recorded)."""
        if attempt.requested_by:
            try:
                await intake.lock_person(session, attempt.requested_by)
                _, named = await intake.refused_today(session, attempt.requested_by)
            except intake.PersonBusy:
                # Nothing screened; the connection goes back to the pool before the refusal's own row is written.
                await _abandon(session)
                return await self._refuse(attempt, "refused_budget", "busy", mode=mode)
            except Exception as exc:  # noqa: BLE001 - a count that cannot be read refuses
                log.warning("research refusal count failed (%s)", exc.__class__.__name__)
                await _abandon(session)
                return await self._refuse(attempt, "refused_policy", "policy_error", mode=mode)
            if named >= intake.PERSONAL_RECORD_DAILY:
                await intake.note_limit(attempt.requested_by, named)
                return await self._refuse(attempt, "refused_budget", "too_many_blocked", mode=mode)
        try:
            c = canon.canonical(query)
            verdict = await screen.screen(session, self.settings, c, classes=rules.classes,
                                          blocked_domains=rules.blocked_domains, owner_terms=values.owner_terms)
        except canon.QueryRefused as exc:
            return await self._blocked(attempt, exc.code, mode=mode, provider=provider)
        except blind_index.IndexIncomplete:
            return await self._blocked(attempt, "credit_repair_index_incomplete", mode=mode, provider=provider)
        except Exception as exc:  # noqa: BLE001 - a screen that fails refuses
            log.warning("research screen failed (%s)", exc.__class__.__name__)
            await _abandon(session)
            return await self._blocked(attempt, "screen_error", mode=mode, provider=provider)
        if verdict.blocked:
            return await self._blocked(attempt, verdict.code, mode=mode, provider=provider, ref=verdict.ref)
        return c, verdict

    async def _from_cache(self, attempt: store.Attempt, c: canon.Canonical, verdict: screen.Verdict,
                          hit: dict[str, Any], key: str, options: dict[str, Any], mode: str,
                          rules: policy.AgentPolicy) -> ResearchResponse:
        at = self.clock()
        provider = str(hit.get("provider", ""))
        results = normalize.drop_blocked(list(hit.get("results") or []), rules.blocked_domains)
        retrieved = str(hit.get("retrieved_at") or at.isoformat())
        numbered = normalize.numbered(results, provider=provider, retrieved_at=retrieved, count=options["count"])
        status = str(hit.get("status", "ok")) if numbered else "empty"
        unresponsive = list(hit.get("unresponsive") or [])
        try:
            query_id = await store.open_query(attempt, query_text=c.text, cache_key=key, options=options,
                                              provider=provider, mode=mode, engines=list(hit.get("engines") or []),
                                              flags=verdict.flags, at=at)
            numbered = await store.close_query(query_id, status=status, results=numbered, unresponsive=unresponsive,
                                               provider_version=str(hit.get("provider_version", "")), cached=True,
                                               latency_ms=0, at=at)
        except Exception as exc:  # noqa: BLE001 - an answer that cannot be recorded is not given
            log.warning("research cache hit not recorded (%s)", exc.__class__.__name__)
            raise _Stop(await self._refuse(attempt, "failed", "store_error", mode=mode, provider=provider,
                                           query_text=c.text, options=options)) from None
        return ResearchResponse(status=status, query_id=query_id, provider=provider,
                                provider_version=str(hit.get("provider_version", "")), cached=True, query=c.text,
                                options=options, retrieved_at=retrieved, unresponsive=unresponsive, results=numbered,
                                flags=verdict.flags)

    async def _send(self, attempt: store.Attempt, c: canon.Canonical, verdict: screen.Verdict, key: str,
                    options: dict[str, Any], mode: str, provider_name: str, values: policy.ResearchSettings,
                    rules: policy.AgentPolicy) -> ResearchResponse:
        refuse_args = {"mode": mode, "provider": provider_name, "query_text": c.text, "options": options}

        # 4. Engines that are not cooling down, then a budget and spacing slot.
        if mode == "live" and self.settings.searxng_url.rstrip("/") != BASE_URL:
            return await self._refuse(attempt, "provider_misconfigured", "searxng_url_not_fixed", **refuse_args)
        try:
            if mode == "live":
                async with session_scope() as session:
                    detail = await health.load(session)
                at = self.clock()
                engines = health.available_engines(detail, at)
                if not engines:
                    until = min(health.cooling(detail, at).values())
                    return await self._refuse(attempt, "provider_down", "all_engines_cooling",
                                              retry_after=max(int((until - at).total_seconds()), 1), **refuse_args)
            else:
                engines = list(health.ENGINES)
            caps = budget.Caps(values.global_daily_cap, rules.daily_cap, values.person_daily_cap)
            slot = await budget.reserve(provider=provider_name, agent=attempt.agent, person=attempt.requested_by,
                                        caps=caps, spacing=self.spacing, at=self.clock())
        except budget.BudgetRefused as exc:
            return await self._refuse(attempt, exc.status, exc.code, retry_after=exc.retry_after, **refuse_args)
        except Exception as exc:  # noqa: BLE001 - a budget that cannot be checked refuses
            log.warning("research budget failed (%s)", exc.__class__.__name__)
            return await self._refuse(attempt, "refused_budget", "budget_error", **refuse_args)

        try:
            query_id = await store.open_query(attempt, query_text=c.text, cache_key=key, options=options,
                                              provider=provider_name, mode=mode, engines=engines, flags=verdict.flags,
                                              at=self.clock())
        except Exception as exc:  # noqa: BLE001 - nothing is sent that is not recorded
            log.warning("research query not recorded (%s)", exc.__class__.__name__)
            await self._release(slot, sent=False)
            return await self._refuse(attempt, "failed", "store_error", **refuse_args)

        # 5. The provider.
        request = SearchRequest(query=c.text, engines=tuple(engines), language=options["language"],
                                time_range=options["time_range"], page=options["page"])
        try:
            answer = await self._provider(mode).search(request)
        except Exception as exc:  # noqa: BLE001 - counted as sent: it may have gone out
            log.warning("research provider failed (%s)", exc.__class__.__name__)
            await self._release(slot, sent=True, query_id=query_id)
            return await self._finish(query_id, attempt, status="failed", reason="provider_error", **refuse_args)
        await self._release(slot, sent=answer.sent, query_id=query_id)

        # 6. Cut, normalize, clean, flag.
        at = self.clock()
        retrieved = at.isoformat()
        status, reason, results = answer.status, answer.reason, []
        if status in normalize.ANSWERED:
            try:
                found, _ = normalize.searxng_results(answer.data)
                found = normalize.drop_blocked(found, rules.blocked_domains)
                results = normalize.numbered(found, provider=provider_name, retrieved_at=retrieved,
                                             count=options["count"])
            except Exception as exc:  # noqa: BLE001 - an answer that cannot be cleaned is not used
                log.warning("research answer not usable (%s)", exc.__class__.__name__)
                status, reason, results = "failed", "normalize_error", []
            if status in ("ok", "partial") and not results:
                status, reason = "empty", reason if status == "partial" else ""

        # 7. Store, then health and cache (those two may fail without losing the answer).
        try:
            results = await store.close_query(query_id, status=status, reason=reason, http_status=answer.http_status,
                                              results=results, unresponsive=answer.unresponsive,
                                              provider_version=answer.version, latency_ms=answer.latency_ms,
                                              sources_days=values.sources_days, at=at)
        except Exception as exc:  # noqa: BLE001 - results that cannot be recorded are not handed out
            log.warning("research results not recorded (%s)", exc.__class__.__name__)
            return ResearchResponse(status="failed", reason="store_error", query_id=query_id, provider=provider_name,
                                    query=c.text, options=options)
        if mode == "live":
            try:
                if answer.status in normalize.ANSWERED or answer.reason == "all_engines_failing":
                    await health.record_search(asked=engines, unresponsive=answer.unresponsive,
                                               answered=answer.status in normalize.ANSWERED, now=at)
                else:
                    await health.record_failure(answer.reason, now=at)
            except Exception as exc:  # noqa: BLE001 - health is advisory
                log.warning("research health not recorded (%s)", exc.__class__.__name__)
        ttl = cache.lifetime(status, options["time_range"])
        if ttl is not None:
            try:
                await cache.put(key, kind="search", provider=provider_name, ttl=ttl, at=at, response={
                    "status": status, "provider": provider_name, "provider_version": answer.version,
                    "retrieved_at": retrieved, "engines": engines, "unresponsive": answer.unresponsive,
                    "results": [_cacheable(r) for r in results]})
            except Exception as exc:  # noqa: BLE001 - the next identical search is simply sent again
                log.warning("research answer not cached (%s)", exc.__class__.__name__)
        return ResearchResponse(status=status, reason=reason, query_id=query_id, provider=provider_name,
                                provider_version=answer.version, query=c.text, options=options,
                                retrieved_at=retrieved, unresponsive=answer.unresponsive, results=results,
                                flags=verdict.flags)

    async def _release(self, slot: int, *, sent: bool, query_id: int | None = None) -> None:
        try:
            await budget.finish(slot, sent=sent, query_id=query_id, at=self.clock())
        except Exception as exc:  # noqa: BLE001 - an unfinished slot stops counting after ten minutes
            log.warning("research budget slot not finished (%s)", exc.__class__.__name__)

    async def _finish(self, query_id: int, attempt: store.Attempt, *, status: str, reason: str, mode: str,
                      provider: str, query_text: str, options: dict[str, Any]) -> ResearchResponse:
        try:
            await store.close_query(query_id, status=status, reason=reason, at=self.clock())
        except Exception as exc:  # noqa: BLE001
            log.warning("research query not closed (%s)", exc.__class__.__name__)
        return ResearchResponse(status=status, reason=reason, query_id=query_id, provider=provider, query=query_text,
                                options=options)

    async def probe(self, *, with_config: bool = False) -> bool:
        """The worker's health probe (plan section 1.4): SearXNG's /healthz, and /config when asked, recorded in the
        research_searxng row. Only while research is live: False, and nothing is sent, in any other mode."""
        try:
            async with session_scope() as session:
                mode = (await policy.load_settings(session)).mode
        except policy.SettingsUnreadable:
            return False
        if mode != "live" or self.settings.searxng_url.rstrip("/") != BASE_URL:
            return False
        found = await self._provider("live").health(with_config=with_config)
        await health.record_probe(healthz_ok=found.ok, config=found.config, error=found.reason, now=self.clock())
        return True

    async def fetch(self, *, agent: str, task_id: int | None, url: object) -> ResearchResponse:
        """Page fetching is not part of this release: always refused, nothing is sent, the URL is not kept."""
        attempt = store.Attempt(agent=agent, task_id=task_id, kind="fetch")
        try:
            async with session_scope() as session:
                task = await session.get(Task, task_id) if task_id is not None else None
                if task is not None:
                    origin = await resolve_origin(session, task)
                    attempt = store.Attempt(agent=agent, capability=origin.capability, task_id=task_id,
                                            request_id=origin.request_id, requested_by=origin.person,
                                            trigger_source=origin.trigger, kind="fetch")
        except Exception as exc:  # noqa: BLE001 - recorded without its origin
            log.warning("research fetch origin not resolved (%s)", exc.__class__.__name__)
        return await self._refuse(attempt, "refused_policy", "not_supported", mode="off")


def _cacheable(result: dict[str, Any]) -> dict[str, Any]:
    """A numbered result as the cache keeps it: its content, without this query's numbering."""
    return {"url": result["url"], "domain": result["domain"], "title": result["title"], "snippet": result["snippet"],
            "type": result["type"], "engines": result["source"]["engines"], "published_at": result["published_at"],
            "provider_score": result["provider_score"], "flags": result["flags"]}
