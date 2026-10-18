"""Leads, support tickets, the outbox, and the intake endpoints workflow engines call."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from .. import tasks as task_service
from ..config import Settings, get_settings
from ..models import Lead, OutboxEntry, SupportTicket
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
    task = await task_service.create_task(session, settings, agent="sales", capability="score_lead", params={"lead_id": lead.id},
                                          source=source, created_by=actor, priority=40)
    return lead, [task.id]


async def intake_ticket(session: AsyncSession, settings: Settings, body: TicketBody, source: str, actor: str) -> tuple[SupportTicket, list[int]]:
    """Store a client message, then triage it and draft a reply (both draft-level; sending needs approval)."""
    ticket = SupportTicket(source=source, **body.model_dump())
    session.add(ticket)
    await session.flush()
    ids = []
    for capability in ("triage_ticket", "draft_reply"):
        task = await task_service.create_task(session, settings, agent="support", capability=capability, params={"ticket_id": ticket.id},
                                              source=source, created_by=actor, priority=30 if capability == "triage_ticket" else 60)
        ids.append(task.id)
    return ticket, ids


@router.get("/leads")
async def list_leads(_: Principal = Depends(require_user), session: AsyncSession = Depends(get_db), limit: int = Query(100, ge=1, le=500)) -> dict:
    rows = (await session.scalars(select(Lead).order_by(Lead.id.desc()).limit(limit))).all()
    return {"leads": [to_dict(r) for r in rows]}


@router.post("/leads", status_code=201)
async def create_lead(body: LeadBody, request: Request, principal: Principal = Depends(require_operator),
                      session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    lead, task_ids = await intake_lead(session, settings, body, "dashboard", principal.name)
    await audit.record(session, principal.name, "lead.created", "lead", lead.id, ip=client_ip(request))
    await session.commit()
    return {"lead": to_dict(lead), "task_ids": task_ids}


@router.get("/support/tickets")
async def list_tickets(_: Principal = Depends(require_user), session: AsyncSession = Depends(get_db), limit: int = Query(100, ge=1, le=500)) -> dict:
    rows = (await session.scalars(select(SupportTicket).order_by(SupportTicket.id.desc()).limit(limit))).all()
    return {"tickets": [to_dict(r) for r in rows]}


@router.post("/support/tickets", status_code=201)
async def create_ticket(body: TicketBody, request: Request, principal: Principal = Depends(require_operator),
                        session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    ticket, task_ids = await intake_ticket(session, settings, body, "dashboard", principal.name)
    await audit.record(session, principal.name, "support.ticket_created", "support_ticket", ticket.id, ip=client_ip(request))
    await session.commit()
    return {"ticket": to_dict(ticket), "task_ids": task_ids}


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
    return {"lead": to_dict(lead)}
