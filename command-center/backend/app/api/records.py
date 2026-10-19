"""Leads, support tickets, the outbox, and the intake endpoints workflow engines call."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from .. import tasks as task_service
from ..config import Settings, get_settings
from ..models import Lead, OutboxEntry, SupportTicket, Task
from .deps import Principal, client_ip, get_db, require_operator, require_service, require_user, to_dict

router = APIRouter(prefix="/api", tags=["records"])


class LeadBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    company: str = Field("", max_length=200)
    email: str = Field("", max_length=320)
    phone: str = Field("", max_length=50)
    website: str = Field("", max_length=500)
    service_interest: str = Field("", max_length=300)
    message: str = Field("", max_length=5000)


class TicketBody(BaseModel):
    customer_name: str = Field("", max_length=200)
    customer_email: str = Field("", max_length=320)
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field("", max_length=20000)


async def intake_lead(session: AsyncSession, settings: Settings, body: LeadBody, source: str, actor: str) -> tuple[Lead, list[int]]:
    """Store a lead and queue its scoring (a draft-level action, so it runs without approval)."""
    lead = Lead(source=source, **body.model_dump())
    session.add(lead)
    await session.flush()
    task = await task_service.create_task(session, settings, agent="communications", capability="score_lead", params={"lead_id": lead.id},
                                          source=source, created_by=actor, priority=40)
    return lead, [task.id]


async def intake_ticket(session: AsyncSession, settings: Settings, body: TicketBody, source: str, actor: str) -> tuple[SupportTicket, list[int]]:
    """Store a client message, then triage it and draft a reply (both draft-level; sending needs approval)."""
    ticket = SupportTicket(source=source, **body.model_dump())
    session.add(ticket)
    await session.flush()
    ids = []
    for capability in ("triage_ticket", "draft_reply"):
        task = await task_service.create_task(session, settings, agent="communications", capability=capability, params={"ticket_id": ticket.id},
                                              source=source, created_by=actor, priority=30 if capability == "triage_ticket" else 60)
        ids.append(task.id)
    return ticket, ids


# The email capability behind each record's "sent" status, and the task parameter naming the record.
LEAD_EMAIL = ("send_proposal", "lead_id")
TICKET_EMAIL = ("send_reply", "ticket_id")


async def delivery_modes(session: AsyncSession, email: tuple[str, str], ids: list[int]) -> dict[int, OutboxEntry]:
    """{record id: its latest email in the outbox}, found through the approved task that recorded it.

    The record's status says what was approved ("proposal_sent", "replied"); the outbox entry's mode says
    whether anything left the machine ("mock": recorded, not sent). Nothing stored is changed.
    """
    if not ids:
        return {}
    capability, key = email
    ref = Task.params[key].astext
    rows = await session.execute(
        select(ref, OutboxEntry).join(Task, Task.id == OutboxEntry.task_id)
        .where(Task.capability == capability, OutboxEntry.integration == "email", ref.in_([str(i) for i in ids]))
        .order_by(OutboxEntry.id)
    )
    return {int(record_id): entry for record_id, entry in rows.all()}  # the latest entry wins


def with_delivery(record: Lead | SupportTicket, entries: dict[int, OutboxEntry]) -> dict[str, Any]:
    """The record plus delivery_mode ('mock' or 'live'; None if no email was recorded) and delivery_outbox_id."""
    entry = entries.get(record.id)
    return {**to_dict(record), "delivery_mode": entry.mode if entry else None, "delivery_outbox_id": entry.id if entry else None}


@router.get("/leads")
async def list_leads(_: Principal = Depends(require_user), session: AsyncSession = Depends(get_db), limit: int = Query(100, ge=1, le=500)) -> dict:
    rows = (await session.scalars(select(Lead).order_by(Lead.id.desc()).limit(limit))).all()
    entries = await delivery_modes(session, LEAD_EMAIL, [r.id for r in rows])
    return {"leads": [with_delivery(r, entries) for r in rows]}


@router.post("/leads", status_code=201)
async def create_lead(body: LeadBody, request: Request, principal: Principal = Depends(require_operator),
                      session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    lead, task_ids = await intake_lead(session, settings, body, "dashboard", principal.name)
    await audit.record(session, principal.name, "lead.created", "lead", lead.id, ip=client_ip(request))
    await session.commit()
    return {"lead": with_delivery(lead, {}), "task_ids": task_ids}


@router.get("/support/tickets")
async def list_tickets(_: Principal = Depends(require_user), session: AsyncSession = Depends(get_db), limit: int = Query(100, ge=1, le=500)) -> dict:
    rows = (await session.scalars(select(SupportTicket).order_by(SupportTicket.id.desc()).limit(limit))).all()
    entries = await delivery_modes(session, TICKET_EMAIL, [r.id for r in rows])
    return {"tickets": [with_delivery(r, entries) for r in rows]}


@router.post("/support/tickets", status_code=201)
async def create_ticket(body: TicketBody, request: Request, principal: Principal = Depends(require_operator),
                        session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    ticket, task_ids = await intake_ticket(session, settings, body, "dashboard", principal.name)
    await audit.record(session, principal.name, "support.ticket_created", "support_ticket", ticket.id, ip=client_ip(request))
    await session.commit()
    return {"ticket": with_delivery(ticket, {}), "task_ids": task_ids}


@router.get("/outbox")
async def outbox(_: Principal = Depends(require_user), session: AsyncSession = Depends(get_db), limit: int = Query(100, ge=1, le=500)) -> dict:
    rows = (await session.scalars(select(OutboxEntry).order_by(OutboxEntry.id.desc()).limit(limit))).all()
    return {"entries": [to_dict(r) for r in rows]}


# Called by workflow engines (n8n's lead-intake workflow, a web form relay...) with the service token.

@router.post("/integrations/workflow/leads", status_code=201)
async def workflow_lead(body: LeadBody, principal: Principal = Depends(require_service),
                        session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    lead, task_ids = await intake_lead(session, settings, body, "workflow", principal.name)
    await session.commit()
    return {"lead_id": lead.id, "task_ids": task_ids}


@router.post("/integrations/workflow/support", status_code=201)
async def workflow_ticket(body: TicketBody, principal: Principal = Depends(require_service),
                          session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    ticket, task_ids = await intake_ticket(session, settings, body, "workflow", principal.name)
    await session.commit()
    return {"ticket_id": ticket.id, "task_ids": task_ids}


@router.get("/leads/{lead_id}")
async def get_lead(lead_id: int, _: Principal = Depends(require_user), session: AsyncSession = Depends(get_db)) -> dict:
    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(404, "no such lead")
    return {"lead": with_delivery(lead, await delivery_modes(session, LEAD_EMAIL, [lead.id]))}
