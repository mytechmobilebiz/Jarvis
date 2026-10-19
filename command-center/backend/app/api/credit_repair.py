"""The Credit Repair module's API. Every endpoint touching client data needs the
sensitive-data permission and writes to the module's own access log."""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, credit_repair, licensing
from ..config import Settings, get_settings
from ..credit_repair import store
from ..models import AppSetting, User
from .deps import OPERATOR_ROLES, Principal, client_ip, get_db, require_admin, require_user

router = APIRouter(prefix="/api/credit-repair", tags=["credit repair"])


class EnableBody(BaseModel):
    acknowledge: bool


class ClientBody(BaseModel):
    full_name: str = Field(min_length=2, max_length=200)
    address: str = Field("", max_length=500)
    email: str = Field("", max_length=320)
    phone: str = Field("", max_length=50)
    dob: str = Field("", max_length=10, description="YYYY-MM-DD")
    ssn_last4: str = Field("", pattern=r"^(\d{4})?$")
    notes: str = Field("", max_length=4000)


class ClientPatch(BaseModel):
    status: str | None = Field(None, pattern="^(intake|active|paused|closed)$")
    contract_signed_on: date | None = None
    services_completed_on: date | None = None
    fees_collected_cents: int | None = Field(None, ge=0)


class ItemBody(BaseModel):
    bureau: str = Field(pattern="^(equifax|experian|transunion)$")
    creditor: str = Field(min_length=1, max_length=200)
    account_last4: str = Field("", pattern=r"^(\d{4})?$")
    reason: str = Field(pattern="^(not_mine|inaccurate_balance|inaccurate_status|duplicate|outdated|other)$")
    detail: str = Field("", max_length=1000)


async def sensitive_user(principal: Principal = Depends(require_user), session: AsyncSession = Depends(get_db),
                         settings: Settings = Depends(get_settings)) -> Principal:
    user = await session.get(User, principal.user_id)
    if user is None or not user.can_access_sensitive:
        raise HTTPException(403, "your account needs the sensitive-data permission")
    if not store.configured(settings):
        raise HTTPException(409, "the Credit Repair store is not configured on this installation")
    if await credit_repair.enabled_state(session) is None:
        raise HTTPException(409, "the Credit Repair module is not enabled")
    return principal


async def sensitive_writer(principal: Principal = Depends(sensitive_user)) -> Principal:
    """Changing client records also needs the operator or admin role; viewers with the permission can only read."""
    if principal.role not in OPERATOR_ROLES:
        raise HTTPException(403, "operator or admin role required")
    return principal


@router.get("/status")
async def status(principal: Principal = Depends(require_user), session: AsyncSession = Depends(get_db),
                 settings: Settings = Depends(get_settings)) -> dict:
    ent = await licensing.current_entitlements(session, settings)
    user = await session.get(User, principal.user_id)
    return {
        "configured": store.configured(settings),
        "entitled": ent.has("credit_repair"),
        "enabled": await credit_repair.enabled_state(session),
        "can_access": bool(user and user.can_access_sensitive),
        "notice": credit_repair.COMPLIANCE_NOTICE,
    }


