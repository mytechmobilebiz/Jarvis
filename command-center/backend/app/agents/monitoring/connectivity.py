"""Is the Command Center itself online? The control check made before a site is recorded as down.

Monitoring runs on the owner's PC. When that PC loses its internet connection
(Wi-Fi drops, the router restarts, Windows wakes from sleep before the network
is back), every client site fails the same way: the name does not resolve or
nothing connects. None of them is down.

So a check whose home page never answered (no HTTP status at all) is not
recorded until a control check has run: a GET to a few well-known hosts that
have nothing to do with the client sites. Any HTTP answer from any of them,
whatever its status, means the Command Center is online and the failure is the
site's own. If none answers, the site was *not checked*: no SiteCheck row is
written, its last status and last-checked time stay as they were, and its
uptime and incidents are computed only from the checks that really ran (see
app/uptime.py). The outage is kept in the integration_status row
CONNECTIVITY_ROW (state, since, last_ok_at, last_error_at), and one warning
event (EVENT_OFFLINE) is emitted when it starts and one info event
(EVENT_ONLINE) when it ends, however many sweeps run in between.

A check that got any HTTP answer, even an error page, needs no control check:
the site answered. When every client site is down while the control hosts
answer (all on one server that failed, say), that is real downtime and is
recorded as such.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ... import events
from ...config import Settings
from ...models import IntegrationStatus
from ..http import ReadOnlyHttpClient
from .checks import SiteReport, describe_fetch_error

CONNECTIVITY_ROW = "internet"  # integration_status.name
EVENT_OFFLINE = "monitoring.offline"
EVENT_ONLINE = "monitoring.online"
OFFLINE_REASON = "not checked: the Command Center could not reach the internet (not recorded as down)"
# Besides the WordPress.org API (settings.wp_version_api_url), already used for the version check: two
# unrelated operators, so one of them being down does not look like the PC being offline.
EXTRA_CONTROL_URLS = ("https://www.cloudflare.com/cdn-cgi/trace", "https://www.google.com/generate_204")
CONTROL_MAX_BYTES = 2_048
# A control check that started at most this long before a site's failure was seen also covers it.
CONTROL_REUSE_SECONDS = 30.0


def control_urls(settings: Settings) -> list[str]:
    return [settings.wp_version_api_url, *EXTRA_CONTROL_URLS]


def is_network_failure(report: SiteReport) -> bool:
    """The home page never answered (DNS, connect, TLS handshake or timeout): it may be this PC's connection."""
    return report.status == "down" and report.http_status is None


async def internet_reachable(http: ReadOnlyHttpClient, urls: list[str], timeout: float) -> tuple[bool, str]:
    """(True, "") as soon as any of urls gives an HTTP answer; else (False, why each one failed)."""

    async def probe(url: str) -> str:
        try:
            await asyncio.wait_for(http.fetch(url, max_bytes=CONTROL_MAX_BYTES, follow_redirects=False), timeout)
        except TimeoutError:
            return f"{urlsplit(url).hostname}: no answer within {timeout:g} seconds"
        except Exception as exc:  # noqa: BLE001 - every failure is the answer
            return f"{urlsplit(url).hostname}: {describe_fetch_error(exc, timeout)}"
        return ""

    probes = [asyncio.ensure_future(probe(u)) for u in urls]
    errors: list[str] = []
    try:
        for finished in asyncio.as_completed(probes):
            error = await finished
            if not error:
                return True, ""
            errors.append(error)
    finally:
        for p in probes:
            p.cancel()
        await asyncio.gather(*probes, return_exceptions=True)
    return False, "; ".join(sorted(errors)) or "no control hosts configured"


@dataclass
class _Control:
    started: float  # time.monotonic()
    online: bool
    error: str


