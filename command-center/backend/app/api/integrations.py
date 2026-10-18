"""Integration status, and the endpoints n8n workflows call back into."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, customer_settings, events
from ..config import Settings, get_settings
from ..integrations import build_hub
from ..integrations.n8n import N8nClient
from ..workflow import build_engine
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
    engine = build_engine(settings)
    workflows = None
    workflow_error = ""
    try:
        workflows = await engine.list_workflows()
    except Exception as exc:  # noqa: BLE001 - shown on the page, not fatal
        workflow_error = str(exc)
    ai, _key = await customer_settings.resolve_ai(session, settings)
    engine_item = {
        "name": engine.name,
        "title": engine.title,
        "mode": "live" if engine.delivers else "disabled",
        "description": {
            "n8n": "Self-hosted n8n beside the Command Center: receives alerts and runs the morning briefing workflow.",
            "webhook": "Events are POSTed, HMAC-signed, to the configured workflow webhook.",
        }.get(engine.name, "No workflow engine: events are recorded but not handed on."),
        "live_requirements": "" if engine.delivers else "Set WORKFLOW_ENGINE to n8n or webhook.",
        "status": statuses.get(engine.name),
        "workflows": workflows,
        "workflow_error": workflow_error,
    }
    if engine.name == "n8n":
        engine_item["webhook_url"] = N8nClient(settings).events_webhook_url
    items = [
        engine_item,
        {
            "name": "anthropic",
            "title": "Claude (Anthropic API)",
            "mode": "live" if ai.provider == "anthropic" else "disabled",
            "description": "Planning, summaries and drafts by the AI supervisor and agents. Without it, deterministic rules and templates are used.",
            "live_requirements": "" if ai.provider == "anthropic" else f"{ai.reason}. Add your Anthropic API key on the Settings page (metered, capped per day).",
            "model": ai.model,
            "status": None,
        },
    ]
    items += [{**d, "status": statuses.get(d["name"])} for d in build_hub(settings).describe()]
    return {"integrations": items}


@router.post("/workflow/ack")
@router.post("/n8n/ack")
async def n8n_ack(
    body: AckBody, _: Principal = Depends(require_service), session: AsyncSession = Depends(get_db)
) -> dict:
    """Called by the workflow engine (the n8n event router, or any other) once it has handled an event."""
    event = await session.get(Event, body.event_id, with_for_update=True)
    if event is None:
        raise HTTPException(404, "no such event")
    now = datetime.now(UTC)
    event.n8n_ack_at = now
    event.n8n_execution_id = body.execution_id
    detail = {"last_ack_event_id": event.id, "last_ack_workflow": body.workflow}
    stmt = pg_insert(IntegrationStatus).values(name=get_settings().workflow_engine, last_ok_at=now, detail=detail, updated_at=now)
    await session.execute(stmt.on_conflict_do_update(
        index_elements=[IntegrationStatus.name],
        set_={"last_ok_at": now, "updated_at": now, "detail": IntegrationStatus.detail.op("||")(stmt.excluded.detail)},
    ))
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
