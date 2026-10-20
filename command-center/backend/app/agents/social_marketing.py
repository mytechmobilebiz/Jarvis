"""Social Media & Marketing: social drafts, publishing through Postonomous, and ad performance.

A publish approval names the platform, the Postonomous account (none can be
connected in this build) and the approved social draft, and binds the draft's
content. Ad figures are synthetic sample data until an ad account is
connected, and every ad finding says so.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .. import approvals
from .. import tasks as task_service
from .ads import AdsCapabilities
from .base import AgentContext, AgentResult, BaseAgent, Finding, Intent
from .content import PLATFORMS, SocialCapabilities
from .research import TrendResearchCapabilities

SAMPLE = "[Sample data]"
# Postonomous has no account settings yet: the live adapter has not been built.
POSTONOMOUS_ACCOUNT = "no Postonomous account connected"


async def social_publish_context(session: AsyncSession, params: dict[str, Any], *, lock: bool = False) -> dict[str, Any]:
    platform = params.get("destination")
    if platform not in PLATFORMS:
        raise ValueError(f"unknown social platform '{platform}' (one of: {', '.join(PLATFORMS)})")
    draft = await approvals.load_draft(session, params.get("artifact_id"), kinds=("social_drafts",), what="social draft", lock=lock)
    if draft.status != "approved":
        raise ValueError(f"social draft #{draft.id} has not been approved yet: review it and mark it approved in Drafts & reports first")
    facts = approvals.draft_facts(draft)
    return {
        "target": f'{platform} through Postonomous ({POSTONOMOUS_ACCOUNT}): social draft #{draft.id} "{draft.title}"',
        "environment": approvals.delivery_note("postonomous"),
        "production": False,
        "delivery": approvals.delivery("postonomous"),
        "draft": facts,
        "binding": {"platform": platform, "account": POSTONOMOUS_ACCOUNT, "artifact_id": draft.id, "draft_sha256": facts["sha256"],
                    "delivery": approvals.delivery("postonomous")},  # approved as a mock: never published for real
    }


def label_sample_findings(result: AgentResult) -> AgentResult:
    """Ad findings from synthetic figures: marked as sample data on their own, and informational (never a warning about real spend)."""
    if not result.data.get("sample_data"):
        return result
    findings = [Finding(code=f.code, severity="info", title=f.title if f.title.startswith(SAMPLE) else f"{SAMPLE} {f.title}",
                        detail=(f.detail + " " if f.detail else "") + "Synthetic figures: no ad account is connected.")
                for f in result.findings]
    return result.model_copy(update={"findings": findings})


class SocialMarketingAgent(TrendResearchCapabilities, SocialCapabilities, AdsCapabilities, BaseAgent):
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

    async def approval_context(self, session: AsyncSession, capability: str, params: dict[str, Any], *,
                               lock: bool = False) -> dict[str, Any]:
        return await social_publish_context(session, params, lock=lock) if capability == "publish_draft" else {}

    async def run(self, ctx: AgentContext, capability_name: str, raw_params: dict[str, Any]) -> AgentResult:
        if capability_name == "publish_draft":
            await task_service.recheck_target(self, ctx, capability_name, raw_params)  # in this session, rows locked
        result = await super().run(ctx, capability_name, raw_params)
        return label_sample_findings(result) if capability_name == "ads_report" else result
