"""Credit Repair: compliance checks and dispute-letter drafts from the isolated, encrypted store.

Sensitive by design: the supervisor never routes requests here, results name
clients only by reference (CR-0001), letters stay encrypted in the credit
repair database, nothing personal goes to the AI model, events or outbox,
and mailing a letter needs an approval from someone with the sensitive-data
permission (mock in this build).
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import Field
from sqlalchemy import select

from .. import credit_repair
from ..credit_repair import store
from .base import AgentContext, AgentResult, BaseAgent, Finding, Params, Risk, capability

BUREAUS = {
    "equifax": "Equifax Information Services LLC\nP.O. Box 740256\nAtlanta, GA 30374",
    "experian": "Experian\nP.O. Box 4500\nAllen, TX 75013",
    "transunion": "TransUnion Consumer Solutions\nP.O. Box 2000\nChester, PA 19016",
}
REASONS = {
    "not_mine": "This account does not belong to me.",
    "inaccurate_balance": "The balance reported is not accurate.",
    "inaccurate_status": "The payment status reported is not accurate.",
    "duplicate": "This account is reported more than once.",
    "outdated": "This information is older than the reporting period allows.",
    "other": "This information is not accurate.",
}


class ReviewParams(Params):
    client_id: int | None = Field(None, description="One client, or all clients when empty")


class ClientParams(Params):
    client_id: int


class LetterParams(Params):
    letter_id: int


def letter_text(client: dict, bureau: str, items: list[dict], today: datetime) -> str:
    lines = [client["full_name"], *(client["address"].splitlines() or [""]), "", f"{today:%B} {today.day}, {today:%Y}", "", BUREAUS[bureau], "",
             "Re: Request for investigation of disputed information (Fair Credit Reporting Act, 15 U.S.C. 1681i)", "",
             "To whom it may concern,", "",
             "I am writing to dispute the following information in my credit file. Each item is listed with the reason I believe it is inaccurate.", ""]
    for n, item in enumerate(items, 1):
        last4 = f", account ending {item['account_last4']}" if item["account_last4"] else ""
        lines.append(f"{n}. {item['creditor']}{last4}: {REASONS[item['reason']]}" + (f" {item['detail']}" if item["detail"] else ""))
    lines += ["", "Please investigate these items and correct or delete any information that is inaccurate, incomplete or cannot be verified, "
              "as the Fair Credit Reporting Act requires, and send me the results of your investigation.", "",
              "Enclosed: a copy of my government-issued ID and proof of address.", "", "Sincerely,", "", "",
              client["full_name"]]
    ident = [f"Date of birth: {client['dob']}" if client["dob"] else "", f"SSN (last 4): {client['ssn_last4']}" if client["ssn_last4"] else ""]
    if any(ident):
        lines.append(" | ".join(x for x in ident if x))
    return "\n".join(lines)


class CreditRepairAgent(BaseAgent):
    name = "credit_repair"
    title = "Credit Repair & Dispute Management"
    description = (
        "Isolated module: checks Credit Repair Organizations Act timing rules and drafts Fair Credit Reporting Act dispute letters "
        "from the encrypted client store. Never shares personal data with the AI model or other agents. Mailing needs approval (mock)."
    )
    integrations = ("postal_mail",)
    sensitive = True

    def actor(self, ctx: AgentContext) -> str:
        return f"agent:credit_repair (task {ctx.task_id})" + (f", approved by {ctx.approved_by}" if ctx.approved_by else "")

    async def guard(self, ctx: AgentContext) -> None:
        if not store.configured(ctx.settings):
            raise ValueError("the Credit Repair store is not configured on this installation")
        if await credit_repair.enabled_state(ctx.session) is None:
            raise ValueError("the Credit Repair module is not enabled (an admin must enable it and accept the compliance notice)")

    @capability(risk=Risk.READ, description="Check clients against credit-repair timing rules (signed contract, 3-business-day "
                "cancellation period, no fees before services are completed).", params=ReviewParams)
    async def compliance_review(self, ctx: AgentContext, params: ReviewParams) -> AgentResult:
        await self.guard(ctx)
        findings: list[Finding] = []
        async with store.session(ctx.settings) as s:
            q = select(store.CRClient).where(store.CRClient.status != "closed")
            if params.client_id:
                q = q.where(store.CRClient.id == params.client_id)
            clients = (await s.scalars(q)).all()
            for c in clients:
                letters = (await s.scalars(select(store.CRLetter).where(store.CRLetter.client_id == c.id, store.CRLetter.status != "void"))).all()
                if not c.contract_signed_on:
                    findings.append(Finding(code="no_contract", severity="warning",
                                            title=f"{c.reference}: no signed contract recorded; work must not start without one"))
                else:
                    earliest = store.business_days_after(c.contract_signed_on, 3)
                    early = [l for l in letters if l.created_at.date() <= earliest]
                    if early:
                        findings.append(Finding(code="inside_cancellation_period", severity="critical",
                                                title=f"{c.reference}: {len(early)} letter(s) created before the cancellation period ended ({earliest})"))
                if c.fees_collected_cents > 0 and not c.services_completed_on:
                    findings.append(Finding(code="advance_fee", severity="critical",
                                            title=f"{c.reference}: fees recorded before services were completed (advance fees are prohibited)"))
            await store.log_access(s, self.actor(ctx), "compliance_review", params.client_id, clients=len(clients))
        serious = [f for f in findings if f.severity != "info"]
        return AgentResult(summary=f"Compliance review of {len(clients)} client(s): " + (f"{len(serious)} issue(s)" if serious else "no issues found"),
                           data={"clients_reviewed": len(clients)}, findings=findings)

    @capability(risk=Risk.DRAFT, description="Draft dispute letters (one per bureau) for a client's open items; stored encrypted.", params=ClientParams)
    async def draft_dispute_letters(self, ctx: AgentContext, params: ClientParams) -> AgentResult:
        await self.guard(ctx)
        cipher = store.Cipher(ctx.settings)
        async with store.session(ctx.settings) as s:
            client = await s.get(store.CRClient, params.client_id)
            if client is None:
                raise ValueError(f"no credit repair client {params.client_id}")
            if not client.contract_signed_on:
                raise ValueError(f"{client.reference}: no signed contract recorded; letters cannot be drafted yet")
            earliest = store.business_days_after(client.contract_signed_on, 3)
            if datetime.now(UTC).date() <= earliest:
                raise ValueError(f"{client.reference}: the 3-business-day cancellation period runs until {earliest}; draft after that")
            items = (await s.scalars(select(store.CRItem).where(store.CRItem.client_id == client.id, store.CRItem.status == "open"))).all()
            if not items:
                raise ValueError(f"{client.reference}: no open items to dispute")
            view = store.client_view(client, cipher, full=True)
            created = []
            for bureau in sorted({i.bureau for i in items}):
                chosen = [i for i in items if i.bureau == bureau]
                decoded = [{"creditor": cipher.dec(i.creditor_enc), "account_last4": cipher.dec(i.account_last4_enc), "reason": i.reason,
                            "detail": cipher.dec(i.detail_enc)} for i in chosen]
                letter = store.CRLetter(client_id=client.id, bureau=bureau, item_ids=[i.id for i in chosen],
                                        content_enc=cipher.enc(letter_text(view, bureau, decoded, datetime.now(UTC))), task_id=ctx.task_id)
                s.add(letter)
                for i in chosen:
                    i.status = "in_letter"
                await s.flush()
                created.append((letter.id, bureau))
            await store.log_access(s, self.actor(ctx), "draft_dispute_letters", client.id, letters=[l for l, _ in created])
        return AgentResult(summary=f"Drafted {len(created)} dispute letter(s) for {client.reference} ({', '.join(b for _, b in created)}); review before mailing",
                           data={"reference": client.reference, "letter_ids": [l for l, _ in created]})

    @capability(risk=Risk.EXTERNAL, description="Mail a dispute letter by certified mail (mock in this build; approval required).", params=LetterParams)
    async def mail_dispute_letter(self, ctx: AgentContext, params: LetterParams) -> AgentResult:
        await self.guard(ctx)
        async with store.session(ctx.settings) as s:
            letter = await s.get(store.CRLetter, params.letter_id)
            if letter is None or letter.status != "draft":
                raise ValueError(f"letter {params.letter_id} is not a draft waiting to be mailed")
            client = await s.get(store.CRClient, letter.client_id)
            entry = await ctx.integrations.get("postal_mail").mail_letter(ctx, self.name, letter_id=letter.id, bureau=letter.bureau)  # type: ignore[attr-defined]
            letter.status, letter.mailed_at = "mailed_mock", datetime.now(UTC)
            for item in (await s.scalars(select(store.CRItem).where(store.CRItem.id.in_(letter.item_ids)))).all():
                item.status = "disputed"
            await store.log_access(s, self.actor(ctx), "mail_dispute_letter", letter.client_id, letter_id=letter.id, outbox_id=entry.id)
        return AgentResult(summary=f"Letter #{letter.id} for {client.reference} to {letter.bureau} recorded as outbox #{entry.id} (mock: not mailed)",
                           data={"letter_id": letter.id, "outbox_id": entry.id})
