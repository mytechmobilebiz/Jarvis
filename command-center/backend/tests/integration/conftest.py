"""Fixtures shared by the integration tests."""

from __future__ import annotations

import asyncio

import pytest

from app.agents import registry
from app.agents.base import AgentContext, AgentResult, BaseAgent, Params, Risk, capability


class PublishParams(Params):
    title: str


class GateTestAgent(BaseAgent):
    """A stand-in for an agent with an outward-facing action."""

    name = "gate_test"
    title = "Gate test"
    description = "test agent"
    seen_approvers: list[str] = []

    @capability(risk=Risk.EXTERNAL, description="publish something", params=PublishParams)
    async def publish(self, ctx: AgentContext, params: PublishParams) -> AgentResult:
        type(self).seen_approvers.append(ctx.approved_by)
        return AgentResult(summary=f"would publish {params.title}")

    @capability(risk=Risk.READ, description="sleeps", params=Params)
    async def slow(self, ctx: AgentContext, params: Params) -> AgentResult:
        await asyncio.sleep(5)
        return AgentResult(summary="never")

    @capability(risk=Risk.READ, description="explodes", params=Params)
    async def explode(self, ctx: AgentContext, params: Params) -> AgentResult:
        ctx.log("about to fail")
        raise RuntimeError("kaboom")


@pytest.fixture
def gate_agent(monkeypatch):
    agents = dict(registry.all_agents())
    agents["gate_test"] = GateTestAgent()
    GateTestAgent.seen_approvers = []
    monkeypatch.setattr(registry, "_AGENTS", agents)
    return agents["gate_test"]


