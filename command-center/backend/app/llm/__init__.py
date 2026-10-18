from __future__ import annotations

import hashlib

from pydantic import SecretStr

from ..config import Settings
from .base import LLMBudgetExceeded, LLMError, LLMProvider, LLMRefused, LLMUnavailable, NullProvider

__all__ = [
    "LLMBudgetExceeded",
    "LLMError",
    "LLMProvider",
    "LLMRefused",
    "LLMUnavailable",
    "NullProvider",
    "build_provider",
    "runtime_provider",
]


def build_provider(settings: Settings) -> LLMProvider:
    if settings.anthropic_enabled:
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings)
    return NullProvider()


_RUNTIME: dict[tuple, LLMProvider] = {}


async def runtime_provider(session, settings: Settings) -> LLMProvider:
    """The provider the customer's AI settings currently call for (cached per key/model/cap)."""
    from ..customer_settings import resolve_ai

    config, key = await resolve_ai(session, settings)
    if config.provider != "anthropic":
        return NullProvider()
    cache_key = (hashlib.sha256(key.encode()).hexdigest(), config.model, config.daily_cap, settings.llm_effort)
    provider = _RUNTIME.get(cache_key)
    if provider is None:
        from .anthropic_provider import AnthropicProvider

        derived = settings.model_copy(
            update={
                "anthropic_api_key": SecretStr(key),
                "llm_model": config.model,
                "llm_daily_request_cap": config.daily_cap,
                "llm_provider": "anthropic",
            }
        )
        _RUNTIME.clear()
        provider = _RUNTIME[cache_key] = AnthropicProvider(derived)
    return provider
