"""The set of agent modules this deployment runs."""

from __future__ import annotations

from .base import BaseAgent

_AGENTS: dict[str, BaseAgent] | None = None


def _build() -> dict[str, BaseAgent]:
    from ..supervisor.agent import SupervisorAgent
    from .ads import AdsAgent
    from .content import ContentAgent
    from .credit_repair import CreditRepairAgent
    from .sales import SalesAgent
    from .seo import SEOAgent
    from .support import SupportAgent
    from .wordpress_monitor.agent import WordPressMonitorAgent

    # The supervisor and the seven specialist modules.
    agents: list[BaseAgent] = [
        SupervisorAgent(),
        WordPressMonitorAgent(),
        SalesAgent(),
        SupportAgent(),
        ContentAgent(),
        SEOAgent(),
        AdsAgent(),
        CreditRepairAgent(),
    ]
    return {a.name: a for a in agents}


def all_agents() -> dict[str, BaseAgent]:
    global _AGENTS
    if _AGENTS is None:
        _AGENTS = _build()
    return _AGENTS


def get_agent(name: str) -> BaseAgent | None:
    return all_agents().get(name)


def specialist_agents() -> dict[str, BaseAgent]:
    """Every agent the supervisor can delegate to (i.e. all but itself)."""
    return {n: a for n, a in all_agents().items() if n != "supervisor"}