@router.post("/enable")
async def enable(body: EnableBody, request: Request, principal: Principal = Depends(require_admin),
                 session: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    if not body.acknowledge:
        raise HTTPException(400, "the compliance notice must be acknowledged")
    if not (await licensing.current_entitlements(session, settings)).has("credit_repair"):
        raise HTTPException(403, "the Credit Repair module is not included in this edition")
    if not store.configured(settings):
        raise HTTPException(409, "the Credit Repair store is not configured on this installation")
    value = {"enabled": True, "acknowledged_by": principal.name, "acknowledged_at": datetime.now(UTC).isoformat(),
             "notice": credit_repair.COMPLIANCE_NOTICE}
    row = await session.get(AppSetting, credit_repair.ENABLED_KEY)
    if row is None:
        session.add(AppSetting(key=credit_repair.ENABLED_KEY, value=value, updated_by=principal.name))
    else:
        row.value, row.updated_by = value, principal.name
    await audit.record(session, principal.name, "credit_repair.enabled", "module", "credit_repair", ip=client_ip(request))
    await session.commit()
    return {"enabled": value}


@router.post("/disable")
async def disable(request: Request, principal: Principal = Depends(require_admin), session: AsyncSession = Depends(get_db)) -> dict:
    row = await session.get(AppSetting, credit_repair.ENABLED_KEY)
    if row is not None:
        await session.delete(row)
    await audit.record(session, principal.name, "credit_repair.disabled", "module", "credit_repair", ip=client_ip(request))
    await session.commit()
    return {"enabled": None}


@router.get("/clients")
async def list_clients(principal: Principal = Depends(sensitive_user), settings: Settings = Depends(get_settings)) -> dict:
    cipher = store.Cipher(settings)
    async with store.session(settings) as s:
        clients = (await s.scalars(select(store.CRClient).order_by(store.CRClient.id))).all()
        await store.log_access(s, principal.name, "list_clients", count=len(clients))
        return {"clients": [store.client_view(c, cipher) for c in clients]}


@router.post("/clients", status_code=201)
async def create_client(body: ClientBody, principal: Principal = Depends(sensitive_writer), settings: Settings = Depends(get_settings)) -> dict:
    cipher = store.Cipher(settings)
    async with store.session(settings) as s:
        client = store.CRClient(
            created_by=principal.name, reference=await store.next_reference(s),
            full_name_enc=cipher.enc(body.full_name), address_enc=cipher.enc(body.address), email_enc=cipher.enc(body.email),
            phone_enc=cipher.enc(body.phone), dob_enc=cipher.enc(body.dob), ssn_last4_enc=cipher.enc(body.ssn_last4), notes_enc=cipher.enc(body.notes),
        )
        s.add(client)
        await s.flush()
        await store.log_access(s, principal.name, "create_client", client.id)
        return {"client": store.client_view(client, cipher)}


@router.get("/clients/{client_id}")
async def get_client(client_id: int, principal: Principal = Depends(sensitive_user), settings: Settings = Depends(get_settings)) -> dict:
    cipher = store.Cipher(settings)
    async with store.session(settings) as s:
        client = await s.get(store.CRClient, client_id)
        if client is None:
            raise HTTPException(404, "no such client")
        items = (await s.scalars(select(store.CRItem).where(store.CRItem.client_id == client_id).order_by(store.CRItem.id))).all()
        letters = (await s.scalars(select(store.CRLetter).where(store.CRLetter.client_id == client_id).order_by(store.CRLetter.id))).all()
        await store.log_access(s, principal.name, "view_client", client_id)
        modes = store.item_mail_modes(list(letters))
        return {
            "client": store.client_view(client, cipher, full=True),
            "items": [{"id": i.id, "bureau": i.bureau, "creditor": cipher.dec(i.creditor_enc), "account_last4": cipher.dec(i.account_last4_enc),
                       "reason": i.reason, "detail": cipher.dec(i.detail_enc), "status": i.status, "mail_mode": modes.get(i.id)}
                      for i in items],
            "letters": [store.letter_view(l) for l in letters],
        }


@router.patch("/clients/{client_id}")
async def patch_client(client_id: int, body: ClientPatch, principal: Principal = Depends(sensitive_writer),
                       settings: Settings = Depends(get_settings)) -> dict:
    cipher = store.Cipher(settings)
    async with store.session(settings) as s:
        client = await s.get(store.CRClient, client_id)
        if client is None:
            raise HTTPException(404, "no such client")
        changes = body.model_dump(exclude_none=True)
        for key, value in changes.items():
            setattr(client, key, value)
        client.updated_at = datetime.now(UTC)
        await store.log_access(s, principal.name, "update_client", client_id, fields=sorted(changes))
        return {"client": store.client_view(client, cipher)}


@router.post("/clients/{client_id}/items", status_code=201)
async def add_item(client_id: int, body: ItemBody, principal: Principal = Depends(sensitive_writer), settings: Settings = Depends(get_settings)) -> dict:
    cipher = store.Cipher(settings)
    async with store.session(settings) as s:
        if await s.get(store.CRClient, client_id) is None:
            raise HTTPException(404, "no such client")
        item = store.CRItem(client_id=client_id, bureau=body.bureau, creditor_enc=cipher.enc(body.creditor),
                            account_last4_enc=cipher.enc(body.account_last4), reason=body.reason, detail_enc=cipher.enc(body.detail))
        s.add(item)
        await s.flush()
        await store.log_access(s, principal.name, "add_item", client_id, item_id=item.id)
        return {"item_id": item.id}


@router.get("/letters/{letter_id}")
async def get_letter(letter_id: int, principal: Principal = Depends(sensitive_user), settings: Settings = Depends(get_settings)) -> dict:
    cipher = store.Cipher(settings)
    async with store.session(settings) as s:
        letter = await s.get(store.CRLetter, letter_id)
        if letter is None:
            raise HTTPException(404, "no such letter")
        await store.log_access(s, principal.name, "view_letter", letter.client_id, letter_id=letter_id)
        return {"letter": {"id": letter.id, "client_id": letter.client_id, "bureau": letter.bureau, "status": letter.status,
                           "mail_mode": store.mail_mode(letter), "mailed_at": letter.mailed_at.isoformat() if letter.mailed_at else None,
                           "content": cipher.dec(letter.content_enc)}}


@router.get("/access-log")
async def access_log(principal: Principal = Depends(sensitive_user), session: AsyncSession = Depends(get_db),
                     settings: Settings = Depends(get_settings)) -> dict:
    if principal.role != "admin":
        raise HTTPException(403, "admin role required")
    async with store.session(settings) as s:
        rows = (await s.scalars(select(store.CRAccessLog).order_by(store.CRAccessLog.id.desc()).limit(500))).all()
        return {"entries": [{"id": r.id, "at": r.at.isoformat(), "actor": r.actor, "action": r.action, "client_id": r.client_id, "detail": r.detail}
                            for r in rows]}
