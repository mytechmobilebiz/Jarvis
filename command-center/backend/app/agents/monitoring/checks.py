"""Read-only health and exposure checks for one website.

Every request here is a GET through ReadOnlyHttpClient; nothing logs in,
submits a form or changes the site. Bodies of sensitive URLs (debug.log) are
inspected in memory to decide whether they are exposed and are never stored.
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

import httpx

from ...config import Settings
from ..base import Finding
from ..http import BlockedTarget, FetchResult, ReadOnlyHttpClient, open_public_connection
from . import integrity
from .integrity import Hop, Trace

Status = Literal["up", "degraded", "down"]

# Words on an otherwise "working" page that mean the site is not really up
# (carried over from agents/jarvis/scripts/check_websites.py).
PAGE_PROBLEMS = [
    ("account suspended", "shows an Account Suspended page from the web host"),
    ("account has been suspended", "shows an Account Suspended page from the web host"),
    ("error establishing a database connection", "shows a WordPress database connection error"),
    ("there has been a critical error on this website", "shows a WordPress critical error"),
    ("this domain has expired", "shows a domain-expired page"),
    ("domain is for sale", "shows a domain-for-sale page"),
    ("buy this domain", "shows a domain-for-sale page"),
]
PAGE_NOTES = [
    ("briefly unavailable for scheduled maintenance", "is in WordPress maintenance mode"),
    ("coming soon", "shows a Coming Soon page"),
    ("under construction", "shows an Under Construction page"),
    ("maintenance mode", "is in maintenance mode"),
]
BOT_WALL = ("just a moment", "cloudflare", "captcha", "attention required")

KNOWN_NAMESPACES = {
    "wc/v3": "WooCommerce",
    "wc/store": "WooCommerce",
    "wc/store/v1": "WooCommerce",
    "yoast/v1": "Yoast SEO",
    "rankmath/v1": "Rank Math SEO",
    "aioseo/v1": "All in One SEO",
    "contact-form-7/v1": "Contact Form 7",
    "elementor/v1": "Elementor",
    "wordfence/v1": "Wordfence",
    "jetpack/v4": "Jetpack",
    "gf/v2": "Gravity Forms",
    "litespeed/v1": "LiteSpeed Cache",
    "redirection/v1": "Redirection",
    "akismet/v1": "Akismet",
    "ithemes-security/v1": "Solid Security",
    "wpforms/v1": "WPForms",
    "updraftplus/v1": "UpdraftPlus",
}
# Matched on the attributes of one tag (integrity.META_RE, LINK_RE): see integrity.py on backtracking.
GENERATOR_NAME_RE = re.compile(r"(?<![\w-])name\s*=\s*[\"']generator[\"']", re.I)
GENERATOR_CONTENT_RE = re.compile(r"(?<![\w-])content\s*=\s*[\"']WordPress\s*([\d.]+)?", re.I)
FEED_GENERATOR_RE = re.compile(r"<generator>\s*https?://wordpress\.org/\?v=([\d.]+)\s*</generator>", re.I)
LINK_RE = re.compile(r"<link\b([^>]{0,1000}+)>", re.I)
REST_REL_RE = re.compile(r"(?<![\w-])rel\s*=\s*[\"']https://api\.w\.org/[\"']", re.I)
HREF_RE = re.compile(r"(?<![\w-])href\s*=\s*[\"']([^\"']+)", re.I)
REST_LINK_HEADER_RE = re.compile(r"<([^>]{0,2000}+)>;\s*rel=\"https://api\.w\.org/\"")
PLUGIN_RE = re.compile(r"/wp-content/plugins/([a-z0-9_.-]+)/", re.I)
THEME_RE = re.compile(r"/wp-content/themes/([a-z0-9_.-]+)/", re.I)
PHP_RE = re.compile(r"PHP/(\d+)\.(\d+)(?:\.(\d+))?", re.I)
PHP_SUPPORTED_FROM = (8, 2)  # PHP 8.1 reached end of life on 2025-12-31


@dataclass
class TLSInfo:
    days_left: int | None = None
    issuer: str = ""
    error: str = ""
    expires_at: str = ""
    protocol: str = ""


@dataclass
class SiteReport:
    url: str
    status: Status = "up"
    http_status: int | None = None
    response_ms: int | None = None
    final_url: str = ""
    tls_days_left: int | None = None
    tls_issuer: str = ""
    is_wordpress: bool = False
    wp_version: str = ""
    findings: list[Finding] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def add(self, code: str, severity: str, title: str, detail: str = "") -> None:
        self.findings.append(Finding(code=code, severity=severity, title=title, detail=detail))  # type: ignore[arg-type]

    def degrade(self, to: Status) -> None:
        order = {"up": 0, "degraded": 1, "down": 2}
        if order[to] > order[self.status]:
            self.status = to

    def summary(self) -> str:
        bits = [f"{self.url} is {self.status.upper()}"]
        if self.http_status is not None:
            bits.append(f"HTTP {self.http_status}")
        if self.response_ms is not None:
            bits.append(f"{self.response_ms} ms")
        if self.is_wordpress:
            bits.append(f"WordPress {self.wp_version or '(version hidden)'}")
        if self.tls_days_left is not None:
            bits.append(f"TLS {self.tls_days_left} days left")
        serious = [f for f in self.findings if f.severity != "info"]
        if serious:
            bits.append(f"{len(serious)} issue(s): " + "; ".join(f.title for f in serious[:3]))
        return ", ".join(bits)


def describe_fetch_error(exc: BaseException, timeout: float) -> str:
    if isinstance(exc, BlockedTarget):
        return f"refused to check: {exc}"
    if isinstance(exc, httpx.TimeoutException):
        return f"no response within {timeout:g} seconds"
    if isinstance(exc, httpx.ConnectError):
        text = str(exc).lower()
        if "name or service not known" in text or "nodename nor servname" in text or "getaddrinfo" in text:
            return "domain does not resolve (DNS) - the domain may have expired"
        if "certificate" in text or "ssl" in text:
            return f"TLS certificate problem: {exc}"
        if "refused" in text:
            return "refuses connections (the server may be down)"
        return f"cannot be reached ({exc})"
    if isinstance(exc, httpx.TooManyRedirects):
        return "redirects in a loop"
    if isinstance(exc, httpx.HTTPError):
        return f"cannot be reached ({exc.__class__.__name__}: {exc})"
    return f"check failed ({exc.__class__.__name__}: {exc})"


def parse_version(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", v)[:3])


@dataclass
class PageMarkers:
    """WordPress signs in a home page (and its Link header)."""

    markers: list[str] = field(default_factory=list)
    version: str = ""
    rest_url: str = ""  # as linked, not yet resolved or checked
    plugins: set[str] = field(default_factory=set)
    themes: list[str] = field(default_factory=list)


def page_markers(html: str, link_header: str) -> PageMarkers:
    page = PageMarkers()
    for tag in integrity.META_RE.finditer(html):
        content = GENERATOR_CONTENT_RE.search(tag.group(1)) if GENERATOR_NAME_RE.search(tag.group(1)) else None
        if content:
            page.markers.append("generator")
            page.version = content.group(1) or ""
            break
    lower = html.lower()
    if "/wp-content/" in lower or "/wp-includes/" in lower:
        page.markers.append("wp-paths")
    m = REST_LINK_HEADER_RE.search(link_header)
    if m:
        page.rest_url = m.group(1)
        page.markers.append("rest-link-header")
    else:
        for tag in LINK_RE.finditer(html):
            href = HREF_RE.search(tag.group(1)) if REST_REL_RE.search(tag.group(1)) else None
            if href:
                page.rest_url = href.group(1)
                page.markers.append("rest-link")
                break
    page.plugins = set(PLUGIN_RE.findall(html))
    page.themes = sorted(set(THEME_RE.findall(html)))
    return page


def _page_heading(lower: str) -> str:
    """The <title> and first <h1> text of a lower-cased page, or all of a very short page."""
    if len(lower) <= 3000:
        return lower
    parts = []
    for tag in ("title", "h1"):
        start = lower.find("<" + tag)
        if start >= 0:
            end = lower.find("</" + tag, start, start + 2000)
            parts.append(lower[start:end if end >= 0 else start + 2000])
    return " ".join(parts)


async def probe_tls(host: str, port: int, timeout: float, context: ssl.SSLContext | None = None,
                    allowed_private: set[str] | frozenset[str] = frozenset()) -> TLSInfo:
    ctx = context or ssl.create_default_context()
    try:
        _, writer = await asyncio.wait_for(
            open_public_connection(host, port, set(allowed_private), ssl=ctx, server_hostname=host), timeout=timeout
        )
    except ssl.SSLCertVerificationError as exc:
        return TLSInfo(error=exc.verify_message or str(exc))
    except BlockedTarget as exc:
        return TLSInfo(error=f"probe failed: {exc}")
    except (OSError, TimeoutError, ssl.SSLError) as exc:
        return TLSInfo(error=f"probe failed: {exc.__class__.__name__}: {exc}")
    try:
        cert = writer.get_extra_info("peercert") or {}
        ssl_object = writer.get_extra_info("ssl_object")
        protocol = ssl_object.version() if ssl_object is not None else ""
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ssl.SSLError):
            pass
    not_after = cert.get("notAfter")
    issuer = ""
    for rdn in cert.get("issuer", ()):
        for key, value in rdn:
            if key in ("organizationName", "commonName") and not issuer:
                issuer = value
    if not not_after:
        return TLSInfo(issuer=issuer, error="certificate has no expiry date")
    expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), tz=UTC)
    return TLSInfo(days_left=(expires - datetime.now(UTC)).days, issuer=issuer, expires_at=expires.isoformat(), protocol=protocol or "")


class SiteChecker:
    def __init__(
        self,
        http: ReadOnlyHttpClient,
        settings: Settings,
        *,
        latest_wp_version: str | None = None,
        tls_context: ssl.SSLContext | None = None,
        retry_delay: float = 2.0,
    ) -> None:
        self.http = http
        self.settings = settings
        self.latest_wp_version = latest_wp_version
        self.tls_context = tls_context
        self.retry_delay = retry_delay

    async def _walk(
        self, url: str, *, headers: dict[str, str] | None = None, max_bytes: int | None = None, max_hops: int = 5,
        hops: list[Hop] | None = None,
    ) -> tuple[FetchResult, str]:
        """GET url, following its redirects one hop at a time while they stay on the site's domain.

        Returns the last response and, if a redirect points to another domain, that
        target, which is not requested. Errors propagate; hops (if given) keeps the
        redirects followed so far.
        """
        hops = [] if hops is None else hops
        current, elapsed = url, 0
        for _ in range(max_hops + 1):
            res = await self.http.fetch(current, max_bytes=max_bytes, headers=headers, follow_redirects=False)
            elapsed += res.elapsed_ms
            location = res.headers.get("location", "") if res.status in integrity.REDIRECT_STATUSES else ""
            nxt = integrity.resolve(current, location) if location else ""
            last = replace(res, url=url, elapsed_ms=elapsed, redirects=[h.url for h in hops])
            if not nxt:
                return last, ""
            hops.append(Hop(url=current, status=res.status, location=nxt))
            if not integrity.same_site(url, nxt):
                return last, nxt
            current = nxt
        raise httpx.TooManyRedirects(f"more than {max_hops} redirects")

    async def _fetch_home(self, url: str) -> tuple[FetchResult | None, str, str, str]:
        """The home page and any other-domain redirect target (not requested); on failure the error and the URL that failed."""
        error, failed_url = "", url
        for attempt in range(2):  # one retry, so a passing hiccup does not raise an alert
            try:
                home, offsite = await self._walk(url)
                return home, offsite, "", ""
            except BlockedTarget as exc:
                return None, "", describe_fetch_error(exc, self.settings.monitor_timeout_seconds), url
            except Exception as exc:  # noqa: BLE001 - every failure becomes a finding
                error = describe_fetch_error(exc, self.settings.monitor_timeout_seconds)
                try:
                    failed_url = str(exc.request.url)  # type: ignore[attr-defined]
                except (AttributeError, RuntimeError):
                    failed_url = url
            if attempt == 0 and self.retry_delay:
                await asyncio.sleep(self.retry_delay)
        return None, "", error, failed_url

    async def _get(self, url: str, **kwargs: Any) -> FetchResult | None:
        """A secondary probe; a redirect to another domain counts as no answer and is not followed."""
        try:
            res, offsite = await self._walk(url, **kwargs)
        except Exception:  # noqa: BLE001 - secondary probes are best effort
            return None
        return None if offsite else res

    async def check(self, url: str, *, deep: bool = True, report: SiteReport | None = None) -> SiteReport:
        """Check a site; pass report to keep what was found so far if the check is cut short."""
        report = SiteReport(url=url) if report is None else report
        report.details["deep"] = deep
        home, offsite_target, error, failed_url = await self._fetch_home(url)
        if home is None:
            report.status = "down"
            if failed_url.rstrip("/") != url.rstrip("/"):
                report.add("fetch_failed", "critical", f"Site {error} after redirecting to {failed_url}")
                report.details["redirect_check"] = {"chain": [url, failed_url], "offsite": False}
            else:
                report.add("fetch_failed", "critical", f"Site {error}")
            return report

        report.http_status = home.status
        report.response_ms = home.elapsed_ms
        report.final_url = offsite_target or home.final_url
        report.details["redirects"] = home.redirects
        body = home.text.lower()
        self._assess_availability(report, home, body)
        self._assess_headers(report, home)
        offsite = self._assess_redirects(report, url, home, offsite_target)
        if urlsplit(home.final_url).scheme == "https":
            if integrity.same_site(url, home.final_url):  # never probe another domain's server
                await self._assess_tls(report, home.final_url)
        else:
            report.add("no_https", "warning", "Site is not served over HTTPS", f"final URL {home.final_url}")
        if offsite:
            return report  # the page served is not the site's own; nothing more to learn from it

        # Pattern matching on a large page is CPU work: keep it off the event loop.
        integrity_findings = await asyncio.to_thread(integrity.html_findings, url, home.final_url, home.text)
        report.findings.extend(integrity_findings)
        if any(f.severity == "critical" for f in integrity_findings):
            report.degrade("degraded")
        report.details["external_scripts"] = await asyncio.to_thread(integrity.external_script_hosts, url, home.final_url, home.text)
        await self._assess_wordpress(report, home, deep=deep)
        if deep:
            await self._assess_cloaking(report, url, home)
            await self._assess_http_redirect(report, url)
        return report

    async def trace(self, url: str, headers: dict[str, str] | None = None, max_hops: int = 6) -> Trace:
        """Walk the redirects from url one hop at a time; stop, without visiting it, at any other domain."""
        trace = Trace()
        try:
            res, offsite = await self._walk(url, headers=headers, max_hops=max_hops, hops=trace.hops)
        except Exception as exc:  # noqa: BLE001 - recorded on the trace
            trace.error = describe_fetch_error(exc, self.settings.monitor_timeout_seconds)
            return trace
        if offsite:
            trace.offsite_target = trace.final_url = offsite
        else:
            trace.final_url, trace.final_status, trace.html = res.final_url, res.status, res.text
        return trace

    def _assess_redirects(self, report: SiteReport, url: str, home: FetchResult, offsite_target: str = "") -> bool:
        chain = [*home.redirects, home.final_url] if home.redirects else [home.final_url]
        if chain[0] != url and not home.redirects:
            chain = [url, home.final_url]
        if offsite_target:
            chain.append(offsite_target)
        findings = integrity.chain_findings(url, chain)
        report.findings.extend(findings)
        offsite = any(f.code == "redirect_offsite" for f in findings)
        if offsite:
            report.degrade("degraded")
        report.details["redirect_check"] = {"chain": chain, "offsite": offsite}
        return offsite

    async def _assess_cloaking(self, report: SiteReport, url: str, home: FetchResult) -> None:
        normal = Trace(final_url=home.final_url, final_status=home.status, html=home.text)
        variants = {
            "arriving from Google on a phone": await self.trace(url, integrity.SEARCH_VISITOR),
            "identifying as Googlebot": await self.trace(url, integrity.CRAWLER),
        }
        findings = await asyncio.to_thread(integrity.cloaking_findings, url, normal, variants)
        report.findings.extend(findings)
        if findings:
            report.degrade("degraded")
        report.details.setdefault("redirect_check", {})["variants"] = {k: v.as_dict() for k, v in variants.items()}

    async def _assess_http_redirect(self, report: SiteReport, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "https":
            return
        plain = f"http://{parts.netloc}/"
        try:
            res = await self.http.fetch(plain, follow_redirects=False, max_bytes=4_000)
        except Exception:  # noqa: BLE001 - port 80 closed is fine
            return
        location = integrity.resolve(plain, res.headers.get("location", ""))
        if res.status == 200:
            report.add("no_https_redirect", "warning", "Plain http:// serves the site instead of redirecting to https://")
        elif res.status in integrity.REDIRECT_STATUSES and not location.startswith("https://"):
            report.add("no_https_redirect", "warning", "http:// does not redirect to https://",
                       f"redirects to {location or 'an invalid address'}")

    def _assess_availability(self, report: SiteReport, home: FetchResult, body: str) -> None:
        status = home.status
        if status in (403, 429, 503) and any(w in body for w in BOT_WALL):
            report.add("bot_protection", "info", f"Bot protection answered (HTTP {status}); the site itself looks up")
            return
        for needle, problem in PAGE_PROBLEMS:
            if needle in body:
                report.degrade("down")
                report.add("page_problem", "critical", f"Site {problem}")
                return
        if status == 503:
            # WordPress maintenance mode (core/plugin updates) answers 503 on purpose.
            for needle, note in PAGE_NOTES:
                if needle in body:
                    report.degrade("degraded")
                    report.add("page_notice", "warning", f"Site {note} (HTTP 503)")
                    return
        if status >= 500:
            report.degrade("down")
            report.add("http_error", "critical", f"Site answers with a server error (HTTP {status})")
            return
        if status == 401:
            report.add("auth_required", "info", "Site asks for a password (HTTP 401)")
        elif status >= 400:
            report.degrade("degraded")
            report.add("http_error", "warning", f"Site answers with an error (HTTP {status})")
        heading = _page_heading(body)  # not the whole page: a footer may say "coming soon" about anything
        for needle, note in PAGE_NOTES:
            if needle in heading:
                report.degrade("degraded")
                report.add("page_notice", "warning", f"Site {note}")
                break
        if home.elapsed_ms > self.settings.monitor_slow_ms:
            report.degrade("degraded")
            report.add(
                "slow_response",
                "warning",
                f"Home page took {home.elapsed_ms} ms (threshold {self.settings.monitor_slow_ms} ms)",
            )

    def _assess_headers(self, report: SiteReport, home: FetchResult) -> None:
        h = home.headers
        if urlsplit(home.final_url).scheme == "https" and "strict-transport-security" not in h:
            report.add("missing_hsts", "info", "No Strict-Transport-Security header")
        if h.get("x-content-type-options", "").lower() != "nosniff":
            report.add("missing_nosniff", "info", "No X-Content-Type-Options: nosniff header")
        powered = h.get("x-powered-by", "")
        match = PHP_RE.search(powered)
        if match:
            version = (int(match.group(1)), int(match.group(2)))
            label = f"{version[0]}.{version[1]}"
            if version < PHP_SUPPORTED_FROM:
                report.add(
                    "php_eol",
                    "warning",
                    f"Server runs PHP {label}, which no longer receives security fixes",
                    f"X-Powered-By: {powered}",
                )
            else:
                report.add("php_version_disclosed", "info", f"Server discloses its PHP version ({label})")
        server = h.get("server", "")
        if re.search(r"/\d", server):
            report.add("server_version_disclosed", "info", f"Server header discloses software version ({server})")
        report.details["server"] = server
        report.details["powered_by"] = powered

    async def _assess_tls(self, report: SiteReport, final_url: str) -> None:
        parts = urlsplit(final_url)
        host = parts.hostname or ""
        try:
            await self.http.check_target(host)
        except BlockedTarget:
            return
        info = await probe_tls(host, parts.port or 443, self.settings.monitor_timeout_seconds, self.tls_context,
                               self.http.allowed_private)
        if info.error:
            if info.error.startswith("probe failed"):
                report.add("tls_probe_failed", "info", "Could not inspect the TLS certificate directly", info.error)
            else:
                report.degrade("degraded")
                report.add("tls_invalid", "critical", f"TLS certificate is not valid: {info.error}")
            return
        report.tls_days_left = info.days_left
        report.tls_issuer = info.issuer
        report.details["tls"] = {"expires_at": info.expires_at, "protocol": info.protocol, "issuer": info.issuer}
        if info.protocol in ("TLSv1", "TLSv1.1"):
            report.add("tls_old_protocol", "warning", f"Server negotiated an outdated TLS version ({info.protocol})")
        days = info.days_left if info.days_left is not None else 9999
        if days < 0:
            report.degrade("down")
            report.add("tls_expired", "critical", f"TLS certificate expired {-days} day(s) ago")
        elif days <= 7:
            report.add("tls_expiring", "critical", f"TLS certificate expires in {days} day(s)")
        elif days <= 21:
            report.add("tls_expiring", "warning", f"TLS certificate expires in {days} days")

    async def _assess_wordpress(self, report: SiteReport, home: FetchResult, *, deep: bool) -> None:
        page = await asyncio.to_thread(page_markers, home.text, home.headers.get("link", ""))
        version, markers = page.version, page.markers
        origin = f"{urlsplit(home.final_url).scheme}://{urlsplit(home.final_url).netloc}/"
        rest_url = integrity.resolve(home.final_url, page.rest_url) if page.rest_url else ""
        if not rest_url or not integrity.same_site(report.url, rest_url):  # the page cannot send the probe elsewhere
            rest_url = urljoin(origin, "wp-json/")

        namespaces: list[str] = []
        rest = await self._get(rest_url, max_bytes=400_000)
        if rest is not None and rest.status == 200:
            try:
                index = json.loads(rest.text)
            except json.JSONDecodeError:
                index = None
            if isinstance(index, dict) and isinstance(index.get("namespaces"), list):
                namespaces = [str(n) for n in index["namespaces"]]
                markers.append("rest-index")
                report.details["site_name"] = str(index.get("name", ""))[:200]

        if not markers:
            report.details["wordpress_markers"] = []
            return
        report.is_wordpress = True
        report.details["wordpress_markers"] = markers
        report.details["rest_url"] = rest_url
        report.details["rest_namespaces"] = namespaces
        plugins = sorted({KNOWN_NAMESPACES[n] for n in namespaces if n in KNOWN_NAMESPACES})
        report.details["plugins_detected"] = sorted(set(plugins) | page.plugins)
        report.details["themes_detected"] = page.themes

        if not deep:
            report.wp_version = version
            return

        if not version:
            feed = await self._get(urljoin(origin, "feed/"), max_bytes=200_000)
            if feed is not None and feed.status == 200:
                fm = FEED_GENERATOR_RE.search(feed.text)
                if fm:
                    version = fm.group(1)
        report.wp_version = version
        if version:
            report.add("wp_version_disclosed", "info", f"WordPress version {version} is publicly visible")
            self._assess_core_version(report, version)

        await self._assess_exposures(report, origin, rest_url)

    def _assess_core_version(self, report: SiteReport, version: str) -> None:
        latest = self.latest_wp_version
        if not latest:
            return
        report.details["latest_wp_version"] = latest
        have, want = parse_version(version), parse_version(latest)
        if not have or not want or have >= want:
            return
        severity = "critical" if have[0] < want[0] else "warning"
        report.add(
            "wp_outdated",
            severity,
            f"WordPress {version} is behind the current release {latest}",
            "Core updates carry security fixes; schedule an update after a backup.",
        )

    async def _assess_exposures(self, report: SiteReport, origin: str, rest_url: str) -> None:
        xmlrpc = await self._get(urljoin(origin, "xmlrpc.php"), max_bytes=4_000)
        if xmlrpc is not None and "xml-rpc server accepts post requests only" in xmlrpc.text.lower():
            report.add(
                "xmlrpc_enabled",
                "warning",
                "XML-RPC is enabled (a common brute-force and amplification target)",
                "Disable it unless Jetpack or a mobile app needs it.",
            )

        users = await self._get(urljoin(rest_url, "wp/v2/users?per_page=100"), max_bytes=200_000)
        if users is not None and users.status == 200:
            try:
                listed = json.loads(users.text)
            except json.JSONDecodeError:
                listed = None
            if isinstance(listed, list) and listed:
                # Only the count is kept; account names are not stored.
                report.details["rest_users_exposed"] = len(listed)
                report.add(
                    "user_enumeration",
                    "warning",
                    f"REST API lists {len(listed)} user account(s) to anyone",
                    "Usernames help password-guessing attacks; restrict /wp-json/wp/v2/users.",
                )

        readme = await self._get(urljoin(origin, "readme.html"), max_bytes=20_000)
        if readme is not None and readme.status == 200 and "wordpress" in readme.text.lower():
            report.add("readme_exposed", "info", "readme.html is reachable and reveals the WordPress install")

        debug = await self._get(
            urljoin(origin, "wp-content/debug.log"), max_bytes=2_048, headers={"Range": "bytes=0-2047"}
        )
        if debug is not None and debug.status in (200, 206):
            ctype = debug.headers.get("content-type", "")
            text = debug.text
            if "html" not in ctype and re.search(r"PHP (Warning|Notice|Fatal|Deprecated|Parse)|\[\d{2}-\w{3}-\d{4}", text):
                report.add(
                    "debug_log_exposed",
                    "critical",
                    "wp-content/debug.log is publicly readable",
                    "It can leak file paths and errors; disable WP_DEBUG_LOG or block the file.",
                )

        uploads = await self._get(urljoin(origin, "wp-content/uploads/"), max_bytes=20_000)
        if uploads is not None and uploads.status == 200 and "index of /" in uploads.text.lower():
            report.add("directory_listing", "warning", "Directory listing is on for wp-content/uploads/")
