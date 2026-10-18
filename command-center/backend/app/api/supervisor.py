from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from .. import tasks as task_service
from ..config import Settings, get_settings
from ..models import SupervisorRequest, Task
from .deps import Principal, client_ip, get_db, require_operator_or_service, require_user, to_dict

router = APIRouter(prefix="/api/supervisor", tags=["supervisor"])


class RequestBody(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


async def submit_request(
    session: AsyncSession, settings: Settings, message: str, *, source: str, requested_by: str
) -> SupervisorRequest:
    request = SupervisorRequest(message=message.strip(), source=source, requested_by=requested_by, status="planning")
    session.add(request)
    await session.flush()
    await task_service.create_task(
        session,
        settings,
        agent="supervisor",
        capability="plan_request",
        params={"request_id": request.id},
        source="supervisor",
        created_by=requested_by,
        request_id=request.id,
        priority=10,
    )
    return request


@router.post("/requests", status_code=202)
async def create_request(
    body: RequestBody,
    request: Request,
    principal: Principal = Depends(require_operator_or_service),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    source = "n8n" if principal.is_service else "dashboard"
    req = await submit_request(session, settings, body.message, source=source, requested_by=principal.name)
    await audit.record(session, principal.name, "supervisor.request", "supervisor_request", req.id, {"message": body.message[:500]}, ip=client_ip(request))
    await session.commit()
    return {"request": to_dict(req)}


@router.get("/requests")
async def list_requests(
    _: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_db),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    total = await session.scalar(select(func.count()).select_from(SupervisorRequest))
    rows = (
        await session.scalars(select(SupervisorRequest).order_by(SupervisorRequest.id.desc()).limit(limit).offset(offset))
    ).all()
    return {"requests": [to_dict(r) for r in rows], "total": total}


@router.get("/requests/{request_id}")
async def get_request(request_id: int, _: Principal = Depends(require_user), session: AsyncSession = Depends(get_db)) -> dict:
    req = await session.get(SupervisorRequest, request_id)
    if req is None:
        raise HTTPException(404, "no such request")
    tasks = (await session.scalars(select(Task).where(Task.request_id == request_id).order_by(Task.id))).all()
    return {"request": to_dict(req), "tasks": [to_dict(t) for t in tasks]}
