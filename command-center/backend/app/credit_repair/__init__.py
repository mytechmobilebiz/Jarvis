"""The isolated Credit Repair module (see COMMERCIALIZATION.md section 4)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..models import AppSetting

ENABLED_KEY = "credit_repair_enabled"

COMPLIANCE_NOTICE = (
    "Credit repair is regulated. Before using this module with real clients, the business is responsible for complying with "
    "the federal Credit Repair Organizations Act (written contract, required disclosures, a 3-business-day right to cancel, "
    "and no fees before services are fully performed), the Fair Credit Reporting Act dispute process, the FTC Telemarketing "
    "Sales Rule, and state credit-services laws (some require registration or bonding, or restrict these services), and for "
    "protecting clients' personal data. This software helps organise the work; it is not legal advice. Have the module and "
    "your processes reviewed by a qualified lawyer first."
)


async def enabled_state(session: AsyncSession) -> dict | None:
    row = await session.get(AppSetting, ENABLED_KEY)
    return row.value if row and isinstance(row.value, dict) and row.value.get("enabled") else None


def is_configured(settings: Settings) -> bool:
    from .store import configured

    return configured(settings)
