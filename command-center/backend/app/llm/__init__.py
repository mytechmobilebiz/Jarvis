from __future__ import annotations

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
]


def build_provider(settings: Settings) -> LLMProvider:
    if settings.anthropic_enabled:
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings)
    return NullProvider()
