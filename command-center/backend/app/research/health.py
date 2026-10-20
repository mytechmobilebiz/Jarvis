"""SearXNG's health, and the gateway's own engine cool-downs (plan section 3.11).

The worker keeps the integration_status row ROW ("research_searxng") from two sources: its /healthz and /config
probe (record_probe) and every search it sends (record_search). The row's detail holds codes and times only:
  version, pinned, version_ok    the version /config reports, compared with the pinned image tag;
  engines                        per engine: state (ok, failing, suspended), reason code, until, failures;
  recent                         the last RECENT searches: True when every engine asked failed;
  consecutive_failures, healthz  for the status screens.

The cool-down copies SearXNG's own suspension (its reason, with the time settings.yml gives that reason) and keeps it
in this row, so a SearXNG restart, which forgets its suspensions, does not send the next search straight back to a
site that has just blocked this PC. Cooling engines are left out of the engines= list (app/research/gateway.py).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import session_scope
from ..models import IntegrationStatus

ROW = "research_searxng"
# The image docker-compose.research.yml pins (tag form); /config reports "2026.10.4+d48c4b555", maybe "+dirty".
PINNED_TAG = "2026.10.4-d48c4b555"
ENGINES = ("duckduckgo", "brave", "wikipedia")
ENGINE_TITLES = {"duckduckgo": "DuckDuckGo", "brave": "Brave", "wikipedia": "Wikipedia"}
RECENT = 3

# reason code: (SearXNG's words, seconds an engine is paused with research/searxng/settings.yml, what people read)
REASONS: dict[str, tuple[str, int, str]] = {
    "timeout": ("timeout", 5, "timed out"),
    "connection_error": ("http connection error", 5, "could not be reached"),
    "network_error": ("network error", 5, "could not be reached"),
    "proxy_error": ("proxy error", 5, "could not be reached"),
    "ssl_error": ("ssl error", 5, "failed the certificate check"),
    "http_error": ("http error", 5, "answered with an error"),
    "protocol_error": ("http protocol error", 5, "answered with an error"),
    "parsing_error": ("parsing error", 5, "answered with something SearXNG could not read"),
    "api_error": ("server api error", 5, "answered with an error"),
    "too_many_requests": ("too many requests", 900, "said too many requests"),
    "access_denied": ("access denied", 3600, "refused access"),
    "captcha": ("captcha", 3600, "asked for a CAPTCHA"),
    "cf_access_denied": ("cloudflare access denied", 86400, "refused access through Cloudflare"),
    "recaptcha": ("recaptcha", 604800, "asked for a reCAPTCHA"),
    "cf_captcha": ("cloudflare captcha", 1296000, "asked for a Cloudflare CAPTCHA"),
    "engine_error": ("unexpected crash", 5, "failed"),
}
# Longest match first: "http protocol error" before "http error", "cloudflare captcha" before "captcha".
_BY_WORDS = sorted(((words, code) for code, (words, _, _) in REASONS.items()), key=lambda p: -len(p[0]))
# A block that may last much longer than the time SearXNG reports (Cloudflare and reCAPTCHA blocks are worded alike).
LONG_BLOCKS = frozenset({"captcha", "access_denied", "cf_access_denied", "recaptcha", "cf_captcha"})


def classify(message: object) -> tuple[str, bool]:
    """(reason code, already suspended) for one of SearXNG's unresponsive_engines messages."""
    text = str(message or "").strip().lower()
    suspended = text.startswith("suspended:")
    if suspended:
        text = text.split(":", 1)[1].strip()
    for words, code in _BY_WORDS:
        if words in text:
            return code, suspended
    return "engine_error", suspended


def pause_seconds(reason: str) -> int:
    return REASONS.get(reason, REASONS["engine_error"])[1]


def normalize_version(version: str) -> str:
    """'2026.10.4+d48c4b555+dirty' -> '2026.10.4-d48c4b555' (the Docker tag form)."""
    version = (version or "").strip()
    if version.endswith("+dirty"):
        version = version[: -len("+dirty")]
    return version.replace("+", "-")


def version_ok(reported: str) -> bool:
    return normalize_version(reported) == PINNED_TAG


