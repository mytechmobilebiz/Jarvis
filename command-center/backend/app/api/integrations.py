"""Integration status, and the endpoints n8n workflows call back into."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, events
from ..config import Settings, get_settings
from ..integrations import build_hub
from ..integrations.n8n import N8nClient
from ..models import Event, IntegrationStatus
from .deps import Principal, client_ip, get_db, require_operator, require_service, require_user, to_dict

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


class AckBody(BaseModel):
    event_id: int
    execution_id: str = Field("", max_length=200)
    workflow: str = Field("", max_length=200)


@router.get("")
async def list_integrations(
    _: Principal = Depends(require_user), session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)
) -> dict:
    statuses = {r.name: to_dict(r) for r in (await session.scalars(select(IntegrationStatus))).all()}
    n8n = N8nClient(settings)
    workflows = None
    workflow_error = ""
    try:
        workflows = await n8n.list_workflows()
    except Exception as exc:  # noqa: BLE001 - shown on the page, not fatal
        workflow_error = str(exc)
    items = [
        {
            "name": "n8n",
            "title": "n8n workflow engine",
            "mode": "live",
            "description": "Self-hosted n8n beside the Command Center: receives alerts, runs the morning briefing and lead intake workflows.",
            "live_requirements": "",
            "status": statuses.get("n8n"),
            "workflows": workflows,
            "workflow_error": workflow_error,
            "webhook_url": n8n.events_webhook_url,
        },
        {
            "name": "anthropic",
            "title": "Claude (Anthropic API)",
            "mode": "live" if settings.anthropic_enabled else "disabled",
            "description": "Planning, summaries and drafts by the AI supervisor and agents. Without it, deterministic rules and templates are used.",
            "live_requirements": "" if settings.anthropic_enabled else "Set ANTHROPIC_API_KEY (metered; capped by LLM_DAILY_REQUEST_CAP).",
            "model": settings.llm_model,
            "status": None,
        },
    ]
    items += [{**d, "status": statuses.get(d["name"])} for d in build_hub(settings).describe()]
    return {"integrations": items}


@router.post("/n8n/ack")
async def n8n_ack(
    body: AckBody, _: Principal = Depends(require_service), session: AsyncSession = Depends(get_db)
) -> dict:
    """Called by the n8n event-router workflow once it has handled an event."""
    event = await session.get(Event, body.event_id, with_for_update=True)
    if event is None:
        raise HTTPException(404, "no such event")
    now = datetime.now(UTC)
    event.n8n_ack_at = now
    event.n8n_execution_id = body.execution_id
    status = await session.get(IntegrationStatus, "n8n", with_for_update=True)
    if status is None:
        status = IntegrationStatus(name="n8n", detail={})
        session.add(status)
    status.last_ok_at = now
    status.detail = {**(status.detail or {}), "last_ack_event_id": event.id, "last_ack_workflow": body.workflow}
    status.updated_at = now
    await session.commit()
    return {"ok": True, "event_id": event.id}


@router.post("/n8n/test", status_code=201)
async def n8n_test(
    request: Request,
    principal: Principal = Depends(require_operator),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Emit a test event; the worker forwards it and n8n acknowledges it back."""
    event = await events.emit(
        session,
        settings,
        kind="system.test",
        severity="warning",
        title=f"Test event requested by {principal.name}",
        source="dashboard",
        detail={"test": True},
    )
    await audit.record(session, principal.name, "integration.n8n_test", "event", event.id, ip=client_ip(request))
    await session.commit()
    return {"event": to_dict(event)}
