"""Uptime figures from the stored site checks.

A check counts as "up" unless its status was "down" (a degraded site still
answers). Figures are based on the checks that ran, normally every 15 minutes,
so they are estimates at that resolution.

Time nobody checked (the owner's PC off or asleep, or without internet: see
agents/monitoring/connectivity.py, which then records no check rather than a
"down" one) counts neither as up nor as down. So every figure comes with its
coverage: the number of checks behind it (n_24h, n_7d, n_30d), the clock hours
that had at least one check (hours_checked_*), and the oldest check in the
30-day window (first_check_at). Incidents end where the checks stopped.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import SiteCheck

WINDOWS = {"24h": "24 hours", "7d": "7 days", "30d": "30 days"}
# Between two checks further apart than this (and than GAP_FACTOR times the site's usual interval), nobody checked.
MIN_UNCHECKED_GAP = timedelta(minutes=45)
GAP_FACTOR = 3

_STATS_SQL = text("""
SELECT site_id,
       count(*) FILTER (WHERE checked_at > now() - interval '24 hours')                      AS n_24h,
       count(*) FILTER (WHERE checked_at > now() - interval '24 hours' AND status <> 'down') AS up_24h,
       count(*) FILTER (WHERE checked_at > now() - interval '7 days')                        AS n_7d,
       count(*) FILTER (WHERE checked_at > now() - interval '7 days' AND status <> 'down')   AS up_7d,
       count(*)                                                                               AS n_30d,
       count(*) FILTER (WHERE status <> 'down')                                               AS up_30d,
       avg(response_ms) FILTER (WHERE checked_at > now() - interval '24 hours')               AS avg_ms_24h,
       min(checked_at)                                                                        AS first_30d,
       least(24, count(DISTINCT date_trunc('hour', checked_at)) FILTER (WHERE checked_at > now() - interval '24 hours'))
                                                                                              AS hours_24h,
       least(168, count(DISTINCT date_trunc('hour', checked_at)) FILTER (WHERE checked_at > now() - interval '7 days'))
                                                                                              AS hours_7d,
       least(720, count(DISTINCT date_trunc('hour', checked_at)))                             AS hours_30d
  FROM site_checks
 WHERE checked_at > now() - interval '30 days'
 GROUP BY site_id
""")


def _pct(up: int, n: int) -> float | None:
    return round(100.0 * up / n, 2) if n else None


async def uptime_stats(session: AsyncSession) -> dict[int, dict[str, Any]]:
    """{site_id: {"24h": %, "7d": %, "30d": %, "n_24h": n, "n_7d": n, "n_30d": n, "checks_30d": n,
    "hours_checked_24h": h, "hours_checked_7d": h, "hours_checked_30d": h, "avg_response_ms_24h": ms,
    "first_check_at": iso}} for every site with checks.

    n_* is the number of checks behind each percentage (checks_30d, kept for older clients, equals n_30d);
    hours_checked_* the clock hours of the window (at most 24, 168 or 720) with at least one check.
    first_check_at is the oldest check in the 30-day window: on a younger site the 30-day figure covers only
    the time since then.
    """
    out: dict[int, dict[str, Any]] = {}
    for r in (await session.execute(_STATS_SQL)).mappings():
        out[r["site_id"]] = {
            "24h": _pct(r["up_24h"], r["n_24h"]),
            "7d": _pct(r["up_7d"], r["n_7d"]),
            "30d": _pct(r["up_30d"], r["n_30d"]),
            "n_24h": r["n_24h"],
            "n_7d": r["n_7d"],
            "n_30d": r["n_30d"],
            "checks_30d": r["n_30d"],
            "hours_checked_24h": r["hours_24h"],
            "hours_checked_7d": r["hours_7d"],
            "hours_checked_30d": r["hours_30d"],
            "avg_response_ms_24h": int(r["avg_ms_24h"]) if r["avg_ms_24h"] is not None else None,
            "first_check_at": r["first_30d"].isoformat() if r["first_30d"] else None,
        }
    return out


def unchecked_gap(times: list[datetime]) -> timedelta:
    """How far apart two checks must be for the time between them to count as not checked.

    GAP_FACTOR times the site's usual (median) interval, at least MIN_UNCHECKED_GAP: a 15-minute sweep
    that is late does not split an incident, an hourly schedule is not mistaken for gaps.
    """
    intervals = [b - a for a, b in zip(times, times[1:]) if b > a]
    usual = statistics.median(intervals) if intervals else timedelta(0)
    return max(MIN_UNCHECKED_GAP, usual * GAP_FACTOR)


async def incidents(session: AsyncSession, site_id: int, days: int = 30) -> list[dict[str, Any]]:
    """Periods the site was down, from consecutive checks: [{"start", "end", "minutes", "reason", "end_reason"}].

    end_reason "recovered": end is the first check that found it up again. "not_checked": the checks stopped
    (see unchecked_gap) while it was down, so end is the last check that found it down and minutes is how
    long it was seen down, at least; a down check after the gap starts a new incident. None: still down,
    end is None and minutes runs until now.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (
        await session.execute(
            select(SiteCheck.checked_at, SiteCheck.status, SiteCheck.findings)
            .where(SiteCheck.site_id == site_id, SiteCheck.checked_at > since)
            .order_by(SiteCheck.checked_at)
        )
    ).all()
    now = datetime.now(UTC)
    gap = unchecked_gap([at for at, _, _ in rows])
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    last_at: datetime | None = None
    for at, status, findings in rows:
        if current is not None and last_at is not None and at - last_at > gap:
            current.update(end=last_at, end_reason="not_checked")  # nobody checked in between
            out.append(current)
            current = None
        if status == "down" and current is None:
            reason = next((f.get("title", "") for f in findings or [] if f.get("severity") == "critical"), "down")
            current = {"start": at, "end": None, "reason": reason, "end_reason": None}
        elif status != "down" and current is not None:
            current.update(end=at, end_reason="recovered")
            out.append(current)
            current = None
        last_at = at
    if current is not None:
        if last_at is not None and now - last_at > gap:
            current.update(end=last_at, end_reason="not_checked")
        out.append(current)
    for inc in out:
        inc["minutes"] = int(((inc["end"] or now) - inc["start"]).total_seconds() // 60)
        inc["start"] = inc["start"].isoformat()
        inc["end"] = inc["end"].isoformat() if inc["end"] else None
    return out
