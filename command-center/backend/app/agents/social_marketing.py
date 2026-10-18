"""Social Media & Marketing: social drafts, publishing through Postonomous, and ad performance."""

from __future__ import annotations

from .ads import AdsCapabilities
from .base import BaseAgent, Intent
from .content import SocialCapabilities


class SocialMarketingAgent(SocialCapabilities, AdsCapabilities, BaseAgent):
    name = "social_marketing"
    title = "Social Media & Marketing (Postonomous)"
    description = (
        "Drafts social posts, publishes approved posts through Postonomous, and reports ad performance with budget "
        "proposals. Publishing and budget changes need approval; both are mocks in this build, and ad figures are "
        "sample data until an ad account is connected."
    )
    integrations = ("postonomous", "ads")

    def intents(self) -> list[Intent]:
        return self.social_intents() + self.ads_intents()
