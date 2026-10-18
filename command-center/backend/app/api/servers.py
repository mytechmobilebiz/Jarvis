"""Managed servers (reached only through cc-gate), host-key pinning, the SSH key and the command audit trail."""

from __future__ import annotations

import base64
import hashlib
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, secrets
from ..config import Settings, get_settings
from ..models import Server, ServerCommand, Site
from ..ssh import keys
from ..ssh.client import SSHError, scan_host_key
from .deps import Principal, client_ip, get_db, require_admin, require_operator, require_user, to_dict

router = APIRouter(tags=["servers"])

HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$|^[0-9a-f:.]{2,45}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
HOST_KEY_RE = re.compile(r"^(ssh-ed25519|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521|ssh-rsa) [A-Za-z0-9+/]+={0,3}$")


def _env(v: str) -> str:
    if v not in ("production", "staging", "test"):
        raise ValueError("environment must be production, staging or test")
    return v


class ServerBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(22, ge=1, le=65535)
    username: str = Field("ccagent", max_length=32)
    environment: str = "production"
    notes: str = Field("", max_length=4000)

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip().lower()
        if not HOST_RE.match(v):
            raise ValueError("not a host name or IP address")
        return v

    @field_validator("username")
    @classmethod
    def _user(cls, v: str) -> str:
        if v == "root" or not USER_RE.match(v):
            raise ValueError("use the dedicated unprivileged account (never root)")
        return v

    @field_validator("environment")
    @classmethod
    def _environment(cls, v: str) -> str:
        return _env(v)


class ServerPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    environment: str | None = None
    enabled: bool | None = None
    notes: str | None = Field(None, max_length=4000)

    @field_validator("environment")
    @classmethod
    def _environment(cls, v: str | None) -> str | None:
        return None if v is None else _env(v)


class TrustBody(BaseModel):
    # Either the fingerprint shown after a scan (compared by a person against the
    # server), or the server's public host key pasted from the server itself.
    fingerprint: str = Field("", max_length=100)
    host_key: str = Field("", max_length=2000)


class KeyBody(BaseModel):
    replace: bool = False


