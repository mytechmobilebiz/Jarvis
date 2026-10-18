"""Suspicious redirects, cloaking and injected content, all detected read-only."""

from __future__ import annotations

import time

import httpx
import pytest

from app.agents.monitoring import checks, integrity
from app.agents.monitoring.checks import TLSInfo
from app.agents.monitoring.integrity import Trace, same_site

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
    assert any("Googlebot" in a for a in agents) and any("iPhone" in a for a in agents) and any("Windows NT" in a for a in agents)
    assert web.methods() == {"GET"}


async def test_redirect_to_another_domain_is_flagged_and_degrades():
    web = FakeWeb()
    web.add("acme.example.test", lambda r: httpx.Response(302, headers={"location": "https://cheap-pills.example.org/"}))
    web.add("cheap-pills.example.org", static_site("<html>buy cheap viagra</html>"))
    report = await run(web, deep=False)
    assert report.status == "degraded"
    assert codes(report)["redirect_offsite"] == "warning"
    assert report.details["redirect_check"]["offsite"] is True
    assert report.details["redirect_check"]["chain"] == [ORIGIN, "https://cheap-pills.example.org/"]
    assert "cheap-pills.example.org" not in {r.url.host for r in web.requests}  # recorded, never visited


async def test_redirect_to_an_unreachable_domain_is_flagged_without_visiting_it():
    web = FakeWeb()
    web.add("acme.example.test", lambda r: httpx.Response(302, headers={"location": "http://evil.invalid/landing"}))
    report = await run(web, deep=False)
    assert report.status == "degraded"
    found = codes(report)
    assert found["redirect_offsite"] == "warning" and "fetch_failed" not in found
    assert "evil.invalid" in next(f.title for f in report.findings if f.code == "redirect_offsite")
    assert report.final_url == "http://evil.invalid/landing"
    assert "evil.invalid" not in {r.url.host for r in web.requests}


async def test_probes_and_tls_never_follow_or_reach_another_domain(monkeypatch):
    probed: list[str] = []

    async def fake_probe_tls(host, port, timeout, context=None, allowed_private=frozenset()):
        probed.append(host)
        return TLSInfo(days_left=90, issuer="Test CA")

    monkeypatch.setattr(checks, "probe_tls", fake_probe_tls)
    site = wordpress_site("https://acme.example.test", headers={"link": '<https://rest.elsewhere.example/wp-json/>; rel="https://api.w.org/"'})

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/xmlrpc.php", "/readme.html"):
            return httpx.Response(302, headers={"location": "https://elsewhere.example/landing"})
        return site(request)

    web = FakeWeb()
    web.add("acme.example.test", handle)
    web.add("elsewhere.example", static_site("<html>XML-RPC server accepts POST requests only. WordPress</html>"))
    web.add("rest.elsewhere.example", lambda r: httpx.Response(200, json={"namespaces": ["wc/v3"]}))
    found = codes(await run(web, url="https://acme.example.test"))
    assert {"xmlrpc_enabled", "readme_exposed"}.isdisjoint(found)
    assert {"elsewhere.example", "rest.elsewhere.example"}.isdisjoint({r.url.host for r in web.requests})
    assert "/wp-json/" in web.paths("acme.example.test")  # the page's REST link pointed elsewhere: the site's own is used
    assert probed == ["acme.example.test"]

    # An https site that sends visitors to another https site: the other site's server is never probed.
    probed.clear()
    redirecting = FakeWeb()
    redirecting.add("acme.example.test", lambda r: httpx.Response(301, headers={"location": "https://elsewhere.example/"}))
    report = await run(redirecting, url="https://acme.example.test", deep=False)
    assert codes(report)["redirect_offsite"] == "warning"
    assert probed == ["acme.example.test"]
    assert "elsewhere.example" not in {r.url.host for r in redirecting.requests}


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


