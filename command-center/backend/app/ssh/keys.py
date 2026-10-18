"""This installation's SSH key: generated here, private half stored encrypted, never shown or sent to a model."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import asyncssh
from sqlalchemy.ext.asyncio import AsyncSession

from .. import secrets
from ..config import Settings
from ..models import AppSetting

SECRET_NAME = "ssh_private_key"
SETTING_KEY = "ssh_public_key"
GATE_PATH = "/usr/local/bin/cc-gate"
KEY_COMMENT = "infinite-command-center"


class NoKey(RuntimeError):
    pass


def authorized_keys_line(openssh: str) -> str:
    """The line for the restricted account's authorized_keys: this key can only run cc-gate."""
    return f'command="{GATE_PATH}",restrict {openssh.strip()}'


async def public_key(session: AsyncSession) -> dict[str, Any] | None:
    row = await session.get(AppSetting, SETTING_KEY)
    if row is None or not row.value:
        return None
    return {**row.value, "authorized_keys_line": authorized_keys_line(row.value["openssh"])}


async def generate(session: AsyncSession, settings: Settings, actor: str) -> dict[str, Any]:
    """Create (or replace) the key pair. Replacing it locks the Command Center out until servers get the new key."""
    key = asyncssh.generate_private_key("ssh-ed25519", comment=KEY_COMMENT)
    private = key.export_private_key("openssh").decode("ascii")
    await secrets.put_secret(session, settings, SECRET_NAME, private, actor)  # raises SecretsUnavailable without CC_SECRETS_KEY
    info = {
        "openssh": key.export_public_key("openssh").decode("ascii").strip(),
        "fingerprint": key.get_fingerprint("sha256"),
        "created_at": datetime.now(UTC).isoformat(),
        "created_by": actor,
    }
    row = await session.get(AppSetting, SETTING_KEY)
    if row is None:
        session.add(AppSetting(key=SETTING_KEY, value=info, updated_by=actor))
    else:
        row.value, row.updated_by, row.updated_at = info, actor, datetime.now(UTC)
    return {**info, "authorized_keys_line": authorized_keys_line(info["openssh"])}


async def load_private_key(session: AsyncSession, settings: Settings) -> asyncssh.SSHKey:
    pem = await secrets.get_secret(session, settings, SECRET_NAME)
    if not pem:
        raise NoKey("no SSH key yet: an admin creates one on the Servers page")
    return asyncssh.import_private_key(pem)