def fingerprint_of(openssh: str) -> str:
    blob = base64.b64decode(openssh.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


async def server_view(session: AsyncSession, server: Server) -> dict:
    d = to_dict(server)
    d["host_key_pinned"] = bool(server.host_key)
    d["sites"] = [
        {"id": s.id, "name": s.name, "server_site_key": s.server_site_key, "environment": s.environment}
        for s in (await session.scalars(select(Site).where(Site.server_id == server.id).order_by(Site.name))).all()
    ]
    return d


async def _server(session: AsyncSession, server_id: int, *, lock: bool = False) -> Server:
    server = await session.get(Server, server_id, with_for_update=lock)
    if server is None:
        raise HTTPException(404, "no such server")
    return server


@router.get("/api/servers")
async def list_servers(_: Principal = Depends(require_user), session: AsyncSession = Depends(get_db)) -> dict:
    servers = (await session.scalars(select(Server).order_by(Server.name))).all()
    return {"servers": [await server_view(session, s) for s in servers]}


@router.post("/api/servers", status_code=201)
async def create_server(body: ServerBody, request: Request, principal: Principal = Depends(require_admin),
                        session: AsyncSession = Depends(get_db)) -> dict:
    server = Server(name=body.name.strip(), host=body.host, port=body.port, username=body.username,
                    environment=body.environment, notes=body.notes, created_by=principal.name)
    session.add(server)
    try:
        await session.flush()
    except IntegrityError:
        raise HTTPException(409, "a server with that name already exists") from None
    await audit.record(session, principal.name, "server.created", "server", server.id,
                       {"host": server.host, "port": server.port, "username": server.username, "environment": server.environment},
                       ip=client_ip(request))
    await session.commit()
    return {"server": await server_view(session, server)}


@router.get("/api/servers/{server_id}")
async def get_server(server_id: int, _: Principal = Depends(require_user), session: AsyncSession = Depends(get_db)) -> dict:
    return {"server": await server_view(session, await _server(session, server_id))}


@router.patch("/api/servers/{server_id}")
async def patch_server(server_id: int, body: ServerPatch, request: Request, principal: Principal = Depends(require_admin),
                       session: AsyncSession = Depends(get_db)) -> dict:
    server = await _server(session, server_id, lock=True)
    changes = body.model_dump(exclude_none=True)
    for key, value in changes.items():
        setattr(server, key, value)
    server.updated_at = datetime.now(UTC)
    await audit.record(session, principal.name, "server.updated", "server", server.id, changes, ip=client_ip(request))
    await session.commit()
    return {"server": await server_view(session, server)}


@router.post("/api/servers/{server_id}/host-key/scan")
async def scan_key(server_id: int, request: Request, principal: Principal = Depends(require_admin),
                   session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    """Ask the server for its host key. Nothing is trusted until a person compares the fingerprint and pins it."""
    server = await _server(session, server_id, lock=True)
    try:
        offered, fingerprint = await scan_host_key(server.host, server.port, settings)
    except SSHError as exc:
        raise HTTPException(502, str(exc)) from None
    server.offered_host_key, server.offered_fingerprint = offered, fingerprint
    server.updated_at = datetime.now(UTC)
    await audit.record(session, principal.name, "server.host_key_scanned", "server", server.id, {"fingerprint": fingerprint}, ip=client_ip(request))
    await session.commit()
    return {"fingerprint": fingerprint, "host_key": offered, "matches_pinned": offered == server.host_key if server.host_key else None}


@router.post("/api/servers/{server_id}/host-key/trust")
async def trust_key(server_id: int, body: TrustBody, request: Request, principal: Principal = Depends(require_admin),
                    session: AsyncSession = Depends(get_db)) -> dict:
    server = await _server(session, server_id, lock=True)
    if body.host_key:
        key = " ".join(body.host_key.strip().split()[:2])
        if not HOST_KEY_RE.match(key):
            raise HTTPException(400, "paste the server's public host key line, e.g. from /etc/ssh/ssh_host_ed25519_key.pub")
        pinned, fingerprint = key, fingerprint_of(key)
    else:
        if not server.offered_host_key:
            raise HTTPException(400, "scan the host key first, or paste the server's public host key")
        if body.fingerprint.strip() != server.offered_fingerprint:
            raise HTTPException(400, "the fingerprint does not match the one the server offered; compare it on the server")
        pinned, fingerprint = server.offered_host_key, server.offered_fingerprint
    previous = server.host_key_fingerprint
    server.host_key, server.host_key_fingerprint = pinned, fingerprint
    server.host_key_trusted_by, server.host_key_trusted_at = principal.name, datetime.now(UTC)
    server.offered_host_key, server.offered_fingerprint = "", ""
    server.updated_at = datetime.now(UTC)
    await audit.record(session, principal.name, "server.host_key_pinned", "server", server.id,
                       {"fingerprint": fingerprint, "previous": previous}, ip=client_ip(request))
    await session.commit()
    return {"server": await server_view(session, server)}


@router.delete("/api/servers/{server_id}/host-key")
async def unpin_key(server_id: int, request: Request, principal: Principal = Depends(require_admin),
                    session: AsyncSession = Depends(get_db)) -> dict:
    server = await _server(session, server_id, lock=True)
    previous = server.host_key_fingerprint
    server.host_key = server.host_key_fingerprint = server.host_key_trusted_by = ""
    server.host_key_trusted_at = None
    await audit.record(session, principal.name, "server.host_key_unpinned", "server", server.id, {"previous": previous}, ip=client_ip(request))
    await session.commit()
    return {"server": await server_view(session, server)}


@router.get("/api/servers/{server_id}/commands")
async def server_commands(server_id: int, _: Principal = Depends(require_operator), session: AsyncSession = Depends(get_db),
                          limit: int = Query(100, ge=1, le=1000)) -> dict:
    await _server(session, server_id)
    rows = (await session.scalars(
        select(ServerCommand).where(ServerCommand.server_id == server_id).order_by(ServerCommand.id.desc()).limit(limit)
    )).all()
    total = await session.scalar(select(func.count()).select_from(ServerCommand).where(ServerCommand.server_id == server_id))
    return {"commands": [to_dict(r) for r in rows], "total": total}


@router.get("/api/ssh/key")
async def get_key(_: Principal = Depends(require_admin), session: AsyncSession = Depends(get_db)) -> dict:
    return {"key": await keys.public_key(session)}


@router.post("/api/ssh/key", status_code=201)
async def create_key(body: KeyBody, request: Request, principal: Principal = Depends(require_admin),
                     session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    existing = await keys.public_key(session)
    if existing and not body.replace:
        raise HTTPException(409, "a key already exists; replacing it locks the Command Center out of every server until they get the new key")
    try:
        info = await keys.generate(session, settings, principal.name)
    except secrets.SecretsUnavailable as exc:
        raise HTTPException(409, str(exc)) from None
    await audit.record(session, principal.name, "ssh.key_replaced" if existing else "ssh.key_created", "ssh_key", "installation",
                       {"fingerprint": info["fingerprint"]}, ip=client_ip(request))
    await session.commit()
    return {"key": info}
