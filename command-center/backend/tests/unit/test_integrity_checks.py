"""Suspicious redirects, cloaking and injected content, all detected read-only."""

from __future__ import annotations

import httpx

from app.agents.monitoring import integrity
from app.agents.monitoring.integrity import same_site

from ..fakes import WP_HOME, FakeWeb, static_site, wordpress_site
from .test_wordpress_checks import ORIGIN, codes, run


def test_same_site_rules():
    assert same_site("https://acme.test", "https://www.acme.test/x")
    assert same_site("https://www.acme.test", "http://acme.test")
    assert same_site("https://acme.test", "https://shop.acme.test")
    assert not same_site("https://acme.test", "https://acme.test.evil.example")
    assert not same_site("https://acme.test", "https://notacme.test")
    assert not same_site("https://acme.test", "")


async def test_clean_site_has_no_integrity_findings():
    web = FakeWeb()
    web.add("acme.example.test", wordpress_site(ORIGIN))
    report = await run(web)
    assert not {"redirect_offsite", "conditional_redirect", "js_redirect_offsite", "hidden_iframe", "obfuscated_script",
                "hidden_spam", "cloaked_spam", "spam_terms"} & set(codes(report))
    assert report.status == "up"
    # Deep scans also look at the site as a search visitor and as a crawler, with GETs only.
    agents = {r.headers.get("user-agent", "") for r in web.requests}
    assert any("Googlebot" in a for a in agents) and any("iPhone" in a for a in agents)
    assert web.methods() == {"GET"}


async def test_redirect_to_another_domain_is_flagged_and_degrades():
    web = FakeWeb()
    web.add("acme.example.test", lambda r: httpx.Response(302, headers={"location": "https://cheap-pills.example.org/"}))
    web.add("cheap-pills.example.org", static_site("<html>buy cheap viagra</html>"))
    report = await run(web, deep=False)
    assert report.status == "degraded"
    assert codes(report)["redirect_offsite"] == "warning"
    assert report.details["redirect_check"]["offsite"] is True


async def test_redirect_to_an_unreachable_domain_says_so_without_extra_requests():
    web = FakeWeb()
    web.add("acme.example.test", lambda r: httpx.Response(302, headers={"location": "http://evil.invalid/landing"}))
    report = await run(web, deep=False)
    assert report.status == "down"
    found = codes(report)
    assert found["redirect_offsite"] == "critical" and "fetch_failed" not in found
    assert "evil.invalid" in next(f.title for f in report.findings if f.code == "redirect_offsite")


async def test_redirect_to_raw_ip_is_critical_and_www_is_fine():
    assert integrity.chain_findings("https://acme.test", ["https://acme.test", "https://www.acme.test/"]) == []
    found = integrity.chain_findings("https://acme.test", ["https://acme.test", "http://203.0.113.9/x"])
    assert {f.code: f.severity for f in found} == {"redirect_offsite": "critical", "redirect_downgrade": "warning"}


async def test_cloaked_redirect_for_search_visitors_is_caught_without_visiting_the_target():
    def cloaked(request: httpx.Request) -> httpx.Response:
        if "google.com" in request.headers.get("referer", ""):
            return httpx.Response(302, headers={"location": "https://scam.example.net/win"})
        return wordpress_site(ORIGIN)(request)

    web = FakeWeb()
    web.add("acme.example.test", cloaked)
    report = await run(web)
    found = codes(report)
    assert found["conditional_redirect"] == "critical"
    assert "redirect_offsite" not in found  # ordinary visitors are not redirected
    assert report.status == "degraded"
    assert "scam.example.net" not in {r.url.host for r in web.requests}


async def test_spam_shown_only_to_crawlers_is_caught():
    spammy = WP_HOME.replace("<h1>Acme Plumbing</h1>", "<h1>Acme Plumbing</h1><a href='https://x.example'>cheap cialis online pharmacy</a>")

    def cloaked(request: httpx.Request) -> httpx.Response:
        if "Googlebot" in request.headers.get("user-agent", "") and request.url.path in ("", "/"):
            return httpx.Response(200, text=spammy.replace("{origin}", ORIGIN), headers={"content-type": "text/html"})
        return wordpress_site(ORIGIN)(request)

    web = FakeWeb()
    web.add("acme.example.test", cloaked)
    found = codes(await run(web))
    assert found["cloaked_spam"] == "critical"
    assert "spam_terms" not in found


async def test_injected_content_on_the_home_page():
    page = WP_HOME.replace("</body>", """
      <script>eval(atob('ZG9jdW1lbnQ='))</script>
      <script>window.location.href = "https://redirect-scam.example/?r=1";</script>
      <iframe src="https://malware.example/x" width="0" height="0" frameborder="0"></iframe>
      <div style="display:none">best online casino and payday loan offers</div>
      <script src="https://cdn.evil-scripts.example/a.js"></script>
    </body>""")
    web = FakeWeb()
    web.add("acme.example.test", wordpress_site(ORIGIN, home_html=page))
    report = await run(web, deep=False)
    found = codes(report)
    assert found["js_redirect_offsite"] == "critical"
    assert found["hidden_iframe"] == "critical"
    assert found["obfuscated_script"] == "warning"
    assert found["hidden_spam"] == "critical"
    assert report.status == "degraded"
    assert "cdn.evil-scripts.example" in report.details["external_scripts"]
    assert {"redirect-scam.example", "malware.example"}.isdisjoint({r.url.host for r in web.requests})


async def test_meta_refresh_to_another_domain():
    page = '<html><head><meta http-equiv="refresh" content="0; url=https://elsewhere.example/"></head><body>Hi</body></html>'
    web = FakeWeb()
    web.add("acme.example.test", static_site(page))
    assert codes(await run(web, deep=False))["meta_refresh_offsite"] == "critical"


async def test_plain_http_must_redirect_to_https():
    def https_site(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "http":
            return httpx.Response(200, text="<html>plain</html>")  # serves content over http too
        return wordpress_site("https://acme.example.test")(request)

    web = FakeWeb()
    web.add("acme.example.test", https_site)
    report = await run(web, url="https://acme.example.test")
    assert codes(report)["no_https_redirect"] == "warning"