# Inputs built to make backtracking regexes run for minutes (each repeated to ~200 KB).
HOSTILE_MARKUP = [
    "<p display:none ", "<div style='display:none'>", "<p ", "<meta http-equiv=refresh ",
    "<meta http-equiv=refresh content='0;url=", '<meta name="generator" ', "<script src='x' ", "<script>",
    "<iframe src='//x.example' width=0 ", "<link rel='https://api.w.org/' ", "window.location.href='//",
    " onclick='", "<noscript>", "String.fromCharCode(11,", "window. ", "top . ", "<script ", "<a onclick='x' ",
    '<a title="', "<a>", "<!--<noscript>", "<p style='display:none'>a</p>", '<span style="display:none"></span>',
    "<!--!>", "<script><!--<script>",
]


@pytest.mark.parametrize("chunk", HOSTILE_MARKUP)
def test_page_analysis_stays_fast_on_hostile_markup(chunk):
    page = chunk * (200_000 // len(chunk))
    site, url = "https://acme.test", "https://acme.test/"
    for analyse in (
        lambda: integrity.html_findings(site, url, page),
        lambda: integrity.external_script_hosts(site, url, page),
        lambda: integrity.cloaking_findings(site, Trace(final_url=url, html=page), {"as a crawler": Trace(final_url=url, html=page)}),
        lambda: checks.page_markers(page, page[:8000]),
    ):
        started = time.perf_counter()
        analyse()
        assert time.perf_counter() - started < 1.0


# Whole pages up to the 600 KB body limit (monitor_max_body_bytes). Each is tried at
# 40 KB first, where a quadratic pattern already takes seconds, so a regression fails fast.
HOSTILE_PAGES = {
    "chained location prefixes": lambda size: "window. " * (size // 8),
    "spaced location prefixes": lambda size: "top . " * (size // 6),
    "script openers in one tag": lambda size: "<script " * (size // 8) + ">",
    "script sources in one tag": lambda size: "<script src='x' " * (size // 16) + ">",
    "hidden blocks with one close": lambda size: "<p style='display:none'>" * (size // 24) + "</p>",
    "tags inside an open attribute": lambda size: '<a title="' + "<b onclick='x'>" * (size // 15),
    "spaces after location": lambda size: "location" + " " * size,
    "spaced assignments": lambda size: ("location.href=" + " " * 40) * (size // 54),
    "hosts holding redirects": lambda size: "location='//[" * (size // 13),
    "click handlers holding redirects": lambda size: '<a onclick="location=\'//x' * (size // 25),
    "comment-wrapped script openers": lambda size: "<script><!--" + "<script></script>" * (size // 17),
}


@pytest.mark.parametrize("build", HOSTILE_PAGES.values(), ids=HOSTILE_PAGES.keys())
def test_page_analysis_is_linear_up_to_the_body_limit(build):
    site, url = "https://acme.test", "https://acme.test/"
    for size, limit in ((40_000, 0.5), (600_000, 2.0)):
        page = build(size)
        for analyse, args in (
            (integrity.html_findings, (site, url, page)),
            (integrity.external_script_hosts, (site, url, page)),
            (checks.page_markers, (page, "")),
        ):
            started = time.perf_counter()
            analyse(*args)
            assert time.perf_counter() - started < limit, (size, analyse.__name__)


async def test_malformed_urls_do_not_break_the_check():
    page = """<html><head><script src="//["></script><script src="http://[x/a.js"></script>
      <meta http-equiv="refresh" content="0;url=http://[x"><link rel="https://api.w.org/" href="http://[x" />
      </head><body><iframe src="//[" width="0" height="0"></iframe><script>location.href="//[x";</script></body></html>"""
    assert integrity.external_script_hosts("https://acme.test", "https://acme.test/", page) == []
    assert integrity.html_findings("https://acme.test", "https://acme.test/", page) == []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "http" or "Googlebot" in request.headers.get("user-agent", ""):
            return httpx.Response(301, headers={"location": "http://[x"})
        if request.url.path == "/feed/":
            return httpx.Response(302, headers={"location": "//["})
        return wordpress_site("https://acme.example.test", home_html=page)(request)

    web = FakeWeb()
    web.add("acme.example.test", handle)
    report = await run(web, url="https://acme.example.test")
    assert report.status == "up" and report.is_wordpress
    assert next(f for f in report.findings if f.code == "no_https_redirect").detail == "redirects to an invalid address"


@pytest.mark.parametrize("location", [
    "/\\evil.example/", "https://evil.example\\@acme.example.test/", "\\\\evil.example", "///evil.example",
    "https:/evil.example/", " //evil.example",
])
async def test_location_read_as_a_browser_reads_it(location):
    web = FakeWeb()
    web.add("acme.example.test", lambda r: httpx.Response(302, headers={"location": location}))
    report = await run(web, deep=False)
    assert codes(report)["redirect_offsite"] == "warning"
    assert "evil.example" in next(f.title for f in report.findings if f.code == "redirect_offsite")
    assert "evil.example" not in {r.url.host for r in web.requests}


@pytest.mark.parametrize("location", ["ftp://evil.example/", "https://[", "http://localhost:99999/", "intent://x#Intent;scheme=x;end", ""])
async def test_redirect_to_an_invalid_or_non_web_address_is_a_failure(location):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("", "/"):
            return httpx.Response(301, headers={"location": "/home/"})
        return httpx.Response(302, headers={"location": location}, text="<html>Moved</html>")

    web = FakeWeb()
    web.add("acme.example.test", handle)
    report = await run(web, deep=False)
    assert report.status == "down" and set(codes(report)) == {"fetch_failed"}
    assert "invalid or non-web address" in report.findings[0].title
    assert report.findings[0].title.endswith(f"after redirecting to {ORIGIN}/home/")
    assert web.paths("acme.example.test") == ["/", "/home/"] * 2  # one retry, nothing else requested


async def test_app_link_redirect_shown_only_to_phones_is_a_conditional_redirect():
    def handle(request: httpx.Request) -> httpx.Response:
        if "iPhone" in request.headers.get("user-agent", "") and request.url.path in ("", "/"):
            return httpx.Response(302, headers={"location": "intent://evil.example/#Intent;scheme=https;package=com.android.chrome;end"})
        if "Googlebot" in request.headers.get("user-agent", "") and request.url.path in ("", "/"):
            return httpx.Response(302, headers={"location": "intent://www.acme.example.test/#Intent;package=com.acme;end"})
        return wordpress_site(ORIGIN)(request)

    web = FakeWeb()
    web.add("acme.example.test", handle)
    report = await run(web)
    redirects = [f for f in report.findings if f.code == "conditional_redirect"]
    assert [(f.severity, f.title) for f in redirects] == [
        ("critical", "Visitors arriving from Google on a phone are redirected to another domain: evil.example")]
    assert report.status == "degraded"
    variants = report.details["redirect_check"]["variants"]
    assert variants["arriving from Google on a phone"]["offsite_target"].startswith("intent://evil.example/")
    assert "invalid or non-web address" in variants["identifying as Googlebot"]["error"]  # the site's own app: no finding
    assert "evil.example" not in {r.url.host for r in web.requests}


def test_resolve_follows_browser_rules():
    assert integrity.resolve("https://acme.test/a/", "https:x") == "https://acme.test/a/x"
    assert integrity.resolve("https://acme.test/a/", "/\\evil.test/x") == "https://evil.test/x"
    assert integrity.resolve("https://acme.test/", "/\t/evil.test/") == "https://evil.test/"
    assert integrity.resolve("https://acme.test/", "//[") == ""
    assert integrity.resolve("https://acme.test/", "javascript:alert(1)") == ""


async def test_site_registered_by_its_unicode_name_is_not_offsite():
    assert same_site("https://bücher.example", "https://xn--bcher-kva.example/")
    assert same_site("https://www.BÜCHER.example", "http://shop.xn--bcher-kva.example")
    assert not same_site("https://bücher.example", "https://xn--bcher-kva.example.evil.test")
    web = FakeWeb()
    web.add("bücher.example", static_site())  # httpx reports the host decoded, and the final URL in punycode
    report = await run(web, "http://bücher.example", deep=False)
    assert report.status == "up" and "redirect_offsite" not in codes(report)


def test_tag_manager_snippet_and_visible_embeds_are_not_hidden_iframes():
    page = """<body><noscript><iframe src="https://www.googletagmanager.com/ns.html?id=GTM-X" height="0" width="0"
      style="display:none;visibility:hidden"></iframe></noscript>
      <iframe src="https://www.youtube.com/embed/x" width="560" height="315" marginheight="0" marginwidth="0"></iframe>
      <noscript><iframe src="https://pixel.example/t" width="1" height="1"></iframe></noscript></body>"""
    found = {f.code: f.severity for f in integrity.html_findings("https://acme.test", "https://acme.test/", page)}
    assert found == {"hidden_iframe_noscript": "info"}


def test_navigation_links_are_not_script_redirects():
    page = """<button onclick="window.location.href='https://calendly.com/acme'">Book</button>
      <a href="#" ONCLICK=location='https://booking.example/acme'>Book</a>
      <form onsubmit="if (a > b) location.assign('https://pay.example/'); return false"></form>
      <div data-location="https://maps.example/acme"></div>
      <script>if (q) window.location.href = 'https://acme.test?s=' + q; location.assign("https://acme.test:443/shop");</script>"""
    assert integrity.html_findings("https://acme.test", "https://acme.test/", page) == []
    for script in ('window.top.location.href = "https://evil.example/"', 'document.location="//evil.example:8443/x"',
                   # Formatted code: any run of spaces after "location", "=" and "replace(".
                   '\n      window.location.href =\n        "https://evil.example/?utm_source=organic&campaign=1234567890";',
                   'window.location.replace(\n        "https://evil.example/"\n      );',
                   "location.href =\r\n        'https://evil.example/'", "location          = 'https://evil.example/'"):
        [finding] = integrity.html_findings("https://acme.test", "https://acme.test/", f"<script>{script}</script>")
        assert finding.code == "js_redirect_offsite" and finding.title.endswith("evil.example")


@pytest.mark.parametrize("page", [
    """<img src=x onerror="window.location='https://evil.example/'">""",
    """<body onload="location.href='https://evil.example/'"><p>Hi</p></body>""",
    """<svg onload="top.location.replace('https://evil.example/')"></svg>""",
    # Text that only looks like a click handler must not hide a redirect next to it.
    """<i title=' onclick="'></i><script>window.location="https://evil.example/"</script>""",
    """<img title='onclick=' onerror="location='//evil.example'" src=x>""",
    """<img alt="a> <b onclick='" onerror="location='//evil.example'" src=x>""",
    """<script>var a = ' onclick="'; window.location = "https://evil.example/"; var b = '"';</script>""",
    # A handler, or an address that is no host, running on into a real redirect does not hide it.
    """<b onclick="location="//x><script>location='https://evil.example/'</script>""",
    """<p>location="//[<script>location='https://evil.example/'</script>""",
    # One script to a browser: "<!--<script>" keeps the first "</script" from ending it, so this is no tag.
    """<script><!--<script></script><b onclick="\nlocation='https://evil.example/';\n//"></b></script>""",
])
def test_handlers_that_run_by_themselves_are_redirects(page):
    [finding] = integrity.html_findings("https://acme.test", "https://acme.test/", page)
    assert (finding.code, finding.severity) == ("js_redirect_offsite", "critical") and finding.title.endswith("evil.example")


@pytest.mark.parametrize("before,after", [
    ("<!-- <noscript> -->", ""),
    ('<div title="<noscript>"></div>', ""),
    ('<script>var s = "<noscript>";</script>', "<noscript><img src='/pixel.gif'></noscript>"),
    ("<noscript>", ""),  # never closed
    ("<!--<noscript>-->", "<!--</noscript>-->"),
    ("<script><!--<script></script><noscript>\ndocument.write('", "');\n//</noscript></script>"),  # no noscript: one script
])
def test_only_a_real_closed_noscript_makes_a_hidden_iframe_harmless(before, after):
    page = f'<body>{before}<iframe src="https://evil.example/x" width="0" height="0"></iframe>{after}</body>'
    found = {f.code: f.severity for f in integrity.html_findings("https://acme.test", "https://acme.test/", page)}
    assert found == {"hidden_iframe": "critical"}


@pytest.mark.parametrize("script", [
    "<script><!-- <script> --> </script>",  # "-->" leaves both escaped states
    "<script><!--<script></script> var a; --></script>",
    "<script><!--><script></script>",  # "<!-->" opens and ends the escape at once
    "<script><!--\ndocument.write(\"<script src='/a.js'></script>\");\n//--></script>",  # legacy themes
])
def test_comment_wrapped_scripts_end_where_a_browser_ends_them(script):
    page = f"""{script}<button onclick="location.href='https://booking.example/'">Book</button>"""
    assert integrity.html_findings("https://acme.test", "https://acme.test/", page) == []


def test_script_text_in_a_comment_does_not_hide_visible_spam():
    page = "<!-- <script --><p>cheap viagra here</p><script>var spam = 'online casino';</script>"
    found = {f.code: f.title for f in integrity.html_findings("https://acme.test", "https://acme.test/", page)}
    assert found == {"spam_terms": "Spam-like terms on the page: viagra"}


def test_a_comment_ends_where_a_browser_ends_it():
    site, url = "https://acme.test", "https://acme.test/"
    for comment in ("<!-- x --!>", "<!----!>", "<!-->", "<!--->"):
        found = {f.code: f.title for f in integrity.html_findings(site, url, comment + "<p>cheap viagra here</p><!-- -->")}
        assert found == {"spam_terms": "Spam-like terms on the page: viagra"}, comment
    button = """<button onclick="location.href='https://booking.example/'">Book</button>"""
    assert integrity.html_findings(site, url, f"<!-- x --!>{button}<!-- -->") == []
    assert integrity.html_findings(site, url, "<!--!><p>cheap viagra here</p>-->") == []  # the opener's dashes do not count


def test_long_or_padded_tags_are_still_examined():
    site, url, pad = "https://acme.test", "https://acme.test/", "x" * 5000
    script = f'<script data-v="{pad}" src="https://evil.example/x.js"></script>'
    assert integrity.external_script_hosts(site, url, script) == ["evil.example"]
    for page, code in (
        (f'<iframe data-v="{pad}" src="https://evil.example/" width="0" height="0"></iframe>', "hidden_iframe"),
        (f'<iframe src="https://evil.example/{pad}" width="0" height="0"></iframe>', "hidden_iframe"),
        (f'<meta data-v="{pad}" http-equiv="refresh" content="0; url=https://evil.example/">', "meta_refresh_offsite"),
        (f'<div data-v="{pad}" style="display:none">cheap viagra</div>', "hidden_spam"),
    ):
        assert [f.code for f in integrity.html_findings(site, url, page)] == [code]


def test_hidden_spam_counts_however_many_hidden_blocks_come_first():
    padding = '<span style="display:none"></span>' * 600
    nested = '<div style="display:none"><p style="display:none">payday loans</p> and online casinos</div>'
    for page in (padding + '<div style="display:none">cheap viagra online pharmacy</div>', padding + nested):
        found = {f.code: f.severity for f in integrity.html_findings("https://acme.test", "https://acme.test/", page)}
        assert found == {"hidden_spam": "critical"}


def test_spam_terms_match_whole_words_and_their_plurals():
    assert integrity.spam_terms("Ask our Specialist about Sporny cialisation") == []
    assert integrity.spam_terms("A socialist commercialises the casino onlineshop") == []
    assert integrity.spam_terms("Buy CIALIS and visit our online casino.") == ["cialis", "online casino"]
    assert integrity.spam_terms("Payday loans, online casinos, free porno on pornhub, cheapviagra from online pharmacies") == [
        "online casino", "online pharmacy", "payday loan", "porn", "viagra"]
    [finding] = integrity.chain_findings("https://acme.test", ["https://acme.test", "https://cheapviagra.example/"])
    assert (finding.code, finding.severity) == ("redirect_offsite", "critical")


async def test_content_injected_only_for_crawlers_is_cloaking():
    injected = WP_HOME.replace("</body>", '<script>window.location.replace("https://scam.example/")</script></body>')

    def cloaked(request: httpx.Request) -> httpx.Response:
        if "Googlebot" in request.headers.get("user-agent", "") and request.url.path in ("", "/"):
            return httpx.Response(200, text=injected.replace("{origin}", ORIGIN), headers={"content-type": "text/html"})
        return wordpress_site(ORIGIN)(request)

    web = FakeWeb()
    web.add("acme.example.test", cloaked)
    report = await run(web)
    found = codes(report)
    assert found["cloaked_injection"] == "critical" and "js_redirect_offsite" not in found
    assert "scam.example" in next(f.title for f in report.findings if f.code == "cloaked_injection")
    assert report.status == "degraded"
    assert "scam.example" not in {r.url.host for r in web.requests}


async def test_coming_soon_counts_only_in_the_page_heading():
    filler = "<p>" + "Plumbing, heating and drains for the whole county. " * 80 + "</p>"
    footer = f"<html><head><title>Acme Plumbing</title></head><body><h1>Acme</h1>{filler}<p>New blog posts coming soon!</p></body></html>"
    banner = f"<html><head><title>Coming Soon - Acme</title></head><body>{filler}</body></html>"
    web = FakeWeb()
    web.add("footer.example.test", static_site(footer))
    web.add("banner.example.test", static_site(banner))
    footer_report = await run(web, "http://footer.example.test", deep=False)
    assert footer_report.status == "up" and "page_notice" not in codes(footer_report)
    banner_report = await run(web, "http://banner.example.test", deep=False)
    assert banner_report.status == "degraded" and codes(banner_report)["page_notice"] == "warning"


def _home_by_visitor(home_for: dict[str, str]):
    """The WordPress fake site, with a different home page for user agents containing a key of home_for."""
    def handle(request: httpx.Request) -> httpx.Response:
        agent = request.headers.get("user-agent", "")
        html = next((page for key, page in home_for.items() if key in agent), WP_HOME)
        if request.url.path in ("", "/"):
            return httpx.Response(200, text=html.replace("{origin}", ORIGIN), headers={"content-type": "text/html"})
        return wordpress_site(ORIGIN)(request)

    return handle


async def test_scripts_served_only_to_some_visitors_are_cloaking():
    web = FakeWeb()
    web.add("acme.example.test", _home_by_visitor({
        "iPhone": WP_HOME.replace("</head>", "<script src='https://cdn.evil.example/r.js'></script></head>"),
        "Googlebot": WP_HOME.replace("</body>", "<script>eval(atob('ZG9jdW1lbnQ='))</script></body>"),
    }))
    report = await run(web)
    found = {f.code: f for f in report.findings}
    assert found["cloaked_script"].severity == "warning"
    assert found["cloaked_script"].title == "Script domain(s) loaded only for visitors arriving from Google on a phone: cdn.evil.example"
    assert found["cloaked_injection"].severity == "warning" and "Googlebot" in found["cloaked_injection"].title
    assert "Obfuscated JavaScript" in found["cloaked_injection"].title and "obfuscated_script" not in found
    assert "cdn.evil.example" in report.details["external_scripts"]  # so new_external_script tracks it too
    assert report.status == "up"  # warnings only: ad and analytics scripts are often left out for bots
    assert "cdn.evil.example" not in {r.url.host for r in web.requests}


async def test_redirect_shown_only_to_desktop_search_visitors_is_caught():
    injected = WP_HOME.replace("</body>", "<script>window.location.href='https://evil.example/'</script></body>")
    web = FakeWeb()
    web.add("acme.example.test", _home_by_visitor({"Windows NT": injected}))
    report = await run(web)
    found = {f.code: f for f in report.findings}
    assert found["cloaked_injection"].severity == "critical" and "desktop browser" in found["cloaked_injection"].title
    assert "evil.example" in found["cloaked_injection"].title and "js_redirect_offsite" not in found
    assert report.status == "degraded"
    assert "evil.example" not in {r.url.host for r in web.requests}
