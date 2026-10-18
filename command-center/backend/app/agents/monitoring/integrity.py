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
DESKTOP_SEARCH_VISITOR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 "
    "Safari/537.36",
    "Referer": "https://www.google.com/",
}

# Whole words, with a plural ending ("payday loans"), so "cialis" in "specialist"
# does not count; drug names and "porn" count inside other words too ("cheapviagra", "pornhub").
SPAM_TERMS = (
    "cialis", "online pharmacy", "payday loan", "replica watch", "casino online", "online casino", "slot gacor", "judi online",
    "bandar togel", "situs slot", "xxx video", "buy cheap", "essay writing service",
)
_SPAM_IRREGULAR = {"online pharmacies": "online pharmacy"}
SPAM_RE = re.compile(
    r"(viagra|levitra|\bporn)|\b(" + "|".join(re.escape(t) for t in (*_SPAM_IRREGULAR, *SPAM_TERMS)) + r")(?:e?s)?\b"
)
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Pages can be large and these run on every check, so no pattern may backtrack
# badly: a tag is found by its opening alone, read forward to its '>' (see
# tags()), and its attributes are then tested on their own.
META_RE = re.compile(r"<meta\b", re.I)
SCRIPT_TAG_RE = re.compile(r"<script\b", re.I)
IFRAME_RE = re.compile(r"<iframe\b", re.I)
HIDDEN_OPEN_RE = re.compile(r"<(div|span|p)\b", re.I)
REFRESH_RE = re.compile(r"(?<![\w-])http-equiv\s*=\s*[\"']?refresh\b", re.I)
CONTENT_RE = re.compile(r"(?<![\w-])content\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"']+))", re.I)
REFRESH_URL_RE = re.compile(r"url\s*=\s*['\"]?([^\"'\s]+)", re.I)
# Anchored on the left so "data-location=" and "x.location" do not count; the
# host stops at a path, query, fragment or backslash and is parsed separately.
# The repeated prefix (window.top.) is bounded, spaces included, so no start
# backtracks through it; past "location" the spaces are free (formatted code).
JS_REDIRECT_RE = re.compile(
    r"(?<![\w.$-])(?:(?:window|document|top|self|parent)\s{0,8}\.\s{0,8}){0,4}location(?:\s*\.\s*href)?\s*"
    r"(?:=|\.\s*(?:replace|assign)\s*\()\s*[\"'](https?:)?//([^\"'/\\\s?#]+)",
    re.I,
)
# Handlers that run only when the visitor acts (onclick=...): a button that
# navigates is a link, not a redirect. onload, onerror and the like run by
# themselves, so they are not in this list.
USER_ACTION_RE = re.compile(
    r"on(?:click|dblclick|auxclick|contextmenu|mouse(?:down|up)|pointer(?:down|up)|touch(?:start|end)|key(?:down|up|press)"
    r"|submit|change|input)",
    re.I,
)
# Tags as a browser's tokenizer reads them, for _markup(): a quote opens an
# attribute value only right after '=', a quoted value may hold '>', and a
# value or tag left open runs to the end of the page.
_WS = "\t\n\f\r "
_ATTR = (rf"(=[^{_WS}/>=]*+|[^{_WS}/>=]++)"
         rf"(?:[{_WS}]*+=[{_WS}]*+(?:\"[^\"]*+\"?|'[^']*+'?|[^{_WS}>\"'][^{_WS}>]*+))?+")
ATTR_RE = re.compile(_ATTR)
TAG_END_RE = re.compile(rf"(?:[{_WS}/]++|{_ATTR})*+>")
MARKUP_RE = re.compile(rf"<!--|<(?P<slash>/?)(?P<name>[a-z][^{_WS}/>]*+)|<[!?/]", re.I)
COMMENT_END_RE = re.compile(r"--!?>")
# Elements whose content is text, not markup (with scripting on, as for <noscript>).
RAW_TEXT_CLOSE_RES = {
    tag: re.compile(rf"</{tag}(?=[{_WS}/>])", re.I)
    for tag in ("script", "style", "noscript", "title", "textarea", "xmp", "iframe", "noembed", "noframes")
}
# What can end a script's escaped states (see _script_close): "<!-->" ends one as it opens.
SCRIPT_TOKEN_RE = re.compile(rf"<!--(?!-*>)|-->|<(/?)script(?=[{_WS}/>])", re.I)
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
    """The spam terms in text, each named once in its base form ("payday loans" counts as "payday loan")."""
    found = SPAM_RE.findall(html_lib.unescape(text).lower())
    return sorted({anywhere or _SPAM_IRREGULAR.get(word, word) for anywhere, word in found})


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


def tags(html: str, open_re: re.Pattern[str]) -> Iterator[tuple[re.Match[str], str]]:
    """Each tag that open_re finds, with its attributes up to the next '>' however long they are; forward only."""
    end = -1
    for m in open_re.finditer(html):
        if m.start() < end:
            continue  # inside the previous tag's attributes
        end = html.find(">", m.end())
        if end < 0:
            return
        yield m, html[m.end():end]


@dataclass
class _Markup:
    raw: list[tuple[int, int]] = field(default_factory=list)  # comments, <script> and <style>: never shown
    noscript: list[tuple[int, int]] = field(default_factory=list)  # real, closed <noscript> elements
    user_actions: list[tuple[int, int]] = field(default_factory=list)  # onclick=... and the like on real tags


def _markup(html: str) -> _Markup:
    """Comments, raw-text elements and tags as a browser's tokenizer splits the page, in one forward scan.

    Text in a comment, a script or an attribute value is never taken for a tag,
    and whatever is left open (a comment, a tag, a script) runs to the end of
    the page, where the scan stops.
    """
    out, pos = _Markup(), 0
    while (m := MARKUP_RE.search(html, pos)) is not None:
        if m.group("name") is None:  # a comment, or a bogus one such as <!doctype ...> or <?xml ...>
            if m.group(0) == "<!--":
                close = COMMENT_END_RE.search(html, m.start() + 2)  # "<!-->" is a whole comment
                if close and close.group(0) == "--!>" and close.start() < m.end():  # "<!--!>" is not
                    close = COMMENT_END_RE.search(html, m.end())
                pos = close.end() if close else len(html)
            else:
                close_at = html.find(">", m.end())
                pos = len(html) if close_at < 0 else close_at + 1
            out.raw.append((m.start(), pos))
            continue
        tag = TAG_END_RE.match(html, m.end())
        if tag is None:
            break
        pos = tag.end()
        if m.group("slash"):
            continue  # an end tag: its attributes do nothing
        if USER_ACTION_RE.search(html, m.end(), pos):
            attrs = ATTR_RE.finditer(html, m.end(), pos - 1)
            out.user_actions += [a.span() for a in attrs if USER_ACTION_RE.fullmatch(a.group(1))]
        name = m.group("name").lower()
        if name in RAW_TEXT_CLOSE_RES:
            close = _script_close(html, pos) if name == "script" else RAW_TEXT_CLOSE_RES[name].search(html, pos)
            end_tag = TAG_END_RE.match(html, close.end()) if close else None
            end = end_tag.end() if end_tag else len(html)
            if name in ("script", "style"):
                out.raw.append((m.start(), end))
            elif name == "noscript" and end_tag:
                out.noscript.append((m.start(), end))
            pos = end
    return out


def _script_close(html: str, pos: int) -> re.Match[str] | None:
    """The "</script" that ends a script body starting at pos, as the tokenizer's escape states find it.

    Inside "<!--", a "<script ... </script" pair does not end the element; "-->" leaves both states.
    """
    escaped = double = False
    for token in SCRIPT_TOKEN_RE.finditer(html, pos):
        if token.group(0) == "<!--":
            escaped = True
        elif token.group(0) == "-->":
            escaped = double = False
        elif token.group(1):  # </script
            if not double:
                return token
            double = False
        elif escaped:  # <script inside <!--
            double = True
    return None


def _inside(spans: list[tuple[int, int]], pos: int) -> bool:
    i = bisect.bisect_right(spans, pos, key=lambda span: span[0]) - 1
    return i >= 0 and spans[i][0] <= pos < spans[i][1]


def _without_scripts(html: str, raw: list[tuple[int, int]]) -> str:
    """The page without its comments, scripts and styles (raw spans from _markup())."""
    parts, pos = [], 0
    for start, end in raw:
        parts.append(html[pos:start])
        pos = end
    parts.append(html[pos:])
    return " ".join(parts)


def _hidden_texts(html: str) -> Iterator[str]:
    """The text of <div>, <span> and <p> blocks styled to be invisible; text inside several of them is given once."""
    closes: dict[str, list[int]] = {}
    covered = 0
    for m, attrs in tags(html, HIDDEN_OPEN_RE):
        if not HIDDEN_STYLE_RE.search(attrs):
            continue
        tag, body = m.group(1).lower(), m.end() + len(attrs) + 1
        if tag not in closes:
            closes[tag] = [c.start() for c in CLOSE_TAG_RES[tag].finditer(html)]
        i = bisect.bisect_left(closes[tag], body)
        if i == len(closes[tag]) or closes[tag][i] - body > HIDDEN_BLOCK_TEXT:
            continue
        start, end = max(body, covered), closes[tag][i]
        if start < end:  # blocks overlap when nested: the text passed on stays within the page's length
            yield html[start:end]
            covered = end


def _is_tag_pixel(url: str) -> bool:
    host = bare_host(url)
    return any(host == h or host.endswith("." + h) for h in TAG_IFRAME_HOSTS)


def external_script_hosts(site_url: str, page_url: str, html: str) -> list[str]:
    hosts = set()
    for _tag, attrs in tags(html, SCRIPT_TAG_RE):
        src = SRC_RE.search(attrs)
        dest = resolve(page_url, html_lib.unescape(src.group(1))) if src else ""
        if dest and not same_site(site_url, dest):
            hosts.add(_hostname(dest))
    return sorted(hosts)


def html_findings(site_url: str, page_url: str, html: str) -> list[Finding]:
    """Client-side redirects, hidden iframes, obfuscated scripts and hidden spam in a page."""
    out: list[Finding] = []
    markup = _markup(html)
    for _tag, attrs in tags(html, META_RE):
        content = CONTENT_RE.search(attrs) if REFRESH_RE.search(attrs) else None
        target = REFRESH_URL_RE.search(next(g for g in content.groups() if g is not None)) if content else None
        dest = resolve(page_url, html_lib.unescape(target.group(1))) if target else ""
        if dest and not same_site(site_url, dest):
            out.append(Finding(code="meta_refresh_offsite", severity="critical",
                               title=f"Page refreshes visitors to another domain: {_hostname(dest)}"))
            break
    pos = 0
    while (redirect := JS_REDIRECT_RE.search(html, pos)) is not None:
        # A match passed over may have run on into a real redirect: look again from its host. Host
        # runs hold no quote, so the next match's host starts after this one's and the scan stays linear.
        pos = redirect.start(2)
        if _inside(markup.user_actions, redirect.start()):
            continue  # runs when the visitor clicks: a link, not a redirect
        authority = redirect.group(2)
        host = _hostname("//" + authority)
        if host and not same_site(site_url, "//" + authority):
            out.append(Finding(code="js_redirect_offsite", severity="critical",
                               title=f"Script sends visitors to another domain: {host}",
                               detail="Injected JavaScript redirects are a common sign of a hacked site."))
            break
    noscript_noted = False
    for tag, attrs in tags(html, IFRAME_RE):
        src = SRC_RE.search(attrs)
        if not src or not HIDDEN_RE.search(attrs):
            continue
        dest = resolve(page_url, html_lib.unescape(src.group(1)))
        if not dest or same_site(site_url, dest) or _is_tag_pixel(dest):
            continue
        if not _inside(markup.noscript, tag.start()):
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
        visible = spam_terms(_without_scripts(html, markup.raw))
        if visible:
            out.append(Finding(code="spam_terms", severity="warning", title="Spam-like terms on the page: " + ", ".join(visible[:5]),
                               detail="Expected for some businesses; otherwise a sign of injected spam."))
    return out


def cloaking_findings(site_url: str, normal: Trace, variants: dict[str, Trace]) -> list[Finding]:
    """Compare what ordinary visitors get with what search visitors and crawlers get."""
    out: list[Finding] = []
    normal_spam = set(spam_terms(normal.html))
    normal_titles = {f.title for f in html_findings(site_url, normal.final_url, normal.html)}
    normal_scripts = set(external_script_hosts(site_url, normal.final_url, normal.html))
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
                    if f.severity != "info" and f.code not in ("hidden_spam", "spam_terms") and f.title not in normal_titles]
        if injected:
            out.append(Finding(code="cloaked_injection",
                               severity="critical" if any(f.severity == "critical" for f in injected) else "warning",
                               title=f"Shown only to visitors {label}: " + "; ".join(f.title for f in injected[:3]),
                               detail="Ordinary visitors do not get it. Showing different content by visitor is a classic sign "
                                      "of a hacked site."))
        scripts = sorted(set(external_script_hosts(site_url, trace.final_url, trace.html)) - normal_scripts)
        if scripts:  # warning: ad and analytics scripts are often left out for visitors that look like bots
            out.append(Finding(code="cloaked_script", severity="warning",
                               title=f"Script domain(s) loaded only for visitors {label}: " + ", ".join(scripts[:5]),
                               detail="Ordinary visitors do not get them. Confirm someone added them; injected scripts are "
                                      "often served only to search visitors or phones."))
    return out
