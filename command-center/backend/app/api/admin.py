"""Customer settings, secrets, edition and license, and user management (admins only)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, customer_settings, licensing
from ..config import Settings, get_settings
from ..models import AppSetting, Session, User
from ..secrets import SecretsUnavailable, delete_secret, put_secret
from ..security import hash_password
from .deps import Principal, client_ip, get_db, require_admin, require_user

router = APIRouter(prefix="/api", tags=["admin"])


class SecretBody(BaseModel):
    value: str = Field(min_length=8, max_length=4000)


class LicenseBody(BaseModel):
    license: dict[str, Any] | str


class UserBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field("", max_length=200)
    role: str = Field(pattern="^(admin|operator|viewer)$")
    password: str = Field(min_length=12, max_length=1024)


class UserPatch(BaseModel):
    can_access_sensitive: bool | None = None
    display_name: str | None = Field(None, max_length=200)
    role: str | None = Field(None, pattern="^(admin|operator|viewer)$")
    is_active: bool | None = None
    password: str | None = Field(None, min_length=12, max_length=1024)


# ------------------------------------------------------------------ edition

@router.get("/system/edition")
async def edition(
    _: Principal = Depends(require_user), session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)
) -> dict:
    ent = await licensing.current_entitlements(session, settings)
    users = await session.scalar(select(func.count()).select_from(User).where(User.is_active.is_(True)))
    from ..models import Site

    sites = await session.scalar(select(func.count()).select_from(Site))
    return {**ent.describe(), "usage": {"sites": sites, "users": users}}


@router.post("/system/license")
async def install_license(
    body: LicenseBody, request: Request,
    principal: Principal = Depends(require_admin), session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings),
) -> dict:
    document = body.license
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except json.JSONDecodeError:
            raise HTTPException(400, "the license is not valid JSON") from None
    try:
        lic = licensing.verify_license(document, settings.license_public_key)
    except licensing.LicenseError as exc:
        raise HTTPException(400, str(exc)) from None
    row = await session.get(AppSetting, "license")
    if row is None:
        row = AppSetting(key="license", value=document)
        session.add(row)
    row.value = document
    row.updated_by = principal.name
    row.updated_at = datetime.now(UTC)
    await audit.record(session, principal.name, "license.installed", "license", lic.license_id, {"edition": lic.edition}, ip=client_ip(request))
    await session.commit()
    licensing.clear_cache()
    return {"license_id": lic.license_id, "edition": lic.edition, "expires_at": lic.expires_at}


@router.delete("/system/license")
async def remove_license(
    request: Request, principal: Principal = Depends(require_admin), session: AsyncSession = Depends(get_db)
) -> dict:
    await session.execute(delete(AppSetting).where(AppSetting.key == "license"))
    await audit.record(session, principal.name, "license.removed", "license", "", ip=client_ip(request))
    await session.commit()
    licensing.clear_cache()
    return {"ok": True}


# ------------------------------------------------------- customer settings

@router.get("/settings")
async def get_customer_settings(
    _: Principal = Depends(require_admin), session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)
) -> dict:
    values = await customer_settings.load(session, settings)
    ai, _key = await customer_settings.resolve_ai(session, settings)
    return {
        "settings": {
            key: {"value": values[key], "label": spec.label, "description": spec.description, "kind": spec.kind, "choices": list(spec.choices)}
            for key, spec in customer_settings.SPECS.items()
        },
        "secrets": await customer_settings.secrets_overview(session),
        "secrets_available": bool(settings.secrets_key and settings.secrets_key.get_secret_value()),
        "ai": {"provider": ai.provider, "model": ai.model, "daily_cap": ai.daily_cap, "key_source": ai.key_source, "reason": ai.reason},
    }


@router.patch("/settings")
async def patch_customer_settings(
    body: dict[str, Any], request: Request,
    principal: Principal = Depends(require_admin), session: AsyncSession = Depends(get_db),
) -> dict:
    try:
        changed = await customer_settings.update(session, body, principal.name)
    except customer_settings.SettingError as exc:
        raise HTTPException(400, str(exc)) from None
    await audit.record(session, principal.name, "settings.updated", "settings", "", changed, ip=client_ip(request))
    await session.commit()
    return {"updated": changed}


@router.put("/settings/secrets/{name}")
async def set_secret(
    name: str, body: SecretBody, request: Request,
    principal: Principal = Depends(require_admin), session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings),
) -> dict:
    if name not in customer_settings.SECRETS:
        raise HTTPException(404, "unknown secret")
    if name == "anthropic_api_key" and not body.value.startswith("sk-ant-"):
        raise HTTPException(400, "that does not look like an Anthropic API key (sk-ant-...)")
    try:
        row = await put_secret(session, settings, name, body.value.strip(), principal.name)
    except SecretsUnavailable as exc:
        raise HTTPException(409, str(exc)) from None
    # The value itself is never logged or returned.
    await audit.record(session, principal.name, "secret.set", "secret", name, ip=client_ip(request))
    await session.commit()
    customer_settings.clear_ai_cache()
    return {"name": name, "set": True, "hint": row.hint}


@router.delete("/settings/secrets/{name}")
async def remove_secret(
    name: str, request: Request, principal: Principal = Depends(require_admin), session: AsyncSession = Depends(get_db)
) -> dict:
    if name not in customer_settings.SECRETS:
        raise HTTPException(404, "unknown secret")
    removed = await delete_secret(session, name)
    await audit.record(session, principal.name, "secret.removed", "secret", name, ip=client_ip(request))
    await session.commit()
    customer_settings.clear_ai_cache()
    return {"name": name, "removed": removed}


# -------------------------------------------------------------------- users

def user_view(u: User) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "display_name": u.display_name,
        "role": u.role,
        "is_active": u.is_active,
        "can_access_sensitive": u.can_access_sensitive,
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
    }


async def _active_admins(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True)))


@router.get("/users")
async def list_users(_: Principal = Depends(require_admin), session: AsyncSession = Depends(get_db)) -> dict:
    return {"users": [user_view(u) for u in (await session.scalars(select(User).order_by(User.id))).all()]}


@router.post("/users", status_code=201)
async def create_user(
    body: UserBody, request: Request,
    principal: Principal = Depends(require_admin), session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings),
) -> dict:
    ent = await licensing.current_entitlements(session, settings)
    active = await session.scalar(select(func.count()).select_from(User).where(User.is_active.is_(True)))
    if ent.max_users is not None and active >= ent.max_users:
        raise HTTPException(403, f"the {ent.edition.title} edition allows {ent.max_users} user(s)")
    user = User(
        email=body.email.strip().lower(), display_name=body.display_name, role=body.role, password_hash=hash_password(body.password)
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        raise HTTPException(409, "a user with that email already exists") from None
    await audit.record(session, principal.name, "user.created", "user", user.id, {"email": user.email, "role": user.role}, ip=client_ip(request))
    await session.commit()
    return {"user": user_view(user)}


@router.patch("/users/{user_id}")
async def patch_user(
    user_id: int, body: UserPatch, request: Request,
    principal: Principal = Depends(require_admin), session: AsyncSession = Depends(get_db),
) -> dict:
    user = await session.get(User, user_id, with_for_update=True)
    if user is None:
        raise HTTPException(404, "no such user")
    demoting = (body.role is not None and body.role != "admin") or body.is_active is False
    if user.role == "admin" and user.is_active and demoting and await _active_admins(session) <= 1:
        raise HTTPException(400, "there must always be at least one active admin")
    if user.id == principal.user_id and body.is_active is False:
        raise HTTPException(400, "you cannot deactivate your own account")
    changes = body.model_dump(exclude_none=True, exclude={"password"})
    for key, value in changes.items():
        setattr(user, key, value)
    if body.password:
        user.password_hash = hash_password(body.password)
        changes["password"] = "changed"
    if body.password or body.is_active is False:
        await session.execute(delete(Session).where(Session.user_id == user.id))
    await audit.record(session, principal.name, "user.updated", "user", user.id, changes, ip=client_ip(request))
    await session.commit()
    return {"user": user_view(user)}
