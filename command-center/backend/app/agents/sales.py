"""Sales & Proposals: lead scoring, proposal drafts, and sending after approval (mock)."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import Field

from ..models import Artifact, Lead
from .base import AgentContext, AgentResult, ArtifactSpec, BaseAgent, EventSpec, Intent, NoParams, Params, Risk, capability

TEMPLATE_PATH = Path("/app/templates/proposal-template.html")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SERVICES = {
    "website": ("website", "web design", "redesign", "landing page", "wordpress", "site"),
    "seo": ("seo", "google", "rank", "search"),
    "ai": ("ai", "chatbot", "automation", "assistant"),
    "social": ("social", "facebook", "instagram", "content"),
    "ads": ("ads", "advertising", "ppc", "google ads", "meta ads"),
}
SPAM = ("crypto", "backlink", "guest post", "casino", "loan offer", "rank you #1", "seo services for your", "bitcoin", "viagra")


class LeadParams(Params):
    lead_id: int


def score(lead: Lead) -> tuple[int, list[str], bool]:
    """A transparent, rule-based score from 0 to 100, with the reasons."""
    text = f"{lead.service_interest} {lead.message}".lower()
    if any(s in text for s in SPAM) or len(re.findall(r"https?://", lead.message)) > 2:
        return 0, ["looks like spam (sales pitch or link list)"], True
    points, reasons = 30, ["base 30"]

    def add(n: int, why: str) -> None:
        nonlocal points
        points += n
        reasons.append(f"+{n} {why}")

    if EMAIL_RE.match(lead.email or ""):
        add(10, "valid email")
    if lead.company:
        add(10, "company named")
    if lead.phone:
        add(5, "phone given")
    if lead.website:
        add(10, "has a website (redesign opportunity)")
    matched = [k for k, words in SERVICES.items() if any(re.search(rf"\b{re.escape(w)}\b", text) for w in words)]
    if matched:
        add(15, f"asks about services we offer ({', '.join(matched)})")
    if re.search(r"\b(budget|price|pricing|cost|quote|\$\d)", text):
        add(10, "talks budget or pricing")
    if re.search(r"\b(asap|urgent|this (week|month)|soon|deadline|launch)\b", text):
        add(10, "has a timeline")
    if len(lead.message.strip()) > 40:
        add(5, "detailed message")
    return min(points, 100), reasons, False


def fill_template(template: str, lead: Lead, today: datetime) -> str:
    values = {
        "{{CLIENT_NAME}}": lead.name,
        "{{CLIENT_COMPANY}}": lead.company or lead.name,
        "{{CLIENT_EMAIL}}": lead.email,
        "{{PROPOSAL_DATE}}": f"{today:%B} {today.day}, {today:%Y}",
        "{{VALID_UNTIL}}": (lambda d: f"{d:%B} {d.day}, {d:%Y}")(today + timedelta(days=30)),
    }
    for placeholder, value in values.items():
        template = template.replace(placeholder, html.escape(value))  # lead data comes from web forms
    return template


BUILT_IN_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><title>Proposal for {{CLIENT_COMPANY}}</title>
<style>body{font-family:Arial,sans-serif;max-width:720px;margin:40px auto;color:#1b1d2a;line-height:1.55}
h1{color:#4f46e5}table{border-collapse:collapse;width:100%}td{border:1px solid #ddd;padding:8px}</style></head><body>
<h1>Website Design Proposal</h1>
<p><strong>Prepared for:</strong> {{CLIENT_NAME}}, {{CLIENT_COMPANY}} ({{CLIENT_EMAIL}})<br>
<strong>Date:</strong> {{PROPOSAL_DATE}} &middot; <strong>Valid until:</strong> {{VALID_UNTIL}}</p>
<p>Dear {{CLIENT_NAME}},</p>
<p>Thank you for considering Infinite Marketing. This proposal outlines how we would design and build a website for {{CLIENT_COMPANY}}.</p>
<h2>Scope</h2><p>[Packages, page counts and pricing: copy them from your standard proposal before sending.]</p>
<h2>Next steps</h2><p>Reply to this email or call us to get started.</p>
<p>Infinite Marketing</p>
<p style="color:#888;font-size:12px">Built-in placeholder template. Put your own at templates/proposal-template.html.</p>
</body></html>"""


