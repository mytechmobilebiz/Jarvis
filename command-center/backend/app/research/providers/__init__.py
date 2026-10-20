"""Research providers: the mock (the default) and the private SearXNG container. This build has no other provider: no
DeepSeek, no page fetching."""

from __future__ import annotations

import httpx

from .base import ProviderFetchResult, ProviderHealth, ProviderSearchResult, ResearchProvider, SearchRequest
from .mock import MockProvider
from .searxng import SearXNGProvider

__all__ = ["MockProvider", "ProviderFetchResult", "ProviderHealth", "ProviderSearchResult", "ResearchProvider",
           "SearXNGProvider", "SearchRequest", "for_mode"]


def for_mode(mode: str, *, transport: httpx.AsyncBaseTransport | None = None) -> ResearchProvider:
    """The provider for research mode "mock" or "live"; anything else is refused."""
    if mode == "mock":
        return MockProvider()
    if mode == "live":
        return SearXNGProvider(transport=transport)
    raise ValueError("research is off")
