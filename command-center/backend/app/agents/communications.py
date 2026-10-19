"""Email & Customer Communications: leads, proposals and client messages.

Outgoing email (proposals, replies) is an external action: it waits for a human
approval and, in this build, goes to the mock email adapter (the Outbox).
The approval names the recipient, the subject and the draft, and binds the
draft's content: a draft that is swapped or changed, or a recipient that
changes, after the request makes the send refuse (checked again in the
sending task's own session, with the rows locked, right before it sends).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .. import approvals
from .. import tasks as task_service
from ..models import Lead, SupportTicket
from .base import AgentContext, AgentResult, BaseAgent, Intent
from .sales import EMAIL_RE, SalesCapabilities
from .support import SupportCapabilities

SENDS = ("send_reply", "send_proposal")


def proposal_subject(lead: Lead) -> str:
    """The subject send_proposal uses (app/agents/sales.py)."""
    return f"Website Design Proposal for {lead.company or lead.name} - Infinite Marketing"


def email_context(*, recipient: str, subject: str, draft: Any, binding: dict[str, Any]) -> dict[str, Any]:
    facts = approvals.draft_facts(draft)
    return {
        "target": f'email to {recipient}, subject "{subject}", draft #{draft.id}',
        "environment": approvals.delivery_note("email"),
        "production": False,
        "delivery": approvals.delivery("email"),
        "draft": facts,
        "binding": {**binding, "recipient": recipient, "subject": subject, "artifact_id": draft.id, "draft_sha256": facts["sha256"]},
    }


async def reply_context(session: AsyncSession, ticket_id: int | None, *, lock: bool = False) -> dict[str, Any]:
    ticket = await session.get(SupportTicket, ticket_id, with_for_update=lock) if ticket_id is not None else None
    if ticket is None:
        raise ValueError(f"no support ticket {ticket_id}")
    draft = await approvals.load_draft(session, ticket.reply_artifact_id, kinds=("support_reply",),
                                       what=f"reply draft for ticket {ticket.id}", lock=lock)
    if not ticket.customer_email:
        raise ValueError(f"ticket {ticket.id} has no client email address")
    return email_context(recipient=ticket.customer_email, subject=f"Re: {ticket.subject}", draft=draft,
                         binding={"ticket_id": ticket.id})


async def proposal_context(session: AsyncSession, lead_id: int | None, *, lock: bool = False) -> dict[str, Any]:
    lead = await session.get(Lead, lead_id, with_for_update=lock) if lead_id is not None else None
    if lead is None:
        raise ValueError(f"no lead {lead_id}")
    draft = await approvals.load_draft(session, lead.proposal_artifact_id, kinds=("proposal",),
                                       what=f"proposal draft for lead {lead.id}", lock=lock)
    if not EMAIL_RE.match(lead.email or ""):
        raise ValueError(f"lead {lead.id} has no valid email address")
    # The covering email greets the lead by name; the proposal itself is the attachment.
    return email_context(recipient=lead.email, subject=proposal_subject(lead), draft=draft,
                         binding={"lead_id": lead.id, "greeting": lead.name})


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

    async def approval_context(self, session: AsyncSession, capability: str, params: dict[str, Any], *,
                               lock: bool = False) -> dict[str, Any]:
        if capability == "send_reply":
            return await reply_context(session, params.get("ticket_id"), lock=lock)
        if capability == "send_proposal":
            return await proposal_context(session, params.get("lead_id"), lock=lock)
        return {}

    async def run(self, ctx: AgentContext, capability_name: str, raw_params: dict[str, Any]) -> AgentResult:
        if capability_name in SENDS:
            # The worker checked before starting, in another transaction; this check, with the rows locked, is the one that counts.
            await task_service.recheck_target(self, ctx, capability_name, raw_params)
        return await super().run(ctx, capability_name, raw_params)
