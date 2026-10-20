"""The Research Gateway: web search for the agents through the private SearXNG container (plan RESEARCH_GATEWAY_PLAN.md).

A capability that declares research gets a ResearchClient as ctx.research. The client is tied to one agent and one
task, and through the task to its trigger, which the gateway reads from the database on every call. It offers
search() and fetch(); fetch() always refuses in this release. It holds no key, no database session and no other way
out: everything goes through app/research/gateway.py, which screens, counts, stores and refuses.

What comes back is untrusted reference data with citations. It never authorizes anything: no server command,
customer e-mail, WordPress change, payment or workflow follows from it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .normalize import ResearchResponse

if TYPE_CHECKING:
    from .gateway import Gateway

__all__ = ["ResearchClient", "ResearchResponse"]


class ResearchClient:
    __slots__ = ("_gateway", "agent", "task_id")

    def __init__(self, gateway: Gateway, *, agent: str, task_id: int | None) -> None:
        self._gateway = gateway
        self.agent = agent
        self.task_id = task_id

    async def search(self, query: str, *, count: int = 10, language: str = "en-US", time_range: str | None = None,
                     page: int = 1) -> ResearchResponse:
        """Searches the web for query (refused with a reason code when anything does not allow it)."""
        return await self._gateway.search(agent=self.agent, task_id=self.task_id, query=query, count=count,
                                          language=language, time_range=time_range, page=page)

    async def fetch(self, url: str) -> ResearchResponse:
        """Page fetching is not part of this release: always refused with not_supported."""
        return await self._gateway.fetch(agent=self.agent, task_id=self.task_id, url=url)
