"""Web research capabilities (plan section 3.18): search only, risk READ, through the Research Gateway (app/research).

Each one declares ResearchUse, so the worker gives it the reduced context: ctx.research and nothing that could put
host names, command output, memory or client data into a query (no SSH, integrations, AI model or HTTP client). The
query is what a person typed (a direct run, screened by app/api/work.py before the task exists) or words of the
operator's own request (a Supervisor step, screened by app/supervisor/agent.py); the gateway screens it again.

What comes back is untrusted reference data. The task's summary is a fixed line ("Web search #123: 7 sources"), the
only part an AI summary ever sees; the citations go in data.citations, a list the dashboard shows apart from any
reply. No research capability creates findings, events, artifacts or tasks, and a refusal is a result with a reason
code, never an exception.

  supervisor          web_search                   (and the admin's connectivity test, internal)
  seo_content         research_topic
  social_marketing    trend_research
  wordpress_security  check_advisories             a software name and version a person typed, never scan output
  server_management   check_advisories
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, field_validator

from ..research.gateway import LANGUAGE
from ..research.normalize import MAX_RESULTS
from .base import AgentContext, AgentResult, NoParams, Params, ResearchUse, Risk, capability

if TYPE_CHECKING:
    from ..research import ResearchResponse

TIMEOUT_SECONDS = 90
CVE_ID = re.compile(r"^CVE-\d{4}-\d{4,}$")
# A version as the screen's class S reads one ("6.9", "8.2.1", "v3", "1.2.0-rc1").
VERSION = re.compile(r"^(?:v[0-9]+|[0-9]+(?:\.[0-9]+){1,3})(?:[-.]?(?:alpha|beta|rc|p)[0-9]*)?$", re.I)
SOFTWARE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+\-]*$")
ADVISORY_WORDS = "security advisory"  # fixed words added to a typed software name and version
CONNECTIVITY_QUERY = "Wikipedia"  # the connectivity test's fixed, general query
SEARCH = ResearchUse(search=True)


class SearchParams(Params):
    """Parameters of a research capability. query_text() is what is screened and sent; typed_text() the person's own
    words in it (the Supervisor's query-from-request rule compares those with the request)."""

    count: int = Field(10, ge=1, le=MAX_RESULTS, description="How many results (1-20)")
    language: str = Field("en-US", max_length=10, description="Result language, e.g. en-US")
    time_range: Literal["day", "week", "month", "year"] | None = Field(None, description="Only recent pages")

    @field_validator("language")
    @classmethod
    def _language(cls, value: str) -> str:
        if not LANGUAGE.fullmatch(value):
            raise ValueError("a language code such as en-US")
        return value

    def query_text(self) -> str:
        raise NotImplementedError

    def typed_text(self) -> str:
        return self.query_text()

    def screened(self, canonical: str) -> dict[str, Any]:
        """The parameters to store once the query passed the screen, the query in its canonical form."""
        return self.model_dump(mode="json")


class ResearchParams(SearchParams):
    query: str = Field(min_length=1, max_length=256, description="What to search for, in your own words (at most 128 "
                       "characters); never names, contact details or anything private")

    def query_text(self) -> str:
        return self.query

    def screened(self, canonical: str) -> dict[str, Any]:
        return {**self.model_dump(mode="json"), "query": canonical}


class TrendParams(ResearchParams):
    time_range: Literal["day", "week", "month", "year"] | None = Field("month", description="Only recent pages")


class AdvisoryParams(SearchParams):
    software: str = Field(min_length=2, max_length=60, description="A public software name as you typed it, e.g. "
                          "WordPress, OpenSSH, Contact Form 7 (never a client's custom plugin or a host name)")
    version: str = Field(min_length=1, max_length=30, description="Its version, e.g. 6.9 or 9.6p1")

    @field_validator("software")
    @classmethod
    def _software(cls, value: str) -> str:
        value = " ".join(value.split())
        if not SOFTWARE.fullmatch(value):
            raise ValueError("letters, digits, spaces and . _ + - only")
        return value

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        value = value.strip()
        if not VERSION.fullmatch(value):
            raise ValueError("a version number such as 6.9, 8.2.1 or 9.6p1")
        return value

    def query_text(self) -> str:
        return f"{self.software} {self.version} {ADVISORY_WORDS}"

    def typed_text(self) -> str:
        return f"{self.software} {self.version}"


def research_data(res: ResearchResponse) -> dict[str, Any]:
    """The search's outcome in codes and counts (the query text stays in the task's parameters only)."""
    return {"query_id": res.query_id, "status": res.status, "reason": res.reason, "provider": res.provider,
            "provider_version": res.provider_version, "cached": res.cached, "retrieved_at": res.retrieved_at,
            "result_count": len(res.results), "unresponsive": res.unresponsive, "flags": res.flags,
            "retry_after": res.retry_after}


def summary_line(label: str, res: ResearchResponse) -> str:
    """The fixed line an AI summary may see: numbers and codes, never web text or the query."""
    number = f" #{res.query_id}" if res.query_id is not None else ""
    if res.ok:
        return f"{label}{number}: {len(res.results)} sources"
    if res.status in ("blocked", "refused_policy", "refused_budget", "refused_rate"):
        return f"{label}{number} refused: {res.reason or res.status}"
    return f"{label}{number}: {res.status}" + (f" ({res.reason})" if res.reason else "")


def refused(label: str, reason: str) -> AgentResult:
    return AgentResult(summary=f"{label} refused: {reason}",
                       data={"citations": [], "research": {"status": "refused_policy", "reason": reason}})


async def search(ctx: AgentContext, label: str, params: SearchParams, *, cves: bool = False) -> AgentResult:
    if ctx.research is None:  # never for Credit Repair, nor for a capability that did not declare research
        return refused(label, "no_research_client")
    res = await ctx.research.search(params.query_text(), count=params.count, language=params.language,
                                    time_range=params.time_range)
    data: dict[str, Any] = {"citations": res.citations(), "research": research_data(res)}
    if cves:
        data["cves"] = cve_ids(res)
    return AgentResult(summary=summary_line(label, res), data=data)


def cve_ids(res: ResearchResponse) -> list[str]:
    """CVE ids in the results' titles and snippets: only whole words that match ^CVE-\\d{4}-\\d{4,}$, nothing else."""
    found: set[str] = set()
    for r in res.results:
        for word in re.split(r"[^A-Za-z0-9\-]+", f"{r.get('title', '')} {r.get('snippet', '')}"):
            if CVE_ID.fullmatch(word.upper()):
                found.add(word.upper())
    return sorted(found)[:50]


class WebSearchCapabilities:
    """The Supervisor's web search, and the admin's connectivity test (internal: app/tasks.py RESEARCH_TEST)."""

    @capability(risk=Risk.READ, research=SEARCH, params=ResearchParams, timeout_seconds=TIMEOUT_SECONDS,
                description="Search the public web (the private SearXNG service) and return numbered citations, "
                "unverified. The query must use the operator's own words; nothing is summarised by an AI model.",
                examples=("Search the web for WordPress 6.9 release notes",))
    async def web_search(self, ctx: AgentContext, params: ResearchParams) -> AgentResult:
        return await search(ctx, "Web search", params)

    @capability(risk=Risk.READ, research=SEARCH, params=NoParams, timeout_seconds=TIMEOUT_SECONDS,
                description="Check that the Research Gateway reaches its search service with one fixed search "
                "(internal: an admin starts it from the Research Center).")
    async def research_connectivity_test(self, ctx: AgentContext, params: NoParams) -> AgentResult:
        if ctx.research is None:
            return refused("Connectivity test", "no_research_client")
        res = await ctx.research.search(CONNECTIVITY_QUERY, count=3)
        return AgentResult(summary=summary_line("Connectivity test", res),
                           data={"citations": res.citations(), "research": research_data(res)})


class TopicResearchCapabilities:
    """SEO & Content: research a topic on the public web (citations only; drafts never use web text)."""

    @capability(risk=Risk.READ, research=SEARCH, params=ResearchParams, timeout_seconds=TIMEOUT_SECONDS,
                description="Research a topic on the public web for content planning: numbered citations, unverified "
                "(no AI summary of web text).",
                examples=("Research the topic 'home care marketing trends' on the web",))
    async def research_topic(self, ctx: AgentContext, params: ResearchParams) -> AgentResult:
        return await search(ctx, "Topic research", params)


class TrendResearchCapabilities:
    """Social Media & Marketing: recent pages about a public trend (citations only)."""

    @capability(risk=Risk.READ, research=SEARCH, params=TrendParams, timeout_seconds=TIMEOUT_SECONDS,
                description="Search the public web for recent pages about a trend or topic (the last month unless set "
                "otherwise): numbered citations, unverified.",
                examples=("Trend research: short video marketing for dentists",))
    async def trend_research(self, ctx: AgentContext, params: TrendParams) -> AgentResult:
        return await search(ctx, "Trend research", params)


class AdvisoryCapabilities:
    """WordPress Security and Server Management: public security advisories for software a person names.

    The software name and version are typed by a person, never taken from a scan, a site or a server (custom plugin
    and theme names often carry the client's name). CVE ids are read from the results only when a whole word matches
    ^CVE-\\d{4}-\\d{4,}$. No finding or event is ever made from web text: findings reach the AI summary and warning
    events reach n8n.
    """

    @capability(risk=Risk.READ, research=SEARCH, params=AdvisoryParams, timeout_seconds=TIMEOUT_SECONDS,
                description="Search the public web for security advisories about a software name and version you "
                "type (never taken from a scan): numbered citations and the CVE ids they mention, unverified.",
                examples=("Check advisories for WordPress 6.9",))
    async def check_advisories(self, ctx: AgentContext, params: AdvisoryParams) -> AgentResult:
        return await search(ctx, "Advisory search", params, cves=True)
