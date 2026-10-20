"""The mock provider (research mode "mock", the default provider): fixed answers in SearXNG's JSON shape, made here.

Nothing leaves the process and no HTTP client exists in this module, so mock mode can never reach the live service,
whatever is configured. The answers go through the same validation, cleaning and storage as live ones.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .base import NOT_SUPPORTED, ProviderFetchResult, ProviderHealth, ProviderSearchResult, SearchRequest

MOCK_VERSION = "mock"


def answer(request: SearchRequest) -> dict[str, Any]:
    """Three web results and one reference result on reserved example domains, the same for the same query."""
    tag = hashlib.sha256(request.query.lower().encode("utf-8")).hexdigest()[:8]
    engines = [e for e in request.engines if e != "wikipedia"] or list(request.engines)
    results = [{"url": f"https://example.com/research/{tag}/{n}", "title": f"Example result {n} (mock provider)",
                "content": "A fixed result from the mock research provider. Nothing was searched on the web.",
                "engine": engines[0], "engines": engines, "score": float(4 - n), "publishedDate": None,
                "category": "general"} for n in (1, 2, 3)]
    infoboxes = []
    if "wikipedia" in request.engines:
        infoboxes.append({"infobox": "Example reference (mock provider)", "id": f"https://example.org/wiki/{tag}",
                          "content": "A fixed reference entry from the mock research provider.",
                          "urls": [{"title": "Wikipedia", "url": f"https://example.org/wiki/{tag}"}],
                          "engine": "wikipedia", "engines": ["wikipedia"]})
    return {"query": request.query, "results": results, "answers": [], "corrections": [], "infoboxes": infoboxes,
            "suggestions": [], "unresponsive_engines": []}


class MockProvider:
    """Keeps nothing: the worker holds one for its whole life, and a query must not outlive its retention in memory."""

    name = "mock"

    async def search(self, request: SearchRequest) -> ProviderSearchResult:
        return ProviderSearchResult(status="ok", http_status=200, data=answer(request), sent=True, attempts=1,
                                    latency_ms=0, version=MOCK_VERSION)

    async def fetch(self, url: str) -> ProviderFetchResult:
        return NOT_SUPPORTED

    async def health(self, *, with_config: bool = False) -> ProviderHealth:
        return ProviderHealth(ok=True, config={"version": MOCK_VERSION, "engines": []} if with_config else None)
