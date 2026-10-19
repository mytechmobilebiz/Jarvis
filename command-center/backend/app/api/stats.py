"""Read-only aggregates for the dashboard (counts only; no task contents)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .deps import Principal, get_db, require_user

router = APIRouter(prefix="/api/stats", tags=["stats"])

# Planning and summarising are the supervisor's own bookkeeping, not work anyone asked for.
INTERNAL = {("supervisor", "plan_request"), ("supervisor", "summarize_request")}

# Display categories for finished tasks, in legend order. Every registered capability must map to one
# (tests/unit/test_stats.py fails otherwise), so a new capability cannot silently vanish from the chart.
CATEGORIES: list[tuple[str, str]] = [
    ("website_scans", "Website scans"),
    ("seo", "SEO"),
    ("content", "Content & marketing"),
    ("email", "Email & clients"),
    ("server_checks", "Server checks"),
    ("repairs", "Repairs"),
    ("reports", "Reports"),
    ("credit_repair", "Credit repair"),
]

CATEGORY_OF: dict[tuple[str, str], str] = {
    ("monitoring", "check_site"): "website_scans",
    ("monitoring", "check_url"): "website_scans",
    ("monitoring", "check_all_sites"): "website_scans",
    ("wordpress_security", "security_scan"): "website_scans",
    ("wordpress_security", "security_scan_all"): "website_scans",
    ("wordpress_security", "wp_diagnose"): "website_scans",
    ("seo_content", "audit_page"): "seo",
    ("seo_content", "keyword_brief"): "seo",
    ("seo_content", "draft_blog_post"): "content",
    ("seo_content", "publish_draft"): "content",
    ("social_marketing", "draft_social_posts"): "content",
    ("social_marketing", "publish_draft"): "content",
    ("social_marketing", "change_budget"): "content",
    ("social_marketing", "ads_report"): "content",
    ("communications", "triage_ticket"): "email",
    ("communications", "draft_reply"): "email",
    ("communications", "send_reply"): "email",
    ("communications", "support_overview"): "email",
    ("communications", "score_lead"): "email",
    ("communications", "draft_proposal"): "email",
    ("communications", "send_proposal"): "email",
    ("communications", "lead_overview"): "email",
    ("server_management", "test_connection"): "server_checks",
    ("server_management", "scan_host_key"): "server_checks",
    ("server_management", "server_overview"): "server_checks",
    ("server_management", "check_services"): "server_checks",
    ("monitoring", "check_server"): "server_checks",
    ("wordpress_security", "repair_restore_site_url"): "repairs",
    ("wordpress_security", "repair_deactivate_plugin"): "repairs",
    ("wordpress_security", "repair_activate_plugin"): "repairs",
    ("wordpress_security", "repair_update_plugin"): "repairs",
    ("wordpress_security", "repair_reinstall_plugin"): "repairs",
    ("wordpress_security", "repair_reinstall_core"): "repairs",
    ("wordpress_security", "repair_maintenance_mode"): "repairs",
    ("wordpress_security", "repair_flush_caches"): "repairs",
    ("wordpress_security", "repair_quarantine_debug_log"): "repairs",
    ("server_management", "restart_service"): "repairs",
    ("wordpress_security", "security_report"): "reports",
    ("monitoring", "sites_overview"): "reports",
    ("monitoring", "uptime_report"): "reports",
    ("supervisor", "daily_briefing"): "reports",
    ("supervisor", "health_review"): "reports",
    ("credit_repair", "compliance_review"): "credit_repair",
    ("credit_repair", "draft_dispute_letters"): "credit_repair",
    ("credit_repair", "mail_dispute_letter"): "credit_repair",
}

# range -> (length, bucket); buckets are aligned to the caller's local clock.
RANGES: dict[str, tuple[timedelta, str]] = {
    "24h": (timedelta(hours=24), "hour"),
    "7d": (timedelta(days=7), "6 hours"),
    "30d": (timedelta(days=30), "day"),
}

_BUCKET_SQL = {
    "hour": "date_trunc('hour', finished_at AT TIME ZONE :tz)",
    "6 hours": "date_trunc('day', finished_at AT TIME ZONE :tz)"
    " + floor(extract(hour FROM finished_at AT TIME ZONE :tz) / 6) * interval '6 hours'",
    "day": "date_trunc('day', finished_at AT TIME ZONE :tz)",
}

_FINISHED = "status IN ('succeeded', 'failed') AND finished_at IS NOT NULL"


def category(agent: str, capability: str) -> str | None:
    """The chart category of a capability, or None for the supervisor's internal steps."""
    if (agent, capability) in INTERNAL:
        return None
    return CATEGORY_OF.get((agent, capability), "reports")


