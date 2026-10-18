"""Mock adapters for outward-facing integrations.

Each adapter has the method a live adapter will have, refuses to run without
a human approval on the task, and records what it *would* have done in the
outbox table. Nothing leaves the machine. Making one live means writing the
live adapter, adding its credentials, and the owner's authorization.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ..models import OutboxEntry
from . import Integration

if TYPE_CHECKING:
    from ..agents.base import AgentContext


async def _record(ctx: AgentContext, integration: Integration, agent: str, action: str, *,
                  recipient: str = "", subject: str = "", body: str = "", payload: dict[str, Any] | None = None) -> OutboxEntry:
    integration.require_approval(ctx)
    entry = OutboxEntry(
        task_id=ctx.task_id,
        agent=agent,
        integration=integration.name,
        mode="mock",
        action=action,
        recipient=recipient,
        subject=subject,
        body=body,
        payload=payload or {},
        approved_by=ctx.approved_by,
    )
    ctx.session.add(entry)
    await ctx.session.flush()
    ctx.log(f"{integration.title} (mock): recorded '{action}' as outbox #{entry.id}; nothing was sent")
    return entry


class MockEmail(Integration):
    name = "email"
    title = "Email (SMTP)"
    description = "Proposals and support replies. Mock: approved emails are recorded in the Outbox, not sent."
    live_requirements = "SMTP server, sending account and app password, and the owner's authorization to send."

    async def send(self, ctx: AgentContext, agent: str, *, to: str, subject: str, body: str,
                   attachments: list[dict[str, Any]] | None = None) -> OutboxEntry:
        return await _record(ctx, self, agent, "send_email", recipient=to, subject=subject, body=body,
                             payload={"attachments": attachments or []})


class MockPostonomous(Integration):
    name = "postonomous"
    title = "Postonomous (social media publishing)"
    description = "Scheduling and publishing social posts through Postonomous. Mock: approved posts are recorded in the Outbox, not published."
    live_requirements = ("Postonomous API documentation, an API URL and key for the owner's account, and the owner's authorization "
                         "to publish. The live adapter has not been built because its API has not been provided.")

    async def publish(self, ctx: AgentContext, agent: str, *, platform: str, text: str, when: str = "") -> OutboxEntry:
        return await _record(ctx, self, agent, "publish_post", recipient=platform, body=text, payload={"scheduled_for": when})


class MockWordPressPublisher(Integration):
    name = "wordpress_publish"
    title = "WordPress publishing (REST API)"
    description = "Publishing posts to a client site. Mock: approved posts are recorded in the Outbox; no site is changed."
    live_requirements = "A per-site application password with an Author role, and the owner's authorization per site."

    async def publish(self, ctx: AgentContext, agent: str, *, site_url: str, title: str, content: str, status: str = "draft") -> OutboxEntry:
        return await _record(ctx, self, agent, "create_post", recipient=site_url, subject=title, body=content,
                             payload={"post_status": status})


class MockAdsPlatform(Integration):
    name = "ads"
    title = "Ad platforms (Meta / Google Ads)"
    description = "Reads campaign performance and changes budgets. Mock: performance is synthetic sample data; budget changes are recorded, not applied."
    live_requirements = "Read-only API tokens for reporting; a separate, owner-approved token and spend limits for budget changes."

    CAMPAIGNS = [
        ("cmp-101", "Website Design - Search"),
        ("cmp-102", "Local SEO - Retargeting"),
        ("cmp-103", "AI Services - Awareness"),
    ]

    def insights(self, days: int, today: date | None = None) -> list[dict[str, Any]]:
        """Deterministic synthetic daily metrics, clearly marked as sample data."""
        today = today or datetime.now(UTC).date()
        rows = []
        for cid, name in self.CAMPAIGNS:
            for back in range(days):
                day = today - timedelta(days=back + 1)
                seed = int(hashlib.sha256(f"{cid}{day}".encode()).hexdigest()[:8], 16)
                spend = 20 + seed % 40 + (25 if cid == "cmp-103" and back == 0 else 0)
                clicks = 15 + seed % 60
                conversions = (seed >> 8) % 5 if cid != "cmp-103" else (seed >> 8) % 2
                revenue = conversions * (180 + (seed >> 12) % 120)
                rows.append({"campaign_id": cid, "campaign": name, "date": day.isoformat(), "spend": float(spend),
                             "clicks": clicks, "conversions": conversions, "revenue": float(revenue), "sample_data": True})
        return rows

    async def set_daily_budget(self, ctx: AgentContext, agent: str, *, campaign_id: str, amount: float, reason: str) -> OutboxEntry:
        return await _record(ctx, self, agent, "set_daily_budget", recipient=campaign_id,
                             subject=f"Daily budget -> ${amount:,.2f}", body=reason, payload={"amount": amount})


class MockPostalMail(Integration):
    name = "postal_mail"
    title = "Certified mail (credit dispute letters)"
    description = "Mailing dispute letters to credit bureaus. Mock: records the letter id only; no personal data leaves the isolated store."
    live_requirements = "A certified-mail provider agreement, legal review of the Credit Repair module, and the owner's authorization."

    async def mail_letter(self, ctx: AgentContext, agent: str, *, letter_id: int, bureau: str) -> OutboxEntry:
        # Ids only: the letter itself stays encrypted in the credit repair database.
        return await _record(ctx, self, agent, "mail_dispute_letter", recipient=bureau,
                             subject=f"Dispute letter #{letter_id}", payload={"letter_id": letter_id})


ADAPTERS: list[type[Integration]] = [MockEmail, MockPostonomous, MockWordPressPublisher, MockAdsPlatform, MockPostalMail]
