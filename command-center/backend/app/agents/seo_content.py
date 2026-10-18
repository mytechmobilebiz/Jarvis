"""SEO & Content: read-only SEO audits, keyword briefs, blog drafts and WordPress publishing (approval, mock)."""

from __future__ import annotations

from .base import BaseAgent, Intent
from .content import ContentCapabilities
from .seo import SEOCapabilities


class SEOContentAgent(SEOCapabilities, ContentCapabilities, BaseAgent):
    name = "seo_content"
    title = "SEO & Content"
    description = (
        "Read-only on-page SEO audits, keyword and content briefs, and blog drafts in Infinite Marketing's voice. "
        "Publishing to a client's WordPress site waits for approval (mock in this build). Never changes a site by itself."
    )
    integrations = ("wordpress_publish",)

    def intents(self) -> list[Intent]:
        return self.seo_intents() + self.content_intents()
