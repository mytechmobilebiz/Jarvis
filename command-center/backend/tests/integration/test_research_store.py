"""The research tables on PostgreSQL: budgets and spacing, the cache, the query log, retention, the migration."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app import db
from app.db import session_scope
from app.models import IntegrationStatus, ResearchQuery, ResearchReservation, ResearchUsageDaily
from app.research import budget, cache, policy, store
from app.research.budget import BudgetRefused, Caps, Spacing

pytestmark = pytest.mark.usefixtures("clean_db")

NOW = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
LOOSE = Spacing(interval=0.001, burst=1000)


async def reserve(at=NOW, *, agent="seo_content", person="admin@test.local", caps=Caps(50, 20, 30), spacing=LOOSE):
    return await budget.reserve(provider="searxng", agent=agent, person=person, caps=caps, spacing=spacing, at=at)


async def test_parallel_reservations_never_exceed_the_cap():
    results = await asyncio.gather(*(reserve(caps=Caps(5, 20, 30)) for _ in range(20)), return_exceptions=True)
    taken = [r for r in results if isinstance(r, int)]
    refused = [r for r in results if isinstance(r, BudgetRefused)]
    assert len(taken) == 5 and len(refused) == 15 and {r.code for r in refused} == {"global_cap"}
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(ResearchReservation)) == 5
        usage = await s.scalar(select(ResearchUsageDaily.reserved))
    assert usage == 5


@pytest.mark.parametrize("caps, code", [(Caps(50, 2, 30), "agent_cap"), (Caps(50, 20, 2), "person_cap")])
async def test_agent_and_person_caps(caps, code):
    await reserve(caps=caps)
    await reserve(caps=caps)
    with pytest.raises(BudgetRefused) as exc:
        await reserve(caps=caps)
    assert (exc.value.status, exc.value.code) == ("refused_budget", code)
    # Another agent and another person still have their own allowance.
    await reserve(caps=caps, agent="supervisor", person="op@test.local")


@pytest.mark.parametrize("caps", [Caps(0, 20, 30), Caps(50, 0, 30), Caps(50, 20, 0)])
async def test_a_cap_of_zero_refuses(caps):
    with pytest.raises(BudgetRefused) as exc:
        await reserve(caps=caps)
    assert exc.value.code == "cap_zero"


async def test_a_crashed_reservation_is_freed_after_ten_minutes():
    caps = Caps(1, 20, 30)
    await reserve(NOW, caps=caps)  # never finished: the worker crashed mid-call
    with pytest.raises(BudgetRefused):
        await reserve(NOW + timedelta(minutes=9), caps=caps)
    await reserve(NOW + timedelta(minutes=11), caps=caps)
    assert await budget.expire_stale(NOW + timedelta(minutes=11)) == 1


async def test_a_sent_reservation_keeps_counting_and_a_released_one_does_not():
    caps = Caps(1, 20, 30)
    slot = await reserve(caps=caps)
    await budget.finish(slot, sent=False, at=NOW)
    slot = await reserve(caps=caps)
    await budget.finish(slot, sent=True, query_id=7, at=NOW)
    with pytest.raises(BudgetRefused):
        await reserve(NOW + timedelta(hours=2), caps=caps)
    async with session_scope() as s:
        usage = await s.get(ResearchUsageDaily, (NOW.date(), "searxng", "seo_content", "search"))
    assert (usage.reserved, usage.sent) == (2, 1)


async def test_the_day_rolls_over_at_utc_midnight():
    caps = Caps(1, 20, 30)
    late = datetime(2026, 10, 20, 23, 59, 30, tzinfo=UTC)
    slot = await reserve(late, caps=caps)
    await budget.finish(slot, sent=True, at=late)
    with pytest.raises(BudgetRefused):
        await reserve(late + timedelta(seconds=20), caps=caps)
    await reserve(late + timedelta(seconds=40), caps=caps)


async def test_spacing_allows_a_burst_of_three_then_one_every_three_seconds():
    spacing = budget.SEARXNG_SPACING
    for _ in range(3):
        await reserve(NOW, spacing=spacing)
    with pytest.raises(BudgetRefused) as exc:
        await reserve(NOW + timedelta(seconds=1), spacing=spacing)
    assert (exc.value.status, exc.value.code, exc.value.retry_after) == ("refused_rate", "spacing", 2)
    await reserve(NOW + timedelta(seconds=3), spacing=spacing)


async def test_no_lock_is_held_after_a_reservation():
    await reserve()
    async with db.get_engine().connect() as conn:
        held = await conn.scalar(text(
            "SELECT count(*) FROM pg_locks l JOIN pg_database d ON d.oid = l.database "
            "WHERE l.locktype = 'advisory' AND d.datname = current_database() AND l.classid = :ns"),
            {"ns": budget.LOCK_NAMESPACE})
    assert held == 0


# --- Cache -----------------------------------------------------------------------------------------------------------


def test_the_cache_key_is_the_question_only():
    key = cache.cache_key("search", "WordPress  6.9 News", language="en-US", time_range=None, page=1, count=10)
    assert key == cache.cache_key("search", "wordpress 6.9 news", language="en-US", time_range=None, page=1, count=10)
    assert key != cache.cache_key("search", "wordpress 6.9 news", language="de", time_range=None, page=1, count=10)
    assert key != cache.cache_key("search", "wordpress 6.9 news", language="en-US", time_range="day", page=1,
                                  count=10)


@pytest.mark.parametrize("status, time_range, ttl", [
    ("ok", None, timedelta(hours=6)), ("partial", "week", timedelta(hours=6)), ("ok", "day", timedelta(hours=1)),
    ("empty", None, timedelta(hours=1)), ("failed", None, None), ("provider_down", None, None),
    ("unreachable", None, None), ("bad_request", None, None),
])
def test_lifetimes(status, time_range, ttl):
    assert cache.lifetime(status, time_range) == ttl


async def test_hits_until_the_entry_expires():
    await cache.put("k1", kind="search", provider="searxng", response={"status": "ok", "results": []},
                    ttl=timedelta(hours=1), at=NOW)
    assert (await cache.lookup("k1", NOW + timedelta(minutes=59)))["status"] == "ok"
    assert await cache.lookup("k1", NOW + timedelta(minutes=61)) is None
    assert await cache.lookup("other", NOW) is None
    async with session_scope() as s:
        assert await s.scalar(text("SELECT hits FROM research_cache WHERE cache_key = 'k1'")) == 1


async def test_identical_searches_in_flight_wait_for_each_other():
    order: list[str] = []

    async def search(name: str, delay: float) -> None:
        await asyncio.sleep(delay)
        async with cache.merged("same-key"):
            order.append(f"{name} in")
            await asyncio.sleep(0.2)
            order.append(f"{name} out")

    await asyncio.gather(search("first", 0), search("second", 0.05))
    assert order == ["first in", "first out", "second in", "second out"]


async def test_a_lock_that_is_never_released_refuses_after_the_wait():
    async with cache.merged("stuck"):
        with pytest.raises(Exception, match="lock"):
            async with cache.merged("stuck", wait_seconds=1):
                pass


# --- The query log -------------------------------------------------------------------------------------------------


async def test_blocked_and_unscreened_rows_never_hold_text_options_or_a_key():
    attempt = store.Attempt(agent="seo_content", task_id=4, trigger_source="dashboard")
    for status in ("blocked", "refused_policy"):
        query_id = await store.log_attempt(attempt, status=status, reason="contact_detail", mode="mock",
                                           query_text="jane.doe@example.com", cache_key="k",
                                           options={"count": 10}, ref="lead:3")
        row = await store.query_row(query_id)
        assert (row.query_text, row.cache_key, row.options) == (None, None, {})
    assert row.blocked_by_ref is None  # a lead or ticket id belongs to a screen refusal only

    with pytest.raises(IntegrityError):  # the database holds the rule too
        async with session_scope() as s:
            s.add(ResearchQuery(agent="seo_content", trigger_source="dashboard", status="blocked", query_text="x"))


async def test_refusal_rows_are_counted_per_day():
    attempt = store.Attempt(agent="supervisor", trigger_source="dashboard")
    await store.log_attempt(attempt, status="blocked", reason="secret", mode="live", provider="searxng", at=NOW)
    await store.log_attempt(attempt, status="failed", reason="cache_error", mode="live", provider="searxng", at=NOW)
    async with session_scope() as s:
        usage = await s.get(ResearchUsageDaily, (NOW.date(), "searxng", "supervisor", "search"))
    assert (usage.blocked, usage.failed) == (1, 1)


async def test_results_are_stored_with_each_page_once():
    attempt = store.Attempt(agent="seo_content", trigger_source="dashboard")
    result = {"rank": 1, "url": "https://example.com/a", "domain": "example.com", "title": "A", "snippet": "s",
              "type": "web", "source": {"provider": "mock", "engines": ["brave"], "site_name": None},
              "published_at": None, "provider_score": 1.0, "authority": None, "flags": ["instruction_like"]}
    ids = []
    for _ in range(2):
        query_id = await store.open_query(attempt, query_text="example query", cache_key="k", options={},
                                          provider="mock", mode="mock", engines=["brave"], flags=[], at=NOW)
        stored = await store.close_query(query_id, status="ok", results=[result], at=NOW)
        ids.append(stored[0]["result_id"])
    async with session_scope() as s:
        sources = (await s.execute(text("SELECT url, flags, expires_at FROM research_sources"))).all()
        results = await s.scalar(text("SELECT count(*) FROM research_results"))
    assert len(sources) == 1 and sources[0].flags == {"instruction_like": True} and results == 2
    assert sources[0].expires_at == NOW + timedelta(days=30) and ids[0] != ids[1]


async def test_retention_purges_old_records_but_not_promoted_pages():
    attempt = store.Attempt(agent="seo_content", trigger_source="dashboard")
    old = NOW - timedelta(days=91)
    await store.log_attempt(attempt, status="blocked", reason="secret", mode="mock", at=old)
    await store.log_attempt(attempt, status="blocked", reason="secret", mode="mock", at=NOW)
    async with session_scope() as s:
        await s.execute(text("INSERT INTO research_sources (url, url_sha256, expires_at, promoted_item_id) VALUES "
                             "('https://a.example', 'a', :old, NULL), ('https://b.example', 'b', :old, 5), "
                             "('https://c.example', 'c', :new, NULL)"), {"old": NOW - timedelta(days=1),
                                                                         "new": NOW + timedelta(days=1)})
    counts = await store.purge_expired(policy.ResearchSettings(), NOW)
    assert counts["queries"] == 1 and counts["sources"] == 1
    async with session_scope() as s:
        assert sorted((await s.scalars(text("SELECT url FROM research_sources"))).all()) == [
            "https://b.example", "https://c.example"]


# --- SearXNG's health row ------------------------------------------------------------------------------------------


async def test_the_health_row_keeps_versions_engine_pauses_and_recent_failures():
    from app.research import health

    await health.record_probe(healthz_ok=True, now=NOW, config={
        "version": "2026.10.4+d48c4b555+dirty",
        "engines": [{"name": "brave", "enabled": True}, {"name": "duckduckgo", "enabled": True},
                    {"name": "google", "enabled": False}]})
    async with session_scope() as s:
        detail = await health.load(s)
    assert (detail["version"], detail["version_ok"], detail["enabled_engines"]) == (
        "2026.10.4-d48c4b555", True, ["brave", "duckduckgo"])
    assert health.state(detail, mode="live", now=NOW) == "healthy"

    failing = [{"engine": e, "reason": "captcha", "suspended": False} for e in health.ENGINES]
    for n in range(3):
        await health.record_search(asked=list(health.ENGINES), unresponsive=failing, answered=False,
                                    now=NOW + timedelta(hours=2 * n))
    async with session_scope() as s:
        detail = await health.load(s)
        row = await s.get(IntegrationStatus, health.ROW)
    later = NOW + timedelta(hours=4, minutes=10)  # within the last search's 60-minute CAPTCHA pause
    assert detail["recent"] == [True, True, True] and detail["consecutive_failures"] == 3
    assert health.state(detail, mode="live", now=later) == "all_engines_failing"
    assert row.last_error == "all_engines_failing" and "captcha" in health.suspension_message(detail, later).lower()

    await health.record_search(asked=["brave"], unresponsive=[], answered=True, now=later)
    await health.record_probe(healthz_ok=False, error="healthz_503", now=later)
    async with session_scope() as s:
        detail = await health.load(s)
    assert detail["engines"]["brave"]["state"] == "ok" and detail["consecutive_failures"] == 0
    assert health.state(detail, mode="live", now=later) == "unreachable"


async def test_rows_left_running_by_a_stopped_worker_are_closed():
    attempt = store.Attempt(agent="seo_content", trigger_source="dashboard")
    query_id = await store.open_query(attempt, query_text="example query", cache_key="k", options={},
                                      provider="searxng", mode="live", engines=[], flags=[], at=NOW)
    assert await store.mark_abandoned(NOW + timedelta(minutes=5)) == 0
    assert await store.mark_abandoned(NOW + timedelta(minutes=11)) == 1
    row = await store.query_row(query_id)
    assert (row.status, row.reason_code) == ("failed", "abandoned")


# --- The migration ----------------------------------------------------------------------------------------------------


async def test_the_migration_runs_twice_and_its_seed_never_overwrites_a_row(migrated):
    sql = (db.MIGRATIONS_DIR / "0008_research.sql").read_text(encoding="utf-8")
    async with migrated.connect() as conn:
        raw = (await conn.get_raw_connection()).driver_connection
        await raw.execute(sql)  # clean_db emptied the tables: the seed comes back
        seeded = {r[0]: r[1] for r in await raw.fetch("SELECT agent, search_daily_cap FROM research_policy")}
        await raw.execute("UPDATE research_policy SET search_daily_cap = 3 WHERE agent = 'seo_content'")
        await raw.execute(sql)
        after = await raw.fetchval("SELECT search_daily_cap FROM research_policy WHERE agent = 'seo_content'")
    assert seeded == {a: p.daily_cap for a, p in policy.DEFAULT_POLICIES.items()} and after == 3


async def test_no_research_table_has_a_foreign_key_into_an_older_table(migrated):
    async with migrated.connect() as conn:
        refs = (await conn.execute(text(
            "SELECT conrelid::regclass::text, confrelid::regclass::text FROM pg_constraint "
            "WHERE contype = 'f' AND conrelid::regclass::text LIKE 'research_%'"))).all()
    assert all(target.startswith("research_") for _, target in refs) and refs
