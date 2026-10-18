"""Encrypted storage for customer-entered secrets (API keys, credentials).

The key comes from CC_SECRETS_KEY (a deployment setting, generated at
install). Without it the store is read-only empty: nothing can be saved, so a
secret is never written in the clear.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .models import SecretValue


class SecretsUnavailable(RuntimeError):
    pass


def _fernet(settings: Settings) -> Fernet:
    raw = settings.secrets_key.get_secret_value() if settings.secrets_key else ""
    if not raw:
        raise SecretsUnavailable("CC_SECRETS_KEY is not set; secrets cannot be stored")
    # Accept any long random string: derive the 32-byte Fernet key from it.
    key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
    return Fernet(key)


def hint(value: str) -> str:
    return f"…{value[-4:]}" if len(value) >= 8 else "set"


async def put_secret(session: AsyncSession, settings: Settings, name: str, value: str, actor: str) -> SecretValue:
    token = _fernet(settings).encrypt(value.encode("utf-8")).decode("ascii")
    row = await session.get(SecretValue, name)
    if row is None:
        row = SecretValue(name=name, ciphertext=token)
        session.add(row)
    row.ciphertext = token
    row.hint = hint(value)
    row.updated_by = actor
    row.updated_at = datetime.now(UTC)
    return row


async def get_secret(session: AsyncSession, settings: Settings, name: str) -> str | None:
    row = await session.get(SecretValue, name)
    if row is None:
        return None
    try:
        return _fernet(settings).decrypt(row.ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, SecretsUnavailable):
        return None


async def delete_secret(session: AsyncSession, name: str) -> bool:
    row = await session.get(SecretValue, name)
    if row is None:
        return False
    await session.delete(row)
    return True
