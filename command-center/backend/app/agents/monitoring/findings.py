"""A site's open issues: the latest quick check and the latest deep check together.

The 15-minute uptime sweep is a quick check; the nightly security scan (and
"Scan now") is a deep check, the only kind that looks for cloaking, exposed
files, user enumeration and an outdated WordPress core. Every check updates
the site's latest check, so taking the open issues from that check alone
would drop the night's security findings at the next quick sweep.

The rule: when the latest deep check is the newer of the two, it covers
everything a quick check does and is the only source. Otherwise the quick
check gives everything it re-checks, and the deep check adds what only a deep
check can find (DEEP_ONLY_CODES). The site's status takes the same rule:
a quick check cannot clear what only a deep check can see (carried_causes).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ...models import SiteCheck

# Findings only a deep check looks for (monitoring/checks.py: _assess_cloaking, _assess_http_redirect,
# the core version and _assess_exposures).
DEEP_ONLY_CODES = frozenset({
    "conditional_redirect", "cloaked_spam", "cloaked_injection", "cloaked_script", "no_https_redirect",
    "wp_version_disclosed", "wp_outdated", "xmlrpc_enabled", "user_enumeration", "readme_exposed",
    "debug_log_exposed", "directory_listing",
})
# Of those, the ones that make a deep check's status degraded (checks.py degrades on a critical cloaking finding).
DEEP_STATUS_CODES = frozenset({"conditional_redirect", "cloaked_spam", "cloaked_injection"})
DEEP_WINDOW = timedelta(days=30)  # an older deep check is no longer a current source
SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
STATUS_ORDER = {"up": 0, "degraded": 1, "down": 2}
NO_OPEN_FINDINGS: dict[str, Any] = {"open_findings": [], "open_findings_sources": {"quick": None, "deep": None}}


def _is_deep(check: SiteCheck) -> bool:
    return bool((check.details or {}).get("deep"))


def carried_causes(deep: SiteCheck | None) -> list[dict[str, Any]]:
    """The deep check's status-changing findings that a quick check cannot re-check."""
    if deep is None:
        return []
    return [f for f in deep.findings or [] if f.get("code") in DEEP_STATUS_CODES and f.get("severity") == "critical"]


def effective_status(status: str, carried: list[dict[str, Any]]) -> str:
    """A quick check's status for the site: at least degraded while the latest deep check found cloaking."""
    if carried and STATUS_ORDER.get(status, 0) < STATUS_ORDER["degraded"]:
        return "degraded"
    return status


def merge(quick: SiteCheck | None, deep: SiteCheck | None) -> dict[str, Any]:
    """{"open_findings": [...], "open_findings_sources": {"quick": iso|None, "deep": iso|None}}.

    Each finding is the stored one (code, severity, title, detail) plus "source" ("quick" or "deep") and
    "checked_at". The sources are the checks the list was built from: "quick" is None when there is no
    quick check or the deep check is newer.
    """
    if quick is not None and deep is not None and deep.checked_at >= quick.checked_at:
        quick = None
    picked: list[dict[str, Any]] = []
    if quick is not None:
        picked += [{**f, "source": "quick", "checked_at": quick.checked_at.isoformat()} for f in quick.findings or []]
    if deep is not None:
        partial = bool((deep.details or {}).get("partial"))
        for f in deep.findings or []:
            # With a newer quick check, only what a quick check cannot see (and the note that the scan was cut short).
            if quick is None or f.get("code") in DEEP_ONLY_CODES or (partial and f.get("code") == "check_timeout"):
                picked.append({**f, "source": "deep", "checked_at": deep.checked_at.isoformat()})
    picked = [f for f in picked if f.get("severity") != "info"]
    picked.sort(key=lambda f: SEVERITY_ORDER.get(f.get("severity", "info"), 3))
    return {
        "open_findings": picked,
        "open_findings_sources": {
            "quick": quick.checked_at.isoformat() if quick is not None else None,
            "deep": deep.checked_at.isoformat() if deep is not None else None,
        },
    }


_LATEST_SQL = text("""
SELECT c.id
  FROM unnest(CAST(:site_ids AS bigint[])) AS s(id)
 CROSS JOIN LATERAL (
       SELECT id FROM site_checks
        WHERE site_id = s.id AND (details @> '{"deep": true}') = :deep AND checked_at > :since
        ORDER BY checked_at DESC
        LIMIT 1) AS c
""")


async def _latest_ids(session: AsyncSession, site_ids: list[int], deep: bool) -> list[int]:
    since = datetime.now(UTC) - DEEP_WINDOW if deep else datetime.min.replace(tzinfo=UTC)
    return list((await session.scalars(_LATEST_SQL, {"site_ids": site_ids, "deep": deep, "since": since})).all())


async def latest_checks(session: AsyncSession, site_ids: list[int]) -> dict[int, tuple[SiteCheck | None, SiteCheck | None]]:
    """{site_id: (latest quick check, latest deep check within DEEP_WINDOW)}: one index walk per site and kind."""
    out: dict[int, tuple[SiteCheck | None, SiteCheck | None]] = {i: (None, None) for i in site_ids}
    if not site_ids:
        return out
    ids = [*await _latest_ids(session, site_ids, False), *await _latest_ids(session, site_ids, True)]
    for check in (await session.scalars(select(SiteCheck).where(SiteCheck.id.in_(ids)))).all() if ids else []:
        quick, deep = out[check.site_id]
        out[check.site_id] = (quick, check) if _is_deep(check) else (check, deep)
    return out


async def latest_deep_check(session: AsyncSession, site_id: int) -> SiteCheck | None:
    ids = await _latest_ids(session, [site_id], True)
    return await session.get(SiteCheck, ids[0]) if ids else None


async def open_findings(session: AsyncSession, site_ids: list[int]) -> dict[int, dict[str, Any]]:
    """{site_id: {"open_findings": [...], "open_findings_sources": {...}}} for each site (see merge)."""
    return {site_id: merge(quick, deep) for site_id, (quick, deep) in (await latest_checks(session, site_ids)).items()}
