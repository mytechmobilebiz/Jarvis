"""Editions, feature entitlements and offline-verifiable license files.

An installation's edition comes from a signed license file when one is
installed and valid; otherwise from the CC_EDITION deployment setting (the
owner's own installation runs the internal "developer" edition).

License file (JSON), signed with Infinite Marketing's Ed25519 private key,
which never ships with the software:

    {"payload": {"license_id": "...", "customer": "...", "edition": "professional",
                 "issued_at": "2026-10-18", "expires_at": "2027-10-18",
                 "max_sites": 50, "max_users": 5},
     "signature": "<base64 Ed25519 signature of the canonical payload JSON>"}

Expiry never deletes or locks data: an expired license falls back to the
read-mostly behaviour of the configured edition and says so.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .models import AppSetting

FEATURES: dict[str, str] = {
    "ai_llm": "AI planning, summaries and drafts with the customer's own model API key",
    "multi_user": "More than one sign-in, with viewer / operator / admin roles",
    "api_access": "Service tokens for outside automation (n8n or other engines)",
    "client_reports": "Per-client reports and briefings",
    "credit_repair": "Credit Repair module (isolated, encrypted sensitive-data store)",
    "white_label": "Own branding on the dashboard and reports",
    "custom_workflow_engine": "Use a workflow engine other than the default",
}

PERSONAL_AGENTS = frozenset({"supervisor", "wordpress_monitor", "content", "seo"})
PROFESSIONAL_AGENTS = PERSONAL_AGENTS | {"sales", "support", "ads"}


@dataclass(frozen=True)
class Edition:
    code: str
    title: str
    sellable: bool
    max_sites: int | None
    max_users: int | None
    agents: frozenset[str] | None  # None: every agent
    features: frozenset[str]


EDITIONS: dict[str, Edition] = {
    "personal": Edition("personal", "Personal", True, 5, 1, PERSONAL_AGENTS, frozenset({"ai_llm"})),
    "professional": Edition(
        "professional", "Professional", True, 50, 5, PROFESSIONAL_AGENTS,
        frozenset({"ai_llm", "multi_user", "api_access", "client_reports"}),
    ),
    "agency": Edition("agency", "Agency", True, None, None, None, frozenset(FEATURES)),
    "developer": Edition("developer", "Developer (internal, not for sale)", False, None, None, None, frozenset(FEATURES)),
}


class LicenseError(ValueError):
    pass


def canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_license(payload: dict[str, Any], private_key: Ed25519PrivateKey) -> dict[str, Any]:
    """Used by Infinite Marketing's licensing tool, never by an installation."""
    return {"payload": payload, "signature": base64.b64encode(private_key.sign(canonical(payload))).decode("ascii")}


@dataclass
class License:
    license_id: str
    customer: str
    edition: str
    issued_at: str
    expires_at: str | None
    max_sites: int | None
    max_users: int | None

    @property
    def expired(self) -> bool:
        return bool(self.expires_at) and date.fromisoformat(self.expires_at[:10]) < datetime.now(UTC).date()


def verify_license(document: dict[str, Any], public_key_b64: str) -> License:
    if not public_key_b64:
        raise LicenseError("this installation has no license public key configured")
    try:
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        payload = document["payload"]
        signature = base64.b64decode(document["signature"])
    except (KeyError, TypeError, ValueError) as exc:
        raise LicenseError("not a license file") from exc
    try:
        public.verify(signature, canonical(payload))
    except InvalidSignature as exc:
        raise LicenseError("the license signature is not valid") from exc
    edition = str(payload.get("edition", ""))
    if edition not in EDITIONS or not EDITIONS[edition].sellable:
        raise LicenseError(f"unknown edition '{edition}'")
    return License(
        license_id=str(payload.get("license_id", "")),
        customer=str(payload.get("customer", "")),
        edition=edition,
        issued_at=str(payload.get("issued_at", "")),
        expires_at=payload.get("expires_at"),
        max_sites=payload.get("max_sites"),
        max_users=payload.get("max_users"),
    )


@dataclass
class Entitlements:
    edition: Edition
    source: str  # "license" or "configuration"
    license: License | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def max_sites(self) -> int | None:
        return self.license.max_sites if self.license and self.license.max_sites is not None else self.edition.max_sites

    @property
    def max_users(self) -> int | None:
        return self.license.max_users if self.license and self.license.max_users is not None else self.edition.max_users

    def allows_agent(self, name: str) -> bool:
        return self.edition.agents is None or name in self.edition.agents

    def has(self, feature: str) -> bool:
        return feature in self.edition.features

    def describe(self) -> dict[str, Any]:
        return {
            "edition": self.edition.code,
            "title": self.edition.title,
            "source": self.source,
            "license": None
            if not self.license
            else {
                "license_id": self.license.license_id,
                "customer": self.license.customer,
                "issued_at": self.license.issued_at,
                "expires_at": self.license.expires_at,
                "expired": self.license.expired,
            },
            "limits": {"max_sites": self.max_sites, "max_users": self.max_users},
            "agents": None if self.edition.agents is None else sorted(self.edition.agents),
            "features": {k: {"enabled": k in self.edition.features, "description": v} for k, v in FEATURES.items()},
            "notes": self.notes,
        }


_CACHE: dict[str, tuple[float, Entitlements]] = {}
_TTL = 30.0


def clear_cache() -> None:
    _CACHE.clear()


async def current_entitlements(session: AsyncSession, settings: Settings) -> Entitlements:
    cached = _CACHE.get("current")
    if cached and time.monotonic() - cached[0] < _TTL:
        return cached[1]
    configured = EDITIONS.get(settings.edition)
    notes: list[str] = []
    if configured is None:
        notes.append(f"CC_EDITION '{settings.edition}' is unknown; using Personal")
        configured = EDITIONS["personal"]
    result = Entitlements(edition=configured, source="configuration", notes=notes)
    row = await session.get(AppSetting, "license")
    if row is not None:
        try:
            lic = verify_license(row.value, settings.license_public_key)
            if lic.expired:
                notes.append(f"license {lic.license_id} expired on {lic.expires_at}; using the configured edition")
            else:
                result = Entitlements(edition=EDITIONS[lic.edition], source="license", license=lic, notes=notes)
        except LicenseError as exc:
            notes.append(f"installed license ignored: {exc}")
    _CACHE["current"] = (time.monotonic(), result)
    return result
