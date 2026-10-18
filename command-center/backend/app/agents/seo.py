"""SEO & Research: read-only on-page audits and keyword briefs."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

from pydantic import Field, field_validator

from .base import AgentContext, AgentResult, ArtifactSpec, Finding, Intent, Params, Risk, capability
from .drafting import write
from .monitoring.agent import normalize_url


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self.links: list[dict[str, str]] = []
        self.h1: list[str] = []
        self.h2 = 0
        self.images = 0
        self.images_no_alt = 0
        self.lang = ""
        self.jsonld = 0
        self.text_parts: list[str] = []
        self._in: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        self._in.append(tag)
        if tag == "html":
            self.lang = a.get("lang", "")
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key:
                self.meta[key] = a.get("content", "")
        elif tag == "link":
            self.links.append(a)
        elif tag == "img":
            self.images += 1
            if not a.get("alt", "").strip():
                self.images_no_alt += 1
        elif tag == "h2":
            self.h2 += 1
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self.jsonld += 1

    def handle_endtag(self, tag: str) -> None:
        while self._in and self._in.pop() != tag:
            pass

    def handle_data(self, data: str) -> None:
        current = self._in[-1] if self._in else ""
        if current == "title":
            self.title += data
        elif current == "h1":
            self.h1.append(data.strip())
        if current not in ("script", "style", "title", "noscript"):
            self.text_parts.append(data)

    @property
    def word_count(self) -> int:
        return len(re.findall(r"\w+", " ".join(self.text_parts)))


class AuditParams(Params):
    url: str = Field(description="Page to audit, e.g. https://example.com/services")

    @field_validator("url")
    @classmethod
    def _norm(cls, v: str) -> str:
        return normalize_url(v)


class BriefParams(Params):
    topic: str = Field(min_length=3, max_length=200, description="Service or subject, e.g. 'home care websites in Atlanta'")
    location: str = Field("", max_length=100)


def audit_findings(p: PageParser, status: int, final_url: str) -> tuple[list[Finding], int]:
    f: list[Finding] = []

    def add(code: str, severity: str, title: str, detail: str = "", cost: int = 0) -> None:
        f.append(Finding(code=code, severity=severity, title=title, detail=detail))  # type: ignore[arg-type]
        nonlocal score
        score -= cost

    score = 100
    title = re.sub(r"\s+", " ", p.title).strip()
    desc = p.meta.get("description", "").strip()
    if status >= 400:
        add("http_error", "critical", f"Page answers HTTP {status}", cost=40)
    if not title:
        add("no_title", "critical", "No <title>", cost=20)
    elif not 15 <= len(title) <= 65:
        add("title_length", "warning", f"Title is {len(title)} characters (aim for 15-65)", title, cost=5)
    if not desc:
        add("no_meta_description", "warning", "No meta description", cost=10)
    elif not 50 <= len(desc) <= 160:
        add("description_length", "info", f"Meta description is {len(desc)} characters (aim for 50-160)", cost=2)
    if "noindex" in p.meta.get("robots", "").lower():
        add("noindex", "critical", "The page tells search engines not to index it (robots noindex)", cost=30)
    if not p.h1:
        add("no_h1", "warning", "No <h1> heading", cost=8)
    elif len(p.h1) > 1:
        add("multiple_h1", "info", f"{len(p.h1)} <h1> headings (one is clearer)", cost=2)
    canonical = next((l.get("href", "") for l in p.links if "canonical" in l.get("rel", "").lower()), "")
    if not canonical:
        add("no_canonical", "info", "No canonical link", cost=2)
    elif urlsplit(urljoin(final_url, canonical)).netloc != urlsplit(final_url).netloc:
        add("canonical_offsite", "warning", "Canonical points to another domain", canonical, cost=10)
    if "viewport" not in p.meta:
        add("no_viewport", "warning", "No mobile viewport meta tag", cost=8)
    if not p.lang:
        add("no_lang", "info", "No lang attribute on <html>", cost=1)
    if p.images_no_alt:
        add("img_alt", "warning" if p.images_no_alt > 3 else "info", f"{p.images_no_alt} of {p.images} images have no alt text", cost=min(10, p.images_no_alt))
    if p.word_count < 300:
        add("thin_content", "warning", f"Only about {p.word_count} words of text", cost=8)
    if not p.jsonld:
        add("no_structured_data", "info", "No structured data (JSON-LD)", cost=3)
    if "og:title" not in p.meta:
        add("no_open_graph", "info", "No Open Graph tags for social sharing", cost=2)
    if urlsplit(final_url).scheme != "https":
        add("no_https", "warning", "Page is not served over HTTPS", cost=10)
    return f, max(score, 0)


class SEOCapabilities:
    """Read-only SEO audits and keyword briefs; part of the SEO & Content agent."""

    name: str

    def seo_intents(self) -> list[Intent]:
        return [
            Intent("audit_page", (r"\bseo\b", r"\baudit\b", r"\bon-?page\b", r"\bmeta (tags?|description)\b"),
                   lambda text, urls: [{"url": u} for u in urls] or None, weight=2),
            Intent("keyword_brief", (r"\bkeywords?\b", r"\bbrief\b.*\b(seo|content|keyword)", r"\bresearch\b"),
                   lambda text, urls: None if urls else {"topic": re.sub(r"(?i)^.*?(keywords?|research|brief)\s*(for|on|about)?\s*", "", text).strip(" .?!")[:200] or text[:200]}),
        ]

    @capability(risk=Risk.READ, description="Audit one page's on-page SEO (title, description, headings, indexability, images, structured data).",
                params=AuditParams, examples=("SEO audit of example.com",))
    async def audit_page(self, ctx: AgentContext, params: AuditParams) -> AgentResult:
        page = await ctx.http.fetch(params.url)
        parser = PageParser()
        parser.feed(page.text)
        findings, score = audit_findings(parser, page.status, page.final_url)
        origin = f"{urlsplit(page.final_url).scheme}://{urlsplit(page.final_url).netloc}/"
        extras: dict[str, Any] = {}
        for name in ("robots.txt", "sitemap.xml", "sitemap_index.xml", "wp-sitemap.xml"):
            try:
                r = await ctx.http.fetch(urljoin(origin, name), max_bytes=50_000)
                extras[name] = r.status == 200
            except Exception:  # noqa: BLE001 - optional probes
                extras[name] = False
        if not extras["robots.txt"]:
            findings.append(Finding(code="no_robots_txt", severity="info", title="No robots.txt"))
        if not any(extras[k] for k in ("sitemap.xml", "sitemap_index.xml", "wp-sitemap.xml")):
            findings.append(Finding(code="no_sitemap", severity="warning", title="No XML sitemap found at the usual addresses"))
            score = max(score - 5, 0)
        lines = [f"# SEO audit: {page.final_url}", "", f"Score: **{score}/100** (higher is better; a quick on-page check, not a ranking prediction)", "",
                 f"- Title: {parser.title.strip() or '(none)'}", f"- Meta description: {parser.meta.get('description', '(none)')}",
                 f"- H1: {', '.join(parser.h1) or '(none)'}", f"- Words: about {parser.word_count}", "", "## Findings"]
        order = {"critical": 0, "warning": 1, "info": 2}
        lines += [f"- **{x.severity}**: {x.title}" + (f" ({x.detail})" if x.detail else "") for x in sorted(findings, key=lambda x: order[x.severity])]
        return AgentResult(
            summary=f"SEO score {score}/100 for {page.final_url}: " + "; ".join(x.title for x in findings if x.severity != "info")[:300],
            data={"url": page.final_url, "score": score, "title": parser.title.strip(), "word_count": parser.word_count, "files": extras},
            findings=findings,
            artifacts=[ArtifactSpec(kind="seo_audit", title=f"SEO audit - {urlsplit(page.final_url).netloc}", content="\n".join(lines),
                                    data={"url": page.final_url, "score": score})],
        )

    @capability(risk=Risk.DRAFT, description="Write a keyword and content brief for a service or topic (a draft; no live keyword data).",
                params=BriefParams, examples=("Keyword research for home care websites in Atlanta",))
    async def keyword_brief(self, ctx: AgentContext, params: BriefParams) -> AgentResult:
        where = f" in {params.location}" if params.location else ""

        def template() -> str:
            t = params.topic
            return "\n".join([
                f"# Keyword brief: {t}{where}", "",
                "_Template draft: no AI model or keyword-volume source is connected, so these are starting points to validate, not data._", "",
                "## Seed keywords", f"- {t}{where}", f"- best {t}{where}", f"- {t} cost", f"- {t} near me", f"- affordable {t}", "",
                "## Questions to answer on the page", f"- How much does {t} cost?", f"- How long does {t} take?", f"- What should I look for in {t}?", "",
                "## Suggested page outline", f"1. H1: {t.capitalize()}{where}", "2. Who it is for and the problem it solves", "3. Packages and pricing",
                "4. Process and timeline", "5. Proof: portfolio and reviews", "6. FAQ (the questions above)", "7. Call to action",
            ])

        text, writer = await write(
            ctx, purpose="keyword_brief",
            system="You are an SEO strategist at Infinite Marketing, a web design and digital marketing agency. Write a concise keyword and "
                   "content brief in Markdown: seed and long-tail keywords grouped by intent, questions to answer, and a page outline. "
                   "You have no live search-volume data: never invent volumes or rankings; say what to validate.",
            prompt=f"Topic: {params.topic}\nLocation: {params.location or 'not specified'}",
            fallback=template,
        )
        return AgentResult(summary=f"Keyword brief for '{params.topic}{where}' ({writer})",
                           artifacts=[ArtifactSpec(kind="keyword_brief", title=f"Keyword brief - {params.topic}", content=text, data={"writer": writer})],
                           data={"writer": writer})
