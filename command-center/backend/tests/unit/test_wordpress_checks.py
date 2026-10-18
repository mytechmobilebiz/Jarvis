from __future__ import annotations

import httpx
import pytest

from app.agents.http import ReadOnlyHttpClient
from app.agents.monitoring.agent import normalize_url
from app.agents.monitoring.checks import SiteChecker, page_markers, parse_version
from app.config import Settings

from ..fakes import FakeWeb, static_site, wordpress_site

ORIGIN = "http://acme.example.test"


async def run(web: FakeWeb, url: str = ORIGIN, deep: bool = True, latest: str | None = "6.8.3", **settings):
    s = Settings(monitor_retry_delay_seconds=0, **settings)
    async with ReadOnlyHttpClient(s, transport=web.transport) as http:
        return await SiteChecker(http, s, latest_wp_version=latest, retry_delay=0).check(url, deep=deep)


def codes(report):
    return {f.code: f.severity for f in report.findings}


async def test_wordpress_site_with_exposures():
    web = FakeWeb()
    web.add("acme.example.test", wordpress_site(ORIGIN))
    report = await run(web)
    assert report.status == "up"
    assert report.is_wordpress
    assert report.wp_version == "6.4.1"
    found = codes(report)
    assert found["wp_outdated"] == "warning"
    assert found["xmlrpc_enabled"] == "warning"
    assert found["user_enumeration"] == "warning"
    assert found["debug_log_exposed"] == "critical"
    assert found["directory_listing"] == "warning"
    assert found["readme_exposed"] == "info"
    assert found["no_https"] == "warning"
    assert report.details["rest_users_exposed"] == 2
    assert "Yoast SEO" in report.details["plugins_detected"]
    assert "WooCommerce" in report.details["plugins_detected"]
    assert "contact-form-7" in report.details["plugins_detected"]
    assert report.details["themes_detected"] == ["astra"]
    # usernames are never stored, only the count
    assert "admin" not in str(report.details)
    # strictly read-only
    assert web.methods() == {"GET"}


async def test_hardened_up_to_date_site_has_no_warnings():
    web = FakeWeb()
    web.add("acme.example.test", wordpress_site(ORIGIN, version="6.8.3", exposed=False))
    report = await run(web)
    assert report.status == "up"
    assert {c for c, s in codes(report).items() if s != "info"} == {"no_https"}


async def test_major_version_behind_is_critical():
    web = FakeWeb()
    web.add("acme.example.test", wordpress_site(ORIGIN, version="5.9.2"))
    report = await run(web)
    assert codes(report)["wp_outdated"] == "critical"


async def test_shallow_check_skips_exposure_probes():
    web = FakeWeb()
    web.add("acme.example.test", wordpress_site(ORIGIN))
    report = await run(web, deep=False)
    assert report.is_wordpress
    paths = web.paths("acme.example.test")
    assert "/xmlrpc.php" not in paths and "/wp-content/debug.log" not in paths
    assert "xmlrpc_enabled" not in codes(report)


async def test_static_site_is_not_wordpress():
    web = FakeWeb()
    web.add("plain.example.test", static_site())
    report = await run(web, "http://plain.example.test")
    assert report.status == "up"
    assert not report.is_wordpress


@pytest.mark.parametrize(
    "body,status,expected,code",
    [
        ("<h1>Error establishing a database connection</h1>", 500, "down", "page_problem"),
        ("<p>There has been a critical error on this website.</p>", 200, "down", "page_problem"),
        ("<p>This Account has been suspended.</p>", 200, "down", "page_problem"),
        ("oops", 502, "down", "http_error"),
        ("missing", 404, "degraded", "http_error"),
        ("<p>Briefly unavailable for scheduled maintenance. Check back in a minute.</p>", 503, "degraded", "page_notice"),
    ],
)
async def test_availability_problems(body, status, expected, code):
    web = FakeWeb()
    web.add("site.example.test", static_site(body, status=status))
    report = await run(web, "http://site.example.test")
    assert report.status == expected
    assert code in codes(report)


async def test_bot_protection_is_not_an_outage():
    web = FakeWeb()
    web.add("cf.example.test", static_site("<title>Just a moment...</title> cloudflare", status=403))
    report = await run(web, "http://cf.example.test")
    assert report.status == "up"
    assert "bot_protection" in codes(report)


async def test_unreachable_site_is_down_after_one_retry():
    web = FakeWeb()
    report = await run(web, "http://nowhere.example.test")
    assert report.status == "down"
    assert codes(report) == {"fetch_failed": "critical"}
    assert "does not resolve" in report.findings[0].title
    assert len([r for r in web.requests if r.url.host == "nowhere.example.test"]) == 2


async def test_timeout_is_reported():
    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    web = FakeWeb()
    web.add("slow.example.test", slow)
    report = await run(web, "http://slow.example.test")
    assert report.status == "down"
    assert "no response within" in report.findings[0].title


async def test_slow_response_degrades(monkeypatch):
    web = FakeWeb()
    web.add("site.example.test", static_site())
    report = await run(web, "http://site.example.test", monitor_slow_ms=-1)
    assert report.status == "degraded"
    assert "slow_response" in codes(report)


async def test_end_of_life_php_header():
    web = FakeWeb()
    web.add("site.example.test", static_site(headers={"x-powered-by": "PHP/7.4.33", "server": "Apache/2.4.41 (Ubuntu)"}))
    report = await run(web, "http://site.example.test")
    found = codes(report)
    assert found["php_eol"] == "warning"
    assert found["server_version_disclosed"] == "info"


async def test_internal_target_is_refused_not_fetched():
    web = FakeWeb()
    report = await run(web, "http://127.0.0.1:5432")
    assert report.status == "down"
    assert "refused to check" in report.findings[0].title
    assert web.requests == []


def test_normalize_url():
    assert normalize_url("Example.COM/") == "https://example.com"
    assert normalize_url("http://example.com:8080/blog/") == "http://example.com:8080/blog"
    with pytest.raises(ValueError):
        normalize_url("https://")


def test_parse_version():
    assert parse_version("6.4.1") == (6, 4, 1)
    assert parse_version("6.8") == (6, 8)


def test_page_markers_read_long_tags():
    pad = "x" * 5000
    page = (f'<meta data-v="{pad}" name="generator" content="WordPress 6.4.1" />'
            f'<link data-v="{pad}" rel="https://api.w.org/" href="https://acme.test/wp-json/" />')
    markers = page_markers(page, "")
    assert markers.version == "6.4.1" and markers.rest_url == "https://acme.test/wp-json/"
    assert markers.markers == ["generator", "rest-link"]
