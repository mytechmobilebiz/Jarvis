"""The set of agent modules this deployment runs: the AI Supervisor and the seven official agents."""

from __future__ import annotations

from .base import BaseAgent

_AGENTS: dict[str, BaseAgent] | None = None

SUPERVISOR = "supervisor"
# The seven official agents, in the owner's order.
OFFICIAL_ORDER = (
    "wordpress_security",
    "seo_content",
    "social_marketing",
    "server_management",
    "communications",
    "monitoring",
    "credit_repair",
)


def _build() -> dict[str, BaseAgent]:
    from ..supervisor.agent import SupervisorAgent
    from .communications import CommunicationsAgent
    from .credit_repair import CreditRepairAgent
    from .monitoring.agent import MonitoringAgent
    from .seo_content import SEOContentAgent
    from .server_management import ServerManagementAgent
    from .social_marketing import SocialMarketingAgent
    from .wordpress_security import WordPressSecurityAgent

    agents: list[BaseAgent] = [
        SupervisorAgent(),
        WordPressSecurityAgent(),
        SEOContentAgent(),
        SocialMarketingAgent(),
        ServerManagementAgent(),
        CommunicationsAgent(),
        MonitoringAgent(),
        CreditRepairAgent(),
    ]
    built = {a.name: a for a in agents}
    assert tuple(n for n in built if n != SUPERVISOR) == OFFICIAL_ORDER
    return built


def all_agents() -> dict[str, BaseAgent]:
    global _AGENTS
    if _AGENTS is None:
        _AGENTS = _build()
    return _AGENTS


def get_agent(name: str) -> BaseAgent | None:
    return all_agents().get(name)


def specialist_agents() -> dict[str, BaseAgent]:
    """The seven agents the supervisor can delegate to (every agent but itself)."""
    return {n: a for n, a in all_agents().items() if n != SUPERVISOR}
