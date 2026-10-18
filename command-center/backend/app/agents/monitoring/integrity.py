"""Read-only signs that a site was tampered with: suspicious redirects and injected content.

Typical WordPress compromises redirect visitors to spam or scam domains, often
only for some visitors (people arriving from Google, phones, search-engine
crawlers), or inject obfuscated scripts, hidden iframes and spam links. Every
check here inspects responses the monitor already fetched, or makes extra GET
requests with different request headers; a redirect to another domain is
recorded but never followed, so the monitor does not visit the destination.
"""

from __future__ import annotations

import bisect
import html as html_lib
import ipaddress
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import idna

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
SPAM_RE = re.compile(r"\b(?:" + "|".join(re.escape(t) for t in SPAM_TERMS) + r")\b")  # whole words: not "cialis" in "specialist"
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Pages can be large and these run on every check, so no pattern may backtrack
# badly: a tag is matched with one bounded, possessive run of attributes and
# the attributes are then tested on their own.
META_RE = re.compile(r"<meta\b([^>]{0,1000}+)>", re.I)
SCRIPT_TAG_RE = re.compile(r"<script\b([^>]{0,1000}+)>", re.I)
IFRAME_RE = re.compile(r"<iframe\b([^>]{0,1000}+)>", re.I)
HIDDEN_OPEN_RE = re.compile(r"<(div|span|p)\b([^>]{0,1000}+)>", re.I)
REFRESH_RE = re.compile(r"(?<![\w-])http-equiv\s*=\s*[\"']?refresh\b", re.I)
CONTENT_RE = re.compile(r"(?<![\w-])content\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"']+))", re.I)
REFRESH_URL_RE = re.compile(r"url\s*=\s*['\"]?([^\"'\s]+)", re.I)
# Anchored on the left so "data-location=" and "x.location" do not count; the
# host stops at a path, query, fragment or backslash and is parsed separately.
JS_REDIRECT_RE = re.compile(
    r"(?<![\w.$-])(?:(?:window|document|top|self|parent)\s*\.\s*)*location(?:\s*\.\s*href)?\s*"
    r"(?:=|\.\s*(?:replace|assign)\s*\()\s*[\"'](https?:)?//([^\"'/\\\s?#]+)",
    re.I,
)
# Inline event handlers (onclick=...): a button that navigates is a link, not a redirect.
ON_ATTR_RE = re.compile(r"(?<![\w-])on[a-z]+\s*=\s*(?:\"[^\"]*\"|'[^']*')", re.I)
HIDDEN_RE = re.compile(
    r"(?<![\w-])(?:width|height)\s*=\s*[\"']?[01](?![\w.%])|display\s*:\s*none|visibility\s*:\s*hidden", re.I
)
HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|left\s*:\s*-\d{3,}px", re.I)
SRC_RE = re.compile(r"(?<![\w-])src\s*=\s*[\"']([^\"']+)[\"']", re.I)
OBFUSCATION_RES = (
    (re.compile(r"eval\s*\(\s*(?:atob|unescape|decodeURIComponent|String\.fromCharCode)\s*\(", re.I), "eval of decoded text"),
    (re.compile(r"eval\s*\(\s*function\s*\(\s*p\s*,\s*a\s*,\s*c\s*,\s*k\s*,\s*e\s*,\s*[rd]\s*\)", re.I), "packed (p,a,c,k,e,d) script"),
    (re.compile(r"document\.write\s*\(\s*(?:unescape|atob)\s*\(", re.I), "document.write of decoded text"),
    (re.compile(r"String\.fromCharCode\s*\(\s*(?:\d{2,3}\s*,\s*){24,}", re.I), "long String.fromCharCode sequence"),
)
CLOSE_TAG_RES = {tag: re.compile(rf"</{tag}>", re.I) for tag in ("div", "span", "p")}
HIDDEN_BLOCK_TEXT = 4000  # characters of hidden text looked at per block
MAX_HIDDEN_BLOCKS = 500
SCRIPT_OPEN_RE = re.compile(r"<script\b", re.I)
SCRIPT_CLOSE_RE = re.compile(r"</script\s*>", re.I)
NOSCRIPT_OPEN_RE = re.compile(r"<noscript\b", re.I)
NOSCRIPT_CLOSE_RE = re.compile(r"</noscript\s*>", re.I)
# Tag managers and ad pixels ship hidden iframes on purpose (the Google Tag Manager <noscript> snippet).
TAG_IFRAME_HOSTS = ("googletagmanager.com", "doubleclick.net")
_C0_AND_SPACE = "".join(map(chr, range(0x21)))
_SPECIAL_SCHEME_RE = re.compile(r"(https?):/*", re.I)


def _ascii_host(host: str) -> str:
    """An internationalised host name as it goes on the wire (punycode), the way httpx sends it."""
    if host.isascii():
        return host
    try:
        return idna.encode(host).decode("ascii")
    except (idna.IDNAError, UnicodeError):
        return host


def bare_host(url_or_host: str) -> str:
    try:
        host = urlsplit(url_or_host if "//" in url_or_host else "//" + url_or_host).hostname
    except ValueError:
        host = None
    host = _ascii_host((host or "").lower().rstrip("."))
    return host[4:] if host.startswith("www.") else host


def same_site(site_url: str, other: str) -> bool:
    """True for the same host, www/apex variants, and subdomains of the site's domain."""
    a, b = bare_host(site_url), bare_host(other)
    return bool(a) and bool(b) and (a == b or b.endswith("." + a) or a.endswith("." + b))


def resolve(base: str, ref: str) -> str:
    """ref (a Location header, a link) resolved against base as a browser does it; "" if it is not an http(s) URL.

    Browsers drop tabs and newlines, read a backslash as a slash and skip extra
    slashes before the host; urljoin does none of that, so "/\\evil.test/"
    would otherwise look like a path on the same site.
    """
    ref = re.sub(r"[\t\n\r]", "", ref).strip(_C0_AND_SPACE).replace("\\", "/")
    try:
        special = _SPECIAL_SCHEME_RE.match(ref)
        if special:
            scheme, slashes = special.group(1).lower(), special.group(0)[len(special.group(1)) + 1:]
            rest = ref[special.end():]
            # "https:x" on an https page is a relative path; otherwise whatever follows the slashes is the host.
            same_scheme = scheme == urlsplit(base).scheme.lower() and len(slashes) < 2
            ref = slashes + rest if same_scheme else f"{scheme}://{rest}"
        elif ref.startswith("//"):
            ref = "//" + ref.lstrip("/")
        url = urljoin(base, ref)
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:  # e.g. "//[" (an unclosed IPv6 bracket)
        return ""
    return url if parts.scheme in ("http", "https") and parts.hostname else ""


def _hostname(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


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
    return sorted(set(SPAM_RE.findall(html_lib.unescape(text).lower())))


def chain_findings(site_url: str, urls: list[str]) -> list[Finding]:
    """Findings for a followed redirect chain: the URLs in order, ending with the final URL."""
    out: list[Finding] = []
    offsite = next((u for u in urls if not same_site(site_url, u)), "")
    if offsite:
        host = _hostname(offsite) or offsite
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


def _elements(html: str, open_re: re.Pattern[str], close_re: re.Pattern[str]) -> list[tuple[int, int]]:
    """(start, end) of each element opened by open_re, found by searching forward only (no backtracking)."""
    spans: list[tuple[int, int]] = []
    pos = 0
    while (opening := open_re.search(html, pos)) is not None:
        closing = close_re.search(html, opening.end())
        pos = closing.end() if closing else len(html)  # an unclosed element runs to the end, as in a browser
        spans.append((opening.start(), pos))
    return spans


def _inside(spans: list[tuple[int, int]], pos: int) -> bool:
    i = bisect.bisect_right(spans, pos, key=lambda span: span[0]) - 1
    return i >= 0 and spans[i][0] <= pos < spans[i][1]


def _without_scripts(html: str) -> str:
    parts, pos = [], 0
    for start, end in _elements(html, SCRIPT_OPEN_RE, SCRIPT_CLOSE_RE):
        parts.append(html[pos:start])
        pos = end
    parts.append(html[pos:])
    return " ".join(parts)


def _hidden_texts(html: str) -> Iterator[str]:
    """The text of <div>, <span> and <p> blocks styled to be invisible (a bounded number of them)."""
    examined = 0
    for m in HIDDEN_OPEN_RE.finditer(html):
        if not HIDDEN_STYLE_RE.search(m.group(2)):
            continue
        close = CLOSE_TAG_RES[m.group(1).lower()].search(html, m.end(), m.end() + HIDDEN_BLOCK_TEXT + 7)
        if close is not None:
            yield html[m.end():close.start()]
        examined += 1
        if examined >= MAX_HIDDEN_BLOCKS:
            return


def _is_tag_pixel(url: str) -> bool:
    host = bare_host(url)
    return any(host == h or host.endswith("." + h) for h in TAG_IFRAME_HOSTS)


def external_script_hosts(site_url: str, page_url: str, html: str) -> list[str]:
    hosts = set()
    for tag in SCRIPT_TAG_RE.finditer(html):
        src = SRC_RE.search(tag.group(1))
        dest = resolve(page_url, html_lib.unescape(src.group(1))) if src else ""
        if dest and not same_site(site_url, dest):
            hosts.add(_hostname(dest))
    return sorted(hosts)


def html_findings(site_url: str, page_url: str, html: str) -> list[Finding]:
    """Client-side redirects, hidden iframes, obfuscated scripts and hidden spam in a page."""
    out: list[Finding] = []
    for tag in META_RE.finditer(html):
        attrs = tag.group(1)
        content = CONTENT_RE.search(attrs) if REFRESH_RE.search(attrs) else None
        target = REFRESH_URL_RE.search(next(g for g in content.groups() if g is not None)) if content else None
        dest = resolve(page_url, html_lib.unescape(target.group(1))) if target else ""
        if dest and not same_site(site_url, dest):
            out.append(Finding(code="meta_refresh_offsite", severity="critical",
                               title=f"Page refreshes visitors to another domain: {_hostname(dest)}"))
            break
    for _scheme, authority in JS_REDIRECT_RE.findall(ON_ATTR_RE.sub(" ", html)):
        host = _hostname("//" + authority)
        if host and not same_site(site_url, "//" + authority):
            out.append(Finding(code="js_redirect_offsite", severity="critical",
                               title=f"Script sends visitors to another domain: {host}",
                               detail="Injected JavaScript redirects are a common sign of a hacked site."))
            break
    noscript = _elements(html, NOSCRIPT_OPEN_RE, NOSCRIPT_CLOSE_RE)
    noscript_noted = False
    for tag in IFRAME_RE.finditer(html):
        attrs = tag.group(1)
        src = SRC_RE.search(attrs)
        if not src or not HIDDEN_RE.search(attrs):
            continue
        dest = resolve(page_url, html_lib.unescape(src.group(1)))
        if not dest or same_site(site_url, dest) or _is_tag_pixel(dest):
            continue
        if not _inside(noscript, tag.start()):
            out.append(Finding(code="hidden_iframe", severity="critical",
                               title=f"Hidden iframe loads another domain: {_hostname(dest)}"))
            break
        if not noscript_noted:  # shown only to visitors without JavaScript
            noscript_noted = True
            out.append(Finding(code="hidden_iframe_noscript", severity="info",
                               title=f"Hidden iframe in a <noscript> block loads another domain: {_hostname(dest)}"))
    for regex, label in OBFUSCATION_RES:
        if regex.search(html):
            out.append(Finding(code="obfuscated_script", severity="warning", title=f"Obfuscated JavaScript on the page ({label})",
                               detail="Legitimate plugins rarely ship obfuscated inline code; check where it comes from."))
            break
    hidden_spam = sorted({t for text in _hidden_texts(html) for t in spam_terms(text)})
    if hidden_spam:
        out.append(Finding(code="hidden_spam", severity="critical", title="Hidden spam text on the page: " + ", ".join(hidden_spam[:5])))
    else:
        visible = spam_terms(_without_scripts(html))
        if visible:
            out.append(Finding(code="spam_terms", severity="warning", title="Spam-like terms on the page: " + ", ".join(visible[:5]),
                               detail="Expected for some businesses; otherwise a sign of injected spam."))
    return out


def cloaking_findings(site_url: str, normal: Trace, variants: dict[str, Trace]) -> list[Finding]:
    """Compare what ordinary visitors get with what search visitors and crawlers get."""
    out: list[Finding] = []
    normal_spam = set(spam_terms(normal.html))
    normal_titles = {f.title for f in html_findings(site_url, normal.final_url, normal.html)}
    for label, trace in variants.items():
        if trace.offsite_target and not normal.offsite_target:
            out.append(Finding(
                code="conditional_redirect",
                severity="critical",
                title=f"Visitors {label} are redirected to another domain: {_hostname(trace.offsite_target)}",
                detail="Ordinary visitors are not redirected. Showing different content by visitor is a classic sign of a hacked site.",
            ))
        extra = sorted(set(spam_terms(trace.html)) - normal_spam)
        if extra:
            out.append(Finding(code="cloaked_spam", severity="critical",
                               title=f"Spam shown only to visitors {label}: " + ", ".join(extra[:5])))
        # Spam is covered above; anything else injected only for these visitors is cloaking too.
        injected = [f for f in html_findings(site_url, trace.final_url, trace.html)
                    if f.severity == "critical" and f.code != "hidden_spam" and f.title not in normal_titles]
        if injected:
            out.append(Finding(code="cloaked_injection", severity="critical",
                               title=f"Shown only to visitors {label}: " + "; ".join(f.title for f in injected[:3]),
                               detail="Ordinary visitors do not get it. Showing different content by visitor is a classic sign "
                                      "of a hacked site."))
    return out
