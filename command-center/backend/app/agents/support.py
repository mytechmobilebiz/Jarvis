"""Client Support: triage of client messages and reply drafts; sending after approval (mock)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import Field

from ..models import Artifact, SupportTicket
from .base import AgentContext, AgentResult, BaseAgent, EventSpec, Intent, NoParams, Params, Risk, capability
from .drafting import untrusted, write

CATEGORIES = [
    ("outage", r"\b(down|not loading|offline|broken|error|crash|can'?t access|500|white screen)\b", "urgent"),
    ("security", r"\b(hack(ed)?|malware|virus|spam links|phishing|defaced|suspicious)\b", "urgent"),
    ("billing", r"\b(invoice|bill(ing)?|charge[ds]?|refund|payment|card|subscription|renewal)\b", "high"),
    ("change_request", r"\b(change|update|add|remove|replace|edit|new page|photo|text|menu)\b", "normal"),
    ("email", r"\b(email|inbox|mailbox|outlook|gmail|smtp)\b", "high"),
    ("seo", r"\b(google|seo|ranking|search|traffic)\b", "normal"),
    ("question", r"\?", "low"),
]
NEGATIVE = r"\b(angry|frustrat\w*|unacceptable|terrible|disappoint\w*|cancel|again|still|worst)\b"
POSITIVE = r"\b(thanks?|thank you|great|love|appreciate|awesome)\b"


class TicketParams(Params):
    ticket_id: int


def triage(subject: str, body: str) -> dict:
    text = f"{subject}\n{body}".lower()
    category, priority = "general", "normal"
    for name, pattern, prio in CATEGORIES:
        if re.search(pattern, text):
            category, priority = name, prio
            break
    negative = len(re.findall(NEGATIVE, text))
    sentiment = "negative" if negative else ("positive" if re.search(POSITIVE, text) else "neutral")
    if sentiment == "negative" and priority in ("low", "normal"):
        priority = "high"
    return {"category": category, "priority": priority, "sentiment": sentiment, "method": "rules"}


class SupportAgent(BaseAgent):
    name = "support"
    title = "Client Support"
    description = "Triages client messages (category, priority, sentiment) and drafts replies. Replies are sent only after approval (mock email in this build)."
    integrations = ("email",)

    def intents(self) -> list[Intent]:
        return [Intent("support_overview", (r"\b(support|tickets?|client messages?|inbox)\b",), lambda text, urls: {})]

    async def _ticket(self, ctx: AgentContext, ticket_id: int) -> SupportTicket:
        # Locked: triage and drafting are queued together and may run at the same time.
        ticket = await ctx.session.get(SupportTicket, ticket_id, with_for_update=True)
        if ticket is None:
            raise ValueError(f"no support ticket {ticket_id}")
        return ticket

    @staticmethod
    def _apply_triage(ticket: SupportTicket) -> dict:
        result = triage(ticket.subject, ticket.body)
        ticket.category, ticket.priority, ticket.sentiment, ticket.triage = result["category"], result["priority"], result["sentiment"], result
        if ticket.status == "open":
            ticket.status = "triaged"
        ticket.updated_at = datetime.now(UTC)
        return result

    @capability(risk=Risk.DRAFT, description="Classify a client message: category, priority and sentiment.", params=TicketParams)
    async def triage_ticket(self, ctx: AgentContext, params: TicketParams) -> AgentResult:
        ticket = await self._ticket(ctx, params.ticket_id)
        result = self._apply_triage(ticket)
        events = []
        if ticket.priority == "urgent":
            events.append(EventSpec(kind="support.urgent", severity="critical", title=f"Urgent client message #{ticket.id}: {ticket.subject[:80]}",
                                    detail={"ticket_id": ticket.id, "category": ticket.category}))
        return AgentResult(summary=f"Ticket #{ticket.id}: {result['category']}, {result['priority']} priority, {result['sentiment']}",
                           data={"ticket_id": ticket.id, **result}, events=events)

    @capability(risk=Risk.DRAFT, description="Draft a reply to a client message for review.", params=TicketParams)
    async def draft_reply(self, ctx: AgentContext, params: TicketParams) -> AgentResult:
        ticket = await self._ticket(ctx, params.ticket_id)
        if not ticket.category:
            self._apply_triage(ticket)
        first = (ticket.customer_name or "there").split()[0]

        def template() -> str:
            opener = {
                "outage": "I'm sorry your website is giving you trouble. We're looking into it right now and will update you shortly.",
                "security": "Thank you for flagging this. We're treating it as urgent and checking the site now.",
                "billing": "Thanks for reaching out about your account. I'm checking the details and will get back to you today.",
                "change_request": "Thanks for the request. We'll make the change and send you a link to review it.",
            }.get(ticket.category, "Thanks for reaching out. We've received your message and will get back to you shortly.")
            return f"Hi {first},\n\n{opener}\n\nBest regards,\nInfinite Marketing Support"

        text, writer = await write(
            ctx, purpose="support_reply",
            system="You are the client support lead at Infinite Marketing (websites, SEO, hosting). Draft a short, warm, professional email "
                   "reply. Acknowledge the issue, say the next step, no promises of timelines, refunds, credits or outcomes, no technical "
                   "jargon. Sign as 'Infinite Marketing Support'. Output only the email body.",
            prompt=f"Category: {ticket.category}; priority: {ticket.priority}; client: {ticket.customer_name}\n"
                   f"Subject: {ticket.subject}\nMessage:\n{untrusted(ticket.body)}",
            fallback=template,
        )
        artifact = Artifact(task_id=ctx.task_id, agent=self.name, kind="support_reply", title=f"Reply - ticket #{ticket.id}: {ticket.subject[:60]}",
                            content=text, content_type="text/plain", data={"ticket_id": ticket.id, "writer": writer})
        ctx.session.add(artifact)
        await ctx.session.flush()
        ticket.reply_artifact_id = artifact.id
        ticket.status = "drafted"
        ticket.updated_at = datetime.now(UTC)
        return AgentResult(summary=f"Reply draft #{artifact.id} for ticket #{ticket.id} ({writer})", data={"artifact_id": artifact.id, "writer": writer})

    @capability(risk=Risk.EXTERNAL, description="Email the drafted reply to the client (mock email in this build).", params=TicketParams)
    async def send_reply(self, ctx: AgentContext, params: TicketParams) -> AgentResult:
        ticket = await self._ticket(ctx, params.ticket_id)
        draft = await ctx.session.get(Artifact, ticket.reply_artifact_id) if ticket.reply_artifact_id else None
        if draft is None:
            raise ValueError(f"ticket {ticket.id} has no drafted reply")
        if not ticket.customer_email:
            raise ValueError(f"ticket {ticket.id} has no client email address")
        entry = await ctx.integrations.get("email").send(  # type: ignore[attr-defined]
            ctx, self.name, to=ticket.customer_email, subject=f"Re: {ticket.subject}", body=draft.content)
        ticket.status = "replied"
        ticket.updated_at = datetime.now(UTC)
        return AgentResult(summary=f"Reply to {ticket.customer_email} recorded as outbox #{entry.id} (mock: not sent)", data={"outbox_id": entry.id})

    @capability(risk=Risk.READ, description="Summarise open client messages by priority.", params=NoParams, examples=("Any urgent support tickets?",))
    async def support_overview(self, ctx: AgentContext, params: NoParams) -> AgentResult:
        from sqlalchemy import func, select

        rows = (await ctx.session.execute(
            select(SupportTicket.priority, func.count()).where(SupportTicket.status.not_in(("replied", "closed"))).group_by(SupportTicket.priority)
        )).all()
        counts = dict(rows)
        return AgentResult(summary="Open tickets: " + (", ".join(f"{counts[p]} {p}" for p in ("urgent", "high", "normal", "low") if p in counts) or "none"),
                           data={"open_by_priority": counts})