class SalesAgent(BaseAgent):
    name = "sales"
    title = "Sales & Proposals"
    description = "Scores incoming leads, drafts website design proposals from the proposal template, and sends them only after approval (mock email in this build)."
    integrations = ("email",)

    def intents(self) -> list[Intent]:
        return [
            Intent("lead_overview", (r"\bleads?\b", r"\bprospects?\b", r"\bpipeline\b"), lambda text, urls: {}),
        ]

    @capability(risk=Risk.DRAFT, description="Score a lead from 0 to 100 with reasons; marks obvious spam.", params=LeadParams)
    async def score_lead(self, ctx: AgentContext, params: LeadParams) -> AgentResult:
        lead = await ctx.session.get(Lead, params.lead_id)
        if lead is None:
            raise ValueError(f"no lead {params.lead_id}")
        value, reasons, spam = score(lead)
        lead.score, lead.score_reasons = value, reasons
        if lead.status == "new":
            lead.status = "spam" if spam else ("qualified" if value >= 60 else "new")
        lead.updated_at = datetime.now(UTC)
        events = []
        if value >= 75:
            events.append(EventSpec(kind="lead.hot", severity="warning", title=f"Hot lead: {lead.company or lead.name} (score {value})",
                                    detail={"lead_id": lead.id, "score": value}))
        return AgentResult(summary=f"Lead #{lead.id} {lead.company or lead.name}: score {value}" + (" (spam)" if spam else ""),
                           data={"lead_id": lead.id, "score": value, "reasons": reasons, "status": lead.status}, events=events)

    @capability(risk=Risk.DRAFT, description="Draft the website design proposal for a lead (HTML, for review).", params=LeadParams)
    async def draft_proposal(self, ctx: AgentContext, params: LeadParams) -> AgentResult:
        lead = await ctx.session.get(Lead, params.lead_id)
        if lead is None:
            raise ValueError(f"no lead {params.lead_id}")
        if lead.status == "spam":
            raise ValueError(f"lead {lead.id} is marked as spam")
        template, source = (TEMPLATE_PATH.read_text(encoding="utf-8"), "owner template") if TEMPLATE_PATH.is_file() else (BUILT_IN_TEMPLATE, "built-in placeholder template")
        content = fill_template(template, lead, datetime.now(UTC))
        artifact = Artifact(task_id=ctx.task_id, agent=self.name, kind="proposal", title=f"Proposal - {lead.company or lead.name}",
                            content=content, content_type="text/html", data={"lead_id": lead.id, "template": source})
        ctx.session.add(artifact)
        await ctx.session.flush()
        lead.proposal_artifact_id = artifact.id
        lead.status = "proposal_drafted"
        lead.updated_at = datetime.now(UTC)
        return AgentResult(summary=f"Proposal draft #{artifact.id} for {lead.company or lead.name} ({source}); review it before sending",
                           data={"lead_id": lead.id, "artifact_id": artifact.id, "template": source})

    @capability(risk=Risk.EXTERNAL, description="Email the drafted proposal to the lead (mock email in this build).", params=LeadParams)
    async def send_proposal(self, ctx: AgentContext, params: LeadParams) -> AgentResult:
        lead = await ctx.session.get(Lead, params.lead_id)
        if lead is None or not lead.proposal_artifact_id:
            raise ValueError(f"lead {params.lead_id} has no drafted proposal")
        if not EMAIL_RE.match(lead.email or ""):
            raise ValueError(f"lead {lead.id} has no valid email address")
        company = lead.company or lead.name
        body = (f"Hi {lead.name},\n\nThank you for your interest in Infinite Marketing. Attached is your website design proposal for "
                f"{company}.\n\nIf you have any questions, just reply to this email.\n\nInfinite Marketing")
        entry = await ctx.integrations.get("email").send(  # type: ignore[attr-defined]
            ctx, self.name, to=lead.email, subject=f"Website Design Proposal for {company} - Infinite Marketing", body=body,
            attachments=[{"artifact_id": lead.proposal_artifact_id, "filename": f"Infinite-Marketing-Proposal-{company}.html"}])
        lead.status = "proposal_sent"
        lead.updated_at = datetime.now(UTC)
        return AgentResult(summary=f"Proposal email to {lead.email} recorded as outbox #{entry.id} (mock: not sent)", data={"outbox_id": entry.id})

    @capability(risk=Risk.READ, description="Summarise the lead pipeline.", params=NoParams, examples=("How do our leads look?",))
    async def lead_overview(self, ctx: AgentContext, params: NoParams) -> AgentResult:
        from sqlalchemy import func, select

        counts = dict((await ctx.session.execute(select(Lead.status, func.count()).group_by(Lead.status))).all())
        hot = (await ctx.session.scalars(select(Lead).where(Lead.score >= 75, Lead.status.in_(("new", "qualified", "proposal_drafted"))).limit(5))).all()
        summary = "Leads: " + (", ".join(f"{n} {s.replace('_', ' ')}" for s, n in sorted(counts.items())) or "none yet")
        if hot:
            summary += ". Hot: " + "; ".join(f"{h.company or h.name} ({h.score})" for h in hot)
        return AgentResult(summary=summary, data={"counts": counts, "hot": [h.id for h in hot]})