def _local_floor(t: datetime, bucket: str) -> datetime:
    """The start of t's bucket, as naive local wall-clock time (the form the SQL returns)."""
    if bucket == "day":
        return t.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket == "6 hours":
        return t.replace(hour=t.hour - t.hour % 6, minute=0, second=0, microsecond=0)
    return t.replace(minute=0, second=0, microsecond=0)


def _step(bucket: str) -> timedelta:
    return {"hour": timedelta(hours=1), "6 hours": timedelta(hours=6), "day": timedelta(days=1)}[bucket]


async def _finished_between(session: AsyncSession, start: datetime, end: datetime) -> dict[str, int]:
    rows = await session.execute(
        text(f"SELECT agent, capability, status, count(*) FROM tasks WHERE {_FINISHED}"
             " AND finished_at >= :start AND finished_at < :end GROUP BY 1, 2, 3"),
        {"start": start, "end": end},
    )
    out = {"succeeded": 0, "failed": 0}
    for agent, capability, status, n in rows.all():
        if category(agent, capability) is not None:
            out[status] += n
    return out


@router.get("/task-activity")
async def task_activity(
    _: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_db),
    range_: Literal["24h", "7d", "30d"] = Query("24h", alias="range"),
    tz: str = Query("UTC", max_length=64),
) -> dict[str, Any]:
    """Finished tasks per category and time bucket, plus today against yesterday by the same clock time."""
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise HTTPException(400, f"unknown time zone: {tz!r}") from e
    length, bucket = RANGES[range_]
    now = datetime.now(UTC)
    since = now - length
    rows = await session.execute(
        text(f"SELECT {_BUCKET_SQL[bucket]} AS b, agent, capability, status, count(*) AS n FROM tasks"
             f" WHERE {_FINISHED} AND finished_at >= :since GROUP BY 1, 2, 3, 4"),
        {"tz": tz, "since": since},
    )
    keys = [k for k, _label in CATEGORIES]
    by_bucket: dict[datetime, dict[str, Any]] = {}
    totals = {k: 0 for k in keys}
    failed_total = 0
    for b, agent, capability, status, n in rows.all():
        cat = category(agent, capability)
        if cat is None:
            continue
        slot = by_bucket.setdefault(b, {"counts": {k: 0 for k in keys}, "failed": 0})
        slot["counts"][cat] += n
        totals[cat] += n
        if status == "failed":
            slot["failed"] += n
            failed_total += n

    # Every bucket from the start of the range to now, so the chart has a continuous time axis.
    buckets = []
    t = _local_floor(since.astimezone(zone).replace(tzinfo=None), bucket)
    end = now.astimezone(zone).replace(tzinfo=None)
    while t <= end:
        slot = by_bucket.get(t, {"counts": {k: 0 for k in keys}, "failed": 0})
        buckets.append({"start": t.replace(tzinfo=zone).isoformat(), **slot})
        t += _step(bucket)

    local_now = now.astimezone(zone)
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = (today_start.replace(tzinfo=None) - timedelta(days=1)).replace(tzinfo=zone)
    yesterday_same_time = (local_now.replace(tzinfo=None) - timedelta(days=1)).replace(tzinfo=zone)
    return {
        "range": range_,
        "bucket": bucket,
        "tz": tz,
        "since": since.isoformat(),
        "categories": [{"key": k, "label": label} for k, label in CATEGORIES],
        "buckets": buckets,
        "totals": totals,
        "failed": failed_total,
        "today": await _finished_between(session, today_start, now),
        "yesterday_same_time": await _finished_between(session, yesterday_start, yesterday_same_time),
    }
