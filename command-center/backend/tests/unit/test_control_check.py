"""The control check (is the Command Center itself online?), open-issue merging and incident gaps: no database."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import httpx

from app.agents.http import ReadOnlyHttpClient
from app.agents.monitoring import connectivity
from app.agents.monitoring import findings as open_issues
from app.agents.monitoring.checks import SiteReport
from app.config import Settings
from app.credit_repair import store
from app.models import IntegrationStatus, SiteCheck
from app.uptime import MIN_UNCHECKED_GAP, unchecked_gap

from ..fakes import FakeWeb

URLS = ["https://control-a.example/", "https://control-b.example/", "https://control-c.example/"]


async def _hang(request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(60)
    raise AssertionError("should have been abandoned")


async def reachable(web: FakeWeb, urls: list[str] = URLS, timeout: float = 5) -> tuple[bool, str]:
    async with ReadOnlyHttpClient(Settings(), transport=web.transport) as http:
        return await connectivity.internet_reachable(http, urls, timeout)


async def test_any_http_answer_means_online_whatever_its_status():
    web = FakeWeb()
    web.add("control-b.example", lambda r: httpx.Response(503, text="maintenance"))
    assert await reachable(web) == (True, "")
    assert web.methods() == {"GET"}


async def test_no_answer_from_any_control_host_means_offline_and_says_why():
    ok, why = await reachable(FakeWeb())
    assert ok is False
    assert why.count("does not resolve") == 3 and "control-a.example" in why and "control-c.example" in why


async def test_a_hanging_control_host_is_abandoned_once_another_answers():
    web = FakeWeb()
    web.add("control-a.example", _hang)
    web.add("control-b.example", lambda r: httpx.Response(204))
    started = time.monotonic()
    assert (await reachable(web, timeout=30))[0] is True
    assert time.monotonic() - started < 5


async def test_hanging_control_hosts_time_out_as_offline():
    web = FakeWeb()
    for url in URLS:
        web.add(httpx.URL(url).host, _hang)
    ok, why = await reachable(web, timeout=0.2)
    assert ok is False and why.count("no answer within 0.2 seconds") == 3


def test_default_control_hosts_are_unrelated_public_services(settings):
    urls = connectivity.control_urls(settings)
    assert urls[0] == settings.wp_version_api_url and len(urls) == 3
    assert all(u.startswith("https://") for u in urls)


def test_the_control_hosts_can_be_replaced(settings):
    """For a network that blocks the default hosts (and the test stack, which has no internet)."""
    custom = settings.model_copy(update={"monitor_control_urls": " http://wp-test/ , https://status.example/ping"})
    assert connectivity.control_urls(custom) == [settings.wp_version_api_url, "http://wp-test/", "https://status.example/ping"]


def test_only_a_home_page_that_never_answered_needs_the_control_check():
    dns = SiteReport(url="https://a.example", status="down")
    answered_500 = SiteReport(url="https://a.example", status="down", http_status=502)
    degraded = SiteReport(url="https://a.example", status="degraded")
    assert connectivity.is_network_failure(dns)
    assert not connectivity.is_network_failure(answered_500)  # the site's server answered: real downtime
    assert not connectivity.is_network_failure(degraded)


class CountingWeb(FakeWeb):
    def control_requests(self) -> int:
        return sum(1 for r in self.requests if r.url.host.startswith("control-"))


async def test_one_control_check_covers_the_failures_around_it(monkeypatch):
    web = CountingWeb()
    monkeypatch.setattr(connectivity, "control_urls", lambda settings: URLS)
    async with ReadOnlyHttpClient(Settings(), transport=web.transport) as http:
        guard = connectivity.ConnectivityGuard(http, Settings())
        now = time.monotonic()
        results = await asyncio.gather(*(guard.online(failed_at=now) for _ in range(5)))
        assert results == [False] * 5 and web.control_requests() == 3  # one round of three hosts
        assert guard.state == "offline" and "does not resolve" in guard.error
        # A failure seen well after that control check started gets a new one, which now succeeds.
        web.add("control-a.example", lambda r: httpx.Response(200))
        assert await guard.online(failed_at=time.monotonic() + connectivity.CONTROL_REUSE_SECONDS + 1) is True
        assert guard.state == "online"


async def test_the_latest_evidence_decides_the_sweeps_state(monkeypatch):
    monkeypatch.setattr(connectivity, "control_urls", lambda settings: URLS)
    async with ReadOnlyHttpClient(Settings(), transport=FakeWeb().transport) as http:
        guard = connectivity.ConnectivityGuard(http, Settings())
        assert guard.state is None  # nothing failed and nothing answered: no evidence either way
        guard.saw_answer()
        assert guard.state == "online"
        await asyncio.sleep(0.01)
        assert await guard.online(failed_at=time.monotonic()) is False  # no control host answers
        assert guard.state == "offline"
        await asyncio.sleep(0.01)
        guard.saw_answer()  # a site answered after the control check failed: the connection is back
        assert guard.state == "online"


def test_connectivity_view_without_and_with_a_record():
    assert connectivity.connectivity_view(None) == {"state": "unknown", "since": None, "last_ok_at": None, "last_error_at": None,
                                                    "last_error": ""}
    at = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
    row = IntegrationStatus(name="internet", last_error_at=at, last_error="all failed",
                            detail={"state": "offline", "since": at.isoformat()})
    view = connectivity.connectivity_view(row)
    assert view["state"] == "offline" and view["since"] == at.isoformat() and view["last_error"] == "all failed"


# ------------------------------------------------------------------ open issues


def check(minutes_ago: int, *, deep: bool, findings: list[dict], status: str = "up", partial: bool = False,
          compared: bool = False) -> SiteCheck:
    details = {"deep": deep, **({"partial": True} if partial else {}),
               **({"redirect_check": {"chain": [], "variants": {}}} if compared else {})}
    return SiteCheck(site_id=1, checked_at=datetime.now(UTC) - timedelta(minutes=minutes_ago), status=status,
                     findings=findings, details=details)


def f(code: str, severity: str = "warning", title: str = "") -> dict:
    return {"code": code, "severity": severity, "title": title or code, "detail": ""}


def test_a_newer_quick_check_keeps_the_deep_only_findings_of_the_night_scan():
    deep = check(13, deep=True, findings=[f("cloaked_spam", "critical"), f("xmlrpc_enabled"), f("slow_response"),
                                          f("readme_exposed", "info")])
    quick = check(0, deep=False, findings=[f("no_https"), f("missing_hsts", "info")])
    merged = open_issues.merge(quick, deep)
    assert [(x["code"], x["source"]) for x in merged["open_findings"]] == [
        ("cloaked_spam", "deep"), ("no_https", "quick"), ("xmlrpc_enabled", "deep")]  # critical first; no info
    # slow_response is re-checked every quick sweep: the newer quick check's answer counts.
    assert merged["open_findings_sources"] == {"quick": quick.checked_at.isoformat(), "deep": deep.checked_at.isoformat()}
    assert merged["open_findings"][0]["checked_at"] == deep.checked_at.isoformat()


def test_a_newer_deep_check_is_the_only_source():
    quick = check(30, deep=False, findings=[f("http_error", "critical")])
    deep = check(5, deep=True, findings=[f("user_enumeration")])
    merged = open_issues.merge(quick, deep)
    assert [x["code"] for x in merged["open_findings"]] == ["user_enumeration"]
    assert merged["open_findings_sources"] == {"quick": None, "deep": deep.checked_at.isoformat()}


def test_open_findings_with_one_kind_of_check_or_none():
    quick = check(1, deep=False, findings=[f("http_error", "critical")])
    assert [x["code"] for x in open_issues.merge(quick, None)["open_findings"]] == ["http_error"]
    assert open_issues.merge(None, None) == open_issues.NO_OPEN_FINDINGS


def test_a_cut_short_deep_scan_says_so_even_after_a_newer_quick_check():
    deep = check(20, deep=True, partial=True, findings=[f("check_timeout", "warning", "results are partial")])
    quick = check(1, deep=False, findings=[f("check_timeout", "warning", "quick timeout")])
    merged = open_issues.merge(quick, deep)
    assert [(x["title"], x["source"]) for x in merged["open_findings"]] == [("quick timeout", "quick"),
                                                                           ("results are partial", "deep")]


def test_a_deep_scan_cut_short_before_the_comparison_clears_nothing_only_a_deep_check_finds():
    full = check(1440, deep=True, compared=True, status="degraded",
                 findings=[f("cloaked_spam", "critical", "spam"), f("xmlrpc_enabled", "warning", "old xmlrpc"), f("slow_response")])
    cut = check(20, deep=True, partial=True, findings=[f("xmlrpc_enabled", "warning", "new xmlrpc"), f("check_timeout")])
    quick = check(1, deep=False, findings=[f("no_https")])
    assert open_issues.made_comparison(full) and not open_issues.made_comparison(cut) and not open_issues.made_comparison(None)
    merged = open_issues.merge(quick, cut, full)
    assert [(x["code"], x["title"], x["checked_at"]) for x in merged["open_findings"]] == [
        ("cloaked_spam", "spam", full.checked_at.isoformat()),  # from the last scan that compared
        ("no_https", "no_https", quick.checked_at.isoformat()),
        ("xmlrpc_enabled", "new xmlrpc", cut.checked_at.isoformat()),  # the newer scan's answer for a code it reported
        ("check_timeout", "check_timeout", cut.checked_at.isoformat()),
    ]
    assert merged["open_findings_sources"] == {"quick": quick.checked_at.isoformat(), "deep": cut.checked_at.isoformat()}
    # The cut-short scan newer than the quick check: still everything it saw, plus what only the full scan could find.
    assert [x["code"] for x in open_issues.merge(check(30, deep=False, findings=[]), cut, full)["open_findings"]] == [
        "cloaked_spam", "xmlrpc_enabled", "check_timeout"]
    # A deep check that compared is its own source: an older one adds nothing.
    clean = check(10, deep=True, compared=True, findings=[])
    assert open_issues.merge(quick, clean, full)["open_findings"][0]["code"] == "no_https"
    assert len(open_issues.merge(quick, clean, full)["open_findings"]) == 1


def test_a_quick_check_cannot_clear_cloaking_found_by_the_deep_check():
    deep = check(13, deep=True, status="degraded", findings=[f("conditional_redirect", "critical"), f("xmlrpc_enabled")])
    carried = open_issues.carried_causes(deep)
    assert [c["code"] for c in carried] == ["conditional_redirect"]
    assert open_issues.effective_status("up", carried) == "degraded"
    assert open_issues.effective_status("down", carried) == "down"
    assert open_issues.effective_status("up", open_issues.carried_causes(None)) == "up"
    # A warning-level deep finding does not change the status.
    assert open_issues.carried_causes(check(1, deep=True, findings=[f("cloaked_script", "warning")])) == []


# -------------------------------------------------------------- incident gaps


def test_unchecked_gap_follows_the_usual_interval():
    start = datetime(2026, 10, 19, tzinfo=UTC)
    every_15 = [start + timedelta(minutes=15 * i) for i in range(10)]
    assert unchecked_gap(every_15) == MIN_UNCHECKED_GAP
    hourly = [start + timedelta(hours=i) for i in range(10)]
    assert unchecked_gap(hourly) == timedelta(hours=3)
    assert unchecked_gap([start]) == MIN_UNCHECKED_GAP


# --------------------------------------------------------- credit repair mail


def test_letter_and_item_mail_modes():
    mock = store.CRLetter(id=1, bureau="experian", item_ids=[10, 11], status="mailed_mock")
    draft = store.CRLetter(id=2, bureau="equifax", item_ids=[12], status="draft")
    live = store.CRLetter(id=3, bureau="experian", item_ids=[11], status="mailed")
    assert [store.mail_mode(x) for x in (mock, draft, live)] == ["mock", None, "live"]
    assert store.item_mail_modes([live, draft, mock]) == {10: "mock", 11: "live"}  # the latest letter wins
    view = store.letter_view(mock)
    assert view["mail_mode"] == "mock" and view["mailed_at"] is None and "content_enc" not in view
