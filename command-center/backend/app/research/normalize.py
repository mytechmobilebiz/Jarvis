"""One result schema for every provider (plan section 3.6), and the cleaning every web string goes through.

Cleaning, in this order, for each field of each result:
1. cut to its cap first (title 200 characters, snippet and infobox text 1,200), so the cleaner never sees more;
2. NUL bytes, control characters and invisible characters removed;
3. app/brain/text.clean(): secrets redacted, e-mail addresses and phone numbers masked, a field holding restricted
   data emptied (flags "redacted" and "withheld"); then cut to its cap again, as a mask can be longer than what it
   replaces;
4. "instruction_like" set from instruction_flags();
5. links kept only for http: and https: URLs, which are then normalized (lower-case scheme and host, no default
   port, fragment, trailing slash, user name or tracking parameters) and de-duplicated, engines merged.
A URL that the cleaner would change (a secret parameter, an address in it) drops its result.

Web text is untrusted: it is shown as plain text and never reaches an AI prompt, a finding or an event.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..brain import text as brain_text
from .canon import strip_invisible

TITLE_CAP = 200
SNIPPET_CAP = 1200
URL_CAP = 2048
ENGINES_CAP = 10
MAX_RESULTS = 20
SCORE_CAP = 1_000_000.0
TRACKING = re.compile(r"(?i)^(?:utm_[a-z0-9_]*|fbclid|gclid|mc_eid|mc_cid|msclkid)$")
DEFAULT_PORTS = {"http": 80, "https": 443}
_SPACE = re.compile(r"\s+")
_ENGINE = re.compile(r"[a-z0-9 _.\-]{1,40}")

# Statuses a search can end with (plan section 3.6; provider_misconfigured is SearXNG answering 403).
STATUSES = ("ok", "partial", "empty", "provider_down", "unreachable", "bad_request", "provider_misconfigured",
            "blocked", "refused_budget", "refused_rate", "refused_policy", "failed")
ANSWERED = ("ok", "partial", "empty")


@dataclass
class ResearchResponse:
    """What a capability gets back from ctx.research.search(): citations to show, never instructions to follow."""

    status: str
    reason: str = ""
    query_id: int | None = None
    provider: str = ""
    provider_version: str = ""
    cached: bool = False
    query: str = ""  # the canonical text sent; empty when the query was refused before screening passed
    options: dict[str, Any] = field(default_factory=dict)
    retrieved_at: str | None = None
    unresponsive: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    retry_after: int | None = None  # seconds, for refused_rate and for engines cooling down

    @property
    def ok(self) -> bool:
        return self.status in ANSWERED

    def citations(self) -> list[dict[str, Any]]:
        """data.citations for a task result: numbered, with where each came from and when."""
        return [{"citation": r["citation"], "rank": r["rank"], "result_id": r.get("result_id"), "url": r["url"],
                 "domain": r["domain"], "title": r["title"], "type": r["type"], "provider": r["source"]["provider"],
                 "engines": r["source"]["engines"], "published_at": r["published_at"],
                 "retrieved_at": r["retrieved_at"], "flags": r["flags"]} for r in self.results]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def clean_text(value: object, cap: int) -> tuple[str, list[str]]:
    """(cleaned text, flags) for one web string, in the order of the module docstring."""
    text = value if isinstance(value, str) else ""
    text = text[:cap]
    text = "".join(ch if ch.isprintable() else " " for ch in strip_invisible(text))  # NUL and controls too
    cleaned = brain_text.clean(_SPACE.sub(" ", text).strip(), "chat")
    out = cleaned.text[:cap].rstrip()  # a mask can be longer than what it replaces ("[email]" for "a@b.co")
    flags: list[str] = []
    if cleaned.restricted:
        flags.append("withheld")
    elif cleaned.secret_types or cleaned.masked:
        flags.append("redacted")
    if out and brain_text.instruction_flags(out):
        flags.append("instruction_like")
    return out, flags


def normalize_url(value: object) -> str | None:
    """The URL in its normal form, or None when it is not a plain http(s) link."""
    if not isinstance(value, str) or not value or len(value) > URL_CAP:
        return None
    raw = strip_invisible(value).strip()
    if any(ch.isspace() or not ch.isprintable() for ch in raw):
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").strip(".")
    if scheme not in DEFAULT_PORTS or not host or parts.username is not None or parts.password is not None:
        return None
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port in (None, DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING.match(k)])
    url = urlunsplit((scheme, netloc, parts.path.rstrip("/"), query, ""))
    return url if len(url) <= URL_CAP else None


def url_sha256(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def domain_of(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def _engines(value: object, fallback: object = None) -> list[str]:
    names = value if isinstance(value, list) else [fallback] if fallback else []
    out = {str(n).lower() for n in names if isinstance(n, str) and _ENGINE.fullmatch(str(n).lower())}
    return sorted(out)[:ENGINES_CAP]


def _score(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return max(-SCORE_CAP, min(SCORE_CAP, float(value)))


def _date(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


def _result(url: object, title: object, snippet: object, kind: str, engines: list[str], published: object,
            score: object) -> dict[str, Any] | None:
    link = normalize_url(url)
    if link is None:
        return None
    check = brain_text.clean(link, "chat")
    if check.text != link or check.restricted:
        return None
    title_text, title_flags = clean_text(title, TITLE_CAP)
    snippet_text, snippet_flags = clean_text(snippet, SNIPPET_CAP)
    return {"url": link, "domain": domain_of(link), "title": title_text, "snippet": snippet_text, "type": kind,
            "engines": engines, "published_at": _date(published), "provider_score": _score(score),
            "flags": sorted(set(title_flags) | set(snippet_flags))}


def searxng_results(data: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """(normalized results in SearXNG's order, how many entries were dropped) from a validated JSON answer.

    Only results and infoboxes are read; each infobox (Wikipedia) becomes one "reference" result.
    """
    found: list[dict[str, Any]] = []
    dropped = 0
    for item in data.get("results") or []:
        entry = _result(item.get("url"), item.get("title"), item.get("content"), "web",
                        _engines(item.get("engines"), item.get("engine")), item.get("publishedDate"),
                        item.get("score")) if isinstance(item, dict) else None
        if entry is None:
            dropped += 1
        else:
            found.append(entry)
    for box in data.get("infoboxes") or []:
        entry = None
        if isinstance(box, dict):
            urls = box.get("urls") if isinstance(box.get("urls"), list) else []
            link = next((u.get("url") for u in urls if isinstance(u, dict) and isinstance(u.get("url"), str)),
                        box.get("id"))
            entry = _result(link, box.get("infobox"), box.get("content"), "reference",
                            _engines(box.get("engines"), box.get("engine")), None, None)
        if entry is None:
            dropped += 1
        else:
            found.append(entry)
    return dedupe(found), dropped


def dedupe(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per normalized URL, the first one's text, every engine that found it."""
    by_url: dict[str, dict[str, Any]] = {}
    for r in results:
        seen = by_url.get(r["url"])
        if seen is None:
            by_url[r["url"]] = dict(r)
        else:
            seen["engines"] = sorted(set(seen["engines"]) | set(r["engines"]))[:ENGINES_CAP]
            seen["flags"] = sorted(set(seen["flags"]) | set(r["flags"]))
    return list(by_url.values())


def drop_blocked(results: list[dict[str, Any]], blocked_domains: tuple[str, ...]) -> list[dict[str, Any]]:
    """Without results from a blocked domain or one of its subdomains."""
    blocked = tuple(d.lower().strip(".") for d in blocked_domains if d)
    return [r for r in results if not any(r["domain"] == d or r["domain"].endswith("." + d) for d in blocked)]


def numbered(results: list[dict[str, Any]], *, provider: str, retrieved_at: str, count: int) -> list[dict[str, Any]]:
    """The first count results in the response's shape, numbered [1], [2], ... after de-duplication."""
    out = []
    for rank, r in enumerate(results[:count], 1):
        out.append({"rank": rank, "citation": f"[{rank}]", "result_id": None, "url": r["url"], "domain": r["domain"],
                    "title": r["title"], "snippet": r["snippet"], "type": r["type"],
                    "source": {"provider": provider, "engines": r["engines"], "site_name": None},
                    "published_at": r["published_at"], "retrieved_at": retrieved_at,
                    "provider_score": r["provider_score"], "authority": None, "flags": r["flags"]})
    return out
