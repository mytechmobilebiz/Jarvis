from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..config import Settings, get_settings
from ..models import Session, User
from ..security import hash_password, hash_token, new_session_token, verify_password
from .deps import Principal, client_ip, get_db, require_user

router = APIRouter(prefix="/api/auth", tags=["auth"])

# A real hash, so a login for an unknown email costs as much as a wrong password.
_DUMMY_HASH = hash_password("timing-equaliser-not-a-password")


class LoginBody(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)


class PasswordBody(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


def user_view(user: User) -> dict:
    return {"id": user.id, "email": user.email, "display_name": user.display_name, "role": user.role,
            "can_access_sensitive": user.can_access_sensitive}


@router.post("/login")
async def login(
    body: LoginBody,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    throttle = request.app.state.login_throttle
    ip = client_ip(request)
    email = body.email.strip().lower()
    key = f"{ip}|{email}"
    if throttle.is_locked(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many failed attempts; try again later")
    user = await session.scalar(select(User).where(User.email == email))
    ok = verify_password(body.password, user.password_hash if user else _DUMMY_HASH)
    if not user or not ok or not user.is_active:
        throttle.record_failure(key)
        await audit.record(session, email or "?", "auth.login_failed", "user", email, ip=ip)
        await session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong email or password")
    throttle.reset(key)
    token = new_session_token()
    now = datetime.now(UTC)
    session.add(
        Session(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=now + timedelta(hours=settings.session_ttl_hours),
            user_agent=request.headers.get("user-agent", "")[:300],
            ip=ip,
        )
    )
    user.last_login_at = now
    await audit.record(session, user.email, "auth.login", "user", user.id, ip=ip)
    await session.commit()
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return {"user": user_view(user)}


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        await session.execute(delete(Session).where(Session.token_hash == hash_token(token)))
        await session.commit()
    response.delete_cookie(settings.session_cookie_name, path="/")
    return {"ok": True}


@router.get("/me")
async def me(principal: Principal = Depends(require_user), session: AsyncSession = Depends(get_db)) -> dict:
    user = await session.get(User, principal.user_id)
    return {"user": user_view(user)}


@router.post("/password")
async def change_password(
    body: PasswordBody,
    request: Request,
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    user = await session.get(User, principal.user_id)
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "current password is wrong")
    user.password_hash = hash_password(body.new_password)
    # Sign out every other session.
    current = hash_token(request.cookies.get(settings.session_cookie_name, ""))
    await session.execute(delete(Session).where(Session.user_id == user.id, Session.token_hash != current))
    await audit.record(session, user.email, "auth.password_changed", "user", user.id, ip=client_ip(request))
    await session.commit()
    return {"ok": True}
