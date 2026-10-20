"""The one interface every research provider has (plan section 3.1).

A provider only talks to its service and maps the answer's envelope to a status: it never screens, caches, counts
or stores (the gateway does, app/research/gateway.py). Every provider that uses HTTP takes an injectable httpx
transport, the test pattern of tests/fakes.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class SearchRequest:
    query: str  # the canonical, screened text
    engines: tuple[str, ...]  # the allowed engines that are not cooling down
    language: str = "en-US"
    time_range: str | None = None  # day, week, month or year
    page: int = 1


@dataclass
class ProviderSearchResult:
    status: str  # ok, partial, empty, provider_down, unreachable, bad_request, provider_misconfigured, failed
    reason: str = ""  # a fixed code, never text from the answer
    http_status: int | None = None
    data: dict[str, Any] = field(default_factory=dict)  # the validated JSON answer (results, infoboxes)
    unresponsive: list[dict[str, Any]] = field(default_factory=list)  # {engine, reason, suspended}
    sent: bool = False  # a request reached the service (it counts against the caps)
    attempts: int = 0
    latency_ms: int | None = None
    version: str = ""


@dataclass
class ProviderFetchResult:
    status: str
    reason: str = ""


@dataclass
class ProviderHealth:
    ok: bool
    reason: str = ""
    config: dict[str, Any] | None = None  # SearXNG's /config answer (version, engines)


class ResearchProvider(Protocol):
    name: str  # "mock" or "searxng"

    async def search(self, request: SearchRequest) -> ProviderSearchResult: ...

    async def fetch(self, url: str) -> ProviderFetchResult: ...

    async def health(self, *, with_config: bool = False) -> ProviderHealth: ...


NOT_SUPPORTED = ProviderFetchResult(status="refused_policy", reason="not_supported")