class ConnectivityGuard:
    """One per sweep, shared by its concurrent site checks.

    online() runs the control check, or reuses one that started at most
    CONTROL_REUSE_SECONDS before the failure it is asked about, so a sweep
    of fifty unreachable sites makes one or two control checks, not fifty.
    It also keeps the latest evidence either way (a site that answered is
    evidence of being online), for the state recorded at the end of the sweep.
    """

    def __init__(self, http: ReadOnlyHttpClient, settings: Settings) -> None:
        self.http = http
        self.settings = settings
        self._lock = asyncio.Lock()
        self._control: _Control | None = None
        self.online_seen: float | None = None  # monotonic time of the latest evidence
        self.offline_seen: float | None = None
        self.error = ""

    def saw_answer(self) -> None:
        self.online_seen = time.monotonic()

    async def online(self, failed_at: float) -> bool:
        async with self._lock:
            control = self._control
            if control is None or control.started < failed_at - CONTROL_REUSE_SECONDS:
                started = time.monotonic()
                ok, error = await internet_reachable(self.http, control_urls(self.settings), self.settings.monitor_timeout_seconds)
                control = self._control = _Control(started=started, online=ok, error=error)
                if ok:
                    self.online_seen = time.monotonic()
                else:
                    self.offline_seen, self.error = time.monotonic(), error
            return control.online

    @property
    def state(self) -> str | None:
        """'online' or 'offline' by the latest evidence of this sweep; None if there was none."""
        if self.offline_seen is not None and (self.online_seen is None or self.offline_seen > self.online_seen):
            return "offline"
        return "online" if self.online_seen is not None else None


async def record_state(session: AsyncSession, settings: Settings, *, online: bool, error: str = "",
                       source: str = "monitoring", task_id: int | None = None) -> str | None:
    """Store the Command Center's connectivity; emit an event only when it changes. Returns the event kind, if any.

    The row is locked, so two sweeps that notice the same outage emit one event between them.
    """
    now = datetime.now(UTC)
    await session.execute(pg_insert(IntegrationStatus).values(name=CONNECTIVITY_ROW, detail={}, updated_at=now)
                          .on_conflict_do_nothing(index_elements=[IntegrationStatus.name]))
    row = await session.scalar(select(IntegrationStatus).where(IntegrationStatus.name == CONNECTIVITY_ROW)
                               .with_for_update().execution_options(populate_existing=True))
    assert row is not None
    detail: dict[str, Any] = dict(row.detail or {})
    previous = detail.get("state")
    state = "online" if online else "offline"
    if online:
        row.last_ok_at = now
    else:
        row.last_error_at, row.last_error = now, error[:500]
    kind = None
    if state != previous:
        since = detail.get("since")
        detail.update(state=state, since=now.isoformat())
        if not online:
            kind = EVENT_OFFLINE
            await events.emit(session, settings, kind=kind, severity="warning", source=source, task_id=task_id,
                              title="Command Center lost internet: website checks are paused, and no site is recorded as down",
                              detail={"since": now.isoformat(), "error": error[:500]})
        elif previous == "offline":
            kind = EVENT_ONLINE
            minutes = _minutes_since(since, now)
            await events.emit(session, settings, kind=kind, severity="info", source=source, task_id=task_id,
                              title="Command Center is back online" + (f" after {minutes} min" if minutes is not None else "")
                              + "; website checks resumed",
                              detail={"offline_since": since, "online_at": now.isoformat()})
    row.detail = detail
    row.updated_at = now
    return kind


def _minutes_since(iso: str | None, now: datetime) -> int | None:
    try:
        return int((now - datetime.fromisoformat(iso)).total_seconds() // 60) if iso else None
    except ValueError:
        return None


def connectivity_view(row: IntegrationStatus | None) -> dict[str, Any]:
    """For /api/system/status: what the monitoring last found about this PC's own connection."""
    if row is None:
        return {"state": "unknown", "since": None, "last_ok_at": None, "last_error_at": None, "last_error": ""}
    detail = row.detail or {}
    return {
        "state": detail.get("state") if detail.get("state") in ("online", "offline") else "unknown",
        "since": detail.get("since"),
        "last_ok_at": row.last_ok_at.isoformat() if row.last_ok_at else None,
        "last_error_at": row.last_error_at.isoformat() if row.last_error_at else None,
        "last_error": row.last_error or "",
    }
