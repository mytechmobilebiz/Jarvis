"""Monitored websites (the Command Center's own records; client sites are never changed)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, licensing
from .. import tasks as task_service
from ..agents.wordpress_monitor.agent import normalize_url
from ..config import Settings, get_settings
from ..models import Site, SiteCheck
from .deps import Principal, client_ip, get_db, require_operator, require_user, to_dict

router = APIRouter(prefix="/api/sites", tags=["sites"])


class SiteBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=3, max_length=2000)
    client: str = Field("", max_length=200)
    notes: str = Field("", max_length=4000)
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return normalize_url(v)


class SitePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    client: str | None = Field(None, max_length=200)
    notes: str | None = Field(None, max_length=4000)
    enabled: bool | None = None


class CheckBody(BaseModel):
    deep: bool = True


async def _site_view(session: AsyncSession, site: Site) -> dict:
    d = to_dict(site)
    check = await session.get(SiteCheck, site.last_check_id) if site.last_check_id else None
    d["last_check"] = to_dict(check) if check else None
    return d


@router.get("")
async def list_sites(_: Principal = Depends(require_user), session: AsyncSession = Depends(get_db)) -> dict:
    sites = (await session.scalars(select(Site).order_by(Site.name))).all()
    return {"sites": [await _site_view(session, s) for s in sites]}


@router.post("", status_code=201)
async def create_site(
    body: SiteBody, request: Request, principal: Principal = Depends(require_operator),
    session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings),
) -> dict:
    ent = await licensing.current_entitlements(session, settings)
    count = await session.scalar(select(func.count()).select_from(Site))
    if ent.max_sites is not None and count >= ent.max_sites:
        raise HTTPException(403, f"the {ent.edition.title} edition monitors up to {ent.max_sites} sites")
    site = Site(name=body.name.strip(), url=body.url, client=body.client.strip(), notes=body.notes, enabled=body.enabled)
    session.add(site)
    try:
        await session.flush()
    except IntegrityError:
        raise HTTPException(409, "that URL is already monitored") from None
    await audit.record(session, principal.name, "site.created", "site", site.id, {"url": site.url}, ip=client_ip(request))
    await session.commit()
    return {"site": await _site_view(session, site)}


async def _site(session: AsyncSession, site_id: int) -> Site:
    site = await session.get(Site, site_id)
    if site is None:
        raise HTTPException(404, "no such site")
    return site


@router.get("/{site_id}")
async def get_site(site_id: int, _: Principal = Depends(require_user), session: AsyncSession = Depends(get_db)) -> dict:
    return {"site": await _site_view(session, await _site(session, site_id))}


@router.patch("/{site_id}")
async def patch_site(
    site_id: int, body: SitePatch, request: Request,
    principal: Principal = Depends(require_operator), session: AsyncSession = Depends(get_db),
) -> dict:
    site = await _site(session, site_id)
    changes = body.model_dump(exclude_none=True)
    for key, value in changes.items():
        setattr(site, key, value)
    site.updated_at = datetime.now(UTC)
    await audit.record(session, principal.name, "site.updated", "site", site.id, changes, ip=client_ip(request))
    await session.commit()
    return {"site": await _site_view(session, site)}


@router.delete("/{site_id}")
async def delete_site(
    site_id: int, request: Request, principal: Principal = Depends(require_operator), session: AsyncSession = Depends(get_db)
) -> dict:
    """Stops monitoring the site and drops its check history. The website itself is untouched."""
    site = await _site(session, site_id)
    await audit.record(session, principal.name, "site.deleted", "site", site.id, {"url": site.url}, ip=client_ip(request))
    await session.delete(site)
    await session.commit()
    return {"ok": True}


@router.post("/{site_id}/check", status_code=201)
async def check_now(
    site_id: int, request: Request, body: CheckBody | None = None,
    principal: Principal = Depends(require_operator),
    session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings),
) -> dict:
    site = await _site(session, site_id)
    task = await task_service.create_task(
        session, settings, agent="wordpress_monitor", capability="check_site",
        params={"site_id": site.id, "deep": (body or CheckBody()).deep},
        source="dashboard", created_by=principal.name, priority=20,
    )
    await audit.record(session, principal.name, "site.check_requested", "site", site.id, {"task_id": task.id}, ip=client_ip(request))
    await session.commit()
    return {"task": to_dict(task)}


@router.get("/{site_id}/checks")
async def site_checks(
    site_id: int, _: Principal = Depends(require_user), session: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    await _site(session, site_id)
    rows = (
        await session.scalars(
            select(SiteCheck).where(SiteCheck.site_id == site_id).order_by(SiteCheck.checked_at.desc()).limit(limit)
        )
    ).all()
    return {"checks": [to_dict(c) for c in rows]}
