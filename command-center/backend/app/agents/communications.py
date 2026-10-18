"""Email & Customer Communications: leads, proposals and client messages.

Outgoing email (proposals, replies) is an external action: it waits for a human
approval and, in this build, goes to the mock email adapter (the Outbox).
"""

from __future__ import annotations

from .base import BaseAgent, Intent
from .sales import SalesCapabilities
from .support import SupportCapabilities


class CommunicationsAgent(SalesCapabilities, SupportCapabilities, BaseAgent):
    name = "communications"
    title = "Email & Customer Communications"
    description = (
        "Scores incoming leads and drafts proposals; triages client messages (category, priority, sentiment) and drafts "
        "replies. Nothing is emailed without approval, and email is a mock in this build (recorded in the Outbox)."
    )
    integrations = ("email",)

    def intents(self) -> list[Intent]:
        return self.sales_intents() + self.support_intents()
