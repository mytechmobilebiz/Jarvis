"""Read-only signs that a site was tampered with: suspicious redirects and injected content.

Typical WordPress compromises redirect visitors to spam or scam domains, often
only for some visitors (people arriving from Google, phones, search-engine
crawlers), or inject obfuscated scripts, hidden iframes and spam links. Every
check here inspects responses the monitor already fetched, or makes extra GET
requests with different request headers; a redirect to another domain is
recorded but never followed, so the monitor does not visit the destination.
"""

from __future__ import annotations

import html as html_lib
import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from ..base import Finding

# Header sets for the cloaking checks. Malware commonly redirects only visitors
# who arrive from a search engine or on a phone, and shows spam only to crawlers.
SEARCH_VISITOR = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.0 Mobile/15E148 Safari/604.1",
    "Referer": "https://www.google.com/",
}
CRAWLER = {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"}

SPAM_TERMS = (
    "viagra", "cialis", "levitra", "online pharmacy", "payday loan", "replica watches", "casino online", "online casino",
    "slot gacor", "judi online", "bandar togel", "situs slot", "porn", "xxx videos", "buy cheap", "essay writing service",
)
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

META_REFRESH_RE = re.compile(
    r"<meta[^>]+http-equiv\s*=\s*[\"']?refresh[\"']?[^>]*content\s*=\s*[\"'][^\"']*?url\s*=\s*['\"]?([^\"'>\s]+)", re.I
)
JS_REDIRECT_RE = re.compile(
    r"(?:(?:window|document|top|self|parent)\s*\.\s*)?location(?:\s*\.\s*href)?\s*(?:=|\.\s*(?:replace|assign)\s*\()\s*[\"'](https?:)?//([^\"'/\s]+)",
    re.I,
)
SCRIPT_SRC_RE = re.compile(r"<script[^>]+src\s*=\s*[\"']([^\"']+)[\"']", re.I)
IFRAME_RE = re.compile(r"<iframe\b[^>]*>", re.I)
HIDDEN_RE = re.compile(r"(width\s*=\s*[\"']?[01][\"'\s>]|height\s*=\s*[\"']?[01][\"'\s>]|display\s*:\s*none|visibility\s*:\s*hidden)", re.I)
SRC_RE = re.compile(r"src\s*=\s*[\"']([^\"']+)[\"']", re.I)
OBFUSCATION_RES = (
    (re.compile(r"eval\s*\(\s*(?:atob|unescape|decodeURIComponent|String\.fromCharCode)\s*\(", re.I), "eval of decoded text"),
    (re.compile(r"eval\s*\(\s*function\s*\(\s*p\s*,\s*a\s*,\s*c\s*,\s*k\s*,\s*e\s*,\s*[rd]\s*\)", re.I), "packed (p,a,c,k,e,d) script"),
    (re.compile(r"document\.write\s*\(\s*(?:unescape|atob)\s*\(", re.I), "document.write of decoded text"),
    (re.compile(r"String\.fromCharCode\s*\(\s*(?:\d{2,3}\s*,\s*){24,}", re.I), "long String.fromCharCode sequence"),
)
HIDDEN_BLOCK_RE = re.compile(r"<(div|span|p)\b[^>]*(display\s*:\s*none|visibility\s*:\s*hidden|left\s*:\s*-\d{3,}px)[^>]*>(.{0,4000}?)</\1>", re.I | re.S)


def bare_host(url_or_host: str) -> str:
    host = urlsplit(url_or_host).hostname if "//" in url_or_host else url_or_host
    host = (host or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def same_site(site_url: str, other: str) -> bool:
    """True for the same host, www/apex variants, and subdomains of the site's domain."""
    a, b = bare_host(site_url), bare_host(other)
    return bool(a) and bool(b) and (a == b or b.endswith("." + a) or a.endswith("." + b))


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


@dataclass
class Hop:
    url: str
    status: int
    location: str = ""


@dataclass
class Trace:
    """A redirect chain walked one hop at a time, stopping before any other domain."""

    hops: list[Hop] = field(default_factory=list)
    final_url: str = ""
    final_status: int | None = None
    html: str = ""
    offsite_target: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "hops": [{"url": h.url, "status": h.status, "location": h.location} for h in self.hops],
            "final_url": self.final_url,
            "final_status": self.final_status,
            "offsite_target": self.offsite_target,
            "error": self.error,
        }


def spam_terms(text: str) -> list[str]:
    lower = html_lib.unescape(text).lower()
    return sorted({t for t in SPAM_TERMS if t in lower})


def chain_findings(site_url: str, urls: list[str]) -> list[Finding]:
    """Findings for a followed redirect chain: the URLs in order, ending with the final URL."""
    out: list[Finding] = []
    offsite = next((u for u in urls if not same_site(site_url, u)), "")
    if offsite:
        host = urlsplit(offsite).hostname or offsite
        suspicious = _is_ip(host) or bool(spam_terms(offsite))
        out.append(Finding(
            code="redirect_offsite",
            severity="critical" if suspicious else "warning",
            title=f"Site redirects visitors to another domain: {host}",
            detail="If the site has not deliberately moved, this is a common sign of a hacked site. "
                   "If it has moved, update the registered URL.",
        ))
    for prev, nxt in zip(urls, urls[1:], strict=False):
        if urlsplit(prev).scheme == "https" and urlsplit(nxt).scheme == "http":
            out.append(Finding(code="redirect_downgrade", severity="warning",
                               title="A redirect drops from HTTPS to plain HTTP", detail=f"{prev} -> {nxt}"))
            break
    if len(urls) > 4:
        out.append(Finding(code="redirect_chain_long", severity="info", title=f"Long redirect chain ({len(urls) - 1} redirects)"))
    return out


def external_script_hosts(site_url: str, page_url: str, html: str) -> list[str]:
    hosts = set()
    for src in SCRIPT_SRC_RE.findall(html):
        host = urlsplit(urljoin(page_url, html_lib.unescape(src))).hostname
        if host and not same_site(site_url, host):
            hosts.add(host.lower())
    return sorted(hosts)


def html_findings(site_url: str, page_url: str, html: str) -> list[Finding]:
    """Client-side redirects, hidden iframes, obfuscated scripts and hidden spam in a page."""
    out: list[Finding] = []
    for target in META_REFRESH_RE.findall(html):
        dest = urljoin(page_url, html_lib.unescape(target))
        if not same_site(site_url, dest):
            out.append(Finding(code="meta_refresh_offsite", severity="critical",
                               title=f"Page refreshes visitors to another domain: {urlsplit(dest).hostname}"))
            break
    for _scheme, host in JS_REDIRECT_RE.findall(html):
        if not same_site(site_url, host):
            out.append(Finding(code="js_redirect_offsite", severity="critical",
                               title=f"Script sends visitors to another domain: {host.lower()}",
                               detail="Injected JavaScript redirects are a common sign of a hacked site."))
            break
    for tag in IFRAME_RE.findall(html):
        src = SRC_RE.search(tag)
        if src and HIDDEN_RE.search(tag) and not same_site(site_url, urljoin(page_url, src.group(1))):
            out.append(Finding(code="hidden_iframe", severity="critical",
                               title=f"Hidden iframe loads another domain: {urlsplit(urljoin(page_url, src.group(1))).hostname}"))
            break
    for regex, label in OBFUSCATION_RES:
        if regex.search(html):
            out.append(Finding(code="obfuscated_script", severity="warning", title=f"Obfuscated JavaScript on the page ({label})",
                               detail="Legitimate plugins rarely ship obfuscated inline code; check where it comes from."))
            break
    hidden_spam = sorted({t for m in HIDDEN_BLOCK_RE.finditer(html) for t in spam_terms(m.group(3))})
    if hidden_spam:
        out.append(Finding(code="hidden_spam", severity="critical", title="Hidden spam text on the page: " + ", ".join(hidden_spam[:5])))
    else:
        visible = spam_terms(re.sub(r"<script\b.*?</script>", " ", html, flags=re.I | re.S))
        if visible:
            out.append(Finding(code="spam_terms", severity="warning", title="Spam-like terms on the page: " + ", ".join(visible[:5]),
                               detail="Expected for some businesses; otherwise a sign of injected spam."))
    return out


def cloaking_findings(site_url: str, normal: Trace, variants: dict[str, Trace]) -> list[Finding]:
    """Compare what ordinary visitors get with what search visitors and crawlers get."""
    out: list[Finding] = []
    normal_spam = set(spam_terms(normal.html))
    for label, trace in variants.items():
        if trace.offsite_target and not normal.offsite_target:
            out.append(Finding(
                code="conditional_redirect",
                severity="critical",
                title=f"Visitors {label} are redirected to another domain: {urlsplit(trace.offsite_target).hostname}",
                detail="Ordinary visitors are not redirected. Showing different content by visitor is a classic sign of a hacked site.",
            ))
        extra = sorted(set(spam_terms(trace.html)) - normal_spam)
        if extra:
            out.append(Finding(code="cloaked_spam", severity="critical",
                               title=f"Spam shown only to visitors {label}: " + ", ".join(extra[:5])))
    return out
