"""Request dependencies: database session, the caller's identity, permissions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..db import sessionmaker
from ..models import Session, User
from ..security import CSRF_HEADER, CSRF_VALUE, SESSION_COOKIE, hash_token, tokens_equal

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
OPERATOR_ROLES = frozenset({"admin", "operator"})


@dataclass(frozen=True)
class Principal:
    kind: str  # "user" or "service"
    name: str
    role: str
    user_id: int | None = None

    @property
    def is_service(self) -> bool:
        return self.kind == "service"


async def get_db() -> AsyncIterator[AsyncSession]:
    async with sessionmaker()() as session:
        yield session


def client_ip(request: Request) -> str:
    real = request.headers.get("x-real-ip")
    if real:
        return real[:64]
    return request.client.host if request.client else ""


async def current_principal(
    request: Request,
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Principal:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        expected = settings.service_token.get_secret_value() if settings.service_token else ""
        if expected and tokens_equal(token, expected):
            return Principal(kind="service", name="service:n8n", role="service")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid service token")

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not signed in")
    now = datetime.now(UTC)
    row = (
        await session.execute(
            select(Session, User)
            .join(User, User.id == Session.user_id)
            .where(Session.token_hash == hash_token(token), Session.expires_at > now, User.is_active.is_(True))
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired")
    sess, user = row
    if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing X-Requested-With header")
    if now - sess.last_seen_at > timedelta(minutes=5):
        sess.last_seen_at = now
        await session.commit()
    return Principal(kind="user", name=user.email, role=user.role, user_id=user.id)


async def require_user(principal: Principal = Depends(current_principal)) -> Principal:
    """Any signed-in person (viewers included). Read-only endpoints use this."""
    if principal.is_service:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this endpoint is for signed-in users")
    return principal


async def require_operator(principal: Principal = Depends(current_principal)) -> Principal:
    """A person allowed to run and approve work. The n8n service token never qualifies."""
    if principal.is_service or principal.role not in OPERATOR_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "operator or admin role required")
    return principal


async def require_admin(principal: Principal = Depends(current_principal)) -> Principal:
    if principal.is_service or principal.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    return principal


async def require_operator_or_service(principal: Principal = Depends(current_principal)) -> Principal:
    if principal.is_service or principal.role in OPERATOR_ROLES:
        return principal
    raise HTTPException(status.HTTP_403_FORBIDDEN, "operator role or service token required")


async def require_service(principal: Principal = Depends(current_principal)) -> Principal:
    if not principal.is_service:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "service token required")
    return principal


def to_dict(obj: Any, exclude: tuple[str, ...] = ()) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in obj.__table__.columns:
        if col.key in exclude:
            continue
        value = getattr(obj, col.key)
        out[col.key] = value.isoformat() if isinstance(value, datetime) else value
    return out
