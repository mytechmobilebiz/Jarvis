"""Uptime figures from the stored site checks.

A check counts as "up" unless its status was "down" (a degraded site still
answers). Figures are based on the checks that ran, normally every 15 minutes,
so they are estimates at that resolution.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import SiteCheck

WINDOWS = {"24h": "24 hours", "7d": "7 days", "30d": "30 days"}

_STATS_SQL = text("""
SELECT site_id,
       count(*) FILTER (WHERE checked_at > now() - interval '24 hours')                      AS n_24h,
       count(*) FILTER (WHERE checked_at > now() - interval '24 hours' AND status <> 'down') AS up_24h,
       count(*) FILTER (WHERE checked_at > now() - interval '7 days')                        AS n_7d,
       count(*) FILTER (WHERE checked_at > now() - interval '7 days' AND status <> 'down')   AS up_7d,
       count(*)                                                                               AS n_30d,
       count(*) FILTER (WHERE status <> 'down')                                               AS up_30d,
       avg(response_ms) FILTER (WHERE checked_at > now() - interval '24 hours')               AS avg_ms_24h
  FROM site_checks
 WHERE checked_at > now() - interval '30 days'
 GROUP BY site_id
""")


def _pct(up: int, n: int) -> float | None:
    return round(100.0 * up / n, 2) if n else None


async def uptime_stats(session: AsyncSession) -> dict[int, dict[str, Any]]:
    """{site_id: {"24h": %, "7d": %, "30d": %, "checks_30d": n, "avg_response_ms_24h": ms}} for every site with checks."""
    out: dict[int, dict[str, Any]] = {}
    for r in (await session.execute(_STATS_SQL)).mappings():
        out[r["site_id"]] = {
            "24h": _pct(r["up_24h"], r["n_24h"]),
            "7d": _pct(r["up_7d"], r["n_7d"]),
            "30d": _pct(r["up_30d"], r["n_30d"]),
            "checks_30d": r["n_30d"],
            "avg_response_ms_24h": int(r["avg_ms_24h"]) if r["avg_ms_24h"] is not None else None,
        }
    return out


async def incidents(session: AsyncSession, site_id: int, days: int = 30) -> list[dict[str, Any]]:
    """Periods the site was down, from consecutive checks: [{"start", "end" (None if ongoing), "minutes", "reason"}]."""
    since = datetime.now(UTC) - timedelta(days=days)
    from sqlalchemy import select

    rows = (
        await session.execute(
            select(SiteCheck.checked_at, SiteCheck.status, SiteCheck.findings)
            .where(SiteCheck.site_id == site_id, SiteCheck.checked_at > since)
            .order_by(SiteCheck.checked_at)
        )
    ).all()
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for at, status, findings in rows:
        if status == "down" and current is None:
            reason = next((f.get("title", "") for f in findings or [] if f.get("severity") == "critical"), "down")
            current = {"start": at, "end": None, "reason": reason}
        elif status != "down" and current is not None:
            current["end"] = at
            out.append(current)
            current = None
    if current is not None:
        out.append(current)
    now = datetime.now(UTC)
    for inc in out:
        inc["minutes"] = int(((inc["end"] or now) - inc["start"]).total_seconds() // 60)
        inc["start"] = inc["start"].isoformat()
        inc["end"] = inc["end"].isoformat() if inc["end"] else None
    return out