def _when(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def cooling(detail: dict[str, Any], now: datetime) -> dict[str, datetime]:
    """{engine: until} for the engines still paused."""
    out: dict[str, datetime] = {}
    for engine, state in (detail.get("engines") or {}).items():
        until = _when(state.get("until")) if isinstance(state, dict) else None
        if until and until > now:
            out[engine] = until
    return out


def available_engines(detail: dict[str, Any], now: datetime) -> list[str]:
    paused = cooling(detail, now)
    return [e for e in ENGINES if e not in paused]


def state(detail: dict[str, Any] | None, *, mode: str, internet: str = "unknown", now: datetime) -> str:
    """healthy, degraded, all_engines_failing, unreachable, pc_offline or off (plan section 3.11)."""
    if mode == "off":
        return "off"
    if internet == "offline":
        return "pc_offline"
    if not detail or detail.get("healthz") != "ok":
        return "unreachable"
    paused = cooling(detail, now)
    recent = detail.get("recent") or []
    if all(e in paused for e in ENGINES) or (len(recent) >= RECENT and all(recent[-RECENT:])):
        return "all_engines_failing"
    engines = detail.get("engines") or {}
    if paused or any(isinstance(s, dict) and s.get("state") != "ok" for s in engines.values()):
        return "degraded"
    return "healthy"


def suspension_message(detail: dict[str, Any], now: datetime) -> str:
    """'SearXNG is running, but DuckDuckGo asked for a CAPTCHA (paused for 60 minutes) ... Results come back at 15:20.'"""
    paused = cooling(detail, now)
    if not paused:
        return ""
    parts = []
    long_block = False
    for engine in ENGINES:
        if engine not in paused:
            continue
        reason = (detail.get("engines") or {}).get(engine, {}).get("reason", "engine_error")
        _, seconds, said = REASONS.get(reason, REASONS["engine_error"])
        long_block = long_block or reason in LONG_BLOCKS
        parts.append(f"{ENGINE_TITLES[engine]} {said} (paused for {_duration(seconds)})")
    first_back = min(paused.values())
    text = "SearXNG is running, but " + _join(parts) + f". Results come back at {first_back:%H:%M} UTC."
    if long_block:
        text += " A block by Cloudflare or reCAPTCHA can last 1 to 15 days."
    return text


def _duration(seconds: int) -> str:
    if seconds < 120:
        return f"{seconds} seconds"
    if seconds < 7200:
        return f"{seconds // 60} minutes"
    if seconds < 172800:
        return f"{seconds // 3600} hours"
    return f"{seconds // 86400} days"


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


# --- The row --------------------------------------------------------------------------------------------------------


async def load(session: AsyncSession) -> dict[str, Any]:
    row = await session.get(IntegrationStatus, ROW, populate_existing=True)
    return dict(row.detail or {}) if row else {}


async def _locked(session: AsyncSession, now: datetime) -> IntegrationStatus:
    await session.execute(pg_insert(IntegrationStatus).values(name=ROW, detail={}, updated_at=now)
                          .on_conflict_do_nothing(index_elements=[IntegrationStatus.name]))
    row = await session.scalar(select(IntegrationStatus).where(IntegrationStatus.name == ROW)
                               .with_for_update().execution_options(populate_existing=True))
    assert row is not None
    return row


async def record_search(*, asked: list[str], unresponsive: list[dict[str, Any]], answered: bool,
                        now: datetime | None = None) -> None:
    """After a search SearXNG answered: each engine asked is ok, failing or paused. Own transaction."""
    now = now or datetime.now(UTC)
    failed = {u["engine"]: u for u in unresponsive if isinstance(u, dict) and u.get("engine") in ENGINES}
    async with session_scope() as session:
        row = await _locked(session, now)
        detail = dict(row.detail or {})
        engines = dict(detail.get("engines") or {})
        for engine in asked:
            before = engines.get(engine) if isinstance(engines.get(engine), dict) else {}
            if engine in failed:
                reason = failed[engine].get("reason", "engine_error")
                until = now + timedelta(seconds=pause_seconds(reason))
                engines[engine] = {"state": "suspended", "reason": reason, "until": until.isoformat(),
                                   "failures": int(before.get("failures", 0)) + 1, "at": now.isoformat()}
            else:
                engines[engine] = {"state": "ok", "reason": "", "until": None, "failures": 0, "at": now.isoformat()}
        all_failed = bool(asked) and all(e in failed for e in asked)
        detail["engines"] = engines
        detail["recent"] = [*(detail.get("recent") or [])[-(RECENT - 1):], all_failed]
        detail["consecutive_failures"] = int(detail.get("consecutive_failures", 0)) + 1 if all_failed else 0
        if answered:
            row.last_ok_at = now
        if all_failed:
            row.last_error_at, row.last_error = now, "all_engines_failing"
        row.detail = detail
        row.updated_at = now


async def record_failure(code: str, now: datetime | None = None) -> None:
    """SearXNG itself did not answer usefully (unreachable, a redirect, an error page). Own transaction."""
    now = now or datetime.now(UTC)
    async with session_scope() as session:
        row = await _locked(session, now)
        detail = dict(row.detail or {})
        detail["consecutive_failures"] = int(detail.get("consecutive_failures", 0)) + 1
        row.last_error_at, row.last_error = now, code[:100]
        row.detail = detail
        row.updated_at = now


async def record_probe(*, healthz_ok: bool, config: dict[str, Any] | None = None, error: str = "",
                       now: datetime | None = None) -> None:
    """The worker's /healthz (every minute) and /config (hourly) probe. Own transaction."""
    now = now or datetime.now(UTC)
    async with session_scope() as session:
        row = await _locked(session, now)
        detail = dict(row.detail or {})
        detail["healthz"] = "ok" if healthz_ok else "failed"
        detail["probed_at"] = now.isoformat()
        if config is not None:
            version = str(config.get("version") or "")
            detail.update(version=normalize_version(version), pinned=PINNED_TAG, version_ok=version_ok(version))
            listed = config.get("engines") if isinstance(config.get("engines"), list) else []
            detail["enabled_engines"] = sorted(str(e.get("name")) for e in listed
                                               if isinstance(e, dict) and e.get("enabled") and e.get("name"))
        if healthz_ok:
            row.last_ok_at = now
        else:
            row.last_error_at, row.last_error = now, (error or "healthz_failed")[:100]
        row.detail = detail
        row.updated_at = now
