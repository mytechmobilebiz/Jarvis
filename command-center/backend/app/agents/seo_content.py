"""SEO & Content: read-only SEO audits, keyword briefs, blog drafts and WordPress publishing (approval, mock).

A publish approval names the site (name, address, environment) and the approved
blog draft, and binds the draft's content; publishing to a production site
(or a site on a production server) needs an admin who types the site's name,
as repairs do.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .. import approvals
from .. import tasks as task_service
from ..models import Server, Site
from .base import AgentContext, AgentResult, BaseAgent, Intent
from .content import ContentCapabilities
from .seo import SEOCapabilities


async def publish_context(session: AsyncSession, params: dict[str, Any], *, lock: bool = False) -> dict[str, Any]:
    """What an approval to publish a blog draft to a client's WordPress site is about (see BaseAgent.approval_context)."""
    if params.get("destination") != "wordpress":
        raise ValueError("this agent publishes to WordPress only; social posts go through Social Media & Marketing")
    draft = await approvals.load_draft(session, params.get("artifact_id"), kinds=("blog_draft",), what="blog draft", lock=lock)
    if draft.status != "approved":
        raise ValueError(f"blog draft #{draft.id} has not been approved yet: review it and mark it approved in Drafts & reports first")
    site = await session.get(Site, params["site_id"], with_for_update=lock) if params.get("site_id") else None
    if site is None:
        raise ValueError("publishing to WordPress needs the site_id of a monitored site" if not params.get("site_id")
                         else f"no monitored site with id {params['site_id']}")
    server = await session.get(Server, site.server_id) if site.server_id else None
    production = site.environment == "production" or (server is not None and server.environment == "production")
    facts = approvals.draft_facts(draft)
    return {
        "target": f'site {site.name} ({site.url}): a draft post "{draft.title}" from blog draft #{draft.id}',
        "environment": "production" if production else site.environment,
        "production": production,
        "confirm": site.name,
        "delivery": approvals.delivery("wordpress_publish"),
        "draft": facts,
        "binding": {"site_id": site.id, "name": site.name, "url": site.url, "artifact_id": draft.id, "draft_sha256": facts["sha256"],
                    "delivery": approvals.delivery("wordpress_publish")},  # approved as a mock: never published for real
    }


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

    async def approval_context(self, session: AsyncSession, capability: str, params: dict[str, Any], *,
                               lock: bool = False) -> dict[str, Any]:
        return await publish_context(session, params, lock=lock) if capability == "publish_draft" else {}

    async def run(self, ctx: AgentContext, capability_name: str, raw_params: dict[str, Any]) -> AgentResult:
        if capability_name == "publish_draft":
            await task_service.recheck_target(self, ctx, capability_name, raw_params)  # in this session, rows locked
        return await super().run(ctx, capability_name, raw_params)
