"""Customer-facing settings: what an admin may change on the Settings page.

Only the keys declared here can be read or written through the API; anything
about the installation itself (database, ports, deployment secrets) is a
deployment setting in app/config.py and is never exposed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .models import AppSetting, SecretValue


class SettingError(ValueError):
    pass


@dataclass(frozen=True)
class Spec:
    label: str
    description: str
    kind: str  # "text", "choice", "integer", "timezone"
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    max_length: int = 300

    def clean(self, value: Any) -> Any:
        if self.kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise SettingError("must be a whole number")
            if self.minimum is not None and value < self.minimum or self.maximum is not None and value > self.maximum:
                raise SettingError(f"must be between {self.minimum} and {self.maximum}")
            return value
        if not isinstance(value, str):
            raise SettingError("must be text")
        value = value.strip()
        if len(value) > self.max_length:
            raise SettingError(f"must be at most {self.max_length} characters")
        if self.kind == "choice" and value not in self.choices:
            raise SettingError(f"must be one of: {', '.join(self.choices)}")
        if self.kind == "timezone":
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise SettingError("is not a known time zone (e.g. America/New_York)") from None
        return value


SPECS: dict[str, Spec] = {
    "business_name": Spec("Business name", "Business name shown on the dashboard and in reports", "text", max_length=200),
    "timezone": Spec("Time zone", "Time zone for reports and schedules", "timezone", max_length=64),
    "notification_email": Spec("Notification email", "Where alerts should go once a notification channel is approved", "text", max_length=320),
    "ai_provider": Spec("AI provider", "AI provider: auto (Claude if a key is set), anthropic, or rule_based (no AI model)", "choice",
                        choices=("auto", "anthropic", "rule_based")),
    "ai_model": Spec("AI model", "Claude model used by the supervisor and agents", "text", max_length=100),
    "ai_daily_request_cap": Spec("AI requests per day", "Most AI requests per day (protects your bill)", "integer", minimum=1, maximum=100_000),
    # Read by app/brain/store.py (AI_CONTEXT_KEY): memory goes into AI drafting prompts only when this is
    # approved_general, and then only approved, general business and knowledge notes (never customer data, rules or
    # the conversation archive).
    "brain_ai_context": Spec("JARVIS Brain in AI prompts",
                             "off (default): memory never goes to the AI model. approved_general: approved, general "
                             "notes (no customer data, no rules, no conversation history) may be added to AI drafting "
                             "prompts as reference material", "choice", choices=("off", "approved_general")),
}

SECRETS: dict[str, tuple[str, str]] = {
    "anthropic_api_key": ("Anthropic API key", "Starts with sk-ant-; usage is billed to your own Anthropic account"),
}


def defaults(settings: Settings) -> dict[str, Any]:
    return {
        "business_name": "Infinite Marketing",
        "timezone": "America/New_York",
        "notification_email": "",
        "ai_provider": settings.llm_provider,
        "ai_model": settings.llm_model,
        "ai_daily_request_cap": settings.llm_daily_request_cap,
        "brain_ai_context": "off",
    }


async def load(session: AsyncSession, settings: Settings) -> dict[str, Any]:
    values = defaults(settings)
    rows = (await session.scalars(select(AppSetting).where(AppSetting.key.in_(SPECS)))).all()
    for row in rows:
        values[row.key] = row.value
    return values


async def update(session: AsyncSession, changes: dict[str, Any], actor: str) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in changes.items():
        spec = SPECS.get(key)
        if spec is None:
            raise SettingError(f"'{key}' is not a customer setting")
        try:
            cleaned[key] = spec.clean(value)
        except SettingError as exc:
            raise SettingError(f"{key} {exc}") from None
    for key, value in cleaned.items():
        row = await session.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key, value=value)
            session.add(row)
        row.value = value
        row.updated_by = actor
        row.updated_at = datetime.now(UTC)
    clear_ai_cache()
    return cleaned


async def secrets_overview(session: AsyncSession) -> dict[str, Any]:
    rows = {r.name: r for r in (await session.scalars(select(SecretValue).where(SecretValue.name.in_(SECRETS)))).all()}
    return {
        name: {
            "label": label,
            "description": description,
            "set": name in rows,
            "hint": rows[name].hint if name in rows else "",
            "updated_at": rows[name].updated_at.isoformat() if name in rows else None,
            "updated_by": rows[name].updated_by if name in rows else "",
        }
        for name, (label, description) in SECRETS.items()
    }


# ---------------------------------------------------------------- AI runtime

@dataclass(frozen=True)
class AIConfig:
    provider: str  # "anthropic" or "rule_based"
    model: str
    daily_cap: int
    key_source: str  # "customer", "deployment" or ""
    reason: str = ""


_AI_CACHE: dict[str, tuple[float, AIConfig, str]] = {}


def clear_ai_cache() -> None:
    _AI_CACHE.clear()


async def resolve_ai(session: AsyncSession, settings: Settings) -> tuple[AIConfig, str]:
    """The effective AI configuration and API key (customer settings first, then deployment defaults)."""
    from . import licensing
    from .secrets import get_secret

    cached = _AI_CACHE.get("ai")
    if cached and time.monotonic() - cached[0] < 30:
        return cached[1], cached[2]
    values = await load(session, settings)
    entitlements = await licensing.current_entitlements(session, settings)
    key = await get_secret(session, settings, "anthropic_api_key") or ""
    source = "customer" if key else ""
    if not key and settings.anthropic_api_key:
        key = settings.anthropic_api_key.get_secret_value()
        source = "deployment" if key else ""
    wanted = values["ai_provider"]
    if not entitlements.has("ai_llm"):
        config = AIConfig("rule_based", values["ai_model"], values["ai_daily_request_cap"], "", "AI models are not included in this edition")
    elif wanted == "rule_based":
        config = AIConfig("rule_based", values["ai_model"], values["ai_daily_request_cap"], "", "rule-based planning selected")
    elif not key:
        config = AIConfig("rule_based", values["ai_model"], values["ai_daily_request_cap"], "", "no Anthropic API key set")
    else:
        config = AIConfig("anthropic", values["ai_model"], values["ai_daily_request_cap"], source)
    _AI_CACHE["ai"] = (time.monotonic(), config, key)
    return config, key
