"""External integrations available to agents.

Every adapter that would act on the outside world (send, publish, spend) is a
*mock* in this build: it records what it would have done in the Command Center
database and sends nothing. Each one also refuses to run at all unless the
task carrying it was approved by a human (ApprovalRequired), so even the mock
path exercises the approval gate the live adapter will need.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from ..config import Settings

if TYPE_CHECKING:
    from ..agents.base import AgentContext

Mode = Literal["mock", "live", "disabled"]


class ApprovalRequired(PermissionError):
    pass


class Integration:
    name: str = ""
    title: str = ""
    mode: Mode = "mock"
    description: str = ""
    # What a live version needs before it can be switched on.
    live_requirements: str = ""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def require_approval(self, ctx: AgentContext) -> None:
        if not ctx.approved_by:
            raise ApprovalRequired(f"{self.title}: this action needs a human approval first")

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "mode": self.mode,
            "description": self.description,
            "live_requirements": self.live_requirements,
        }


@dataclass
class IntegrationHub:
    settings: Settings
    adapters: dict[str, Integration] = field(default_factory=dict)

    def register(self, adapter: Integration) -> None:
        self.adapters[adapter.name] = adapter

    def get(self, name: str) -> Integration:
        return self.adapters[name]

    def describe(self) -> list[dict[str, Any]]:
        return [a.describe() for a in self.adapters.values()]


def build_hub(settings: Settings) -> IntegrationHub:
    from . import mocks

    hub = IntegrationHub(settings)
    for cls in mocks.ADAPTERS:
        hub.register(cls(settings))
    return hub
